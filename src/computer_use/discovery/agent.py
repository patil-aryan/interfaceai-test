"""The observe, decide, act loop. The only place a model is in the decision path."""

from __future__ import annotations

import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from pydantic import BaseModel, Field

from computer_use.discovery.spec import GoalSpec
from computer_use.discovery.tools import (
    FINISH,
    frame_path,
    lookup_target,
    tool_definitions,
)
from computer_use.evidence.sink import EventSink, RunRecorder
from computer_use.guardrails.policy import Allowlist
from computer_use.schema.capability import Action, ElementTarget, is_unanchored
from computer_use.schema.event import EventType, Level, RunKind
from computer_use.surfaces.base import Surface, TargetNotFound

MAX_SNAPSHOT_CHARS = 6_000

# A read must return one value. Anything longer, or spanning lines, is a
# container: the model pointed at the table rather than at the cell.
MAX_READ_CHARS = 120

# Actions that replace what is on screen, and so must be waited out.
CHANGES_THE_SCREEN = (
    Action.NAVIGATE, Action.ACTIVATE, Action.DISMISS, Action.PRESS, Action.SELECT,
)

SYSTEM_PROMPT = """\
You are operating a legacy back-office banking application through its user
interface, the way a human operator would. There is no API.

You see the screen as an accessibility tree: the structure a screen reader
exposes. Each line is a role followed by that element's accessible name in
quotes. This application is served as a frameset, so the observation is split
into several frames and you must say which frame an element is in.

Work one step at a time. After every action you are shown the new screen. If an
action fails you are told why; look at the screen again and try a different way
of identifying the control rather than repeating yourself.

Rules that matter:
- Identify controls by role and accessible name. Never by a value that will be
  different next time. When you read a field, locate it by the row it sits in,
  never by the text it currently contains.
- Every action takes an `intent`: one plain sentence, written for a person who
  will review this saved flow months from now.
- This run is being compiled into a reusable capability that will be replayed
  with different inputs. Prefer the way of doing things that would still work
  for a different record.
- Call `done` only when the goal is met, and give proof that the screen shows
  the record that was requested.
- Never write a person's name, account number or balance into an `intent`. Those
  sentences are kept in the saved capability and read by people who are not
  entitled to that record. Describe the step, not the data: "read the savings
  balance", never "read Eleanor Vance's balance of 4182.55".
"""


class RecordedStep(BaseModel):
    """One action that worked, with the locator chain harvested at the time it worked."""

    action: Action
    intent: str
    target: ElementTarget | None = None
    value: str | None = None
    output: str | None = None
    read_value: str | None = None
    options: list[str] = Field(default_factory=list)
    frame: list[str] = Field(default_factory=list)
    urls_after: list[str] = Field(default_factory=list)
    frames_after: dict[str, str] = Field(default_factory=dict)


class DiscoveryOutcome(BaseModel):
    """What a discovery run produced, successful or not."""

    run_id: str
    model: str
    goal: str
    reached_goal: bool
    stop_reason: str
    summary: str = ""
    steps: list[RecordedStep] = Field(default_factory=list)
    proof: dict[str, Any] | None = None
    proof_target: ElementTarget | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    turns: int = 0
    duration_ms: int = 0
    evidence_dir: str = ""


