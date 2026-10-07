> **PROPOSAL — NOT RATIFIED, NOT BUILT.** Committed for the record under HUMAN_DECISIONS D21 (polly, under the owner's delegation of 2026-10-06). Nothing below is an accepted plan, and no task in TASK_GRAPH.yaml implements it. Its figures are as of 2026-09-07 and were not re-checked when it was committed.

# AI in the analyst: what it is worth, and the plan to get it — 2026-09-07

**Status:** PROPOSAL, awaiting owner ratification. Nothing here is ratified and no code has changed.
**Question asked:** *"How to improve the entire system analyst quality using the AI aspect. I don't see any
AI usage in the current plan. I want AI to be used to enhance the buying and selling decision in the
portfolio management. Prepare a clear plan… and research what kind of added value in terms of overall
profit I can get. How are various fund managers achieving much more growth in terms of XIRR."*

**Evidence base.** Five independent read-only investigations, all under `/tmp/polly-reports/`:
`plan-ai-gaps.md` (1163 lines — what the plan specifies), `decision-path.md` (691 — what the code does),
`data-inventory.md` (514 — what the lake holds), `ai-alpha-evidence.md` (913 — the academic evidence),
`xirr-benchmarks.md` (926 — Indian manager benchmarks and the tax/cost arithmetic). Every figure below
traces to one of those, and each of them traces to a file, a line, a gate report, or a cited paper.

---

## 0. The short answer, before the plan

Three findings, in the order that matters.

**1. The premise is half wrong, and the wrong half is good news.** The plan *does* specify decision-time
AI — A5's tiered monitor (T1 triggered LLM review, T2 deep review), A3 theme mapping, A4 thesis drafting.
It has simply never run: `ops/gates/M5.md:12` — *"No language model ran anywhere in this gate"*; A3 runs on
`StubLLM`; A2's "recommendation" is a mapping table (`analyst/interview/flow.py`). There is no Anthropic
key (B4). So the AI is designed, coded, tested behind a deterministic stub, and unplugged. What is genuinely
*absent* is AI in the **buy/sell/size** decision — the LLM is a verdict-renderer on human-ratified break
conditions, never a ranker or a sizer.

**2. AI is not where your missing XIRR is.** This is the finding I would most like to be wrong about, and
five independent lines of evidence agree:

- **Your own repo already ran the experiment.** The X2 fitted forecast — a PIT-clean expanding-window linear
  model, 9 features, 1.6M matured pairs — returned **−2.71% XIRR / 36.28% max DD** against rules-based
  momentum v2's **+17.13% / 22.06%** on the identical window (`ops/gates/X2-forecast-daily-report.md`).
- **The literature says ~0% for *your specific* strategy.** Every ML asset-pricing paper reporting variable
  importances finds the same three dominant clusters — price trends, liquidity, volatility (Gu-Kelly-Xiu
  §1.5; replicated by Hanauer-Kalsbach across 32 emerging markets including India). Your swing composite
  already harvests cluster 1 **twice** (12-1 momentum, 52-week-high proximity) and cluster 2 once (delivery
  share). ML would be re-finding signals you own.
- **The ceiling arithmetic leaves nothing.** Azevedo-Hoegner-Velikov's **+0.30 net Sharpe** is the honest US
  upper bound — ex-post optimally weighted, long-short, 60 years, 200+ characteristics. Discount for ~40×
  less data, 2–4× the Indian cost floor, no short leg, no microcaps → +0.05 to +0.15. Subtract the cluster
  overlap → **+0.00 to +0.10**. The standard error on annualised Sharpe over your 216 months is **≈0.24**.
  The expected gain is one-fifth of one standard error: *unmeasurable on this sample.*
- **Indian active management, in aggregate, has no stock-selection alpha.** 425 Indian active funds
  (2013–2024): alpha **+3.84%** on market/size/value → **+2.16%** (not significant) adding momentum →
  **+0.07%, t = 0.04** adding profitability. And SPIVA to Dec-2025: **76.3% of large-cap funds lost to
  benchmark over 10 years.**
- **The backtest-to-live haircut is measurable in India, on momentum specifically.** Nifty200 Momentum 30's
  backfilled history implies **+5pp/yr** over Nifty 200. Its **live record since Aug-2020 is +0.60pp/yr.**

**3. Where the money actually is: tax, execution, and measurement honesty — and the amounts are large.**
Your headline 27.91% is a **₹10 lakh, pre-tax, unrailed** number. After tax and measured slippage:

| Book | After-tax XIRR (six-year, ₹1cr floor) | Decade window |
|---|---:|---:|
| ₹10 lakh | **21.8%** | 13.5% |
| ₹1 crore | **19.5%** | 11.4% |
| ₹5 crore | **14.9%** | **7.4% — loses to a ~9.9% TRI benchmark** |

The tax haircut alone is **6.1pp** on the six-year run. **Nothing in the repo reports it.** Two changes
containing no new alpha signal whatsoever — holding-period extension across the LTCG boundary, and
slippage-aware execution scheduling — are worth **+3.2pp at ₹1 crore and +7.5pp at ₹5 crore, after tax.**
For a cross-sectional ML model to merely draw level with them it would have to add **+4.20pp of pre-tax
return** — lifting XIRR from 24.84% to 29.03%, i.e. another quarter of the composite's entire measured edge.

**So the plan below spends AI where AI is strong (reading documents, extracting structure, guarding data
quality, reasoning over evidence for a human) and spends engineering where the rupees are (tax, execution,
railed measurement) — and treats cross-sectional ML as a capped, pre-registered, kill-switched experiment
that must beat a 22.5% after-tax baseline rather than a 19.5% one.**

---

## 1. What AI the plan already specifies, and its exact authority

| ID | Where | Specified as | Has it run? |
|---|---|---|---|
| A5 T1 | `EXECUTION_PLAN.md:228` | Strong model over an evidence bundle → verdict per break condition `INTACT / WEAKENED / BROKEN` + proposed action **within ratified policies** | No — `StubLLM` only |
| A5 T2 | `EXECUTION_PLAN.md:229` | Scheduled deep review → case health report, rotation steering, bench refresh | No |
| A4 | `TASK_GRAPH.yaml` M5.6 | LLM **drafts** falsifiable break conditions; human ratifies | No |
| A3 | M5.5 | LLM maps a theme to a value chain + a **disclosed purity score** | Runs on `StubLLM` |
| A2 | M5.7 | Recommends the ratified policy set | Built as a mapping table, not a model |
| A8 rails | invariant #6 | **A model is forbidden outright** | — |

The authority is deliberately narrow and it is worth preserving: **model → schema validation → policy
validation → deterministic rails → broker.** Three deterministic gates behind one probabilistic step.
M6.4's own acceptance criteria say a proposed action outside ratified policy is *"rejected by code before
reaching rails"* and a malformed response is *"retried then escalated, never interpreted loosely."*

**The one path a model may never take:** `tests/unit/test_rails.py:562` AST-parses every module in
`analyst/rails/` and fails if any callable declares a parameter named `override`, `bypass`, `force`, `skip`,
`disable`, `allow_breach`… and `:591` fails if `analyst/rails/` imports `analyst.llm` or `anthropic`.

---

## 2. Four things that must be fixed before any AI work is meaningful

These are not AI tasks. They are the reason AI work would currently be unmeasurable or unsafe.

### 2.1 The backtests are unrailed — the headline numbers describe a portfolio this system may not hold

Nothing under `backtest/` imports `analyst.rails`. `ReplayEngine._run_session` is literally
`for request in decision.orders: self._broker.place(request)` (`backtest/replay.py:384`). Rails have **no
override** — but they are **not a choke point**. Under the ratified MEDIUM/AGGRESSIVE profile (15% position,
35% sector, 8 holdings, 25% drawdown review — `analyst/interview/flow.py:448`):

- **`MIN_HOLDINGS=8` makes the regime gate structurally unimplementable.** `momentum_v2._park` sells the
  whole basket on a risk-off session; on a 20-name book the 13th sell (8→7) is refused and so is every one
  after. Regime overlays hold **three of the top four slots** on your best table.
- **`MAX_SECTOR=35%`** would bite a sector-blind 20-name equal-weight book — eight names in one sector is a
  40% breach — and **no momentum policy in the repo is sector-aware**.
- **`MAX_ORDER_VALUE`** is derived from the SIP instalment (12 × ₹10k = ₹1.2L) and is simply the wrong scale
  for a lump-sum backtest; it goes from inert to binding as the book compounds.
- **12 of 23 arms breach the 25% drawdown-review trigger**, and the winner clears the decade limit by
  **26 basis points**.

Every rail removes flexibility, so the railed result is very unlikely to be better: **read 27.91% and 17.11%
as upper bounds.** Two claims I will not make until it is re-run: that the *ranking* survives (the arms
`MIN_HOLDINGS` disables occupy three of the top four slots), and that the winner's drawdown margin survives.

### 2.2 The universe is not what the reports imply

The `nifty500` as-of membership screen is a **verified no-op**. There are no constituent snapshots in L1;
`membership_asof('nifty500', 2024-01-01)` returns `None`; the `if members is not None` branch never fires
(`backtest/run.py:639`, `dataplatform/ingest/indices.py:476`). Every sweep figure is actually **"all NSE EQ
above a trailing-365-day median turnover floor"** — wider and more mid-cap than "nifty500", and the sweep
reports do not disclose it. The code fails *safe*, not leaky. But the disclosure is missing and the ₹1 crore
floor's *"10 bp base slippage is a claim rather than a measurement"* (the gate report's own words) matters
much more in that wider universe.

