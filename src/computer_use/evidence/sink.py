"""Where run events go. One append-only JSONL file per run, plus its artefacts."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from computer_use.schema.event import EventType, EvidenceRef, Level, RunEvent, RunKind
from computer_use.schema.result import Actor


class EventSink:
    """Appends events to <run_dir>/events.jsonl, or discards them if given no directory."""

    def __init__(self, run_dir: Path | str | None = None):
        self.path: Path | None = None
        if run_dir is not None:
            directory = Path(run_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self.path = directory / "events.jsonl"

    def emit(self, event: RunEvent) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.to_jsonl() + "\n")


class RunRecorder:
    """Builds well-formed events so callers do not have to repeat the envelope."""

    def __init__(
        self,
        sink: EventSink,
        *,
        run_id: str,
        run_kind: RunKind,
        capability_id: str | None = None,
    ):
        self._sink = sink
        self._run_id = run_id
        self._run_kind = run_kind
        self._capability_id = capability_id
        self._seq = itertools.count(1)

    def emit(
        self,
        type: EventType,
        message: str,
        *,
        level: Level = Level.INFO,
        actor: Actor = Actor.AGENT,
        step_id: str | None = None,
        step_index: int | None = None,
        rationale: str | None = None,
        detail: dict[str, Any] | None = None,
        redacted_fields: list[str] | None = None,
        evidence: EvidenceRef | None = None,
        duration_ms: int | None = None,
    ) -> RunEvent:
        event = RunEvent(
            run_id=self._run_id,
            run_kind=self._run_kind,
            seq=next(self._seq),
            at=datetime.now(timezone.utc),
            type=type,
            level=level,
            actor=actor,
            capability_id=self._capability_id,
            step_id=step_id,
            step_index=step_index,
            message=message,
            rationale=rationale,
            detail=detail or {},
            redacted_fields=redacted_fields or [],
            evidence=evidence,
            duration_ms=duration_ms,
        )
        self._sink.emit(event)
        return event
