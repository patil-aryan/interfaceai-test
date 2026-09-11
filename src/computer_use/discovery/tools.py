"""The action vocabulary, expressed as tools a model can call.

The same `Action` enum is the model's tool list, the artifact's step types and
the replay engine's dispatch table. One vocabulary, three consumers, nothing to
translate between them.
"""

from __future__ import annotations

from typing import Any

from computer_use.schema.capability import (
    Action,
    ContainerWithTextScope,
    ElementTarget,
    LiteralValue,
    RoleLocator,
    RoleNameLocator,
)

FINISH = "done"

_ELEMENT_PROPERTIES: dict[str, Any] = {
    "role": {
        "type": "string",
        "description": "Accessible role exactly as the snapshot shows it: "
                       "textbox, button, link, cell, row, checkbox, combobox.",
    },
    "name": {
        "type": "string",
        "description": "Accessible name, the quoted text after the role in the "
                       "snapshot. Omit only when the element has no name.",
    },
    "frame": {
        "type": "string",
        "description": "Which frame the element is in, as titled in the "
                       "observation. Use an empty string for the main document.",
    },
    "within_row": {
        "type": "string",
        "description": "Optional. Text identifying the row the element sits in. "
                       "Use this when several elements share a role and name.",
    },
    "nth": {
        "type": "integer",
        "description": "Optional, zero-based. Which match to use when more than one fits.",
    },
}

_INTENT = {
    "type": "string",
    "description": "One plain sentence describing why you are doing this, written "
                   "for a human reviewing the saved capability later.",
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {"intent": _INTENT, **properties},
            "required": ["intent", *required],
        },
    }


def _element_tool(name: str, description: str, extra: dict[str, Any] | None = None,
                  extra_required: list[str] | None = None) -> dict:
    return _tool(name, description, {**_ELEMENT_PROPERTIES, **(extra or {})},
                 ["role", "frame", *(extra_required or [])])


def tool_definitions(output_names: list[str]) -> list[dict]:
    """The tools handed to the model for one discovery run."""
    return [
        _tool(
            Action.NAVIGATE.value,
            "Go to an entry point. Use a path such as / or /login, not a full URL.",
            {"path": {"type": "string", "description": "Path to open, e.g. /"}},
            ["path"],
        ),
        _element_tool(
            Action.FILL.value,
            "Type a value into a field, replacing whatever is there.",
            {"value": {"type": "string", "description": "The text to type."}},
            ["value"],
        ),
        _element_tool(
            Action.ACTIVATE.value,
            "Invoke a control: press a button, follow a link, choose a menu item. "
            "There is deliberately no 'click' here, because the same recorded flow "
            "must run on a surface that has no mouse.",
        ),
        _element_tool(
            Action.SELECT.value,
            "Choose one option from a dropdown.",
            {"option": {"type": "string", "description": "The option to choose."}},
            ["option"],
        ),
        _element_tool(
            Action.DISMISS.value,
            "Close an interstitial, dialog or confirmation that is in the way. "
            "Use this rather than activate, so the saved capability records that "
            "this flow expects to be interrupted here.",
        ),
        _tool(
            Action.PRESS.value,
            "Send a key to whatever currently has focus, such as Enter or F3.",
            {"key": {"type": "string", "description": "Key name, e.g. Enter, Tab, F3."}},
            ["key"],
        ),
        _tool(
            Action.WAIT_FOR.value,
            "Wait until a piece of text appears. Use this when a screen is still loading.",
            {
                "text": {"type": "string", "description": "Text that must appear."},
                "frame": _ELEMENT_PROPERTIES["frame"],
            },
            ["text", "frame"],
        ),
        _element_tool(
            Action.READ.value,
            "Extract a value from the screen into one of the capability's declared "
            "outputs. Identify the element by where it sits, never by the value it "
            "currently holds: that value is different for every invocation.",
            {"output": {
                "type": "string",
                "enum": output_names,
                "description": "Which declared output this fills.",
            }},
            ["output"],
        ),
        {
            "name": FINISH,
            "description": "Call this once the goal is met. Supply proof that the screen "
                           "really shows the requested record, not merely that a screen "
                           "loaded. Prefer matching an element against an input parameter.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "What was achieved."},
                    "proof_kind": {
                        "type": "string",
                        "enum": ["element_equals_input", "text_present"],
                        "description": "element_equals_input is stronger and is preferred: "
                                       "it proves the record on screen is the one requested.",
                    },
                    "proof_input": {
                        "type": "string",
                        "description": "For element_equals_input: which input the element must equal.",
                    },
                    "proof_text": {
                        "type": "string",
                        "description": "For text_present: the text that must be on screen.",
                    },
                    **_ELEMENT_PROPERTIES,
                },
                "required": ["summary", "proof_kind", "frame"],
            },
        },
    ]


def frame_path(value: str | None) -> list[str]:
    """Turn the model's frame string into the frame path the surface expects."""
    if not value or value in (".", "/", "main"):
        return []
    return [part for part in value.split("/") if part]


def lookup_target(args: dict[str, Any], description: str) -> ElementTarget:
    """A throwaway target for finding the element once, right now.

    Deliberately not what gets recorded. The model describes the control well
    enough to reach it in the current page; `Surface.describe_target` then
    harvests the chain that goes into the artifact. Keeping these apart is what
    stops the model's guess about robustness becoming the capability's.
    """
    role = args["role"]
    name = args.get("name")
    strategies = [RoleNameLocator(role=role, name=name, exact=False)] if name else []
    strategies.append(RoleLocator(role=role))

    scope = None
    if args.get("within_row"):
        scope = ContainerWithTextScope(
            container_role="row", text=LiteralValue(value=args["within_row"])
        )

    return ElementTarget(
        description=description,
        frame=frame_path(args.get("frame")),
        scope=scope,
        strategies=strategies,
        recorded_strategy=strategies[0].strategy,
        nth=int(args.get("nth") or 0),
    )
