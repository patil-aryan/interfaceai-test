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

## What it actually does

Four capabilities, all discovered by a model driving the real interface, then
compiled into artifacts that replay with no model involved.

| Capability | Steps | What makes it not a lookup |
|---|---|---|
| `member.lookup_profile_and_savings_balance` | 7 | the simple one, and the baseline |
| `member.find_by_surname_and_read_balance` | 8 | a search returns four members; two share a name, a branch and a status. It opens the row carrying the member number it was given |
| `member.open_savings_subaccount` | 13 | five screens, a product picker read off the app itself, a review page, and one step that cannot be undone. A person must authorise it |
| `teller.member_profile_and_balance` | 8 | the same schema and the same engine, on a 24x80 character screen with no markup at all |

---

## Prove all of it in one command

```bash
.venv/bin/python -m fake_core_banking_app.app     # in another terminal
.venv/bin/python scripts/end_to_end.py
#   23/23 flows behaved as declared.
```

That runs every capability on both surfaces, provokes every runtime condition
through the application's fault panel, checks the guardrails refuse what they
should, and has an agent invoke a capability by name. It exits non-zero if any
flow behaves differently from what its capability declared. No model is
involved, so it costs nothing.

```bash
.venv/bin/python scripts/end_to_end.py --watch     # open the browser and slow it down
.venv/bin/python scripts/end_to_end.py --only web  # web, terminal, guardrails or runtime
```

---

## The one to look at first: a write, with a person in the loop

This is the flow the whole design is for. Thirteen steps across five screens,
ending in something that cannot be undone, so the automation stops and hands the
live browser to a person.

Terminal one:

```bash
.venv/bin/python -m computer_use.replay member.open_savings_subaccount \
  --param member_number=100242 --param account_type=SAVINGS \
  --param opening_deposit=250.00 \
  --attended --allowlist allowlist.write.json --headed
```

It drives itself to the review screen and stops at step 10, `Authorise`. It has
not committed anything. Terminal two:

```bash
.venv/bin/python -m computer_use.escalation
```

That shows you what it is stuck on, why, which step, and where the screenshot
is. **The browser is now yours.** It is the same session with the same cookies
on the same screen, not a fresh one. Look at the review page, change something
by hand if you want, then answer `resume` or `abort`.

Answer `resume` and the run completes and reports the account it opened:

```
sub_account_number      0001-100242-02
opening_balance         250.00
control_events          agent → operator, then operator → agent, both timestamped
```

Answer `abort` and it stops without committing, and says so. If you changed the
screen while you held it, the result says that too.

The account it opens is real, in the sense that the application now holds it.
The fixture keeps its data in memory, so restarting it puts everything back.

Run it once with the default allowlist to see it refused before a browser opens:
`allowlist.json` does not grant `allow_irreversible`, and the capability is still
`draft`, so an unattended agent cannot invoke it at all.

---

## Which parts use a model, and which do not

This is the whole thesis, so it is worth being exact.

| Command | Model? |
|---|---|
| `computer_use.discovery` | **yes**, once per capability. It drives the real UI |
| `computer_use.catalog --ask` | **yes**, but only to choose a capability and its arguments. It never sees a screen |
| `computer_use.replay` | no |
| `computer_use.catalog --call` | no |
| `scripts/end_to_end.py` | no |

A run's log proves which it was. A discovery run carries `model_requested` and
`model_responded` events. A replay run carries none, and that absence is the
evidence that no model was in the decision loop.

```bash
grep -c model_ evidence/curated/discovery-run/events.jsonl   # non-zero
grep -c model_ evidence/curated/replay-success/events.jsonl  # zero
```

---

## Failure paths

A replay returns one of three shapes, never a boolean and never an exception.
Underneath `failed` there are thirteen classes. Each row below is provoked and
checked by `scripts/end_to_end.py`.

