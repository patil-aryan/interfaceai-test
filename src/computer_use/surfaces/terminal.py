"""A character-grid surface, driven over a terminal's inbound/outbound protocol.

The web surface has roles, names and a document tree. This one has 24 lines of
80 characters and nothing else, which is the point: everything above
`Surface` works in terms of ElementTarget, Condition and the action verbs, so
the schema and the replay engine should not need to know which is underneath.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel

from computer_use.schema.capability import (
    ContainerWithTextScope,
    ElementTarget,
    GridLocator,
    LabelLocator,
    RegionScope,
    Scope,
    TextLocator,
)
from computer_use.schema.capability import (
    Locator as LocatorSpec,
)
from computer_use.surfaces.base import (
    Handle,
    Observation,
    Surface,
    SurfaceError,
    resolve_value,
)
from fake_core_teller.protocol import COLS, ROWS, SCREEN_START, encode

# A caption is separated from its value by spaces, dots, or both. Every green
# screen in this product writes "MEMBER ID . . ." and every operator reads
# straight past the leader to the value.
LEADER = " ."

# Columns are separated by at least this much whitespace. It is what tells
# "ELEANOR R VANCE", which contains single spaces, from the next column along.
COLUMN_GAP = "  "

# How long to wait for the application to paint a screen.
SCREEN_TIMEOUT_S = 5.0

# How many times to press the return key looking for a screen before accepting
# that it is not reachable from here.
MAX_RETURNS = 4

# The scheme a screen is addressed by, so an allowlist can constrain which
# screens automation may reach exactly as it constrains routes on the web.
SCHEME = "teller"
HOST = "meridian"


class ScreenRegion(BaseModel):
    """Where something sits on the grid. The only address a character screen has."""

    row: int
    column: int
    length: int


def screen_route(title: str) -> str:
    """The screen's name, in the shape an allowlist already knows how to judge."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return f"{SCHEME}://{HOST}/{slug or 'unknown'}"


