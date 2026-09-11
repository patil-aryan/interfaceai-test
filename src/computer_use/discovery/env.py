"""Reading local configuration out of a .env file, without a dependency for it."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path | str = ".env") -> None:
    """Read KEY=VALUE lines into the environment. Anything already set wins."""
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())
