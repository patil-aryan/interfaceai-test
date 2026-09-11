"""Choosing which provider an application needs. One place, so every caller agrees."""

from __future__ import annotations

from computer_use.schema.capability import SurfaceKind
from computer_use.schema.profile import AppProfile
from computer_use.surfaces.base import Surface
from computer_use.surfaces.terminal import TerminalSurface
from computer_use.surfaces.web import WebSurface


def build_surface(
    profile: AppProfile, *, headed: bool = False, slow_mo_ms: int = 0
) -> Surface:
    """Build the provider this application needs. The profile knows what kind of thing it is.

    This is the whole seam. Everything above returns a `Surface`, and nothing
    above knows whether it is talking to a browser or to a character screen.
    """
    if profile.surface_kind is SurfaceKind.TERMINAL:
        return TerminalSurface(slow_mo_ms=slow_mo_ms, show=headed)
    return WebSurface(headless=not headed, slow_mo_ms=slow_mo_ms)
