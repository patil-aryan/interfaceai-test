"""The seam between how we perceive and act on a screen, and the recorded flow."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urljoin

from pydantic import BaseModel, Field

from computer_use.schema.capability import (
    Condition,
    ElementTarget,
    Locator,
    ParamRef,
    Scope,
    ValueSource,
)

# An opaque, surface-specific reference to one control. Callers never inspect it;
# they hand it back to the surface's action methods.
Handle = Any

DEFAULT_RESOLVE_TIMEOUT_MS = 5_000
POLL_INTERVAL_S = 0.15

# How long to wait for a screen to finish arriving before acting on it.
SETTLE_TIMEOUT_MS = 4_000


class SurfaceError(Exception):
    """Something went wrong at the surface, below the level of flow logic."""


class TargetNotFound(SurfaceError):
    """No location strategy resolved the control."""

    def __init__(self, target: ElementTarget, attempts: list[str]):
        self.target = target
        self.attempts = attempts
        super().__init__(
            f"could not resolve {target.description!r}; tried: " + "; ".join(attempts)
        )


class Resolution(BaseModel):
    """How a control was found, and whether the surface has drifted since recording."""

    strategy_used: str
    matches: int
    degraded: bool
    attempts: list[str] = Field(default_factory=list)


class Observation(BaseModel):
    """What the surface currently shows, in a form both a model and a human can read."""

    url: str
    title: str
    # Frame path joined by "/" -> that frame's accessibility snapshot.
    # The empty key is the main document.
    frames: dict[str, str] = Field(default_factory=dict)
    screenshot_path: str | None = None


def frame_path(value: str | None) -> list[str]:
    """Turn a model's frame string into the frame path a surface expects."""
    if not value or value in (".", "/", "main"):
        return []
    return [part for part in value.split("/") if part]


def resolve_value(source: ValueSource | None, params: dict[str, Any]) -> str:
    """Turn a literal or a parameter reference into the text to use."""
    if source is None:
        return ""
    if isinstance(source, ParamRef):
        if source.param not in params:
            raise SurfaceError(f"step needs parameter {source.param!r}, which was not supplied")
        return str(params[source.param])
    return source.value


class TargetVocabulary(BaseModel):
    """How a model is taught to point at something on this surface.

    A browser has roles and accessible names. A character screen has captions,
    rows and columns. Both are ways of saying "that one", and neither is
    meaningful on the other, so the surface owns its own words for it.
    """

    properties: dict[str, Any]
    required: list[str]

    # One sentence saying how to move to the value next to the one you reached.
    # "Raise nth" is sound advice among the cells of a marked-up row and useless
    # on a character grid, so the correction the loop gives has to come from
    # here rather than from the loop.
    narrowing_hint: str


