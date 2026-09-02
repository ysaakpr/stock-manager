"""The golden CA suite's **reference A**: a yfinance cross-check (EXECUTION_PLAN §4.3, M2.7).

Reference B (`test_golden_ca.py`) is our own factor engine checked against closes recomputed by
hand from the published CA terms. This module is the *second, independent* reference the plan
demands — Yahoo Finance's adjusted series for the `.NS` ticker, whose corporate-action database is
compiled entirely independently of our NSE-terms hand computation (ratified as B2 in
AGENTIC_CONTEXT §2). Two references that agree while being independent *in method* mean a
shared-direction error in a third-party adjusted series cannot pass the golden suite.

## What is actually compared, and why it is a ratio and not a price level

The golden cases carry *representative* raw closes (see `tests/golden/README.md` rule 4), not the
exact historical prints, so their absolute adjusted levels are deliberately not equal to Yahoo's.
Comparing price levels would therefore be meaningless. What both references pin — and the only
thing that makes them independent — is the **adjustment applied at each ex-date**:

* For a **split / bonus**, Yahoo encodes the adjustment as a split event in the very series it
  serves (`Ticker.splits`, delivered with `history(actions=True)`); its `multiplier` is the
  reciprocal of our engine's ``price_factor`` (a 1:5 split is Yahoo ``5.0`` ⇔ our ``0.2``; a 1:1
  bonus is Yahoo ``2.0`` ⇔ our ``0.5``). We compare our ``price_factor`` to ``1 / multiplier``
  within a stated tolerance, and independently confirm Yahoo's **adjusted-close series is
  continuous** across the ex-date (an organic-sized move, not the ~-50%/-80% cliff an *un*-adjusted
  or wrong-direction series would show). Together these pin both magnitude and direction.
* For a **merger** on the surviving entity, no price adjustment is due; both references agree the
  factor is ``1`` and Yahoo's series is continuous. No discrepancy.
* For a **demerger / DVR conversion**, Yahoo's adjusted-close model has no way to represent a
  carve-out onto a *different* ISIN, so it records no corporate action and never bridges the
  structural break — its return series carries a spurious return across the ex-date. Our engine
  classifies the event as a structural break and bridges it (§4.3 rule 3, ``ret=None``). This is a
  genuine disagreement; it is documented in ``DISCREPANCIES`` with the verdict that **reference B
  is authoritative**, and the suite is *not* bent to match Yahoo.

## Offline by default; the live pull is opt-in

Every comparison runs against a cached fixture under ``tests/fixtures/yfinance/<case_id>.json``, so
the suite is reproducible with no network (AGENTIC_CONTEXT B8). The one test that touches Yahoo,
``test_refresh_fixtures_from_yfinance``, is marked ``network`` and is **skipped unless explicitly
selected** with ``-m network`` (see ``tests/golden/conftest.py``), so a bare ``uv run pytest`` — and
``make check`` — never reach the network. Regenerate the fixtures with::

    uv run pytest tests/golden/test_yfinance_reference.py -m network

The verification command for this task is the offline half::

    uv run pytest tests/golden/test_yfinance_reference.py -q -m 'not network'
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.corpactions import ActionType, FactorChain, return_series
from tests.golden.casebook import GoldenCase, build_case_chain, load_cases

pytestmark = pytest.mark.golden

# ── configuration ────────────────────────────────────────────────────────────────────────────

#: ISIN → NSE symbol for the `.NS` Yahoo ticker. Reference A is defined on the `.NS` ticker (B2), so
#: this is the one place a symbol is allowed near the golden suite; it is a *fetch address*, never a
#: join key (invariant #2 — every comparison below is keyed by the case's ISIN and ex-date).
CASE_TICKERS: dict[str, str] = {
    "hdfc_merger_2023": "HDFCBANK.NS",
    "irctc_split_2021": "IRCTC.NS",
    "jiofin_demerger_2023": "RELIANCE.NS",  # JFSL carved out of Reliance (the parent ISIN)
    "ltim_merger_2022": "LTIM.NS",
    "ril_bonus_2024": "RELIANCE.NS",
    # Tata Motors' Oct-2025 demerger renamed the listed entity: the passenger-vehicle company
    # retained ISIN INE155A01022 and the full legacy price history, and Yahoo migrated that
    # history to TMPV.NS (the old TATAMOTORS.NS symbol was retired; TMCV.NS is the *new* CV
    # listing, with data only from Nov-2025). Both Tata cases key on INE155A01022, so both use
    # TMPV.NS — the `.NS` ticker that now carries their ex-date windows.
    "tatamotors_demerger_2025": "TMPV.NS",
    "tatamotors_dvr_2024": "TMPV.NS",
}

#: Cases for which reference A cannot be obtained: Yahoo serves no data for the `.NS` ticker at all.
#: This is a *verified* gap, not transient — see ``test_reference_a_unavailable_is_documented``
#: — and it is handled the way the spec handles any reference-A failure: document it and treat
#: reference B as authoritative, never fabricate a series to fill the hole. Keyed by case_id → the
#: reasoned verdict. If Yahoo restores coverage, re-run the live pull and delete the entry.
REFERENCE_A_UNAVAILABLE: dict[str, str] = {
    "ltim_merger_2022": (
        "Yahoo Finance serves no data for LTIM.NS (LTIMindtree) — history() returns zero rows for "
        "every range, recent and full, and the symbol search returns nothing, while peer NSE large "
        "caps (RELIANCE.NS, TCS.NS, INFY.NS, HDFCBANK.NS) return in the same call. LTIMindtree "
        "was formed by the 2022-11-24 merger under test (L&T Infotech absorbing Mindtree), and "
        "Yahoo's coverage of the renamed line is absent here. Verdict: reference B authoritative; "
        "reference A is unavailable for this case. A surviving-entity merger carries a unit price "
        "factor, so the checked-in reference-B literals (adjusted == raw, bridged return) remain "
        "the golden truth and are not weakened by the missing cross-check."
    ),
}

#: Trading-day fetch window around each ex-date, in calendar days either side.
_WINDOW_DAYS = 8

#: Relative tolerance on the ex-date adjustment factor (our ``price_factor`` vs ``1/multiplier``).
#: Splits and bonuses are exact rationals and Yahoo stores the multiplier as a float (``5.0``,
#: ``2.0``), so agreement is to float precision; 0.5% leaves generous head-room while still failing
#: an inverted or off-by-a-ratio multiplier. Stated tolerance for acceptance criterion 1.
FACTOR_TOL = Decimal("0.005")

#: Max |move| of Yahoo's adjusted close across a *scaling* ex-date (split/bonus) or a surviving-
#: entity merger. A correctly adjusted series shows only the organic ex-day move; an *un*-adjusted
#: 1:1 bonus would show ~-50% and an un-adjusted 1:5 split ~-80%, so 20% cleanly separates "Yahoo
#: applied the same adjustment we did" from "Yahoo did not". Stated tolerance for acceptance 1.
CONTINUITY_CEILING = Decimal("0.20")

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "yfinance"


# ── documented disagreements (acceptance criterion 3) ─────────────────────────────────────────


@dataclass(frozen=True)
class Discrepancy:
    """A known, reasoned disagreement between reference A (yfinance) and reference B (engine)."""

    reason: str
    verdict: str


#: The demerger / DVR cases where yfinance is known to be wrong and reference B is authoritative.
#: Keyed by case_id. The verdict is the same in substance for every carve-out and is asserted, not
#: hoped: yfinance's adjusted-close model encodes only splits and dividends, so it cannot represent
#: a carve-out onto a different ISIN and never bridges the break.
DISCREPANCIES: dict[str, Discrepancy] = {
    "jiofin_demerger_2023": Discrepancy(
        reason=(
            "Reliance's demerger of Jio Financial Services carves value onto a *new* ISIN "
            "(INE758E01017). Yahoo records no split/action on the RELIANCE.NS ex-date (2023-07-20) "
            "and leaves its adjusted-close series continuous, so the carve-out surfaces as an "
            "ordinary return rather than a bridged structural break."
        ),
        verdict=(
            "Reference B authoritative. Our engine classifies the demerger as a structural break "
            "and bridges the ex-date return (ret=None, §4.3 rule 3). yfinance's adjusted series "
            "cannot represent a cross-ISIN carve-out, so its return across the break is spurious. "
            "We do NOT bend our output to match yfinance."
        ),
    ),
    "tatamotors_demerger_2025": Discrepancy(
        reason=(
            "Tata Motors' demerger (commercial-vehicles business onto a new ISIN) is, like every "
            "demerger, invisible to Yahoo's split/dividend-only adjustment model: no action is "
            "recorded on the TATAMOTORS.NS ex-date and the adjusted-close series is not bridged."
        ),
        verdict=(
            "Reference B authoritative — same as jiofin_demerger_2023. The engine bridges "
            "the structural break; yfinance treats the carve-out drop as a real return."
        ),
    ),
    "tatamotors_dvr_2024": Discrepancy(
        reason=(
            "The Tata Motors DVR-conversion cancels the 'A' Ordinary (DVR) line against ordinary "
            "shares — a structural event on a separate ISIN, not a split of TATAMOTORS.NS. Yahoo "
            "records no action on the ordinary line's ex-date and does not bridge it."
        ),
        verdict=(
            "Reference B authoritative. The engine marks the DVR conversion a structural break and "
            "bridges the ex-date return; yfinance has no representation for it."
        ),
    ),
}


# ── the case set and its per-ex-date facts ─────────────────────────────────────────────────────

CASES: list[GoldenCase] = load_cases()

#: Cases with a usable reference-A series (everything except the verified-unavailable ones). Only
#: these carry a live cross-check; the unavailable ones are asserted separately as documented gaps.
COMPARABLE_CASES: list[GoldenCase] = [c for c in CASES if c.case_id not in REFERENCE_A_UNAVAILABLE]
UNAVAILABLE_CASES: list[GoldenCase] = [c for c in CASES if c.case_id in REFERENCE_A_UNAVAILABLE]

#: Action types whose engine ``price_factor`` differs from 1 — what a factor cross-check pins.
_SCALING_TYPES = frozenset({ActionType.SPLIT, ActionType.BONUS})
#: Structural breaks Yahoo *cannot* model (carve-outs onto another ISIN) — the documented-bad set.
_CARVE_OUT_TYPES = frozenset({ActionType.DEMERGER, ActionType.DVR_CONVERSION})


def _case_id(case: GoldenCase) -> str:
    return case.case_id


def _primary_action_type(case: GoldenCase) -> ActionType:
    """The case's single defining action type (each golden case models exactly one event)."""
    types = {a.action_type for a in case.actions}
    assert len(types) == 1, (
        f"{case.case_id}: expected one action type, got {sorted(t.value for t in types)}"
    )
    return next(iter(types))


