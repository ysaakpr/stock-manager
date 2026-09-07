"""M2.4 — the adjustment factor chain, its derived series, and the retroactive recompute seam.

The four things asserted here are the four acceptance criteria, and each is written so that it fails
if the factor logic is *inverted* — the discipline CLAUDE.md demands of anything touching adjustment
factors, because a chain that scales the wrong direction silently rewrites ten years of one ISIN's
history (risk register row 1).

1. **Stacked split/bonus factors compose.** A 1:1 bonus and a later 1:5 split scale a price before
   both by 0.5 x 0.2 = 0.1, and the documented example — a ₹1000 2019 close becoming ₹200 after a
   1:5 split — holds exactly. Inverting either factor moves the number the wrong way and fails.

2. **A newly inserted CA rewrites the ISIN's full history and invalidates L2.** Exercised against
   `_FakeConn`, an in-memory stand-in speaking the SQL the recompute issues: recompute deletes and
   re-inserts the *whole* `adjustment_factors` chain and writes one open `l2_invalidation` row;
   inserting a second reconciled CA and recomputing changes every cumulative factor (the rewrite is
   whole, not a patch) and does not stack a duplicate invalidation.

3. **A demerger's ex-date gap is not a return.** The return series marks the structural-break
   crossing `bridged` with `ret=None`, while an ordinary large move on a non-break day is still
   reported — so the bridge is specific to structural breaks, not a blanket mute.

4. **Dividends move the total-return series, not the price-adjusted one.** The price-adjusted series
   is identical with and without a dividend; the total-return reinvests it and diverges by the
   dividend; and the two series coincide exactly when there is no dividend.

Offline and deterministic: no network, no Postgres (AGENTIC_CONTEXT B8). The database seam runs
against `_FakeConn`, which recognises exactly the statements `recompute.py` and
`load_reconciled_actions` issue and raises on anything else, so a query that drifts fails loudly
here rather than silently reading nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from dataplatform.clock import FrozenClock
from dataplatform.corpactions import (
    ActionType,
    DividendKind,
    DividendTerms,
    ExchangeRatioTerms,
    FaceValueTerms,
    FactorError,
    PricePoint,
    RatioTerms,
    build_chain_for_isin,
    build_factor_chain,
    price_adjusted_series,
    recompute_isin,
    recompute_isins,
    return_series,
    total_return_series,
)
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.store.db import Connection

NSE = "nse_corp_actions"
BSE = "bse_corp_actions"

INFY = "INE009A01021"
RIL = "INE002A01018"
TCS = "INE467B01029"

NOW = datetime(2026, 9, 2, 9, 30, tzinfo=UTC)


def ca(
    *,
    isin: str = INFY,
    ex_date: date,
    action_type: ActionType,
    terms: object,
    source: str = NSE,
) -> CorporateAction:
    """A `CorporateAction` for the tables below, defaulting the fields the tests do not vary."""
    return CorporateAction(
        isin=isin,
        ex_date=ex_date,
        action_type=action_type,
        terms=cast("Any", terms),
        source=source,
        raw_text=f"{action_type.value} {ex_date.isoformat()}",
        knowable_date=ex_date,
    )


def fv(from_value: str, to_value: str) -> FaceValueTerms:
    return FaceValueTerms(from_value=Decimal(from_value), to_value=Decimal(to_value))


def bonus(new: str, held: str) -> RatioTerms:
    return RatioTerms(new_shares=Decimal(new), held_shares=Decimal(held))


def prices(*pairs: tuple[date, str]) -> list[PricePoint]:
    return [PricePoint(date=d, close=Decimal(c)) for d, c in pairs]


# ── acceptance 1: split/bonus factors compose across stacked actions ─────────────────────────────


def test_stacked_bonus_then_split_compose_by_multiplication() -> None:
    """A 1:1 bonus (2020) then a 1:5 split (2021): a price before both scales by 0.5 x 0.2 = 0.1."""
    b = ca(ex_date=date(2020, 6, 1), action_type=ActionType.BONUS, terms=bonus("1", "1"))
    s = ca(ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=fv("10", "2"))

    chain = build_factor_chain([s, b])  # deliberately out of order — the builder sorts

    assert chain.price_factor_asof(date(2019, 1, 1)) == Decimal("0.1")  # before both
    assert chain.price_factor_asof(date(2020, 7, 1)) == Decimal("0.2")  # after bonus, before split
    assert chain.price_factor_asof(date(2022, 1, 1)) == Decimal("1")  # newest segment unscaled


def test_documented_convention_1_5_split_on_a_2019_close() -> None:
    """The module's worked example: a ₹1000 2019 close reads ₹200 after a 2021 1:5 split.

    This is the convention every downstream consumer relies on; if the split factor were inverted
    (5 instead of 0.2) the adjusted close would be ₹5000, and this fails.
    """
    s = ca(ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=fv("10", "2"))
    chain = build_factor_chain([s])

    series = price_adjusted_series(
        chain, prices((date(2019, 1, 1), "1000"), (date(2022, 1, 1), "250"))
    )
    assert series[0].adj_close == Decimal("200")  # 1000 x 0.2
    assert series[1].adj_close == Decimal("250")  # after the split: unscaled


def test_quantity_factor_is_the_reciprocal_ratio_from_terms() -> None:
    """qty_factor is the exact reciprocal *ratio* of price_factor — cap and turnover survive.

    For terminating ratios (a 10→2 split, a 1:1 bonus) the product is exactly 1. For a
    non-terminating one (a 2:1 bonus's 1/3) exactness would be a lie about `Decimal`, so the
    invariant is tested as "qty is the ratio the terms state", not "price x qty rounds to 1".
    """
    s = ca(ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=fv("10", "2"))
    b11 = ca(ex_date=date(2020, 6, 1), action_type=ActionType.BONUS, terms=bonus("1", "1"))
    chain = build_factor_chain([s, b11])
    for row in chain.rows:  # both ratios terminate, so the product is exactly 1
        assert row.price_factor * row.qty_factor == Decimal("1")

    # A 2:1 bonus triples the share count; its qty factor is exactly 3 even though price is 1/3.
    b21 = ca(
        isin=RIL, ex_date=date(2020, 6, 1), action_type=ActionType.BONUS, terms=bonus("2", "1")
    )
    (row21,) = build_factor_chain([b21]).rows
    assert row21.qty_factor == Decimal("3")
    # Stacked with the split, the pre-both quantity factor is 5 x 3 = 15.
    stacked = build_factor_chain(
        [
            ca(
                isin=RIL,
                ex_date=date(2021, 6, 1),
                action_type=ActionType.SPLIT,
                terms=fv("10", "2"),
            ),
            b21,
        ]
    )
    assert stacked.qty_factor_asof(date(2019, 1, 1)) == Decimal("15")


def test_consolidation_is_a_split_with_a_factor_above_one() -> None:
    """A face-value consolidation (1 → 10) raises the price factor above 1 — direction, made a test.

    A 1-for-10 consolidation turns ten shares into one; the price of the new share is ten times the
    old, so a pre-event price scales by 10 and quantity by 0.1. If SPLIT's from/to were swapped this
    would read 0.1 and fail.
    """
    c = ca(ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=fv("1", "10"))
    chain = build_factor_chain([c])
    assert chain.price_factor_asof(date(2020, 1, 1)) == Decimal("10")
    assert chain.qty_factor_asof(date(2020, 1, 1)) == Decimal("0.1")


def test_same_day_split_and_bonus_compose_into_one_row() -> None:
    """Two actions sharing an ex-date become one factor row (the table's grain) whose factor is the

    product — a split (0.2) and a same-day 1:1 bonus (0.5) give 0.1, in a single row.
    """
    s = ca(ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=fv("10", "2"))
    b = ca(ex_date=date(2021, 6, 1), action_type=ActionType.BONUS, terms=bonus("1", "1"))
    chain = build_factor_chain([s, b])
    assert len(chain.rows) == 1
    assert chain.rows[0].price_factor == Decimal("0.1")
    assert chain.price_factor_asof(date(2020, 1, 1)) == Decimal("0.1")


def test_a_split_without_quantified_terms_raises_rather_than_guessing() -> None:
    """A reconciled SPLIT that never got its face values is a defect the chain will not default."""
    from dataplatform.corpactions import UnquantifiedTerms

    s = ca(ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=UnquantifiedTerms())
    with pytest.raises(FactorError, match="no quantified face values"):
        build_factor_chain([s])


# ── acceptance 3: a demerger's ex-date gap is a structural break, not a return ────────────────────


def test_demerger_gap_produces_no_spurious_return() -> None:
    """The ex-date crossing of a demerger is bridged (ret=None); ordinary days keep real returns."""
    d = ca(
        isin=RIL,
        ex_date=date(2023, 7, 20),
        action_type=ActionType.DEMERGER,
        terms=ExchangeRatioTerms(shares_received=Decimal(1), shares_held=Decimal(1)),
    )
    chain = build_factor_chain([d])
    series = return_series(
        chain,
        prices(
            (date(2023, 7, 18), "2790"),
            (date(2023, 7, 19), "2800"),
            (date(2023, 7, 20), "2600"),  # ~7% structural gap — not a return
            (date(2023, 7, 21), "2620"),
        ),
    )
    by_date = {r.date: r for r in series}

    assert by_date[date(2023, 7, 20)].bridged is True
    assert by_date[date(2023, 7, 20)].ret is None  # the gap is not counted as a -7% return
    # The day before and after are ordinary returns, present and non-bridged.
    assert by_date[date(2023, 7, 19)].bridged is False
    assert by_date[date(2023, 7, 19)].ret is not None
    assert by_date[date(2023, 7, 21)].ret == Decimal("2620") / Decimal("2600") - Decimal("1")


def test_a_structural_break_carries_unit_factors_and_a_marker() -> None:
    """A demerger scales neither price nor quantity; it only marks the break for returns."""
    d = ca(
        isin=RIL,
        ex_date=date(2023, 7, 20),
        action_type=ActionType.DEMERGER,
        terms=ExchangeRatioTerms(shares_received=Decimal(1), shares_held=Decimal(1)),
    )
    chain = build_factor_chain([d])
    (row,) = chain.rows
    assert row.structural_break is True
    assert row.price_factor == Decimal("1")
    assert row.qty_factor == Decimal("1")
    assert chain.structural_break_dates() == frozenset({date(2023, 7, 20)})


# ── acceptance 4: dividends move the total-return series, not the price-adjusted series ───────────


def test_dividend_affects_total_return_but_not_price_adjusted() -> None:
    """Price-adjusted ignores a dividend; total-return reinvests it, diverging by the yield."""
    div = ca(
        isin=RIL,
        ex_date=date(2023, 7, 20),
        action_type=ActionType.DIVIDEND,
        terms=DividendTerms(dividend_kind=DividendKind.FINAL, amount_inr=Decimal("10")),
    )
    px = prices(
        (date(2023, 7, 18), "2790"),
        (date(2023, 7, 19), "2800"),  # cum-dividend close — the reinvestment base
        (date(2023, 7, 20), "2600"),
    )
    padj = price_adjusted_series(build_chain_for_isin(RIL, [div]), px)
    tr = total_return_series([div], px)

    # Price-adjusted: a dividend is not a change in the share basis, so it is the raw series.
    assert [p.adj_close for p in padj] == [Decimal("2790"), Decimal("2800"), Decimal("2600")]

    # Total-return: dates before ex-date scaled by (P - D)/P with P = 2800, D = 10 ⇒ 2790/2800.
    factor = Decimal("2790") / Decimal("2800")
    tr_by_date = {p.date: p.adj_close for p in tr}
    assert tr_by_date[date(2023, 7, 18)] == Decimal("2790") * factor
    assert tr_by_date[date(2023, 7, 19)] == Decimal("2800") * factor
    assert tr_by_date[date(2023, 7, 20)] == Decimal("2600")  # on/after ex-date: unscaled
    # And the dividend genuinely raised the pre-ex total-return level above the price-adjusted one.
    assert tr_by_date[date(2023, 7, 18)] < Decimal("2790")


def test_total_return_equals_price_adjusted_when_there_is_no_dividend() -> None:
    """With only a split and no dividend, the two series coincide exactly (the other half of #4)."""
    s = ca(isin=RIL, ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=fv("10", "2"))
    px = prices((date(2020, 1, 1), "1000"), (date(2022, 1, 1), "250"))
    padj = price_adjusted_series(build_chain_for_isin(RIL, [s]), px)
    tr = total_return_series([s], px)
    assert [p.adj_close for p in padj] == [p.adj_close for p in tr]


def test_percentage_only_dividend_cannot_be_reinvested_and_raises() -> None:
    """A dividend stated only as a % of face value needs a face value this layer will not fetch."""
    div = ca(
        isin=RIL,
        ex_date=date(2023, 7, 20),
        action_type=ActionType.DIVIDEND,
        terms=DividendTerms(dividend_kind=DividendKind.FINAL, percent_of_face_value=Decimal("160")),
    )
    px = prices((date(2023, 7, 19), "2800"), (date(2023, 7, 20), "2600"))
    with pytest.raises(FactorError, match="percentage of face value"):
        total_return_series([div], px)


# ── acceptance 2: recompute rewrites the full chain and invalidates L2 (the database seam) ────────


def test_recompute_writes_full_chain_and_one_l2_invalidation() -> None:
    """Recomputing an ISIN with one split writes its whole chain and flags its L2 stale, once."""
    s = ca(ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=fv("10", "2"))
    conn = _FakeConn(reconciled=[s])
    clock = FrozenClock(NOW)

    result = recompute_isin(cast("Connection", conn), INFY, clock=clock)

    assert result.factor_rows_written == 1
    assert result.l2_invalidated is True
    assert result.invalidation_skipped is False
    # The factor row landed in adjustment_factors with the documented direction.
    (row,) = conn.factors_for(INFY)
    assert row["ex_date"] == date(2021, 6, 1)
    assert row["price_factor"] == Decimal("0.2")
    assert row["structural_break"] is False
    # Exactly one open invalidation for the ISIN, dated to the earliest series ex-date.
    assert conn.open_invalidations(INFY) == [(INFY, date(2021, 6, 1))]


def test_inserting_a_new_ca_rewrites_the_full_history_not_a_patch() -> None:
    """A backfilled earlier split re-scales every existing cumulative factor: a whole rewrite."""
    later = ca(ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=fv("10", "2"))
    conn = _FakeConn(reconciled=[later])
    clock = FrozenClock(NOW)

    first = recompute_isin(cast("Connection", conn), INFY, clock=clock)
    assert first.factor_rows_written == 1
    assert conn.factors_for(INFY)[0]["cum_price_factor"] == Decimal("0.2")

    # A second reconciled action arrives (an earlier bonus) and the ISIN is recomputed.
    conn.add_reconciled(
        ca(ex_date=date(2020, 6, 1), action_type=ActionType.BONUS, terms=bonus("1", "1"))
    )
    second = recompute_isin(cast("Connection", conn), INFY, clock=clock)

    assert second.factor_rows_written == 2  # the chain was rebuilt whole, both events present
    rows = {r["ex_date"]: r for r in conn.factors_for(INFY)}
    # The earlier row now carries cumulative factor 0.5 x 0.2 = 0.1 — history rewritten.
    assert rows[date(2020, 6, 1)]["cum_price_factor"] == Decimal("0.1")
    assert rows[date(2021, 6, 1)]["cum_price_factor"] == Decimal("0.2")
    # The prior open invalidation still stands, so a duplicate is not stacked.
    assert second.l2_invalidated is False
    assert second.invalidation_skipped is True
    assert len(conn.open_invalidations(INFY)) == 1


def test_recompute_reads_only_reconciled_actions() -> None:
    """An unreconciled CA is physically absent from the recomputed chain (invariant, via M2.3)."""
    reconciled_split = ca(
        ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=fv("10", "2")
    )
    unreconciled_bonus = ca(
        ex_date=date(2020, 6, 1), action_type=ActionType.BONUS, terms=bonus("1", "1")
    )
    conn = _FakeConn(reconciled=[reconciled_split], unreconciled=[unreconciled_bonus])

    result = recompute_isin(cast("Connection", conn), INFY, clock=FrozenClock(NOW))

    assert result.factor_rows_written == 1  # only the split; the bonus never reaches a factor
    (row,) = conn.factors_for(INFY)
    assert row["ex_date"] == date(2021, 6, 1)
    assert row["cum_price_factor"] == Decimal("0.2")  # not 0.1 — the bonus was excluded


def test_recompute_with_only_a_dividend_writes_no_factor_rows_but_invalidates_l2() -> None:
    """A dividend moves no price factor, so the chain is empty — yet L2 (its TR series) is stale."""
    div = ca(
        ex_date=date(2023, 7, 20),
        action_type=ActionType.DIVIDEND,
        terms=DividendTerms(dividend_kind=DividendKind.FINAL, amount_inr=Decimal("10")),
    )
    conn = _FakeConn(reconciled=[div])
    result = recompute_isin(cast("Connection", conn), INFY, clock=FrozenClock(NOW))

    assert result.factor_rows_written == 0
    assert conn.factors_for(INFY) == []
    assert result.l2_invalidated is True  # the total-return series depends on the dividend
    assert conn.open_invalidations(INFY) == [(INFY, date(2023, 7, 20))]


def test_recompute_clears_a_stale_row_when_its_action_is_no_longer_reconciled() -> None:
    """If the only reconciled action is withdrawn, recompute leaves an empty chain, no factors."""
    s = ca(ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=fv("10", "2"))
    conn = _FakeConn(reconciled=[s])
    recompute_isin(cast("Connection", conn), INFY, clock=FrozenClock(NOW))
    assert len(conn.factors_for(INFY)) == 1

    conn.unreconcile_all(INFY)
    result = recompute_isin(cast("Connection", conn), INFY, clock=FrozenClock(NOW))
    assert result.factor_rows_written == 0
    assert conn.factors_for(INFY) == []  # the stale row was deleted, not left behind


# ── the in-memory database stand-in ──────────────────────────────────────────────────────────────


class _FakeCursor:
    """A cursor over a fixed result set — only `fetchone`/`fetchall`, which is all callers use."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    """In-memory stand-in for `store.db.Connection` speaking exactly the SQL the recompute seam and

    `load_reconciled_actions` issue: the reconciled-actions SELECT, the `adjustment_factors`
    delete/insert, and the `l2_invalidation` existence check and insert. It is not a Postgres
    emulator — an unrecognised statement raises, so a query that drifts from what the fake models
    fails loudly here. Cast to `Connection` at the call site; no test opens a real database (B8).
    """

    def __init__(
        self,
        *,
        reconciled: Sequence[CorporateAction] = (),
        unreconciled: Sequence[CorporateAction] = (),
    ) -> None:
        self._ca: list[dict[str, Any]] = []
        for action in reconciled:
            self._ca.append({"action": action, "reconciled": True})
        for action in unreconciled:
            self._ca.append({"action": action, "reconciled": False})
        self._factors: dict[tuple[str, date], dict[str, Any]] = {}
        self._invalidations: list[dict[str, Any]] = []

    # -- helpers the tests drive the fake with --------------------------------------------------

    def add_reconciled(self, action: CorporateAction) -> None:
        self._ca.append({"action": action, "reconciled": True})

    def unreconcile_all(self, isin: str) -> None:
        for rec in self._ca:
            if cast("CorporateAction", rec["action"]).isin == isin:
                rec["reconciled"] = False

    def factors_for(self, isin: str) -> list[dict[str, Any]]:
        return [v for (i, _), v in sorted(self._factors.items()) if i == isin]

    def open_invalidations(self, isin: str) -> list[tuple[str, date | None]]:
        return [
            (inv["isin"], inv["from_date"])
            for inv in self._invalidations
            if inv["isin"] == isin and not inv["resolved"]
        ]

    # -- the SQL seam ---------------------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _FakeCursor:
        p = tuple(params)
        if sql.startswith("SELECT isin, ex_date, action_type, ratio_terms"):
            return self._select_reconciled(p)
        if sql.startswith("DELETE FROM adjustment_factors"):
            (isin,) = p
            for key in [k for k in self._factors if k[0] == isin]:
                del self._factors[key]
            return _FakeCursor([])
        if sql.startswith("INSERT INTO adjustment_factors"):
            (isin, ex_date, price_f, qty_f, cum_p, cum_q, ca_id, structural, computed_at) = p
            self._factors[(isin, ex_date)] = {
                "isin": isin,
                "ex_date": ex_date,
                "price_factor": price_f,
                "qty_factor": qty_f,
                "cum_price_factor": cum_p,
                "cum_qty_factor": cum_q,
                "corporate_action_id": ca_id,
                "structural_break": structural,
                "computed_at": computed_at,
            }
            return _FakeCursor([])
        if sql.startswith("SELECT 1 FROM l2_invalidation"):
            (isin,) = p
            hit = any(inv["isin"] == isin and not inv["resolved"] for inv in self._invalidations)
            return _FakeCursor([(1,)] if hit else [])
        if sql.startswith("INSERT INTO l2_invalidation"):
            (isin, reason, from_date, requested_at) = p
            self._invalidations.append(
                {
                    "isin": isin,
                    "reason": reason,
                    "from_date": from_date,
                    "requested_at": requested_at,
                    "resolved": False,
                }
            )
            return _FakeCursor([])
        raise AssertionError(f"_FakeConn does not know this SQL: {sql!r}")

    def _select_reconciled(self, params: tuple[Any, ...]) -> _FakeCursor:
        isin_filter = params[0] if params else None
        rows: list[tuple[Any, ...]] = []
        for rec in self._ca:
            if not rec["reconciled"]:
                continue
            action = cast("CorporateAction", rec["action"])
            if isin_filter is not None and action.isin != isin_filter:
                continue
            rows.append(
                (
                    action.isin,
                    action.ex_date,
                    action.action_type.value,
                    action.ratio_terms_json(),
                    action.record_date,
                    action.announcement_date,
                    action.knowable_date,
                    action.source,
                    action.source_ref,
                    action.raw_text,
                    action.l0_key,
                )
            )
        rows.sort(key=lambda r: (r[0], r[1], r[2], r[7]))
        return _FakeCursor(rows)


def test_one_unbuildable_isin_does_not_abort_the_rest_of_the_batch() -> None:
    """A split nobody quantified costs its own chain, not everybody's.

    This is the shape that discarded an entire overnight reconcile: `recompute_isins` raised out of
    its generator on one BSE split written as bare `Sub Division of Equity shares`, and because the
    finalize runs in a single transaction it rolled back 19,034 reconciled actions and every other
    ISIN's rebuilt chain along with it.
    """
    good = ca(ex_date=date(2021, 6, 1), action_type=ActionType.SPLIT, terms=fv("10", "2"))
    from dataplatform.corpactions import UnquantifiedTerms

    unbuildable = ca(
        isin=RIL,
        ex_date=date(2017, 3, 23),
        action_type=ActionType.SPLIT,
        terms=UnquantifiedTerms(),
    )
    conn = _FakeConn(reconciled=[good, unbuildable])

    results = recompute_isins(cast("Connection", conn), [INFY, RIL], clock=FrozenClock(NOW))

    # The good ISIN is rebuilt and its rows are on the table; the unbuildable one is simply absent.
    assert [r.isin for r in results] == [INFY]
    assert len(conn.factors_for(INFY)) == 1
    assert conn.factors_for(RIL) == []
