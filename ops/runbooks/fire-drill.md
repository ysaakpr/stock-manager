# Runbook — thesis-break fire drill (M6.7)

The M6 gate drill: prove that a break-condition disclosure reaches a journaled, in-policy decision
through the *real* monitoring path (§5.4), automatically and repeatably, without a live model or
the network. It is the standing regression that the T0→T1 escalation, the policy gate and the
evidence-pack reconstruction all still hold after any change to A5/A9.

## What it exercises

An integrity disclosure (an auditor resignation, §5.3 BC3) is injected onto a held name, and the
pipeline runs in production order:

1. **Interlock** — the data-red gate must say green, or nothing runs (invariant #10). The drill
   injects a green gate; the red short-circuit is proved separately in `tests/unit/test_t0.py`.
2. **T0 sweep** — `T0Monitor` runs its real mechanical checks over the book and the disclosure
   feed. The rails are set wide so *only* the announcement fires: the sweep escalates it, journals
   an `ESCALATE`, and queues the flag for T1 (this is the T0→T1 hand-off).
3. **Evidence bundle** — the queued flag is assembled into the exact bundle the model will read
   (`BundleBuilder`), content-addressed so the journal's `evidence_snapshot_ref` reconstructs it.
4. **T1 review** — `T1Reviewer` makes one metered `StubLLM` call, parses a schema-valid verdict per
   break condition, validates the proposed action against the ratified exit menu *in code*, and
   journals the decision with its model and token/rupee cost.
5. **Evidence pack** — `generate_pack` reads those rows back: the BROKEN verdict shows in the
   decision review, and the T1 spend shows in the cost-burn section.

A BROKEN core holding makes T1 journal an `ESCALATE` carrying a validated EXIT directive for A7 to
carry out — T1 has no broker and places no order itself (invariant #6). The "in-policy action" is
that validated directive on the journal line, not a trade.

## Run it

```bash
make up                                             # docker postgres must be reachable
uv run pytest tests/integration/test_fire_drill.py -q
```

The suite skips loudly if postgres is unreachable (`make up` first). It touches no network and
reads no wall clock — time is a `FrozenClock`, the model is `StubLLM`.

## The four things it asserts (M6.7 acceptance)

- **Escalation → verdict → in-policy action.** T0 outcome is `ESCALATED` and the flag is queued;
  T1 returns a verdict per break condition; the BROKEN core yields an in-policy IMMEDIATE exit
  (integrity unlocks it, §5.6) journaled as a T1 `ESCALATE`. The inversion — an IMMEDIATE proposed
  on a *fundamental* break, which the menu does not unlock — is rejected by `validate_action` and
  escalated to the human instead, with no A7 exit triggered.
- **Repeatable.** Two independent case journals produce the same verdict, outcome, action and
  byte-identical token spend (the stub pins its usage, so the cost does not drift with wording).
- **Cost per decision visible.** The T1 row carries `model` and a non-zero `TokenSpend`; the T0
  mechanical escalation carries none (T0 is ~₹0); the pack's cost-burn attributes the spend to the
  T1 tier and it traces back to that one journal line.

## When it fails

- **`not enough values to unpack … queue.pending`** — T0 did not escalate. Almost always the
  injected disclosure's text no longer satisfies the break-condition `KeywordQuery` (the matcher is
  whole-word-anchored; a phrase term must appear in order). Check `injected_disclosure()` against
  `BC3_QUERY`.
- **`UnknownPromptError` from the stub** — the bundle the reviewer sent differs from the one the
  stub was keyed on. The drill builds the bundle once and both registers and reviews it, so this
  means a change to `BundleBuilder`'s rendering or to the T0 flag shape; rebuild is automatic, so
  investigate what changed the prompt.
- **`EvidencePackError: no journal entries` / `no defined rate`** — the SIP park entry or the NAV
  valuation marks are missing or outside the window; the pack needs a cashflow to strike a return.

## Live-model counterpart

This is the stub-mode drill. The live-model re-run against the real Anthropic API — which judges
verdict *quality* and records real per-decision cost — is **M6.8**, parked on a credential (B4).
Passing this drill proves the plumbing, not the model's judgement.
