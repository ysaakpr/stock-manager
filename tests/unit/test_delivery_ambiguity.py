"""An ambiguous master no longer fails a whole delivery file — CCAVENUE, 2026-10-26.

AvenuesAI's ISIN was reissued INE483S01020 → INE483S01038 (switch 2026-10-19), and the identity
master came out of the 2026-10-24 refresh holding two open `CCAVENUE` windows (reconciliation
ids 72/73). Every resolve of `CCAVENUE` then raised `AmbiguousSymbolError`, and because
`delivery.resolve` let it propagate, `nse_delivery` went FAILED for 2026-10-26 on — one symbol of
~3,500 reddening the M17 interlock for the whole session.

Layer 1, pinned here: the same session's bhavcopy states exactly one ISIN for `(CCAVENUE, EQ)`,
and when that ISIN is one of the master's candidates it places the row. The fixtures are real:
three rows of the 2026-10-26 `sec_bhavdata_full` L0 payload and the matching three rows of that
session's UDiFF bhavcopy, copied byte-for-byte (`tests/fixtures/nse_delivery/PROVENANCE.md`).
Offline: no network, no Postgres.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.identity.master import (
    AmbiguousSymbolError,
    Exchange,
    IdentityMaster,
    InMemoryReconciliationQueue,
    SymbolWindow,
)
from dataplatform.identity.session import SessionIdentity
from dataplatform.ingest.models import PriceRow
from dataplatform.ingest.nse import bhavcopy_udiff, delivery
from dataplatform.ingest.nse.delivery import DeliveryRow
from dataplatform.store.l1 import read_prices_raw, write_prices_raw

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
    with pytest.raises(AmbiguousSymbolError):
        delivery.resolve(_delivery_rows(), _broken_master(), session=_session(_restated(outsider)))


def test_without_a_session_statement_the_ambiguity_still_raises() -> None:
    with pytest.raises(AmbiguousSymbolError):
        delivery.resolve(_delivery_rows(), _broken_master())


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