def _ex_date(case: GoldenCase) -> date:
    """The single ex-date under test for this case."""
    chain = build_case_chain(case)
    assert len(chain.rows) == 1, f"{case.case_id}: expected one ex-date, got {len(chain.rows)}"
    return chain.rows[0].ex_date


def _our_price_factor(case: GoldenCase) -> Decimal:
    """Our engine's ``price_factor`` at the case's ex-date (reference B's factor)."""
    return build_case_chain(case).rows[0].price_factor


# ── fixture shape and IO ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class YahooSeries:
    """One cached yfinance window: the adjusted-close series plus the actions Yahoo gives it.

    ``rows`` are ``(date, close, adj_close)`` with prices as ``Decimal`` (money is never ``float``);
    ``splits`` maps an ex-date to Yahoo's split multiplier; ``dividends`` maps a date to a cash
    amount. This is exactly what ``history(actions=True)`` yields for the `.NS` ticker, frozen so
    the comparison is reproducible offline.
    """

    case_id: str
    ticker: str
    rows: tuple[tuple[date, Decimal, Decimal], ...]
    splits: dict[date, Decimal]
    dividends: dict[date, Decimal]
    available: bool = True
    unavailable_reason: str = ""

    def adj_close_on_or_after(self, day: date) -> tuple[date, Decimal]:
        """First (date, adj_close) on or after ``day`` — the post-event anchor."""
        for d, _close, adj in self.rows:
            if d >= day:
                return d, adj
        raise AssertionError(f"{self.case_id}: no cached row on or after {day.isoformat()}")

    def adj_close_strictly_before(self, day: date) -> tuple[date, Decimal]:
        """Last (date, adj_close) strictly before ``day`` — the pre-event anchor."""
        candidates = [(d, adj) for d, _close, adj in self.rows if d < day]
        assert candidates, f"{self.case_id}: no cached row before {day.isoformat()}"
        return candidates[-1]


