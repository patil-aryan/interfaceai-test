"""Executes a capability artifact against a surface. No model is involved."""

from __future__ import annotations

import itertools
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from pydantic import BaseModel, Field

from computer_use.evidence.sink import EventSink, RunRecorder
from computer_use.schema.capability import (
    Action,
    BusinessOutcome,
    CapabilityArtifact,
    InputParam,
    OnFailure,
    Sensitivity,
    Step,
)
from computer_use.schema.event import EventType, EvidenceRef, Level, RunKind
from computer_use.schema.result import (
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
    ):
        self._surface = surface
        self._base_url = base_url.rstrip("/") + "/"
        self._events = events
        self._evidence_root = Path(evidence_root)

    # -- entry point -------------------------------------------------------

    async def run(
        self,
        artifact: CapabilityArtifact,
        params: dict[str, Any],
        *,
        institution: str,
        run_id: str | None = None,
    ) -> ReplayResult:
        run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"
        run_dir = self._evidence_root / run_id
        sink = self._events or EventSink(run_dir)
        rec = RunRecorder(sink, run_id=run_id, run_kind=RunKind.REPLAY, capability_id=artifact.id)

        started = datetime.now(timezone.utc)
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

        for index, step in enumerate(artifact.steps):
            verdict = await self._run_step(state, rec, step, index, params)
            if verdict is not None:
                return self._finish(state, clock, rec, verdict)
            state.steps_completed = index + 1

        return self._finish(state, clock, rec, await self._verify_success(state, rec, params))

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
        for attempt in itertools.count(1):
            rec.emit(EventType.ACTION_ATTEMPTED, step.intent, step_id=step.id, step_index=index,
                     rationale=f"recorded step: {step.action.value}",
                     detail={"attempt": attempt} if attempt > 1 else None)
            began = time.monotonic()

            failure = await self._attempt(state, rec, step, index, params)
            if failure is None:
                failure = await self._verify_checkpoint(rec, step, index, params)

            if failure is None:
                rec.emit(EventType.ACTION_SUCCEEDED, f"{step.action.value} completed",
                         step_id=step.id, step_index=index,
                         duration_ms=int((time.monotonic() - began) * 1000))
                return None

            verdict = await self._apply_policy(state, rec, step, index, params, failure, attempt)
            if verdict is None:          # the step was optional and was skipped
                return None
            if verdict.retry:
                continue
            return verdict

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

    async def _verify_checkpoint(
        self, rec: RunRecorder, step: Step, index: int, params: dict[str, Any]
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
            rec.emit(EventType.CHECKPOINT_PASSED, f"Checkpoint held: {observed}",
                     step_id=step.id, step_index=index)
            return None

        rec.emit(EventType.CHECKPOINT_FAILED,
                 f"Checkpoint did not hold within {step.checkpoint.timeout_ms}ms",
                 level=Level.WARN, step_id=step.id, step_index=index,
                 detail={"expected": expected, "observed": observed})
        return self._step_failure(step, index, FailureClass.CHECKPOINT_FAILED,
                                  expected=expected, observed=observed)

    def _step_failure(
        self, step: Step, index: int, classification: FailureClass, **fields: Any
    ) -> FailureDetail:
        """Build a failure already tagged with which step it came from."""
        return FailureDetail(classification=classification, step_id=step.id,
                             step_index=index, step_intent=step.intent, **fields)

    async def _apply_policy(
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
            outcome = await self._detect_outcome(state.artifact, params)
            if outcome is not None:
                rec.emit(EventType.OUTCOME_DETECTED,
                         f"Declared business outcome {outcome.code} matched",
                         step_id=step.id, step_index=index,
                         rationale="step failure policy is classify; a declared detector matched "
                                   "before the step was treated as a fault")
                return _Verdict(outcome=outcome)

        if policy is OnFailure.ESCALATE:
            rec.emit(EventType.ESCALATION_RAISED,
                     f"Step needs a human: {step.intent}", level=Level.WARN,
                     step_id=step.id, step_index=index, detail={"observed": failure.observed})
            evidence = await self._capture(state, f"{step.id}-escalation")
            failure = failure.model_copy(update={
                "classification": FailureClass.ESCALATION_UNANSWERED,
                "screenshot_path": evidence.screenshot_path,
                "snapshot_path": evidence.snapshot_path,
            })
            return _Verdict(failure=failure, escalated=True)

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
            field = next((o for o in state.artifact.outputs if o.name == step.output), None)
            shown = mask(str(state.outputs[step.output]),
                         field.sensitivity if field else Sensitivity.INTERNAL)
            rec.emit(EventType.OUTPUT_EXTRACTED, f"Extracted {step.output}",
                     step_id=step.id, step_index=index,
                     detail={step.output: shown},
                     redacted_fields=[step.output] if shown != str(state.outputs[step.output]) else [])
        else:
            raise SurfaceError(f"replay cannot perform {action.value}")

    # -- helpers -----------------------------------------------------------

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
        self, artifact: CapabilityArtifact, params: dict[str, Any]
    ) -> BusinessOutcome | None:
        for outcome in artifact.outcomes:
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
            "finished_at": datetime.now(timezone.utc),
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
