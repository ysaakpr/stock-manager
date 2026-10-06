# M6.8 — Live-model fire drill and T1/T2 quality review

**Task:** M6.8 (amended under HUMAN_DECISIONS D16) · **Run:** 2026-10-06 23:32 IST, once ·
**Model:** `claude-opus-5` via **`ClaudeCliLLM`** (`claude -p` on this machine's subscription
login, Claude Code 2.1.291) — *not* the Anthropic API · **Mode:** paper, fixtures only. No broker,
no order, no Kite code touched. · **Verdict: PASS.** Every live test passed. Six model calls were
made against a budget of 25.

This closes the open item that `ops/gates/M6.md` named: M6.9 passed on StubLLM, and live-model
quality and cost were left to this task.

## How it was run

```
uv run pytest tests/integration/test_fire_drill.py -q -m live
......                                                                   [100%]
exit=0   real 1m12.6s
```

- **Collected:** 6 live test cases from 5 test functions. The drill test is parametrised `a`/`b`,
  so the same fixture ran twice to check stability; the other four are three T1 samples and one T2
  deep review.
- **Calls made: 6 / 25.** Every review parsed on its first attempt, so the worst case of 12 calls
  (6 reviews × `max_attempts=2`) was not approached. `BudgetedLLM` counts each call before making
  it, and calls `pytest.exit(returncode=6)` instead of making call 26.
- **What one "call" is:** one `claude -p` invocation. The CLI runs with `--max-turns 2`, because a
  `--json-schema` request needs a turn of its own, so a single invocation may contain up to two
  internal model turns. The 25-call budget therefore bounds invocations, not model turns: the worst
  case is 50 turns. T1 and T2 do not request `--json-schema`, and the token counts above are the
  CLI's totals across whatever turns an invocation took.
- **One extra call outside the drill:** before the run, a single probe call (`"Reply with the
  single word: ok"`) checked which model id the CLI reports, because an id missing from
  `accounting/model_prices.yaml` would have raised `UnknownModelError` mid-drill. It reported
  `claude-opus-5`, which is priced. Counting the probe, the task used **7 model calls in total**.
  The probe went straight to `ClaudeCliLLM`, so the budget did not count it. **Any future probe
  must go through `BudgetedLLM`, inside the `-m live` session**, so that every real-model call is
  counted against the same ceiling.
- **Transcript:** the session writes every response verbatim, with token counts and latency, to
  `<pytest basetemp>/m6_8_live_transcript.json`. The verdicts quoted below come from that file.
  It contains no credential: the CLI holds its own login and the test never sees it.
  **This run's transcript was pruned before it could be committed.** pytest keeps only the three
  most recent basetemp directories, and later runs (this task's `make check` and other agents' test
  runs) rotated out the directory it was in. The quotes and token figures below were copied from it
  while it existed. A future live run should copy the file to `ops/gates/evidence/` straight away.

What `make check` sees: `tests/integration/conftest.py` deselects every `live` test unless the run
says `-m live`, so a bare run of this file reports `7/13 tests collected (6 deselected)` and makes no model
call. Two tests pin this, both running in `make check` without a model:

- `test_a_bare_run_deselects_every_live_test` runs a collect-only pass with no `-m`, with
  `-m live` and with `-m "not live"`. I checked that it **fails** when the conftest gate is removed.
- `test_the_live_call_budget_aborts_before_the_26th_call` lets 25 calls through to a `StubLLM`.
  The 26th raises pytest's `Exit` with return code 6, and the stub's call log still shows 25.

## Real model vs the StubLLM baseline, on the same fixtures

### The fire drill (M6.7 fixtures unchanged, run twice)

**StubLLM baseline** (canned, from `broken_verdict_json()`): BC1 `INTACT`, BC3 `BROKEN`, then
`EXIT / IMMEDIATE`, with the rationale *"integrity break; exit immediately per the ratified menu"*.
The usage is pinned at 1,500 in / 200 out.

**Real model:** both runs matched the baseline's verdicts and action exactly. Each went T0
`ESCALATED`, then T1 `BROKEN`, then `validate_action` cleared `EXIT / IMMEDIATE` (`rejection:
null`). The result was a T1 `ESCALATE` journaled with the exit directive and **no `SELL`**: the
exit is A7's to carry out.

Run `a`, verbatim:

> BC3 `BROKEN` — *"Announcement feed carries a disclosure with subject 'Resignation of Auditor' on
> 2026-08-07 — the exact disclosure BC3 names, escalated T0→T1 as specified."*
> Action rationale — *"An integrity break condition is met on the issuer's own disclosure; auditor
> resignation invalidates reliance on reported segment figures the thesis rests on, and price
> (1000, undisturbed) shows no dislocation yet, so staging only risks exiting into the repricing."*

Run `b`, verbatim:

> BC3 `BROKEN` — *"Announcement feed carries a disclosure with subject 'Resignation of Auditor' on
> 2026-08-07, exactly the condition's trigger."* Rationale — *"BC3 is a T0 integrity condition and
> is met on its face by the auditor-resignation disclosure; integrity breaks are not waited out."*

Both runs reached the same decision. Their reasoning was worded differently and differed in depth,
which is expected because the real model is not deterministic. Determinism is still guaranteed
where M6.7 put it: the journal records the bundle and the verdict, and the stub path stays
byte-identical.

### T1 sample — three bundles that carry the disclosure body

These bundles were built directly, so that each puts one specific piece of evidence in front of the
reviewer. The drill's own bundle does not carry a disclosure body (finding F1).

| Scenario | Evidence | Real-model verdict | Action (after `validate_action`) | Defensible? |
|---|---|---|---|---|
| Resignation, with text | Mid-term resignation citing unverifiable related-party transactions | BC3 `BROKEN`, BC1 `INTACT` | `EXIT / IMMEDIATE`, exit triggered | Yes |
| Auditor **rotation** (keyword false positive) | Section 139(2) ten-year term end, unmodified opinion, no concerns | BC3 `INTACT`, BC1 `INTACT` | `HOLD` | Yes, and this is the most important result |
| One down quarter | Segment revenue -4.2% QoQ after +6.2% the quarter before; order book +9% | BC1 `WEAKENED`, BC3 `INTACT` | `HOLD` | Yes |

Verbatim:

- **Rotation:** BC3 — *"The 2026-08-07 filing is a mandatory Section 139(2) auditor rotation at end
  of a ten-year term with an unmodified FY26 opinion and no reported concerns — not a resignation;
  the keyword hit is a false positive."* Summary — *"Auditor change is mandatory rotation, not
  resignation — both break conditions remain intact; hold and close the T1 review."*
- **One down quarter:** BC1 — *"Automation segment revenue fell 4.2% QoQ (Rs 430cr → Rs 412cr) but
  the prior quarter rose 6.2%, so only one consecutive decline; order book up 9% QoQ."* Summary —
  *"…hold and re-check BC1 on the September-quarter results."* The arithmetic is correct: 430 to 412
  is -4.19%.
- **Resignation with text:** BC3 — *"Filing 2026-08-07 10:00 IST discloses statutory auditor
  resignation with immediate effect, mid-term, citing inability to obtain audit evidence on
  promoter-group related-party transactions."*

There is no StubLLM baseline for these three, because a stub only returns the verdict a test
supplies. The point of the sample is the comparison the stub cannot make: the same keyword hit
(BC3, "auditor") went to `EXIT` when the body described a resignation and to `HOLD` when it
described a rotation.

### T2 — the M6.5 deep-review fixture (`tests/unit/test_t2.make_request`)

**StubLLM baseline** (`review_json()` default): `GREEN`, both theses `INTACT`, no tilts, dial
unchanged.

**Real model:** `AMBER`, both theses `WEAKENED`, a tilt toward `INE001A01001`, and
`recommended_tactical_pct: null` (so the dial is unchanged and no proposal was raised). The answer
was schema-valid on the first attempt, assessed exactly the two theses, and tilted only toward a
name already held in the core.

Verbatim:

> INE001A01001 `WEAKENED` — *"Still mapped to systems integration but refreshed purity 0.60 vs
> ratified 0.80 — material drift, no T1 breach (no two-quarter segment revenue decline evidenced),
> so thesis holds at reduced conviction."*
> INE002A01009 `WEAKENED` — *"Absent from the refreshed 50-name value chain entirely against a
> ratified purity of 0.70 — chain membership unconfirmed, but the fundamental T1 trigger has not
> fired, so not broken pending next segment disclosure."*
> Steering — *"Tilt new SIP money to the one holding still confirmed inside the refreshed chain
> rather than the one that dropped out of it; the higher-purity Bench X is not held and cannot be
> added here."*

