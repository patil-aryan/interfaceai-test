"""Walk a demo one beat at a time, so nothing long has to be typed in front of people.

    python scripts/demo.py            list the beats
    python scripts/demo.py 4          run beat 4
    python scripts/demo.py 4 --watch  run it with the browser open and slowed down

Each beat prints what it is showing, the command it is about to run, and then
runs it. Faults are set and cleared for you.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
WEB = "http://127.0.0.1:8081"
BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"

TERMINAL = ["--allowlist", "allowlist.terminal.json", "--base-url", "teller://meridian"]

THE_QUESTION = (
    "there are two members called THEODORE J VANCE. "
    "What is the savings balance for the one whose member number is 100253?"
)

# The part of an artifact worth reading out loud: its contract, not its steps.
SHOW_CONTRACT = (
    "import json;"
    "a=json.load(open('artifacts/capabilities/"
    "member.lookup_profile_and_savings_balance@1.0.0.json'));"
    "print(json.dumps({k:a[k] for k in "
    "('id','version','status','risk_tier','inputs','outputs','outcomes')}, indent=2))"
)


class Beat:
    """One thing to show, what it proves, and how to run it."""

    def __init__(self, title: str, say: str, argv: list[str] | None = None, *,
                 fault: dict | None = None, watchable: bool = False,
                 manual: list[str] | None = None, costs_money: bool = False):
        self.title = title
        self.say = say
        self.argv = argv or []
        self.fault = fault
        self.watchable = watchable
        self.manual = manual
        self.costs_money = costs_money


def replay(*args: str) -> list[str]:
    return [PY, "-m", "computer_use.replay", *args]


BEATS = [
    Beat("The application we are automating",
         "A 1990s bank console. Framesets, table layouts, no test IDs, and form field\n"
         "names that change on every restart. There is no API. This is the whole problem.",
         manual=[f"open {WEB}/login     sign in as operator1 / changeme"]),

    Beat("A model works out how to do it, once",
         "It sees the screen, picks one action, and is corrected when it points at the\n"
         "wrong thing. Watch it fumble and recover. This is the only part that uses a model\n"
         "to drive the UI, and it happens once per capability.",
         [PY, "-m", "computer_use.discovery",
          "artifacts/goals/member.lookup_profile_and_savings_balance.json",
          "--headed", "--slow", "400"],
         costs_money=True),

    Beat("What that produced",
         "Not a script. A typed contract: inputs with patterns, outputs with types, every\n"
         "step with several ways to find its target and a checkpoint proving it arrived.",
         [PY, "-c", SHOW_CONTRACT]),

    Beat("Replay it. No model anywhere",
         "Same artifact, a member it was never recorded against. Watch the browser drive\n"
         "itself. Nothing is deciding anything, it is walking a list.",
         replay("member.lookup_profile_and_savings_balance", "--param", "member_number=100240"),
         watchable=True),

    Beat("An answer that is not success",
         "No member 999999. That is a business outcome, not a crash. Exit code 0.\n"
         "An agent can branch on it.",
         replay("member.lookup_profile_and_savings_balance", "--param", "member_number=999999")),

    Beat("Refused before anything opens",
         "A malformed member number never reaches a browser. The capability's own contract\n"
         "rejects it.",
         replay("member.lookup_profile_and_savings_balance", "--param", "member_number=12")),

    Beat("The one that is not a lookup",
         "Searching VANCE returns four members. Two of them are both THEODORE J VANCE at\n"
         "WESTBROOK with the same status. On that screen nothing tells them apart. The step\n"
         "says: the SELECT link in the row holding the member number I was given.",
         replay("member.find_by_surname_and_read_balance",
                "--param", "surname=VANCE", "--param", "member_number=100252"),
         watchable=True),

    Beat("Run it again for the other one",
         "Same capability, same surname, one digit different. 112.00 the first time,\n"
         "6401.88 now. That is the row being chosen by a parameter, not by the recording.",
         replay("member.find_by_surname_and_read_balance",
                "--param", "surname=VANCE", "--param", "member_number=100253"),
         watchable=True),

    Beat("A missing thing is still an answer",
         "Member 100244 holds only a checking account. The profile has no savings row and\n"
         "the application says nothing at all. The absence is the answer, and the name and\n"
         "branch it did read still come back.",
         replay("member.find_by_surname_and_read_balance",
                "--param", "surname=RAGHAVAN", "--param", "member_number=100244")),

    Beat("A write, stopped for a person",
         "Thirteen steps, five screens, ending in something that cannot be undone. It drives\n"
         "to the review page and stops. The browser stays open and is yours: same session,\n"
         "same cookies, same screen. Answer from the second terminal.",
         replay("member.open_savings_subaccount",
                "--param", "member_number=100239", "--param", "account_type=SAVINGS",
                "--param", "opening_deposit=250.00", "--attended",
                "--allowlist", "allowlist.write.json", "--headed"),
         manual=[f"second terminal:   {PY} -m computer_use.escalation",
                 "then answer 'resume' to let it commit, or 'abort' to stop it"]),

    Beat("The same write, refused",
         "Default allowlist does not grant irreversible actions, and the capability is still\n"
         "a draft. An unattended agent cannot invoke it at all.",
         replay("member.open_savings_subaccount",
                "--param", "member_number=100242", "--param", "account_type=SAVINGS",
                "--param", "opening_deposit=250.00")),

    Beat("Things going wrong at runtime",
         "Each of these is injected into the application, then cleared. Watch the\n"
         "classification change. A recovered interstitial is still a success. An app error\n"
         "is not a checkpoint failure, it is named for what it was.",
         argv=["RUNTIME"]),

    Beat("The same schema on a screen with no markup",
         "24 lines of 80 characters. No DOM, no roles, no accessibility tree. The only\n"
         "address anything has is where it sits. Same engine, same artifact schema, no\n"
         "changes to either. The model discovered this flow too.",
         replay("teller.member_profile_and_balance", "--param", "member_number=100234", *TERMINAL),
         watchable=True),

    Beat("An agent, given the job in English",
         "It is handed the catalog of approved capabilities and a sentence. It picks one,\n"
         "passes typed arguments, and never sees a screen.",
         [PY, "-m", "computer_use.catalog", "--ask",
          THE_QUESTION],
         costs_money=True),

    Beat("All of it, checked in one command",
         "Every capability on both surfaces, every runtime condition, the guardrails, and\n"
         "an agent invoking by name. Exits non-zero if anything behaves differently from\n"
         "what its capability declared.",
         [PY, str(ROOT / "scripts" / "end_to_end.py")]),
]

RUNTIME = [
    ("a maintenance notice that clears when dismissed", {"interstitial_once": "on"}),
    ("a notice that will not clear", {"maintenance_interstitial": "on"}),
    ("the session dying mid flow", {"expire_after_requests": "4"}),
    ("the product's own error screen", {"app_error": "on"}),
]


def faults(**switches: str) -> None:
    body = "&".join(f"{k}={v}" for k, v in (switches or {"clear": "1"}).items()).encode()
    urllib.request.urlopen(urllib.request.Request(f"{WEB}/_faults", data=body), timeout=10)


def runtime_beat(watch: list[str]) -> None:
    """Inject each condition, run the same capability, and say what came back."""
    for description, switches in RUNTIME:
        faults(**switches)
        print(f"\n  {BOLD}{description}{OFF}")
        done = subprocess.run(
            replay("member.lookup_profile_and_savings_balance",
                   "--param", "member_number=100236", *watch),
            cwd=ROOT, capture_output=True, text=True, timeout=300, check=False)
        try:
            r = json.loads(done.stdout)
        except json.JSONDecodeError:
            print("    (no result)")
            continue
        detail = r.get("failure", {}).get("classification") or r.get("code") or ""
        extra = ""
        if r.get("recoveries"):
            extra = "  recovered: " + ", ".join(x["kind"] for x in r["recoveries"])
        print(f"    -> {r['status']} {detail}{extra}")
        faults()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("beat", nargs="?", type=int, help="which beat to run")
    parser.add_argument("--watch", action="store_true",
                        help="open the browser, or print each terminal screen, and slow down")
    args = parser.parse_args()

    if args.beat is None:
        print(f"\n{BOLD}Demo beats{OFF}   run one with:  python scripts/demo.py <n> [--watch]\n")
        for i, beat in enumerate(BEATS, 1):
            tag = f" {DIM}(uses a model, costs money){OFF}" if beat.costs_money else ""
            print(f"  {i:2}  {beat.title}{tag}")
        print(f"\n{DIM}Start the application first:  {PY} -m fake_core_banking_app.app{OFF}\n")
        return 0

    if not 1 <= args.beat <= len(BEATS):
        print(f"there are {len(BEATS)} beats")
        return 2
    beat = BEATS[args.beat - 1]

    try:
        urllib.request.urlopen(f"{WEB}/login", timeout=5)
    except (urllib.error.URLError, TimeoutError):
        print(f"Start the application first:\n    {PY} -m fake_core_banking_app.app")
        return 2

    print(f"\n{BOLD}{args.beat}. {beat.title}{OFF}\n\n{beat.say}\n")
    watch = ["--headed", "--slow", "300"] if (args.watch and beat.watchable) else []

    for line in beat.manual or []:
        print(f"  {DIM}{line}{OFF}")
    if beat.manual and not beat.argv:
        return 0
    if beat.manual:
        input("\n  press enter to start it, then open the second terminal ")

    if beat.argv == ["RUNTIME"]:
        faults()
        try:
            runtime_beat(watch)
        finally:
            faults()
        return 0

    shown = " ".join(x if " " not in x else f'"{x}"' for x in [*beat.argv, *watch])
    print(f"  {DIM}$ {shown.replace(PY, 'python')}{OFF}\n")
    return subprocess.run([*beat.argv, *watch], cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