def _fixture_path(case_id: str) -> Path:
    return FIXTURE_DIR / f"{case_id}.json"


def _load_fixture(case_id: str) -> YahooSeries:
    """Load a cached yfinance window. Fails loudly (never skips) if the fixture is missing."""
    path = _fixture_path(case_id)
    assert path.exists(), (
        f"missing cached yfinance fixture {path}; regenerate with "
        "`uv run pytest tests/golden/test_yfinance_reference.py -m network`"
    )
    raw = json.loads(path.read_text())
    rows = tuple(
        (date.fromisoformat(r["date"]), Decimal(r["close"]), Decimal(r["adj_close"]))
        for r in raw["rows"]
    )
    splits = {date.fromisoformat(k): Decimal(v) for k, v in raw.get("splits", {}).items()}
    dividends = {date.fromisoformat(k): Decimal(v) for k, v in raw.get("dividends", {}).items()}
    return YahooSeries(
        raw["case_id"],
        raw["ticker"],
        rows,
        splits,
        dividends,
        available=raw.get("available", True),
        unavailable_reason=raw.get("unavailable_reason", ""),
    )


# ── the offline comparisons (run in CI) ────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_case_has_cached_yfinance_record(case: GoldenCase) -> None:
    """Acceptance 1 (precondition): every golden case has a cached reference-A record to compare to.

    A case with a ticker but no fixture is a hole in the cross-check, so this fails loudly rather
    than skipping — an absent reference A must be visible, not silently green. A record is either a
    real series (``available``) or the documented unavailability marker for a verified Yahoo gap.
    """
    assert case.case_id in CASE_TICKERS, f"{case.case_id} has no `.NS` ticker mapping"
    series = _load_fixture(case.case_id)
    assert series.ticker == CASE_TICKERS[case.case_id]
    if case.case_id in REFERENCE_A_UNAVAILABLE:
        assert not series.available, f"{case.case_id}: expected an unavailability marker"
    else:
        assert series.available, f"{case.case_id}: reference A unexpectedly marked unavailable"
        assert series.rows, f"{case.case_id}: cached yfinance window is empty"