### 2.3 A latent zombie-universe bug on the production path

`exchange_listing.delisting_date` is **NULL in all 10,870 rows**, so `tradeable_on()` returns `True` for all
8,598 securities — including the **2,311 marked DELISTED** — on any date at or after their `first_seen_date`.
`tradeable_on` never consults `security_master.status`. Backtests escape today only by accident:
`backtest/run.py:317` derives listing windows from L1 prints instead, and says so. **The correct production
path is the one with no test coverage.** The docstring at `universe.py:209` reads as if it works — *"What it
never does: drop a delisted security. That is the whole point."* True, and exactly the wrong half to be
confident about: it never drops them, and it never retires them. Wire that into a live scoring pipeline and
a model will rank and size positions in companies that stopped trading years ago, with no error and no flag.

### 2.4 The objective is tax-blind, and the tax effect is 6.1pp

`backtest/accounting.py` has no holding-period tracking and no §111A/§112A rates. A 43-day median hold at
**8.57 turns/yr** means essentially the entire book realises at **20% STCG instead of 12.5% LTCG**. Every
pre-tax point is worth **0.774** after-tax points today, and **0.875** at one turn a year.

Re-ranking the existing 23 arms on after-tax XIRR/DD moves almost nothing — one adjacent swap at ranks 11–12
— because every arm holds under 12 months, so tax is a near-uniform ×0.774 haircut. **The sweep's objective
is therefore internally safe and externally blind: the family of arms that would benefit from the tax code
is not in the sweep at all.** The longest-hold arm swept is "3-month holds", median 31 days.