## Judgement on output quality

**T1 is good, and better than the plumbing needed.** On all five T1 reviews the verdicts were
defensible, the actions were sane, and every `observed` line cites something that is actually in
the bundle: the subject line, the section number, the revenue figures. The model:

- applied BC1's "two consecutive quarters" literally, rather than treating any decline as a break;
- caught a keyword false positive that T0 cannot, by design, catch;
- chose `IMMEDIATE` only for the integrity break, which is the one case the ratified menu unlocks
  it for;
- never proposed an action that `validate_action` had to reject.

The weak spot is **decoration in the rationale, not the decision.** In run `a` the model wrote that
the price was *"undisturbed"* and *"shows no dislocation yet"*. The bundle carries a single close
(`1000`, as of the drill date), and one data point cannot show the absence of a dislocation. The
exit would have been correct without that clause; the clause just isn't supported by the evidence.

**T2 is plausible but over-reads its evidence, and the over-reading moves the steering.** The
INE001A01001 assessment is well grounded: the brief does show purity 0.8 ratified against 0.600000
refreshed. The INE002A01009 assessment is not. The brief lists **three candidates out of a 50-name
universe**, and the model turned "not among the three listed" into *"absent from the refreshed
50-name value chain entirely"*. That is a claim about the 47 names the brief never showed it. The
inference then drove the one steering decision in the review: tilting new SIP money away from
INE002A01009. The harm is bounded, because steering stays inside the ratified dial and adding or
dropping a name needs a human. Even so, this is the example a reviewer of T2 output should look for:
a confident sentence about evidence that was never in the bundle. The `AMBER` headline is itself
defensible. A 0.2 purity drift is a reasonable thing to flag, and arguably more honest than the
stub's canned `GREEN`.

## Cost per decision

**These figures are token counts multiplied by list price, not an invoice.** A subscription has no
per-call bill; what it consumes is the plan's rate limit. The counts are the ones the CLI reported.
The rupee figures were priced by `accounting.tokens.TokenPricer` from
`accounting/model_prices.yaml`: claude-opus-5 at $5 / $25 per MTok in/out, 5-minute cache writes at
$6.25 per MTok, and USD/INR 88.00. That card is itself `provenance: reconstructed`.

| Decision | Input + cache-write tok | Output tok | Latency | Cost (₹) | ≈ USD |
|---|---|---|---|---|---|
| T1 drill `a` | 2 + 5,804 | 983 | 14.9 s | 5.36 | 0.061 |
| T1 drill `b` | 2 + 5,804 | 1,010 | 15.5 s | 5.42 | 0.062 |
| T1 resignation (text) | 2 + 5,899 | 476 | 7.5 s | 4.29 | 0.049 |
| T1 rotation | 2 + 5,937 | 474 | 7.9 s | 4.31 | 0.049 |
| T1 one down quarter | 2 + 5,946 | 468 | 7.4 s | 4.30 | 0.049 |
| **T1 mean (n=5)** | ≈ 5,880 | 682 | 10.6 s | **4.73** | **0.054** |
| **T2 deep review (n=1)** | 2 + 5,981 | 1,023 | 16.5 s | **5.54** | **0.063** |

For comparison, the StubLLM baseline's pinned 1,500 / 200 prices at about ₹1.10 per T1 decision.
The real cost is roughly four times that.

Three things in these numbers are artefacts of the CLI rather than of the reviewer:

- **About 5,000 of the ~5,900 input tokens are harness overhead.** The bundle builder estimates the
  drill's T1 prompt at 167 tokens, plus a system prompt of a few hundred. The probe call, a one-word
  question, already wrote 5,022 tokens. On the API the same T1 review would be roughly 800 input
  tokens. That is an estimate, not a measurement, but it puts the API cost near ₹2 rather than ₹4.7.
- **All input is billed as a cache write (1.25× the input rate), and nothing was ever read back**
  (`cache_read = 0` on all six calls, inside 72 seconds). The CLI writes its prefix to the cache on
  every call and never reuses it across invocations.
