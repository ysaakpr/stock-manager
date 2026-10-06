# DATA CONTRACT AUDIT — replay/backtest, costs, rails, analyst pipeline

Repo: `/home/ubuntu/stock-manager` @ `c9f0ed5`. Read-only investigation; nothing in the repo was
edited. All claims carry `file:line`. Where a module is a stub, unwired, or absent, it says so.

**Headline:** the engine's *contract* is largely sound and 20-year-shaped (dated rate card, PIT
guard, survivorship-safe listing windows, Decimal money). What is missing is **data and three
hard-coded era assumptions**. Today a 20-year run cannot start: the lake begins **2016-09-01**, the
rate card begins **2017-07-01** and *raises* before it, settlement is hard-coded T+1, and corporate
actions are never applied to the book during a walk.

---

## 1. INPUT CONTRACT of the replay/backtest engine

### 1.1 The engine itself reads no data

`ReplayEngine` (`backtest/replay.py:293-429`) has no store, no query handle, no dataset. It drives a
`Policy` and a `ReplayBroker`. All data enters through two injected seams: the policy's own data
source, and the broker's `SessionMarket`.

The only thing the engine hands the policy is a scope, not a reader:

```python
# backtest/replay.py:104-130
@dataclass(frozen=True, slots=True)
class SessionContext:
    session: date
    pit: PitContext
    broker: Broker
    clock: Clock

    def __post_init__(self) -> None:
        if self.pit.as_of != self.session:
            raise ReplayError(...)
```

`PitContext` (`dataplatform/query/pit.py:109-177`) exposes exactly one method — `admit(dataset)` —
and holds no connection. **A policy cannot read anything through `ctx.pit`; it can only prove that
what it already read was knowable.**

```python
# dataplatform/query/pit.py:70-106
@dataclass(frozen=True, slots=True)
class Dataset[R]:
    name: str
    records: tuple[R, ...]
    knowable_date: KnowableDate[R] | None = None
```

`admit` raises `PitError` on: no declared extractor (`pit.py:143-150`), a per-record `None`
(`pit.py:154-160`), or `knowable > as_of` (`pit.py:161-169`). It raises, never filters.

What the policy returns:

```python
# backtest/replay.py:133-152
@dataclass(frozen=True, slots=True)
class SessionDecision:
    evidence: EvidenceBundle  # REQUIRED — no default
    orders: tuple[OrderRequest, ...] = ()
    entries: tuple[JournalEntry, ...] = ()
```

Engine-side validation (`replay.py:432-452`): `evidence.trading_date` must equal the session; every
`entry.trading_date` must equal the session. Sessions must be strictly increasing
(`replay.py:330-335`).

Output:

```python
# backtest/replay.py:175-190
@dataclass(frozen=True, slots=True)
class BookSnapshot:
    cash: Decimal
    holdings: tuple[dict[str, str], ...]
    positions: tuple[dict[str, str], ...]
    ledger: tuple[dict[str, str], ...]


# backtest/replay.py:250-265
@dataclass(frozen=True, slots=True)
class ReplayResult:
    journal: tuple[JournalEntry, ...]
    book: BookSnapshot
```

### 1.2 The real datasets — read by `backtest/run.py`, not by the engine

Every actual lake read in a production backtest lives in `backtest/run.py`. This is the true input
contract.

| Dataset | Layer | Read by | Grain | Columns consumed |
|---|---|---|---|---|
| `prices_raw` | L1 Parquet, `date=` partitioned | `_L1Reader` `run.py:244-434` | (isin, exchange, series, trade_date) | `isin, exchange, series, trade_date, open, close, total_traded_qty, total_traded_value, deliv_pct` |
| `prices_adjusted` | L2 Parquet, `isin=` partitioned | `_AdjustedCloseSource` `run.py:473-543` via `QueryService.cross_section` | (isin, exchange, trade_date) | `adj_close` (only) |
| `index_constituents` | L1 Parquet | `_InvestableUniverse.members_asof` `run.py:619-626` -> `membership_asof` `dataplatform/ingest/indices.py:476-495` | (index_slug, as_of) | `members` (ISIN set) |
| `benchmark_tri` | L1 Parquet | `_resolve_benchmark` `run.py:1150` -> `read_tri_series` `indices.py:998-1031` | (index_slug, as_of) | TRI level |
| `pit_fundamentals` | L1 Parquet, `date=<filing_date>` | `_L1FundamentalsData` `run.py:3325-3412` | (isin, filing_id, concept, segment) | `isin, period_start, period_end, filing_date, filing_id, nature, concept, segment, value` |
| static sector CSVs | **`tests/fixtures/`** | `_load_static_sector_map` `run.py:2466-2490` | (isin) | `ISIN Code, Industry` |

> **See the ADDENDUM (Task B / Task C) at the end of this file** — measured ground truth shows
> `index_constituents` and `benchmark_tri` **do not exist on disk**, so rows 3 and 4 are dead code
> paths that silently return `None`, and row 6 covers only **42 ISINs**.

**Every L1 read is pinned to one scope** (`run.py:268`):

```python
_SCOPE = "exchange = 'NSE' AND series = 'EQ'"
```

Crucially, **the trading calendar is derived from the lake itself**, not from
`dataplatform/ingest/calendar.py`:

```python
# backtest/run.py:300-310
def all_sessions(self) -> tuple[date, ...]:
    """Every distinct NSE trading session in the store, ascending — the NSE calendar."""
```

And so are listing windows — no identity master involved:

```python
# backtest/run.py:312-339
def listing_windows(self) -> tuple[ListingWindow, ...]:
    """One tradeable window per ISIN, from its first to its last observed L1 session.
    ... Derived from L1 because no identity master is loaded here; the production
    path reads ``store_listing_calendar``."""
```

### 1.3 Query-layer models (verbatim — these define the contract)

```python
# dataplatform/query/shapes.py:44-73
class AdjustedPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str = Field(pattern=ISIN_PATTERN)
    trade_date: date
    exchange: Exchange = Field(description="the exchange whose bar this row carries")
    primary: Exchange = Field(description="the ISIN's primary exchange for this query")
    fell_back: bool = Field(...)
    adj_open: Decimal = Field(gt=0)
    adj_high: Decimal = Field(gt=0)
    adj_low: Decimal = Field(gt=0)
    adj_close: Decimal = Field(gt=0, description="split/bonus-adjusted close; no dividend effect")
    adj_volume: Decimal = Field(ge=0, description="total_traded_qty x cumulative qty factor")
    tr_close: Decimal = Field(gt=0, description="total-return close: adj_close with dividends in")
    cum_price_factor: Decimal = Field(gt=0)
    cum_qty_factor: Decimal = Field(gt=0)
```

All fields required except the request bounds:

```python
# dataplatform/query/shapes.py:76-102
class AdjustedSeriesRequest(BaseModel):
    isin: str = Field(pattern=ISIN_PATTERN)
    start: date | None = Field(default=None, description="inclusive lower bound; None = open")
    end: date | None = Field(default=None, description="inclusive upper bound; None = open")
    primary: Exchange | None = Field(default=None, ...)

# dataplatform/query/shapes.py:122-136
class CrossSectionRequest(BaseModel):
    trade_date: date
    primary_by_isin: dict[str, Exchange] | None = Field(default=None, ...)
```

Responses: `AdjustedSeries(isin, primary, points, first|None, last|None)` (`shapes.py:105-119`);
`CrossSection(trade_date, rows)` with rows in ISIN order (`shapes.py:139-155`).

### 1.4 Policy record models (the per-strategy contract)

```python
# backtest/policies/naive_momentum.py:58-86
@dataclass(frozen=True, slots=True)
class MomentumRecord:
    isin: str
    momentum: Decimal  # runtime-checked: must be Decimal
    price: Decimal  # runtime-checked: Decimal, > 0
    knowable_date: date
```

