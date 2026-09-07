"""M9.2 — the momentum backtest on L2 back-adjusted prices, through the query layer (X2, §7).

Every acceptance criterion of the task is a test here:

  1. the backtest reads back-adjusted closes; a name with a known split no longer shows a fake
     ~-50% twelve-month momentum across its ex-date
     (``test_adjusted_signal_removes_fake_split_momentum``,
     ``test_split_flips_the_top_n_selection``)
  2. the run completes with no PIT violations and a delta (XIRR/turnover/cost) is reportable
     (``test_adjusted_and_raw_runs_complete_and_delta_reports``)
  3. wiping and rebuilding L2 leaves the run digest byte-identical
     (``test_wipe_and_rebuild_l2_keeps_digest_identical``)

The lake is built through the *real* seam: raw ``prices_raw`` L1 partitions under ``tmp_path``,
adjusted to L2 by the M2.5 materializer from an M2.4 factor chain, then read back through
``QueryService`` by the backtest exactly as production will. No postgres, no network, deterministic.

The known split is a 1:2 face-value split (₹10 → ₹5, price factor 0.5) whose ex-date falls inside
one rebalance's trailing-twelve-month window: the reference session is before the split, the current
session after it. On raw closes that reads as a fake -50 % return (the traded price mechanically
halved); on L2 back-adjusted closes both endpoints are in one share basis, so the return is 0 % —
the true move of a company that only split. That flip is the whole point of the task.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.policies.naive_momentum import MomentumParameters
from backtest.run import (
    _AdjustedCloseSource,
    _L1MomentumData,
    _L1Reader,
    _L1SectorRotationData,
    _signal_source_prose,
    render_delta_report,
    run_naive_momentum,
)
from dataplatform.corpactions.factors import FactorChain, build_factor_chain
from dataplatform.corpactions.taxonomy import ActionType, FaceValueTerms
from dataplatform.identity.master import Exchange as IdentityExchange
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.query.service import QueryService
from dataplatform.query.shapes import AdjustedPoint, CrossSection, CrossSectionRequest
from dataplatform.store.l2 import materialize_isin, wipe_adjusted
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

pytestmark = pytest.mark.integration


class _HasMomentum(Protocol):
    """What :func:`_momentum` needs of a signal record — every policy's record carries both."""

    @property
    def isin(self) -> str: ...

    @property
    def momentum(self) -> Decimal: ...


_PRICE_Q: Final = Decimal("0.0001")

# ── the fixture universe ───────────────────────────────────────────────────────────────────────

SPLIT_ISIN: Final = "INE335Y01020"  # the name that splits 1:2 mid-window
SPLIT_EX: Final = date(2023, 9, 1)  # ex-date of the 1:2 face-value split (₹10 → ₹5)

#: Four filler names with no corporate action, so their adjusted series equals their raw series.
#: Their momentum is engineered to bracket the split name's true (0 %) and fake (-50 %) returns.
FILLERS: Final = {
    "INE100A01010": Decimal("900"),  # -10 % over the window
    "INE200A01010": Decimal("800"),  # -20 %
    "INE300A01010": Decimal("700"),  # -30 %
    "INE400A01010": Decimal("600"),  # -40 %
}

#: One session per month (each is therefore a rebalance) plus a trailing session for T+1 fill
#: headroom. The reference for the 2024-02-01 rebalance's 12-month look-back is 2023-02-01 (before
#: the split); the current session is 2024-02-01 (after it), so the window straddles the ex-date.
_SESSIONS: Final = [
    date(2023, 1, 2),
    date(2023, 2, 1),
    date(2023, 3, 1),
    date(2023, 4, 3),
    date(2023, 5, 2),
    date(2023, 6, 1),
    date(2023, 7, 3),
    date(2023, 8, 1),
    SPLIT_EX,
    date(2023, 10, 2),
    date(2023, 11, 1),
    date(2023, 12, 1),
    date(2024, 1, 1),
    date(2024, 2, 1),
    date(2024, 2, 2),  # fill-headroom session, never itself replayed
]

