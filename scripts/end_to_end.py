"""Run every flow this system claims to handle, and check what each one returns.

No model is involved, so this costs nothing and is safe to run repeatedly. Pass
--include-writes to also exercise the committing path, which changes the
fixture's data. Pass --include-discovery to also drive the two model runs.
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
PYTHON = str(ROOT / ".venv" / "bin" / "python")
WEB = "http://127.0.0.1:8081"
TERMINAL = ["--allowlist", "allowlist.terminal.json", "--base-url", "teller://meridian"]

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


class Check:
    """One flow, the arguments that drive it, and what it must come back with."""

    def __init__(self, name: str, argv: list[str], **expect: str):
        self.name = name
        self.argv = argv
        self.expect = expect


def faults(**switches: object) -> None:
    """Set the fixture's fault switches, or clear them all with no arguments."""
    body = "&".join(
        f"{k}=on" if v is True else f"{k}={v}"
        for k, v in (switches or {"clear": 1}).items()
    ).encode()
    urllib.request.urlopen(urllib.request.Request(f"{WEB}/_faults", data=body), timeout=10)


WATCH: list[str] = []


def replay(argv: list[str]) -> dict:
    """Run one replay and return its result, whatever shape it came back in."""
    done = subprocess.run(
        [PYTHON, "-m", "computer_use.replay", *argv, *WATCH],
        cwd=ROOT, capture_output=True, text=True, timeout=300, check=False,
    )
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        return {"status": "crashed", "stderr": done.stderr[-400:]}


def verdict(result: dict, expect: dict[str, str]) -> tuple[bool, str]:
    """Compare a result against what the flow was supposed to do."""
    got = {"status": result.get("status")}
    if "code" in expect:
        got["code"] = result.get("code")
    if "classification" in expect:
        got["classification"] = (result.get("failure") or {}).get("classification")
    for name in [k for k in expect if k not in ("status", "code", "classification")]:
        got[name] = (result.get("outputs") or {}).get(name)
    wrong = {k: (v, got.get(k)) for k, v in expect.items() if got.get(k) != v}
    if wrong:
        return False, "; ".join(f"{k}: wanted {w!r}, got {g!r}" for k, (w, g) in wrong.items())
    return True, ", ".join(f"{k}={v}" for k, v in got.items())


def web_checks() -> list[Check]:
    lookup = ["member.lookup_profile_and_savings_balance"]
    surname = ["member.find_by_surname_and_read_balance"]
    return [
        Check("web: read a member's balance", [*lookup, "--param", "member_number=100236"],
              status="success", savings_balance="15630.00"),
        Check("web: no such member", [*lookup, "--param", "member_number=999999"],
              status="business_outcome", code="MEMBER_NOT_FOUND"),
        Check("web: malformed member number", [*lookup, "--param", "member_number=12"],
              status="failed", classification="input_invalid"),
        Check("web: pick one of four sharing a surname",
              [*surname, "--param", "surname=VANCE", "--param", "member_number=100252"],
              status="success", savings_balance="112.00"),
        Check("web: pick the other one, identical on screen",
              [*surname, "--param", "surname=VANCE", "--param", "member_number=100253"],
              status="success", savings_balance="6401.88"),
        Check("web: member holds no savings account",
              [*surname, "--param", "surname=RAGHAVAN", "--param", "member_number=100244"],
              status="business_outcome", code="NO_SAVINGS_ACCOUNT"),
        Check("web: surname nobody has",
              [*surname, "--param", "surname=ZZZZZZ", "--param", "member_number=100253"],
              status="business_outcome", code="NO_MEMBER_WITH_SURNAME"),
        Check("web: surname too short to search on",
              [*surname, "--param", "surname=V", "--param", "member_number=100253"],
              status="business_outcome", code="SURNAME_TOO_SHORT"),
    ]


