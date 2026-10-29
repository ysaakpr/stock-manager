"""An ambiguous master no longer fails a whole delivery file — CCAVENUE, 2026-10-26.

AvenuesAI's ISIN was reissued INE483S01020 → INE483S01038 (switch 2026-10-19), and the identity
master came out of the 2026-10-24 refresh holding two open `CCAVENUE` windows (reconciliation
ids 72/73). Every resolve of `CCAVENUE` then raised `AmbiguousSymbolError`, and because
`delivery.resolve` let it propagate, `nse_delivery` went FAILED for 2026-10-26 on — one symbol of
~3,500 reddening the M17 interlock for the whole session.

Layer 1: the same session's bhavcopy states exactly one ISIN for `(CCAVENUE, EQ)`, and when that
ISIN is one of the master's candidates it places the row. Layer 2: an ambiguity the session cannot
settle is quarantined (`symbol_ambiguous`) and raised as an ERROR quality flag, never guessed —
and past `MAX_AMBIGUOUS_QUARANTINED` rows the file fails, because that is a broken master. The
fixtures are real:
three rows of the 2026-10-26 `sec_bhavdata_full` L0 payload and the matching three rows of that
session's UDiFF bhavcopy, copied byte-for-byte (`tests/fixtures/nse_delivery/PROVENANCE.md`).
Offline: no network, no Postgres.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final, cast

import pyarrow.parquet as pq
import pytest

from analyst.commons.inputs import PriceBar
from analyst.commons.volume_quality import delivery_average
from dataplatform.clock import FrozenClock
from dataplatform.identity.master import (
    AmbiguousSymbolError,
    Exchange,
    IdentityMaster,
    InMemoryReconciliationQueue,
    SymbolWindow,
)
from dataplatform.identity.session import SessionIdentity
from dataplatform.ingest.backfill import (
    AMBIGUOUS_DELIVERY_CHECK,
    NSE_DELIVERY,
    SOURCE_SETS,
    WriteContext,
    ambiguity_findings,
)
from dataplatform.ingest.models import PriceRow
from dataplatform.ingest.nse import bhavcopy_udiff, delivery
from dataplatform.ingest.nse.delivery import (
    MAX_AMBIGUOUS_QUARANTINED,
    AmbiguousDeliveryFileError,
    DeliveryRow,
)
from dataplatform.ingest.source_register import load as load_register
from dataplatform.quality.sentinel import QualityFinding
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import PricesRawWriteReport, read_prices_raw, write_prices_raw
from dataplatform.store.paths import Layer, partition_path
from dataplatform.store.schemas import PRICES_RAW_QUARANTINE_DATASET, PriceQuarantineReason

FIXTURES: Final = (
    Path(__file__).resolve().parents[1] / "fixtures" / "nse_delivery" / "ccavenue_reissue_26102026"
)
DELIVERY_FILE: Final = "sec_bhavdata_full_26102026_EXCERPT.csv"
BHAVCOPY_FILE: Final = "BhavCopy_NSE_CM_0_0_0_20261026_F_0000_EXCERPT.csv"

DAY: Final = date(2026, 10, 26)
#: The two ISINs both open `CCAVENUE` windows name: before and after the 2026-10-19 reissue.
OLD: Final = "INE483S01020"
NEW: Final = "INE483S01038"
TWENTY_MICRONS: Final = "INE144J01027"
THREE_I: Final = "INE748C01038"


def _window(symbol: str, isin: str, valid_from: date = date(2026, 2, 3)) -> SymbolWindow:
    return SymbolWindow(
        exchange=Exchange.NSE, symbol=symbol, valid_from=valid_from, valid_to=None, isin=isin
    )


def _broken_master(queue: InMemoryReconciliationQueue | None = None) -> IdentityMaster:
    """The master as the 2026-10-24 refresh left it: two open CCAVENUE windows, the rest sound."""
    return IdentityMaster(
        (
            _window("CCAVENUE", OLD),
            _window("CCAVENUE", NEW),
            _window("20MICRONS", TWENTY_MICRONS, date(2008, 10, 6)),
            _window("3IINFOLTD", THREE_I, date(2006, 1, 2)),
        ),
        queue=queue,
    )


def _delivery_rows() -> tuple[DeliveryRow, ...]:
    return delivery.parse(
        (FIXTURES / DELIVERY_FILE).read_bytes(), filename=DELIVERY_FILE, trade_date=DAY
    )


def _price_rows() -> tuple[PriceRow, ...]:
    return bhavcopy_udiff.parse((FIXTURES / BHAVCOPY_FILE).read_bytes(), filename=BHAVCOPY_FILE)


def _session(rows: tuple[PriceRow, ...] | None = None) -> SessionIdentity:
    stated = _price_rows() if rows is None else rows
    return SessionIdentity.from_statements(stated, exchange=Exchange.NSE, trade_date=DAY)


def _restated(isin: str) -> tuple[PriceRow, ...]:
    """The session's prices with CCAVENUE's stated ISIN replaced — a statement we control."""
    return tuple(
        row.model_copy(update={"isin": isin}) if row.symbol == "CCAVENUE" else row
        for row in _price_rows()
    )


# ── the fixtures are what they claim ────────────────────────────────────────────────────────


def test_the_fixtures_are_the_real_session_and_the_master_really_is_ambiguous() -> None:
    rows = _delivery_rows()
    assert [(r.symbol, r.series) for r in rows] == [
        ("20MICRONS", "EQ"),
        ("3IINFOLTD", "BE"),
        ("CCAVENUE", "EQ"),
    ]
    stated = {(p.symbol, p.series): p.isin for p in _price_rows()}
    assert stated[("CCAVENUE", "EQ")] == NEW

    with pytest.raises(AmbiguousSymbolError) as raised:
        _broken_master().try_resolve("CCAVENUE", DAY)
    assert raised.value.conflict.isins == (OLD, NEW)


# ── layer 1: the session's statement settles an ambiguous master ─────────────────────────────


def test_the_session_statement_places_ccavenue_on_the_isin_it_names() -> None:
    queue = InMemoryReconciliationQueue()
    out = delivery.resolve(_delivery_rows(), _broken_master(queue), session=_session())

    placed = {r.symbol: r.isin for r in out.resolved}
    assert placed == {"20MICRONS": TWENTY_MICRONS, "3IINFOLTD": THREE_I, "CCAVENUE": NEW}
    assert out.ambiguity_resolved_by_session == 1
    assert (out.via_session, out.session_disagrees, out.unresolved) == (0, 0, ())
    # The master is not repaired here: its conflict stays queued for a human.
    assert [c.isins for c in queue.items] == [(OLD, NEW)]


def test_the_choice_follows_the_statement_not_a_preference_between_candidates() -> None:
    """A pre-reissue session states the old ISIN, and the row goes there — no 'newest wins'."""
    out = delivery.resolve(_delivery_rows(), _broken_master(), session=_session(_restated(OLD)))

    assert {r.symbol: r.isin for r in out.resolved}["CCAVENUE"] == OLD
    assert out.ambiguity_resolved_by_session == 1


def test_a_session_isin_outside_the_candidates_is_not_used() -> None:
    """The statement only picks among what the master already holds; it never adds an ISIN."""
    outsider = "INE483S20012"  # AvenuesAI's rights entitlement — a real ISIN, the wrong one
    out = delivery.resolve(
        _delivery_rows(), _broken_master(), session=_session(_restated(outsider))
    )

    assert "CCAVENUE" not in {r.symbol for r in out.resolved}
    assert outsider not in {r.isin for r in out.resolved}
    assert out.ambiguity_resolved_by_session == 0
    [held] = out.ambiguous
    assert (held.row.symbol, held.candidates, held.session_isin) == (
        "CCAVENUE",
        (OLD, NEW),
        outsider,
    )
    assert outsider in held.reason


# ── layer 2: what the session cannot settle is quarantined, never guessed ─────────────────────


def test_an_ambiguous_row_with_no_session_statement_is_quarantined_not_guessed() -> None:
    queue = InMemoryReconciliationQueue()
    out = delivery.resolve(_delivery_rows(), _broken_master(queue))

    assert {r.symbol: r.isin for r in out.resolved} == {
        "20MICRONS": TWENTY_MICRONS,
        "3IINFOLTD": THREE_I,
    }
    [held] = out.ambiguous
    assert (held.row.symbol, held.row.series, held.candidates, held.session_isin) == (
        "CCAVENUE",
        "EQ",
        (OLD, NEW),
        None,
    )
    assert held.reason.startswith("ambiguous")
    # Ambiguous is not unknown: the two stay in separate quarantines.
    assert out.unresolved == ()
    assert [c.isins for c in queue.items] == [(OLD, NEW)]


def _ambiguous_master_and_rows(n: int) -> tuple[IdentityMaster, list[DeliveryRow]]:
    """`n` symbols, each with two open windows — the shape of the 2026-10-10 broken refresh."""
    windows: list[SymbolWindow] = []
    rows: list[DeliveryRow] = []
    for k in range(n):
        symbol = f"REISSUED{k}"
        windows += [_window(symbol, f"INE9{k:02d}A01011"), _window(symbol, f"INE9{k:02d}A01029")]
        rows.append(
            DeliveryRow(
                symbol=symbol, series="EQ", trade_date=DAY, deliv_qty=1, deliv_pct=Decimal("10")
            )
        )
    return IdentityMaster(tuple(windows)), rows


def test_up_to_the_threshold_the_file_is_kept_and_the_rows_quarantined() -> None:
    master, rows = _ambiguous_master_and_rows(MAX_AMBIGUOUS_QUARANTINED)
    out = delivery.resolve(rows, master)
    assert len(out.ambiguous) == MAX_AMBIGUOUS_QUARANTINED
    assert out.resolved == ()


def test_over_the_threshold_the_whole_file_fails() -> None:
    master, rows = _ambiguous_master_and_rows(MAX_AMBIGUOUS_QUARANTINED + 1)
    with pytest.raises(AmbiguousDeliveryFileError, match="broken master"):
        delivery.resolve(rows, master)


def test_the_threshold_is_the_justified_five() -> None:
    """Pinned so a change to the bar is a deliberate edit, not a drift (see its docstring)."""
    assert MAX_AMBIGUOUS_QUARANTINED == 5


def test_session_settled_rows_do_not_count_towards_the_threshold() -> None:
    master, rows = _ambiguous_master_and_rows(MAX_AMBIGUOUS_QUARANTINED + 3)
    settled_by_session = ("INE900A01011", "INE901A01011", "INE902A01011")
    stated = [
        _price_rows()[0].model_copy(update={"symbol": w.symbol, "isin": w.isin})
        for isin in settled_by_session
        for w in master.windows_for(isin)
    ]
    session = SessionIdentity.from_statements(stated, exchange=Exchange.NSE, trade_date=DAY)
    out = delivery.resolve(rows, master, session=session)
    assert out.ambiguity_resolved_by_session == 3
    assert len(out.ambiguous) == MAX_AMBIGUOUS_QUARANTINED


def test_an_unambiguous_master_is_untouched_by_the_new_branch() -> None:
    sound = IdentityMaster(
        (
            _window("CCAVENUE", NEW, DAY),
            _window("20MICRONS", TWENTY_MICRONS, date(2008, 10, 6)),
            _window("3IINFOLTD", THREE_I, date(2006, 1, 2)),
        )
    )
    out = delivery.resolve(_delivery_rows(), sound, session=_session())
    assert out.ambiguity_resolved_by_session == 0
    assert {r.symbol: r.isin for r in out.resolved}["CCAVENUE"] == NEW


# ── the L1 write: the delivery joins, the session publishes ───────────────────────────────────


def test_the_session_writes_with_ccavenue_delivery_joined(tmp_path: Path) -> None:
    report = write_prices_raw(
        _price_rows(),
        exchange=Exchange.NSE,
        delivery_rows=_delivery_rows(),
        master=_broken_master(),
        data_root=tmp_path,
    )

    assert report.delivery_ambiguity_resolved_by_session == 1
    assert (report.delivery_joined, report.delivery_unresolved, report.delivery_orphaned) == (
        3,
        0,
        0,
    )
    by_isin = {r["isin"]: r for r in read_prices_raw(DAY, data_root=tmp_path)}
    assert by_isin[NEW]["deliv_qty"] == 644736
    assert by_isin[NEW]["deliv_pct"] == Decimal("51.1300")
    # The trade-to-trade row still says "not stated", never zero.
    assert by_isin[THREE_I]["deliv_qty"] is None
    assert by_isin[THREE_I]["deliv_pct"] is None


def test_an_unsettled_row_is_quarantined_in_l1_and_its_isin_shows_no_delivery(
    tmp_path: Path,
) -> None:
    """The partition publishes without CCAVENUE's delivery, and says so — it never reads as 0."""
    # The write builds the session from the prices it is given; restating CCAVENUE's series leaves
    # the session silent on `(CCAVENUE, EQ)`, so the master's ambiguity stands unsettled.
    prices = tuple(
        p.model_copy(update={"series": "BE"}) if p.symbol == "CCAVENUE" else p
        for p in _price_rows()
    )
    report = write_prices_raw(
        prices,
        exchange=Exchange.NSE,
        delivery_rows=_delivery_rows(),
        master=_broken_master(),
        data_root=tmp_path,
    )

    assert report.delivery_ambiguous == 1
    assert report.delivery_rows == (
        report.delivery_joined
        + report.delivery_unresolved
        + report.delivery_ambiguous
        + report.delivery_orphaned
    )
    quarantine = pq.read_table(
        partition_path(Layer.L1, PRICES_RAW_QUARANTINE_DATASET, DAY, data_root=tmp_path)
    ).to_pylist()
    [held] = [q for q in quarantine if q["symbol"] == "CCAVENUE"]
    assert held["reason"] == PriceQuarantineReason.SYMBOL_AMBIGUOUS
    assert held["isin"] is None
    assert held["deliv_qty"] == 644736

    by_isin = {r["isin"]: r for r in read_prices_raw(DAY, data_root=tmp_path)}
    assert by_isin[NEW]["deliv_qty"] is None
    assert by_isin[NEW]["deliv_pct"] is None


