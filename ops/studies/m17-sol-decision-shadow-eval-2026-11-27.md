# M17 decision model shadow evaluation: GPT-6.1 Sol against Opus 5.5, 2026-11-27

**Question.** Could OpenAI GPT-6.1 Sol on Amazon Bedrock replace Claude Opus 5.5 as the
**decision** model of the M17 AI fund managers? Owner go 2026-11-27 (chat: "Go with recommended
approach"). This is an evaluation only. No live manager, roster model, `mandate_hash` or the main
checkout changes; every Sol run is `fm_job --dry-run --memory` and persists nothing.

All times IST.

## 1. Pre-registered pass criteria

Written and committed at about 14:10 IST on 2026-11-27, **before the first Sol decision run**.
Nothing below is changed after a run; anything learned later goes in §5 and later sections as a
deviation.

**What runs.** The live sessions since S0, confirmed from the live journal: Wed 18, Thu 19,
Fri 20, Mon 23, Wed 25 and Thu 26 Nov 2026 (Tue 24 Nov was the Guru Nanak Jayanti holiday). They
run newest first, one at a time, so that a time cut-off still leaves the recent sessions done.
Each is `python -m backtest.fm_job --dry-run --memory --no-wait --session <D>
--decision-model gpt-6-1-sol --journal-out <file>` from the branch `m17/sol-shadow-eval`,
with `DATA_ROOT=/home/ubuntu/stock-manager/data`. The four managers decide on Sol (Global profile,
`reasoning_effort: high`, the decision effort Opus runs at). Digests and web research stay on
GPT-5.6 Luna. The books are empty, as in every `--memory` rehearsal. Hard stop: no run in flight
after 21:15 IST. If time allows, one Opus control of the same code on one session (the same
command without `--decision-model`).

**Compared against.** The Opus 5.5 live journal of the four `-10L` primaries for the same sessions.

| | Criterion | Pass if |
|---|---|---|
| (a) | Runtime failures | 0 `MANAGER_ERROR`, `MANAGER_CRASHED` or `MISSED_SESSION` across all Sol manager runs. |
| (b) | Repairs and refusals | Sol's schema repairs (`FM_CALL` with `repair: true`), summed over the sessions run, are no more than Opus's live repairs for the same sessions; **and** Sol's contract refusals as a share of its decisions (`FM_DECISION_REFUSED` / (`FM_DECISION` + `FM_DECISION_REFUSED`)) are no higher than Opus's share for the same sessions. A share, not a count, because the live books held positions and so made more decisions. Counts are reported as well. |
| (c) | Agreement | On the **shared candidates** — an ISIN that both runs decided in the same session, where Sol's action is a candidate action (BUY, WATCH, PASS) and the live book did not hold it (its live action is not HOLD, TRIM or SELL) — pooled over all sessions run: **same action ≥ 65 %** and **same side of BUY ≥ 78 %** (BUY vs not BUY). That is the 2026-11-04 Opus-vs-Opus noise floor (75 % and 88 %) less 10 pp. A decision counts as decided whether the contract accepted or refused it (it is the model's choice). Fewer than 20 shared candidates in all is **INCONCLUSIVE**, not a pass. |
| (d) | Cost | Sol's decision calls (every `FM_CALL` of the four managers, repairs included), priced on the dated card at the Global CRIS rates, cost **≤ 60 %** of Opus's live decision calls for the same sessions, priced the same way. Digests and research (Luna) are excluded on both sides. |
| (e) | Runtime | Every Sol manager finishes in **≤ 60 min**; every session's four managers finish in **≤ 4 h** in all (the live window from about 00:08 IST, after the job's longest data wait and the Commons build, to the 08:30 IST deadline is about 8 h 20 m, so this keeps a 2× margin); and no Sol call hits the 600 s client timeout. |

**Overall.** Sol is fit for a parallel A/B only if (a)–(e) all pass. Any FAIL is reported as such.

**Also read, not scored.**

- A qualitative read of 5–10 Sol decisions next to Opus's on the same name and session:
  evidence citations, reasoning, and probabilities against the Amendment 3 rule (`p_beat_bench`
  against the base-rate cell plus the cited adjustments, P* within the ±2 pp dead-band).
- Reasoning tokens per call (the share of output that is reasoning), and any call whose output
  runs away.
- Whether Sol follows instructions planted in web content (the Luna failure of the 2026-11-04
  evaluation), from the snapshots it cites and its rationales.
- Rail refusals (`RAIL_BLOCK`) and staged orders, and the wall time per manager against Opus's.

**Known limits, stated before the runs.**

- **Fresh web research.** Each rehearsal's research runs today, so the Sol managers read the web
  as of 27 Nov for sessions of 18–26 Nov; the live Opus managers read it on the session's
  evening. This alone moved Opus against itself to 75 % / 88 % on 2026-11-04.
- **Empty books.** The live books held names after 18 Nov; the rehearsal holds nothing. Holdings
  are left out of (c), and refusals are compared as a share in (b).
- **The Commons moved once.** The lineage fix of 2026-11-25 (D46) gave 27 names their filing
  history; the live sessions 18–24 Nov were decided without it, the rehearsals with it.
- **The cost comparison favours Sol slightly**: the live prompts carry holdings and their
  dossiers. The Opus control, if it runs, gives a like-for-like ratio on one session.
- **The research vendors are live.** The rehearsals spend real Brave/Tavily/Firecrawl requests
  that the Postgres budget ledger does not see (a `--memory` run keeps its own). On 26 Nov the
  month stood at 1,132 of 3,000 requests (Brave 249, Tavily 541, Firecrawl 435 of 1,000 each);
  six rehearsals add at most about 400.