---

## 3. What the evidence says AI is actually worth, tiered by confidence

Ranked by (evidence strength) × (expected value ÷ implementation risk). Full citations in
`/tmp/polly-reports/ai-alpha-evidence.md` §4 and §9.

### Tier A — bet on it

| # | Intervention | Is it AI? | Evidence | Expected here |
|---|---|---|---|---|
| A1 | **LLM for document extraction, corporate-action normalisation, filings/announcement parsing, data-quality triage** | **Yes — genuinely** | Kim-Muhn-Nikolaev: GPT-4 CoT **60.35%** directional accuracy on anonymised financial statements vs **52.71%** analyst consensus and **60.45%** for a purpose-built ANN; strongest contamination controls in the literature (0.07% firm identification, 58.96% on a clean 2023 slice) | Attacks the **162 open ERROR `ca_reconciliation` flags**, the **1,358,650 `symbol_unresolved`** quarantined rows, and the 22-of-many XBRL concept map. *Failure mode is a wrong field caught by a golden fixture, not a silently overfit signal.* **Zero alpha risk.** |
| A2 | **Cost-aware rebalancing: longer effective holds, no-trade bands, netting before trading** | No — arithmetic | DeMiguel-Martín-Utrera-Nogales-Uppal: monthly turnover **24.09% → 6.71%** (−72.15%) from combining characteristics before trading; marginal costs −65%; net Sharpe ~100% higher; costs *raise* the number of jointly significant characteristics from 6 to 15. Novy-Marx & Velikov: **<50%/month** is the survivability line | You are at **~48%/month — on the line with no margin.** Implied drag **1.6–2.5%/yr**, larger than the entire modal ML uplift |
| A3 | **LTCG holding-period extension** | No — tax arithmetic | §9.2 of the benchmark study, against the repo's own measured decay (`M10-swing-composite-report.md`: excess 0.35% @5 sessions → 3.35% @63, t = 13.3, ~0.2%/week to 125 sessions; power-law fit `0.0834·t^0.8915`) | **+0.98pp @₹10L, +3.02pp @₹1cr, +7.01pp @₹5cr** after tax. Costs 1.84pp of signal decay, buys 5.30pp of headroom at ₹1cr |
| A4 | **Slippage-aware order sizing / multi-session participation caps** | No | Measured ADV from your own lake + standard √-impact. Median pick ADV is **₹4.3cr** at the ₹1cr floor: a ₹5L position is 1.16% of ADV → **28.0bp** one-way vs the model's 10.1bp; a ₹25L position is 5.81% → **62.7bp** | **+1.56pp @₹1cr, +3.51pp @₹5cr.** Grows with the book — this lever buys future capacity |
| A5 | **Match the liquidity floor to the book size** | No — one parameter | Crossover computed at **≈₹3.3 crore**: below it the ₹1cr floor wins, above it the ₹10cr floor | Mostly *negative knowledge*, and worth having: it says the current floor is right up to ₹3.3cr and wrong above |

### Tier B — small, ring-fenced, kill-switched