def _bar_from(record: dict[str, object]) -> PriceBar:
    close = cast("Decimal", record["close"])
    volume = Decimal(cast("int", record["total_traded_qty"]))
    return PriceBar(
        isin=cast("str", record["isin"]),
        trade_date=cast("date", record["trade_date"]),
        high=cast("Decimal", record["high"]),
        low=cast("Decimal", record["low"]),
        close=close,
        volume=volume,
        raw_close=close,
        traded_value=cast("Decimal", record["total_traded_value"]),
        size=close * volume,
        deliv_qty=None,
        deliv_pct=cast("Decimal | None", record["deliv_pct"]),
    )


def test_downstream_the_quarantined_isin_reads_as_unavailable_never_zero(tmp_path: Path) -> None:
    """The M17 dossier's `deliv_pct_avg_5/20` over a window holding the quarantined session.

    `delivery_average` is what the dossier calls (`analyst.commons.dossier`); fed the bar the L1
    partition actually holds for CCAVENUE, it returns `None` — unavailable — not an average that
    counted the session as 0 % delivered. The control is the same window with the joined value.
    """
    prices = tuple(
        p.model_copy(update={"series": "BE"}) if p.symbol == "CCAVENUE" else p
        for p in _price_rows()
    )
    write_prices_raw(
        prices,
        exchange=Exchange.NSE,
        delivery_rows=_delivery_rows(),
        master=_broken_master(),
        data_root=tmp_path,
    )
    bar = _bar_from({r["isin"]: r for r in read_prices_raw(DAY, data_root=tmp_path)}[NEW])
    assert bar.deliv_pct is None

    earlier = [replace(bar, deliv_pct=Decimal("50")) for _ in range(4)]
    assert delivery_average([*earlier, bar], 5) is None
    joined = replace(bar, deliv_pct=Decimal("51.13"))
    assert delivery_average([*earlier, joined], 5) == Decimal("50.226")


