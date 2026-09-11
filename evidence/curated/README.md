# Curated evidence

One directory per run. Each holds `events.jsonl`, the structured log the system
writes, and `result.json`, what the caller received. Runs that went wrong also
hold a screenshot and the accessibility tree of every frame, captured at the
moment of failure.

| Directory | What it shows |
|---|---|
| `discovery-run/` | a model driving the real interface to meet a goal, plus `discovery.json`, the trace that was compiled into an artifact |
| `replay-success/` | that capability replayed for a different member, with no model involved |
| `replay-not-found/` | a member that does not exist. A business outcome, exit code 0, nothing at error level, no screenshot taken |
| `replay-bad-input/` | a malformed member number, refused by the capability's own contract before any step ran |
| `replay-app-error/` | the product's error screen, recognised by the app profile and reported as `app_error` rather than a bare checkpoint failure |
| `replay-session-recovered/` | the session timing out mid-flow: detected, re-authenticated, and the read-only flow replayed from its first step |
| `replay-row-disambiguated/` | four members share a surname and two share a name, a branch and a status; the row is picked by the member number the caller supplied, not by anything on the recording |
| `replay-no-savings/` | a member who holds no savings account. There is no savings row and the application says nothing, so the answer is the absence itself, returned as a business outcome with the name and branch it did read |
| `replay-policy-blocked/` | an irreversible capability refused by the allowlist before a browser did anything |
| `replay-deposit-rejected/` | an opening deposit below the product minimum. A business outcome, detected by wording confirmed against the running application |
| `replay-write-committed/` | the same capability permitted, opening a real sub-account for a member it was not recorded against |
| `replay-terminal/` | a capability the model discovered on a 24x80 character screen, replayed by the same engine. No markup, no roles, no accessibility tree, and no engine change |
| `artifacts/` | the compiled capabilities, the typed contracts they came from, and the app profile |

## Telling the two kinds of run apart from the log alone

Both write the same event schema and differ by `run_kind`. A discovery run
carries `model_requested` and `model_responded` events. A replay run carries
none, which is the evidence that no model was in the decision loop.

## What is masked, and what is deliberately not

`events.jsonl` and `discovery.json` are redacted as they are written. A member
number appears as `1002**`, a name as `THEO************`, and `redacted_fields`
names what was hidden so a reader cannot mistake masked for empty. Masking is by
value rather than by field, so one value carrying two sensitivity tags takes the
stricter of them.

The discovery trace additionally keeps **no raw screen text and no free prose**
the model wrote. Those can name records the capability never read, and nothing in
the system can recognise what it was never told about. The scrubbed step intents
say what was done.

Two things here are **not** masked, on purpose.

**`result.json`** is what the caller received, and the caller asked for it. This
is the asymmetry the design rests on: redaction happens at the persistence
boundary, not the computation boundary. In a real deployment the result would be
returned and not written to disk; it is kept here because it is the evidence that
the capability works.

**`inputs[].example` in each artifact** is part of the published contract, shown
to a calling agent deciding whether the capability fits its task. Against a real
system that example would be a synthetic identifier rather than a live one.

Everything in this directory is fictional fixture data. No credential appears
anywhere in it.