@pytest.mark.parametrize("case", UNAVAILABLE_CASES, ids=_case_id)
def test_reference_a_unavailable_is_documented(case: GoldenCase) -> None:
    """Acceptance 1 & 3 for the verified Yahoo gap: the missing reference A is documented.

    Where Yahoo serves no data for a case's `.NS` ticker there is nothing to compare against. Rather
    than fabricate a series (which would destroy the independence the cross-check exists for) or
    silently skip, the case carries a cached unavailability marker whose reason matches the checked-
    in verdict, and that verdict makes reference B authoritative. This asserts marker and verdict
    agree, so the gap can never quietly turn into a fabricated pass.
    """
    verdict = REFERENCE_A_UNAVAILABLE[case.case_id]
    assert "authoritative" in verdict.lower()
    series = _load_fixture(case.case_id)
    assert not series.available
    assert not series.rows, f"{case.case_id}: an 'unavailable' marker must carry no fabricated rows"
    assert series.unavailable_reason == verdict, (
        f"{case.case_id}: cached unavailability reason drifted from the documented verdict"
    )


@pytest.mark.parametrize("case", COMPARABLE_CASES, ids=_case_id)
def test_ex_date_factor_agrees_with_yfinance(case: GoldenCase) -> None:
    """Acceptance 1 (core): our ex-date adjustment factor matches yfinance's within FACTOR_TOL.

    Split/bonus: Yahoo's split multiplier must be the reciprocal of our ``price_factor``, pinning
    magnitude *and* direction. Merger: no split is due, both factors are 1. Carve-outs are handled
    by ``test_documented_disagreements`` and skipped here — forcing a factor match there would be
    exactly the "bend our output to match yfinance" the spec forbids.
    """
    action_type = _primary_action_type(case)
    if action_type in _CARVE_OUT_TYPES:
        pytest.skip(f"{case.case_id}: documented disagreement (see test_documented_disagreements)")

    series = _load_fixture(case.case_id)
    ex = _ex_date(case)
    ours = _our_price_factor(case)

    multiplier = series.splits.get(ex)
    if action_type in _SCALING_TYPES:
        assert multiplier is not None, (
            f"{case.case_id}: yfinance recorded no split on the ex-date {ex.isoformat()}, but "
            f"reference B expects a scaling of {ours}"
        )
        yf_factor = Decimal(1) / multiplier
    else:  # MERGER — surviving entity, no scaling due
        assert multiplier is None, (
            f"{case.case_id}: yfinance recorded a split {multiplier} on a merger ex-date; "
            "reference B treats a surviving-entity merger as a unit-factor structural break"
        )
        yf_factor = Decimal(1)

    rel = abs(ours - yf_factor) / ours
    assert rel <= FACTOR_TOL, (
        f"{case.case_id} {ex.isoformat()}: factor mismatch — reference B {ours}, "
        f"reference A (yfinance) {yf_factor}, relative diff {rel} > tol {FACTOR_TOL}"
    )


