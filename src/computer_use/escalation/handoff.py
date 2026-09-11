"""Bringing a human into a run, and letting them drive the same live session.

The automation does not close the browser and hand over a description of the
problem. It stops driving, writes down what it needs, and leaves the session
exactly where it is. The operator works in that window. When they hand back,
the run carries on from the same place.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from computer_use.schema.result import Actor

REQUEST_FILE = "intervention.json"
RESPONSE_FILE = "intervention.response.json"
POLL_SECONDS = 1.0
DEFAULT_WAIT_S = 300


class InterventionRequest(BaseModel):
    """Everything an operator needs to act, without reading the code or the log."""

    request_id: str
    raised_at: datetime
    run_id: str
    run_dir: str

    capability_id: str
    capability_title: str
    goal: str
    risk_tier: str

    step_id: str | None = None
    step_index: int | None = None
    step_intent: str | None = None

    why: str  # why the automation stopped
    expected: str
    observed: str
    asked_of_operator: str  # what they are being asked to do

    screenshot_path: str | None = None
    snapshot_path: str | None = None

    # Who holds the session while this request is open.
    control: Actor = Actor.OPERATOR


class InterventionResponse(BaseModel):
    """The operator's answer, and what they say they did."""

    request_id: str
    decision: str  # "resume" or "abort"
    operator: str
    note: str = ""
    responded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HandoffBroker:
    """Publishes an intervention request and waits for a human to answer it.

    Deliberately a pair of files in the run's evidence directory. That is the
    whole mechanism, and it is the seam: a queue, a websocket or an operator
    console replaces these two writes without the engine noticing.
    """

    def __init__(self, run_dir: Path | str, wait_seconds: int = DEFAULT_WAIT_S):
        self.run_dir = Path(run_dir)
        self.wait_seconds = wait_seconds

    @property
    def request_path(self) -> Path:
        return self.run_dir / REQUEST_FILE

    @property
    def response_path(self) -> Path:
        return self.run_dir / RESPONSE_FILE

    def publish(self, request: InterventionRequest) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.response_path.unlink(missing_ok=True)
        self.request_path.write_text(request.model_dump_json(indent=2), encoding="utf-8")
        return self.request_path

    async def wait(self, request_id: str) -> InterventionResponse | None:
        """Poll for the operator's answer. None means nobody answered in time."""
        deadline = asyncio.get_running_loop().time() + self.wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            answer = self.read_response(request_id)
            if answer is not None:
                return answer
            await asyncio.sleep(POLL_SECONDS)
        return None

    def read_response(self, request_id: str) -> InterventionResponse | None:
        if not self.response_path.is_file():
            return None
        try:
            answer = InterventionResponse.model_validate_json(
                self.response_path.read_text(encoding="utf-8")
            )
        except Exception:
            return None
        return answer if answer.request_id == request_id else None

    def answer(self, response: InterventionResponse) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.response_path.write_text(response.model_dump_json(indent=2), encoding="utf-8")
        return self.response_path


def new_request_id() -> str:
    return f"iv_{uuid.uuid4().hex[:8]}"


def pending_requests(evidence_root: Path | str) -> list[tuple[Path, InterventionRequest]]:
    """Every intervention waiting for an answer, newest first."""
    found: list[tuple[Path, InterventionRequest]] = []
    for path in sorted(Path(evidence_root).glob(f"*/{REQUEST_FILE}")):
        try:
            request = InterventionRequest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception:
            continue
        if (path.parent / RESPONSE_FILE).is_file():
            continue
        found.append((path.parent, request))
    return sorted(found, key=lambda pair: pair[1].raised_at, reverse=True)


def describe(request: InterventionRequest) -> str:
    """The request as an operator should read it."""
    lines = [
        f"  capability : {request.capability_title}  ({request.capability_id})",
        f"  goal       : {request.goal}",
        f"  risk       : {request.risk_tier}",
        f"  run        : {request.run_id}",
    ]
    if request.step_intent:
        lines.append(f"  stopped at : step {request.step_index} - {request.step_intent}")
    lines += [
        f"  why        : {request.why}",
        f"  expected   : {request.expected}",
        f"  observed   : {request.observed}",
    ]
    if request.screenshot_path:
        lines.append(f"  screenshot : {request.screenshot_path}")
    if request.snapshot_path:
        lines.append(f"  screen text: {request.snapshot_path}")
    lines.append(f"  asked of you: {request.asked_of_operator}")
    return "\n".join(lines)
