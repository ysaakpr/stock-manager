# M17 analyst strategy: what short-horizon professionals look at, and how the AI fund managers use it

*Written 2026-10-09 from four research passes: fundamental and event signals, price and volume
signals, LLMs as analysts, and a field-level survey of this lake. It feeds pre-registration
Amendment 1 (`preregistration-m17-ai-fund-managers-2026-10-09.md` §8), the M17.9 Commons screens and
the manager prompt `analyst/fundmanager/prompts/manager.md`.*

## 0. Five conclusions that shape everything below

1. **Three things carry real evidence at 1–12 weeks.**
   - Momentum: 6-1 and 12-1 returns, 52-week-high proximity, sector strength.
   - Earnings momentum: surprise plus the price reaction on the results day, plus volume.
   - Hard red-flag avoidance.

   Named practitioner systems (Minervini, CAN SLIM, Darvas, Weinstein) are bundles of those same
   parts. No independent cost-inclusive test of any of them as a whole system exists.
2. **The 1–4 week horizon has a trap: short-term reversal.** Last week's big winners tend to give
   back gains over the next 1–4 weeks, most strongly in illiquid names and on moves that arrive
   *without volume*. India shows this clearly (Chui et al. 2023; Parthasarathy and Sendilvelu 2022).
   High-volume moves continue; low-volume spikes revert.
3. **LLMs add value as structured readers of data the system supplies, not as traders left
   alone.**
   - Kim, Muhn and Nikolaev found that step-by-step analysis of supplied statements beat analysts.
   - Live arenas and long backtests (FINSABER, StockBench, AI-Trader, Alpha Arena) found
     unconstrained LLM traders rarely beat passive after costs, mis-size by regime, and overtrade.
   - Risk control decided robustness, so risk belongs in the rails.
4. **LLM probabilities are overconfident and lean towards "yes", and more reasoning does not fix
   that** (KalshiBench 2025).
   - The only prompt intervention with measurable benefit is **giving the model base rates**
     (Schoenegger, Tetlock et al. 2025).
   - "Reason like a Bayesian" prompts measurably hurt.
   - Calibration has to be engineered: base rates in the prompt, shrinkage and scoring in code.
5. **Do not trust any number the model writes itself.** FinanceBench and StockBench both show
   hallucinated or mis-computed figures. The platform computes every number; the model cites
   field ids, and code rejects any citation that doesn't exist.

## 1. The decision funnel

```
~2,000 NSE names
  │  L0 HARD EXCLUSIONS (code, never visible as candidates)
  │    GSM / ESM / trade-for-trade, price band ≤ 5 %, below the liquidity floor,
  │    integrity event in the last 60 sessions (auditor/KMP resignation, SEBI order,
  │    rating downgrade to sub-IG / "issuer not cooperating", default)
  ▼
~1,600 universe (M17.1 sheet)
  │  L1 SCREENS (Commons, mechanical, shared, ≤ 15 names each — §2)
  │    S1 Trend leaders · S2 Volume breakout · S3 Pullback in a leader
  │    S4 Earnings momentum · S5 Event watch (facts, not ranked)
  │    + the pre-registered composite shortlist (top 40, the control book's input)
  ▼
≤ ~90 distinct names on the manager's desk
  │  L2 MANAGER TRIAGE (round 0): pick ≤ 12 for deep research, using the screens,
  │    the regime and its own book; must state the claim each request tests
  ▼
≤ 12 deep dives (rounds 1–2): per-ISIN dossier + web snapshots + disconfirming evidence
  │  L3 MEMO + DECISION (final round): scenarios → p → action, cost hurdle, stop, invalidation
  ▼
RAILS (code): caps, participation, min hold, regime-independent; mechanical stop execution
```

The funnel exists because of evidence on retrieval and attention. Forecasting accuracy rose with
five or more relevant documents (Halawi et al.). LLMs drift towards large, famous names when they
choose freely (2503.08750, 2507.20957). Screens give the manager a broad, mechanical starting set
across every cap tier. A manager may still research a name outside the screens, and the scoreboard
measures whether doing so pays.

## 2. Screens (mechanical, computed once per session in the Commons)

Every field below is computable from this lake today (lake survey, 2026-10-09). Thresholds are fixed
here and frozen at S0. The screens list facts and ranks; they never say "buy".

