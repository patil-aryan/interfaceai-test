"""Turn a successful discovery run into a capability artifact.

The transcript is evidence. This is the compiled program: the recorded literals
become parameter references, every step gains a checkpoint, and the model's
claim of success becomes a condition the engine can assert on its own.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from computer_use.discovery.agent import DiscoveryOutcome, RecordedStep
from computer_use.discovery.spec import GoalSpec
from computer_use.schema.capability import (
    Action,
    ApprovalStatus,
    BusinessOutcome,
    CapabilityArtifact,
    Checkpoint,
    ContainerWithTextScope,
    ElementTarget,
    InputParam,
    LiteralValue,
    OnFailure,
    ParamRef,
    Provenance,
    Sensitivity,
    Step,
    TextPresent,
    ValueEquals,
    ValueSource,
    is_unanchored,
)

# A heading worth asserting: short, loud, and not something a value would be.
HEADING = re.compile(r"^[A-Z0-9][A-Z0-9 ./&'-]{3,48}$")

# Text that is different tomorrow, or for the next record. A checkpoint built
# out of any of it passes today and fails on a date it was never tested on.
VOLATILE = re.compile(r"\d{1,4}[/-]\d{1,2}|\d{2}:\d{2}|\b\d{4,}\b")

# A row's accessible name is every cell in it joined together, so anything this
# long is a paragraph rather than a caption.
MAX_HEADING_CHARS = 30

# Success is never a business outcome. A model proposes one anyway, and it would
# match on a perfectly good run and report it as something other than success.
NOT_OUTCOMES = {"SUCCESS", "OK", "COMPLETED", "COMPLETE", "FOUND", "DONE"}


def compile_artifact(spec: GoalSpec, run: DiscoveryOutcome) -> CapabilityArtifact:
    """Compile a successful run. Raises if the run did not reach the goal."""
    if not run.reached_goal:
        raise ValueError(f"cannot compile a run that did not reach its goal: {run.stop_reason}")

    run = run.model_copy(update={"steps": _without_self_corrections(run.steps)})
    inputs = _reconcile_inputs(spec, run)
    examples = {i.name: i.example for i in inputs if i.example}
    committing = _committing_step(run, spec)
    steps: list[Step] = []
    previous: dict[str, str] = {}

    for index, recorded in enumerate(run.steps):
        steps.append(_step(
            f"s{index + 1}", recorded, examples, previous, commits=index == committing
        ))
        previous = recorded.frames_after

    return CapabilityArtifact(
        id=spec.id,
        version=spec.version,
        title=spec.title,
        description=spec.description,
        app_profile=spec.app_profile,
        status=ApprovalStatus.DRAFT,
        risk_tier=spec.risk_tier,
        requires_authenticated_session=spec.requires_authenticated_session,
        inputs=inputs,
        outputs=spec.outputs,
        steps=steps,
        success_condition=_success_condition(spec, run),
        outcomes=[
            BusinessOutcome(
                code=o.code,
                description=o.description,
                detector=TextPresent(text=o.signal_text, frame=_busiest_frame(run)),
            )
            for o in spec.outcomes
            if o.code.upper() not in NOT_OUTCOMES
        ],
        provenance=Provenance(
            goal=spec.goal,
            discovery_run_id=run.run_id,
            model=run.model,
            recorded_at=datetime.now(UTC),
            recorded_against_institution=spec.institution,
            surface_kind=spec.surface_kind,
        ),
    )


def _reconcile_inputs(spec: GoalSpec, run: DiscoveryOutcome) -> list[InputParam]:
    """Correct the contract against what the application actually offered.

    The contract is written from the goal, before anything has been seen. A
    model asked to open a savings sub-account reasonably guesses the account
    type is "savings"; the screen calls it "SAVINGS" and offers "MONEY MARKET"
    and "HOLIDAY CLUB" beside it. The allowed values of an enum belong to the
    application, not to the sentence that asked for it.
    """
    inputs = [i.model_copy() for i in spec.inputs]

    for declared in inputs:
        # An identifier that selects exactly one record selects exactly one
        # person. Whether it is treated as identifying data must not depend on
        # a model choosing the right tag.
        if declared.unique_key and declared.sensitivity in (
            Sensitivity.PUBLIC, Sensitivity.INTERNAL
        ):
            declared.sensitivity = Sensitivity.PII

    by_lower = {(i.example or "").strip().lower(): i for i in inputs if i.example}

    for recorded in run.steps:
        if recorded.value is None:
            continue
        declared = by_lower.get(recorded.value.strip().lower())
        if declared is None:
            continue
        declared.example = recorded.value  # the wording the application uses
        if recorded.options:
            declared.values = recorded.options
            declared.pattern = "^(" + "|".join(re.escape(o) for o in recorded.options) + ")$"

    return inputs


def _committing_step(run: DiscoveryOutcome, spec: GoalSpec) -> int:
    """Which step actually commits, for a capability that writes.

    The last submitting action before the flow starts reading results. Marking
    every button press irreversible would make a guardrail that stops the run at
    the first menu click, which protects nothing and blocks everything.
    """
    from computer_use.schema.capability import RiskTier

    if spec.risk_tier is not RiskTier.IRREVERSIBLE_WRITE:
        return -1
    for index in range(len(run.steps) - 1, -1, -1):
        if run.steps[index].action in (Action.ACTIVATE, Action.PRESS):
            return index
    return -1


def _step(
    step_id: str, recorded: RecordedStep, examples: dict[str, str],
    previous: dict[str, str], *, commits: bool,
) -> Step:
    value = _parameterise(recorded.value, examples)
    target = _retarget(recorded.target, examples)
    return Step(
        id=step_id,
        intent=_generalise(recorded.intent, examples),
        action=recorded.action,
        target=target,
        value=value,
        output=recorded.output,
        checkpoint=_checkpoint(recorded, target, value, previous, examples),
        on_failure=_on_failure(recorded),
        irreversible=commits,
    )


def _addressing(target: ElementTarget | None) -> object:
    """A target stripped of its prose, so two descriptions of one control compare equal."""
    if target is None:
        return None
    return target.model_dump(mode="json", exclude={"description"})


def _without_self_corrections(steps: list[RecordedStep]) -> list[RecordedStep]:
    """Drop a value that was typed into a control and then cleared again.

    A model meeting an unfamiliar form types into the wrong box and empties it.
    That is the discovery loop working, but it is not part of the flow, and
    leaving it in is not merely untidy: here it put the surname in the field the
    application caps at six characters, so the compiled capability worked for
    VANCE and failed for RAGHAVAN.

    Only an immediately adjacent pair is removed. That is narrow enough that
    nothing the flow depends on can sit between the two halves, and it leaves a
    lone clearing step alone, which is a different thing: emptying a field the
    application arrived with already filled.
    """
    kept: list[RecordedStep] = []
    index = 0
    while index < len(steps):
        step = steps[index]
        following = steps[index + 1] if index + 1 < len(steps) else None
        undone = (
            following is not None
            and step.action is Action.FILL
            and following.action is Action.FILL
            and not (following.value or "").strip()
            and _addressing(step.target) == _addressing(following.target)
        )
        if undone:
            index += 2
            continue
        kept.append(step)
        index += 1
    return kept


def _retarget(target: ElementTarget | None, examples: dict[str, str]) -> ElementTarget | None:
    """Generalise a recorded target: its prose, and any scope naming a supplied value.

    The harvester records "the row containing 100253", because that is what it
    could verify against the screen in front of it. Left alone, the capability
    would select that member every time it ran, whatever it was called with.
    Promoting the scope to a parameter reference is what turns one recording
    into "the row for the member number I was given".
    """
    if target is None:
        return None
    update: dict[str, object] = {
        "description": _generalise(target.description, examples)
    }
    scope = target.scope
    if isinstance(scope, ContainerWithTextScope) and isinstance(scope.text, LiteralValue):
        promoted = _parameterise(scope.text.value, examples)
        if isinstance(promoted, ParamRef):
            update["scope"] = scope.model_copy(update={"text": promoted})
    return target.model_copy(update=update)


def _generalise(text: str, examples: dict[str, str]) -> str:
    """Put the parameter's name where the recorded value was.

    "Search for member 100234" describes one recording. "Search for member
    {member_number}" describes the capability, and stops a value from the
    discovery run travelling into a published artifact as prose.
    """
    for name, example in sorted(
        examples.items(), key=lambda pair: len(pair[1]), reverse=True
    ):
        if example:
            text = text.replace(example, "{" + name + "}")
    return text


def _parameterise(value: str | None, examples: dict[str, str]) -> ValueSource | None:
    """Promote a recorded literal to a parameter reference.

    The discovery run drove the UI with the example value each input declares,
    so a typed value that equals one of them is that input, not a constant. This
    is the whole reason the contract is written before the run rather than
    guessed after it.
    """
    if value is None:
        return None
    for name, example in examples.items():
        if value.strip().lower() == example.strip().lower():
            return ParamRef(param=name)
    return LiteralValue(value=value)


def _checkpoint(
    recorded: RecordedStep, target: ElementTarget | None, value: ValueSource | None,
    previous: dict[str, str], examples: dict[str, str],
) -> Checkpoint | None:
    """Assert that the step did what it claimed, using what actually changed.

    Takes the already-generalised target, so the copy nested inside the
    condition does not carry the recorded value back in through its description.
    """
    if recorded.action is Action.FILL and target is not None:
        return Checkpoint(
            condition=ValueEquals(target=target, expected=value or LiteralValue(value="")),
            description="the field holds the value that was entered",
        )

    if recorded.action is Action.READ:
        return None  # reading changes nothing, so there is nothing to assert

    heading = _new_heading(previous, recorded.frames_after, examples)
    if heading is None:
        return None
    text, frame = heading
    return Checkpoint(
        condition=TextPresent(text=text, frame=frame),
        description=f"the screen showing {text!r} is displayed",
    )


def _new_heading(
    before: dict[str, str], after: dict[str, str], examples: dict[str, str]
) -> tuple[str, list[str]] | None:
    """Find a stable heading that appeared as a result of this step."""
    candidates: list[tuple[int, str, list[str]]] = []
    for key, snapshot in after.items():
        was = before.get(key, "")
        frame = [part for part in key.split("/") if part]
        for line in snapshot.splitlines():
            # An accessibility snapshot line reads `- cell "MEMBER PROFILE":`.
            # The heading is the accessible name, not the whole line.
            quoted = re.search(r'"([^"]+)"', line)
            if quoted is None:
                continue
            text = quoted.group(1).strip()
            if not HEADING.fullmatch(text) or text in was:
                continue
            if VOLATILE.search(text):
                continue
            if any(example and example in text for example in examples.values()):
                continue
            if len(text) > MAX_HEADING_CHARS:
                continue
            candidates.append((text, frame))

    if not candidates:
        return None
    # The first surviving one, in document order. A screen announces itself at
    # the top, so the earliest short caption that is new is the screen's name.
    # Taking the shortest instead picked "OPEN", an account status buried in a
    # table, because it happened to be brief.
    return candidates[0]


def _on_failure(recorded: RecordedStep) -> OnFailure:
    """Where a declared outcome shows up, so test for one before calling it a fault.

    Submitting produces the application's own message. A read that cannot find
    what it came for produces nothing at all, and the missing thing is itself
    the answer: a member who holds no savings account has no savings row. In
    both cases a detector is consulted first, and the step still fails normally
    when none matches.
    """
    if recorded.action in (Action.ACTIVATE, Action.PRESS, Action.READ):
        return OnFailure.CLASSIFY
    return OnFailure.FAIL


def _success_condition(spec: GoalSpec, run: DiscoveryOutcome) -> Checkpoint:
    """Assert the goal, not merely that a screen loaded.

    The strong form compares an element on screen against the identifier the
    caller supplied, which is what distinguishes "a member profile loaded" from
    "the member you asked for loaded".
    """
    proof = run.proof or {}
    if run.proof_target is not None and proof.get("proof_input"):
        examples = {i.name: i.example for i in spec.inputs if i.example}
        return Checkpoint(
            condition=ValueEquals(
                target=_retarget(run.proof_target, examples),
                expected=ParamRef(param=proof["proof_input"]),
            ),
            description="the record on screen is the one that was requested",
        )

    text = proof.get("proof_text") or _last_heading(run)
    if text is not None and (VOLATILE.search(text) or len(text) > MAX_HEADING_CHARS):
        text = _last_heading(run)
    if text is None:
        raise ValueError(
            "the run finished without usable proof of success; an artifact without "
            "a success condition cannot be replayed safely"
        )
    return Checkpoint(
        condition=TextPresent(text=text, frame=_busiest_frame(run)),
        description=f"the screen showing {text!r} is displayed",
    )


def _last_heading(run: DiscoveryOutcome) -> str | None:
    if not run.steps:
        return None
    found = _new_heading({}, run.steps[-1].frames_after, {})
    return found[0] if found else None


def _busiest_frame(run: DiscoveryOutcome) -> list[str]:
    """Where this flow does its work, and so where its outcomes will appear."""
    for recorded in reversed(run.steps):
        if recorded.target is not None and recorded.target.frame:
            return recorded.target.frame
    return []


def fragile_targets(artifact: CapabilityArtifact) -> list[str]:
    """Steps addressed by a position with nothing to anchor it.

    The discovery loop refuses an unanchored *read* while there is still a model
    in the conversation that can be asked to point somewhere better. This is the
    backstop for every other action, and for artifacts that arrived some other
    way.
    """
    warnings: list[str] = []
    for step in artifact.steps:
        target = step.target
        if not is_unanchored(target):
            continue
        warnings.append(
            f"{step.id} ({step.action.value}) is addressed as {target.recorded_strategy} "
            f"number {target.nth} with nothing to anchor it; it will break when the "
            f"screen grows a row"
        )
    return warnings
