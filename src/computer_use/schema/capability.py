"""The capability artifact: a recorded UI flow, saved as a callable contract.

This is the file a discovery run produces and a replay run consumes. It is
deliberately decoupled from the model transcript that produced it -- the
transcript is evidence, this is the compiled program.

Two audiences read it: a human reviewer approving an automation that will touch
member accounts, and an AI agent deciding whether this capability answers the
task in front of it. Every field is shaped for one of those two.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class Action(str, Enum):
    """What a step does.

    The test for membership: could a human operator do this at a green-screen
    terminal? There is deliberately no `click`, no `evaluate_javascript`, and no
    `query_selector` -- those would weld the schema to a browser and break the
    promise that this same artifact shape describes a desktop or terminal flow.
    """

    NAVIGATE = "navigate"  # go to an entry point (a URL, a transaction code)
    FILL = "fill"  # put a value into a named control
    ACTIVATE = "activate"  # invoke a control (button, link, menu item)
    SELECT = "select"  # choose one option from a set
    PRESS = "press"  # send a raw key (Enter, Tab, F3)
    READ = "read"  # extract a value into a declared output
    WAIT_FOR = "wait_for"  # block until a condition holds
    ASSERT = "assert"  # verify a condition, no state change
    DISMISS = "dismiss"  # handle a known interstitial or dialog


class SurfaceKind(str, Enum):
    """Which perception/action provider can execute this artifact."""

    WEB = "web"
    LEGACY_WEB = "legacy_web"
    TERMINAL = "terminal"
    DESKTOP = "desktop"


class Sensitivity(str, Enum):
    """Drives redaction. Not decoration -- the evidence writer reads this.

    PUBLIC   flows freely.
    INTERNAL returned to the caller, written to logs.
    PII      returned to the caller, masked in logs and evidence.
    SECRET   never returned, never logged, never stored in an artifact.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    SECRET = "secret"


class RiskTier(str, Enum):
    """How conservatively the guardrails treat this capability as a whole."""

    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    IRREVERSIBLE_WRITE = "irreversible_write"


class ApprovalStatus(str, Enum):
    """Gates unattended replay. A draft may be replayed only with a human watching."""

    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


# --------------------------------------------------------------------------
# Values: literal, or bound to an input parameter
# --------------------------------------------------------------------------


class LiteralValue(BaseModel):
    kind: Literal["literal"] = "literal"
    value: str


class ParamRef(BaseModel):
    """A value supplied by the caller at replay time.

    Promoting recorded literals to parameter references is the whole point of
    the compile step. A flow recorded against member 100234 is worthless; a flow
    parameterised on `member_id` is a capability.
    """

    kind: Literal["param"] = "param"
    param: str


ValueSource = Annotated[LiteralValue | ParamRef, Field(discriminator="kind")]


# --------------------------------------------------------------------------
# Element targeting: the locator ladder
# --------------------------------------------------------------------------


class RoleNameLocator(BaseModel):
    """Accessible role plus accessible name. The first strategy tried.

    Survives markup churn, works on legacy tables, and has a direct analogue in
    the OS accessibility APIs used for desktop surfaces.
    """

    strategy: Literal["role_name"] = "role_name"
    role: str
    name: str
    exact: bool = False


class RoleLocator(BaseModel):
    """Role alone, with no name. Only valid inside a scope that narrows to one."""

    strategy: Literal["role"] = "role"
    role: str


class LabelLocator(BaseModel):
    """The control associated with this visible label text."""

    strategy: Literal["label"] = "label"
    text: str


class PlaceholderLocator(BaseModel):
    strategy: Literal["placeholder"] = "placeholder"
    text: str


class TextLocator(BaseModel):
    """Matched on its own visible text. Common for links in legacy apps."""

    strategy: Literal["text"] = "text"
    text: str


class StructuralLocator(BaseModel):
    """A DOM path, scoped to a named region. Brittle by nature, kept as a fallback."""

    strategy: Literal["structural"] = "structural"
    css: str


class GridLocator(BaseModel):
    """Row/column addressing for character-grid surfaces (terminal emulators)."""

    strategy: Literal["grid"] = "grid"
    row: int
    column: int
    length: int | None = None


class CoordinateLocator(BaseModel):
    """Viewport coordinates. Last resort, always flagged."""

    strategy: Literal["coordinates"] = "coordinates"
    x: int
    y: int
    viewport_width: int
    viewport_height: int


