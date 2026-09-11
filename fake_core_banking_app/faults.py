"""Injectable faults, so every branch of the error taxonomy is demonstrable."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class FaultState:
    slow_response_ms: int = 0  # transient slowness -> recoverable
    session_expired: bool = False  # forces re-authentication
    maintenance_interstitial: bool = False  # unexpected dialog -> recoverable
    validation_error: bool = False  # form rejects valid input
    permission_denied: bool = False  # operator lacks rights
    app_error: bool = False  # the product's own 500 screen
    force_not_found: bool = False  # every lookup returns no match

    def as_dict(self) -> dict:
        return asdict(self)

    def clear(self) -> None:
        for f, default in (
            ("slow_response_ms", 0), ("session_expired", False),
            ("maintenance_interstitial", False), ("validation_error", False),
            ("permission_denied", False), ("app_error", False),
            ("force_not_found", False),
        ):
            setattr(self, f, default)


FAULTS = FaultState()
