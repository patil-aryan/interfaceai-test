"""What is true of a vendor product, regardless of which flow is running.

A capability describes one task. This describes the application: how a session
is established, and which screens mean something other than "the step failed".
Kept apart so that fixing a session-expiry screen fixes every capability at once.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from computer_use.schema.capability import Condition, ElementTarget, SurfaceKind
from computer_use.schema.result import FailureClass, RecoveryKind

PROFILE_DIR = Path("artifacts/app_profiles")


class SignOn(BaseModel):
    """How a session is established. Credentials are supplied at run time, never stored."""

    path: str
    username_field: ElementTarget
    password_field: ElementTarget
    submit: ElementTarget
    success: Condition


class KnownScreen(BaseModel):
    """A screen this product shows that means something other than a failed step."""

    name: str
    detector: Condition

    # Exactly one of these says what it means.
    classification: FailureClass | None = None  # stop, and report it precisely
    recovery: RecoveryKind | None = None  # handle it and carry on

    # For a screen that can be cleared, the control that clears it.
    dismiss: ElementTarget | None = None

    @property
    def recoverable(self) -> bool:
        return self.recovery is not None


class AppProfile(BaseModel):
    """One vendor product at one version."""

    id: str  # e.g. meridian-core@8.4
    vendor: str
    surface_kind: SurfaceKind = SurfaceKind.LEGACY_WEB

    sign_on: SignOn | None = None
    known_screens: list[KnownScreen] = Field(default_factory=list)


def load_app_profile(profile_id: str, directory: Path | str = PROFILE_DIR) -> AppProfile:
    """Read the profile a capability names. A missing one is an error, not a default."""
    path = Path(directory) / f"{profile_id}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"capability names app profile {profile_id!r}, but {path} does not exist"
        )
    return AppProfile.model_validate_json(path.read_text(encoding="utf-8"))
