"""Meridian Teller 8.4 - the same credit union core, on a 24x80 character grid.

No markup, no accessibility tree, no roles. A screen is 24 lines of 80
characters and the only address an element has is where it sits. This exists to
prove the artifact schema and the replay engine are not web-shaped.
"""

from __future__ import annotations

import sys

from fake_core_banking_app.data import MEMBERS, OPERATORS
from fake_core_teller.protocol import COLS, ROWS, SCREEN_START, decode

BANNER = "MERIDIAN TELLER 8.4"
INSTITUTION = "PINECREST CU"

# Where each input field sits, per screen. The terminal is told nothing about
# these; it finds them the same way an operator does, by reading the captions.
SIGN_ON_USER = (5, 20)
SIGN_ON_PASSWORD = (7, 20)
MENU_OPTION = (8, 20)
INQUIRY_MEMBER = (4, 20)


class Screen:
    """A 24x80 character buffer that text is painted into."""

    def __init__(self, title: str):
        self._rows = [" " * COLS for _ in range(ROWS)]
        self.put(0, 0, BANNER)
        self.put(0, 30, title)
        self.put(0, COLS - len(INSTITUTION) - 1, INSTITUTION)
        self.put(1, 0, "-" * COLS)

    def put(self, row: int, col: int, text: str) -> None:
        if not 0 <= row < ROWS:
            return
        line = self._rows[row]
        self._rows[row] = (line[:col] + text + line[col + len(text):])[:COLS]

    def field(self, where: tuple[int, int], width: int, value: str = "") -> None:
        """Paint an input field. Underscores are what makes one visible on a green screen."""
        row, col = where
        self.put(row, col, (value + "_" * width)[:width])

    def render(self) -> str:
        return SCREEN_START + "\n".join(line.rstrip().ljust(COLS) for line in self._rows) + "\n"


def sign_on(message: str = "") -> Screen:
    s = Screen("SIGN ON")
    s.put(3, 24, "* * *   S I G N   O N   * * *")
    s.put(SIGN_ON_USER[0], 6, "USER ID  . . . .")
    s.field(SIGN_ON_USER, 10)
    s.put(SIGN_ON_PASSWORD[0], 6, "PASSWORD . . . .")
    s.field(SIGN_ON_PASSWORD, 10)
    s.put(22, 0, "PF3=EXIT    ENTER=SIGN ON")
    s.put(23, 0, message)
    return s


def main_menu(operator: str, message: str = "") -> Screen:
    s = Screen("MAIN MENU")
    s.put(2, 6, f"OPERATOR . . . . {operator}")
    s.put(4, 6, "1. MEMBER INQUIRY")
    s.put(5, 6, "2. ACCOUNT SERVICING")
    s.put(6, 6, "3. TELLER OPERATIONS")
    s.put(MENU_OPTION[0], 6, "OPTION . . . . .")
    s.field(MENU_OPTION, 2)
    s.put(22, 0, "PF3=SIGN OFF    ENTER=SELECT")
    s.put(23, 0, message)
    return s


def member_inquiry(message: str = "") -> Screen:
    s = Screen("MEMBER INQUIRY")
    s.put(2, 6, "ENTER A SIX DIGIT MEMBER NUMBER.")
    s.put(INQUIRY_MEMBER[0], 6, "MEMBER ID . . .")
    s.field(INQUIRY_MEMBER, 6)
    s.put(22, 0, "PF3=RETURN    ENTER=SEARCH")
    s.put(23, 0, message)
    return s


def member_profile(member) -> Screen:
    s = Screen("MEMBER PROFILE")
    s.put(3, 6, "MEMBER ID . .")
    s.put(3, 22, member.member_id)
    s.put(3, 42, "STATUS . .")
    s.put(3, 56, member.status)
    s.put(4, 6, "NAME . . . . .")
    s.put(4, 22, member.name)
    s.put(5, 6, "BRANCH . . . .")
    s.put(5, 22, member.branch)
    s.put(5, 42, "JOINED . .")
    s.put(5, 56, member.joined)
    s.put(7, 6, "TYPE")
    s.put(7, 22, "ACCOUNT NO")
    s.put(7, 42, "BALANCE")
    s.put(7, 56, "STATUS")
    s.put(8, 6, "-" * 66)
    for offset, account in enumerate(member.accounts[:10]):
        row = 9 + offset
        s.put(row, 6, account.kind)
        s.put(row, 22, account.number)
        s.put(row, 42, f"{account.balance:>10.2f}")
        s.put(row, 56, account.status)
    s.put(22, 0, "PF3=RETURN")
    return s


class Session:
    """Which screen the operator is on, and who they are."""

    def __init__(self) -> None:
        self.operator: str | None = None
        self.screen = sign_on()

    def transmit(self, key: str, fields: dict[tuple[int, int], str]) -> None:
        """Apply one inbound transmission and paint whatever comes next."""
        if self.operator is None:
            self.screen = self._sign_on(key, fields)
            return
        if key == "PF3":
            self.screen = main_menu(OPERATORS[self.operator]["name"])
            return
        title = self.screen._rows[0][30:50].strip()
        if title == "MAIN MENU":
            self.screen = self._menu(fields)
        elif title == "MEMBER INQUIRY":
            self.screen = self._inquiry(fields)
        else:
            self.screen = main_menu(OPERATORS[self.operator]["name"])

    def _sign_on(self, key: str, fields: dict[tuple[int, int], str]) -> Screen:
        user = fields.get(SIGN_ON_USER, "").strip()
        password = fields.get(SIGN_ON_PASSWORD, "")
        if not user and not password:
            return sign_on()
        record = OPERATORS.get(user)
        if record and record["password"] == password:
            self.operator = user
            return main_menu(record["name"])
        return sign_on("SIGN ON FAILED - USER ID OR PASSWORD NOT RECOGNISED")

    def _menu(self, fields: dict[tuple[int, int], str]) -> Screen:
        option = fields.get(MENU_OPTION, "").strip()
        if option == "1":
            return member_inquiry()
        if option in ("2", "3"):
            return main_menu(OPERATORS[self.operator]["name"], "OPTION NOT AVAILABLE")
        return main_menu(OPERATORS[self.operator]["name"], "INVALID OPTION - SELECT 1, 2 OR 3")

    def _inquiry(self, fields: dict[tuple[int, int], str]) -> Screen:
        entered = fields.get(INQUIRY_MEMBER, "").strip()
        if not entered.isdigit() or len(entered) != 6:
            return member_inquiry("INVALID ENTRY - MEMBER NUMBER MUST BE 6 DIGITS")
        member = MEMBERS.get(entered)
        if member is None:
            return member_inquiry(f"NO MEMBER MATCHING {entered}")
        return member_profile(member)


def main() -> None:
    session = Session()
    out = sys.stdout
    out.write(session.screen.render())
    out.flush()
    for line in sys.stdin:
        if line.strip() in ("QUIT", "\x04"):
            break
        key, fields = decode(line)
        session.transmit(key, fields)
        out.write(session.screen.render())
        out.flush()


if __name__ == "__main__":
    main()
