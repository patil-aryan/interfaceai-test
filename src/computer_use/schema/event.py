"""One line in a run's log. Every run appends these to a JSONL file."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from computer_use.schema.result import Actor

EVENT_SCHEMA_VERSION = "1.0"


class RunKind(str, Enum):
    """Which kind of run produced this event."""

    DISCOVERY = "discovery"  # model-driven, figuring the flow out
    REPLAY = "replay"  # deterministic, no model in the decision loop


class Level(str, Enum):
    """How much attention this line deserves."""

    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class EventType(str, Enum):
    """What happened. Grouped by the part of the system that emits it."""

    # Run lifecycle
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"

    # Perception and action on the surface
    OBSERVED = "observed"
    ACTION_ATTEMPTED = "action_attempted"
    ACTION_SUCCEEDED = "action_succeeded"
    ACTION_FAILED = "action_failed"
    LOCATOR_RESOLVED = "locator_resolved"
    LOCATOR_DEGRADED = "locator_degraded"
    CHECKPOINT_PASSED = "checkpoint_passed"
    CHECKPOINT_FAILED = "checkpoint_failed"
    OUTPUT_EXTRACTED = "output_extracted"

    # The model, during discovery only
    MODEL_REQUESTED = "model_requested"
    MODEL_RESPONDED = "model_responded"

    # Classification and recovery
    OUTCOME_DETECTED = "outcome_detected"
    RECOVERY_ATTEMPTED = "recovery_attempted"

    # Guardrails
    POLICY_ALLOWED = "policy_allowed"
    POLICY_BLOCKED = "policy_blocked"

    # Human in the loop
    ESCALATION_RAISED = "escalation_raised"
    CONTROL_TRANSFERRED = "control_transferred"
    OPERATOR_ACTION = "operator_action"


class EvidenceRef(BaseModel):
    """A pointer to a heavy artifact written alongside the log."""

    screenshot_path: str | None = None
    snapshot_path: str | None = None  # accessibility tree or character grid
    html_path: str | None = None


class RunEvent(BaseModel):
    """One append-only record of something that happened during a run."""

    schema_version: str = EVENT_SCHEMA_VERSION

    run_id: str
    run_kind: RunKind
    seq: int  # monotonic within the run; authoritative for ordering
    at: datetime

    type: EventType
    level: Level = Level.INFO
    actor: Actor = Actor.AGENT

    capability_id: str | None = None
    step_id: str | None = None
    step_index: int | None = None

    message: str  # one short human-readable line
    rationale: str | None = None  # why this was done, when an actor chose it

    detail: dict[str, Any] = Field(default_factory=dict)
    redacted_fields: list[str] = Field(default_factory=list)

    evidence: EvidenceRef | None = None
    duration_ms: int | None = None

    def to_jsonl(self) -> str:
        """Serialise to a single line, suitable for appending to a .jsonl file."""
        return self.model_dump_json(exclude_none=True)