| Screen | Serves | Rule | Evidence |
|---|---|---|---|
| **S1 Trend leaders** | Positional, mainly | Close > SMA200 and SMA200 rising over 21 sessions; close ≥ 0.85 × the 52-week high; rank by the mean z-score of (6-1 return / σ₂₅₂) and (12-1 return / σ₂₅₂) (the Nifty Momentum 30 construction), with a bonus for a top-half sector 6-1 return. Top 15 | Momentum strong; 52-week-high proximity moderate (India: Raju 2023); industry momentum moderate–strong |
| **S2 Volume breakout** | Swing, mainly | Close at a 60-session closing high; that session's volume ≥ 2 × its 50-session median; 5-session return < 15 % (not extended); ATR(10)/ATR(50) < 0.8 over the prior 10 sessions (contraction before the move). Rank by volume ratio. Top 15 | High-volume return premium moderate (Gervais et al.; 41 countries, Kaniel et al.); volume-conditioned continuation (NSE study); contraction mainly helps with timing |
| **S3 Pullback in a leader** | Swing | Top-quintile 6-1 momentum and close > SMA200; down 4–12 % from the 20-session high over the last 3–10 sessions; pullback volume below its 20-session median; close within 3 % of SMA20 or SMA50. Rank by momentum. Top 15 | Uses the reversal effect rather than fighting it: buys leaders after a low-volume dip |
| **S4 Earnings momentum** | Both | Results filed in the last 30 sessions; SUE = (EPS_q − EPS_{q−4}) / σ(8 quarterly YoY changes) in the top quintile; earnings-announcement return over [−1, +1] versus NIFTY 500 > 0; day-0 volume ≥ 2 × the 50-session median. Rank by SUE + EAR rank. Top 15 | PEAD moderate (India 2013–2018 studies, mixed recently); EAR adds to SUE and the two together beat either alone (Brandt et al.); earnings momentum subsumes price momentum and avoids crashes (Novy-Marx) |
| **S5 Event watch** | Both | Facts only, unranked: bulk and block deals (buyer, side, % of equity), buyback, bonus or split announcements, order-win announcements, index inclusion or exclusion announcements, results board-meeting intimations in the next 10 sessions — each from a frozen keyword table on announcement subjects | Insider and promoter buys moderate; bulk deals mostly *before* the event; buybacks moderate; bonus and split effects last days, not weeks; index inclusion transient |

**Per-name features in every dossier** (computed, cited by field id):
- momentum: 1w, 4w, 13w and 52w returns, 6-1 and 12-1;
- distance from the 52-week high and the 200-session mean, ATR(14), ATR as a % of price, 20-session
  volatility;
- volume ratios: 5-session over the 50-session median, and day-0;
- delivery: delivery % and a z-score of delivered quantity against the name's own 60 sessions;
- for F&O names: open-interest build-up category, put-call ratio on OI, rollover;
- valuation and growth: P/E against the sector median, TTM revenue and PAT growth, ROE, net-margin
  trend;
- earnings: SUE and EAR of the last results;
- debt-equity (the only leverage field the XBRL facts carry);
- events: announcements in the last 20 sessions (subject lines), corporate actions due, cap tier.

**What the lake cannot supply, so the manager must use the web or go without:**
- promoter pledge and its changes (the shareholding table is near-empty and pledge is always null);
- MF, FII and DII holdings;
- cash flow and absolute debt;
- consensus estimates;
- a reliable results calendar (board-meeting intimations exist only as announcement subjects).

The prompt names these gaps so the manager knows where a web query is worth spending.

## 3. Base rates (the calibration anchor)

The Commons publishes a frozen **base-rate table** from the lake, 2016-10 → 2026-09, on the PIT
universe at the ₹1 cr floor. It is bucketed by:
- screen membership (S1–S4, composite, none);
- cap tier (large, mid, small);
- regime (the three-state gate below);
- horizon (5, 20 and 60 sessions).

Each cell gives P(beat NIFTY 500), the median excess return, the interquartile range of the excess
return, and n. Every candidate memo starts from its cell's base rate and must justify each move away
from it with a named reason and an approximate size. That is the "reference class first" practice,
and it is the one prompt technique that measurably helped LLM forecasters.

The table is computed from **history** and contains no LLM output. That makes it a fact, so sharing
it does not couple the managers.

## 4. Regime (computed, given to every manager; never a rail)

| State | Rule (NIFTY 500 TRI proxy) | Typical professional response |
|---|---|---|
| **RISK-ON** | Index > rising SMA200, and the share of the universe above its SMA50 ≥ 50 % | Full gross exposure; breakouts and leaders |
| **NEUTRAL** | Anything else that is not RISK-OFF | Fewer new positions, stricter entry (top screen ranks only), tighter stops |
| **RISK-OFF** | Index < SMA200 and 24-month return < 0, **or** 126-session realised volatility in its top 20 % with the index below SMA200 | Raise cash in steps; no new breakouts; prefer low-volatility leaders; momentum-crash state (Daniel–Moskowitz; Cooper et al.) |

