"""A browser surface, driven through Playwright."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Frame, Locator, Page, async_playwright

from computer_use.schema.capability import (
    Condition,
    ContainerWithTextScope,
    Locator as LocatorSpec,
    RegionScope,
    Scope,
)
from computer_use.surfaces.base import (
    Handle,
    Observation,
    Surface,
    SurfaceError,
    resolve_value,
)


@dataclass
class Point:
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
        """Walk a frame path. An empty path means the main document."""
        frame = self.page.main_frame
        for name in path:
            children = [c for c in frame.child_frames if c.name == name]
            if not children:
                available = [c.name for c in frame.child_frames]
                raise SurfaceError(f"frame {name!r} not found; available: {available}")
            frame = children[0]
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

    # -- actions -----------------------------------------------------------

    async def navigate(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")

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
            body = await self._frame_text(condition.frame)
            present = condition.text in body
            held = present if kind == "text_present" else not present
            seen = "present" if present else "absent"
            return held, f"text {condition.text!r} is {seen}"

        if kind == "element_present":
            try:
                _, resolution = await self.resolve(condition.target, params)
            except Exception as exc:
                return False, str(exc)
            return True, f"resolved via {resolution.strategy_used}"

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