def terminal_checks() -> list[Check]:
    teller = ["teller.member_profile_and_balance", *TERMINAL]
    return [
        Check("terminal: read a member's balance",
              [*teller, "--param", "member_number=100234"],
              status="success", savings_balance="4182.55"),
        Check("terminal: a longer figure in a right aligned column",
              [*teller, "--param", "member_number=100236"],
              status="success", savings_balance="15630.00"),
        Check("terminal: member holds no savings account",
              [*teller, "--param", "member_number=100244"],
              status="business_outcome", code="NO_SAVINGS_ACCOUNT"),
        Check("terminal: no such member", [*teller, "--param", "member_number=999999"],
              status="business_outcome", code="MEMBER_NOT_FOUND"),
        Check("terminal: malformed member number", [*teller, "--param", "member_number=12"],
              status="failed", classification="input_invalid"),
    ]


def guardrail_checks() -> list[Check]:
    write = ["member.open_savings_subaccount", "--param", "member_number=100240",
             "--param", "account_type=SAVINGS", "--param", "opening_deposit=250.00"]
    return [
        Check("guardrail: irreversible capability, default allowlist", write,
              status="failed", classification="policy_blocked"),
        Check("guardrail: draft capability, unattended",
              [*write, "--allowlist", "allowlist.write.json"],
              status="failed", classification="policy_blocked"),
        Check("guardrail: route outside the allowlist",
              ["member.lookup_profile_and_savings_balance", "--param", "member_number=100236",
               "--allowlist", "allowlist.locked.json"],
              status="failed", classification="policy_blocked"),
    ]


def fault_checks() -> list[tuple[str, dict, Check]]:
    """Each one sets a fault, runs a flow, and is cleared again afterwards."""
    lookup = ["member.lookup_profile_and_savings_balance", "--param", "member_number=100236"]
    return [
        ("a maintenance notice that clears when dismissed", {"interstitial_once": True},
         Check("runtime: recovered interstitial", lookup, status="success")),
        ("a notice that will not clear", {"maintenance_interstitial": True},
         Check("runtime: unrecoverable interstitial", lookup, status="failed")),
        ("the session dying mid flow", {"expire_after_requests": 3},
         Check("runtime: re-authenticated mid flow", lookup, status="success")),
        ("the product's own error screen", {"app_error": True},
         Check("runtime: application error", lookup,
               status="failed", classification="app_error")),
        ("every lookup returning no match", {"force_not_found": True},
         Check("runtime: forced not found", lookup,
               status="business_outcome", code="MEMBER_NOT_FOUND")),
        ("an operator without the privilege", {"permission_denied": True},
         Check("runtime: permission denied",
               ["member.open_savings_subaccount", "--param", "member_number=100240",
                "--param", "account_type=SAVINGS", "--param", "opening_deposit=250.00",
                "--allowlist", "allowlist.write.json", "--attended", "--no-confirm"],
               status="failed", classification="permission_denied")),
    ]