| # | Intervention | Evidence | Honest expectation |
|---|---|---|---|
| B1 | **Ensemble ML as ONE capped leg among four or five** | Azevedo et al. net MVE Sharpe 1.15 → 1.45; GKX decile Sharpe 1.35 VW / 2.45 EW, roughly doubling OLS-3; Hanauer-Kalsbach "up to 2% p.a. net" in emerging markets via ensembling | **+0.00 to +0.10 Sharpe**, ≈0% incremental return over *this* composite. Budget the **57% haircut** (Azevedo) and **58% post-publication decay** (McLean-Pontiff) *before* starting. Must be measured against the **quality** sleeve, because that is what ML substitutes for (RMW weight falls 59% → 38% when the best ML signal enters) |
| B2 | **Continuous inverse-volatility position scaling** | Barroso & Santa-Clara: momentum Sharpe **0.53 → 0.97**, max DD **−96.69% → −45.20%**, worst month −78.96% → −28.40%, kurtosis 18.24 → 2.68, **at unchanged turnover**, 85 years, replicated in FR/DE/JP/UK. Momentum's realised variance has an **out-of-sample R² of 57.8%** — more than half its risk is predictable, the highest of any factor | **Test once, cheaply — and note the repo disagrees with the literature here.** Your own measurements: regime gate six-year Calmar **1.17 → 0.93**, decade **0.69 → 0.68**, verification window **0.97 → 0.93**; standalone "vol target 15%" 6.39% XIRR. *Three tests, three non-results.* But those were a **binary regime gate** and a **low-vol leg**, not continuous inverse-vol scaling of gross exposure on the composite, which has never been tested. Literature prior is high; local prior is negative; the experiment is cheap. Run it, believe the lake |
| B3 | **CNN on daily OHLCV images** | Jiang-Kelly-Xiu — the most EOD-native ML result that exists: >53% accuracy; VW monthly decile Sharpe 0.5 vs 0.3 for 1-week reversal; **US-trained models transfer to 26 foreign markets, lifting country Sharpe 0.3 → 0.7** | The transfer property is the only credible published answer to India's small-sample problem. But the tradeable VW increment is ~+0.2 Sharpe, the 2.4 EW / 7.2 weekly figures are microcap- and turnover-flattered and disclaimed by the authors, and it is a real computer-vision project. **Not now** |

### Tier C — do not build

| # | Why not |
|---|---|
| **LLM news-sentiment daily trading** | Lopez-Lira & Tang's own numbers: Sharpe decayed **6.54 → 3.68 → 2.33 → 1.22** over 32 months; turnover **~190%/day**; **unprofitable at 20bps round trip. India's STT alone is 20bps.** Plus Sarkar-Vafa: memorisation survives masking; Glasserman-Lin: the distraction effect exceeds look-ahead bias. Dead on arrival in Indian cash equities |
| **ML covariance / sophisticated risk estimation** | Ledoit-Wolf Table 1, and these are *your* rows: at **N=30** nonlinear vs linear shrinkage is 14.16% → 14.08% volatility and **+0.02 Sharpe**; at N=50, **+0.01**. The 15%/12% Sharpe boosts arrive at **N=250/500**. De Nard et al.'s economically meaningful gains are at N=500/1000. DeMiguel-Garlappi-Uppal: none of 14 models consistently beats 1/N, and you would need **~3,000 months for 25 assets**. Use one line of shrinkage; do not build a project |
| **"Virtue of complexity" / high-complexity return models** | Nagel (2025): the flagship result reduces mechanically to volatility-timed momentum and earns **negative** abnormal returns on reversal-injected artificial data — *"it not only fails to learn, but mislearns."* Buncic (2025): expanding-window linear regression with mild shrinkage gets **Sharpe 0.699** vs **0.485** for the most complex model. Take the lesson, not the method |
| **ML market-impact / execution models** | Needs order-level intraday data you do not have. Calibrate a parametric fixed + spread + √-impact model on your own fills |
| **More regime-gate work** | Measured negative three times in this repo. Stop |

---

## 4. What top Indian managers actually do — and what of it you can have

| Layer | 10y CAGR / XIRR | Max DD | Calmar | Source of the return |
|---|---:|---:|---:|---|
| Nifty 50 TRI | ~11.9% | ~38% (2008: −60%) | 0.20–0.25 | Beta |
| Nifty 500 TRI | ~13.2% | — | — | Beta + slight cap tilt |
| Midcap 150 / Smallcap 250 TRI | 17.21% / 15.86% *(since Apr-2005)* | −70%+ | — | **Cap beta — not skill** |
| Top-quartile active MF | benchmark **+2 to +8pp** | — | — | Quality-growth tilt. **After profitability, aggregate alpha is +0.07%, t=0.04** |
| Top-decile PMS (net, TWRR) | **24–36%**; Aequitas ~34.3% XIRR /13y | — | — | Concentration + smallcap + the illiquidity niche. Their own disclosure shows client XIRR from **1.06% to 77.59% on the same strategy** — timing dispersion dominates |
| **Your composite, after tax @₹1cr** | **19.5%** (six-year) / 11.4% (decade) | ~24% | 0.82 / 0.46 | Momentum + 52w-high + delivery share |

**Decomposition of an 8-point gap between a 20% solo book and a 12% Nifty 500 TRI:** roughly **+4–5pp cap
beta**, **+2–4pp the illiquidity niche**, **+1–3pp rules-based factor harvesting**, **−1.5 to −4pp friction**.

> **1 to 3 percentage points is the honest ceiling attributable to better decisions — and almost all of it
> is costs not paid and drawdowns not panic-sold, not better picks.**

Two India-specific facts that should shape the strategy more than any model:

- **From India's own survivorship-adjusted factor library (IIMA), computed directly:** momentum **+12.6%/yr**
  over 20 years, value **+8.1%**, and **size is −2.1% — negative in India, consistently.** Small-cap
  outperformance in India is not a size premium; it is illiquidity and beta.
