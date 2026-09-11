"""Injectable faults, so every branch of the error taxonomy is demonstrable."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class FaultState:
    slow_response_ms: int = 0  # transient slowness -> recoverable
    session_expired: bool = False  # forces re-authentication
    maintenance_interstitial: bool = False  # unexpected dialog, every request
    interstitial_once: bool = False  # unexpected dialog, clears when dismissed
    validation_error: bool = False  # form rejects valid input
    permission_denied: bool = False  # operator lacks rights
    app_error: bool = False  # the product's own 500 screen
    force_not_found: bool = False  # every lookup returns no match
    expire_after_requests: int = 0  # sign-on succeeds, then the session dies mid-flow
    _requests_since_sign_on: int = 0

    def note_request(self) -> bool:
        """Count an authenticated request. True the once the session should die.

        Fires a single time, like a real timeout: sign on again and work
        continues. A fault that expired the session forever would only ever
        demonstrate giving up.
        """
        if self.expire_after_requests <= 0:
            return False
        self._requests_since_sign_on += 1
        if self._requests_since_sign_on <= self.expire_after_requests:
            return False
        self.expire_after_requests = 0
        return True

    def as_dict(self) -> dict:
        return asdict(self)

    def clear(self) -> None:
        for f, default in (
            ("slow_response_ms", 0), ("session_expired", False),
            ("maintenance_interstitial", False), ("validation_error", False),
            ("permission_denied", False), ("app_error", False),
            ("force_not_found", False), ("interstitial_once", False),
            ("expire_after_requests", 0), ("_requests_since_sign_on", 0),
        ):
            setattr(self, f, default)


FAULTS = FaultState()