class DiscoveryAgent:
    """Drives a surface with a model until the goal is met or a stop condition fires."""

    def __init__(
        self,
        surface: Surface,
        client: Any,
        *,
        base_url: str,
        spec: GoalSpec,
        policy: Allowlist,
        model: str,
        events: EventSink | None = None,
        evidence_root: Path | str = "evidence",
        sign_on_path: str | None = None,
    ):
        self._surface = surface
        self._client = client
        self._base_url = base_url.rstrip("/") + "/"
        self._spec = spec
        self._policy = policy
        self._model = model
        self._events = events
        self._evidence_root = Path(evidence_root)
        self._sign_on_path = sign_on_path

    async def run(self, run_id: str | None = None) -> DiscoveryOutcome:
        run_id = run_id or f"disc_{uuid.uuid4().hex[:10]}"
        run_dir = self._evidence_root / run_id
        sink = self._events or EventSink(run_dir)
        rec = RunRecorder(sink, run_id=run_id, run_kind=RunKind.DISCOVERY,
                          capability_id=self._spec.id)
        clock = time.monotonic()

        params = self._spec.example_params()
        rec.emit(EventType.RUN_STARTED, f"Discovery started for {self._spec.id}",
                 detail={"goal": self._spec.goal, "model": self._model,
                         "institution": self._spec.institution,
                         "inputs": {k: "<supplied>" for k in params}},
                 redacted_fields=[f"inputs.{k}" for k in params])

        outcome = DiscoveryOutcome(
            run_id=run_id, model=self._model, goal=self._spec.goal,
            reached_goal=False, stop_reason="not started",
            evidence_dir=str(run_dir),
        )

        tools = tool_definitions([o.name for o in self._spec.outputs])
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self._opening_brief(params)}
        ]

        outcome.stop_reason = f"reached the {self._spec.max_steps} step limit"
        for turn in range(1, self._spec.max_steps + 1):
            outcome.turns = turn
            rec.emit(EventType.MODEL_REQUESTED, f"Asking the model for turn {turn}",
                     detail={"turn": turn, "messages": len(messages)})

            response = await self._client.messages.create(
                model=self._model, max_tokens=2048,
                system=SYSTEM_PROMPT, tools=tools, messages=messages,
            )

            calls = [b for b in response.content if b.type == "tool_use"]
            said = " ".join(b.text for b in response.content if b.type == "text").strip()
            rec.emit(EventType.MODEL_RESPONDED,
                     said or f"{len(calls)} tool call(s)",
                     detail={"turn": turn, "stop_reason": response.stop_reason,
                             "tools": [b.name for b in calls]})

            if not calls:
                outcome.stop_reason = "the model stopped without acting"
                outcome.summary = said
                break

            messages.append({"role": "assistant", "content": response.content})
            results: list[dict[str, Any]] = []
            finished = False

            for call in calls:
                if call.name == FINISH:
                    target, complaint = await self._check_proof(rec, outcome, dict(call.input))
                    if complaint is not None:
                        rec.emit(EventType.CHECKPOINT_FAILED, "Proof of success rejected",
                                 level=Level.WARN, detail={"reason": complaint})
                        results.append({
                            "type": "tool_result", "tool_use_id": call.id,
                            "content": complaint, "is_error": True,
                        })
                        continue
                    outcome.reached_goal = True
                    outcome.stop_reason = "the model reported the goal met"
                    outcome.summary = call.input.get("summary", "")
                    outcome.proof = dict(call.input)
                    outcome.proof_target = target
                    rec.emit(EventType.CHECKPOINT_PASSED, "Goal reported met",
                             detail={"proof": _safe(dict(call.input), self._avoid(outcome)),
                                     "proof_verified": target is not None},
                             redacted_fields=["detail.proof.summary"])
                    finished = True
                    break

                text, ok = await self._perform(rec, call, outcome, turn)
                results.append({
                    "type": "tool_result", "tool_use_id": call.id,
                    "content": text, **({"is_error": True} if not ok else {}),
                })

            if finished:
                break
            messages.append({"role": "user", "content": results})

        outcome.duration_ms = int((time.monotonic() - clock) * 1000)
        rec.emit(
            EventType.RUN_FINISHED,
            f"Discovery finished: {'goal met' if outcome.reached_goal else 'goal not met'}",
            level=Level.INFO if outcome.reached_goal else Level.WARN,
            detail={"stop_reason": outcome.stop_reason, "turns": outcome.turns,
                    "steps_recorded": len(outcome.steps)},
        )
        return outcome

    async def _check_proof(
        self, rec: RunRecorder, outcome: DiscoveryOutcome, proof: dict[str, Any]
    ) -> tuple[ElementTarget | None, str | None]:
        """Check the claim of success on the live page before accepting it.

        Returns the harvested locator, or a complaint to send back to the model.
        A proof pointing at the whole screen is not a proof: comparing an entire
        screen against a member number can never be true, and the capability
        would fail on its very first replay.
        """
        if proof.get("proof_kind") != "element_equals_input":
            return None, _text_proof_complaint(proof.get("proof_text"))
        if not proof.get("proof_input") or not proof.get("role"):
            return None, ("A proof of kind element_equals_input needs both `proof_input` "
                          "and the element's `role` and `frame`.")

        declared = {i.name for i in self._spec.inputs}
        if proof["proof_input"] not in declared:
            return None, (
                f"{proof['proof_input']!r} is not an input of this capability. A proof "
                f"compares the screen against something the caller supplied, so it must "
                f"name one of {sorted(declared)}. Comparing against a value this run just "
                f"read proves only that the screen agrees with itself. If no input is "
                f"shown on this screen, use proof_kind text_present with the screen's "
                f"fixed heading instead."
            )

        try:
            target = lookup_target(proof, "The record shown is the one that was requested")
            handle, _ = await self._surface.resolve(target, {})
            value = (await self._surface.read(handle)).strip()
        except Exception as exc:
            return None, (f"That proof element could not be found: "
                          f"{type(exc).__name__}: {exc}. Point at the single cell that "
                          f"displays the identifier.")

        expected = self._spec.example_params().get(proof["proof_input"], "")
        if "\n" in value or len(value) > MAX_READ_CHARS:
            return None, (
                f"That proof points at a container holding {len(value)} characters, not at "
                f"one value. Comparing a whole screen against {proof['proof_input']} can "
                f"never be true. Use `within_row` and `nth` to name the single cell showing "
                f"the identifier."
            )
        if expected and value != expected:
            return None, (
                f"That element shows {value!r}, but {proof['proof_input']} is {expected!r}. "
                f"Point at the cell that displays the identifier the caller supplied."
            )

        harvested = await self._surface.describe_target(
            handle, target.frame, target.description,
            content_varies=True, avoid=self._avoid(outcome),
            anchors=self._anchors(),
        )
        return harvested, None

    # -- one tool call -----------------------------------------------------

    async def _perform(
        self, rec: RunRecorder, call: Any, outcome: DiscoveryOutcome, turn: int
    ) -> tuple[str, bool]:
        """Carry out one tool call. Returns what to tell the model, and whether it worked."""
        args = dict(call.input)
        intent = args.get("intent", call.name)
        known = self._avoid(outcome)
        rec.emit(EventType.ACTION_ATTEMPTED, _scrub(intent, known),
                 step_index=len(outcome.steps),
                 rationale=f"model chose {call.name}",
                 detail={"turn": turn, "args": _safe(args, known)},
                 redacted_fields=["detail.args", "message"] if known else [])

        try:
            action = Action(call.name)
        except ValueError:
            return f"{call.name} is not an action you can take.", False

        blocked = self._refuse(rec, action, args, outcome)
        if blocked is not None:
            return blocked, False

        # An action that submits a form is not finished when the click returns.
        # It is finished when the screen it produced has stopped changing.
        before = await self._signature() if action in CHANGES_THE_SCREEN else None
        try:
            step = await self._act(
                action, args, intent, self._avoid(outcome), self._anchors()
            )
            if before is not None:
                await self._surface.settle(changed_from=before)
        except TargetNotFound as exc:
            rec.emit(EventType.ACTION_FAILED, f"Could not find the control for: {intent}",
                     level=Level.WARN, detail={"attempts": exc.attempts})
            return (f"No control matched that description. Strategies tried: "
                    f"{'; '.join(exc.attempts)}. Look at the screen below and "
                    f"identify it differently.\n\n{await self._render()}"), False
        except Exception as exc:
            rec.emit(EventType.ACTION_FAILED, f"{type(exc).__name__}: {exc}", level=Level.WARN)
            return f"That failed: {type(exc).__name__}: {exc}\n\n{await self._render()}", False

        landing = await self._refuse_landing(rec, outcome)
        if landing is not None:
            return landing, False

        too_wide = _not_a_single_value(step)
        if too_wide is not None:
            rec.emit(EventType.ACTION_FAILED, f"Read returned a container, not a value: {intent}",
                     level=Level.WARN, detail={"chars": len(step.read_value or "")})
            return too_wide, False

        adrift = _not_anchored(step)
        if adrift is not None:
            rec.emit(EventType.ACTION_FAILED, f"Read is held in place by nothing: {intent}",
                     level=Level.WARN,
                     detail={"nth": step.target.nth if step.target else None})
            return adrift, False

        mistyped = self._wrong_type(step)
        if mistyped is not None:
            rec.emit(EventType.ACTION_FAILED, f"Read does not match its declared type: {intent}",
                     level=Level.WARN, detail={"output": step.output})
            return mistyped, False

        step.urls_after = await self._safe_urls()
        observation = await self._surface.observe()
        step.frames_after = {k: _clip(v) for k, v in observation.frames.items()}
        outcome.steps.append(step)

        if step.output and step.read_value is not None:
            outcome.outputs[step.output] = step.read_value

        rec.emit(EventType.ACTION_SUCCEEDED, f"{action.value} completed",
                 step_index=len(outcome.steps) - 1,
                 detail={"recorded_strategy": step.target.recorded_strategy if step.target else None,
                         "scope": bool(step.target and step.target.scope)})
        return await self._render(observation), True

    async def _act(
        self, action: Action, args: dict[str, Any], intent: str,
        avoid: tuple[str, ...] = (), anchors: tuple[str, ...] = (),
    ) -> RecordedStep:
        """Do it, and harvest the locator chain while the element is still in front of us."""
        surface = self._surface

        if action is Action.NAVIGATE:
            await surface.navigate(urljoin(self._base_url, args["path"]))
            return RecordedStep(action=action, intent=intent, value=args["path"])

        if action is Action.WAIT_FOR:
            from computer_use.schema.capability import TextPresent
            condition = TextPresent(text=args["text"], frame=frame_path(args.get("frame")))
            held, observed = await surface.wait_for(condition, {}, 10_000)
            if not held:
                raise TimeoutError(observed)
            return RecordedStep(action=action, intent=intent, value=args["text"],
                                frame=frame_path(args.get("frame")))

        if action is Action.PRESS:
            await surface.press(args["key"])
            return RecordedStep(action=action, intent=intent, value=args["key"])

        target = lookup_target(args, intent)
        handle, _ = await surface.resolve(target, {})
        reading = action is Action.READ

        # Harvest and read first. Activating a link navigates away and detaches
        # the element, so anything we want to know about it must be asked now.
        recorded = await surface.describe_target(
            handle, target.frame, intent, content_varies=reading,
            avoid=avoid, anchors=anchors,
        )
        value = await surface.read(handle) if reading else None

        offered: list[str] = []
        if action is Action.SELECT:
            offered = await surface.options(handle)

        if action is Action.FILL:
            await surface.fill(handle, args["value"])
        elif action is Action.SELECT:
            await surface.select(handle, args["option"])
        elif action in (Action.ACTIVATE, Action.DISMISS):
            await surface.activate(handle)

        return RecordedStep(
            action=action, intent=intent, target=recorded, frame=target.frame,
            value=args.get("value") or args.get("option"),
            output=args.get("output") if reading else None,
            read_value=value.strip() if value is not None else None,
            options=offered,
        )

    def _wrong_type(self, step: RecordedStep) -> str | None:
        """Reject a read whose value cannot be the type the contract declares.

        Both of these cells sit in the row captioned SAVINGS, so both are
        properly anchored and neither looks wrong on its own. Only the contract
        knows that `savings_balance` is a decimal, which makes "0001-100253-01"
        the account number one column too far left. This is the declared type
        earning its keep at the moment it can still be acted on.
        """
        if step.action is not Action.READ or step.output is None:
            return None
        if step.read_value is None:
            return None
        declared = next((o for o in self._spec.outputs if o.name == step.output), None)
        if declared is None:
            return None
        value = step.read_value.strip()

        if declared.type == "decimal":
            try:
                Decimal(value.replace(",", "").replace("$", ""))
            except InvalidOperation:
                return (
                    f"{step.output} is declared as a decimal amount, and that cell holds "
                    f"{value!r}, which is not a number. You have the right row but the "
                    f"wrong column. Raise `nth` to move further along the row until you "
                    f"reach the cell under the amount heading."
                )
        if declared.type == "enum" and declared.values and value not in declared.values:
            allowed = ", ".join(declared.values)
            return (
                f"{step.output} must be one of: {allowed}. That cell holds {value!r}. "
                f"You have the right row but the wrong column; change `nth` to reach the "
                f"cell holding one of those values."
            )
        return None

    def _anchors(self) -> tuple[str, ...]:
        """Supplied values that identify one record, and so may name a container.

        The general rule is that no locator is built out of a value this run
        touched. A unique key is the one exception worth making: "the SELECT
        link in the row for the member number I was given" is only expressible
        by naming that row, and the number is a parameter, so replay substitutes
        whichever one it was called with. A non-unique value such as a surname
        is excluded by definition, because it matches more than one row.
        """
        return tuple(
            i.example for i in self._spec.inputs if i.unique_key and i.example
        )

    def _avoid(self, outcome: DiscoveryOutcome) -> tuple[str, ...]:
        """Every value this run has touched. No locator may be built out of any of them.

        The inputs it drives the UI with, and also everything already read back:
        an extracted value is by definition different on the next invocation, so
        a locator or scope built from one would only ever find this record.
        """
        values = list(self._spec.example_params().values())
        values += list(outcome.outputs.values())
        return tuple(v for v in values if v)

    # -- guardrails --------------------------------------------------------

    def _refuse(
        self, rec: RunRecorder, action: Action, args: dict[str, Any], outcome: DiscoveryOutcome
    ) -> str | None:
        """Refuse an action the allowlist does not permit, and say so to the model.

        Signing in is refused outright, whatever the allowlist says. A session is
        a precondition established by the app profile from credentials the model
        never sees. A model left at a sign-on screen will try to get past it, and
        that is not a behaviour worth recording into a capability.
        """
        going_to_sign_on = (
            self._sign_on_path
            and action is Action.NAVIGATE
            and args.get("path", "").rstrip("/").endswith(self._sign_on_path.rstrip("/"))
        )
        if going_to_sign_on:
            rec.emit(EventType.POLICY_BLOCKED, "Refused: navigating to sign-on",
                     level=Level.ERROR, step_index=len(outcome.steps))
            return ("Signing in is not part of any capability and is refused. The "
                    "session is already established. If the screen is asking you to "
                    "sign in, say so and stop rather than attempting it.")

        decision = self._policy.check_action(action)
        if decision.allowed and action is Action.NAVIGATE:
            decision = self._policy.check_url(urljoin(self._base_url, args.get("path", "")))
        if decision.allowed:
            return None
        rec.emit(EventType.POLICY_BLOCKED, f"Refused: {action.value}", level=Level.ERROR,
                 step_index=len(outcome.steps), detail={"reason": decision.reason})
        return f"Refused by policy: {decision.reason}. Choose a different approach."

    async def _refuse_landing(
        self, rec: RunRecorder, outcome: DiscoveryOutcome
    ) -> str | None:
        """Refuse a page reached indirectly, such as by following a link."""
        for url in await self._safe_urls():
            decision = self._policy.check_url(url)
            if not decision.allowed:
                rec.emit(EventType.POLICY_BLOCKED,
                         f"Landed outside the allowlist: {_scrub(url, self._avoid(outcome))}",
                         level=Level.ERROR, detail={"reason": decision.reason})
                return f"That took the session somewhere policy forbids: {decision.reason}."
        return None

    async def _signature(self) -> str | None:
        try:
            return await self._surface.signature()
        except Exception:
            return None

    async def _safe_urls(self) -> list[str]:
        try:
            return await self._surface.current_urls()
        except Exception:
            return []

    # -- what the model sees ----------------------------------------------

    def _opening_brief(self, params: dict[str, str]) -> str:
        lines = [f"Goal: {self._spec.goal}", ""]
        if params:
            lines.append("Use exactly these values:")
            lines += [f"  {k} = {v}" for k, v in params.items()]
            lines.append("")
        lines.append("Values you must extract, using the `read` tool:")
        lines += [f"  {o.name} ({o.type}) - {o.description}" for o in self._spec.outputs]
        lines.append("")
        lines.append("You are signed in already. Here is the screen:")
        return "\n".join(lines)

    async def _render(self, observation: Any = None) -> str:
        """The current screen, as the accessibility tree of every frame."""
        if observation is None:
            observation = await self._surface.observe()
        parts = [f"URL: {observation.url}", f"Title: {observation.title}"]
        for path, snapshot in observation.frames.items():
            parts.append(f"\n--- frame: {path or '(main document)'} ---\n{_clip(snapshot)}")
        return "\n".join(parts)


