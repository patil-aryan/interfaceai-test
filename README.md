# Computer-Use Automation System

A record-once, replay-many automation layer for back-office applications that
have no API.

A model is given a goal in plain English and drives the real user interface
until the goal is met. That successful run is compiled into a **capability
artifact**: a typed, versioned, reviewable description of the flow. From then on
the artifact is replayed deterministically, with no model in the decision loop,
against different inputs.

> The model discovers. The artifact is the capability. Deterministic replay is
> the production path.

The target is a deliberately hostile stand-in for a legacy core banking system:
frameset layout, nested tables, non-semantic markup, no test IDs, and form field
names that are regenerated on every restart.

---

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[fixture,dev,discovery]"
.venv/bin/playwright install chromium
```

Discovery needs a model. Replay never does.

```bash
cp .env.example .env      # then put your key in ANTHROPIC_API_KEY
```

`.env` is gitignored. Credentials for the target application are supplied at run
time and never enter an artifact, a log, or the model's context.

### Running without live services

Everything runs locally. There is no external dependency beyond the model API,
and that is needed only for discovery. To exercise the whole system without a
model, replay the artifacts already in `artifacts/capabilities/`.

Start the target application first, in its own terminal:

```bash
.venv/bin/python -m fake_core_banking_app.app          # http://127.0.0.1:8081
```

---

## The demo path

**1. Discover a capability from a sentence.** Add `--headed --slow 500` to watch
the browser drive itself.

```bash
.venv/bin/python -m computer_use.discovery \
  "look up member 100234 and read their full name, membership status, and current savings balance"
```

This proposes the typed contract the sentence implies and saves it to
`artifacts/goals/` for review, drives the application until the goal is met,
confirms each declared business outcome against the real application, and writes
a replayable artifact to `artifacts/capabilities/`.

**2. Replay it deterministically, with different inputs and no model.**

```bash
.venv/bin/python -m computer_use.replay member.lookup_profile_and_savings_balance \
  --param member_number=100253 --attended
```

**3. Replay it into an error, to see how that is reported.**

```bash
# a member that does not exist: an answer, not a crash
.venv/bin/python -m computer_use.replay member.lookup_profile_and_savings_balance \
  --param member_number=100777 --attended

# a malformed member number: refused before a step runs
.venv/bin/python -m computer_use.replay member.lookup_profile_and_savings_balance \
  --param member_number=12 --attended
```

**4. Pick one record out of a list of several that look alike.** Four members
share the surname `VANCE`, and two of them share a first name, a branch and a
status as well. The capability searches by surname and then activates the
`SELECT` link *in the row holding the member number it was given*, which is a
locator bound to a parameter rather than to the recording.

```bash
for m in 100252 100253; do
  .venv/bin/python -m computer_use.replay member.find_by_surname_and_read_balance \
    --param surname=VANCE --param member_number=$m --attended
done
#   THEODORE J VANCE  WESTBROOK  112.00
#   THEODORE J VANCE  WESTBROOK  6401.88
```

The same capability shows what happens when the answer is a missing row rather
than a message. Member 100244 exists and holds only a checking account, so the
profile simply has no savings row and the application says nothing about it:

```bash
.venv/bin/python -m computer_use.replay member.find_by_surname_and_read_balance \
  --param surname=RAGHAVAN --param member_number=100244 --attended
#   business_outcome  NO_SAVINGS_ACCOUNT
#   partial outputs still returned: ANAND K RAGHAVAN, MAIN OFFICE
```

**5. Run a capability that changes something.** The default allowlist refuses it,
which is the point.

```bash
.venv/bin/python -m computer_use.replay member.open_savings_subaccount \
  --param member_number=100241 --param account_type=SAVINGS \
  --param opening_deposit=250.00 --attended
#   failed  policy_blocked
```

**6. Permit it, and a person is asked before anything is committed.** Leave this
running and answer it from another terminal.

```bash
.venv/bin/python -m computer_use.replay member.open_savings_subaccount \
  --param member_number=100241 --param account_type=SAVINGS \
  --param opening_deposit=250.00 --attended \
  --allowlist allowlist.write.json --headed