Locator = Annotated[
    RoleNameLocator | RoleLocator | LabelLocator | PlaceholderLocator | TextLocator | StructuralLocator | GridLocator | CoordinateLocator,
    Field(discriminator="strategy"),
]

# Strategies that address a control by what it *is*, rather than by where it sits
# or what shape of markup surrounds it. A target with none of these is held in
# place only by its position.
SEMANTIC_STRATEGIES = ("role_name", "label", "placeholder", "text")


class RegionScope(BaseModel):
    """Search only inside a named landmark, e.g. the Member Search panel."""

    kind: Literal["region"] = "region"
    role: str
    name: str


class ContainerWithTextScope(BaseModel):
    """Search only inside the container holding this text.

    The text may be bound to an input parameter, which is how a step targets
    "the SELECT link in the row for the member number I was given".

    Nested table layouts make ancestor containers match too, because their text
    includes their children's. Resolution therefore takes the *innermost* match:
    the matching container that contains no other matching container.
    """

    kind: Literal["container_with_text"] = "container_with_text"
    container_role: str = "row"  # row, listitem, group, cell, region
    text: ValueSource


Scope = Annotated[
    RegionScope | ContainerWithTextScope,
    Field(discriminator="kind"),
]


class ElementTarget(BaseModel):
    """How to find one control, with fallbacks, ordered best-first.

    Replay tries `strategies` in order and records which one resolved. Falling
    back to a later strategy than `recorded_strategy` is the drift signal: the
    flow still works, but the surface has moved under us.
    """

    description: str  # human-readable, for review and for failure messages
    frame: list[str] = Field(default_factory=list)  # frame path for framesets
    scope: Scope | None = None  # narrow the search to a container, if any
    strategies: list[Locator]
    recorded_strategy: str
    nth: int = 0  # disambiguator when several controls match


def is_unanchored(target: ElementTarget | None) -> bool:
    """Whether a target is held in place by nothing but its position on screen.

    `role=cell, nth=27` counts cells from the top of the frame. It is correct on
    the screen it was recorded from and wrong the moment that screen grows a
    row, which happens when the member being read simply has one more account
    than the member it was recorded against. A scope or a strategy that names
    the element is enough to anchor it; a bare position is not.
    """
    if target is None or target.scope is not None or target.nth == 0:
        return False
    return not any(s.strategy in SEMANTIC_STRATEGIES for s in target.strategies)


# --------------------------------------------------------------------------
# Conditions: used for both step checkpoints and business-outcome detectors
# --------------------------------------------------------------------------


class TextPresent(BaseModel):
    kind: Literal["text_present"] = "text_present"
    text: str
    frame: list[str] = Field(default_factory=list)


class TextAbsent(BaseModel):
    kind: Literal["text_absent"] = "text_absent"
    text: str
    frame: list[str] = Field(default_factory=list)


class ElementPresent(BaseModel):
    kind: Literal["element_present"] = "element_present"
    target: ElementTarget


class ElementAbsent(BaseModel):
    """The control is not on this screen.

    The mirror of element_present, and the only way to state a situation whose
    signal is a missing row rather than a message: a member who holds no savings
    account has no savings row, and the application says nothing about it.
    """

    kind: Literal["element_absent"] = "element_absent"
    target: ElementTarget


class ValueEquals(BaseModel):
    kind: Literal["value_equals"] = "value_equals"
    target: ElementTarget
    expected: ValueSource


class UrlMatches(BaseModel):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str  # regex, matched against the current location


Condition = Annotated[
    TextPresent | TextAbsent | ElementPresent | ElementAbsent | ValueEquals | UrlMatches,
    Field(discriminator="kind"),
]


class Checkpoint(BaseModel):
    """Proof that a step actually did what it claimed.

    Without this, replay is a sequence of hopeful clicks. The brief's own
    glossary calls a checkpoint the difference between reaching a state and
    assuming you did.
    """

    condition: Condition
    timeout_ms: int = 5_000
    description: str | None = None


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


class OnFailure(str, Enum):
    """What replay does when a step's checkpoint does not hold in time."""

    FAIL = "fail"  # hard failure, stop and surface a debuggable error
    CLASSIFY = "classify"  # test the declared outcome detectors first
    RETRY = "retry"  # transient condition, try again up to `retries`
    ESCALATE = "escalate"  # route to a human operator
    SKIP = "skip"  # optional step (an interstitial that may not appear)


