"""Confirm an artifact's declared outcomes against the running application.

A detector a model proposed is a guess about wording. An unverified detector is
worse than none: it never fires, and it makes the artifact look like it handles
a case it does not.
"""

from __future__ import annotations

from typing import Any

from computer_use.discovery.spec import GoalSpec
from computer_use.replay.engine import ReplayEngine
from computer_use.schema.capability import BusinessOutcome, CapabilityArtifact, TextPresent

# A detector shorter than this is not distinctive enough to trust.
MIN_DETECTOR_CHARS = 6

WORDING_TOOL = {
    "name": "report_wording",
    "description": "Report the exact phrase on screen that signals the described situation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {
                "type": "boolean",
                "description": "False if this screen does not show the situation at all.",
            },
            "phrase": {
                "type": "string",
                "description": "The exact wording, copied character for character from the "
                               "screen. Choose the shortest phrase that is unambiguous, and "
                               "leave out anything specific to this attempt, such as the "
                               "identifier that was searched for.",
            },
        },
        "required": ["found"],
    },
}


async def verify_outcomes(
    engine: ReplayEngine, surface: Any, client: Any, model: str,
    spec: GoalSpec, artifact: CapabilityArtifact, *, institution: str,
) -> tuple[list[BusinessOutcome], list[str]]:
    """Drive each declared outcome and keep only the ones the app really shows."""
    verified: list[BusinessOutcome] = []
    dropped: list[str] = []

    for declared in spec.outcomes:
        if not declared.trigger:
            dropped.append(f"{declared.code} (no trigger declared, so it cannot be provoked)")
            continue

        # A trigger names only the value that causes the outcome. The rest of
        # the contract still has to be satisfied, or the run is refused for
        # invalid input and never reaches the screen we came to look at.
        # From the artifact, not the goal spec: the compiler has corrected the
        # contract against what the application really offers, and the goal spec
        # still holds what was guessed before anything had been seen.
        base = {i.name: i.example for i in artifact.inputs if i.example}
        params = {**base, **declared.trigger}
        await engine.run(artifact, params, institution=institution, unattended=False)
        observation = await surface.observe()
        screen = "\n".join(f"--- {k or 'main'} ---\n{v}" for k, v in observation.frames.items())

        reported = await _ask_wording(client, model, declared.description, screen)
        if reported is None:
            dropped.append(f"{declared.code} (the trigger did not produce it)")
            continue

        phrase = _without_trigger_values(reported, params)
        if len(phrase) < MIN_DETECTOR_CHARS:
            dropped.append(
                f"{declared.code} (the only wording found was the value we searched for)"
            )
            continue
        if not any(phrase in snapshot for snapshot in observation.frames.values()):
            dropped.append(f"{declared.code} (the reported wording was not on screen)")
            continue

        verified.append(BusinessOutcome(
            code=declared.code,
            description=declared.description,
            detector=TextPresent(text=phrase, frame=_frame_containing(observation, phrase)),
        ))

    return verified, dropped


async def _ask_wording(client: Any, model: str, situation: str, screen: str) -> str | None:
    """Ask what this screen actually says, rather than trusting what was predicted."""
    response = await client.messages.create(
        model=model,
        max_tokens=512,
        tools=[WORDING_TOOL],
        tool_choice={"type": "tool", "name": WORDING_TOOL["name"]},
        messages=[{
            "role": "user",
            "content": (
                f"Situation: {situation}\n\n"
                f"This is the screen the application produced. If it shows that "
                f"situation, report the exact wording that signals it.\n\n{screen}"
            ),
        }],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == WORDING_TOOL["name"]:
            args = dict(block.input)
            if args.get("found") and args.get("phrase"):
                return args["phrase"].strip()
    return None


def _frame_containing(observation: Any, phrase: str) -> list[str]:
    for key, snapshot in observation.frames.items():
        if phrase in snapshot:
            return [part for part in key.split("/") if part]
    return []


def _without_trigger_values(phrase: str, trigger: dict[str, str]) -> str:
    """Strip the values this probe searched for out of the wording it produced.

    The application echoes what was asked for: "NO MEMBER MATCHING 999999". The
    number is this probe's, not the outcome's, and a detector carrying it would
    only ever fire for the one probe that recorded it.
    """
    cleaned = phrase
    for value in trigger.values():
        if value:
            cleaned = cleaned.replace(value, " ")
    return " ".join(cleaned.split()).strip(" -:,.")
