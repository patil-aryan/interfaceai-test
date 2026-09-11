"""The wire between a terminal and the application, kept legible on purpose.

A real 3270 buffers typing locally and transmits the whole screen when an
attention key is pressed. That is modelled here rather than sending keystrokes,
because it is what makes `fill` a local act and `activate` the round trip.
"""

from __future__ import annotations

ROWS = 24
COLS = 80

# Written before every screen, so a reader knows where one screen ends and the
# next begins without guessing from timing.
SCREEN_START = "\f"

# One inbound transmission: the attention key, then each input field the
# terminal is holding, addressed by where it sits on the screen.
#     KEY=ENTER|5,20=operator1|7,20=changeme
KEY_PREFIX = "KEY="
SEPARATOR = "|"


def encode(key: str, fields: dict[tuple[int, int], str]) -> str:
    """Build one inbound transmission."""
    parts = [f"{KEY_PREFIX}{key}"]
    parts += [f"{row},{col}={value}" for (row, col), value in sorted(fields.items())]
    return SEPARATOR.join(parts)


def decode(line: str) -> tuple[str, dict[tuple[int, int], str]]:
    """Read one inbound transmission back into an attention key and field values."""
    key = "ENTER"
    fields: dict[tuple[int, int], str] = {}
    for part in line.strip().split(SEPARATOR):
        if part.startswith(KEY_PREFIX):
            key = part[len(KEY_PREFIX):]
            continue
        if "=" not in part:
            continue
        where, value = part.split("=", 1)
        row, _, col = where.partition(",")
        if row.strip().isdigit() and col.strip().isdigit():
            fields[(int(row), int(col))] = value
    return key, fields