India VIX, breadth (share above SMA50 and SMA200, advance/decline) and sector returns are supplied
alongside it. The regime is information for the manager, not a lock on its decisions. FINSABER and
StockBench show LLM agents mis-size in down markets, so the scoreboard reports performance per
regime state.

## 5. Exits (evidence-ranked)

| Exit | Evidence | M17 rule |
|---|---|---|
| **Stop-loss on momentum or breakout positions** | Moderate–strong. A 10 % stop on momentum cut the worst month from −50 % to −11 % and raised Sharpe (Han–Zhou). Stops add value when returns are serially correlated (Kaminski–Lo) | The manager sets `stop_pct` per BUY, from 1.5–3 × ATR, at most 15 %. **The stop is mechanical**: a close below it triggers a sale at the next open with no LLM involved. The manager may tighten a stop, never loosen it |
| **Trailing stop** | Moderate; the exact parameters are not robust | Positional: the manager may convert its stop to a trail (2–3 × ATR, or a close below SMA50); executed mechanically |
| **Time stop** | Weak but cheap | Guidance: swing positions with no progress (< +1 × ATR) after 10–15 sessions, positional after ~30, must be justified or exited |
| **Profit target** | Fixed caps cut the right tail that pays for momentum; selling winners early is a documented error (disposition effect) | No fixed targets. Partial trims are allowed and must name what changed |
| **Thesis invalidation** | Practitioner standard | Each BUY carries ≥ 1 observable, dated invalidation (a metric or event, a threshold, a date), evaluated mechanically where its data is in the lake |
| **Red-flag event** | Strong as a tail event (auditor exits, SEBI orders, downgrades) | A new integrity event in a held name puts a forced `SELL` review first in the next session; the manager must sell or give an evidence-cited reason |

## 6. Swing vs positional playbooks

The prompt skeleton is identical for every manager; only the playbook block differs, driven by the
mandate.

**Swing (1–4 weeks)**
- **Entries:** S2 breakouts on volume, S3 pullbacks in leaders, early S4 (the first 1–10 sessions
  after results).
- **Never:** a name up ≥ 15 % in 5 sessions on weak volume (the reversal trap); a name locked at its
  upper circuit; a name with results due within 3 sessions unless the thesis *is* the results.
- **Stops:** about 2 × ATR. Time stop at 10–15 sessions.
- **Liquidity:** prefer names well above the floor. The reversal and impact evidence is
  concentrated in the illiquid tail.

**Positional (1–3 months)**
- **Entries:** S1 trend leaders, S4 earnings momentum, sector leadership.
- **Quality filters, which matter here as crash protection rather than alpha:**
  - debt-equity < 1.5 outside financials;
  - positive TTM PAT;
  - no persistent margin decline.
- **Stops:** 2.5–3 × ATR, convertible to a trail. Time stop ~30 sessions.
- **Results:** hold through results only when the name's SUE history is positive and the position
  sits within its risk budget.

## 7. How the prompt addresses each known LLM failure

| Failure mode (evidence) | Countermeasure |
|---|---|
| Hallucinated or mis-computed numbers (FinanceBench, StockBench) | All numbers come from the bundle and are cited as `[F:<field_id>]` or `[S:<snapshot_id>]`. Code rejects unknown ids. The model never does arithmetic the platform can do |
| Overconfidence and a "yes" lean (KalshiBench; the silicon-crowd study) | Base-rate cell first; named adjustments; scenarios before p; code shrinks p towards the base rate, and both raw and shrunk p are scored |
| Anchoring, sunk cost and disposition (2412.06593; capability makes sunk cost *worse*) | Holdings are reviewed with **no cost basis or P&L shown**: "would you buy it today at today's price on today's evidence?" P&L reaches the rails only |
| Overtrading | No action is the stated default; an explicit cost hurdle from the shared cost model; minimum hold; turnover appears on the scoreboard |
| Reading the news after the move (Lopez-Lira) | Every news item carries its timestamp and the move since; a required `already_priced_in` field; residual drift is most plausible in small caps and on bad news |
| Herding into large, famous names | Screens span every cap tier; the cap-tier mix of BUYs is reported against the control book |
| Mis-sizing by regime (FINSABER, StockBench) | Regime state is in every bundle; the memo must say how the thesis behaves in it; performance is reported per regime |
| Self-critique theatre and debate sycophancy (Huang et al.; 2509.23055) | Disconfirmation comes from **data**: each research request must include a "kill test", the data most likely to sink the idea. No inner bull/bear personas |
| Forecasting where it has no edge (Halawi: the edge is selective) | A required `edge_type`. `NONE` forces PASS or HOLD; the model is told abstaining is expected on most names |
| Schema violations by reasoning models (StockBench) | A flat schema with one repair retry (M17.4) |
| Prompt tuning on history is contaminated (memorization studies) | Prompts are fixed before S0 and never tuned on backtests; prompt variants are new managers |