- **Momentum's worst long-only drawdown is −70.5% with 65 months to recover, and its worst year is 2009,
  not 2008.** The *rebound* kills momentum. That is precisely what a de-risking overlay is for — and
  precisely why B2 deserves one honest test even though the local prior is negative.

**Your structural advantage is capacity, and it is real.** ₹3.7 trillion of Indian small-cap mutual fund
money keeps 83% of holdings inside the top 750 stocks and caps sub-rank-1000 exposure at ~2%; SBI Small Cap
needs 65 days to liquidate half its book. **Below ~₹5 crore you can own the rank-750–2,500 tail where the
alpha empirically lives, and no institution can follow you there.** That advantage is worth more than any
model, and levers A3–A5 are what convert it into kept rupees.

---

## 5. The plan

Five phases. Each phase is gated on the previous one because the ordering is load-bearing, not stylistic:
**you cannot measure an improvement against an unrailed, tax-blind, undisclosed-universe baseline.**

### Phase 0 — Make the measurement honest *(prerequisite; no AI)*

| Task | What | Why now |
|---|---|---|
| **P0.1** | **Rail the backtest.** Insert a rail check between `decide` and `place` in `ReplayEngine._run_session`; build `ProposedOrder` + `Portfolio` from the broker's own book; place only when `assessment.allowed`; journal `RAIL_BLOCK` otherwise. Keep `RailEngine` broker-free. Settle two calibrations *in the task*: `max_order_value_inr` is SIP-derived and wrong for a lump-sum backtest, and the only sector map is the survivorship-biased current-day fixture (`run.py:2466`) | Every downstream number is meaningless without it. Expect the regime arms to break loudly on `MIN_HOLDINGS` — that is a **finding about the strategy**, not a bug in the rail. `momentum_v2._park` should be redesigned to park via the CASH sleeve *down to the floor*, not to zero |
| **P0.2** | **After-tax accounting.** Realised holding-period tracking + §111A/§112A rates in `backtest/accounting.py`; report **after-tax XIRR** beside XIRR in `BacktestResult`; model the trailing-stop/tax interaction explicitly | The repo currently cannot see a 6.1pp effect |
| **P0.3** | **Disclose the universe.** Make every sweep report state plainly that the index-membership screen was a no-op and the universe was "all NSE EQ above the floor"; report whether membership data was present, as `UniverseParameters`' own docstring already promises | An undisclosed universe invalidates the benchmark comparison |
| **P0.4** | **Fix the zombie universe.** Populate `exchange_listing.delisting_date`; make `tradeable_on` consult status or L1 prints; add the missing test for the production path | A live scoring path would silently trade delisted names |
| **P0.5** | **Re-run the sweep, railed and after-tax, and publish `M12-strategy-verdict.md`** — the walk-forward gate that has never been run. Rank on **after-tax XIRR / max drawdown**, both floors, three windows, never averaged, selection-window winner named *before* the verification window is read | This becomes the real baseline. Note the repo's own walk-forward Spearman between selection and verification orderings of just 23 hand-specified arms is **0.372** — weak. That number is the single best argument for humility about any model with more free parameters |

### Phase 1 — The levers that dominate AI on expected value *(no AI)*

| Task | What | Expected (after tax, ₹1cr) |
|---|---|---|
| **P1.1** | **Long-hold arms.** Add to `backtest.sweep.ARMS`: `max_hold=min_hold=250` sessions with the trailing stop on; the same with it off (isolates the stop/tax interaction); and an **LTCG-aware exit** arm where a FIFO lot within 45 sessions of 12 months is held to the boundary unless the stop fires. ~15 min of compute on the existing one-pass lake | **+3.02pp** (+7.01pp at ₹5cr) |
| **P1.2** | **Execution scheduler.** Per-name ADV in the decision path; participation caps; split large clips over 2–3 sessions | **+1.56pp** (+3.51pp at ₹5cr) |
| **P1.3** | **Floor/book matching.** Ship the **≈₹3.3 crore** crossover into the policy config | Prevents a −2.9pp error at ₹1cr |
| **P1.4** | **Measure cash drag first, then fix it.** `backtest/run.py` cannot currently report average cash balance — a one-hour change with high information value. Stop-outs fire intra-cycle and cash waits up to 10 sessions | +0.3 to +0.8pp, *estimated, not measured* |
| **P1.5** | **One honest test of continuous inverse-vol scaling** on the composite (σ_target/σ̂ from trailing 21–63 daily returns, parameter-free, never an estimated weight). Pre-register it; accept a negative result | Literature says Calmar 0.69 → 0.82–0.90; this lake has said no three times to adjacent ideas |
| **P1.6** | **Add a quality/profitability leg.** The one GKX cluster the composite lacks, and the one Azevedo shows ML mostly substitutes for. Buildable *today* from the dense 13 XBRL concepts: earnings yield, revenue/PAT growth, margins, effective tax rate, other-income share, dilution. **Not** ROE or P/B — book equity covers only ~21% of filings and skews large; use them masked, never ranked | By the 1/√K result this *lowers* turnover |

