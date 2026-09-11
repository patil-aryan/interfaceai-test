"""What a replay returns to its caller: success, a business outcome, or a failure."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Failure taxonomy
# --------------------------------------------------------------------------


class FailureClass(str, Enum):
    """Why a replay stopped badly. Declared business outcomes never appear here."""

    TARGET_NOT_FOUND = "target_not_found"
    CHECKPOINT_FAILED = "checkpoint_failed"
    SUCCESS_CONDITION_FAILED = "success_condition_failed"

    SESSION_LOST = "session_lost"
    SURFACE_UNAVAILABLE = "surface_unavailable"
    TIMEOUT = "timeout"

    PERMISSION_DENIED = "permission_denied"
    APP_ERROR = "app_error"

    POLICY_BLOCKED = "policy_blocked"
    INPUT_INVALID = "input_invalid"

    ESCALATION_UNANSWERED = "escalation_unanswered"
    ESCALATION_ABANDONED = "escalation_abandoned"

    INTERNAL_ERROR = "internal_error"


class FailureDetail(BaseModel):
    """Enough to debug a failure without re-running it."""

    classification: FailureClass

    step_id: str | None = None
    step_index: int | None = None
    step_intent: str | None = None

    expected: str
    observed: str

    locator_attempts: list[str] = Field(default_factory=list)

    screenshot_path: str | None = None
    snapshot_path: str | None = None


# --------------------------------------------------------------------------
# Recorded on every result, whatever the status
# --------------------------------------------------------------------------


class Degradation(BaseModel):
    """A locator resolved, but via a later fallback than when it was recorded."""

    step_id: str
    recorded_strategy: str
    actual_strategy: str
    element_description: str


class RecoveryKind(str, Enum):
    RETRIED_TRANSIENT = "retried_transient"
    DISMISSED_INTERSTITIAL = "dismissed_interstitial"
    REAUTHENTICATED = "reauthenticated"
    WAITED_FOR_LOAD = "waited_for_load"


class Recovery(BaseModel):
    """A recoverable condition that was detected and handled."""

    step_id: str | None
    kind: RecoveryKind
    detected: str
    action: str
    attempts: int = 1
    resolved: bool = True


class Actor(str, Enum):
    AGENT = "agent"
    OPERATOR = "operator"
    SYSTEM = "system"


class ControlEvent(BaseModel):
    """A transfer of control over the live session."""

    at: datetime
    from_actor: Actor
    to_actor: Actor
    reason: str
    operator_ref: str | None = None
    operator_actions_recorded: int = 0


# --------------------------------------------------------------------------
# The three result shapes
# --------------------------------------------------------------------------


class ReplayResultBase(BaseModel):
    """Fields present on every result."""

    capability_id: str
    capability_version: str
    run_id: str
    institution: str

    started_at: datetime
    finished_at: datetime
    duration_ms: int

    steps_total: int
    steps_completed: int

    degradations: list[Degradation] = Field(default_factory=list)
    recoveries: list[Recovery] = Field(default_factory=list)
    control_events: list[ControlEvent] = Field(default_factory=list)

    evidence_dir: str


class SuccessResult(ReplayResultBase):
    """The flow completed and the capability's success condition held."""

    status: Literal["success"] = "success"

    # Keys are the capability's declared output names; the executor validates
    # values against the declared types. Unmasked -- redaction for the event
    # log happens in the guardrails module.
    outputs: dict[str, Any]


class BusinessOutcomeResult(ReplayResultBase):
    """A legitimate answer that is not success, e.g. no such member."""

    status: Literal["business_outcome"] = "business_outcome"

    code: str
    message: str
    partial_outputs: dict[str, Any] = Field(default_factory=dict)


class FailureResult(ReplayResultBase):
    """Something went wrong that the capability did not anticipate."""

    status: Literal["failed"] = "failed"

    failure: FailureDetail
    escalated: bool = False


ReplayResult = Annotated[
    Union[SuccessResult, BusinessOutcomeResult, FailureResult],
    Field(discriminator="status"),
]