class Step(BaseModel):
    id: str
    intent: str  # one plain sentence, written for a human reviewer
    action: Action

    target: ElementTarget | None = None  # None for navigate / press / wait_for
    value: ValueSource | None = None  # for fill / select / navigate / press
    output: str | None = None  # for read: the declared output it populates

    checkpoint: Checkpoint | None = None
    condition: Condition | None = None  # for wait_for / assert

    timeout_ms: int = 10_000
    on_failure: OnFailure = OnFailure.FAIL
    retries: int = 0

    irreversible: bool = False  # guardrails gate these; see RiskTier


# --------------------------------------------------------------------------
# Contract: what the caller supplies and what it gets back
# --------------------------------------------------------------------------


class InputParam(BaseModel):
    name: str
    type: Literal["string", "integer", "decimal", "boolean", "date", "enum"]
    required: bool = True
    description: str
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    pattern: str | None = None  # regex for string inputs
    values: list[str] | None = None  # allowed values for enum inputs
    example: str | None = None  # shown in the agent-facing catalog

    # True only for identifiers that select exactly one record. A surname never
    # qualifies; a member number does.
    unique_key: bool = False


class OutputField(BaseModel):
    name: str
    type: Literal["string", "integer", "decimal", "boolean", "date", "enum"]
    description: str
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    values: list[str] | None = None


class BusinessOutcome(BaseModel):
    """A legitimate result that is not success and is not a crash.

    "No such member" is an answer the caller needs, not an exception. Declaring
    these in the artifact is what lets replay return them as data instead of
    failing, and lets a calling agent branch on them.
    """

    code: str  # e.g. MEMBER_NOT_FOUND
    description: str
    detector: Condition
    terminal: bool = True

    # Where this detector means anything, if it does not mean the same thing
    # everywhere. "NO MEMBER MATCHING" is specific wording and says the same
    # thing on any screen that shows it. The absence of a savings row is true of
    # the sign-on screen, the search results and every error page, so it is only
    # an answer at the step that went looking for one.
    at_step: str | None = None


# --------------------------------------------------------------------------
# Provenance and confidence
# --------------------------------------------------------------------------


class Provenance(BaseModel):
    """Where this artifact came from. Points at the transcript, never embeds it."""

    goal: str  # the natural-language goal the discovery run was given
    discovery_run_id: str
    model: str
    recorded_at: datetime
    recorded_against_institution: str
    surface_kind: SurfaceKind


class Stability(BaseModel):
    """How much we trust this artifact, measured rather than asserted."""

    replays: int = 0
    successes: int = 0
    last_verified_at: datetime | None = None
    degradations_seen: int = 0  # times a locator needed a fallback strategy


# --------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------


class CapabilityArtifact(BaseModel):
    """One recorded flow, versioned and reviewable.

    Resolution order at load time is app profile, then this artifact, then an
    optional institution override. This artifact is the base recording: it
    should hold nothing that is true of the application generally (that belongs
    in the app profile) and nothing specific to one institution (that belongs in
    an override).
    """

    schema_version: str = SCHEMA_VERSION

    id: str  # stable, e.g. "member.savings_balance"
    version: str  # semver of this capability
    title: str
    description: str  # what a calling agent reads to decide if this fits

    app_profile: str  # e.g. "meridian-core@8.4"
    status: ApprovalStatus = ApprovalStatus.DRAFT
    risk_tier: RiskTier = RiskTier.READ_ONLY

    # Declared precondition. How a session is established lives in the app
    # profile, not here -- the capability only states whether it needs one.
    requires_authenticated_session: bool = True

    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    steps: list[Step]

    # Asserted once, after the final step. Distinct from the per-step
    # checkpoints: every step can pass and still leave the session somewhere
    # other than the state the caller was promised. This is the condition the
    # replay result's `success` status actually means.
    success_condition: Checkpoint

    outcomes: list[BusinessOutcome] = Field(default_factory=list)

    provenance: Provenance
    stability: Stability = Field(default_factory=Stability)

    @model_validator(mode="after")
    def _irreversible_needs_unique_key(self) -> CapabilityArtifact:
        if self.risk_tier is RiskTier.IRREVERSIBLE_WRITE and not any(
            i.unique_key for i in self.inputs
        ):
            raise ValueError(
                "an irreversible capability must declare at least one input with "
                "unique_key=True; acting on a non-unique identifier such as a "
                "surname risks operating on the wrong record"
            )
        return self