def catalog_check() -> tuple[bool, str]:
    """What an agent sees, and one capability invoked by name with typed arguments."""
    listed = subprocess.run([PYTHON, "-m", "computer_use.catalog"],
                            cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    count = listed.stdout.split(" ", 1)[0]
    called = subprocess.run(
        [PYTHON, "-m", "computer_use.catalog", "--call",
         "member.lookup_profile_and_savings_balance", "--arg", "member_number=100241"],
        cwd=ROOT, capture_output=True, text=True, timeout=300, check=False,
    )
    try:
        result = json.loads(called.stdout)
    except json.JSONDecodeError:
        return False, f"the call returned nothing parseable: {called.stderr[-200:]}"
    if result.get("status") != "success":
        return False, f"the call returned {result.get('status')}"
    return True, f"{count} capabilities listed, one invoked by name"


def locked_allowlist() -> Path:
    """An allowlist that permits the console but not a member's record."""
    base = json.loads((ROOT / "allowlist.json").read_text())
    base["name"] = "locked-down"
    base["routes"] = ["/", "/login", "/banner", "/nav", "/work", "/members/inquiry"]
    path = ROOT / "allowlist.locked.json"
    path.write_text(json.dumps(base, indent=2) + "\n")
    return path


def run(checks: list[Check], passed: list[str], failed: list[str]) -> None:
    for check in checks:
        ok, detail = verdict(replay(check.argv), check.expect)
        mark = f"{GREEN}pass{OFF}" if ok else f"{RED}FAIL{OFF}"
        print(f"  {mark}  {check.name:52} {DIM}{detail}{OFF}")
        (passed if ok else failed).append(check.name)


def _summary(passed: list[str], failed: list[str]) -> int:
    """Print the tally and give back the exit code it implies."""
    total = len(passed) + len(failed)
    print(f"\n{len(passed)}/{total} flows behaved as declared.")
    if failed:
        print(f"{RED}Failed:{OFF} " + ", ".join(failed))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-writes", action="store_true",
                        help="also commit a sub-account, which changes the fixture's data")
    parser.add_argument("--include-discovery", action="store_true",
                        help="also run the two model-driven discovery runs, which cost money")
    parser.add_argument("--watch", action="store_true",
                        help="open the browser and print each terminal screen, slowly")
    parser.add_argument("--only", choices=["web", "terminal", "guardrails", "runtime"],
                        help="run one group instead of all of them")
    args = parser.parse_args()
    if args.watch:
        WATCH.extend(["--headed", "--slow", "250"])

    try:
        urllib.request.urlopen(f"{WEB}/login", timeout=5)
    except (urllib.error.URLError, TimeoutError):
        print(f"{RED}The web fixture is not running.{OFF} Start it in another terminal:\n"
              f"    {PYTHON} -m fake_core_banking_app.app")
        return 2

    passed: list[str] = []
    failed: list[str] = []
    locked = locked_allowlist()
    faults()

    only = args.only
    try:
        if only in (None, "web"):
            print("\nThe legacy web application")
            run(web_checks(), passed, failed)

        if only in (None, "terminal"):
            print("\nThe character screen, same schema and same engine")
            run(terminal_checks(), passed, failed)

        if only in (None, "guardrails"):
            print("\nGuardrails")
            run(guardrail_checks(), passed, failed)

        if only not in (None, "runtime"):
            raise SystemExit(_summary(passed, failed))

        print("\nRuntime conditions, each provoked in the application")
        for description, switches, check in fault_checks():
            faults(**switches)
            try:
                run([check], passed, failed)
            finally:
                faults()
            print(f"        {DIM}{description}{OFF}")

        if only is not None:
            raise SystemExit(_summary(passed, failed))

        print("\nThe agent-facing catalog")
        ok, detail = catalog_check()
        mark = f"{GREEN}pass{OFF}" if ok else f"{RED}FAIL{OFF}"
        print(f"  {mark}  {'catalog: listed and invoked by name':52} {DIM}{detail}{OFF}")
        (passed if ok else failed).append("catalog")

        if args.include_writes:
            print("\nThe committing path (this changes the fixture's data)")
            run([Check(
                "write: sub-account opened and confirmed",
                ["member.open_savings_subaccount", "--param", "member_number=100245",
                 "--param", "account_type=SAVINGS", "--param", "opening_deposit=250.00",
                 "--allowlist", "allowlist.write.json", "--attended", "--no-confirm"],
                status="success")], passed, failed)

        if args.include_discovery:
            print("\nDiscovery, with a model in the loop")
            for goal, extra in (
                ("artifacts/goals/member.lookup_profile_and_savings_balance.json", []),
                ("artifacts/goals/teller.member_profile_and_balance.json", TERMINAL),
            ):
                done = subprocess.run(
                    [PYTHON, "-m", "computer_use.discovery", goal, *extra],
                    cwd=ROOT, capture_output=True, text=True, timeout=1800, check=False,
                )
                ok = "goal met" in done.stdout
                mark = f"{GREEN}pass{OFF}" if ok else f"{RED}FAIL{OFF}"
                name = f"discovery: {Path(goal).stem}"
                print(f"  {mark}  {name:52}")
                (passed if ok else failed).append(name)
    finally:
        faults()
        locked.unlink(missing_ok=True)

    return _summary(passed, failed)


if __name__ == "__main__":
    sys.exit(main())
