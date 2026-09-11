"""Invoking a catalogued capability by name. No model is involved in doing it."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from computer_use.escalation.handoff import HandoffBroker
from computer_use.guardrails.policy import Allowlist
from computer_use.replay.engine import ReplayEngine
from computer_use.schema.capability import CapabilityArtifact
from computer_use.schema.profile import load_app_profile
from computer_use.schema.result import ReplayResult
from computer_use.surfaces.web import WebSurface


async def invoke(
    artifact: CapabilityArtifact,
    arguments: dict[str, Any],
    *,
    policy: Allowlist,
    base_url: str,
    institution: str | None = None,
    credentials: tuple[str, str],
    evidence_root: Path | str = "evidence",
    headed: bool = False,
    slow_mo_ms: int = 0,
    operator_wait_s: int = 300,
) -> ReplayResult:
    """Run one capability and return its typed result.

    The same engine the command line uses. An agent calling a capability and a
    person replaying one are the same execution path, which is the only way the
    guarantees mean anything.
    """
    run_id = f"run_{uuid.uuid4().hex[:10]}"
    profile = load_app_profile(artifact.app_profile)
    surface = WebSurface(headless=not headed, slow_mo_ms=slow_mo_ms)
    await surface.start()
    try:
        engine = ReplayEngine(
            surface,
            base_url=base_url,
            evidence_root=evidence_root,
            policy=policy,
            profile=profile,
            credentials=credentials,
            handoff=HandoffBroker(Path(evidence_root) / run_id, wait_seconds=operator_wait_s),
        )
        return await engine.run(
            artifact,
            {name: str(value) for name, value in arguments.items()},
            institution=institution or artifact.provenance.recorded_against_institution,
            run_id=run_id,
        )
    finally:
        await surface.stop()


def for_the_agent(result: ReplayResult) -> dict[str, Any]:
    """The result, reduced to what a calling agent needs to decide what to do next.

    A caller should never have to read a log to find out what happened, and
    should never be handed a stack trace where an answer was expected.
    """
    common = {"status": result.status, "run_id": result.run_id}
    if result.status == "success":
        return {**common, "outputs": result.outputs}
    if result.status == "business_outcome":
        return {
            **common,
            "outcome": result.code,
            "message": result.message,
            "partial_outputs": result.partial_outputs,
        }
    return {
        **common,
        "error": result.failure.classification.value,
        "at_step": result.failure.step_intent or result.failure.step_id,
        "expected": result.failure.expected,
        "observed": result.failure.observed,
        "needed_a_person": result.escalated,
    }