REF_SESSION: Final = date(2023, 2, 1)  # look-back reference for the 2024-02-01 rebalance
REBALANCE: Final = date(2024, 2, 1)  # the rebalance whose window straddles the split
_PRE_SPLIT_CLOSE: Final = Decimal("1000")
_POST_SPLIT_CLOSE: Final = Decimal("500")  # 1000 x 0.5 — the price mechanically halved


def _split_close(session: date) -> Decimal:
    """The split name's raw close: ₹1000 before the ex-date, ₹500 on and after it (a pure split)."""
    return _PRE_SPLIT_CLOSE if session < SPLIT_EX else _POST_SPLIT_CLOSE


def _filler_close(target: Decimal, session: date) -> Decimal:
    """A filler's raw close: ₹1000 through the look-back reference, then its target level after."""
    return _PRE_SPLIT_CLOSE if session <= REF_SESSION else target


def _write_l1_partition(data_root: Path, trade_date: date, rows: list[tuple[str, Decimal]]) -> None:
    """Write one raw NSE ``prices_raw`` L1 partition — rows are ``(isin, close)``.

    OHLC are all the close (all the L2 adjuster and the fill reference read is a price level), the
    volume is a flat 1000 and ``total_traded_value = close * volume`` so every name is liquid enough
    to fill. Uses the real ``PRICES_RAW_SCHEMA`` so the DuckDB read exercises the on-disk contract.
    """
    volume = 1000
    records = [
        {
            "isin": isin,
            "exchange": "NSE",
            "symbol": isin[:6],
            "series": "EQ",
            "trade_date": trade_date,
            "open": close.quantize(_PRICE_Q),
            "high": close.quantize(_PRICE_Q),
            "low": close.quantize(_PRICE_Q),
            "close": close.quantize(_PRICE_Q),
            "last": close.quantize(_PRICE_Q),
            "prev_close": close.quantize(_PRICE_Q),
            "total_traded_qty": volume,
            "total_traded_value": (close * volume).quantize(_PRICE_Q),
            "total_trades": volume,
            "deliv_qty": None,
            "deliv_pct": None,
        }
        for isin, close in rows
    ]
    path = l1_partition_path(PRICES_RAW_DATASET, trade_date, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=PRICES_RAW_SCHEMA)
    pq.write_table(table, path, compression="snappy", version="2.6")


def _split_action() -> CorporateAction:
    """The reconciled 1:2 face-value split on ``SPLIT_ISIN`` (₹10 → ₹5, price factor 0.5)."""
    return CorporateAction(
        isin=SPLIT_ISIN,
        ex_date=SPLIT_EX,
        action_type=ActionType.SPLIT,
        terms=FaceValueTerms(from_value=Decimal("10"), to_value=Decimal("5")),
        source="nse_corp_actions",
        raw_text="FV SPLIT FROM RS.10/- TO RS.5/-",
        knowable_date=SPLIT_EX,
    )


def _materialize_l2(data_root: Path) -> None:
    """Materialize L2 for every fixture name: the split name from its chain, fillers as identity."""
    action = _split_action()
    materialize_isin(
        SPLIT_ISIN,
        chain=build_factor_chain((action,)),
        actions=(action,),
        data_root=data_root,
    )
    for isin in FILLERS:
        materialize_isin(
            isin,
            chain=FactorChain(isin=isin, rows=()),
            actions=(),
            data_root=data_root,
        )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A single-exchange L1 lake with the split name and four fillers, L2 materialized."""
    for session in _SESSIONS:
        rows: list[tuple[str, Decimal]] = [(SPLIT_ISIN, _split_close(session))]
        rows.extend((isin, _filler_close(target, session)) for isin, target in FILLERS.items())
        _write_l1_partition(tmp_path, session, rows)
    _materialize_l2(tmp_path)
    return tmp_path


def _momentum(records: Sequence[_HasMomentum], isin: str) -> Decimal:
    """The momentum of one ISIN in a signal's records (asserts it is present)."""
    for record in records:
        if record.isin == isin:
            return record.momentum
    raise AssertionError(f"{isin} not in the signal records")


