# Running the demo

Everything below is one command per beat. Nothing long has to be typed live.

```bash
.venv/bin/python -m fake_core_banking_app.app     # terminal 1, leave it running
.venv/bin/python scripts/demo.py                  # terminal 2, lists the beats
.venv/bin/python scripts/demo.py 7 --watch        # runs beat 7 with the browser open
```

Each beat prints what it is showing, the exact command it is about to run, and
then runs it. `--watch` opens the browser and slows it to 300ms a step, and on
the character screen it prints each 24x80 screen as it is painted. Faults are
set and cleared for you.

---

## The ten minute version

Beats 1, 4, 7, 8, 10, 12, 13. That is: the application, a replay, the row
disambiguation twice, the human handoff, things going wrong, and the character
screen.

Skip beat 2 and beat 14 unless you want to show a model working, because they
cost money and beat 2 takes a few minutes.

---

## What each beat is for

| # | Beat | The point |
|---|---|---|
| 1 | The application | Framesets, table layouts, no test IDs, no API. This is why the problem is hard |
| 2 | A model works it out, once | The only place a model drives a UI. Watch it get corrected and recover |
| 3 | What that produced | A typed contract, not a script. Read the inputs and outcomes out loud |
| 4 | Replay it | Same artifact, a member it never saw. Nothing is deciding anything |
| 5 | An answer that is not success | `MEMBER_NOT_FOUND`, exit code 0. Not a crash |
| 6 | Refused before anything opens | The contract rejects a bad member number with no browser |
| 7 | The one that is not a lookup | Four VANCEs, two identical on screen. The row is chosen by parameter |
| 8 | The other one | One digit different, a different balance. That is the proof for beat 7 |
| 9 | A missing thing is still an answer | No savings row, so `NO_SAVINGS_ACCOUNT` with partial outputs |
| 10 | A write, stopped for a person | Two terminals. The browser becomes theirs, same session |
| 11 | The same write, refused | Deny by default. No grant, still a draft, so no |
| 12 | Things going wrong | Four conditions injected and cleared. Watch the classification change |
| 13 | No markup at all | Same schema, same engine, a 24x80 green screen |
| 14 | An agent, in English | It picks a capability and passes typed arguments. Never sees a screen |
| 15 | All of it | 23 checks, exits non-zero on any mismatch |

---

## Beat 10 needs two terminals

This is the one worth rehearsing. Start it:

```bash
.venv/bin/python scripts/demo.py 10
```

It drives itself to the review screen and stops at step 10, `Authorise`, without
committing anything. In the second terminal:

```bash
.venv/bin/python -m computer_use.escalation
```

That shows what it is stuck on, which step, why, and where the screenshot is.
**The browser is now theirs.** It is the same session with the same cookies on
the same screen, not a fresh one. Offer to let them change something by hand.

Answer `resume` and the run finishes and reports the account it opened, with
both control transfers timestamped on the result. Answer `abort` and the
automation stops.

**Know this before you demo it.** While the request is open the browser really
is theirs, so if someone clicks `Authorise` in that window themselves, the
account gets opened by them, not by the automation. `abort` then means "the
automation did not proceed", not "nothing happened". The result says
`changed_the_screen: true` and counts an operator action, which is the system
working, not a bug. If that happens and you meant to show a clean abort, say so
out loud and point at the event. It is a better moment than the one you planned.

The fixture keeps its data in memory, so restarting it undoes any account that
got opened:

```bash
# in terminal 1, stop it with ctrl-c and start it again
.venv/bin/python -m fake_core_banking_app.app
```

---

## If someone asks which parts use a model

| Command | Model? |
|---|---|
| `computer_use.discovery` (beat 2) | yes, once per capability, and it drives the UI |
| `computer_use.catalog --ask` (beat 14) | yes, to pick a capability. It never sees a screen |
| everything else | no |

The log settles it. A discovery run carries `model_requested` and
`model_responded` events. A replay run carries none.

```bash
grep -c model_ evidence/curated/discovery-run/events.jsonl   # 26
grep -c model_ evidence/curated/replay-success/events.jsonl  # 0
```

---

## If someone asks about failure paths

Beat 12 runs four of them. The full list is in the README, and every row of it
is checked by `scripts/end_to_end.py`. To drive one by hand, the application has
a fault panel at `http://127.0.0.1:8081/_faults`, or:

```bash
curl -s -X POST http://127.0.0.1:8081/_faults -d "app_error=on"
.venv/bin/python -m computer_use.replay member.lookup_profile_and_savings_balance \
  --param member_number=100236
curl -s -X POST http://127.0.0.1:8081/_faults -d "clear=1"
```

The thing to point out: the result says `app_error`, not `checkpoint_failed`.
The app profile recognises the product's own error screen, so the failure is
named for what it actually was rather than for the step that noticed it.

---

## If something is broken on the day

```bash
.venv/bin/python scripts/end_to_end.py
```

23 checks, about two minutes, no model. If that passes, the system is fine and
the problem is the demo. If a beat misbehaves, run this first.

Operators: `operator1` / `changeme` can open accounts. `readonly1` / `changeme`
cannot, which is how permission denial is shown.