class TerminalSurface(Surface):
    """Observes and acts on a 24x80 character screen."""

    kind = "terminal"

    def __init__(
        self, *, command: list[str] | None = None, slow_mo_ms: int = 0, show: bool = False
    ):
        # The interpreter already running, so the application starts under the
        # same environment as the automation driving it.
        self._command = command or [sys.executable, "-m", "fake_core_teller.app"]
        self._slow_mo_ms = slow_mo_ms
        # A browser can be watched by opening it. A subprocess on a pipe cannot,
        # so watching one means printing each screen as it is painted.
        self._show = show
        self._process: asyncio.subprocess.Process | None = None
        self._rows: list[str] = [" " * COLS for _ in range(ROWS)]
        # Typing is local until an attention key transmits it, which is what a
        # real terminal does and what makes `fill` cost no round trip.
        self._pending: dict[tuple[int, int], str] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._receive()

    async def stop(self) -> None:
        if self._process is None:
            return
        try:
            self._process.stdin.write(b"QUIT\n")
            await self._process.stdin.drain()
            await asyncio.wait_for(self._process.wait(), timeout=2)
        except Exception:
            self._process.kill()
        self._process = None

    # -- the wire ----------------------------------------------------------

    async def _receive(self) -> None:
        """Read exactly one painted screen into the buffer."""
        if self._process is None or self._process.stdout is None:
            raise SurfaceError("the terminal application is not running")
        stream = self._process.stdout

        async def read_screen() -> list[str]:
            while True:
                raw = await stream.readline()
                if not raw:
                    raise SurfaceError("the terminal application closed the connection")
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith(SCREEN_START):
                    rows = [line[len(SCREEN_START):]]
                    break
            while len(rows) < ROWS:
                more = await stream.readline()
                if not more:
                    break
                rows.append(more.decode("utf-8", "replace").rstrip("\n"))
            return rows

        rows = await asyncio.wait_for(read_screen(), timeout=SCREEN_TIMEOUT_S)
        self._rows = [row.ljust(COLS)[:COLS] for row in rows] + [
            " " * COLS for _ in range(ROWS - len(rows))
        ]
        if self._show:
            self._display()

    async def _transmit(self, key: str) -> None:
        """Send the attention key and everything typed since the last one."""
        if self._process is None or self._process.stdin is None:
            raise SurfaceError("the terminal application is not running")
        if self._slow_mo_ms:
            await asyncio.sleep(self._slow_mo_ms / 1000)
        line = encode(key, self._pending) + "\n"
        self._pending = {}
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()
        await self._receive()

    # -- perception --------------------------------------------------------

    @property
    def title(self) -> str:
        """The screen's name, painted in the banner row where every screen carries it."""
        return self._rows[0][30:58].strip() if self._rows else ""

    async def observe(self, *, screenshot_path: str | None = None) -> Observation:
        if screenshot_path:
            await self.screenshot(screenshot_path)
        return Observation(
            url=screen_route(self.title),
            title=self.title,
            frames={"": self._text()},
            screenshot_path=screenshot_path,
        )

    async def snapshot(self, frame: list[str]) -> str:
        return self._text()

    async def screenshot(self, path: str) -> str:
        """A character screen's screenshot is its characters, numbered so a reader can locate them."""
        numbered = "\n".join(f"{i:2d} |{row}|" for i, row in enumerate(self._rows))
        Path(path).with_suffix(".txt").write_text(numbered, encoding="utf-8")
        return str(Path(path).with_suffix(".txt"))

    async def current_urls(self) -> list[str]:
        return [screen_route(self.title)]

    def _text(self) -> str:
        return "\n".join(row.rstrip() for row in self._rows)

    def _display(self) -> None:
        """Print the screen, so a person can watch the run the way they watch a browser."""
        edge = "+" + "-" * COLS + "+"
        body = "\n".join(f"|{row}|" for row in self._rows)
        print(f"\n{edge}\n{body}\n{edge}", file=sys.stderr, flush=True)

    # -- targeting ---------------------------------------------------------

    async def _resolve_scope(
        self, scope: Scope | None, frame: list[str], params: dict[str, Any]
    ) -> Handle | None:
        if scope is None:
            return None
        if isinstance(scope, RegionScope):
            raise SurfaceError("a character screen has no named regions; scope by row text")

        assert isinstance(scope, ContainerWithTextScope)
        text = resolve_value(scope.text, params)
        matches = [i for i, row in enumerate(self._rows) if text in row]
        if not matches:
            raise SurfaceError(f"no row containing {text!r}")
        # The innermost match on a grid is simply the first row carrying the
        # text: rows do not nest, so there is no ancestor to mistake for it.
        return ScreenRegion(row=matches[0], column=0, length=COLS)

    async def _locate(
        self, strategy: LocatorSpec, scope: Handle | None, frame: list[str], nth: int
    ) -> tuple[Handle | None, int]:
        rows = range(ROWS) if scope is None else [scope.row]
        kind = strategy.strategy

        if kind == "grid":
            row = scope.row if scope is not None else strategy.row
            length = strategy.length or self._run_length(row, strategy.column)
            return ScreenRegion(row=row, column=strategy.column, length=length), 1

        if kind == "label":
            found = [self._after_caption(r, strategy.text) for r in rows]
        elif kind == "text":
            found = [self._exactly(r, strategy.text) for r in rows]
        else:
            raise SurfaceError(
                f"{kind!r} addresses markup; a character screen offers grid, label and text"
            )

        hits = [region for region in found if region is not None]
        if not hits:
            return None, 0
        return (hits[nth] if nth < len(hits) else None), len(hits)

    def _after_caption(self, row: int, caption: str) -> ScreenRegion | None:
        """The value a caption points at, read the way an operator reads it."""
        line = self._rows[row]
        at = line.find(caption)
        if at < 0:
            return None
        cursor = at + len(caption)
        while cursor < COLS and line[cursor] in LEADER:
            cursor += 1
        if cursor >= COLS:
            return None
        end = line.find(COLUMN_GAP, cursor)
        end = COLS if end < 0 else end
        return ScreenRegion(row=row, column=cursor, length=end - cursor)

    def _exactly(self, row: int, text: str) -> ScreenRegion | None:
        at = self._rows[row].find(text)
        return None if at < 0 else ScreenRegion(row=row, column=at, length=len(text))

    def _run_length(self, row: int, column: int) -> int:
        """How far a value at this column runs before the next column starts."""
        line = self._rows[row]
        end = line.find(COLUMN_GAP, column)
        return (COLS if end < 0 else end) - column

    # -- action ------------------------------------------------------------

    def absolute(self, base_url: str, path: str) -> str:
        """A screen name is already absolute. There is no relative address on a grid."""
        return screen_route(path.strip("/"))

    async def navigate(self, url: str) -> None:
        """Go to a named screen. A terminal has a way back, not an address bar.

        There is no jumping straight to a screen, so this walks back the way an
        operator would, and says so plainly if the screen is not reachable that
        way rather than pretending it arrived.
        """
        wanted = urlparse(url).path.strip("/").split("/")[-1]
        for _ in range(MAX_RETURNS):
            if not wanted or screen_route(self.title).endswith(f"/{wanted}"):
                return
            await self._transmit("PF3")
        raise SurfaceError(
            f"cannot reach screen {wanted!r} from {self.title!r} by returning"
        )

    async def fill(self, handle: Handle, value: str) -> None:
        self._pending[(handle.row, handle.column)] = value
        # Paint it locally, so a checkpoint can read back what was typed before
        # anything has been transmitted, exactly as it would on the glass.
        line = self._rows[handle.row]
        shown = value.ljust(handle.length)[: handle.length]
        self._rows[handle.row] = line[: handle.column] + shown + line[handle.column + handle.length :]

    async def select(self, handle: Handle, option: str) -> None:
        """A character screen has no dropdown; choosing is typing the code."""
        await self.fill(handle, option)

    async def activate(self, handle: Handle) -> None:
        await self._transmit("ENTER")

    async def press(self, key: str, handle: Handle | None = None) -> None:
        await self._transmit(key.upper())

    async def read(self, handle: Handle) -> str:
        row = self._rows[handle.row]
        return row[handle.column : handle.column + handle.length].strip().strip("_").strip()

    async def options(self, handle: Handle) -> list[str]:
        return []

    # -- conditions --------------------------------------------------------

    async def check(self, condition: Any, params: dict[str, Any]) -> tuple[bool, str]:
        kind = condition.kind
        body = self._text()

        if kind in ("text_present", "text_absent"):
            present = condition.text in body
            held = present if kind == "text_present" else not present
            return held, f"text {condition.text!r} is {'present' if present else 'absent'}"

        if kind in ("element_present", "element_absent"):
            try:
                await self.resolve(condition.target, params, 500)
            except Exception:
                return kind == "element_absent", f"{condition.target.description!r} is not on screen"
            return kind == "element_present", f"{condition.target.description!r} is on screen"

        if kind == "value_equals":
            expected = resolve_value(condition.expected, params)
            try:
                handle, _ = await self.resolve(condition.target, params)
            except Exception as exc:
                return False, str(exc)
            actual = await self.read(handle)
            return actual == expected, f"value is {actual!r}, expected {expected!r}"

        if kind == "url_matches":
            here = screen_route(self.title)
            return bool(re.search(condition.pattern, here)), f"screen is {here!r}"

        raise SurfaceError(f"condition {kind!r} is not supported on a character screen")

    # -- recording ---------------------------------------------------------

    async def describe_target(
        self, handle: Handle, frame: list[str], description: str,
        *, content_varies: bool = False, avoid: tuple[str, ...] = (),
        anchors: tuple[str, ...] = (),
    ) -> ElementTarget:
        """Harvest the ways to find this region again, caption first and position last."""
        strategies: list[LocatorSpec] = []
        caption = self._caption_left_of(handle)
        if caption and not any(v and v in caption for v in avoid + anchors):
            strategies.append(LabelLocator(text=caption))

        shown = await self.read(handle)
        if shown and not content_varies and not any(v and v in shown for v in avoid + anchors):
            strategies.append(TextLocator(text=shown))

        # Always last, and always present: on a grid, position is the address
        # that cannot fail to exist, which is exactly why it is the weakest.
        strategies.append(GridLocator(
            row=handle.row, column=handle.column, length=handle.length
        ))

        scope = None
        anchor = next((a for a in anchors if a and a in self._rows[handle.row]), None)
        if anchor is not None and not content_varies:
            from computer_use.schema.capability import LiteralValue
            scope = ContainerWithTextScope(container_role="row", text=LiteralValue(value=anchor))

        return ElementTarget(
            description=description, frame=frame, scope=scope,
            strategies=strategies, recorded_strategy=strategies[0].strategy, nth=0,
        )

    def _caption_left_of(self, handle: Handle) -> str | None:
        """The caption this value sits beside, which is how an operator names it."""
        left = self._rows[handle.row][: handle.column].rstrip(LEADER)
        if not left.strip():
            return None
        return left.strip().split(COLUMN_GAP)[-1].strip()
