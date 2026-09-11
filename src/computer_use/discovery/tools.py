"""The action vocabulary, expressed as tools a model can call.

The same `Action` enum is the model's tool list, the artifact's step types and
the replay engine's dispatch table. One vocabulary, three consumers, nothing to
translate between them.
"""

from __future__ import annotations

from typing import Any

from computer_use.schema.capability import Action
from computer_use.surfaces.base import TargetVocabulary

FINISH = "done"

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


def tool_definitions(output_names: list[str], vocabulary: TargetVocabulary) -> list[dict]:
    """The tools handed to the model for one discovery run.

    The verbs are the same on every surface, because they are the same verbs the
    artifact and the replay engine use. Only the words for naming a control come
    from the surface, which is the one part that cannot be shared.
    """
    def _element_tool(name: str, description: str, extra: dict[str, Any] | None = None,
                      extra_required: list[str] | None = None) -> dict:
        return _tool(name, description, {**vocabulary.properties, **(extra or {})},
                     [*vocabulary.required, *(extra_required or [])])

    wait_properties: dict[str, Any] = {
        "text": {"type": "string", "description": "Text that must appear."}
    }
    wait_required = ["text"]
    if "frame" in vocabulary.properties:
        wait_properties["frame"] = vocabulary.properties["frame"]
        wait_required.append("frame")

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
            wait_properties,
            wait_required,
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
                    **vocabulary.properties,
                },
                "required": ["summary", "proof_kind", *vocabulary.required],
            },
        },
    ]
