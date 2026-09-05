"""M10.5 — derived fundamental metrics are point-in-time, and say why when they cannot be.

Acceptance, each pinned with a case that fails if the logic is inverted:

1. **Only filings knowable on the as-of date enter a metric.** A fact filed after `as_of` raises
   `PitError` — the leak is named, not filtered (invariant #7). A restatement is invisible until its
   own filing date: the same period read as-of the day before the restatement shows the original,
   as-of the day after shows the restated figure, and the two are never blended (invariant #8).
2. **P/E joins trailing earnings to the as-of price on ISIN, via market capitalisation**, so a split
   between two filings (shares double, price halves) leaves it unchanged; earnings growth is YoY on
   the same period nature (a quarter against the same quarter a year earlier, never against an
   annual figure).
3. **A metric that needs the balance sheet says `BALANCE_SHEET_ABSENT`** — P/B and ROE are never
   invented from P&L data — and every other impossibility is a stated `Unavailable` reason.

Also pinned: TTM sums exactly four *consecutive* quarters (a gap is a gap, an annual fact is never
summed in), consolidated is preferred with a standalone fallback, and the owners' share of profit
is used only when every quarter states it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest

from dataplatform.ingest.xbrl import Nature
from dataplatform.query.fundamentals_metrics import (
    FundamentalMetrics,
    Unavailable,
    compute_metrics,
)
from dataplatform.query.pit import PitError

ISIN = "INE002A01018"
OTHER = "INE009A01021"
_D = Decimal

# Eight consecutive quarter ends, FY2023-24 and FY2024-25.
_Q = [
    date(2023, 6, 30),
    date(2023, 9, 30),
    date(2023, 12, 31),
    date(2024, 3, 31),
    date(2024, 6, 30),
    date(2024, 9, 30),
    date(2024, 12, 31),
    date(2025, 3, 31),
]


def _start_of(quarter_end: date) -> date:
    month = quarter_end.month - 2
    return date(quarter_end.year, month, 1)


@dataclass(frozen=True, slots=True)
class _Row:
    """A lightweight `FactRow` — the shape the metrics module reads, without the pydantic model."""

    isin: str
    period_start: date | None
    period_end: date
    filing_date: date
    nature: Nature
    concept: str
    value: Decimal
    segment: str | None = None
    filing_id: str = ""

    def __post_init__(self) -> None:
        if not self.filing_id:
            # One filing per (period, nature, filing date) unless the test says otherwise.
            object.__setattr__(
                self, "filing_id", f"F-{self.period_end}-{self.nature}-{self.filing_date}"
            )


def _q(
    concept: str,
    quarter_end: date,
    value: str,
    *,
    filed_after_days: int = 45,
    nature: Nature = Nature.CONSOLIDATED,
    isin: str = ISIN,
    filing_date: date | None = None,
) -> _Row:
    return _Row(
        isin=isin,
        period_start=_start_of(quarter_end),
        period_end=quarter_end,
        filing_date=filing_date or (quarter_end + timedelta(days=filed_after_days)),
        nature=nature,
        concept=concept,
        value=_D(value),
    )


def _annual(
    concept: str, fy_end: date, value: str, *, nature: Nature = Nature.CONSOLIDATED
) -> _Row:
    return _Row(
        isin=ISIN,
        period_start=date(fy_end.year - 1, 4, 1),
        period_end=fy_end,
        filing_date=fy_end + timedelta(days=45),
        nature=nature,
        concept=concept,
        value=_D(value),
    )


def _company(
    *,
    pat: list[str],
    revenue: list[str] | None = None,
    shares: str | None = "1000",
    equity: str | None = None,
    nature: Nature = Nature.CONSOLIDATED,
    paid_up: str | None = "5000",
) -> list[_Row]:
    """A filer with one quarterly fact set per quarter in `_Q[:len(pat)]`."""
    rows: list[_Row] = []
    for i, value in enumerate(pat):
        rows.append(_q("profit_after_tax", _Q[i], value, nature=nature))
        if paid_up is not None:
            rows.append(_q("paid_up_equity_capital", _Q[i], paid_up, nature=nature))
        rows.append(_q("eps_basic", _Q[i], str(_D(value) / _D(shares or "1000")), nature=nature))
        if revenue is not None:
            rows.append(_q("revenue_from_operations", _Q[i], revenue[i], nature=nature))
        if shares is not None:
            rows.append(_q("shares_outstanding", _Q[i], shares, nature=nature))
    if equity is not None:
        rows.append(_annual("shareholders_equity_excl_revaluation", _Q[3], equity, nature=nature))
    return rows


def _one(rows: list[_Row], *, as_of: date, price: str | None = "50") -> FundamentalMetrics:
    prices = {ISIN: _D(price)} if price is not None else None
    result = compute_metrics(rows, as_of=as_of, prices=prices)
    assert ISIN in result
    return result[ISIN]


# ── 1. point-in-time ────────────────────────────────────────────────────────────────────────────


def test_a_fact_filed_after_the_as_of_date_raises_rather_than_being_dropped() -> None:
    rows = _company(pat=["10", "10", "10", "10", "10"])
    as_of = _Q[3] + timedelta(days=45)  # the fifth quarter's filing is 45 days after _Q[4] > as_of
    with pytest.raises(PitError, match="not knowable"):
        compute_metrics(rows, as_of=as_of, prices={ISIN: _D("50")})
    # The same rows, restricted to what was knowable, compute fine — the guard is the only barrier.
    knowable = [r for r in rows if r.filing_date <= as_of]
    assert compute_metrics(knowable, as_of=as_of, prices={ISIN: _D("50")})[ISIN].quarters == 4


def test_a_restatement_is_invisible_until_its_own_filing_date() -> None:
    rows = _company(pat=["10", "10", "10", "10"])
    original_filing = _Q[0] + timedelta(days=45)
    restated_filing = _Q[3] + timedelta(days=45)  # Q1 restated a year later, alongside Q4
    rows.append(_q("profit_after_tax", _Q[0], "40", filing_date=restated_filing))  # Q1 PAT 10 -> 40
    before = _one(
        [r for r in rows if r.filing_date < restated_filing],
        as_of=restated_filing - timedelta(days=1),
    )
    after = _one(rows, as_of=restated_filing)
    assert before.earnings_ttm == Unavailable.INSUFFICIENT_QUARTERS  # only 3 quarters knowable
    assert after.earnings_ttm == _D("70")  # 40 + 10 + 10 + 10 — restated Q1, never the original
    assert original_filing < restated_filing


def test_the_original_figure_stands_before_the_restatement_and_is_replaced_after_it() -> None:
    rows = _company(pat=["10", "10", "10", "10", "10"])
    restated_filing = _Q[4] + timedelta(days=45)
    rows.append(_q("profit_after_tax", _Q[1], "100", filing_date=restated_filing))
    day_before = restated_filing - timedelta(days=1)
    before = _one([r for r in rows if r.filing_date <= day_before], as_of=day_before)
    after = _one(rows, as_of=restated_filing)
    assert before.earnings_ttm == _D("40")  # Q1..Q4 at 10 each
    assert after.earnings_ttm == _D("130")  # Q2 restated to 100, plus Q3, Q4, Q5 at 10


def test_knowable_date_never_exceeds_as_of() -> None:
    rows = _company(pat=["10", "10", "10", "10"])
    as_of = _Q[3] + timedelta(days=60)
    m = _one(rows, as_of=as_of)
    assert m.knowable_date <= as_of
    assert m.knowable_date == _Q[3] + timedelta(days=45)
    assert m.days_since_filing() == 15


# ── 2. valuation and growth ─────────────────────────────────────────────────────────────────────


def test_pe_is_market_cap_over_trailing_earnings_on_isin() -> None:
    rows = _company(pat=["10", "20", "30", "40"], shares="1000")
    m = _one(rows, as_of=_Q[3] + timedelta(days=60), price="50")
    assert m.market_cap == _D("50000")
    assert m.earnings_ttm == _D("100")
    assert m.pe_ttm == _D("500")
    assert m.earnings_yield == _D("0.002")


def test_pe_is_unchanged_by_a_split_between_filings_inversion() -> None:
    """Shares double and the price halves: market cap and P/E are the same; summed EPS is not."""
    rows = _company(pat=["10", "10", "10", "10"], shares="1000")
    # A 1:2 split before the fifth filing: shares 2000 from here on, EPS per share halves.
    rows.append(_q("profit_after_tax", _Q[4], "10"))
    rows.append(_q("shares_outstanding", _Q[4], "2000"))
    rows.append(_q("eps_basic", _Q[4], "0.005"))
    before = _one(
        [r for r in rows if r.period_end <= _Q[3]], as_of=_Q[3] + timedelta(days=60), price="50"
    )
    after = _one(rows, as_of=_Q[4] + timedelta(days=60), price="25")
    assert before.market_cap == after.market_cap == _D("50000")
    assert before.pe_ttm == after.pe_ttm == _D("1250")
    # The per-share TTM straddles the split and is *not* comparable — so P/E does not use it.
    assert before.eps_ttm == _D("0.04")
    assert after.eps_ttm == _D("0.035")


def test_yoy_growth_is_quarter_against_the_same_quarter_last_year_inversion() -> None:
    rows = _company(pat=["10", "10", "10", "10", "15"])
    m = _one(rows, as_of=_Q[4] + timedelta(days=60))
    # Latest quarter is _Q[4] (15) against _Q[0] (10): +50%. Reversed, it would be -33%.
    assert m.earnings_yoy == _D("0.5")
    assert isinstance(m.earnings_yoy, Decimal) and m.earnings_yoy > _ZERO_D


_ZERO_D = _D("0")


def test_yoy_with_a_non_positive_base_is_stated_not_computed() -> None:
    rows = _company(pat=["-5", "10", "10", "10", "20"])
    m = _one(rows, as_of=_Q[4] + timedelta(days=60))
    assert m.earnings_yoy == Unavailable.NEGATIVE_BASE


def test_yoy_needs_the_prior_year_quarter() -> None:
    rows = _company(pat=["10", "10", "10"])
    m = _one(rows, as_of=_Q[2] + timedelta(days=60))
    assert m.earnings_yoy == Unavailable.NO_PRIOR_YEAR


def test_ttm_yoy_compares_two_full_trailing_years() -> None:
    rows = _company(pat=["10", "10", "10", "10", "20", "20", "20", "20"])
    m = _one(rows, as_of=_Q[7] + timedelta(days=60))
    assert m.earnings_ttm == _D("80")
    assert m.earnings_ttm_yoy == _D("1")  # 80 vs 40


def test_net_margin_and_its_trend() -> None:
    rows = _company(
        pat=["10", "10", "10", "10", "20", "20", "20", "20"],
        revenue=["100", "100", "100", "100", "100", "100", "100", "100"],
    )
    m = _one(rows, as_of=_Q[7] + timedelta(days=60))
    assert m.net_margin_ttm == _D("0.2")
    assert m.net_margin_trend == _D("0.1")  # 20% now vs 10% a year earlier


# ── 3. what cannot be computed is said, never invented ──────────────────────────────────────────


def test_pb_and_roe_without_a_balance_sheet_are_marked_absent_not_zero() -> None:
    rows = _company(pat=["10", "10", "10", "10"], equity=None)
    m = _one(rows, as_of=_Q[3] + timedelta(days=60))
    assert m.book_value == Unavailable.BALANCE_SHEET_ABSENT
    assert m.pb == Unavailable.BALANCE_SHEET_ABSENT
    assert m.roe == Unavailable.BALANCE_SHEET_ABSENT


def test_pb_and_roe_with_an_annual_equity_figure() -> None:
    rows = _company(pat=["10", "10", "10", "10"], shares="1000", equity="400")
    m = _one(rows, as_of=_Q[3] + timedelta(days=60), price="50")
    assert m.book_value == _D("400")
    assert m.pb == _D("125")  # 50,000 / 400
    assert m.roe == _D("0.1")  # 40 / 400


def test_negative_equity_is_a_stated_reason() -> None:
    rows = _company(pat=["10", "10", "10", "10"], equity="-5")
    m = _one(rows, as_of=_Q[3] + timedelta(days=60))
    assert m.pb == Unavailable.NON_POSITIVE_EQUITY
    assert m.roe == Unavailable.NON_POSITIVE_EQUITY


def test_loss_making_ttm_has_no_pe() -> None:
    rows = _company(pat=["-10", "-10", "5", "5"])
    m = _one(rows, as_of=_Q[3] + timedelta(days=60))
    assert m.earnings_ttm == _D("-10")
    assert m.pe_ttm == Unavailable.NON_POSITIVE_EARNINGS
    assert isinstance(m.earnings_yield, Decimal) and m.earnings_yield < _ZERO_D


def test_no_price_and_no_share_count_are_stated() -> None:
    rows = _company(pat=["10", "10", "10", "10"], shares=None)
    no_shares = _one(rows, as_of=_Q[3] + timedelta(days=60), price="50")
    assert no_shares.shares_outstanding == Unavailable.NO_SHARE_COUNT
    assert no_shares.pe_ttm == Unavailable.NO_SHARE_COUNT
    no_price = _one(
        _company(pat=["10", "10", "10", "10"]), as_of=_Q[3] + timedelta(days=60), price=None
    )
    assert no_price.market_cap == Unavailable.NO_PRICE
    assert no_price.pe_ttm == Unavailable.NO_PRICE


def test_ttm_needs_four_consecutive_quarters_and_never_sums_an_annual_fact() -> None:
    three = _one(_company(pat=["10", "10", "10"]), as_of=_Q[2] + timedelta(days=60))
    assert three.earnings_ttm == Unavailable.INSUFFICIENT_QUARTERS

    gapped = _company(pat=["10", "10", "10", "10"])
    gapped = [r for r in gapped if r.period_end != _Q[1]]  # Q2 never filed
    gapped.append(_q("profit_after_tax", _Q[4], "10"))
    gapped.append(_q("eps_basic", _Q[4], "0.01"))
    gapped.append(_q("shares_outstanding", _Q[4], "1000"))
    m = _one(gapped, as_of=_Q[4] + timedelta(days=60))
    assert m.earnings_ttm == Unavailable.QUARTER_GAP

    with_annual = _company(pat=["10", "10", "10", "10"])
    with_annual.append(_annual("profit_after_tax", _Q[3], "40"))
    m2 = _one(with_annual, as_of=_Q[3] + timedelta(days=60))
    assert m2.earnings_ttm == _D("40")  # four quarters; the annual figure is not a fifth quarter
    assert m2.quarters == 4


def test_consolidated_is_preferred_with_a_standalone_fallback_and_never_mixed() -> None:
    consolidated = _company(pat=["10", "10", "10", "10"], nature=Nature.CONSOLIDATED)
    standalone = _company(pat=["1", "1", "1", "1"], nature=Nature.STANDALONE)
    both = _one(consolidated + standalone, as_of=_Q[3] + timedelta(days=60))
    assert both.nature is Nature.CONSOLIDATED
    assert both.earnings_ttm == _D("40")
    only_standalone = _one(standalone, as_of=_Q[3] + timedelta(days=60))
    assert only_standalone.nature is Nature.STANDALONE
    assert only_standalone.earnings_ttm == _D("4")


def test_owners_share_is_used_only_when_every_quarter_states_it() -> None:
    rows = _company(pat=["10", "10", "10", "10"])
    rows += [_q("profit_attributable_to_owners", q, "8") for q in _Q[:4]]
    m = _one(rows, as_of=_Q[3] + timedelta(days=60))
    assert m.earnings_ttm == _D("32")
    partial = _company(pat=["10", "10", "10", "10"])
    partial += [_q("profit_attributable_to_owners", q, "8") for q in _Q[:3]]  # Q4 missing
    m2 = _one(partial, as_of=_Q[3] + timedelta(days=60))
    assert m2.earnings_ttm == _D("40")


def test_segment_facts_and_other_isins_do_not_bleed_in() -> None:
    rows = _company(pat=["10", "10", "10", "10"])
    rows.append(
        _Row(
            isin=ISIN,
            period_start=_start_of(_Q[3]),
            period_end=_Q[3],
            filing_date=_Q[3] + timedelta(days=45),
            nature=Nature.CONSOLIDATED,
            concept="profit_after_tax",
            value=_D("999"),
            segment="Retail",
        )
    )
    rows += [_q("profit_after_tax", q, "7", isin=OTHER) for q in _Q[:4]]
    prices = {ISIN: _D("50"), OTHER: _D("20")}
    result = compute_metrics(rows, as_of=_Q[3] + timedelta(days=60), prices=prices)
    assert result[ISIN].earnings_ttm == _D("40")  # the 999 segment fact never entered
    assert result[OTHER].earnings_ttm == _D("28")
    assert result[OTHER].pe_ttm == Unavailable.NO_SHARE_COUNT  # priced, but no share count filed


def test_every_metric_field_is_a_decimal_or_a_stated_reason() -> None:
    rows = _company(pat=["10", "10", "10", "10"], revenue=["100"] * 4, equity="400")
    m = _one(rows, as_of=_Q[3] + timedelta(days=60))
    for name in (
        "revenue_ttm",
        "earnings_ttm",
        "eps_ttm",
        "revenue_yoy",
        "earnings_yoy",
        "earnings_ttm_yoy",
        "net_margin_ttm",
        "net_margin_trend",
        "shares_outstanding",
        "market_cap",
        "pe_ttm",
        "earnings_yield",
        "book_value",
        "pb",
        "roe",
    ):
        value = getattr(m, name)
        assert isinstance(value, Decimal | Unavailable), (name, value)


# ── 4. a filing stated at the wrong scale is excluded whole ─────────────────────────────────────


def test_a_filing_100x_off_in_paid_up_capital_is_excluded_from_every_metric_inversion() -> None:
    """PFC Dec 2023: every figure 100x too small. Without the guard the TTM is wrong by ~25%."""
    rows = _company(pat=["100", "100", "100", "100"], paid_up="5000")
    # The third filing is a scale slip: paid-up 50 (100x too small) and PAT 1 (100x too small).
    slipped = [r for r in rows if r.period_end == _Q[2]]
    rows = [r for r in rows if r.period_end != _Q[2]]
    for r in slipped:
        value = (
            r.value / 100
            if r.concept in ("paid_up_equity_capital", "profit_after_tax")
            else r.value
        )
        rows.append(
            _Row(r.isin, r.period_start, r.period_end, r.filing_date, r.nature, r.concept, value)
        )
    m = _one(rows, as_of=_Q[3] + timedelta(days=60))
    assert m.filings_excluded_scale == 1
    # Q3 is gone: only three quarters remain knowable, so the TTM is stated as unavailable rather
    # than computed from a figure 100x too small (which would have given 301 instead of 400).
    assert m.earnings_ttm == Unavailable.INSUFFICIENT_QUARTERS
    assert m.quarters == 3


def test_a_filing_100x_too_large_is_excluded_too() -> None:
    rows = _company(pat=["100", "100", "100", "100", "100"], paid_up="5000")
    big = [r for r in rows if r.period_end == _Q[4]]
    rows = [r for r in rows if r.period_end != _Q[4]]
    for r in big:
        rows.append(
            _Row(
                r.isin,
                r.period_start,
                r.period_end,
                r.filing_date,
                r.nature,
                r.concept,
                r.value * 100,
            )
        )
    m = _one(rows, as_of=_Q[4] + timedelta(days=60))
    assert m.filings_excluded_scale == 1
    assert m.earnings_ttm == _D("400")  # Q1..Q4 — the 10,000 quarter never entered
    assert m.latest_period_end == _Q[3]


def test_a_genuine_capital_change_is_not_a_scale_error() -> None:
    rows = _company(pat=["100", "100", "100", "100", "100"], paid_up="5000")
    # A rights issue that doubles paid-up capital from Q4 is real, and 2x is not a power of ten.
    rows = [
        _Row(
            r.isin,
            r.period_start,
            r.period_end,
            r.filing_date,
            r.nature,
            r.concept,
            r.value * 2
            if (r.concept == "paid_up_equity_capital" and r.period_end >= _Q[3])
            else r.value,
        )
        for r in rows
    ]
    m = _one(rows, as_of=_Q[4] + timedelta(days=60))
    assert m.filings_excluded_scale == 0
    assert m.earnings_ttm == _D("400")


def test_fewer_than_three_filings_cannot_be_judged_for_scale() -> None:
    rows = _company(pat=["100", "1"], paid_up="5000")
    rows = [
        _Row(
            r.isin,
            r.period_start,
            r.period_end,
            r.filing_date,
            r.nature,
            r.concept,
            r.value / 100
            if (r.concept == "paid_up_equity_capital" and r.period_end == _Q[1])
            else r.value,
        )
        for r in rows
    ]
    m = _one(rows, as_of=_Q[1] + timedelta(days=60))
    assert m.filings_excluded_scale == 0