| What happens | What comes back |
|---|---|
| no member with that number | `business_outcome MEMBER_NOT_FOUND` |
| the member holds no savings account | `business_outcome NO_SAVINGS_ACCOUNT` |
| a surname nobody has | `business_outcome NO_MEMBER_WITH_SURNAME` |
| a deposit below the product minimum | `business_outcome INVALID_DEPOSIT_AMOUNT` |
| a malformed member number | `failed input_invalid`, before any step runs |
| a maintenance notice that clears | `success`, with the recovery recorded |
| a notice that will not clear | `failed`, after three attempts, saying so |
| the session dying mid flow | `success`, re-authenticated and replayed from step 1 |
| the product's own error screen | `failed app_error` |
| an operator without the privilege | `failed permission_denied` |
| a route outside the allowlist | `failed policy_blocked` |
| an irreversible step nobody authorised | `failed escalation_unanswered` |

To provoke them by hand, the application has a fault panel at
`http://127.0.0.1:8081/_faults`:

```bash
curl -s -X POST http://127.0.0.1:8081/_faults -d "interstitial_once=on"
curl -s -X POST http://127.0.0.1:8081/_faults -d "expire_after_requests=3"
curl -s -X POST http://127.0.0.1:8081/_faults -d "app_error=on"
curl -s -X POST http://127.0.0.1:8081/_faults -d "clear=1"
```

---

## Architecture