class Surface(ABC):
    """A screen the agent can observe and act on.

    Implementations exist per surface kind: a browser, a terminal emulator, a
    desktop application. Everything above this class works only in terms of
    ElementTarget, Condition and the action verbs, so nothing upstream knows
    which kind it is talking to.

    The subclass supplies mechanism. This class owns two pieces of policy that
    must behave identically on every surface: the order in which location
    strategies are tried, and what counts as degradation.
    """

    kind: str = "abstract"

    # What a model needs to be told about reading this surface, inserted into
    # the discovery prompt. Perception differs; everything after it does not.
    perception_brief: str = ""

    # -- teaching a model to point ------------------------------------------

    @abstractmethod
    def target_vocabulary(self) -> TargetVocabulary:
        """The words a model may use to name one control here."""

    @abstractmethod
    def lookup(self, args: dict[str, Any], description: str) -> ElementTarget:
        """A throwaway target for finding an element once, right now.

        Deliberately not what gets recorded. The model describes the control
        well enough to reach it on the screen in front of it; `describe_target`
        then harvests the chain that goes into the artifact. Keeping these apart
        is what stops the model's guess about robustness becoming the
        capability's.
        """

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    # -- perception --------------------------------------------------------

    @abstractmethod
    async def observe(self, *, screenshot_path: str | None = None) -> Observation: ...

    @abstractmethod
    async def snapshot(self, frame: list[str]) -> str:
        """A readable structural dump of one frame, for evidence and for the model."""

    @abstractmethod
    async def screenshot(self, path: str) -> str: ...

    # -- targeting ---------------------------------------------------------

    async def resolve(
        self,
        target: ElementTarget,
        params: dict[str, Any],
        timeout_ms: int = DEFAULT_RESOLVE_TIMEOUT_MS,
    ) -> tuple[Handle, Resolution]:
        """Walk the fallback chain, retrying until the deadline, and return the first hit.

        Retrying matters because a screen that is still loading is
        indistinguishable from one that lacks the control. Failing instantly
        would turn ordinary slowness into a hard failure, which is exactly the
        confusion the result contract exists to avoid.

        Recording which strategy fired is the other half: succeeding on a later
        strategy than the one recorded means the surface moved under us, and is
        reported as a degradation rather than hidden.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        attempts: list[str] = []

        while True:
            attempts = []
            try:
                scope = await self._resolve_scope(target.scope, target.frame, params)
            except Exception as exc:
                attempts.append(f"scope: {exc}")
                scope = None
                if not attempts or time.monotonic() >= deadline:
                    raise TargetNotFound(target, attempts) from exc
                await asyncio.sleep(POLL_INTERVAL_S)
                continue

            for strategy in target.strategies:
                try:
                    handle, matches = await self._locate(
                        strategy, scope, target.frame, target.nth
                    )
                except Exception as exc:  # a strategy this surface cannot express
                    attempts.append(f"{strategy.strategy}: unsupported ({exc})")
                    continue

                attempts.append(f"{strategy.strategy}: {matches} match(es)")
                if matches == 0 or handle is None:
                    continue

                return handle, Resolution(
                    strategy_used=strategy.strategy,
                    matches=matches,
                    degraded=strategy.strategy != target.recorded_strategy,
                    attempts=attempts,
                )

            if time.monotonic() >= deadline:
                raise TargetNotFound(target, attempts)
            await asyncio.sleep(POLL_INTERVAL_S)

    async def wait_for(
        self, condition: Condition, params: dict[str, Any], timeout_ms: int
    ) -> tuple[bool, str]:
        """Poll a condition until it holds or the deadline passes.

        This is what a checkpoint actually is: not "is this true now" but "does
        this become true within the time this screen is allowed to take".
        """
        deadline = time.monotonic() + timeout_ms / 1000
        held, observed = False, "not evaluated"
        while True:
            try:
                held, observed = await self.check(condition, params)
            except Exception as exc:
                # A frame detaching mid-navigation, or a page still loading,
                # is 'not true yet', not a fault. Polling will ask again.
                held, observed = False, 'could not evaluate: ' + str(exc)
            if held or time.monotonic() >= deadline:
                return held, observed
            await asyncio.sleep(POLL_INTERVAL_S)

    @abstractmethod
    async def _resolve_scope(
        self, scope: Scope | None, frame: list[str], params: dict[str, Any]
    ) -> Handle | None:
        """Narrow the search area, or None for the whole frame.

        Implementations must return the *innermost* match: nested layouts make
        ancestor containers match too, because their text includes their
        children's, and picking an ancestor silently targets the wrong control.
        """

    @abstractmethod
    async def _locate(
        self, strategy: Locator, scope: Handle | None, frame: list[str], nth: int
    ) -> tuple[Handle | None, int]:
        """Apply one location strategy. Returns the handle and how many matched."""

    # -- actions -----------------------------------------------------------

    async def signature(self) -> str:
        """A comparable fingerprint of everything currently on screen."""
        observation = await self.observe()
        return "\n".join(f"--{k}--\n{v}" for k, v in observation.frames.items())

    async def settle(
        self, *, changed_from: str | None = None, timeout_ms: int = SETTLE_TIMEOUT_MS
    ) -> str:
        """Wait until the screen has changed from `changed_from`, then stopped changing.

        Both halves matter. Waiting only for stability accepts the screen the
        action was supposed to replace, because an old screen is perfectly
        stable. Waiting only for change accepts a half-rendered one.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        previous: str | None = None
        while time.monotonic() < deadline:
            try:
                current = await self.signature()
            except Exception:
                current = None
            if current is not None:
                if current == previous and (changed_from is None or current != changed_from):
                    return current
                previous = current
            await asyncio.sleep(POLL_INTERVAL_S)
        return previous or ""

    async def wait_for_ready(self, timeout_ms: int = 5_000) -> None:
        """Poll until the surface stops changing shape, after a navigation or sign-on."""
        deadline = time.monotonic() + timeout_ms / 1000
        previous: list[str] = []
        while time.monotonic() < deadline:
            try:
                current = await self.current_urls()
            except Exception:
                current = []
            if current and current == previous:
                return
            previous = current
            await asyncio.sleep(POLL_INTERVAL_S)

    @abstractmethod
    async def describe_target(
        self, handle: Handle, frame: list[str], description: str,
        *, content_varies: bool = False, avoid: tuple[str, ...] = (),
        anchors: tuple[str, ...] = (),
    ) -> ElementTarget:
        """Every verified way to find this element again, ranked best first.

        Set content_varies for an element whose text is the thing being read: a
        locator derived from that text would only ever find this one record.
        Pass the run's input values as `avoid`, so no locator or scope is built
        out of text that will be different on the next invocation. Pass the
        subset of those that uniquely identify a record as `anchors`: a
        container may be found by one, because the compiler turns it back into
        the parameter it came from and replay substitutes its own value.
        """

    def absolute(self, base_url: str, path: str) -> str:
        """Turn a recorded path into the address this surface understands.

        Joining a path onto a base is a web habit, and it was quietly built into
        the replay engine until a character screen had to run the same artifact.
        A surface whose locations are not URLs overrides this.
        """
        return urljoin(base_url, path)

    @abstractmethod
    async def current_urls(self) -> list[str]:
        """Every location the session currently holds open, however it got there."""

    @abstractmethod
    async def navigate(self, url: str) -> None: ...

    @abstractmethod
    async def fill(self, handle: Handle, value: str) -> None: ...

    @abstractmethod
    async def activate(self, handle: Handle) -> None: ...

    @abstractmethod
    async def select(self, handle: Handle, option: str) -> None: ...

    @abstractmethod
    async def press(self, key: str, handle: Handle | None = None) -> None: ...

    @abstractmethod
    async def read(self, handle: Handle) -> str: ...

    @abstractmethod
    async def options(self, handle: Handle) -> list[str]:
        """Every value this control offers, if it is a chooser. Empty if it is not."""

    # -- conditions --------------------------------------------------------

    @abstractmethod
    async def check(
        self, condition: Condition, params: dict[str, Any]
    ) -> tuple[bool, str]:
        """Evaluate a condition. Returns whether it holds and what was observed."""