Others, all required, all `Decimal`:

- `MomentumV2Record` — `isin, momentum_0_12, momentum_12_1, price, volatility, knowable_date`
  (`momentum_v2.py:88-131`); plus `RegimeReading(index_level, moving_average, knowable_date)`
  (`momentum_v2.py:134-160`)
- `SectorRotationRecord` — momentum record + `sector: str` (`sector_rotation.py:75-111`)
- `FundamentalsRecord` — `isin, earnings_yield, earnings_growth|None, roe|None, price,
  knowable_date, filing_date` (`fundamentals_value.py:80-108`)
- `SwingRecord` — `isin, high_proximity, delivery_share, momentum_12_1, volatility, price,
  knowable_date` + five M12.1 legs with neutral defaults (`swing_composite.py:115-160`)

Parameter objects, all a-priori and stated: `MomentumParameters(top_n=20,
buy_budget_fraction=0.98, sleeve=TACTICAL)` (`naive_momentum.py:88-114`); `MomentumV2Parameters`
(`momentum_v2.py:163-251`); `SectorRotationParameters(top_k=5, top_n=20, ...)`
(`sector_rotation.py:130-166`); `FundamentalsValueParameters(signal, top_n=20, sell_band,
max_staleness_days=200, ...)` (`fundamentals_value.py:111-145`); `SwingCompositeParameters`
(`swing_composite.py:190-232`).

### 1.5 Broker-side market contract

```python
# execution/sim_broker.py:103-128
@dataclass(frozen=True, slots=True)
class ReferenceBar:
    isin: str
    session: date
    exchange: Exchange
    open: Decimal
    vwap: Decimal
    traded_value: Decimal  # must be > 0 — "a zero-liquidity session cannot fill"


# execution/sim_broker.py:131-142
class SessionMarket(Protocol):
    def next_session(self, after: date) -> date: ...
    def reference_bar(self, isin: str, session: date) -> ReferenceBar: ...
```

Supplied by `_L1Market` (`run.py:440-462`) over `reference_bars_on` (`run.py:358-395`), which
requires `open > 0 AND total_traded_qty > 0 AND total_traded_value > 0`.

Broker value objects (`execution/broker.py`): `OrderRequest` `:142-172`, `Fill` `:175-210`,
`Order` `:213-228`, `Position` `:231-244`, `Holding` `:247-254`, `LedgerEntry` `:257-272`,
`Margins` `:275-288`, `Broker` protocol `:294-338`.

---

## 2. COST MODEL inputs — and time-variance

### 2.1 Answer, plainly

**Yes, the cost model is fully time-varying — by design and by construction.** Rates are keyed on
`trade_date`, read from a dated YAML card, and a date the card does not cover **raises** rather
than borrowing a later card.

```python
# execution/costs/model.py:325-339
def schedule_for(self, trade_date: date) -> Schedule:
    in_force = [s for s in self.schedules if s.effective_from <= trade_date]
    if not in_force:
        earliest = self.schedules[0].effective_from
        raise NoRateScheduleError(
            f"no rate schedule covers {trade_date.isoformat()}; the card starts at "
            f"{earliest.isoformat()}. Add a schedule to rates.yaml rather than pricing the "
            f"trade with a later one."
        )
    return in_force[-1]
```

The regime *shape* — not just the rate — is data. Stamp duty changed from state-wise-both-sides to
uniform-buy-only in 2020; that is encoded, not branched:

```python
# execution/costs/model.py:211-233
class StampDuty(_Frozen):
    regime: StampDutyRegime  # STATE | UNIFORM
    sides: list[Side] = Field(min_length=1)
    uniform_rate: Rate | None = None
    state_rates: dict[str, Rate] = Field(default_factory=dict)
```

Likewise the GST base is per-schedule (`GstRule.applies_to`, `model.py:204-208`).

```python
# execution/costs/model.py:276-293
class Schedule(_Frozen):
    id: str
    effective_from: date
    label: str
    provenance: Provenance
    sources_read_on: date
    sources: list[str] = Field(min_length=1)
    notes: str

    brokerage_per_order: Rate
    stt: SttRates
    exchange_transaction: dict[Exchange, Rate] = Field(min_length=1)
    sebi_turnover_rate: Rate
    gst: GstRule
    stamp_duty: StampDuty
    dp_charge: DpCharge
```

### 2.2 Every parameter, its type, its source

| Parameter | Type | Source today |
|---|---|---|
| `brokerage_per_order` | `Rate` (Decimal >= 0) | `rates.yaml` per schedule |
| `stt.buy_rate` / `sell_rate` | `Rate` | `rates.yaml` |
| `exchange_transaction[Exchange]` | `dict[Exchange, Rate]`, min 1 | `rates.yaml` |
| `sebi_turnover_rate` | `Rate` | `rates.yaml` |
| `gst.rate`, `gst.applies_to` | `Rate`, `list[GstComponent]` | `rates.yaml` |
| `stamp_duty.regime/sides/uniform_rate/state_rates` | see above | `rates.yaml` |
| `dp_charge.depository_fee/broker_fee/gst_applies` | `Rate, Rate, bool` | `rates.yaml` |
| `CostModel.account_state` | `str \| None` | **hard-coded** `_ACCOUNT_STATE = "MH"` at `backtest/run.py:149`, passed at `run.py:1351` |
| trade `isin/trade_date/side/quantity/price/exchange` | `Trade` `model.py:376-401` | per fill from `SimBroker._fill` `sim_broker.py:439-447` |

Floats are structurally impossible: `_decimal_from_text` (`model.py:158-171`) rejects a bare YAML
number; `_require_decimal` (`model.py:357-364`) rejects a float price. Rounding is contract-note
shaped: `_to_paisa` (`model.py:141-143`), `_to_rupee` half-up (`model.py:146-152`). DP charge is
levied once per scrip per day via `charge_all` (`model.py:460-475`).

### 2.3 The 20-year blocker

`execution/costs/rates.yaml` holds **four** schedules; the earliest is:

```yaml
# execution/costs/rates.yaml:33-34
  - id: gst-era-state-stamp
    effective_from: "2017-07-01"
```

Then `2020-07-01` (`rates.yaml:79`), `2024-10-01` (`rates.yaml:115`), `2025-04-01`
(`rates.yaml:150`).

**A 20-year run (~2006-2026) raises `NoRateScheduleError` on the first fill before 2017-07-01.**
The machinery to fix this is a YAML append, not a code change — but the regimes are absent: the
service-tax era (pre-GST), the STT cuts (0.125% -> 0.1% delivery, 2013), the 2006-08 STT levels, and
the pre-2017 exchange/SEBI schedules are all missing.

Two further gaps inside the existing pre-2020 schedule:

```yaml
# execution/costs/rates.yaml:107-110
      state_rates:
        KA: "0.0001"
        MH: "0.0001"
```

Only two states; the schedule's own notes (`rates.yaml:53-58`) state no capped state (Telangana,
Haryana) is encoded and an unlisted state raises. And both pre-2024 schedules are
`provenance: reconstructed` (`rates.yaml:36`, `rates.yaml:81`), which is a stated accuracy caveat on
any long-horizon result.

---

## 3. RAILS / risk limits — and their data dependencies

### 3.1 The rails are **not wired into any backtest**

```
$ grep -rn 'analyst.rails\|RailEngine\|check_order' backtest/ execution/
(no matches)
```

Rails have exactly two production callers, both in the live/paper analyst path:
`analyst/cash/manager.py:265,355` and `analyst/rotation/engine.py:309`. **A 20-year qualification
run through `backtest/run.py` evaluates zero rails.** Every policy in `backtest/policies/` places
orders straight into `SimBroker`.