# ── the quality flag: the row reaches /status/quality ─────────────────────────────────────────


def test_each_quarantined_row_raises_one_error_flag_carrying_what_a_human_needs() -> None:
    out = delivery.resolve(_delivery_rows(), _broken_master())
    [finding] = ambiguity_findings(out.ambiguous, exchange=Exchange.NSE)

    assert finding.check_name == AMBIGUOUS_DELIVERY_CHECK
    assert finding.severity == "ERROR"
    assert finding.logical_date == DAY
    assert finding.isin is None
    assert finding.source == PRICES_RAW_QUARANTINE_DATASET
    assert finding.detail["symbol"] == "CCAVENUE"
    assert finding.detail["series"] == "EQ"
    assert finding.detail["candidates"] == [OLD, NEW]
    assert finding.detail["trade_date"] == "2026-10-26"
    assert finding.detail["session_isin"] is None
    assert str(finding.detail["reason"]).startswith("ambiguous")
    # A retry of the same file raises the same fingerprint, so persist_findings dedupes it.
    [again] = ambiguity_findings(
        delivery.resolve(_delivery_rows(), _broken_master()).ambiguous, exchange=Exchange.NSE
    )
    assert again.fingerprint == finding.fingerprint


def _lake(tmp_path: Path, prices_csv: bytes) -> L0Store:
    """An L0 lake holding the session's bhavcopy where the delivery write looks for it."""
    store = L0Store(clock=FrozenClock(DAY), data_root=tmp_path)
    store.put(
        "nse_bhavcopy_udiff",
        DAY,
        "BhavCopy_NSE_CM_0_0_0_20261026_F_0000.csv.zip",
        prices_csv,
        content_type="application/zip",
    )
    return store