@pytest.mark.parametrize("case", COMPARABLE_CASES, ids=_case_id)
def test_yfinance_adjusted_series_is_continuous_across_scaling(case: GoldenCase) -> None:
    """Acceptance 1 (direction guard): Yahoo's adjusted series shows only an organic move at the ex.

    For a split/bonus/merger, a correctly adjusted series is continuous across the ex-date. If Yahoo
    had *not* applied the split, or applied it in the wrong direction, the crossing would show a
    cliff far outside CONTINUITY_CEILING. This is the series-level counterpart to the factor check:
    together they rule out a shared-direction error surviving the cross-check. Carve-outs are
    excluded — their (correct-for-us) discontinuity is the documented disagreement.
    """
    action_type = _primary_action_type(case)
    if action_type in _CARVE_OUT_TYPES:
        pytest.skip(f"{case.case_id}: carve-out; discontinuity is expected and documented")

    series = _load_fixture(case.case_id)
    ex = _ex_date(case)
    _pre_day, pre = series.adj_close_strictly_before(ex)
    _ex_day, at_ex = series.adj_close_on_or_after(ex)
    move = abs(at_ex / pre - Decimal(1))
    assert move <= CONTINUITY_CEILING, (
        f"{case.case_id}: yfinance adjusted close jumps {move:.1%} across the ex-date "
        f"({pre} → {at_ex}); a properly adjusted split/bonus/merger stays within "
        f"{CONTINUITY_CEILING:.0%}, so yfinance did not apply the same adjustment reference B did"
    )


@pytest.mark.parametrize(
    "case",
    [c for c in COMPARABLE_CASES if _primary_action_type(c) in _CARVE_OUT_TYPES],
    ids=_case_id,
)
def test_documented_disagreements(case: GoldenCase) -> None:
    """Acceptance 3: each carve-out disagreement is documented with a reasoned verdict.

    The assertion makes the documentation load-bearing rather than decorative:

    * yfinance records **no** corporate action (no split, no dividend) on the ex-date — it does not
      model the carve-out at all;
    * yfinance's adjusted-close series therefore yields a **finite** return across the crossing,
      whereas reference B **bridges** it (``ret=None``);
    * a ``DISCREPANCIES`` entry states the reason and the verdict (reference B authoritative).
    """
    disc = DISCREPANCIES.get(case.case_id)
    assert disc is not None, f"{case.case_id}: carve-out has no documented discrepancy/verdict"
    assert "Reference B authoritative" in disc.verdict

    series = _load_fixture(case.case_id)
    ex = _ex_date(case)

    assert ex not in series.splits, (
        f"{case.case_id}: yfinance unexpectedly recorded a split on a carve-out ex-date — "
        "revisit the discrepancy verdict"
    )
    assert ex not in series.dividends

    # yfinance produces a concrete, finite return across the crossing (it does not bridge the
    # break) — reference A yields a value exactly where reference B yields None.
    pre_day, pre = series.adj_close_strictly_before(ex)
    _ex_day, at_ex = series.adj_close_on_or_after(ex)
    yf_return = at_ex / pre - Decimal(1)
    assert yf_return.is_finite()

    # ...whereas reference B bridges exactly this ex-date (ret=None), which is the disagreement.
    chain: FactorChain = build_case_chain(case)
    assert ex in chain.structural_break_dates()
    rets = {r.date: r for r in return_series(chain, case.price_points())}
    assert rets[ex].bridged is True
    assert rets[ex].ret is None, (
        f"{case.case_id}: reference B must bridge the structural break at {ex.isoformat()}; "
        f"yfinance instead reports a {yf_return:.2%} return across {pre_day.isoformat()}→ex"
    )


