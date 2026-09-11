"""Executes a capability artifact against a surface. No model is involved."""

from __future__ import annotations

import itertools
import re
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from pydantic import BaseModel, Field

from computer_use.escalation.handoff import (
    HandoffBroker,
    InterventionRequest,
    new_request_id,
)
from computer_use.evidence.sink import EventSink, RunRecorder
from computer_use.guardrails.policy import Allowlist
from computer_use.schema.capability import (
    Action,
    BusinessOutcome,
    CapabilityArtifact,
    InputParam,
    OnFailure,
    RiskTier,
    Sensitivity,
    Step,
)
from computer_use.schema.event import EventType, EvidenceRef, Level, RunKind
from computer_use.schema.profile import AppProfile, KnownScreen
from computer_use.schema.result import (
    Actor,
    BusinessOutcomeResult,
    ControlEvent,
    Degradation,
    FailureClass,
    FailureDetail,
    FailureResult,
    Recovery,
    RecoveryKind,
    ReplayResult,
    SuccessResult,
)
from computer_use.surfaces.base import Surface, SurfaceError, TargetNotFound, resolve_value

# How many times one run may clear a recognised screen before concluding that
# clearing it is not working.
# Least to most restrictive, so that one value seen under two tags takes the
# stricter of the two.
STRICTNESS = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.PII: 2,
    Sensitivity.SECRET: 3,
}

MAX_RECOVERIES = 3

# How many times a read-only capability may be replayed from the top after the
# session was restored.
MAX_RESTARTS = 1


def mask(value: str, sensitivity: Sensitivity) -> str:
    """Redact a value for the log while leaving enough to correlate runs."""
    if sensitivity is Sensitivity.SECRET:
        return "***"
    if sensitivity is Sensitivity.PII:
        text = str(value)
        return text[:4] + "*" * max(len(text) - 4, 2)
    return str(value)