### 3.2 The rail inventory and what each needs

| Rail (`analyst/rails/policies.py:69-97`) | Evaluated in | Data needed |
|---|---|---|
| `MAX_ORDER_VALUE` | `engine.py:165-173` | order price x qty |
| `MAX_ORDER_PCT` | `engine.py:174-183` | order value / `Portfolio.total_value` |
| `MAX_POSITION` | `engine.py:187-210` | resulting lot value / book value (buys only) |
| `MAX_SECTOR` | `engine.py:213-229` | **`ProposedOrder.sector` + `Lot.sector` — a sector classification per ISIN** |
| `MIN_HOLDINGS` | `engine.py:232-254` | holding count before/after |
| `CROSS_CASE_CONCENTRATION` | `engine.py:257-273` | `HouseholdExposure` — value per ISIN across *all* cases + household total |
| `DRAWDOWN_REVIEW` | `engine.py:276-307` | a chronological `Sequence[Decimal]` of case value |

Reference data actually required:

- **Sector/industry per ISIN** — mandatory (`Lot.__post_init__` raises on a blank sector,
  `policies.py:121-122`; `ProposedOrder.__post_init__` likewise, `policies.py:213-214`).
  **There is no sector data source in the platform.** The only mapping is `_load_static_sector_map`
  reading `tests/fixtures/nifty_indices/constituents` (`run.py:167`, `run.py:2466-2490`), whose own
  docstring says it is *"a static current-day map applied backward, which is survivorship-biased"*.
  See the Task C addendum: it covers **42 ISINs**.
- **ADTV / liquidity** — **no rail consumes it.** Liquidity lives only in the universe screen
  (`_InvestableUniverse.liquid_asof`, `run.py:628-637`), not in the rails.
- **Market cap** — no rail consumes it. Market cap appears only inside fundamentals metrics.
- **Benchmark** — no rail consumes it. `assess_drawdown` is absolute, not relative to a benchmark.
- **Prices** — the rails do not fetch prices; they take `ProposedOrder.price` and `Lot.price` as
  marks, both `Decimal`-enforced (`policies.py:52-66`).

Value objects the rails read: `Lot(isin, sector, quantity, price)` `policies.py:100-130`;
`Portfolio(case_id, lots, cash)` `policies.py:133-188`; `ProposedOrder(request, price, sector)`
`policies.py:191-234`; `HouseholdExposure(isin, household_value_in_isin, household_total_value)`
`policies.py:237-258`; verdicts `RailAssessment` `policies.py:280-307` and `DrawdownStatus`
`policies.py:310-329`.

The caps themselves are ratified data, not code:

```python
# analyst/cases/policies.py:204-233
class RiskRails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_position_pct: Percent = Field(gt=0, le=100, ...)
    max_sector_pct: Percent = Field(gt=0, le=100, ...)
    min_holdings: int = Field(ge=1, ...)
    drawdown_review_pct: Percent = Field(gt=0, le=100, ...)
    max_order_value_inr: Money = Field(gt=0, ...)
    max_order_pct_of_case: Percent = Field(gt=0, le=100, ...)
```

There is no override parameter anywhere in the package — asserted by grep test at
`tests/unit/test_rails.py`. `RailEngine.guard_order` (`engine.py:336-384`) journals a `RAIL_BLOCK`
and never places; `review_drawdown` (`engine.py:386-432`) journals an `ESCALATE` with
`FORCED_REVIEW_EVENT` (`engine.py:66`).

---

## 4. THESIS / MAPPER pipeline — current state

**The mapper is an LLM call with no data source and no production caller.**

`map_theme(llm, theme, model, universe, ...)` (`analyst/mapper/engine.py:420-482`) asks an `LLM`
for a value chain and candidate proxies, then drops anything outside the supplied `PitUniverse`:

```python
# analyst/mapper/engine.py:541-542
        if isin not in universe:
            continue  # not listed-and-in-scope as of the date; drop, do not widen the universe
```

Precise current state:

1. **No classification or thematic data source is wired anywhere.** The theme -> sector/industry
   link is produced by the model, grounded only by the ISIN list in the prompt
   (`engine.py:404-417`).
2. **Purity is arithmetic over disclosed revenue shares** (`analyst/mapper/purity.py`,
   `score_purity`), not a model guess — but the disclosure inputs come from the model's answer, not
   from a store.
3. **`map_theme` has no non-test caller.** `grep -rn 'map_theme(' --include=*.py .` returns only its
   own definition at `analyst/mapper/engine.py:420`. It is a library function; no daily loop, no
   backtest, no CLI invokes it.
4. **The universe passed in must be built by the caller.** The mapper does not construct a
   `PitUniverse`.
5. **Sector for the rails is unrelated to the mapper** — the rails take a bare `sector: str` on each
   order, and the only ISIN->sector map in the repo is the survivorship-biased fixture CSV set.
6. **Thesis is a separate, self-contained governance object** (`analyst/thesis/models.py:454-503`):
   `case_id, isin, sleeve, version, status, supersedes_version, driver, theme_purity,
   expected_evidence, break_conditions, ratification`. `authorize_buy`
   (`analyst/thesis/engine.py`) gates CORE buys on a ratified thesis. `BreakCondition`
   (`models.py:359-383`) refuses an unfalsifiable condition at construction. It consumes no market
   data and is not on any backtest path.

**Not a stub, but not a pipeline:** every piece exists and is tested; nothing connects
theme -> instrument set in a runnable driver.

---

## 5. JOURNAL / DECISION record

```python
# analyst/journal/models.py:249-307
class JournalEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime  # required, must be tz-aware
    trading_date: date  # required
    case_id: str | None = None
    actor: Actor  # required
    decision: Decision  # required
    isin: str | None = None  # pattern-checked
    sleeve: Sleeve | None = None
    evidence_snapshot_ref: str | None = None  # ^sha256:[0-9a-f]{64}$
    break_conditions_evaluated: tuple[BreakConditionEvaluation, ...] = ()
    rationale: str | None = None
    model: str | None = None
    tokens: TokenSpend | None = None
    orders_ref: str | None = None
    payload: Mapping[str, str] = {}  # strings only
```

Conditional requirements (`models.py:328-367`): rationale for
BUY/SELL/ESCALATE/SKIPPED_DATA_RED/AUTH_REQUIRED/DEFERRED/RAIL_BLOCK/POLICY_PROPOSAL; isin **and**
sleeve for BUY/SELL/DEFERRED; `evidence_snapshot_ref` for HEARTBEAT; model whenever tokens present.

The PIT check at this layer (`models.py:335-340`): `trading_date` may not exceed the IST date of
`ts`. Enums mirror the `decision_journal` CHECK constraints in `0001_init.sql`
(`Actor` `models.py:85-112`, `Decision` `models.py:115-156`, `Sleeve` `models.py:159-169`,
`Verdict` `models.py:172-182`).

`RecordedEntry` (`models.py:370-380`) adds `id: int` and `recorded_at: datetime` — deliberately
**excluded** from the determinism bytes.

Evidence, content-addressed and timestamp-free by design:

```python
# analyst/journal/evidence.py:180-204
class EvidenceBundle(BaseModel):
    case_id: str | None = None
    trading_date: date
    actor: Actor
    rendered_prompt: str | None = None
    items: tuple[EvidenceItem, ...] = Field(min_length=1, ...)

# analyst/journal/evidence.py:145-162
class EvidenceItem(BaseModel):
    kind: EvidenceKind
    source: str
    label: str
    isin: str | None = None
    as_of: date | None = None
    knowable_at: datetime | None = None      # <- the PIT stamp, OPTIONAL
    value: Money | None = None
    text: str | None = None
    detail: Mapping[str, str] = {}

# analyst/journal/evidence.py:222-241
class EvidenceRef(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", ...)
    size_bytes: int = Field(ge=0, ...)
    item_count: int = Field(ge=1, ...)
    case_id: str | None = None
    trading_date: date
```

