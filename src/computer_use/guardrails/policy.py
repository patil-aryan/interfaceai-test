"""The allowlist: what the system may do, checked before it acts. Deny by default."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from computer_use.schema.capability import (
    Action,
    ApprovalStatus,
    CapabilityArtifact,
    RiskTier,
    Step,
)

DEFAULT_ALLOWLIST_PATH = Path("allowlist.json")


class Decision(BaseModel):
    """Whether something is permitted, and the sentence explaining the answer."""

    allowed: bool
    reason: str


class Allowlist(BaseModel):
    """Explicit permissions for one deployment. Anything not listed is refused."""

    name: str = "default"

    # Host must match exactly, or match a leading "*." suffix pattern. An entry
    # containing a colon also pins the port.
    hosts: list[str] = Field(default_factory=list)

    # Regexes matched in full against the URL path.
    routes: list[str] = Field(default_factory=lambda: [".*"])

    actions: list[Action] = Field(default_factory=list)

    allow_irreversible: bool = False
    require_approval_for_unattended: bool = True

    def check_url(self, url: str) -> Decision:
        """Permit a navigation only if both its host and its path are listed."""
        parsed = urlparse(url)
        host = parsed.hostname or ""
        authority = f"{host}:{parsed.port}" if parsed.port else host

        if not _host_listed(host, authority, self.hosts):
            return Decision(
                allowed=False,
                reason=f"host {authority!r} is not in the allowlist {self.hosts}",
            )

        path = parsed.path or "/"
        if not any(re.fullmatch(pattern, path) for pattern in self.routes):
            return Decision(
                allowed=False,
                reason=f"path {path!r} matches none of the permitted routes {self.routes}",
            )

        return Decision(allowed=True, reason=f"{authority}{path} is permitted")

    def check_action(self, action: Action, *, irreversible: bool = False) -> Decision:
        """Permit an action verb, treating irreversible ones as a separate grant."""
        if action not in self.actions:
            return Decision(
                allowed=False,
                reason=f"action {action.value!r} is not permitted; allowed: "
                + ", ".join(a.value for a in self.actions),
            )
        if irreversible and not self.allow_irreversible:
            return Decision(
                allowed=False,
                reason="step is marked irreversible and this allowlist does not "
                "grant allow_irreversible",
            )
        return Decision(allowed=True, reason=f"action {action.value!r} is permitted")

    def check_step(self, step: Step, url: str | None = None) -> Decision:
        """Everything that can be judged about one step before performing it."""
        verdict = self.check_action(step.action, irreversible=step.irreversible)
        if not verdict.allowed:
            return verdict
        if url is not None:
            return self.check_url(url)
        return verdict

    def check_artifact(
        self, artifact: CapabilityArtifact, *, unattended: bool, will_commit: bool = True
    ) -> Decision:
        """Judge a whole capability before any of its steps run.

        `will_commit` is False for a run that stops before the first
        irreversible step. Such a run cannot change anything, so the
        irreversible grant does not apply to it.
        """
        if (
            unattended
            and self.require_approval_for_unattended
            and artifact.status is not ApprovalStatus.APPROVED
        ):
            return Decision(
                allowed=False,
                reason=f"capability status is {artifact.status.value!r}; unattended "
                f"replay requires {ApprovalStatus.APPROVED.value!r}",
            )

        if (
            will_commit
            and artifact.risk_tier is RiskTier.IRREVERSIBLE_WRITE
            and not self.allow_irreversible
        ):
            return Decision(
                allowed=False,
                reason="capability is tiered irreversible_write and this allowlist "
                "does not grant allow_irreversible",
            )

        unlisted = sorted({s.action.value for s in artifact.steps} - {a.value for a in self.actions})
        if unlisted:
            return Decision(
                allowed=False,
                reason=f"capability uses actions that are not permitted: {', '.join(unlisted)}",
            )

        return Decision(allowed=True, reason=f"capability {artifact.id} is permitted")


def _host_listed(host: str, authority: str, listed: list[str]) -> bool:
    """Match a host against exact entries, host:port entries, and *.suffix wildcards."""
    for entry in listed:
        if entry in (host, authority):
            return True
        if entry.startswith("*.") and host.endswith(entry[1:]):
            return True
    return False


def load_allowlist(path: Path | str = DEFAULT_ALLOWLIST_PATH) -> Allowlist:
    """Read an allowlist from JSON. A missing file is an error, never an open door."""
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(
            f"no allowlist at {file}; the system refuses to run without one"
        )
    return Allowlist.model_validate_json(file.read_text(encoding="utf-8"))