### Phase 2 — AI where AI is strongest: reading documents *(the real AI phase)*

| Task | What | Governing constraint |
|---|---|---|
| **P2.1** | **Start the clocks that decay.** Begin monthly capture of `nifty_index_constituents` **now** (parser M3.9 exists, tested, `robots.txt` allows) and of `nse_announcements` / `bse_announcements` (parser M3.8 exists, fixtures exist, feeds are timestamped at source — natural PIT). Backfill is the hard half; **forward accumulation is trivial and every month of delay is history you can never have** | These two gaps get strictly worse with time. #1 unblocks PIT index membership, benchmark-relative labels, and the *only* sector map — without which "sector-neutral" is a look-ahead, not an approximation |
| **P2.2** | **LLM-assisted CA reconciliation.** Point a model at the **162 open ERROR** and 241 open WARN `ca_reconciliation` flags: read the source filing, propose the corrected action, and emit a *proposal* that a human or a golden fixture validates. Never auto-apply | Highest-value AI task in the plan. A wrong field is caught by `tests/golden/test_golden_ca.py`, which already includes `test_golden_literals_are_direction_sensitive` |
| **P2.3** | **LLM-assisted identity recovery.** Attack the **1,358,650 `symbol_unresolved`** quarantined rows by extending `symbol_history` backward from `nse_symbol_changes`. This recovers 3 years of delivery-% history **with no new fetch campaign** — the bytes are already in L0 | Cheapest item on the data-gap list. Also removes a documented survivorship-correlated bias: names with missing delivery skew to those later renamed or delisted (`backtest/forecast.py:166`) |
| **P2.4** | **Extend the XBRL concept map** beyond 22 concepts toward balance-sheet and cash-flow items, with a golden fixture per taxonomy era (Ind-AS / Banking / Non-Ind-AS). LLM-assisted mapping, deterministic extraction | Unlocks quality, accruals and leverage factor families. High effort, real payoff, **and note the Screener path is a dead end for training data at any effort** — restated data is physically quarantined by invariant #8 and `dataplatform/store/restated.py` |
| **P2.5** | **Live T1/T2 fire drill** — M6.8, the only `NEEDS_SECRET` task in the graph. Needs an Anthropic key. Assess verdict *quality*, not plumbing, and record real cost per decision tier | The plan's designed AI has never met a real model. `accounting/tokens.py` already meters it |

### Phase 3 — The bridge: let a decision reach a portfolio at all

**Path B — the entire analyst — has zero production constructions outside `tests/`.** The registered
scheduler jobs are ingestion only. Your System 2 has never made a decision outside a test, and
`decision_journal`, `thesis` and `policy_set` hold 0 rows.

| Task | What | Governing invariant |
|---|---|---|
| **P3.1** | **Extract the conviction→weight seam.** One pure function `weights_from_scores(scores) -> weights` in `backtest/sip.py`, returning **exactly today's equal weights by default**, replacing six duplicated `_equal_weights` copies. A provable no-op: the railed digests from P0.1 must hold | Decimal-not-float; weights sum to 1 ± 0.0001 (`sip.py:297`); ISIN tie-breaks for determinism |
| **P3.2** | **One shared score type** carrying `isin`, score as `Decimal`, raw `price`, `knowable_date`, and a provenance triple (`model_id`, `feature_set_version`, `prompt_digest`). `ForecastRecord` is 90% of this already and is the right template because its score is **cardinal, in return units, and comparable against a cost bar** | #7 — `knowable_date` lets `PitContext.admit` **raise** structurally; #2 — ISIN only |
| **P3.3** | **The live `Policy` driver — and it is a driver, not a translator.** `backtest.replay.Policy` was designed as the one decision surface for both worlds; its own docstring says so. Per session: run the data-red and auth interlocks (journal `SKIPPED_DATA_RED` / `AUTH_REQUIRED` on refusal); build `SessionContext` with `FrozenClock(today)`; call **the same `Policy` object the sweep replays**; pass every order through the **same** rail check P0.1 installed; hand allowed orders to `StagingCoordinator.stage` (kill switch first); append every entry through a real `Journal`. Register as a scheduler job beside `EOD_PIPELINE` | **#5 — inject a `Broker` protocol, never a concrete broker.** Paper and real differ by one injected object and nothing else. **Two translated paths are still two paths** |
| **P3.4** | **Journal the model.** No migration required: `model` column, `payload: Mapping[str,str]` for prompt digest / feature-set version / coefficients, `evidence_snapshot_ref` + `EvidenceItem`s for the scored feature vector, `MeteredLLM.journal_fields()` for tokens. Optionally add a `MODEL`/`SIGNAL` member to `EvidenceKind` (in no DDL CHECK). **Do not widen `Actor` or `Decision`** — those are DDL-constrained | #9 every decision journaled; #12 append-only, so a bad model output is permanently on the record |

### Phase 4 — The capped ML experiment, pre-registered

Only after Phases 0–1 have shipped, so it competes against a **~22.5% after-tax** baseline.