class ReplayEngine:
    """Walks an artifact's steps, verifies each one, and returns a typed result."""

    def __init__(
        self,
        surface: Surface,
        *,
        base_url: str,
        events: EventSink | None = None,
        evidence_root: Path | str = "evidence",
        policy: Allowlist | None = None,
        profile: AppProfile | None = None,
        credentials: tuple[str, str] | None = None,
        handoff: HandoffBroker | None = None,
        confirm_irreversible: bool = True,
        stop_before_irreversible: bool = False,
    ):
        self._surface = surface
        self._base_url = base_url.rstrip("/") + "/"
        self._events = events
        self._evidence_root = Path(evidence_root)
        self._policy = policy
        self._profile = profile
        self._credentials = credentials
        self._handoff = handoff
        self._confirm_irreversible = confirm_irreversible
        self._stop_before_irreversible = stop_before_irreversible

    # -- entry point -------------------------------------------------------

    async def run(
        self,
        artifact: CapabilityArtifact,
        params: dict[str, Any],
        *,
        institution: str,
        run_id: str | None = None,
        unattended: bool = True,
    ) -> ReplayResult:
        run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"
        run_dir = self._evidence_root / run_id
        sink = self._events or EventSink(run_dir)
        rec = RunRecorder(sink, run_id=run_id, run_kind=RunKind.REPLAY, capability_id=artifact.id)

        started = datetime.now(UTC)
        clock = time.monotonic()
        state = _RunState(
            artifact=artifact, run_id=run_id, institution=institution,
            started_at=started, run_dir=run_dir,
        )

        safe_params, redacted = self._redact(artifact, params)
        rec.emit(
            EventType.RUN_STARTED,
            f"Replay started for {artifact.id} v{artifact.version}",
            detail={"institution": institution, "inputs": safe_params,
                    "approval": artifact.status.value, "risk_tier": artifact.risk_tier.value},
            redacted_fields=redacted,
        )

        if self._policy is not None:
            decision = self._policy.check_artifact(
                artifact, unattended=unattended,
                will_commit=not self._stop_before_irreversible,
            )
            if not decision.allowed:
                rec.emit(EventType.POLICY_BLOCKED, "Replay refused by policy",
                         level=Level.ERROR, detail={"reason": decision.reason})
                return self._failure(
                    state, clock,
                    FailureDetail(
                        classification=FailureClass.POLICY_BLOCKED,
                        expected=f"a capability permitted by allowlist {self._policy.name!r}",
                        observed=decision.reason,
                    ),
                )
            rec.emit(EventType.POLICY_ALLOWED, f"Permitted by allowlist {self._policy.name!r}",
                     detail={"reason": decision.reason, "unattended": unattended})

        session = await self._establish_session(state, rec, artifact)
        if session is not None:
            return self._failure(state, clock, session)

        problems = self._validate_params(artifact, params)
        if problems:
            rec.emit(EventType.RUN_FINISHED, "Replay rejected: invalid inputs",
                     level=Level.ERROR, detail={"problems": problems})
            return self._failure(
                state, clock,
                FailureDetail(
                    classification=FailureClass.INPUT_INVALID,
                    expected="arguments matching the capability's declared inputs",
                    observed="; ".join(problems),
                ),
            )

        for _ in range(MAX_RESTARTS + 1):
            verdict = await self._walk_steps(state, rec, artifact, params)
            if verdict is None:
                return self._finish(
                    state, clock, rec, await self._verify_success(state, rec, params)
                )
            if not verdict.restart:
                return self._finish(state, clock, rec, verdict)
            rec.emit(EventType.RECOVERY_ATTEMPTED,
                     "Signed in again; replaying this read-only capability from its first step",
                     detail={"steps_discarded": state.steps_completed})
            state.steps_completed = 0
            state.outputs = {}

        return self._finish(state, clock, rec, _Verdict(failure=FailureDetail(
            classification=FailureClass.SESSION_LOST,
            expected="a session that survives the length of this capability",
            observed=f"the session was lost and restored {MAX_RESTARTS} times without finishing",
        )))

    async def _walk_steps(
        self, state: _RunState, rec: RunRecorder,
        artifact: CapabilityArtifact, params: dict[str, Any],
    ) -> _Verdict | None:
        """Run every step in order. Returns None when they all passed."""
        for index, step in enumerate(artifact.steps):
            verdict = await self._run_step(state, rec, step, index, params)
            if verdict is not None:
                return verdict
            state.steps_completed = index + 1
        return None

    async def _verify_success(
        self, state: _RunState, rec: RunRecorder, params: dict[str, Any]
    ) -> _Verdict:
        """Assert the capability's own success condition, once, after the last step."""
        condition = state.artifact.success_condition
        held, observed = await self._surface.wait_for(
            condition.condition, params, condition.timeout_ms
        )
        if held:
            rec.emit(EventType.CHECKPOINT_PASSED, "Success condition held")
            return _Verdict()

        outcome = await self._detect_outcome(state.artifact, params)
        if outcome is not None:
            rec.emit(EventType.OUTCOME_DETECTED,
                     f"Declared business outcome {outcome.code} matched at the end of the flow")
            return _Verdict(outcome=outcome)

        evidence = await self._capture(state, "success-condition")
        return _Verdict(failure=FailureDetail(
            classification=FailureClass.SUCCESS_CONDITION_FAILED,
            expected=condition.description or f"success condition: {condition.condition.kind}",
            observed=observed,
            screenshot_path=evidence.screenshot_path,
            snapshot_path=evidence.snapshot_path,
        ))

    # -- one step ----------------------------------------------------------

    async def _run_step(
        self, state: _RunState, rec: RunRecorder, step: Step, index: int, params: dict[str, Any]
    ) -> _Verdict | None:
        """Run one step to conclusion. Returns a verdict only if the run must stop."""
        blocked = self._check_allowlist(rec, step, index, params)
        if blocked is not None:
            return _Verdict(failure=blocked)

        if step.irreversible and self._stop_before_irreversible:
            rec.emit(EventType.RUN_FINISHED,
                     f"Stopping before the first step that commits: {step.intent}",
                     step_id=step.id, step_index=index)
            return _Verdict(failure=self._step_failure(
                step, index, FailureClass.POLICY_BLOCKED,
                expected="a run that stops before committing anything",
                observed="reached the first irreversible step and stopped, as asked",
            ))

        if step.irreversible and self._confirm_irreversible:
            verdict = await self._escalate(
                state, rec, step, index,
                FailureDetail(
                    classification=FailureClass.ESCALATION_UNANSWERED,
                    step_id=step.id, step_index=index, step_intent=step.intent,
                    expected="a person to authorise an irreversible step",
                    observed="the step was reached and has not been performed",
                ),
                why="this step commits something that cannot be undone",
                ask=("Check the entry on screen. Reply resume to let the automation "
                     "commit it, or abort to stop."),
                proceed_on_resume=True,
            )
            if verdict is not None:
                return verdict

        for attempt in itertools.count(1):
            rec.emit(EventType.ACTION_ATTEMPTED, step.intent, step_id=step.id, step_index=index,
                     rationale=f"recorded step: {step.action.value}",
                     detail={"attempt": attempt} if attempt > 1 else None)
            began = time.monotonic()

            failure = await self._attempt(state, rec, step, index, params)
            if failure is None:
                failure = await self._verify_checkpoint(state, rec, step, index, params)

            if failure is None:
                rec.emit(EventType.ACTION_SUCCEEDED, f"{step.action.value} completed",
                         step_id=step.id, step_index=index,
                         duration_ms=int((time.monotonic() - began) * 1000))
                landed = await self._check_landing(rec, step, index)
                if landed is not None:
                    return _Verdict(failure=landed)
                return None

            known = await self._handle_known_screen(state, rec, step, index, failure)
            if known is not None:
                if known.retry:
                    continue
                return known                    # includes a request to restart

            verdict = await self._apply_failure_policy(state, rec, step, index, params, failure, attempt)
            if verdict is None:          # the step was optional and was skipped
                return None
            if verdict.retry:
                continue
            return verdict

    def _check_allowlist(
        self, rec: RunRecorder, step: Step, index: int, params: dict[str, Any]
    ) -> FailureDetail | None:
        """Refuse a step the allowlist does not permit. Returns None when permitted."""
        if self._policy is None:
            return None
        url = None
        if step.action is Action.NAVIGATE:
            url = urljoin(self._base_url, resolve_value(step.value, params))
        decision = self._policy.check_step(step, url)
        if decision.allowed:
            return None
        rec.emit(EventType.POLICY_BLOCKED, f"Step refused by policy: {step.intent}",
                 level=Level.ERROR, step_id=step.id, step_index=index,
                 detail={"reason": decision.reason})
        return self._step_failure(
            step, index, FailureClass.POLICY_BLOCKED,
            expected=f"a step permitted by allowlist {self._policy.name!r}",
            observed=decision.reason,
        )

    async def _escalate(
        self, state: _RunState, rec: RunRecorder, step: Step, index: int,
        failure: FailureDetail, *, why: str, ask: str, proceed_on_resume: bool,
    ) -> _Verdict | None:
        """Hand the live session to a person, wait, and take it back.

        The browser is not closed and no new session is made. The automation
        simply stops driving the window it already has, so whatever the operator
        does happens in the same session, with the same cookies, on the same
        screen the automation was looking at.
        """
        evidence = await self._capture(state, f"{step.id}-escalation")
        detail = failure.model_copy(update={
            "screenshot_path": evidence.screenshot_path,
            "snapshot_path": evidence.snapshot_path,
        })

        if self._handoff is None:
            rec.emit(EventType.ESCALATION_RAISED,
                     f"Needs a human, and no operator channel is configured: {step.intent}",
                     level=Level.ERROR, step_id=step.id, step_index=index)
            return _Verdict(failure=detail.model_copy(update={
                "classification": FailureClass.ESCALATION_UNANSWERED,
                "observed": f"{failure.observed} (no operator channel was configured)",
            }), escalated=True)

        request = InterventionRequest(
            request_id=new_request_id(),
            raised_at=datetime.now(UTC),
            run_id=state.run_id, run_dir=str(state.run_dir),
            capability_id=state.artifact.id,
            capability_title=state.artifact.title,
            goal=state.artifact.provenance.goal,
            risk_tier=state.artifact.risk_tier.value,
            step_id=step.id, step_index=index, step_intent=step.intent,
            why=why, expected=detail.expected, observed=detail.observed,
            asked_of_operator=ask,
            screenshot_path=detail.screenshot_path,
            snapshot_path=detail.snapshot_path,
        )
        self._handoff.publish(request)
        rec.emit(EventType.ESCALATION_RAISED, f"Operator asked to take over: {why}",
                 level=Level.WARN, step_id=step.id, step_index=index,
                 detail={"request_id": request.request_id,
                         "request_file": str(self._handoff.request_path)},
                 evidence=evidence)

        before = await self._safe_signature()
        state.control_events.append(ControlEvent(
            at=datetime.now(UTC), from_actor=Actor.AGENT,
            to_actor=Actor.OPERATOR, reason=why,
        ))
        rec.emit(EventType.CONTROL_TRANSFERRED, "The session is now the operator's",
                 actor=Actor.SYSTEM, step_id=step.id, step_index=index,
                 detail={"request_id": request.request_id})

        answer = await self._handoff.wait(request.request_id)

        if answer is None:
            rec.emit(EventType.CONTROL_TRANSFERRED,
                     "Nobody answered; the session returns to the automation",
                     level=Level.ERROR, actor=Actor.SYSTEM)
            state.control_events.append(ControlEvent(
                at=datetime.now(UTC), from_actor=Actor.OPERATOR,
                to_actor=Actor.AGENT, reason="the request went unanswered",
            ))
            return _Verdict(failure=detail.model_copy(update={
                "classification": FailureClass.ESCALATION_UNANSWERED,
                "observed": f"{detail.observed} (no operator answered in "
                            f"{self._handoff.wait_seconds}s)",
            }), escalated=True)

        after = await self._safe_signature()
        changed = before != after
        rec.emit(EventType.OPERATOR_ACTION,
                 f"{answer.operator} chose to {answer.decision}"
                 + (f": {answer.note}" if answer.note else ""),
                 actor=Actor.OPERATOR, step_id=step.id, step_index=index,
                 detail={"request_id": request.request_id,
                         "changed_the_screen": changed,
                         "urls": [
                             mask(u, Sensitivity.PII) if any(
                                 str(v) and str(v) in u for v in state.outputs.values()
                             ) else u
                             for u in await self._safe_urls()
                         ]})
        state.control_events.append(ControlEvent(
            at=datetime.now(UTC), from_actor=Actor.OPERATOR,
            to_actor=Actor.AGENT, reason=f"operator chose to {answer.decision}",
            operator_ref=answer.operator,
            operator_actions_recorded=1 if changed else 0,
        ))
        rec.emit(EventType.CONTROL_TRANSFERRED, "The automation has the session again",
                 actor=Actor.SYSTEM, step_id=step.id, step_index=index)

        if answer.decision != "resume":
            stopped = f"{answer.operator} stopped the run" + (
                f": {answer.note}" if answer.note else ""
            )
            if changed:
                # A person had the live session and used it. "Stopped" would tell
                # the caller nothing happened, and something did. What they did is
                # not knowable from here, only that the screen is not where the
                # automation left it.
                stopped += (
                    "; the session was changed while they held it, so the "
                    "application may not be in the state this run started from"
                )
            return _Verdict(failure=detail.model_copy(update={
                "classification": FailureClass.ESCALATION_ABANDONED,
                "observed": stopped,
            }), escalated=True)

        return None if proceed_on_resume else _Verdict(retry=True)

    async def _safe_signature(self) -> str:
        try:
            return await self._surface.signature()
        except Exception:
            return ""

    async def _safe_urls(self) -> list[str]:
        try:
            return await self._surface.current_urls()
        except Exception:
            return []

    async def _establish_session(
        self, state: _RunState, rec: RunRecorder, artifact: CapabilityArtifact
    ) -> FailureDetail | None:
        """Sign in, if this capability says it needs a session and we hold credentials.

        Inside the engine rather than in the caller, because the engine promises
        to always return a result. A sign-on screen that never appears because
        the product is down is a failure to report, not an exception to raise.
        """
        if not artifact.requires_authenticated_session or self._credentials is None:
            return None
        try:
            if await self._sign_on():
                rec.emit(EventType.RECOVERY_ATTEMPTED, "Session established")
                return None
            observed = "the sign-on screen did not lead to a signed-in session"
        except Exception as exc:
            observed = f"{type(exc).__name__}: {exc}"

        screen = await self._match_known_screen()
        classification = FailureClass.SESSION_LOST
        if screen is not None and screen.classification is not None:
            classification = screen.classification
            observed = f"{screen.name} ({observed})"

        rec.emit(EventType.RUN_FINISHED, "Replay could not establish a session",
                 level=Level.ERROR, detail={"classification": classification.value,
                                            "observed": observed})
        evidence = await self._capture(state, "sign-on")
        return FailureDetail(
            classification=classification,
            expected="a signed-in session before the first step",
            observed=observed,
            screenshot_path=evidence.screenshot_path,
            snapshot_path=evidence.snapshot_path,
        )

    async def _handle_known_screen(
        self, state: _RunState, rec: RunRecorder, step: Step, index: int,
        failure: FailureDetail,
    ) -> _Verdict | None:
        """Ask the app profile whether this screen means something it knows about.

        A step does not fail because the application is broken. It fails because
        something is on the screen. The profile is what turns "the checkpoint did
        not hold" into "the session expired" or "this operator lacks the right".
        """
        if self._profile is None:
            return None
        screen = await self._match_known_screen()
        if screen is None:
            return None

        if screen.recoverable and state.recovery_attempts < MAX_RECOVERIES:
            state.recovery_attempts += 1
            recovered = await self._recover(state, rec, step, index, screen)
            if recovered and screen.recovery is RecoveryKind.REAUTHENTICATED:
                return self._after_reauthentication(state, rec, step, index, failure)
            if recovered:
                return _Verdict(retry=True)

        if screen.classification is None:
            # Recognised, recoverable, and still here after several attempts.
            # Say that, rather than reporting a bare checkpoint failure.
            evidence = await self._capture(state, f"{step.id}-unrecovered")
            return _Verdict(failure=failure.model_copy(update={
                "observed": f"{screen.name} kept reappearing after "
                            f"{state.recovery_attempts} attempts to clear it",
                "screenshot_path": evidence.screenshot_path,
                "snapshot_path": evidence.snapshot_path,
            }))
        rec.emit(EventType.OUTCOME_DETECTED, f"Recognised screen: {screen.name}",
                 level=Level.WARN, step_id=step.id, step_index=index,
                 detail={"profile": self._profile.id, "screen": screen.name,
                         "classification": screen.classification.value})
        evidence = await self._capture(state, f"{step.id}-{screen.classification.value}")
        return _Verdict(failure=failure.model_copy(update={
            "classification": screen.classification,
            "observed": f"{screen.name} ({failure.observed})",
            "screenshot_path": evidence.screenshot_path,
            "snapshot_path": evidence.snapshot_path,
        }))

    def _after_reauthentication(
        self, state: _RunState, rec: RunRecorder, step: Step, index: int,
        failure: FailureDetail,
    ) -> _Verdict:
        """Signing in again restores the session, not the place in the flow.

        Re-running the earlier steps is only safe when none of them changed
        anything. For a capability that writes, replaying a submit could open a
        second account, so the honest answer is to stop and tell the caller.
        """
        if state.artifact.risk_tier is RiskTier.READ_ONLY:
            return _Verdict(restart=True)

        rec.emit(EventType.RUN_FINISHED,
                 "Session restored, but this capability writes, so it will not be replayed",
                 level=Level.ERROR, step_id=step.id, step_index=index,
                 detail={"risk_tier": state.artifact.risk_tier.value})
        return _Verdict(failure=failure.model_copy(update={
            "classification": FailureClass.SESSION_LOST,
            "observed": (
                f"the session expired partway through a {state.artifact.risk_tier.value} "
                f"capability; it was restored, but replaying the earlier steps could "
                f"repeat work that was already committed, so the run was stopped"
            ),
        }))

    async def _match_known_screen(self) -> KnownScreen | None:
        for screen in self._profile.known_screens:
            try:
                held, _ = await self._surface.check(screen.detector, {})
            except Exception:
                continue
            if held:
                return screen
        return None

    async def _recover(
        self, state: _RunState, rec: RunRecorder, step: Step, index: int, screen: KnownScreen
    ) -> bool:
        """Clear a screen the profile says is recoverable. True if it worked."""
        try:
            if screen.dismiss is not None:
                handle, _ = await self._surface.resolve(screen.dismiss, {})
                await self._surface.activate(handle)
            elif screen.recovery is RecoveryKind.REAUTHENTICATED:
                if not await self._sign_on():
                    return False
            else:
                return False
        except Exception as exc:
            rec.emit(EventType.RECOVERY_ATTEMPTED, f"Could not clear {screen.name}: {exc}",
                     level=Level.WARN, step_id=step.id, step_index=index)
            return False

        rec.emit(EventType.RECOVERY_ATTEMPTED, f"Handled {screen.name}, retrying the step",
                 step_id=step.id, step_index=index,
                 detail={"recovery": screen.recovery.value})
        state.recoveries.append(Recovery(
            step_id=step.id, kind=screen.recovery,
            detected=screen.name, action="cleared it and retried the step",
            attempts=state.recovery_attempts,
        ))
        return True

    async def _sign_on(self) -> bool:
        """Re-establish a session using the profile. Credentials never touch the log."""
        sign_on = self._profile.sign_on if self._profile else None
        if sign_on is None or self._credentials is None:
            return False
        user, password = self._credentials
        await self._surface.navigate(urljoin(self._base_url, sign_on.path))
        for target, value in (
            (sign_on.username_field, user), (sign_on.password_field, password)
        ):
            handle, _ = await self._surface.resolve(target, {})
            await self._surface.fill(handle, value)
        handle, _ = await self._surface.resolve(sign_on.submit, {})
        await self._surface.activate(handle)
        held, _ = await self._surface.wait_for(sign_on.success, {}, 10_000)
        return held

    async def _check_landing(
        self, rec: RunRecorder, step: Step, index: int
    ) -> FailureDetail | None:
        """Refuse a page the step navigated to indirectly, such as by following a link."""
        if self._policy is None:
            return None
        try:
            urls = await self._surface.current_urls()
        except Exception:
            return None
        for url in urls:
            decision = self._policy.check_url(url)
            if decision.allowed:
                continue
            rec.emit(EventType.POLICY_BLOCKED, f"Landed outside the allowlist: {url}",
                     level=Level.ERROR, step_id=step.id, step_index=index,
                     detail={"reason": decision.reason, "url": url})
            return self._step_failure(
                step, index, FailureClass.POLICY_BLOCKED,
                expected=f"a location permitted by allowlist {self._policy.name!r}",
                observed=decision.reason,
            )
        return None

    async def _attempt(
        self, state: _RunState, rec: RunRecorder, step: Step, index: int, params: dict[str, Any]
    ) -> FailureDetail | None:
        """Perform the step's action. Returns a failure, or None if it worked."""
        try:
            await self._perform(state, rec, step, index, params)
        except TargetNotFound as exc:
            return self._step_failure(
                step, index, FailureClass.TARGET_NOT_FOUND,
                expected=f"a control matching {exc.target.description!r}",
                observed="no location strategy resolved it",
                locator_attempts=exc.attempts,
            )
        except Exception as exc:
            return self._step_failure(
                step, index, FailureClass.INTERNAL_ERROR,
                expected=f"the surface to perform {step.action.value}",
                observed=f"{type(exc).__name__}: {exc}",
            )
        return None

    def _scrub(self, state: _RunState, text: str, params: dict[str, Any]) -> str:
        """Mask anything this run supplied or read out of a message bound for the log.

        A checkpoint reports what it compared, and what it compared is the
        member number the caller passed. The message is as much a place for a
        value to escape as the field it was read from.
        """
        declared = {i.name: i.sensitivity for i in state.artifact.inputs}
        declared.update({o.name: o.sensitivity for o in state.artifact.outputs})
        values: dict[str, Sensitivity] = {}
        for source in (params, state.outputs):
            for name, raw in source.items():
                text_value = str(raw)
                if len(text_value) >= 4:
                    values[text_value] = declared.get(name, Sensitivity.PII)
        for raw in sorted(values, key=len, reverse=True):
            text = text.replace(raw, mask(raw, values[raw]))
        return text

    async def _verify_checkpoint(
        self, state: _RunState, rec: RunRecorder, step: Step, index: int,
        params: dict[str, Any],
    ) -> FailureDetail | None:
        """Assert the step's post-condition. Returns a failure, or None if it held."""
        if step.checkpoint is None:
            return None
        expected = step.checkpoint.description or (
            f"{step.checkpoint.condition.kind} within {step.checkpoint.timeout_ms}ms"
        )
        held, observed = await self._surface.wait_for(
            step.checkpoint.condition, params, step.checkpoint.timeout_ms
        )
        if held:
            rec.emit(EventType.CHECKPOINT_PASSED,
                     f"Checkpoint held: {self._scrub(state, observed, params)}",
                     step_id=step.id, step_index=index)
            return None

        rec.emit(EventType.CHECKPOINT_FAILED,
                 f"Checkpoint did not hold within {step.checkpoint.timeout_ms}ms",
                 level=Level.WARN, step_id=step.id, step_index=index,
                 detail={"expected": self._scrub(state, expected, params),
                         "observed": self._scrub(state, observed, params)})
        return self._step_failure(step, index, FailureClass.CHECKPOINT_FAILED,
                                  expected=expected, observed=observed)

    def _step_failure(
        self, step: Step, index: int, classification: FailureClass, **fields: Any
    ) -> FailureDetail:
        """Build a failure already tagged with which step it came from."""
        return FailureDetail(classification=classification, step_id=step.id,
                             step_index=index, step_intent=step.intent, **fields)

    async def _apply_failure_policy(
        self, state: _RunState, rec: RunRecorder, step: Step, index: int,
        params: dict[str, Any], failure: FailureDetail, attempt: int,
    ) -> _Verdict | None:
        """Decide what a failed step means, using the policy the artifact declared."""
        policy = step.on_failure

        if policy is OnFailure.SKIP:
            rec.emit(EventType.RECOVERY_ATTEMPTED, f"Optional step skipped: {step.intent}",
                     step_id=step.id, step_index=index)
            state.recoveries.append(Recovery(
                step_id=step.id, kind=RecoveryKind.DISMISSED_INTERSTITIAL,
                detected=failure.observed, action="skipped an optional step",
            ))
            return None

        if policy is OnFailure.RETRY and attempt <= step.retries:
            rec.emit(EventType.RECOVERY_ATTEMPTED,
                     f"Transient condition, retrying ({attempt}/{step.retries})",
                     step_id=step.id, step_index=index, detail={"observed": failure.observed})
            state.recoveries.append(Recovery(
                step_id=step.id, kind=RecoveryKind.RETRIED_TRANSIENT,
                detected=failure.observed, action="retried the step", attempts=attempt,
            ))
            return _Verdict(retry=True)

        if policy is OnFailure.CLASSIFY:
            outcome = await self._detect_outcome(state.artifact, params, step_id=step.id)
            if outcome is not None:
                rec.emit(EventType.OUTCOME_DETECTED,
                         f"Declared business outcome {outcome.code} matched",
                         step_id=step.id, step_index=index,
                         rationale="step failure policy is classify; a declared detector matched "
                                   "before the step was treated as a fault")
                return _Verdict(outcome=outcome)

        if policy is OnFailure.ESCALATE:
            verdict = await self._escalate(
                state, rec, step, index, failure,
                why=f"the automation could not complete this step: {failure.observed}",
                ask=("Take the browser, put the screen into the state this step was "
                     "trying to reach, then reply resume. Reply abort to stop."),
                proceed_on_resume=False,
            )
            return verdict if verdict is not None else _Verdict(failure=failure, escalated=True)

        evidence = await self._capture(state, f"{step.id}-failure")
        return _Verdict(failure=failure.model_copy(update={
            "screenshot_path": evidence.screenshot_path,
            "snapshot_path": evidence.snapshot_path,
        }))

    # -- performing one action --------------------------------------------

    async def _perform(
        self, state: _RunState, rec: RunRecorder, step: Step, index: int, params: dict[str, Any]
    ) -> None:
        surface, action = self._surface, step.action

        if action is Action.NAVIGATE:
            await surface.navigate(urljoin(self._base_url, resolve_value(step.value, params)))
            return

        if action in (Action.WAIT_FOR, Action.ASSERT):
            if step.condition is None:
                raise SurfaceError(f"step {step.id} has action {action.value} but no condition")
            held, observed = await surface.wait_for(step.condition, params, step.timeout_ms)
            if not held:
                raise SurfaceError(observed)
            return

        if action is Action.PRESS and step.target is None:
            await surface.press(resolve_value(step.value, params))
            return

        if step.target is None:
            raise SurfaceError(f"step {step.id} has action {action.value} but no target")

        handle, resolution = await surface.resolve(step.target, params, timeout_ms=step.timeout_ms)
        event_type = EventType.LOCATOR_DEGRADED if resolution.degraded else EventType.LOCATOR_RESOLVED
        rec.emit(event_type,
                 f"Resolved {step.target.description!r} via {resolution.strategy_used}",
                 level=Level.WARN if resolution.degraded else Level.INFO,
                 step_id=step.id, step_index=index,
                 detail={"recorded_strategy": step.target.recorded_strategy,
                         "actual_strategy": resolution.strategy_used,
                         "matches": resolution.matches, "attempts": resolution.attempts})
        if resolution.degraded:
            state.degradations.append(Degradation(
                step_id=step.id, recorded_strategy=step.target.recorded_strategy,
                actual_strategy=resolution.strategy_used,
                element_description=step.target.description,
            ))

        if action is Action.FILL:
            await surface.fill(handle, resolve_value(step.value, params))
        elif action in (Action.ACTIVATE, Action.DISMISS):
            await surface.activate(handle)
        elif action is Action.SELECT:
            await surface.select(handle, resolve_value(step.value, params))
        elif action is Action.PRESS:
            await surface.press(resolve_value(step.value, params), handle)
        elif action is Action.READ:
            if not step.output:
                raise SurfaceError(f"step {step.id} reads a value but names no output field")
            raw = await surface.read(handle)
            state.outputs[step.output] = self._coerce(state.artifact, step.output, raw)
            shown = mask(str(state.outputs[step.output]),
                         self._sensitivity_of(state, step.output))
            rec.emit(EventType.OUTPUT_EXTRACTED, f"Extracted {step.output}",
                     step_id=step.id, step_index=index,
                     detail={step.output: shown},
                     redacted_fields=[step.output] if shown != str(state.outputs[step.output]) else [])
        else:
            raise SurfaceError(f"replay cannot perform {action.value}")

    # -- helpers -----------------------------------------------------------

    def _sensitivity_of(self, state: _RunState, output: str) -> Sensitivity:
        """The strictest tag on any output currently holding this same value.

        A sub-account number arrived once as a `pii` output and again as an
        `internal` one. Masking each field by its own tag left the second copy
        in the clear beside the masked first, which protects nothing.
        """
        value = str(state.outputs.get(output, ""))
        declared = {o.name: o.sensitivity for o in state.artifact.outputs}
        sharing = [
            declared.get(name, Sensitivity.INTERNAL)
            for name, held in state.outputs.items()
            if str(held) == value
        ]
        return max(sharing or [Sensitivity.INTERNAL], key=lambda s: STRICTNESS[s])

    def _redact(
        self, artifact: CapabilityArtifact, params: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        declared = {p.name: p for p in artifact.inputs}
        safe, redacted = {}, []
        for name, value in params.items():
            spec: InputParam | None = declared.get(name)
            sensitivity = spec.sensitivity if spec else Sensitivity.INTERNAL
            shown = mask(str(value), sensitivity)
            safe[name] = shown
            if shown != str(value):
                redacted.append(f"inputs.{name}")
        return safe, redacted

    def _validate_params(
        self, artifact: CapabilityArtifact, params: dict[str, Any]
    ) -> list[str]:
        problems: list[str] = []
        declared = {p.name: p for p in artifact.inputs}
        for name, spec in declared.items():
            if name not in params or params[name] in (None, ""):
                if spec.required:
                    problems.append(f"{name} is required")
                continue
            value = str(params[name])
            if spec.pattern and not re.fullmatch(spec.pattern, value):
                problems.append(f"{name}={value!r} does not match {spec.pattern}")
            if spec.values and value not in spec.values:
                problems.append(f"{name}={value!r} is not one of {spec.values}")
        for name in params:
            if name not in declared:
                problems.append(f"{name} is not an input of this capability")
        return problems

    def _coerce(self, artifact: CapabilityArtifact, output: str, raw: str) -> Any:
        field = next((o for o in artifact.outputs if o.name == output), None)
        text = raw.strip()
        if field is None:
            return text
        if field.type == "decimal":
            cleaned = text.replace(",", "").replace("$", "").strip()
            try:
                return str(Decimal(cleaned))
            except InvalidOperation:
                return text
        if field.type == "integer":
            try:
                return int(text.replace(",", ""))
            except ValueError:
                return text
        return text

    async def _detect_outcome(
        self, artifact: CapabilityArtifact, params: dict[str, Any],
        step_id: str | None = None,
    ) -> BusinessOutcome | None:
        for outcome in artifact.outcomes:
            if outcome.at_step is not None and outcome.at_step != step_id:
                continue
            try:
                held, _ = await self._surface.check(outcome.detector, params)
            except Exception:
                continue
            if held:
                return outcome
        return None

    async def _capture(self, state: _RunState, label: str) -> EvidenceRef:
        state.run_dir.mkdir(parents=True, exist_ok=True)
        shot = state.run_dir / f"{label}.png"
        snap = state.run_dir / f"{label}.txt"
        try:
            await self._surface.screenshot(str(shot))
        except Exception:
            shot = None
        try:
            observation = await self._surface.observe()
            snap.write_text(
                "\n\n".join(f"--- frame {k or '(main)'} ---\n{v}"
                            for k, v in observation.frames.items()),
                encoding="utf-8",
            )
        except Exception:
            snap = None
        return EvidenceRef(
            screenshot_path=str(shot) if shot else None,
            snapshot_path=str(snap) if snap else None,
        )

    # -- result assembly ---------------------------------------------------

    def _finish(
        self, state: _RunState, clock: float, rec: RunRecorder, verdict: _Verdict
    ) -> ReplayResult:
        if verdict.failure is not None:
            rec.emit(EventType.RUN_FINISHED,
                     f"Replay failed: {verdict.failure.classification.value}",
                     level=Level.ERROR,
                     detail={"step_id": verdict.failure.step_id,
                             "expected": verdict.failure.expected,
                             "observed": verdict.failure.observed})
            return self._failure(state, clock, verdict.failure, escalated=verdict.escalated)

        if verdict.outcome is not None:
            rec.emit(EventType.RUN_FINISHED,
                     f"Replay finished: business_outcome {verdict.outcome.code}",
                     detail={"status": "business_outcome", "code": verdict.outcome.code,
                             "steps_completed": state.steps_completed})
            return BusinessOutcomeResult(
                **self._envelope(state, clock),
                code=verdict.outcome.code,
                message=verdict.outcome.description,
                partial_outputs=state.outputs,
            )

        rec.emit(EventType.RUN_FINISHED, "Replay finished: success",
                 detail={"status": "success", "steps_completed": state.steps_completed})
        return SuccessResult(**self._envelope(state, clock), outputs=state.outputs)

    def _failure(
        self, state: _RunState, clock: float, failure: FailureDetail, *, escalated: bool = False
    ) -> FailureResult:
        return FailureResult(**self._envelope(state, clock), failure=failure, escalated=escalated)

    def _envelope(self, state: _RunState, clock: float) -> dict[str, Any]:
        return {
            "capability_id": state.artifact.id,
            "capability_version": state.artifact.version,
            "run_id": state.run_id,
            "institution": state.institution,
            "started_at": state.started_at,
            "finished_at": datetime.now(UTC),
            "duration_ms": int((time.monotonic() - clock) * 1000),
            "steps_total": len(state.artifact.steps),
            "steps_completed": state.steps_completed,
            "degradations": state.degradations,
            "recoveries": state.recoveries,
            "control_events": state.control_events,
            "evidence_dir": str(state.run_dir),
        }


# --------------------------------------------------------------------------
# Small internal carriers
# --------------------------------------------------------------------------


class _Verdict(BaseModel):
    """Why a step ended. Retry means run it again; the others stop the run."""

    retry: bool = False
    restart: bool = False
    outcome: BusinessOutcome | None = None
    failure: FailureDetail | None = None
    escalated: bool = False


class _RunState(BaseModel):
    """Everything accumulated while a single replay is in flight."""

    artifact: CapabilityArtifact
    run_id: str
    institution: str
    started_at: datetime
    run_dir: Path

    steps_completed: int = 0
    outputs: dict[str, Any] = Field(default_factory=dict)
    degradations: list[Degradation] = Field(default_factory=list)
    recoveries: list[Recovery] = Field(default_factory=list)
    control_events: list[ControlEvent] = Field(default_factory=list)
    recovery_attempts: int = 0