[The system diagram](https://excalidraw.com/#json=Wg5kkh_EieBgmpXVEqtWZ,Ch1wFCB3P-OmAA8wDy9jjQ)
shows the discovery loop step by step, the eight passes that turn a recorded run
into a program, the replay engine, and the seam both surfaces sit behind. The
same drawing is in `architecture.excalidraw`, and it renders inline at the top
of [REPORT.md](REPORT.md).

---

## Ask it something, in plain English

This is the point of the whole system, so start here. An agent is given the
catalog of approved capabilities and a job in words. It picks one, calls it with
typed arguments, and never sees a screen.

```bash
.venv/bin/python -m computer_use.catalog --ask "what is the savings balance for member 100236?"
```

It prints the capability it chose, the arguments it passed, the typed result
that came back, and its answer. Questions worth trying, each exercising
something different:

**1. A plain lookup.**

```bash
--ask "what is the savings balance for member 100236, and is their membership active?"
#   picks member_lookup_profile_and_savings_balance
#   PRIYA N RAGHAVAN, DORMANT, 15630.00
```

**2. Two people the screen cannot tell apart.** Members 100252 and 100253 are
both `THEODORE J VANCE`, both at `WESTBROOK`, and a surname search shows them on
adjacent rows with the same name, branch and status. Only the number separates
them, and the capability picks the row by the number it was given.

```bash
--ask "there are two members called THEODORE J VANCE. What is the savings \
balance for the one whose member number is 100253?"
#   picks member_find_by_surname_and_read_balance
#   6401.88, not 112.00
```

**3. An answer that is not a balance.** Member 100244 exists and holds only a
checking account. The profile simply has no savings row and the application says
nothing about it.

```bash
--ask "member 100244, surname RAGHAVAN. What is in their savings account?"
#   business_outcome NO_SAVINGS_ACCOUNT, with the name and branch it did read
```

**4. Something that changes data.** The catalog tells the agent the capability
is an irreversible write awaiting approval, so it stops and asks first.

```bash
--ask "open a new savings sub-account for member 100245 with an opening deposit of 250.00"
#   the agent reports the risk tier and asks for confirmation before proceeding
```

**5. A question with no answer.** There is no member 999999.

```bash
--ask "pull up member 999999 and tell me their balance"
#   business_outcome MEMBER_NOT_FOUND, exit code 0. An answer, not a crash.
```

Records worth asking about:

| Member | Name | Why it is interesting |
|---|---|---|
| 100234, 100241, 100252, 100253 | all VANCE | four share a surname, two share a name and branch |
| 100236, 100244 | both RAGHAVAN | one has savings, the other holds only a checking account |
| 100237 | DESMOND A HALE | restricted, zero balance, funds under a legal hold |
| 100245 | WENDELL ABERNATHY | dormant since 2006, `3.19` in savings |
| 100240 | COLM P BRENNAN | two accounts, so the savings row is not the only row |
| 100242 | HIRO S NAKAMURA | checking only, so there is no savings balance to give |

---

## Watching it drive the application

Nothing above opens a browser, because replay is meant to run unattended. Add
`--headed --slow 300` to any command to watch it work.

```bash
.venv/bin/python -m computer_use.replay member.find_by_surname_and_read_balance \
  --param surname=VANCE --param member_number=100253 --headed --slow 300
```

The same flags work on the character screen, where `--headed` prints each 24x80
screen as it is painted.

```bash
.venv/bin/python -m computer_use.replay teller.member_profile_and_balance \
  --param member_number=100234 --headed --slow 300
```

---

## Where the natural language actually enters

Two places, and they are different.

**A sentence becomes a capability.** This is discovery. A model is given a goal
and drives the real application until it is met, once. What it did is compiled
into an artifact that never needs a model again.

```bash
.venv/bin/python -m computer_use.discovery \
  "look up member 100234 and read their full name, membership status, and current savings balance" \
  --headed --slow 400
```

It first proposes the typed contract the sentence implies and saves it to
`artifacts/goals/` so a person can read and edit it before anything runs. Then
it drives the application, confirms each declared business outcome against the
real thing, and writes the artifact.

**A sentence becomes a call.** That is `--ask` above. No model touches the
application; the model only chooses which saved capability fits and what to pass
it.

To re-run a contract that was already reviewed, give it the file instead of a
sentence:

```bash
.venv/bin/python -m computer_use.discovery artifacts/goals/teller.member_profile_and_balance.json
```

---

## The rest of the demo path

**Replay deterministically, with different inputs and no model.**

```bash
.venv/bin/python -m computer_use.replay member.lookup_profile_and_savings_balance \
  --param member_number=100253
```

**Replay into an error, to see how that is reported.**

```bash
# a member that does not exist: an answer, not a crash
.venv/bin/python -m computer_use.replay member.lookup_profile_and_savings_balance \
  --param member_number=100777

# a malformed member number: refused before a step runs
.venv/bin/python -m computer_use.replay member.lookup_profile_and_savings_balance \
  --param member_number=12
```

**Open a sub-account, with a person authorising it.** That is the headline
demo above. It is thirteen steps, five screens, and one irreversible commit.

**Run the same schema against a surface with no markup at all.** The teller
terminal is 24 lines of 80 characters. There is no DOM, no roles, no
accessibility tree, and the only address an element has is where it sits.

```bash
.venv/bin/python -m computer_use.replay teller.member_profile_and_balance \
  --param member_number=100234
#   success  ELEANOR R VANCE  ACTIVE  4182.55
```

The model discovered that flow too, on the same loop that discovers a browser
flow. Only the words for naming a control differ, because a green screen has
captions and columns where a page has roles and names. Nothing in the replay
engine changed to make it work: the application's profile declares
`surface_kind: terminal` and the provider is built from that. A screen's name is
its route, so the allowlist constrains which screens automation may reach
exactly as it constrains URLs on the web.

**See what an agent sees.**

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
  --call member.find_by_surname_and_read_balance \
  --arg surname=VANCE --arg member_number=100252
```

Exit codes: `0` for success and for a business outcome, `1` for a failure.
Every run writes `evidence/<run_id>/events.jsonl`, plus a screenshot and the
accessibility tree of every frame whenever something goes wrong.

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
scripts/end_to_end.py    runs every flow and checks what each one returns
architecture.excalidraw  the system diagram
allowlist.json           what the system is permitted to do
```

`REPORT.md` explains the design decisions and the trade-offs behind them.
