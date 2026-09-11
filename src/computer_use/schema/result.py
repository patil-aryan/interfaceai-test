"""What a replay returns to its caller.

A production AI agent invokes a capability and gets one of exactly three
shapes back:

    success           the flow completed and the declared outputs are attached
    business_outcome  a legitimate answer that is not success ("no such member")
    failed            something went wrong; here is the step, the expectation,
                      and what was actually observed

Keeping these three apart is the point of the file. A single result object with
a boolean `ok` invites callers to write `if ok: ... else: raise`, which turns a
legitimate business answer into a crash. Three shapes make that impossible to
write by accident.

Two things are recorded on *every* result regardless of status, because they
are how the system reports on itself: locator degradations (drift), and
recoveries (conditions we detected and handled). Control transfers are recorded
too, so a handoff to a human is part of the result rather than a side channel.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Failure taxonomy
# --------------------------------------------------------------------------


class FailureClass(str, Enum):
    """Why a replay stopped, when it stopped badly.

    These are hard failures only. Anything the capability *declared* as a
    business outcome never reaches this enum -- it returns as
    `BusinessOutcomeResult` instead. The distinction is deliberate: a
    permission denial the author anticipated is an answer; an unanticipated one
    is a bug in the capability, and should look like one.
    """

    # Targeting and verification
    TARGET_NOT_FOUND = "target_not_found"  # no locator rung resolved the control
    CHECKPOINT_FAILED = "checkpoint_failed"  # step ran, post-condition did not hold
    SUCCESS_CONDITION_FAILED = "success_condition_failed"  # every step ran, end state wrong

    # Session and environment
    SESSION_LOST = "session_lost"  # expired, and re-authentication did not recover it
    SURFACE_UNAVAILABLE = "surface_unavailable"  # could not reach the application at all
    TIMEOUT = "timeout"  # a wait or load exceeded its budget

    # The application said no, and the capability did not anticipate it
    PERMISSION_DENIED = "permission_denied"
    APP_ERROR = "app_error"  # the application's own error screen

    # Refused before or during execution
    POLICY_BLOCKED = "policy_blocked"  # allowlist, risk tier, or approval state
    INPUT_INVALID = "input_invalid"  # caller's arguments failed the declared contract

    # Human-in-the-loop
    ESCALATION_UNANSWERED = "escalation_unanswered"  # nobody took the intervention in time
    ESCALATION_ABANDONED = "escalation_abandoned"  # operator declined to continue

    INTERNAL_ERROR = "internal_error"  # a defect in this engine, not in the target


class FailureDetail(BaseModel):
    """Everything needed to debug a failure without re-running it.

    `expected` and `observed` are separate fields rather than one prose message
    because the pair is what makes a failure actionable, and because a reviewer
    scanning many failures needs them in fixed positions.
    """

    classification: FailureClass

    step_id: str | None = None  # None if we failed before any step ran
    step_index: int | None = None
    step_intent: str | None = None  # the artifact's plain-English sentence

    expected: str  # "heading 'MEMBER PROFILE' present within 5000ms"
    observed: str  # "no matching element; page heading was 'SYSTEM ERROR'"

    # Which rungs of the locator ladder were tried, in order, and what happened
    # to each. Present only for TARGET_NOT_FOUND.
    locator_attempts: list[str] = Field(default_factory=list)

    screenshot_path: str | None = None
    snapshot_path: str | None = None  # accessibility tree / character grid dump


# --------------------------------------------------------------------------
# Things that happened along the way, recorded on every result
# --------------------------------------------------------------------------


class Degradation(BaseModel):
    """A locator resolved, but on a lower rung than when it was recorded.

    The flow still worked. The surface moved. This is the early warning that a
    capability is drifting for this institution, logged before it breaks.
    """

    step_id: str
    recorded_rung: str
    actual_rung: str
    element_description: str


class RecoveryKind(str, Enum):
    RETRIED_TRANSIENT = "retried_transient"
    DISMISSED_INTERSTITIAL = "dismissed_interstitial"
    REAUTHENTICATED = "reauthenticated"
    WAITED_FOR_LOAD = "waited_for_load"


class Recovery(BaseModel):
    """A recoverable condition that was detected and handled.

    Reported even on success, because a capability that quietly needs three
    retries every run is a capability about to fail.
    """

    step_id: str | None
    kind: RecoveryKind
    detected: str  # what we saw
    action: str  # what we did about it
    attempts: int = 1
    resolved: bool = True


class Actor(str, Enum):
    AGENT = "agent"
    OPERATOR = "operator"
    SYSTEM = "system"


class ControlEvent(BaseModel):
    """A transfer of control over the live session.

    Recorded on the result, not in a side channel, so a caller can see that a
    human touched this run without going and reading the logs.
    """

    at: datetime
    from_actor: Actor
    to_actor: Actor
    reason: str
    operator_ref: str | None = None  # an opaque operator id, never a name
    operator_actions_recorded: int = 0


# --------------------------------------------------------------------------
# The three result shapes
# --------------------------------------------------------------------------


class ReplayResultBase(BaseModel):
    """Fields present on every result, whatever the status."""

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

    evidence_dir: str  # where the event log, screenshots and snapshots landed


class SuccessResult(ReplayResultBase):
    """The flow completed and the capability's success condition held."""

    status: Literal["success"] = "success"

    # Keys are the capability's declared output names. Values are validated
    # against the declared types by the executor before this is constructed,
    # so the shape is `dict` here only because it is defined per capability.
    #
    # These values are returned to the caller in full. The redacted copy that
    # goes to the event log is produced separately, by the guardrails module,
    # using the sensitivity tag on each declared output.
    outputs: dict[str, Any]


class BusinessOutcomeResult(ReplayResultBase):
    """A legitimate answer that is not success.

    Not an error. "No such member" is what the caller asked to find out. The
    code comes from the capability's declared `outcomes`, so a calling agent
    can branch on a stable identifier rather than parsing a message.
    """

    status: Literal["business_outcome"] = "business_outcome"

    code: str  # e.g. MEMBER_NOT_FOUND -- declared in the artifact
    message: str
    partial_outputs: dict[str, Any] = Field(default_factory=dict)


class FailureResult(ReplayResultBase):
    """Something went wrong that the capability did not anticipate."""

    status: Literal["failed"] = "failed"

    failure: FailureDetail
    escalated: bool = False  # was a human offered this before we gave up


ReplayResult = Annotated[
    Union[SuccessResult, BusinessOutcomeResult, FailureResult],
    Field(discriminator="status"),
]
