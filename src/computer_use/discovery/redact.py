"""Masking a discovery trace before it is written to disk.

The event log is redacted as it is produced. The trace was not, and it held more
than the log does: every value the run read, and the full text of every screen it
saw. Both are evidence, so both are redacted.
"""

from __future__ import annotations

import re

from computer_use.discovery.agent import DiscoveryOutcome
from computer_use.discovery.spec import GoalSpec
from computer_use.replay.engine import mask
from computer_use.schema.capability import Sensitivity

# Least to most restrictive. A value seen under two tags takes the stricter one.
STRICTNESS = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.PII: 2,
    Sensitivity.SECRET: 3,
}

# Below this length a value is too common to substitute: replacing every "25"
# on a screen would corrupt the evidence the trace is meant to be.
MIN_MASKABLE = 4

# Free prose the model wrote about a live screen. It can name anything it saw,
# including records this capability never read and so cannot recognise as
# sensitive. Masking the values we know about is not enough, so it is not kept;
# the scrubbed step intents say what was done.
FREE_TEXT = {"summary", "note"}
WITHHELD = "<withheld: free text written about a live screen>"


def redacted_trace(run: DiscoveryOutcome, spec: GoalSpec) -> DiscoveryOutcome:
    """A copy of the trace safe to keep: values masked, raw screen text dropped."""
    masks = _mask_map(run, spec)

    steps = []
    for recorded in run.steps:
        target = recorded.target
        if target is not None:
            target = target.model_copy(update={
                "description": _apply(target.description, masks)
            })
        steps.append(recorded.model_copy(update={
            "intent": _apply(recorded.intent, masks),
            "target": target,
            "value": _apply(recorded.value, masks),
            "read_value": _apply(recorded.read_value, masks),
            # A URL carrying a record id carries the record id.
            "urls_after": [_apply(u, masks) for u in recorded.urls_after],
            # A trace records what the automation did, not a copy of every screen
            # it passed through. Those screens hold other people's records too,
            # and nothing here can know which parts of them are sensitive. The
            # screenshots and snapshots taken at failure points are the evidence,
            # and they are captured deliberately.
            "frames_after": {
                frame: f"<{len(text)} characters of screen text, not retained>"
                for frame, text in recorded.frames_after.items()
            },
        }))

    return run.model_copy(update={
        "steps": steps,
        # Free prose the model wrote about a screen. It can name anything it
        # saw, including records this capability never read and therefore cannot
        # recognise as sensitive. Masking the values we know about is not enough,
        # so it is not kept. The scrubbed step intents say what was done.
        "summary": WITHHELD,
        "outputs": {name: _apply(str(value), masks) for name, value in run.outputs.items()},
        "proof": {
            key: WITHHELD if key in FREE_TEXT
            else (_apply(value, masks) if isinstance(value, str) else value)
            for key, value in (run.proof or {}).items()
        } or None,
    })


def _mask_map(run: DiscoveryOutcome, spec: GoalSpec) -> list[tuple[str, str]]:
    """Every raw value this run handled, paired with what it should read as.

    Keyed by the value rather than by the field, so that one value tagged two
    ways is masked the same in both places. The sub-account number arrived as a
    `pii` output and again as an `internal` one; masking per field left the
    second copy in the clear beside the first.
    """
    declared = {i.name: i.sensitivity for i in spec.inputs}
    declared.update({o.name: o.sensitivity for o in spec.outputs})

    strictest: dict[str, Sensitivity] = {}

    def note(raw: str | None, sensitivity: Sensitivity) -> None:
        if not raw or len(raw) < MIN_MASKABLE:
            return
        current = strictest.get(raw)
        if current is None or STRICTNESS[sensitivity] > STRICTNESS[current]:
            strictest[raw] = sensitivity

    for name, value in run.outputs.items():
        note(str(value), declared.get(name, Sensitivity.PII))
    for recorded in run.steps:
        note(recorded.read_value, declared.get(recorded.output or "", Sensitivity.PII))
        note(recorded.value, Sensitivity.PII)
    for declared_input in spec.inputs:
        note(declared_input.example, declared_input.sensitivity)

    # Longest first, so masking a member number does not leave fragments of the
    # account number that contains it.
    return sorted(
        ((raw, mask(raw, sensitivity)) for raw, sensitivity in strictest.items()),
        key=lambda pair: len(pair[0]), reverse=True,
    )


def _apply(text: str | None, masks: list[tuple[str, str]]) -> str | None:
    """Replace every known value, ignoring case.

    The model writes its summary in prose: it read `ELEANOR R VANCE` off the
    screen and wrote `Eleanor R Vance`. Matching exactly leaves that in the clear.
    """
    if text is None:
        return None
    for raw, masked in masks:
        text = re.sub(re.escape(raw), masked, text, flags=re.IGNORECASE)
    return text
