"""The human-written half of a capability: its contract. The model discovers the flow."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from computer_use.schema.capability import (
    InputParam,
    OutputField,
    RiskTier,
    SurfaceKind,
)


class OutcomeSpec(BaseModel):
    """A legitimate non-success result, and the words on screen that signal it."""

    code: str  # e.g. MEMBER_NOT_FOUND
    description: str
    signal_text: str  # proposed wording; replaced by what the app really says

    # Inputs that provoke this outcome, so the detector can be confirmed against
    # the running application instead of trusted because a model suggested it.
    trigger: dict[str, str] = Field(default_factory=dict)


class GoalSpec(BaseModel):
    """What to discover, and the typed contract the resulting capability must honour."""

    id: str  # stable capability id, e.g. "member.savings_balance"
    version: str = "1.0.0"
    title: str
    description: str

    goal: str  # the plain-English instruction handed to the model
    app_profile: str
    institution: str = "pinecrest-cu"
    surface_kind: SurfaceKind = SurfaceKind.LEGACY_WEB

    risk_tier: RiskTier = RiskTier.READ_ONLY
    requires_authenticated_session: bool = True

    # Declared by a human, not proposed by the model. These become the
    # artifact's contract verbatim, and give the compiler the values it needs
    # to turn recorded literals into parameter references.
    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)

    # Answers the caller needs that are not success and are not crashes.
    outcomes: list[OutcomeSpec] = Field(default_factory=list)

    max_steps: int = 30

    def example_params(self) -> dict[str, str]:
        """The concrete values the discovery run drives the UI with."""
        return {i.name: i.example for i in self.inputs if i.example is not None}


def load_goal_spec(path: Path | str) -> GoalSpec:
    """Read a goal spec from JSON."""
    return GoalSpec.model_validate_json(Path(path).read_text(encoding="utf-8"))
