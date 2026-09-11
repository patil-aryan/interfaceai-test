# Design write-up

## 1. Architecture

The system is split at one seam: **discovery is expensive, uncertain and happens
once; replay is cheap, deterministic and happens forever.**

```
a goal in English
   -> contract        the typed inputs and outputs the goal implies, saved for review
   -> discovery       a model drives the real UI, observe / decide / act
   -> compiler        the successful trace becomes a capability artifact
   -> verification    each declared business outcome is provoked and confirmed
   -> replay          the artifact runs with new inputs, no model in the loop
   -> escalation      when it cannot safely proceed, a person takes the live session
```

Five decisions shape everything else.

**The replay engine is an interpreter, not generated code.** It walks an
artifact and dispatches on a fixed action vocabulary. The alternative, having
the model emit Python, produces something nobody can review, diff, version or
approve before it runs unattended against member records. Data can be reviewed.

**One action vocabulary, three consumers.** The `Action` enum is simultaneously
the tool list handed to the model, the step types an artifact may contain, and
the engine's dispatch table. Nothing is translated between them. There is no
`click`, because a terminal has no mouse; the verb is `activate`.

**The model's locator is thrown away.** When the model says "the link named
MEMBER INQUIRY", that finds the element once. `Surface.describe_target` then
harvests every way of addressing it, applies each candidate back to the live
page, and records only those that resolve to the same element. What the model
chose is evidence; what the page can prove is the capability.

**The engine is a total function.** `run()` returns a `ReplayResult` for every
input and never raises. The caller is an agent, not a person at a terminal, and
a traceback is not one of the three shapes it understands.

**Application knowledge lives in an app profile, not in artifacts.** Sign-on, and
the screens that mean "session expired" or "not authorised", belong to the vendor
product. Fixing one of those fixes every capability at once.

Trade-off accepted: a single process, synchronous, file-backed. Queues,
databases and services would add infrastructure the brief explicitly does not
reward, and the seams for them are visible (the event sink, the handoff broker).

## 2. Artifact schema

An artifact is a **contract**, not a step list, because an AI agent has to decide
whether it answers the task in front of it.

- **Typed inputs**, each with a regex the value must match in full, a
  sensitivity tag, and a `unique_key` flag. A capability tiered
  `irreversible_write` cannot be constructed unless at least one input is a
  unique key: acting on a surname risks acting on the wrong person, and that is
  enforced by a model validator, not by convention.
- **Typed outputs**, coerced on the way out. Money is `Decimal`, never `float`.
- **An ordered step list**, every step carrying a plain-English `intent` written
  for a reviewer, and a `checkpoint` asserting it actually arrived.
- **A locator chain per target**, ordered best first: accessible role and name,
  label, placeholder, text, a scoped DOM path, grid coordinates, pixels. Replay
  records which one fired.
- **A success condition** asserted once at the end, distinct from the per-step
  checkpoints, because every step can pass while the screen shows the wrong
  record.
- **Declared business outcomes**, each with a detector confirmed against the
  running application.
- **Provenance**, naming the goal, the model, the run, and the institution it was
  recorded against; it points at the transcript and never embeds it.
- **Approval status and risk tier**, which the guardrails read.

Three layers were designed for and one is implemented: app profile (per vendor
product), capability (per flow), institution override (per tenant).

## 3. Determinism and error handling

**Determinism** comes from the locator chain plus polling. Every strategy in a
chain was verified against the live page at recording time, so a fallback is a
rung that has actually worked, not a guess. `resolve` retries the whole chain to
a deadline, because a screen still loading is indistinguishable from one missing
the control. Container scopes resolve to the **innermost** match: nested tables
make ancestors match too, and taking the first silently reads a different
member's row.

**Drift** is measured on success. Every resolution compares the strategy that
fired against the one recorded. A capability that starts resolving by a weaker
strategy still works and is decaying, and that appears on the result before it
ever fails.

**The result is one of three shapes**, never a boolean:

| Shape | Meaning | Example |
|---|---|---|
| `success` | the goal was reached | the balance, typed |
| `business_outcome` | a legitimate answer that is not success | `MEMBER_NOT_FOUND` |
| `failed` | something the caller must debug | `app_error`, with what step, expected, observed |

Underneath, thirteen failure classes. Every one that the fixture can provoke has
been made to fire:

| Condition | Reported as |
|---|---|
| a member that does not exist | `business_outcome MEMBER_NOT_FOUND` |
| a deposit below the product minimum | `business_outcome INVALID_DEPOSIT_AMOUNT` |
| a malformed member number | `failed input_invalid`, before any step runs |
| a one-off maintenance notice | `success`, with `dismissed_interstitial` recorded |
| a notice that will not clear | `failed`, after three attempts, saying so |
| the session timing out mid-flow | `success`, with `reauthenticated` recorded |
| the product's own error screen | `failed app_error` |
| an operator without the privilege | `failed permission_denied` |
| a route outside the allowlist | `failed policy_blocked` |
| an irreversible step nobody authorised | `failed escalation_unanswered` |

Recovery is bounded and risk-aware. Re-authenticating restores the session but
not the place in the flow, so a **read-only** capability is replayed from its
first step, and a writing one is stopped: replaying a submit could open a second
account.

