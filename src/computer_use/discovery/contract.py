"""Turn a goal written in English into the typed contract a capability needs."""

from __future__ import annotations

import json
from typing import Any

from computer_use.discovery.spec import GoalSpec

# Success is the absence of every outcome, never an outcome itself. Models
# propose these anyway, and one of them would match on a perfectly good run.
NOT_OUTCOMES = {"SUCCESS", "OK", "COMPLETED", "COMPLETE", "FOUND", "DONE"}

CONTRACT_TOOL = {
    "name": "propose_contract",
    "description": "Propose the typed contract for the capability described by the goal.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Stable dotted identifier, lowercase, e.g. member.savings_balance",
            },
            "title": {"type": "string", "description": "Short human title."},
            "description": {
                "type": "string",
                "description": "One or two sentences a calling agent reads to decide "
                               "whether this capability fits its task.",
            },
            "restated_goal": {
                "type": "string",
                "description": "The goal restated for an operator, with the concrete "
                               "values replaced by the parameter they stand for.",
            },
            "risk_tier": {
                "type": "string",
                "enum": ["read_only", "reversible_write", "irreversible_write"],
                "description": "read_only if it only looks things up.",
            },
            "inputs": {
                "type": "array",
                "description": "Values the caller supplies each invocation. Any concrete "
                               "value in the goal, such as a member number, is an input.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string",
                                 "enum": ["string", "integer", "decimal", "boolean", "date", "enum"]},
                        "description": {"type": "string"},
                        "sensitivity": {"type": "string",
                                        "enum": ["public", "internal", "pii", "secret"]},
                        "pattern": {
                            "type": "string",
                            "description": "Regex the value must match in full. This is the "
                                           "capability's guard against being called with "
                                           "nonsense, so make it as tight as the real format "
                                           "allows: a six digit member number is ^[0-9]{6}$. "
                                           "Use .+ only when the format is genuinely free.",
                        },
                        "example": {"type": "string",
                                    "description": "The concrete value from the goal. "
                                                   "The discovery run drives the UI with this."},
                        "unique_key": {"type": "boolean",
                                       "description": "True only if this identifies exactly one "
                                                      "record. A surname does not; a member "
                                                      "number does."},
                    },
                    "required": ["name", "type", "description", "sensitivity", "example",
                                 "pattern"],
                },
            },
            "outputs": {
                "type": "array",
                "description": "Values the caller gets back.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string",
                                 "enum": ["string", "integer", "decimal", "boolean", "date", "enum"]},
                        "description": {"type": "string"},
                        "sensitivity": {"type": "string",
                                        "enum": ["public", "internal", "pii", "secret"]},
                        "values": {"type": "array", "items": {"type": "string"},
                                   "description": "Allowed values, for an enum output."},
                    },
                    "required": ["name", "type", "description", "sensitivity"],
                },
            },
            "outcomes": {
                "type": "array",
                "description": "Legitimate answers that are neither success nor a crash. "
                               "'No such member' is an answer the caller needs. Include the "
                               "ones a real operator would meet doing this task.",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string",
                                 "description": "SHOUTING_SNAKE_CASE, e.g. MEMBER_NOT_FOUND"},
                        "description": {"type": "string"},
                        "signal_text": {
                            "type": "string",
                            "description": "Wording you would expect on screen when this "
                                           "happens. It is checked against the real "
                                           "application and replaced by what it really says.",
                        },
                        "trigger": {
                            "type": "object",
                            "description": "Input values that provoke this outcome, as a flat "
                                           "object keyed by input name. For a record that does "
                                           "not exist, a well-formed identifier that is very "
                                           "unlikely to match. Omit if no input can cause it.",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["code", "description", "signal_text"],
                },
            },
        },
        "required": ["id", "title", "description", "restated_goal", "risk_tier",
                     "inputs", "outputs", "outcomes"],
    },
}

GUIDANCE = """\
You are defining the contract for a reusable automation of a back-office banking
application. A person has asked for something in plain English. Your job is not
to do it, but to say what the reusable version of it looks like.

The goal names concrete values, such as a particular member number. Those are
not part of the capability: they are what the caller supplies each time it is
invoked. Turn each one into an input, and put the concrete value in `example`.

Anything the person asked to be told is an output. Give each one a real type:
a money amount is decimal, a fixed set of states is enum.

Some tasks have legitimate answers that are not success: the record does not
exist, the operator lacks rights, the application rejects the entry. Those are
answers the caller needs, not crashes. Declare them as outcomes with the wording
you would expect to see on screen.

An outcome is never success. Do not declare SUCCESS, OK, COMPLETED or anything
meaning the task worked: success is the absence of every outcome, and is proved
separately. Declare only the ways this can legitimately not succeed.

Whenever an input value can cause an outcome, you must supply `trigger` with
values that cause it: an identifier that will not be found, an amount below a
stated minimum, a code that is not valid. Every declared outcome is provoked
against the real application, and one that cannot be provoked is thrown away
rather than shipped as a detector that will never fire. Prefer three outcomes
you can trigger over six you cannot.

Mark a member number, account number or similar record identifier as
`unique_key`. Mark anything identifying a person, including their name, balance
and identifiers, as `pii`. A password or token is `secret` and must never be an
output.
"""


async def propose_contract(
    client: Any, model: str, goal: str, *, app_profile: str, institution: str
) -> GoalSpec:
    """Ask the model for the typed contract implied by a goal sentence."""
    response = await client.messages.create(
        model=model,
        max_tokens=2048,
        system=GUIDANCE,
        tools=[CONTRACT_TOOL],
        tool_choice={"type": "tool", "name": CONTRACT_TOOL["name"]},
        messages=[{"role": "user", "content": f"Goal: {goal}"}],
    )

    proposal = None
    for block in response.content:
        if block.type == "tool_use" and block.name == CONTRACT_TOOL["name"]:
            proposal = dict(block.input)
    if proposal is None:
        raise RuntimeError(f"the model proposed no contract for: {goal}")

    return GoalSpec(
        id=proposal["id"],
        title=proposal["title"],
        description=proposal["description"],
        goal=proposal["restated_goal"],
        app_profile=app_profile,
        institution=institution,
        risk_tier=proposal["risk_tier"],
        inputs=proposal["inputs"],
        outputs=proposal["outputs"],
        outcomes=[
            o for o in proposal.get("outcomes", [])
            if o["code"].upper() not in NOT_OUTCOMES
        ],
    )


def write_spec(spec: GoalSpec, path: Any) -> None:
    """Save a proposed contract so a human can read, edit and re-run it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec.model_dump(mode="json"), indent=2) + "\n",
                    encoding="utf-8")
