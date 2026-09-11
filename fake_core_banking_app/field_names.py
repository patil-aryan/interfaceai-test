"""Generated form-field names that change whenever the server restarts.

Real vendor form builders emit names like `fld_00427` and renumber them between
releases. Nothing in a recorded artifact may depend on these, which is the whole
point of the locator fallback chain.

Set MERIDIAN_BUILD_ID to pin them for debugging.
"""

from __future__ import annotations

import os
import random

BUILD_ID = os.environ.get("MERIDIAN_BUILD_ID") or f"{random.randrange(1, 9999):04d}"

_rng = random.Random(BUILD_ID)
_assigned: dict[str, str] = {}


def field_name(logical: str) -> str:
    """Return this build's generated name for a logical control."""
    if logical not in _assigned:
        prefix = "btn" if logical.endswith("_button") else "fld"
        _assigned[logical] = f"{prefix}_{_rng.randrange(10_000, 99_999)}"
    return _assigned[logical]
