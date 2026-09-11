"""Saved artifacts, presented to an AI agent as callable tools.

This is the whole point of the system stated in one file. An agent does not
reason about a screen; it reads a catalog of capabilities, picks one by name,
and calls it with typed arguments. The typed contract that made an artifact
reviewable is the same thing that makes it callable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from computer_use.schema.capability import ApprovalStatus, CapabilityArtifact, InputParam

CAPABILITY_DIR = Path("artifacts/capabilities")

# A decimal is offered as a string. Handing an agent a JSON number invites it
# back as a float, which is the one thing money must never be.
JSON_TYPES = {
    "string": "string",
    "enum": "string",
    "decimal": "string",
    "date": "string",
    "integer": "integer",
    "boolean": "boolean",
}


def load_catalog(directory: Path | str = CAPABILITY_DIR) -> list[CapabilityArtifact]:
    """Every saved capability, newest version of each id."""
    best: dict[str, CapabilityArtifact] = {}
    for path in sorted(Path(directory).glob("*.json")):
        artifact = CapabilityArtifact.model_validate_json(path.read_text(encoding="utf-8"))
        if artifact.status is ApprovalStatus.DEPRECATED:
            continue
        held = best.get(artifact.id)
        if held is None or artifact.version > held.version:
            best[artifact.id] = artifact
    return [best[key] for key in sorted(best)]


def tool_name(capability_id: str) -> str:
    """Capability ids are dotted; tool names may only hold letters, digits and underscores."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", capability_id)[:64]


def by_tool_name(catalog: list[CapabilityArtifact]) -> dict[str, CapabilityArtifact]:
    return {tool_name(a.id): a for a in catalog}


def tool_schema(artifact: CapabilityArtifact) -> dict[str, Any]:
    """One capability, described so an agent can decide whether it fits and call it."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for declared in artifact.inputs:
        properties[declared.name] = _describe_input(declared)
        if declared.required:
            required.append(declared.name)

    return {
        "name": tool_name(artifact.id),
        "description": _describe(artifact),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def catalog_tools(catalog: list[CapabilityArtifact]) -> list[dict[str, Any]]:
    return [tool_schema(a) for a in catalog]


def _describe_input(declared: InputParam) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": JSON_TYPES.get(declared.type, "string"),
        "description": declared.description,
    }
    if declared.values:
        schema["enum"] = declared.values
    if declared.pattern:
        schema["description"] += f" Must match {declared.pattern}."
    if declared.example:
        schema["description"] += f" For example {declared.example}."
    return schema


def _describe(artifact: CapabilityArtifact) -> str:
    """What the capability does, what it returns, and how it can legitimately not succeed.

    The outcomes matter as much as the outputs: an agent that does not know
    MEMBER_NOT_FOUND is a possible answer will treat it as a fault.
    """
    lines = [artifact.description.strip()]

    if artifact.outputs:
        lines.append("")
        lines.append("Returns:")
        lines += [f"  {o.name} ({o.type}) - {o.description}" for o in artifact.outputs]

    if artifact.outcomes:
        lines.append("")
        lines.append("May instead report one of these, which are answers and not errors:")
        lines += [f"  {o.code} - {o.description}" for o in artifact.outcomes]

    lines.append("")
    lines.append(
        f"Risk: {artifact.risk_tier.value}. Approval: {artifact.status.value}. "
        f"Version {artifact.version}, recorded against {artifact.provenance.recorded_against_institution}."
    )
    if artifact.risk_tier.value != "read_only":
        lines.append(
            "This changes data. It is refused unless the deployment's allowlist "
            "permits it, and a person is asked before the step that commits."
        )
    return "\n".join(lines)