# ── the live pull (opt-in; regenerates the fixtures) ────────────────────────────────────────────


def _fetch_case_series(case_id: str, ticker: str, ex: date) -> YahooSeries:
    """Fetch one case's yfinance window. Network-only; imported lazily to keep offline runs clean.

    Assumes network access and that Yahoo serves the `.NS` ticker. Never called by the offline
    suite — only by ``test_refresh_fixtures_from_yfinance`` under ``-m network``.
    """
    import yfinance as yf

    start = (ex - timedelta(days=_WINDOW_DAYS)).isoformat()
    end = (ex + timedelta(days=_WINDOW_DAYS)).isoformat()
    ticker_obj = yf.Ticker(ticker)
    frame = ticker_obj.history(start=start, end=end, auto_adjust=False, actions=True)
    if len(frame) == 0:
        raise RuntimeError(f"{case_id}: yfinance returned no rows for {ticker} {start}..{end}")

    rows = tuple(
        (idx.date(), Decimal(str(row["Close"])), Decimal(str(row["Adj Close"])))
        for idx, row in frame.iterrows()
    )
    splits_series = ticker_obj.splits
    splits = {
        ts.date(): Decimal(str(mult))
        for ts, mult in (splits_series.items() if splits_series is not None else [])
        if start <= ts.date().isoformat() <= end
    }
    dividends_series = ticker_obj.dividends
    dividends = {
        ts.date(): Decimal(str(amt))
        for ts, amt in (dividends_series.items() if dividends_series is not None else [])
        if start <= ts.date().isoformat() <= end
    }
    return YahooSeries(case_id, ticker, rows, splits, dividends)


def _write_fixture(series: YahooSeries) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "case_id": series.case_id,
        "ticker": series.ticker,
        "source": "yfinance / Yahoo Finance (reference A, EXECUTION_PLAN §4.3, B2)",
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "available": series.available,
        "unavailable_reason": series.unavailable_reason,
        "rows": [
            {"date": d.isoformat(), "close": str(c), "adj_close": str(a)} for d, c, a in series.rows
        ],
        "splits": {d.isoformat(): str(v) for d, v in series.splits.items()},
        "dividends": {d.isoformat(): str(v) for d, v in series.dividends.items()},
    }
    _fixture_path(series.case_id).write_text(json.dumps(payload, indent=2) + "\n")


@pytest.mark.network
@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_refresh_fixtures_from_yfinance(case: GoldenCase) -> None:
    """Opt-in live pull: refresh a case's cached yfinance window from Yahoo.

    Skipped unless selected with ``-m network`` (``tests/golden/conftest.py``), so the default suite
    and ``make check`` stay offline (AGENTIC_CONTEXT B8). Running it rewrites the fixture;
    review the diff before committing.

    A case listed in ``REFERENCE_A_UNAVAILABLE`` is *expected* to return nothing; if Yahoo still
    serves no data, an unavailability marker is written. If it unexpectedly returns data, that is a
    signal the gap has closed — the fixture is written as a real series and the offline
    unavailability test will then fail, prompting removal from ``REFERENCE_A_UNAVAILABLE``.
    """
    ticker = CASE_TICKERS[case.case_id]
    known_unavailable = case.case_id in REFERENCE_A_UNAVAILABLE
    try:
        series = _fetch_case_series(case.case_id, ticker, _ex_date(case))
    except RuntimeError:
        if not known_unavailable:
            raise
        _write_fixture(
            YahooSeries(
                case_id=case.case_id,
                ticker=ticker,
                rows=(),
                splits={},
                dividends={},
                available=False,
                unavailable_reason=REFERENCE_A_UNAVAILABLE[case.case_id],
            )
        )
        return
    _write_fixture(series)
    assert series.rows