# ── acceptance 1: the adjusted signal removes the fake split momentum ────────────────────────────


def test_adjusted_signal_removes_fake_split_momentum(lake: Path) -> None:
    """The split name reads -50 % on raw closes and 0 % on L2 back-adjusted closes.

    Same lake, same rebalance date, same look-back — only the close source differs. Raw closes carry
    the mechanical halving straight into the twelve-month return (fake -50 %); the adjusted signal,
    read through ``QueryService.cross_section``, expresses both endpoints in one basis and reports
    the true 0 %. A test that fails if the adjustment is dropped or the raw path is left in place.
    """
    reader = _L1Reader(data_root=lake)
    try:
        raw_data = _L1MomentumData(reader, _SESSIONS)  # default signal source is raw L1
        raw_records = raw_data.signal(REBALANCE).records

        with QueryService(data_root=lake) as svc:
            adjusted_data = _L1MomentumData(
                reader, _SESSIONS, signal_closes=_AdjustedCloseSource(svc, reader)
            )
            adjusted_records = adjusted_data.signal(REBALANCE).records
    finally:
        reader.close()

    raw_momentum = _momentum(raw_records, SPLIT_ISIN)
    adjusted_momentum = _momentum(adjusted_records, SPLIT_ISIN)

    # Raw: 500 / 1000 - 1 = -0.5, the fake collapse of a name that only split.
    assert raw_momentum == Decimal("-0.5")
    # Adjusted: both endpoints in the post-split basis (500 / 500) - 1 = 0, the true move.
    assert adjusted_momentum == Decimal("0")

    # The sizing price stays the raw current close in both — execution fills on the raw bar.
    raw_price = next(r.price for r in raw_records if r.isin == SPLIT_ISIN)
    adjusted_price = next(r.price for r in adjusted_records if r.isin == SPLIT_ISIN)
    assert raw_price == adjusted_price == _POST_SPLIT_CLOSE


def test_split_flips_the_top_n_selection(lake: Path) -> None:
    """The de-corruption changes the decision: the split name is dropped raw, chosen adjusted.

    Fillers are engineered at -10 % and -20 % (and lower). On raw closes the split name's fake
    -50 % ranks it out of the top-2; on adjusted closes its true 0 % ranks it in. This is the
    behavioural consequence of criterion 1 — the signal fix actually moves the portfolio.
    """
    reader = _L1Reader(data_root=lake)
    try:
        raw_top2 = _top_n_isins(_L1MomentumData(reader, _SESSIONS), REBALANCE, 2)
        with QueryService(data_root=lake) as svc:
            adjusted_top2 = _top_n_isins(
                _L1MomentumData(reader, _SESSIONS, signal_closes=_AdjustedCloseSource(svc, reader)),
                REBALANCE,
                2,
            )
    finally:
        reader.close()

    assert SPLIT_ISIN not in raw_top2
    assert SPLIT_ISIN in adjusted_top2


def _top_n_isins(data: _L1MomentumData, as_of: date, n: int) -> list[str]:
    """The top-``n`` ISINs the policy would hold — highest momentum, ties broken by ISIN."""
    ranked = sorted(data.signal(as_of).records, key=lambda r: (-r.momentum, r.isin))
    return [r.isin for r in ranked[:n]]


# ── acceptance 2: the run completes with no PIT violation, and a delta is reportable ─────────────