| Task | What | Kill criteria, pre-committed |
|---|---|---|
| **P4.1** | **Label and feature discipline.** ~2.5M instrument-days at the ₹1cr floor (1.5–2.0M once fundamentals are required, 2018-05+). Join fundamentals by **`filing_date` lagged one session** — a date is not a timestamp, and a post-close filing is knowable D+1. Carry `lag_days` as an explicit feature (a long lag is informative about the *filer*). Label on **calendar-anchored forward dates with a staleness tolerance**, never row offsets, or a suspension gap becomes a fake N-day return | **The label layer is where survivorship sneaks back in.** At 12m, **114,533 rows (3.2%)** are names that died mid-horizon and are systematically the worst outcomes. `shift(-H).dropna()` deletes exactly those → survivorship-free features with a survivorship-biased label set, *the worse combination because it looks clean*. Choose and record: terminal-return with `truncated=True`, or a stated delisting haircut. **Never drop, never impute zero** |
| **P4.2** | **The model as ONE capped leg** behind the existing `*Data` protocol, following `_L1ForecastData` as the worked example. Reuse `_mature` verbatim: a training pair enters the fit only once `t + horizon <= session`. Pure-Python linear algebra accumulated in fixed order — no numpy, no dict-order dependence, no unseeded randomness. Winsorize: this panel destroys any un-winsorized cross-sectional statistic | Evaluate on the **Deflated Sharpe Ratio** with trials honestly counted — including the 46 arm-runs already swept. Cap turnover **below 50%/month**. Exclude the smallest quintile before believing anything. Measure against the **quality sleeve** from P1.6, not against cash. **Kill it if it does not beat the composite net of cost and tax on a pre-registered out-of-sample window** |
| **P4.3** | **Deterministic replay for a model in the decision path.** Cache the model response against the **content-addressed evidence-bundle hash** (M6.3: *"reproducible from its content hash and contains exactly what was sent"*, *"a bundle for date D contains nothing knowable after D"*); treat the response as a **stored input**; make a replay-time cache **miss a loud failure**, never a live call | This is the mechanism that lets a nondeterministic component live inside a byte-identically replayable path. §7 already exempts T1/T2 from replay and pays for it in paper-mode-only evaluation; this is strictly better. For a *fitted* model, frozen seeded weights are already sufficient |

---

## 6. Expected value, stated honestly

Six-year window, ₹1 crore book, ₹1cr floor, after tax. Baseline as reported: 27.91% pre-tax → **19.47%
after tax** (and this baseline is itself an unrailed upper bound pending P0.5).

| Phase | Intervention | Δ after-tax XIRR | Δ Calmar | Effort | Confidence |
|---|---|---:|---:|---|---|
| P1.1 | LTCG holding-period extension | **+3.02pp** | + | ~2 days | High on the tax arithmetic; Medium on the extrapolated decay |
| P1.2 | Execution scheduling | **+1.56pp** | + | Medium | High (measured ADV, standard √-impact) |
| P1.3 | Floor/book matching | prevents −2.88pp | — | Trivial | High |
| P1.4 | Cash-drag reduction | +0.3 to +0.8pp | — | Low | **Low — measure first** |
| P1.5 | Inverse-vol scaling | ~0 to +1pp | +0.13 to +0.21 *if it works* | Days | Literature High, **local prior negative** |
| P1.6 | Quality leg | unquantified, lowers turnover | + | Medium | Medium-High |
| P2.2–2.4 | LLM data quality / CA / identity / XBRL | **indirect — protects every number above** | — | Medium-High | High that it works; unquantified in pp |
| **P4.2** | **Cross-sectional ML leg** | **−2.5 to +1.5pp, modal ≈ 0** | ≈ +0.03 | **Months** | **Low** |

**Phases 0–1 total: ≈ +3.2 to +5pp after tax at ₹1 crore, +7.5pp at ₹5 crore**, from arithmetic that cannot
be very wrong. **Phase 4 total: modally zero**, unmeasurable at this sample size, for months of work.

And the honest downside of Phase 4, spelled out: **ML plausibly underperforms the rules baseline by 1–3%
annualised net.** Six mechanisms — you cannot statistically distinguish success from failure at n=216
months; Harvey-Liu's haircut is worst exactly where you live (>50% when Sharpe < 0.4); complexity gains may
be mechanical rather than learned (Nagel); estimation error swamps optimisation at your N; the model wants
turnover and **turnover is taxed at 20% while a turnover reduction is taxed at nothing**; and complexity
adds failure modes a rank-normalised screen cannot have — a stale feature, a leaked PIT boundary, a retrain
that shifts the whole book overnight.

---

## 7. Governance: what needs the owner, and what needs §12