- **Output runs to 470–1,020 tokens for a JSON answer of about 250.** The CLI does not break the
  difference down. It is consistent with adaptive thinking billed as output, but some of it may be
  a second internal turn within the same invocation (see "What one 'call' is" above). The
  transcript cannot tell the two apart. The two drill runs, whose bundle lacks the disclosure body,
  used about twice as much output as the bundles that carried it.

## Failures

**No test failures, retries, malformed answers, refusals or policy rejections.** Six calls, all
first-attempt, `stop_reason: end_turn` on every one. The findings below are about the system around
the model, not about the run. None of them blocks this task. F1, F3 and F4, and the
T2 brief's 3-of-50 candidate list, are now rows in `ops/BACKLOG.md`.

- **F1. The drill's T1 bundle carries the flag line but not the disclosure.** `run_drill` builds the
  `BundleRequest` without `announcements=`. The model therefore decided the drill from *"break
  condition BC3 keyword hit on 1 announcement(s): Resignation of Auditor"* alone, with `Recent
  filings & news: (none within the token budget)`. The verdict was right, but only because the
  subject line happened to be truthful. The rotation sample shows that T1 can reject a false
  positive **only when it is shown the body**. Whatever production path builds T1 bundles should
  pass the triggering announcement through. This is worth a backlog item. I left the M6.7 fixture
  unchanged, so that the comparison above stays like-for-like.
- **F2. The drill's thesis is an unratified `PROPOSAL`.** The rendered bundle reads *"Thesis (v1,
  PROPOSAL)"*. The model did not object, and T1 does not enforce ratification; T2 does, in
  `T2Request.__post_init__`. In production the theses shown to T1 should be ratified, and that is
  not currently checked at T1.
- **F3. The journal line carries the cost but not the provider.** `TokenSpend` is tokens in, tokens
  out and ₹. Whether a cost was invoiced (API) or a subscription estimate (CLI) is recorded only in
  `token_usage.provider`, and the drill runs `MeteredLLM` with `ledger=None`, so for these decisions
  the distinction lives only in this report. A burn report that sums the journal would mix the two
  kinds of cost.
- **F4. The output ceiling is not enforced.** `ClaudeCliLLM` ignores `max_tokens` (the CLI exposes
  no ceiling), so the 1,000-token drill answers were uncapped. This had no effect here, but it means
  the budget is a call count, not a token count.

## Verdict on the M6 exit criteria (EXECUTION_PLAN.md §9, M6)

> ✅ Thesis-break fire drill: injected news matching a break condition escalates T0→T1, produces a
> verdict + journaled action within policy; monthly evidence pack auto-generates; token cost per
> decision visible in journal.

| Criterion | StubLLM (`ops/gates/M6.md`) | Real model (this run) |
|---|---|---|
| Injected news escalates T0→T1, verdict, journaled in-policy action | PASS | **PASS**, 2/2 drill runs: T0 `ESCALATED`, T1 `BROKEN`, `EXIT / IMMEDIATE` cleared by `validate_action`, T1 `ESCALATE` journaled, no `SELL` |
| Monthly evidence pack auto-generates | PASS, with caveat | **Not re-exercised: the pack does not depend on the model.** `generate_pack` reads only the journal. The M6.9 caveat still stands: nothing in `dataplatform/scheduler/` invokes it on a monthly timer (checked 2026-10-06), so "auto" means invoked by an operator or a test. |
| Token cost per decision visible in journal | PASS (pinned stub usage) | **PASS, with real counts:** each live T1 `ESCALATE` row carries `cost_inr > 0` (asserted), at 5,806 tokens in and ₹5.36 / ₹5.42 for the two drill runs. The provider is not on the line (F3). |

**Overall: M6's first and third boxes now hold against a real model, not only the stub. Its second
box is unchanged by this run and keeps the scheduling caveat.** Live-model quality, which M6.9 left
open, is now assessed: T1 verdicts were defensible on the sampled bundles; fitness in paper mode
depends on F1 (bundles must carry the disclosure body). T2 is usable but needs human reading of its
per-thesis claims (see the INE002A01009 example above). M6.8's acceptance:

- fire drill passes on the real model, which is named (`ClaudeCliLLM`), with the verdict captured
  verbatim — **met**;
- cost per T1 and per T2 decision recorded, stated as token count × list price — **met**;
- a written quality judgement with specific examples — **met**;
- `live` deselected by a bare run, and a test pins it — **met**;
- at most 25 calls, with the abort pinned under a stub — **met** (6 used).
