"""The operator surface: see what the automation is stuck on, and take the session.

    python -m computer_use.escalation

Shows any run waiting for a person. The browser the automation was using is
still open and is yours while a request is open. Do what is needed in that
window, then answer here.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

from computer_use.escalation.handoff import (
    HandoffBroker,
    InterventionResponse,
    describe,
    pending_requests,
)


def watch(evidence_root: Path, interval: float) -> tuple[Path, object] | None:
    """Block until a run needs a person, or the operator gives up."""
    print(f"watching {evidence_root}/ for runs that need a person. ctrl-c to stop.")
    while True:
        waiting = pending_requests(evidence_root)
        if waiting:
            return waiting[0]
        time.sleep(interval)


def answer(run_dir: Path, request, operator: str) -> int:
    print()
    print("=" * 78)
    print("A RUN NEEDS YOU")
    print("=" * 78)
    print(describe(request))
    print()
    print("The browser the automation was using is open and is yours right now.")
    print("Do whatever is needed in that window, then answer here.")
    print()

    decision = ""
    while decision not in ("resume", "abort"):
        decision = input("  resume (hand it back) or abort (stop the run)? ").strip().lower()
    note = input("  a note for the record, optional: ").strip()

    path = HandoffBroker(run_dir).answer(InterventionResponse(
        request_id=request.request_id, decision=decision, operator=operator, note=note,
    ))
    print(f"\nrecorded in {path}. the automation is taking the session back.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m computer_use.escalation",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--evidence-dir", default="evidence")
    parser.add_argument("--operator", default=getpass.getuser())
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true",
                        help="answer the oldest open request and exit")
    args = parser.parse_args()

    root = Path(args.evidence_dir)
    if args.once:
        waiting = pending_requests(root)
        if not waiting:
            print("nothing is waiting for a person.")
            return 1
        run_dir, request = waiting[0]
    else:
        try:
            run_dir, request = watch(root, args.interval)
        except KeyboardInterrupt:
            print("\nstopped watching.")
            return 1
    return answer(run_dir, request, args.operator)


if __name__ == "__main__":
    sys.exit(main())