## 8. What changes in the plan

These are additive and dated 2026-10-09, before S0, as pre-registration §8 Amendment 1:

1. The Commons gains screens S1–S5, the per-name dossier features (§2), the base-rate table (§3)
   and the regime state (§4). The work is new task M17.9; the composite shortlist and the control
   books are unchanged.
2. New hard exclusions in the universe: a price band ≤ 5 %, and an integrity event in the last 60
   sessions (a frozen keyword table).
3. A manager's declared stop is executed mechanically and can only be tightened (the M17.7 job;
   rails in M17.5's style).
4. The decision schema gains fields: `edge_type`, `base_rate_cell`, `adjustments[]`, `scenarios[]`,
   `already_priced_in`, `cost_hurdle_check`, `premortem[]`, `catalyst`. Probabilities are scored
   raw (the pre-registered Brier criterion) **and** shrunk (secondary).
5. Buys into a session where the stock opens and stays locked at its upper band are unfilled
   (SimBroker check; the M17.7 job).

## 9. Sources (selected; full lists in the research notes)

- Momentum and reversal:
  - Joshipura (NSE), https://nsearchives.nseindia.com/content/research/res_paperfinal223.pdf
  - Chui et al. 2023, https://researcher.manipal.edu/en/publications/momentum-reversals-and-liquidity-indian-evidence/
  - Daniel–Moskowitz, https://www.nber.org/papers/w20439
  - Nifty200 Momentum 30 whitepaper, https://www.niftyindices.com/docs/default-source/indices/nifty200-momentum-30/nifty200_momentum_30_index_whitepaper_sep_20.pdf
  - Quantpedia on the 52-week high in India, https://quantpedia.com/an-analysis-of-52-weeks-high-effect-on-indian-stocks/
  - Freefincal on momentum and liquidity, https://freefincal.com/?p=345828
- Volume: Kaniel–Ozoguz–Starks 2012; Gervais–Kaniel–Mingelgrin WP, https://rodneywhitecenter.wharton.upenn.edu/wp-content/uploads/2014/04/9901.pdf
- Stops: Han–Zhou, summarised at https://alphaarchitect.com/2016/08/taming-the-momentum-roller-coaster-fact-or-fiction/; Kaminski–Lo, https://dspace.mit.edu/entities/publication/bb69ca4b-0cdc-487f-831d-63b2e84fafee
- Earnings:
  - Novy-Marx, https://mysimon.rochester.edu/novy-marx/research/FMFM.pdf
  - Brandt et al., via https://quantpedia.com/strategies/post-earnings-announcement-effect/
  - India PEAD, https://repository.iimb.ac.in/handle/2074/20501 and https://eprints.nottingham.ac.uk/26569
  - Martineau 2022, https://cfr.ivo-welch.info/published/papers/martineau2021rest.pdf
- Events and red flags:
  - Promoter pledging and crash risk, https://ideas.repec.org/a/eme/jamrpp/jamr-01-2023-0003.html
  - Bulk deals, https://studentlive.iimcal.ac.in/sites/all/files/pdfs/wp_863.pdf
  - Index inclusion, https://nsearchives.nseindia.com/content/research/comppaper90.pdf
  - GSM, https://nsearchives.nseindia.com/s3fs-public/inline-files/Rules_vs_discretion_in_market_surveillance_Aggarwal_Bhatia_Zaveri_WhitePaper_0.pdf
- LLMs:
  - Kim–Muhn–Nikolaev, https://arxiv.org/abs/2407.17866
  - Lopez-Lira–Tang, https://arxiv.org/abs/2304.07619
  - FINSABER, https://arxiv.org/abs/2505.07078
  - StockBench, https://arxiv.org/abs/2510.02209
  - AI-Trader, https://arxiv.org/abs/2512.10971
  - KalshiBench, https://arxiv.org/abs/2512.16030
  - Schoenegger et al., https://arxiv.org/abs/2506.01578
  - Halawi et al., https://arxiv.org/abs/2402.18563
  - FinanceBench, https://arxiv.org/abs/2311.11944
  - Huang et al., https://arxiv.org/abs/2310.01798
- Evidence caveat: most India-specific studies are small event studies, and many were read as
  abstracts or summaries. Every threshold above is a starting point frozen for a fair test, not a
  claim of optimality.