def test_the_delivery_source_set_hands_the_flags_to_the_runners_sink(tmp_path: Path) -> None:
    """The nightly path: `_write_delivery` publishes the session and sends the flag on."""
    text = (FIXTURES / BHAVCOPY_FILE).read_text()
    # The bhavcopy states CCAVENUE in another series, so nothing settles the EQ delivery row.
    lake = _lake(tmp_path, text.replace(",CCAVENUE,EQ,", ",CCAVENUE,BE,").encode())
    sunk: list[QualityFinding] = []

    def sink(found: Sequence[QualityFinding]) -> None:
        sunk.extend(found)

    ctx = WriteContext(
        l0=lake,
        data_root=lake.data_root,
        master=_broken_master(),
        register=load_register(),
        findings=sink,
    )
    report = cast("PricesRawWriteReport", SOURCE_SETS[NSE_DELIVERY].write(_delivery_rows(), ctx))

    assert report.delivery_ambiguous == 1
    assert [(f.check_name, f.detail["symbol"]) for f in sunk] == [
        (AMBIGUOUS_DELIVERY_CHECK, "CCAVENUE")
    ]


def test_the_delivery_source_set_raises_no_flag_when_the_session_settles_it(
    tmp_path: Path,
) -> None:
    lake = _lake(tmp_path, (FIXTURES / BHAVCOPY_FILE).read_bytes())
    sunk: list[QualityFinding] = []
    ctx = WriteContext(
        l0=lake,
        data_root=lake.data_root,
        master=_broken_master(),
        register=load_register(),
        findings=sunk.extend,
    )
    report = cast("PricesRawWriteReport", SOURCE_SETS[NSE_DELIVERY].write(_delivery_rows(), ctx))

    assert report.delivery_ambiguity_resolved_by_session == 1
    assert report.delivery_ambiguous == 0
    assert sunk == []