def test_adjusted_and_raw_runs_complete_and_delta_reports(lake: Path) -> None:
    """Both runs complete (no ``PitError``) and the delta report renders XIRR/turnover/cost.

    A ``PitError`` anywhere in the walk would raise out of ``run_naive_momentum``; the run
    returning at all is the no-look-ahead evidence (invariant #7). The two runs differ — the split
    flip changes what is bought — so the digests differ and the report has a real delta to state.
    """
    params = MomentumParameters(top_n=2)
    raw = run_naive_momentum(
        start=_SESSIONS[0], end=_SESSIONS[-1], parameters=params, data_root=lake, adjusted=False
    )
    adjusted = run_naive_momentum(
        start=_SESSIONS[0], end=_SESSIONS[-1], parameters=params, data_root=lake, adjusted=True
    )

    # Both walked the full window and journalled every session (invariant #9): one heartbeat or
    # rebalance per replayed session, plus a BUY/SELL entry per order, so the journal is non-empty
    # and at least as long as the session count.
    assert raw.sessions == adjusted.sessions > 0
    assert len(raw.result.journal) >= raw.sessions
    assert len(adjusted.result.journal) >= adjusted.sessions
    assert raw.adjusted is False and adjusted.adjusted is True

    # The split flip changed the portfolio, so the runs are genuinely different.
    assert raw.result.digest() != adjusted.result.digest()

    report = render_delta_report(raw, adjusted)
    assert "M9.2" in report
    assert "Portfolio XIRR" in report
    assert "Turnover" in report
    assert "Total costs" in report
    assert adjusted.result.digest() in report
    # The prose follows the runs: these digests differ, so the report must not claim an empty
    # corporate-action store — which it did, as fixed text, until the server's first real run
    # (2,784 factors) printed "no CAs in the store" under a -0.75 pp delta on 2026-09-07.
    assert "Digests identical:** False" in report
    assert "digests differ" in report
    assert "no CAs in the store" not in report


# ── acceptance 3: wipe + rebuild L2 leaves the run digest byte-identical ─────────────────────────


def test_wipe_and_rebuild_l2_keeps_digest_identical(lake: Path) -> None:
    """L2 is fully recomputable, so a wipe-and-rebuild yields a byte-identical backtest.

    Invariant #3 made operational at the backtest level: L2 holds no primary data, ``wipe_adjusted``
    removes it entirely, ``materialize_isin`` rebuilds it deterministically from L1 + factors, and a
    replay over the rebuilt L2 reproduces the same journal and book — the same digest.
    """
    params = MomentumParameters(top_n=2)
    first = run_naive_momentum(
        start=_SESSIONS[0], end=_SESSIONS[-1], parameters=params, data_root=lake, adjusted=True
    )

    removed = wipe_adjusted(data_root=lake)
    assert removed == 1 + len(FILLERS)  # one partition per materialized ISIN
    _materialize_l2(lake)

    second = run_naive_momentum(
        start=_SESSIONS[0], end=_SESSIONS[-1], parameters=params, data_root=lake, adjusted=True
    )

    assert first.result.digest() == second.result.digest()


# ── the multi-arm report adapters read the same signal source (M9.5/M10.3/M10.6) ─────────────────


def test_sector_rotation_records_rank_on_the_supplied_signal_source(lake: Path) -> None:
    """The sector adapter's momentum follows its ``signal_closes``, and its price stays raw.

    The M10.3 (and by the same seam M9.5/M10.6) report drivers pass one signal source to every arm
    they run, so the arms are comparable and the report can name the source. This pins that the
    adapter honours it: the split name reads its fake -50 % on raw closes and its true 0 % on the
    L2 back-adjusted ones, while the sizing price is the raw post-split close either way
    (invariant #3 — an adjusted series never prices a fill).
    """
    sectors = dict.fromkeys((SPLIT_ISIN, *FILLERS), "Test Industry")
    reader = _L1Reader(data_root=lake)
    try:
        raw = _L1SectorRotationData(reader, _SESSIONS, sectors).signal(REBALANCE).records
        with QueryService(data_root=lake) as svc:
            adjusted = (
                _L1SectorRotationData(
                    reader, _SESSIONS, sectors, signal_closes=_AdjustedCloseSource(svc, reader)
                )
                .signal(REBALANCE)
                .records
            )
    finally:
        reader.close()

    assert _momentum(raw, SPLIT_ISIN) == Decimal("-0.5")
    assert _momentum(adjusted, SPLIT_ISIN) == Decimal("0")
    assert next(r.price for r in raw if r.isin == SPLIT_ISIN) == _POST_SPLIT_CLOSE
    assert next(r.price for r in adjusted if r.isin == SPLIT_ISIN) == _POST_SPLIT_CLOSE