def _clip(text: str, limit: int = MAX_SNAPSHOT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def _safe(args: dict[str, Any], known: tuple[str, ...]) -> dict[str, Any]:
    """Keep the shape of the call in the log without echoing anything sensitive.

    The typed value is replaced outright. Every other argument is scrubbed of
    values this run has supplied or already read, because a row caption chosen
    by the model can contain a member number just as easily as the field can.
    """
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key in ("value", "summary", "note"):
            # Free prose about a live screen cannot be sanitised: it can name a
            # record this run never read and so cannot recognise.
            out[key] = f"<{key} withheld>"
        elif isinstance(value, str):
            out[key] = _scrub(value, known)
        else:
            out[key] = value
    return out


def _scrub(text: str, known: tuple[str, ...]) -> str:
    """Replace anything this run supplied or read with a marker."""
    for value in sorted((v for v in known if v and len(v) >= 4), key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    return text


def _not_anchored(step: RecordedStep) -> str | None:
    """Reject a read that could only be found again by counting, and say how to fix it.

    Every cell on these screens has role `cell`, so a cell picked out purely by
    its number is the 27th cell on the screen the recording happened to see. It
    is also the shape a caption produces: a caption is the first cell in its
    row, so there is no label beside it and no row text to scope by, and the
    harvester has nothing to hold on to. Catching it here, rather than warning
    about it after the run, means the model can point somewhere better while it
    is still looking at the screen.
    """
    if step.action is not Action.READ or step.output is None:
        return None
    if not is_unanchored(step.target):
        return None
    return (
        "That read can only be found again by counting cells from the top of the "
        "screen, so it would break as soon as the record has one more row than "
        "this one. It is also what happens when you point at a caption rather "
        "than the value, because a caption is the first cell in its row and has "
        "nothing beside it to name it by. Point at the cell holding the value, "
        "usually immediately to the right of its caption, and pass `within_row` "
        "with that caption's text."
    )


def _not_a_single_value(step: RecordedStep) -> str | None:
    """Reject a read that grabbed a whole table, and say how to narrow it."""
    if step.output is None or step.read_value is None:
        return None
    value = step.read_value
    if "\n" not in value and len(value) <= MAX_READ_CHARS:
        return None
    return (
        f"That read returned {len(value)} characters spanning the whole screen, "
        f"which means you selected a container rather than the single value. "
        f"Narrow it: pass `within_row` with the caption text beside the value you "
        f"want, and `nth` to pick the cell within that row. It starts: "
        f"{value[:80]!r}"
    )


# Text that differs between invocations: dates, times, and long identifiers such
# as the account number a run has just created.
VOLATILE_PROOF = re.compile(r"\d{1,4}[/-]\d{1,2}|\d{2}:\d{2}|\b\d{4,}\b")
MAX_PROOF_TEXT = 40


def _text_proof_complaint(text: str | None) -> str | None:
    """Reject a proof of success built out of something that changes every run."""
    if not text:
        return ("A proof of kind text_present needs `proof_text`. Better still, use "
                "element_equals_input and point at the cell showing the identifier "
                "the caller supplied.")
    if VOLATILE_PROOF.search(text):
        return (
            f"{text!r} contains a value that is different on every run, such as a date "
            f"or a newly created account number. A success condition built from it would "
            f"hold once and never again. Give the screen's fixed heading instead, or use "
            f"element_equals_input."
        )
    if len(text) > MAX_PROOF_TEXT:
        return (
            f"{text!r} is the whole screen rather than a heading. Give the short fixed "
            f"caption that names this screen."
        )
    return None