## 4. Heterogeneity and multi-tenant

**Surface abstraction.** `Surface` is an abstract class owning policy, with one
implementation owning mechanism. The base class owns the fallback order, the
definition of degradation, the retry deadline and the settle rule; subclasses
implement locate, act and observe. The engine imports `Surface`, never
Playwright, and contains no mention of a browser or a DOM. A terminal surface
implements `_locate` for `grid` and raises for `structural`; the chain skips a
rung a surface cannot express rather than failing, so one artifact can carry
strategies for several surfaces. Perception is the accessibility tree, which
exists on desktop platforms too, rather than the DOM, which does not.

**Multi-tenant.** The fixture serves two institutions running the same product
at different versions, with different wording: `MEMBER ID` against
`ACCOUNT NUMBER`, `Search` against `Find`, `SAVINGS` against `REGULAR SHARES`.
The intended resolution is three layers merged at load time: the app profile for
what is true of the product, the artifact for the flow, and an institution
override patching only what a tenant renamed. Drift between tenants surfaces as
degradation counts on artifacts replayed against a tenant they were not recorded
on, which is the signal that an override is needed. The override layer is
designed and its directory exists; it is not implemented, and that is a cut.

## 5. Escalation and handoff

"Stuck" is detected three ways: a step whose declared policy is `escalate`, a
step marked irreversible when confirmation is required, and a recognised screen
the profile cannot recover from.

The handoff is real rather than described. The engine **stops driving the browser
it already has**. Nothing is closed and nothing is re-created, so the operator
works in the same session, with the same cookies, on the same screen. An
intervention request is written carrying the capability, the goal, the step, why
it stopped, and a screenshot plus the accessibility tree of every frame. A
minimal operator surface shows it and takes the answer. Control transfers are
recorded on the result as `ControlEvent`s naming who held the session and
whether they changed anything, and the run resumes from where it stopped.

The mechanism is two files in the run's evidence directory. That is deliberately
the smallest thing that is genuinely real, and it is the seam: a queue, a
websocket or a co-browsing console replaces those two writes without the engine
noticing. The operator surface is mocked; the control-transfer model is not.

## 6. Safety

**An explicit allowlist**, deny by default. Permitted hosts, route patterns and
action types, plus separate grants for irreversible actions and for replaying an
unapproved artifact unattended. A missing allowlist is an error, never an open
door: the failure mode of that choice is a system that will not start, and the
failure mode of the alternative is a system that starts and does anything.

It checks where a step **landed**, not only where it was sent. Checking only
`navigate` steps constrained nothing, because the member page is reached by
following a link; and checking only the top-level URL constrained nothing
either, because in a frameset the address bar never moves. Every live frame must
be permitted.

**Risk is handled conservatively.** An irreversible capability is refused unless
the allowlist grants it; only the step that actually commits is marked, so the
gate does not fire on every menu click; and a person is asked before it runs.

**Redaction happens at the persistence boundary**, not the computation boundary.
Inputs and outputs are masked on the way into the log according to their
sensitivity, with `redacted_fields` naming what was hidden so a reader cannot
mistake masked for empty. The result returned to the caller carries the real
values, because the caller asked for them. No locator is ever built from a value
the run supplied or read.

Redaction is applied by value rather than by field, so one value carrying two
tags takes the stricter of them. A sub-account number arrived once as a `pii`
output and again as an `internal` one; masking each field on its own tag left the
second copy in the clear beside the masked first. The same rule applies to the
discovery trace, which additionally drops the raw text of every screen it saw:
those screens hold other people's records, and nothing in the system can know
which parts of them matter.

**Limits.** The allowlist is host and path based, so it cannot express "this
operator may open accounts below this amount". Redaction still depends on the
contract's tags being right, and those tags are proposed by a model; the one rule
that does not depend on that is a `unique_key` input, which is always treated as
identifying. Only values the run itself supplied or read can be masked, so
another member's data visible on the same screen is not recognised, which is why
the trace keeps no screen text at all. The model's context is not redacted: it
sees the screen, which is unavoidable for computer use and would need a separate
masking layer before a real deployment. Finally, the result handed back to the
caller is deliberately unmasked, because the caller asked for it; the evidence
directory keeps a copy of that result, and in a real deployment it would not.

## 7. Cuts

Deliberately left out, each at a seam that exists:

- **The institution override layer.** Designed, directory present, not merged at
  load time. The fixture already serves the second tenant to build against.
- **A second surface.** The abstraction is exercised only by one implementation,
  which is the honest limit of the claim.
- **A real operator console.** The handoff is two files and a terminal.
- **An agent-facing catalog.** Artifacts are invoked by id on the command line;
  exposing them as a tool schema is mechanical from the contract already there.
- **Multi-run stability scoring.** The `stability` block exists on every artifact
  and nothing updates it.
- **Outcome triggers the model cannot supply.** An outcome that cannot be
  provoked is dropped rather than shipped as a detector that would never fire,
  so some real outcomes are absent instead of wrong.

What I would build next, in order: the institution override layer, because it is
the claim the brief presses hardest on and the fixture is already built for it;
then the agent-facing catalog, because the typed contract makes it nearly free;
then stability scoring, because approval should be earned by evidence rather
than asserted.