```

```bash
.venv/bin/python -m computer_use.escalation        # the operator surface
```

The browser stays open and is the operator's while the request is open. It is
the same session, with the same cookies, on the same screen. Answering `resume`
hands it back and the run continues from where it stopped.

**7. Run the same schema against a surface with no markup at all.** The teller
terminal is 24 lines of 80 characters. There is no DOM, no roles, no
accessibility tree, and the only address an element has is where it sits.

```bash
.venv/bin/python -m computer_use.replay teller.member_savings_balance \
  --param member_number=100234 \
  --allowlist allowlist.terminal.json --base-url "teller://meridian"
#   success  ELEANOR R VANCE  ACTIVE  4182.55
```

Add `--headed --slow 300` to watch it, which prints each screen as it is
painted. It is the character-screen equivalent of opening the browser.

Nothing in the replay engine changed to make that work. The application's
profile declares `surface_kind: terminal`, and the CLI builds the provider that
matches. A screen's name is its route, so the allowlist constrains which screens
automation may reach exactly as it constrains URLs on the web.

**8. See what an AI agent sees, and watch one call a capability.** This is what
the whole system is for: an agent that cannot read a screen, invoking a saved
capability by name with typed arguments.

```bash
.venv/bin/python -m computer_use.catalog            # the catalog, as an agent receives it
.venv/bin/python -m computer_use.catalog --json     # the tool schemas verbatim
```

A capability is discovered as a draft and may be exercised with a person
watching, but a production agent may not invoke it until someone promotes it:

```bash
.venv/bin/python -m computer_use.catalog --approve member.lookup_profile_and_savings_balance
```

Call one directly, with no model anywhere:

```bash
.venv/bin/python -m computer_use.catalog \
  --call member.lookup_profile_and_savings_balance --arg member_number=100253
```

Or hand the catalog to a model with a job in plain English:

```bash
.venv/bin/python -m computer_use.catalog \
  --ask "two members are called THEODORE J VANCE; I need the savings balance \
for the one whose member number is 100253, and for member 100244 whose surname \
is RAGHAVAN"
```

Exit codes: `0` for success and for a business outcome, `1` for a failure.
Every run writes `evidence/<run_id>/events.jsonl`, plus a screenshot and the
accessibility tree of every frame whenever something goes wrong.

---

## Exercising the error paths

The target application can inject faults, at `http://127.0.0.1:8081/_faults`
or by posting to it:

```bash
curl -s -X POST http://127.0.0.1:8081/_faults -d "interstitial_once=on"     # recovered
curl -s -X POST http://127.0.0.1:8081/_faults -d "expire_after_requests=3"  # recovered
curl -s -X POST http://127.0.0.1:8081/_faults -d "app_error=on"             # app_error
curl -s -X POST http://127.0.0.1:8081/_faults -d "clear=1"
```

Replaying the lookup capability under each of those shows the difference between
a recoverable condition, a business outcome, and a hard failure.

Operators: `operator1` / `changeme` can open accounts. `readonly1` / `changeme`
cannot, which is how permission denial is demonstrated.

---

## Layout

```
src/computer_use/
  schema/          the artifact, the result contract, one log line, the app profile
  surfaces/        the perceive-and-act seam, over a browser and a character screen
  discovery/       the model-driven loop, the contract, the compiler, verification
  replay/          the deterministic engine and its command line
  catalog/         saved capabilities, offered to an agent as callable tools
  guardrails/      the allowlist
  escalation/      the handoff broker and the operator surface
  evidence/        the append-only run log

artifacts/
  goals/           typed contracts, proposed from a sentence and kept for review
  capabilities/    compiled artifacts, replayable
  app_profiles/    per-product knowledge: sign-on, and screens that mean something

fake_core_banking_app/   the legacy web target. A fixture, not part of the system.
fake_core_teller/        the same bank on a 24x80 green screen. Also a fixture.
evidence/                one directory per run
allowlist.json           what the system is permitted to do
```

`REPORT.md` explains the design decisions and the trade-offs behind them.