### Is it enough to prove PIT correctness and reproduce a decision?

**Reproduce: yes.** The bundle is complete-by-contract, hashes to a stable address
(`evidence.py:206-219` — no timestamp of its own), and `ReplayResult.digest()`
(`replay.py:280-287`) is a single sha256 over journal-then-book.

**Prove PIT correctness: only partially, and the gap is explicit.**

- `EvidenceItem.knowable_at` is **optional** (`evidence.py:155-157`), with the docstring conceding
  it *"is optional because not every source publishes one, but recording it is what lets the PIT
  leak test (invariant #7) audit a past decision"*.
- Every backtest policy today writes items with `as_of=session` and **no `knowable_at`** — e.g.
  `naive_momentum.py:290-299`.
- The record-level `knowable_date` that the PIT guard actually checks lives on `MomentumRecord`
  etc., **not** on the journalled evidence. It is checked at decision time and then discarded.

Net: a journal row proves *what was shown* and *that the engine asserted the session scope*; it does
not carry the per-fact first-knowable dates an independent auditor would need to re-prove no leak
years later.

---

## 6. MULTI-FUND — single-book, definitively

**The engine is single-book.** Evidence:

- `SimBroker.__init__` (`execution/sim_broker.py:277-302`) holds one `self._cash`, one
  `self._holdings`, one `self._positions`, one `self._ledger`, one `_next_order_seq`, one
  `_next_ledger_seq`. No fund/account key anywhere.
- `PortfolioBook.__init__` (`backtest/accounting.py:164-175`) — one `_cash`, one `_positions`, one
  `_realized`, one `_ledger`, one `_external`.
- `BookSnapshot` (`replay.py:187-190`) — a single `cash: Decimal` and flat tuples.
- `ReplayEngine.__slots__` (`replay.py:316`) — `("_broker", "_clock", "_journal", "_policy",
  "_sessions")`. One broker, one policy.
- `Margins` (`execution/broker.py:275-288`) — one `available`/`utilised`, no account id.
- `grep -rn '\bfund_id\b'` across `analyst/ backtest/ execution/ accounting/ dataplatform/` ->
  **zero hits**. The only `fund` is `CaseService.fund()` (`analyst/cases/service.py:581`), a
  funding-mode transition, not a dimension.

**The analyst layer *does* have a multi-book dimension — it is `case_id`** and it is a Postgres
foreign key: `case_` (`0001_init.sql:259`), with `policy_set`, `thesis`, `decision_journal`,
`order_`, `token_usage` all carrying `case_id REFERENCES case_(case_id)` (`0001_init.sql:289, 331,
363, 415, 472`). `Portfolio` (`analyst/rails/policies.py:145`) and `EvidenceBundle`
(`evidence.py:193`) carry it too. **The backtest stack does not.**

### Exactly what would need a fund dimension added

| Structure | File:line | Change |
|---|---|---|
| `SimBroker._cash`, `_holdings`, `_positions`, `_ledger`, `_orders` | `execution/sim_broker.py:295-302` | key every dict by fund; per-fund cash and ledger sequence |
| `SimBroker._settle_into` | `execution/sim_broker.py:516-537` | per-fund settlement cursor |
| `SimBroker._fill` cash / holdings checks | `execution/sim_broker.py:431-453` | scope "insufficient cash"/"insufficient holdings" to the fund |
| `SimBroker.place` duplicate-staged netting | `execution/sim_broker.py:327-330` | one order per (fund, scrip, session), not per (scrip, session) |
| `SimBroker._post_ledger` seq | `execution/sim_broker.py:301-302` | per-fund monotonic ledger sequence |
| `Margins` | `execution/broker.py:275-288` | add fund identity |
| `Position` | `execution/broker.py:231-244` | add fund field |
| `Holding` | `execution/broker.py:247-254` | add fund field |
| `LedgerEntry` | `execution/broker.py:257-272` | add fund field |
| `Order` / `OrderRequest` | `execution/broker.py:142-228` | orders must carry their fund |
| `Broker` protocol reads | `execution/broker.py:294-338` | `positions()/holdings()/ledger()/margins()` need a fund scope |
| `PortfolioBook` all state | `backtest/accounting.py:169-175` | one book per fund, or a fund key |
| `PortfolioBook._external` (XIRR stream) | `backtest/accounting.py:174` | per-fund cashflow stream -> per-fund NAV/XIRR |
| `PortfolioBook.compare_to_benchmarks` | `backtest/accounting.py:148+` | per-fund comparison |
| `BookSnapshot` / `BookSnapshot.of` | `backtest/replay.py:175-231` | fund-keyed sections; determinism bytes must stay sorted |
| `ReplayEngine` policy/broker slots | `backtest/replay.py:316-340` | N policies, or a policy returning fund-tagged orders |
| `SessionDecision.orders` | `backtest/replay.py:151` | orders must carry their fund |
| `ReplayEngine._run_session` place loop | `backtest/replay.py:384-385` | route each order to its fund's book |
| `_AccountingBroker` mirror + `total_charges` | `backtest/run.py:1019-1046` | per-fund book and charge accumulator |
| `_AccountingBroker` nav_sink | `backtest/run.py:1044-1045` | per-fund NAV path |
| `BacktestResult` (final_nav, drawdown, comparison, ...) | `backtest/run.py:1237-1265` | per-fund metrics |
| `_terminal_prices` | `backtest/run.py:1576-1601` | union across funds |
| `RiskRails` evaluation | `analyst/rails/engine.py:118-150` | already per-case; needs a per-fund rails set + the `HouseholdExposure` aggregation that today has no assembler on any backtest path |

The `CROSS_CASE_CONCENTRATION` rail (`analyst/rails/engine.py:257-273`) is the only piece already
designed for N books — and it takes `HouseholdExposure` as a pre-assembled argument that nothing in
`backtest/` builds.

---

## 7. DETERMINISM — what it demands, and what enforces it

### What the guarantee requires of the data

1. **Frozen clock, moved one session at a time.** `self._clock.freeze_at(session)`
   (`replay.py:362`) before anything else in the session. `SimBroker.place` reads
   `self._clock.today()` for the decision date (`sim_broker.py:325`). No wall clock anywhere in the
   stack.
2. **Strictly increasing, pre-materialised session list.** `replay.py:330-335` rejects a repeat or a
   step backwards; `SimBroker._settle_into` (`sim_broker.py:524-528`) refuses a backwards session
   too.
3. **Deterministic tie-breaks — every ranking sorts by ISIN as the final key.**
   `sorted(candidates, key=lambda r: (-r.momentum, r.isin))` (`naive_momentum.py:193`); sells
   iterate `sorted(held)` (`naive_momentum.py:216`); weights over `sorted(target)`
   (`naive_momentum.py:248`). Query outputs are ISIN-ordered (`shapes.py:141-143`),
   positions/ledger are ISIN- and seq-ordered (`accounting.py:189-195`).
4. **Prices quantised to the paisa tick, adversely.** `_quantise_adverse` (`sim_broker.py:229-238`)
   — without it the slippage ratio puts a 28-digit repeating decimal on the fill and two sums of the
   same fills disagree in the last digit (`sim_broker.py:65-68`).
5. **Weights sum to exactly 1**, residue on the last ISIN (`naive_momentum.py:314-327`).
6. **Evidence carries no timestamp** so the same facts always hash the same
   (`evidence.py:186-188`).
7. **Recorded-form fields excluded.** `ReplayResult` serialises `JournalEntry`, not `RecordedEntry`
   — the DB row id and `recorded_at` differ every run by design (`replay.py:254-258`,
   `models.py:370-380`).
8. **Byte-deterministic writes upstream:** L1/L2 quantise on write
   (`dataplatform/store/schemas.py:204-211`, `dataplatform/store/l2.py:114`).

### Tests that enforce it

| Test | What it pins |
|---|---|
| `tests/integration/test_replay_determinism.py:318` `test_two_runs_are_byte_identical` | the §8.3.3 claim, byte-for-byte |
| `:355`, `:378` | swapping the policy changes output — no strategy in the engine |
| `:366` | do-nothing policy still journals one heartbeat per session (invariant #9) |
| `:391`, `:400` | per-session PIT scope; a policy reaching past the session trips the guard |
| `:412` | engine rejects a decision about the wrong session |
| `:437` | orders are staged for the *next* session |
| `:521` | full journaling lands in the append-only `decision_journal` (needs Postgres) |
| `tests/golden/test_reference_case.py` | hand-computed 5-year SIP + split + merger + demerger, literal expected values to the paisa |
| `tests/property/test_sim_broker_property.py` | ledger vs independent re-sum of fills (the quantisation property) |
| `tests/property/test_rails_property.py` | no generated order stream breaches a cap |
| `tests/property/test_book_property.py`, `test_costs_property.py`, `test_sip_property.py` | book, cost and allocator invariants |
| `tests/unit/test_backtest_venue.py` | the `exchange='NSE'` venue pin |
| `tests/unit/test_replay_engine.py` | engine unit surface |
| `tests/unit/test_clock_guard.py` | no `datetime.now()` creeps in |
| `tests/unit/test_layout.py` | the `dataplatform/` vs stdlib `platform` rename guard |

---

## 8. WHAT WOULD BREAK ON A 20-YEAR RUN TODAY

Ordered by how early it stops you.

### 8.1 Blockers — the run does not start or does not complete

| # | Item | Evidence | Failure |
|---|---|---|---|
| **B1** | **The lake is 10 years, not 20.** `data/L1/prices_raw` spans `date=2016-09-01` ... `date=2026-09-04` (2,475 partitions). L0 `nse_bhavcopy_legacy` holds 2016-2024 plus a **single** 2010 probe file (`data/L0/nse_bhavcopy_legacy/2010/01/cm04JAN2010bhav.csv.zip`). | `backtest/run.py:1319-1321` | `BacktestError: no trading sessions in [...]` for any window before 2016-09-01. Nothing to backfill from — L0 for 2006-2016 does not exist. |
| **B2** | **Rate card starts 2017-07-01.** | `execution/costs/rates.yaml:34`; raise at `execution/costs/model.py:332-338` | `NoRateScheduleError` on the **first fill** of any pre-2017-07-01 session. Loud, immediate, unrecoverable without new schedules. Note even the *existing* 10-year lake has ~10 months (2016-09 -> 2017-06) that cannot be priced. |
| **B3** | **Corporate actions are never applied to the book during a walk.** `PortfolioBook.apply_split/apply_bonus/apply_demerger` exist (`backtest/accounting.py:286+`) but `grep` shows **no call site** in any run driver — only `record_fill` (`backtest/run.py:1042`) and `deposit` (`backtest/run.py:1356`). | `backtest/run.py:1038-1046` | A holding through a 2:1 split keeps its pre-split share count and is marked at the post-split raw close (`_terminal_prices`, `backtest/run.py:1576-1601`) -> **the position silently halves in value**. Over 20 years this is not an edge case; it is most large-cap holdings. Only the *signal* is split-corrected (via L2 `adj_close`), never the book. |
| **B4** | **Settlement is hard-coded T+1 for all history.** | `execution/sim_broker.py:516-537` (`_settle_into` rolls positions -> holdings on the next `execute_session`), documented at `execution/sim_broker.py:399` and `:517`; the T+1 assumption is baked into the window trim too (`backtest/run.py:1203-1234`, "Execution is T+1") | India was **T+3 until 2003, T+2 from 2003 to 2022, T+1 phased Feb-2022 -> Jan-2023**. A 20-year run gives 2006-2022 trades an extra day (or two) of settled liquidity they never had — an optimistic bias on any strategy that sells and re-buys. There is no era switch. |

### 8.2 Silent-wrong-answer risks

| # | Item | Evidence | Failure |
|---|---|---|---|
| **S1** | **Index membership screen is a dead no-op.** `data/L1/index_constituents` **does not exist**; the only constituent snapshot on disk is L0, dated `2026-09-03`. | `membership_asof` -> `None` (`dataplatform/ingest/indices.py:493-494`); consumed at `backtest/run.py:642-645` where `None` means "do not narrow" | The M9.3 "investable universe" reduces to the turnover floor alone. The report claims an index-constrained universe it never applied. Over 20 years this is the difference between NIFTY-500 and every microcap that ever printed. |
| **S2** | **Benchmark falls back to an ad-hoc L1 proxy.** `data/L1/benchmark_tri` **does not exist** -> `read_tri_series` returns `None` (`dataplatform/ingest/indices.py:1009-1010, 1023-1024`). | `_resolve_benchmark` `backtest/run.py:1150-1195`; disclosure text `backtest/run.py:1692-1695` | A 20-year excess-return figure is struck against a **price-return proxy with an estimated dividend accrual**, not a real TRI. The code says so; the number still ends up in a table. |
| **S3** | **Sector data is a current-day fixture applied backward, covering 42 ISINs.** | `_STATIC_SECTOR_MAP_DIR = Path("tests/fixtures/nifty_indices/constituents")` (`backtest/run.py:167`); `_load_static_sector_map` docstring admits it is *"survivorship-biased"* (`backtest/run.py:2472-2475`); universe narrowed to mapped names at `backtest/run.py:2584`; `knowable_date=as_of` stamped regardless at `backtest/run.py:2601` | Sector rotation over 20 years assumes every name always sat in its 2026 industry and was always in the index. Also: a **backtest driver reading from `tests/fixtures/`** is a structural smell. See Task C addendum. |
| **S4** | **Float arithmetic in the swing signal path.** The entire feature computation is `CAST(... AS DOUBLE)` in DuckDB, and results re-enter `Decimal` via `Decimal(str(round(x, 8)))`. | `backtest/run.py:3936-3939` (four `CAST ... AS DOUBLE`), `backtest/run.py:4017-4022`, `backtest/run.py:4000` (`fallback = ... else 0.0`) | Not a money bug — no rupee is a float — but it is a **ranking** bug risk: two names within 1e-9 of each other can order differently across DuckDB versions/platforms, and the ISIN tie-break only fires on exact equality. It also puts a float in the PIT-guarded record path. `_max_drawdown` (`backtest/run.py:1612-1628`) and the cost model are clean Decimal; XIRR's root-find is float by *documented* design (`backtest/xirr.py:16-20`). |
| **S5** | **Dividends never reach cash.** `grep dividend` over `backtest/accounting.py`, `execution/sim_broker.py` finds only benchmark commentary. | `backtest/accounting.py` (no dividend method); `backtest/run.py:1085` | A 20-year equity run understates total return by roughly the cumulative dividend yield (~1.3%/yr compounding for India ~ 25-30% terminal). `tr_close` exists in L2 (`dataplatform/query/shapes.py:71`) but no policy or book consumes it. |
| **S6** | **Delivery data is thin and imputed.** `deliv_pct` source `nse_sec_bhavdata_full` covers **2019-2026 only** in L0; `nse_mto` covers 2016-2019. Missing delivery takes the universe median. | `backtest/run.py:3999-4013`; caveat at `backtest/run.py:3893-3897` | Pre-2016 (and much of 2016-2019) delivery is absent entirely. The swing composite's delivery leg would be pure imputation for the first decade of a 20-year run — a leg that measures nothing but still moves the rank. |
| **S7** | **Momentum look-back is 365 *calendar* days into a `bisect` over a lake-derived calendar.** | `_LOOKBACK_DAYS = 365` (`backtest/run.py:146`); `_lookback_session` `backtest/run.py:758-764` | Correct as written, but it silently returns `None` (no rank at all) for the first year of any window unless `lookback_sessions=calendar` is passed (`backtest/run.py:1344`). A window starting at the lake's edge produces no candidates and no error. |
| **S8** | **Survivorship handling is real but only as good as L1.** `listing_windows` derives windows from observed L1 prints (`backtest/run.py:312-339`), which is genuinely survivorship-safe *within the lake*. | `backtest/run.py:318-319` | A name delisted in 2012 has no L1 rows at all, so it is invisible — not "kept in the universe as of 2010", simply absent. Survivorship bias returns the moment the window predates the lake. The production path (`store_listing_calendar`, `dataplatform/query/universe.py:47-55`) needs a populated identity master, which the backtest does not use. |
| **S9** | **Holiday calendar covers 2016-2026 and raises outside it.** | `dataplatform/ingest/data/nse_holidays.yaml:25-27`; `_require_coverage` raises (`dataplatform/ingest/calendar.py:230-242`) | The backtest itself dodges this (it derives the calendar from L1, `backtest/run.py:300-310`) — but **every ingestion, gap-report and quality path for a pre-2016 backfill raises `CalendarCoverageError`**. You cannot verify a 20-year backfill's completeness without extending this file. |
| **S10** | **Rails are absent from the run.** | §3.1 — no import of `analyst.rails` in `backtest/` or `execution/` | A 20-year qualification would report returns that no rail ever constrained — i.e. results that could not be achieved by the live system, which *does* rail every order (`analyst/cash/manager.py:265`, `analyst/rotation/engine.py:309`). This breaks the "one decision path" claim at the risk layer even though it holds at the cost layer. |
| **S11** | **Stamp duty resolves against a 2-state table with a hard-coded account state.** | `_ACCOUNT_STATE = "MH"` (`backtest/run.py:149`); `state_rates` = `{KA, MH}` (`execution/costs/rates.yaml:107-110`); raise at `execution/costs/model.py:252-258` | Works today; any new pre-2020 schedule that omits `MH` raises mid-run. Also, the pre-2020 rate is `reconstructed`, not verified. |
| **S12** | **`_reserve_fill_headroom` silently drops the last session.** | `backtest/run.py:1218-1234` | Correct behaviour, but a 20-year window ending at the lake edge quietly loses its final session and the terminal valuation moves. Logged (`backtest/run.py:1228-1233`), not surfaced in `BacktestResult`. |

---

## 9. FIXTURE / GOLDEN inventory

### 9.1 What exists per source and era

| Source | Fixtures | Eras covered |
|---|---|---|
| `nse_bhavcopy/legacy` | `cm01JAN2016`, `cm23MAR2020`, `cm13JUL2020`, `cm16FEB2021`, `cm05JUL2024` | legacy era, 2016-2024 |
| `nse_bhavcopy/udiff` | `20240708`, `20260807` | UDiFF era, from the 2024-07-08 cutover (`dataplatform/ingest/nse/bhavcopy.py:44-48`) |
| `bse_bhavcopy` | `EQ020124_CSV.ZIP` (legacy), `20260807` (udiff) | 1 file per era |
| `nse_delivery` | 4 CSVs incl. `08082022_XLSX` and `30092019_MISDATED` | 2019, 2022, 2026 — format anomalies deliberately captured |
| `nse_mto` | `02092016`, `27092019`, `20062024`, `07082026` | 2016-2026 |
| `nifty_indices/constituents` | 13 CSVs, **all 2026-07/08/09** | **current only** |
| `nifty_indices/tri` | 1 JSON, `20260803-20260805` | current only |
| `nifty_index_close` | `cnx_era` (2012, 2015-11-06), `nifty_era` (2015-11-10, 2026) | the CNX->NIFTY rename boundary — the one genuinely historical era pair |
| `corp_actions` | nse 2026-08-08; bse 2026-08-08, 2026-09-06 (+ EMPTY_PURPOSE) | current only |
| `xbrl` | 37 files (filings, index, integrated) | recent |
| `yfinance` | 7 named CA cases (2021-2025) | the golden CA reference prices |
| `kite` | 13 JSON (orders, holdings, ledger, margins, errors) | current API |
| `nse_equity_list`, `bse_scrip_master`, `screener`, `gdelt`, `rss`, `nse_deals`, `nse_flows`, `nse_shareholding`, `nse_fo`, `announcements` | 1-3 files each | current only |

### 9.2 Golden suite

`tests/golden/cases/` — 7 hand-verified corporate actions:
`irctc_split_2021`, `ltim_merger_2022`, `hdfc_merger_2023`, `jiofin_demerger_2023`,
`ril_bonus_2024`, `tatamotors_dvr_2024`, `tatamotors_demerger_2025`. **All 2021-2025.**

`tests/golden/reference_case/` — the M4.9 hand-computed 5-year SIP over 3 stocks with one split, one
merger and one demerger, literal `Decimal` expectations (`tests/golden/test_reference_case.py:1-21`).
This is the only place `apply_split`/`apply_demerger` are exercised end-to-end — and it builds the
book directly, not through `ReplayEngine`.

### 9.3 What a 20-year qualification would additionally need

**Format-era fixtures (the parsers must be provably correct on old files):**

1. NSE legacy bhavcopy fixtures from **2006-2015** — at least one per known header change; the
   current earliest is 2016-01-01.
2. BSE legacy bhavcopy from 2006-2015 (currently one 2024 file).
3. Pre-2019 delivery/MTO format eras — `sec_bhavdata_full` did not exist for most of that span; the
   MTO `.DAT` era needs coverage back to 2006.
4. Corporate-action feed fixtures from the pre-2016 NSE/BSE formats — currently 2026 only.

**Reference data that does not exist in any form:**

5. **Index constituent snapshots for 2006-2016** — the M3.9 design accrues them forward monthly
   (`dataplatform/ingest/indices.py:9-18` explicitly records that *"no historical constituents
   download"* exists anywhere). Without a purchased or reconstructed history, S1 and S3 are
   permanent for the back half of a 20-year window.
6. **A real NIFTY TRI series 2006-2026** — session-gated (`backtest/run.py:44-46` records the
   licensed feed FAILED at gate C.1).
7. **An ISIN->sector history** — nothing in the repo produces one; the rails structurally require it
   (§3.2).
8. **Dividend history** — needed for both S5 (book cash) and a real TRI.

**Golden cases to add:**

9. CA golden cases from 2006-2020 — the entire existing suite is 2021+, so no parser or factor-chain
   behaviour on older CA formats is pinned.
10. **A settlement-era golden case** — a T+2 and a T+3 round trip, to pin B4 once an era switch
    exists.
11. **A cost-regime golden case per new `rates.yaml` schedule** — the existing
    `tests/unit/test_costs.py` and `tests/property/test_costs_property.py` cover the four current
    schedules; each added historical regime needs its own worked contract note.
12. **A corporate-action-during-a-replay integration test** — there is none today.
    `tests/integration/test_backtest_adjusted.py` proves the *signal* de-corruption on a controlled
    split fixture; nothing proves the *book* survives one, because (B3) the book never applies one.

---

## Summary of what did not fit the task

- No `make check`, no tests run (read-only investigation, no writes inside the repo).
- `analyst/monitor/{t0,t1,t2}`, `analyst/interview`, `analyst/cash`, `analyst/rotation`,
  `analyst/cases/web.py`, `accounting/tokens.py`, `execution/kite_broker.py` and
  `dataplatform/status` were not enumerated at field level. All are substantial, non-stub modules,
  but none sits on the backtest/replay data path, which is what the question asked to bound. Their
  absence from that path was confirmed by grep rather than by full read.
- `dataplatform/query/{screen,quarantine,fundamentals_metrics,announcement_search}.py` are real and
  tested (the restated-fundamentals quarantine of invariant #8 is structural), but only
  `fundamentals_metrics.compute_metrics` is reached from a backtest driver (`backtest/run.py:114`);
  the screen algebra and announcement index have no backtest caller.

---
---

# ADDENDUM — measured ground truth (Task B and Task C)

Added after a parallel store-inventory agent reported that `data/L1/index_constituents` and
`data/L1/benchmark_tri` do not exist. **The inventory agent is correct. This addendum supersedes
any implication in §1.2 that those datasets hold data.** The code paths cited in §1.2 are real —
they are the paths the code *would* read — but on this machine both resolve to nothing and return
`None`.

## TASK B — do `index_constituents` and `benchmark_tri` exist?

### B.1 Dataset directory names, from the code

```
$ grep -n 'CONSTITUENTS_DATASET\|TRI_DATASET' dataplatform/ingest/indices.py
126:CONSTITUENTS_DATASET: Final = "index_constituents"
127:TRI_DATASET: Final = "benchmark_tri"
```

Both are resolved as `layer_root(Layer.L1) / <DATASET>` — `dataplatform/ingest/indices.py:500`
(constituents) and `:1008` (TRI).

### B.2 Filesystem — both ABSENT

```
$ ls -la /home/ubuntu/stock-manager/data/L1/
drwxrwxr-x 2017 ubuntu ubuntu  69632 Sep  6 23:05 pit_fundamentals
drwxr-xr-x 2477 ubuntu ubuntu 184320 Sep  6 16:25 prices_raw
drwxrwxr-x 2463 ubuntu ubuntu  77824 Sep  7 11:24 prices_raw_quarantine

$ for d in index_constituents benchmark_tri; do
    p="data/L1/$d"; [ -e "$p" ] && echo "EXISTS: $p" || echo "ABSENT: $p"; done
ABSENT: data/L1/index_constituents
ABSENT: data/L1/benchmark_tri

$ find . -type d \( -name index_constituents -o -name benchmark_tri \) -not -path './.git/*'
(no output)
```

**Three datasets exist in L1: `pit_fundamentals`, `prices_raw`, `prices_raw_quarantine`. That is
all.**

### B.3 The reader functions, run against the real settings

```
$ uv run python - <<'PY'  (abridged)
settings.data_root      = /home/ubuntu/stock-manager/data
L1 root                 = /home/ubuntu/stock-manager/data/L1
constituents dir        = /home/ubuntu/stock-manager/data/L1/index_constituents | exists: False
benchmark_tri dir       = /home/ubuntu/stock-manager/data/L1/benchmark_tri | exists: False

--- membership_asof() probes across 20 years ---
  membership_asof('nifty500', 2006-06-30) -> None
  membership_asof('nifty500', 2010-06-30) -> None
  membership_asof('nifty500', 2016-06-30) -> None
  membership_asof('nifty500', 2020-06-30) -> None
  membership_asof('nifty500', 2024-06-30) -> None
  membership_asof('nifty500', 2026-06-30) -> None
  _snapshot_dates('nifty500') -> []
  membership_asof('nifty50',  2006..2026)  -> None (every probe)
  _snapshot_dates('nifty50')  -> []
  membership_asof('niftyit',  2006..2026)  -> None (every probe)
  _snapshot_dates('niftyit')  -> []

--- read_tri_series() probes ---
  read_tri_series('nifty50',  2006-12-31) -> None
  read_tri_series('nifty50',  2016-12-31) -> None
  read_tri_series('nifty50',  2026-12-31) -> None
  read_tri_series('nifty500', 2006-12-31) -> None
  read_tri_series('nifty500', 2016-12-31) -> None
  read_tri_series('nifty500', 2026-12-31) -> None
```

**Rows: zero. Slugs with any L1 history: zero. `as_of` span: empty. TRI history: none, at any
date.**

### B.4 What DOES exist — L0 only, one capture date

```
$ find data/L0/nifty_index_constituents -type f | sort
data/L0/nifty_index_constituents/2026/09/ind_nifty500list_20260903.csv          (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_nifty50list_20260903.csv           (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftyautolist_20260903.csv         (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftybanklist_20260903.csv         (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftyconsumerdurableslist_20260903.csv (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftyenergylist_20260903.csv       (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftyfinancelist_20260903.csv      (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftyfmcglist_20260903.csv         (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftyhealthcarelist_20260903.csv   (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftyinfralist_20260903.csv        (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftyitlist_20260903.csv           (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftymedialist_20260903.csv        (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftymetallist_20260903.csv        (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftypharmalist_20260903.csv       (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftyprivatebanklist_20260903.csv  (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftypsubanklist_20260903.csv      (+ .meta.json)
data/L0/nifty_index_constituents/2026/09/ind_niftyrealtylist_20260903.csv       (+ .meta.json)

$ find data/L0/nifty_index_constituents -name '*.csv' | sed 's/.*_\([0-9]\{8\}\)\.csv/\1/' | sort -u
20260903
```

**One capture date, 2026-09-03. 17 slugs. Row counts:**

```
slug,data_rows
nifty500,500
nifty50,50
niftyauto,15
niftybank,14
niftyconsumerdurables,13
niftyenergy,40
niftyfinance,20
niftyfmcg,15
niftyhealthcare,20
niftyinfra,30
niftyit,10
niftymedia,10
niftymetal,15
niftypharma,20
niftyprivatebank,415      <- NOT constituents; this is the HTML error shell (see below)
niftypsubank,12
niftyrealty,10
```

Nothing under `data/L0/` for a TRI/benchmark source at all — the L0 source directories are:
`bse_bhavcopy_legacy, bse_bhavcopy_udiff, bse_corp_actions, bse_scrip_master,
nifty_index_constituents, nse_bhavcopy_legacy, nse_bhavcopy_udiff, nse_corp_actions,
nse_financial_results_index, nse_integrated_filing_index, nse_mto, nse_sec_bhavdata_full,
nse_xbrl_filing`.

### B.5 Postgres `sync_state` — matches the inventory agent exactly

```sql
SELECT source, state, count(*) AS units, min(logical_date), max(logical_date),
       count(DISTINCT logical_date)
FROM sync_state
WHERE source ILIKE '%index%' OR source ILIKE '%tri%' OR source ILIKE '%constituent%'
GROUP BY source, state ORDER BY source, state;
```

```
 ('nifty_index_constituents',     'FAILED',     1, 2026-09-03, 2026-09-03, 1)
 ('nifty_index_constituents',  'PUBLISHED',    16, 2026-09-03, 2026-09-03, 1)
 ('nse_financial_results_index',  'FAILED',     1, 2026-04-01, 2026-04-01, 1)
 ('nse_financial_results_index','PUBLISHED',   90, 2016-04-01, 2026-07-01, 42)
 ('nse_integrated_filing_index','PUBLISHED',  114, 2025-03-01, 2026-09-01, 19)
```

All sources:

```
 ('bse_bhavcopy',                  536, 2024-07-08, 2026-09-04)
 ('bse_bhavcopy_legacy',          1939, 2016-09-01, 2024-07-05)
 ('bse_corp_actions',             6684, 2016-09-01, 2016-09-01)
 ('nifty_index_constituents',       17, 2026-09-03, 2026-09-03)
 ('nse_bhavcopy',                 2471, 2016-09-02, 2026-09-01)
 ('nse_corp_actions',               11, 2016-09-01, 2026-09-01)
 ('nse_financial_results_index',    91, 2016-04-01, 2026-07-01)
 ('nse_integrated_filing_index',   114, 2025-03-01, 2026-09-01)
 ('nse_xbrl_filing',            105606, 2018-05-21, 2026-09-05)
```

**There is no TRI / benchmark source row in `sync_state` at all** — the pipeline has never been run.

The FAILED unit:

```
 FAILED     niftyprivatebank   attempts=1
   last_error: ParseError: ind_niftyprivatebanklist_20260903.csv: body is markup, not CSV —
               the site's Angular shell answered a bad path with HTML and a 200; it must not become...
```

(That explains the bogus 415 "rows": it is an HTML page, correctly refused at parse.)

### B.6 Verdict on Task B

- **`data/L1/index_constituents` — DOES NOT EXIST.** Zero rows, zero slugs, empty `as_of` span.
- **`data/L1/benchmark_tri` — DOES NOT EXIST.** Zero rows. `read_tri_series` returns `None` for
  every slug at every date. **The TRI series does not go back at all — it does not exist.**
- **Index membership is NOT a point-in-time series.** It is **a single L0 snapshot dated
  2026-09-03**, 16 slugs parsed + 1 failed, never promoted to L1. `_snapshot_dates()` returns `[]`
  for every slug, so `membership_asof` returns `None` at every date from 2006 to 2026.
- Consequence, confirmed: `_InvestableUniverse.members_asof` (`backtest/run.py:619-626`) always
  returns `None`, and `constrain` (`backtest/run.py:639-645`) therefore never applies the membership
  screen. **Every M9.3 "investable universe" figure ever reported by this repo was
  turnover-floor-only.** And `_resolve_benchmark` (`backtest/run.py:1150-1195`) always takes the L1
  proxy fallback branch — the "computed TRI" benchmark has never once been used.

My original §1.2 correctly cited the *code paths*; it should have said, and now says, that both
resolve to nothing on this machine. The inventory agent's measurement is right and mine was
incomplete on this point.

---

## TASK C — the static sector map

### C.1 Which exact files

`backtest/run.py:167`:

```python
_STATIC_SECTOR_MAP_DIR = Path("tests/fixtures/nifty_indices/constituents")
```

`backtest/run.py:2478` globs `ind_*list_*.csv` in sorted order, later files overwriting earlier.
Thirteen files match:

```
ind_nifty500list_20260901.csv       (1037 bytes)
ind_nifty50list_20260701.csv        ( 677 bytes)
ind_nifty50list_20260801.csv        ( 671 bytes)
ind_niftyautolist_20260901.csv      ( 426 bytes)
ind_niftybanklist_20260901.csv      ( 355 bytes)
ind_niftyenergylist_20260901.csv    ( 381 bytes)
ind_niftyfmcglist_20260901.csv      ( 396 bytes)
ind_niftyitlist_20260801.csv        ( 366 bytes)
ind_niftyitlist_20260901.csv        ( 366 bytes)
ind_niftymedialist_20260901.csv     ( 284 bytes)
ind_niftymetallist_20260901.csv     ( 284 bytes)
ind_niftypharmalist_20260901.csv    ( 288 bytes)
ind_niftyrealtylist_20260901.csv    ( 255 bytes)
```

All dated 2026-07 / 2026-08 / 2026-09. These are **unit-test fixtures**, not lake data — a 1 KB
"nifty500" file that holds a handful of rows, not 500.

### C.2 How many ISINs

```
$ uv run python -c "from backtest.run import _load_static_sector_map, _STATIC_SECTOR_MAP_DIR; ..."
distinct ISINs   : 42
distinct industry: 12
```

**42 ISINs. Not 500, not 1,900 — forty-two.**

`_L1SectorRotationData._compute` narrows the universe to mapped names only
(`backtest/run.py:2584`):

```python
universe = frozenset(isin for isin in universe if isin in self._sector_by_isin)
```

So **every sector-rotation backtest in this repo ranks at most 42 candidates across 12 industries**,
for the entire window, at every rebalance.

### C.3 Taxonomy

NSE's own "Industry" column from the constituents CSV — the NSE/NIFTY macro-economic sector
taxonomy, not GICS, not NIC. The 12 values present:

```
Automobile and Auto Components
Construction
Fast Moving Consumer Goods
Financial Services
Healthcare
Information Technology
Media Entertainment & Publication
Metals & Mining
Oil Gas & Consumable Fuels
Power
Realty
Telecommunication
```

Header (`tests/fixtures/nifty_indices/constituents/ind_nifty500list_20260901.csv:1`):

```
Company Name,Industry,Symbol,Series,ISIN Code
Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018
```

### C.4 Does it carry any effective / as-of date?

**No.**

```python
# backtest/run.py:2466
def _load_static_sector_map(map_dir: Path) -> dict[str, str]:
```

The return type is `dict[str, str]` — ISIN -> industry. The filename dates are used **only** for
sort order so that "the most recent classification wins" (`backtest/run.py:2478`); they are then
discarded. There is no date in the key, no date in the value, no interval, no version.

**It therefore cannot express a reclassification.** A name that moved from "Financial Services" to
"Information Technology" in 2019 has exactly one industry in this map: its 2026 one, applied to
every date in the backtest.

### C.5 Is the PIT guard catching this? No — it is stamped away.

`backtest/run.py:2596-2602`:

```python
records.append(
    SectorRotationRecord(
        isin=isin,
        momentum=now / then - _ONE,
        price=price,
        sector=self._sector_by_isin[isin],
        knowable_date=as_of,  # <- the decision date, unconditionally
    )
)
```

The sector is stamped `knowable_date = as_of`, i.e. "this 2026 classification was knowable on the
2017 decision date". `ctx.pit.admit` then passes it trivially. **The PIT guard is structurally
unable to catch this class of leak, because the leak is asserted away at construction.**

### C.6 The docstring describes a code path that does not exist

`backtest/run.py:2502-2508` claims:

> "Sector resolution is point-in-time by contract. When the L1 store holds constituent snapshots
> (post M10.1 live fetch / M10.2 accrual) the sector is read through `membership_asof` — the
> snapshot in force on the decision date..."

There is no `membership_asof` call anywhere in `_L1SectorRotationData` (grep of
`backtest/run.py` shows `membership_asof` only at `:109` import, `:623` in `_InvestableUniverse`,
and inside docstrings/report prose at `:556, :558, :595, :2019, :2077, :2104, :2128, :2503, :2923,
:3006`). And `run_sector_rotation_report` calls the static loader unconditionally
(`backtest/run.py:2780`):

```python
sector_by_isin = _load_static_sector_map(
    sector_map_dir
)  # sector_map_dir defaults to the fixture dir
```

There is no branch. The PIT-honest path the docstring describes has not been written.

### C.7 Verdict on Task C — blunt

**No. Sector-based strategies in this repo are not point-in-time-honest, and cannot be with the
current code.**

Four independent reasons, any one of which is disqualifying:

1. The map has **no time dimension at all** — `dict[str, str]` (`backtest/run.py:2466`). A
   reclassification is inexpressible.
2. It is a **2026 classification applied backward** over the whole history — textbook look-ahead,
   and the loader's own docstring says so (`backtest/run.py:2472-2475`).
3. It is **survivorship-biased twice over**: only names that are *in a NIFTY index today* appear at
   all, so every constituent that was dropped, delisted, merged or failed between 2016 and 2026 is
   invisible to sector rotation.
4. It covers **42 ISINs** and comes from **`tests/fixtures/`** — a production backtest driver
   reading unit-test fixtures. The "sector rotation over the investable universe" framing is not
   what the code does; it rotates 42 current large caps.

The PIT guard does not save it (C.5), and the point-in-time branch the docstring promises does not
exist (C.6). Any sector-rotation result produced by this repo to date should be read as an
engine-plumbing demonstration, not as evidence about sector rotation.
