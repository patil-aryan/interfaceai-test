"""A browser surface, driven through Playwright."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from playwright.async_api import Frame, Locator, Page, async_playwright
from pydantic import BaseModel

from computer_use.schema.capability import (
    SEMANTIC_STRATEGIES,
    Condition,
    ContainerWithTextScope,
    CoordinateLocator,
    ElementTarget,
    LabelLocator,
    LiteralValue,
    PlaceholderLocator,
    RegionScope,
    RoleLocator,
    RoleNameLocator,
    Scope,
    StructuralLocator,
    TextLocator,
)
from computer_use.schema.capability import (
    Locator as LocatorSpec,
)
from computer_use.surfaces.base import (
    POLL_INTERVAL_S,
    Handle,
    Observation,
    Surface,
    SurfaceError,
    resolve_value,
)

# Everything the accessibility tree does not expose: the attributes and the
# ancestry that the weaker fallback strategies are built from.
JS_FACTS = """
el => {
  const path = [];
  let n = el;
  while (n && n.nodeType === 1 && path.length < 6) {
    let seg = n.tagName.toLowerCase();
    if (n.id) { seg += '#' + n.id; path.unshift(seg); break; }
    const sibs = n.parentNode ? [...n.parentNode.children].filter(c => c.tagName === n.tagName) : [];
    if (sibs.length > 1) seg += ':nth-of-type(' + (sibs.indexOf(n) + 1) + ')';
    path.unshift(seg);
    n = n.parentElement;
  }
  let label = null;
  if (el.id) {
    const l = el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (l) label = l.innerText.trim();
  }
  if (!label) { const l = el.closest('label'); if (l) label = l.innerText.trim(); }
  const row = el.closest('tr');
  let rowText = null, labelCell = null, rowAll = null;
  if (row) {
    rowAll = (row.innerText || '').trim();
    const first = row.querySelector('td, th');
    if (first && !first.contains(el)) rowText = (first.innerText || '').trim();
    // The caption sitting immediately before this value, which is what a human
    // would name when pointing at it.
    const own = el.closest('td, th');
    let prev = own ? own.previousElementSibling : null;
    while (prev) {
      const txt = (prev.innerText || '').trim();
      if (txt) { labelCell = txt; break; }
      prev = prev.previousElementSibling;
    }
  }
  return {
    css: path.join(' > '),
    placeholder: el.getAttribute('placeholder'),
    label: label,
    text: (el.innerText || '').trim(),
    rowText: rowText,
    rowAll: rowAll,
    labelCell: labelCell,
  };
}
"""

# Best first. A target is judged by how good its strongest surviving strategy is.
STRATEGY_RANK = ("role_name", "label", "placeholder", "text", "role", "structural")

# How far down a strategy's match list to look for the element. A legacy screen
# has hundreds of cells, so an unscoped positional match must stay reachable.
MAX_MATCHES_SEARCHED = 40

# How long to look for something before accepting that it is not there. Absence
# is the one condition whose cost is paid in full every time it holds.
ABSENCE_TIMEOUT_MS = 750

# The shortest cell text that can serve as a scope. A one or two character cell
# is a row number, not a caption, and matches most of the rows on the screen.
MIN_SCOPE_CHARS = 3

# How long to keep trying to resolve a candidate scope before accepting that it
# genuinely does not apply to this element.
SCOPE_SETTLE_TIMEOUT_MS = 1_500


class Point(BaseModel):
    """A coordinate handle, produced only by the last-resort strategy."""

    x: int
    y: int


class WebSurface(Surface):
    """Observes and acts on a web page, including iframe-partitioned layouts."""

    kind = "web"

    def __init__(
        self,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = (1200, 820),
        slow_mo_ms: int = 0,
    ):
        self._headless = headless
        self._viewport = viewport
        self._slow_mo_ms = slow_mo_ms
        self._pw = None
        self._browser = None
        self._page: Page | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless, slow_mo=self._slow_mo_ms
        )
        w, h = self._viewport
        self._page = await self._browser.new_page(viewport={"width": w, "height": h})

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._pw = self._browser = self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise SurfaceError("surface has not been started")
        return self._page

    # -- frames ------------------------------------------------------------

    def _frame(self, path: list[str]) -> Frame:
        """Walk a frame path. An empty path means the main document.

        Frame identity is not stable across navigation: a frameset's children
        exist unnamed and pointing at about:blank before they take their name,
        and an old frame lingers briefly after being replaced. Detached frames
        are skipped, and the newest match wins.
        """
        frame = self.page.main_frame
        for name in path:
            children = [
                c for c in frame.child_frames if c.name == name and not c.is_detached()
            ]
            if not children:
                available = [c.name for c in frame.child_frames]
                raise SurfaceError(f"frame {name!r} not attached; available: {available}")
            frame = children[-1]
        return frame

    def _frame_paths(self) -> list[list[str]]:
        paths: list[list[str]] = [[]]

        def walk(frame: Frame, prefix: list[str]) -> None:
            for child in frame.child_frames:
                path = prefix + [child.name or child.url]
                paths.append(path)
                walk(child, path)

        walk(self.page.main_frame, [])
        return paths

    # -- perception --------------------------------------------------------

    async def observe(self, *, screenshot_path: str | None = None) -> Observation:
        frames: dict[str, str] = {}
        for path in self._frame_paths():
            try:
                frames["/".join(path)] = await self.snapshot(path)
            except Exception as exc:
                frames["/".join(path)] = f"<unavailable: {exc}>"
        shot = await self.screenshot(screenshot_path) if screenshot_path else None
        return Observation(
            url=self.page.url, title=await self.page.title(), frames=frames,
            screenshot_path=shot,
        )

    async def snapshot(self, frame: list[str]) -> str:
        return await self._frame(frame).locator("body").aria_snapshot()

    async def screenshot(self, path: str) -> str:
        await self.page.screenshot(path=path, full_page=False)
        return path

    # -- targeting ---------------------------------------------------------

    async def _resolve_scope(
        self, scope: Scope | None, frame: list[str], params: dict[str, Any]
    ) -> Handle | None:
        if scope is None:
            return None
        root = self._frame(frame)

        if isinstance(scope, RegionScope):
            found = root.get_by_role(scope.role, name=scope.name)
            if await found.count() == 0:
                raise SurfaceError(f"no {scope.role} named {scope.name!r}")
            return found.first

        assert isinstance(scope, ContainerWithTextScope)
        text = resolve_value(scope.text, params)
        candidates = root.get_by_role(scope.container_role).filter(has_text=text)
        total = await candidates.count()
        if total == 0:
            raise SurfaceError(f"no {scope.container_role} containing {text!r}")

        # Nested layouts make ancestors match too, because their text includes
        # their children's. Take the innermost: the match that contains no other
        # match.
        for i in range(total):
            candidate = candidates.nth(i)
            nested = candidate.get_by_role(scope.container_role).filter(has_text=text)
            if await nested.count() == 0:
                return candidate
        return candidates.last

    async def _locate(
        self, strategy: LocatorSpec, scope: Handle | None, frame: list[str], nth: int
    ) -> tuple[Handle | None, int]:
        root: Locator | Frame = scope if scope is not None else self._frame(frame)
        kind = strategy.strategy

        if kind == "role_name":
            found = root.get_by_role(strategy.role, name=strategy.name, exact=strategy.exact)
        elif kind == "role":
            found = root.get_by_role(strategy.role)
        elif kind == "label":
            found = root.get_by_label(strategy.text)
        elif kind == "placeholder":
            found = root.get_by_placeholder(strategy.text)
        elif kind == "text":
            found = root.get_by_text(strategy.text)
        elif kind == "structural":
            found = root.locator(strategy.css)
        elif kind == "coordinates":
            size = self.page.viewport_size or {"width": strategy.viewport_width,
                                               "height": strategy.viewport_height}
            x = round(strategy.x * size["width"] / strategy.viewport_width)
            y = round(strategy.y * size["height"] / strategy.viewport_height)
            return Point(x, y), 1
        elif kind == "grid":
            raise SurfaceError("grid addressing is a terminal strategy, not a web one")
        else:
            raise SurfaceError(f"unknown strategy {kind!r}")

        count = await found.count()
        return (found.nth(nth) if count > nth else None), count

    async def options(self, handle: Handle) -> list[str]:
        """The values a <select> offers. A blank placeholder is not one of them."""
        try:
            values = await handle.evaluate(
                "el => el.tagName === 'SELECT' "
                "? [...el.options].map(o => o.value).filter(v => v !== '') : []"
            )
        except Exception:
            return []
        return [str(v) for v in values]

    # -- describing an element for later --------------------------------

    async def describe_target(
        self, handle: Handle, frame: list[str], description: str,
        *, content_varies: bool = False, avoid: tuple[str, ...] = (),
        anchors: tuple[str, ...] = (),
    ) -> ElementTarget:
        """Harvest every way to address this element, keep only those that find it."""
        await self.settle()
        element = await handle.element_handle()
        if element is None:
            raise SurfaceError(f"cannot describe {description!r}: it is not attached")

        facts = await handle.evaluate(JS_FACTS)
        role, name = _parse_aria_header(await handle.aria_snapshot())
        # A container may be named by a supplied value; the control inside it
        # never may. Anchors are therefore still barred from every strategy.
        candidates = [
            s for s in _candidate_strategies(role, name, facts, content_varies)
            if not _carries(s, avoid + anchors)
        ]

        # Ordered by preference, so that a tie between equally good strategies
        # is settled in favour of the simplest and most readable description.
        scopes: list[Scope | None] = [None]
        for text in (facts.get("labelCell"), facts.get("rowText")):
            if not text or len(text.strip()) < MIN_SCOPE_CHARS:
                continue
            if any(v and v in text for v in avoid):
                continue
            candidate = ContainerWithTextScope(
                container_role="row", text=LiteralValue(value=_shorten(text))
            )
            if candidate not in scopes:
                scopes.append(candidate)

        # A row picked out by an identifier the caller supplied, e.g. "the row
        # for the member number I was given". Recorded as the literal we can
        # verify against the screen in front of us; the compiler promotes it to
        # the parameter it came from, so replay looks for its own value.
        #
        # Never while reading. A read target's value is the thing a later
        # assertion compares, and addressing it by that same value would make
        # the assertion prove itself.
        if not content_varies:
            row_all = facts.get("rowAll") or ""
            for anchor in anchors:
                if not anchor or anchor not in row_all:
                    continue
                candidate = ContainerWithTextScope(
                    container_role="row", text=LiteralValue(value=anchor)
                )
                if candidate not in scopes:
                    scopes.append(candidate)

        # Judge each scope by its strongest surviving strategy, then by how far
        # down the match list it has to count, then prefer no scope at all.
        # A scope earns its complexity only by improving one of the first two.
        best = None
        best_score = None
        for preference, scope in enumerate(scopes):
            verified, nth = await self._verify_strategies(candidates, scope, frame, element)
            if not verified:
                continue
            # Compared in this order: how good the strongest surviving strategy
            # is, then how far down the match list it must count, then whether a
            # scope was needed at all. Earlier keys dominate later ones.
            score = (STRATEGY_RANK.index(verified[0].strategy), nth, preference)
            if best_score is None or score < best_score:
                best_score = score
                best = (scope, verified, nth)

        if best is None:
            raise SurfaceError(
                f"no strategy could find {description!r} again; "
                f"role={role!r} name={name!r} facts={facts}"
            )

        scope, verified, nth = best
        # A coordinate is where this element happened to be. For a value that
        # changes every invocation, that is where *this* value rendered, so the
        # rung could never fire correctly and would make the artifact
        # irreproducible between recordings.
        if not content_varies and not any(
            s.strategy in SEMANTIC_STRATEGIES for s in verified
        ):
            box = await handle.bounding_box()
            size = self.page.viewport_size or {"width": 1200, "height": 820}
            if box:
                verified.append(CoordinateLocator(
                    x=round(box["x"] + box["width"] / 2),
                    y=round(box["y"] + box["height"] / 2),
                    viewport_width=size["width"], viewport_height=size["height"],
                ))

        return ElementTarget(
            description=description, frame=frame, scope=scope,
            strategies=verified, recorded_strategy=verified[0].strategy, nth=nth,
        )

    async def _verify_strategies(
        self, candidates: list[LocatorSpec], scope: Scope | None,
        frame: list[str], element: Any,
    ) -> tuple[list[LocatorSpec], int]:
        """Keep the candidates that really find this element, at one shared index."""
        # A scope that cannot be resolved *yet* is not a scope that does not
        # exist. Giving up instantly here silently demotes a scoped target to an
        # unscoped one whenever the frame is still settling, which made the same
        # element harvest differently between runs.
        deadline = time.monotonic() + SCOPE_SETTLE_TIMEOUT_MS / 1000
        while True:
            try:
                root = await self._resolve_scope(scope, frame, {})
                break
            except Exception:
                if time.monotonic() >= deadline:
                    return [], 0
                await asyncio.sleep(POLL_INTERVAL_S)

        verified: list[LocatorSpec] = []
        agreed: int | None = None
        for spec in candidates:
            index = await self._match_index(spec, root, frame, element)
            if index is None:
                continue
            if agreed is None:
                agreed = index          # the best strategy sets the index
            if index == agreed:         # weaker ones are kept only if they agree
                verified.append(spec)
        if agreed is None:
            return [], 0
        return verified, agreed

    async def _match_index(
        self, spec: LocatorSpec, scope: Handle | None, frame: list[str], element: Any
    ) -> int | None:
        """Which position this strategy has to use to land on the element, if any."""
        root: Locator | Frame = scope if scope is not None else self._frame(frame)
        try:
            found = _apply(root, spec)
            count = await found.count()
        except Exception:
            return None
        for i in range(min(count, MAX_MATCHES_SEARCHED)):
            try:
                if await found.nth(i).evaluate("(el, other) => el === other", element):
                    return i
            except Exception:
                continue
        return None

    # -- actions -----------------------------------------------------------

    async def current_urls(self) -> list[str]:
        """The page plus every live frame. In a frameset the top-level URL never moves."""
        urls = [self.page.url]

        def walk(frame) -> None:
            for child in frame.child_frames:
                if not child.is_detached():
                    urls.append(child.url)
                    walk(child)

        walk(self.page.main_frame)
        return [u for u in urls if u and u != "about:blank"]

    async def navigate(self, url: str) -> None:
        # "load" rather than "domcontentloaded" so a frameset's children have
        # been fetched and named before anything tries to address them.
        await self.page.goto(url, wait_until="load")

    async def fill(self, handle: Handle, value: str) -> None:
        if isinstance(handle, Point):
            await self.page.mouse.click(handle.x, handle.y)
            await self.page.keyboard.type(value)
            return
        await handle.fill(value)

    async def activate(self, handle: Handle) -> None:
        if isinstance(handle, Point):
            await self.page.mouse.click(handle.x, handle.y)
            return
        await handle.click()

    async def select(self, handle: Handle, option: str) -> None:
        try:
            await handle.select_option(option)
        except Exception:
            await handle.select_option(label=option)

    async def press(self, key: str, handle: Handle | None = None) -> None:
        if handle is not None and not isinstance(handle, Point):
            await handle.press(key)
        else:
            await self.page.keyboard.press(key)

    async def read(self, handle: Handle) -> str:
        if isinstance(handle, Point):
            raise SurfaceError("cannot read text from a coordinate handle")
        try:
            return (await handle.input_value()).strip()
        except Exception:
            return (await handle.inner_text()).strip()

    # -- conditions --------------------------------------------------------

    async def _frame_text(self, frame: list[str]) -> str:
        return await self._frame(frame).locator("body").inner_text()

    async def check(self, condition: Condition, params: dict[str, Any]) -> tuple[bool, str]:
        kind = condition.kind

        if kind in ("text_present", "text_absent"):
            # Compared with runs of whitespace collapsed. A condition may have
            # been written from the accessibility tree, which joins cells with
            # single spaces, while the DOM separates them with tabs and
            # newlines. In table-based markup that whitespace carries no
            # meaning, and matching on it exactly is matching on an accident.
            body = _flatten(await self._frame_text(condition.frame))
            present = _flatten(condition.text) in body
            held = present if kind == "text_present" else not present
            seen = "present" if present else "absent"
            return held, f"text {condition.text!r} is {seen}"

        if kind == "element_present":
            try:
                _, resolution = await self.resolve(condition.target, params)
            except Exception as exc:
                return False, str(exc)
            return True, f"resolved via {resolution.strategy_used}"

        if kind == "element_absent":
            # Proving something is not there means waiting for it, and the whole
            # resolve budget would be spent on every check. The screen has
            # already been settled by the time a condition is evaluated, so a
            # short look is enough to tell absent from still-arriving.
            try:
                await self.resolve(condition.target, params, ABSENCE_TIMEOUT_MS)
            except Exception:
                return True, f"{condition.target.description!r} is not on screen"
            return False, f"{condition.target.description!r} is on screen"

        if kind == "value_equals":
            expected = resolve_value(condition.expected, params)
            try:
                handle, _ = await self.resolve(condition.target, params)
            except Exception as exc:
                return False, str(exc)
            actual = await self.read(handle)
            return actual == expected, f"value is {actual!r}, expected {expected!r}"

        if kind == "url_matches":
            urls = [self.page.url] + [f.url for f in self.page.frames]
            matched = [u for u in urls if re.search(condition.pattern, u)]
            return bool(matched), f"urls {urls}"

        raise SurfaceError(f"unknown condition {kind!r}")


# --------------------------------------------------------------------------
# Building strategies from what the page reveals
# --------------------------------------------------------------------------


def _apply(root: Locator | Frame, spec: LocatorSpec) -> Locator:
    """Turn one strategy into a Playwright locator. Mirrors the branches in _locate."""
    kind = spec.strategy
    if kind == "role_name":
        return root.get_by_role(spec.role, name=spec.name, exact=spec.exact)
    if kind == "role":
        return root.get_by_role(spec.role)
    if kind == "label":
        return root.get_by_label(spec.text)
    if kind == "placeholder":
        return root.get_by_placeholder(spec.text)
    if kind == "text":
        return root.get_by_text(spec.text)
    if kind == "structural":
        return root.locator(spec.css)
    raise SurfaceError(f"{kind!r} cannot be verified on a web surface")


def _parse_aria_header(snapshot: str) -> tuple[str | None, str | None]:
    """Read the role and accessible name off the first line of an aria snapshot.

    A single element snapshots as `- textbox "MEMBER ID"` or `- link "1. HOME":`.
    """
    first = snapshot.strip().splitlines()[0] if snapshot.strip() else ""
    match = re.match(r'^-\s*([a-z]+)(?:\s+"(.*?)")?', first)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def _candidate_strategies(
    role: str | None, name: str | None, facts: dict[str, Any], content_varies: bool
) -> list[LocatorSpec]:
    """Every strategy worth trying for this element, ordered best first.

    When content_varies, any strategy derived from the element's own text is
    dropped. A table cell's accessible name *is* its content, so recording
    `cell named "6401.88"` would produce a capability that can only ever find
    the one member it was recorded against.
    """
    own_text = _shorten(facts.get("text") or "")
    out: list[LocatorSpec] = []

    if role and name and not (content_varies and _shorten(name) == own_text):
        out.append(RoleNameLocator(role=role, name=name, exact=False))
    if facts.get("label"):
        out.append(LabelLocator(text=facts["label"]))
    if facts.get("placeholder"):
        out.append(PlaceholderLocator(text=facts["placeholder"]))
    if own_text and not content_varies:
        out.append(TextLocator(text=own_text))
    if role:
        out.append(RoleLocator(role=role))
    if facts.get("css"):
        out.append(StructuralLocator(css=facts["css"]))
    return out


def _shorten(text: str, limit: int = 60) -> str:
    """Collapse whitespace and trim, so a locator carries a line rather than a screen."""
    collapsed = " ".join(text.split())
    return collapsed[:limit].strip()


def _carries(spec: LocatorSpec, avoid: tuple[str, ...]) -> bool:
    """Whether a strategy is built out of text that changes between invocations."""
    if spec.strategy == "role_name":
        text = spec.name
    elif spec.strategy in ("label", "placeholder", "text"):
        text = spec.text
    elif spec.strategy == "structural":
        text = spec.css
    else:
        return False
    return any(value and value in text for value in avoid)


def _flatten(text: str) -> str:
    """Collapse every run of whitespace to one space, for comparing screen text."""
    return " ".join(text.split())