def test_a_report_names_the_signal_source_it_ran_on() -> None:
    """The one sentence every multi-arm report opens its data reality with is not interchangeable.

    Two arms ranked on different closes are not comparable, and a reader cannot tell from the
    numbers which source produced them — so the prose has to say. A report that claimed raw while
    running adjusted (or the reverse) would be the kind of quiet false statement the gate reports
    exist to prevent.
    """
    adjusted, raw = _signal_source_prose(True), _signal_source_prose(False)
    assert "L2 back-adjusted" in adjusted and "adjusted=True" in adjusted
    assert "raw" in raw and "adjusted=False" in raw
    assert adjusted != raw


# ── the delta's two causes are separable (2026-09-07) ────────────────────────────────────────────


class _StitchedService:
    """A ``QueryService`` stand-in whose cross-section carries one ISIN L1 never printed.

    That is the real shape of a stitched L2: a name whose ISIN changed on a face-value split holds
    its predecessor's bars under the *surviving* identity, so L2 answers for an ISIN the session's
    raw L1 partition has only under the retired one. Stubbed rather than built, because
    manufacturing a reissue needs the lineage tables; what is under test here is what
    ``_AdjustedCloseSource`` does with such a row, not how L2 came to hold it.
    """

    #: The surviving identity L2 answers for and L1 (under this identity) does not.
    STITCHED: Final = "INE500A01010"

    def __init__(self, adjusted: Mapping[str, Decimal]) -> None:
        self._adjusted = dict(adjusted)

    def cross_section(self, request: CrossSectionRequest) -> CrossSection:
        rows = tuple(
            AdjustedPoint(
                isin=isin,
                trade_date=request.trade_date,
                exchange=IdentityExchange.NSE,
                primary=IdentityExchange.NSE,
                fell_back=False,
                adj_open=close,
                adj_high=close,
                adj_low=close,
                adj_close=close,
                adj_volume=Decimal("1000"),
                tr_close=close,
                cum_price_factor=Decimal("1"),
                cum_qty_factor=Decimal("1"),
            )
            for isin, close in sorted(self._adjusted.items())
        )
        return CrossSection(trade_date=request.trade_date, rows=rows)

    def close(self) -> None: ...


def test_l1_isins_only_holds_the_candidate_set_to_the_raw_run_s(lake: Path) -> None:
    """The restricted source answers for L1's ISINs only; the open one admits L2's stitched ones.

    Reading L2 changes the price basis *and* the identity coverage, so a raw-vs-adjusted delta
    measured on the open source is a sum of two causes. `l1_isins_only=True` is the arm that
    separates them: same candidates as the raw run, so the difference is the basis alone. A test
    that fails if the restriction stops filtering — or starts dropping the adjusted closes it is
    supposed to keep.
    """
    session = REBALANCE
    reader = _L1Reader(data_root=lake)
    try:
        raw = reader.closes_on(session)
        # L2 answers for every L1 name (halved for the split name) plus one stitched identity.
        service = _StitchedService({**raw, _StitchedService.STITCHED: Decimal("7")})
        open_closes = _AdjustedCloseSource(service, reader)(session)  # type: ignore[arg-type]
        held = _AdjustedCloseSource(service, reader, l1_isins_only=True)(  # type: ignore[arg-type]
            session
        )
    finally:
        reader.close()

    assert _StitchedService.STITCHED in open_closes
    assert _StitchedService.STITCHED not in held
    assert set(held) == set(raw)
    # The restriction drops rows, never rewrites them: every kept ISIN carries L2's close.
    assert all(held[isin] == open_closes[isin] for isin in held)