| # | Decision | Route |
|---|---|---|
| G1 | **Change the ranking objective from XIRR/max-DD to *after-tax* XIRR/max-DD** | Owner decision — it amends the owner decision of 2026-09-07 |
| G2 | **Accept that railing the backtest invalidates every published gate number** and re-baseline | Owner decision; P0.1 must be its own task with its own gate report, never a side effect |
| G3 | **Extend the holding period past 12 months for tax** — a strategy-design change, and it interacts with the 25% trailing stop | Owner decision. **At ₹10 lakh, do not change anything on this evidence alone** — headroom 2.96pp vs 1.84pp decay is inside the extrapolation error |
| G4 | **Wire any backtested policy into A3 candidate ranking or A6 steering** | `HUMAN_GATE`. §5.2 policy surface; `AGENTIC_CONTEXT.md:83` reserves policy ratification to the human. Today **no A-module task depends on any M9–M12 task** — the strategy search terminates in a markdown file no module reads |
| G5 | **Let a fitted model influence a mechanical decision** | Per the ratified **D6** precedent: a signal that trades mechanically must be **PIT-vintaged and backtestable**; otherwise it may only be *evidence shown to a model downstream of ratified break conditions and rails*. A frozen, seeded, PIT-trained model **is** replicable, so it qualifies — but this must be stated out loud, because *"any plan that quietly assumes otherwise is proposing a §1 amendment without saying so"* |
| G6 | **Anthropic API key** for M6.8 / Phase 2 | `NEEDS_SECRET`. Env var only, never a file, never argv. Blocked on B4 |
| G7 | **Compliance note** | An LLM-driven non-replicable strategy would be a **Black Box** algo requiring Research Analyst registration *if offered to others*. Single-tenant own-money use defers this (§11), and Phases 0–2 keep the mechanical path fully replicable. Also inherited: market orders are not permitted for algo orders, and the daily forced logout |

**None of Phases 0–3 requires an EXECUTION_PLAN §1 amendment.** Phase 4 requires only that G5 be stated
explicitly and ratified. If the owner ever wants a *headline-driven* or *tone-driven* signal to trade
mechanically, that **does** require a §12 amendment and it should be raised as one, not smuggled.

---

## 8. What I recommend

1. **Ratify G1 and G2, and do Phase 0.** Without it every number in this document, including mine, is
   provisional. It also produces `M12-strategy-verdict.md`, the walk-forward gate that has never run.
2. **Do Phase 1.** It is worth more than the entire AI programme, it is arithmetic rather than prediction,
   and it takes days rather than months.
3. **Start P2.1 today, in parallel with everything.** Index-constituent and announcement history are the two
   gaps that get strictly worse every month you wait. Forward accumulation costs nothing.
4. **Then do the rest of Phase 2 — this is the real AI in this plan**, and it is aimed at document
   extraction and data integrity, where LLMs are strong and where a mistake is caught by a golden fixture
   rather than discovered in a drawdown.
5. **Then Phase 3**, because until the live `Policy` driver exists, *nothing* — model or rule — can reach
   your portfolio. Your best strategy currently cannot be traded by your own system.
6. **Treat Phase 4 as an experiment with a pre-committed kill switch**, competing against a ~22.5%
   after-tax baseline. If it cannot beat that, it has told you something valuable and cost you one arm.

> **The one-sentence version.** The AI you want in the buy/sell decision is worth approximately nothing on
> top of the composite you already have, because your composite already harvests the price-trend and
> liquidity signals every ML paper keeps rediscovering; the AI that *is* worth building reads documents,
> fixes your corporate actions and recovers your identity history; and the 3–7 percentage points of XIRR you
> are actually missing are sitting in the tax code, the execution schedule, and four unrailed, tax-blind,
> undisclosed-universe backtest reports.

---

## Appendix — full evidence

| Report | Lines | Covers |
|---|---:|---|
| `/tmp/polly-reports/plan-ai-gaps.md` | 1163 | Every AI mention in the plan, categorised; A1–A9 authority; the real baseline numbers per window and floor; the X2→analyst disconnect; nine invariants with forbid/require; TASK_GRAPH schema verbatim; §12 amendment procedure |
| `/tmp/polly-reports/decision-path.md` | 691 | The two disjoint decision paths hop by hop; score and sizing seams as real types; all seven rails and the no-bypass proof; the unrailed-numbers analysis; journal schema; determinism tests; `make check` wiring; five ranked insertion points |
| `/tmp/polly-reports/data-inventory.md` | 514 | L0/L1/L2 inventory with exact counts; survivorship handling; the 22 XBRL concepts and what is and is not computable; label options with matured-row counts; five ranked data gaps; the zombie-universe trap; the filing-lag PIT analysis |
| `/tmp/polly-reports/ai-alpha-evidence.md` | 913 | GKX / Chen-Pelger-Zhu / autoencoder / Kelly-Malamud-Zhou with primary-source tables; LLM evidence and its criticisms; the decay and haircut literature; six ranked applications; the uplift envelope; the Calmar section; what survives on daily bars; §8 on this specific composite |
| `/tmp/polly-reports/xirr-benchmarks.md` | 926 | Indian TRI benchmarks; SPIVA base rates; PMS/AIF top-decile; factor attribution and the IIMA premia; momentum backfill-vs-live; the full cost card; post-tax XIRR by book size; the LTCG break-even; eight ranked decision-quality levers |

*Prepared by polly (orchestrator) from five delegated read-only investigations. No product code was read,
written or changed in producing this document. Every implementation task above is unstarted and unassigned.*
