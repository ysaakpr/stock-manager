"""DQ-CA: the owner's ruling on CORALFINAC's 2017-09-06 dividend — ₹0.20, BSE's record (D42).

Coral India Finance & Housing's board recommended "10% dividend (Re. 1/- per Equity Share of
Face Value of Rs. 10/- each)" on 2017-05-23. A 1:5 sub-division (₹10 → ₹2) went ex on
2017-08-03, before the 2017-09-06 ex-date, so each ₹2 share received 10% of ₹2 = ₹0.20. BSE's
record says ₹0.20; NSE's still says the pre-split "Re 1". The owner ruled (2026-11-16): "Dividend
Amount: ₹0.20 per equity share". It is the BEML mechanism (`superseded_dividends`) with the feeds
the other way round: NSE's record is the stale one, BSE's is booked once.

These tests fail if Re 1 is booked — alone, or on top of ₹0.20 — and pin that the booked note
names the owner's ruling and the issuer's own L0 statement. The rows are the live store's own
(ISIN, ex-date, verbatim raw text). Offline: no network, no database.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

import pytest
import yaml

from dataplatform.corpactions import (
    ActionType,
    ReconcileError,
    ReconciliationReason,
    SingleSourcePolicy,
    collapse_reconciled_rows,
    reconcile,
)
from dataplatform.corpactions.manual_actions import (
    MANUAL_ACTIONS_PATH,
    ManualActionsError,
    SupersededDividend,
    load_manual_actions,
)
from dataplatform.corpactions.taxonomy import DividendKind, DividendTerms
from dataplatform.ingest.corp_actions import CorporateAction

NSE: Final = "nse_corp_actions"
BSE: Final = "bse_corp_actions"
CORAL: Final = "INE558D01021"
EX: Final = date(2017, 9, 6)
ACCEPT: Final = SingleSourcePolicy.ACCEPT
U: Final = DividendKind.UNSPECIFIED
#: The store's raw text of each feed's record (corporate_actions, 2026-11-19).
BSE_RAW: Final = "Dividend - Rs. - 0.2000"
NSE_RAW: Final = "Annual General Meeting/Dividend - Re 1/- Per Share"


def _row(
    source: str, amount: str, raw: str, *, isin: str = CORAL, ex: date = EX
) -> CorporateAction:
    return CorporateAction(
        isin=isin,
        source=source,
        ex_date=ex,
        action_type=ActionType.DIVIDEND,
        terms=DividendTerms(dividend_kind=U, amount_inr=Decimal(amount)),
        raw_text=raw,
        knowable_date=ex,
        l0_key=f"{source}/x",
        component=U.value,
    )


def _bse(amount: str = "0.2", **kw: Any) -> CorporateAction:
    return _row(BSE, amount, BSE_RAW if amount == "0.2" else f"Dividend - Rs. - {amount}", **kw)


def _nse(amount: str = "1", **kw: Any) -> CorporateAction:
    return _row(NSE, amount, NSE_RAW if amount == "1" else f"Dividend - Rs {amount}", **kw)


def _coral() -> SupersededDividend:
    (ruling,) = [r for r in load_manual_actions().superseded if r.isin == CORAL]
    return ruling


def _booked(rows: list[CorporateAction]) -> list[Decimal | None]:
    """What the total return reinvests: the collapse of the rows `reconcile` marked reconciled."""
    result = reconcile(rows, single_source_policy=ACCEPT)
    marked = [a for r in result.reconciled for a in r.sources]
    return [
        a.terms.amount_inr
        for a in collapse_reconciled_rows(marked)
        if isinstance(a.terms, DividendTerms)
    ]


# ── the curated row ─────────────────────────────────────────────────────────────────────────


def test_the_file_supersedes_nses_re_1_and_books_bses_twenty_paise() -> None:
    ruling = _coral()
    assert ruling.ex_date == EX
    assert (ruling.feed, ruling.superseded_kind, ruling.superseded_amount_inr) == (
        NSE,
        U,
        Decimal("1"),
    )
    assert (ruling.booked_feed, ruling.amount_inr) == (BSE, Decimal("0.20"))
    assert ruling.sources[0].l0_key.startswith(f"{BSE}/")


def test_the_issuer_statement_is_an_l0_line_and_the_ruling_is_the_owners() -> None:
    ruling = _coral()
    assert ruling.issuer.url is None and ruling.issuer.l0 is not None
    assert ruling.issuer.l0.l0_key == "nse_pr_bundle/2017/05/PR240517.zip"
    assert (ruling.issuer.l0.member, ruling.issuer.l0.line) == ("An240517.txt", 3253)
    assert "Face Value of Rs. 10/- each" in ruling.issuer.quote
    assert ruling.ruling is not None
    assert ruling.ruling.dated == date(2026, 11, 16)
    assert ruling.ruling.recorded_in == "HUMAN_DECISIONS.md D42"
    assert "₹0.20 per equity share" in ruling.ruling.quote


# ── CORALFINAC books ₹0.20, exactly once ────────────────────────────────────────────────────


def test_coralfinac_reconciles_to_twenty_paise_once() -> None:
    """Fails if NSE's Re 1 is booked — alone (five times the cash) or on top (two bookings)."""
    result = reconcile([_bse(), _nse()], single_source_policy=ACCEPT)
    assert result.queue == ()
    (agreed,) = result.reconciled
    assert agreed.action_type is ActionType.DIVIDEND
    assert isinstance(agreed.terms, DividendTerms)
    assert agreed.terms.amount_inr == Decimal("0.20")
    assert agreed.terms.amount_inr != Decimal("1")
    assert _booked([_bse(), _nse()]) == [Decimal("0.2")]
    assert sum(a for a in _booked([_bse(), _nse()]) if a is not None) == Decimal("0.20")


def test_the_folded_event_is_bses_record() -> None:
    (event,) = collapse_reconciled_rows([_bse(), _nse()])
    assert (event.source, event.raw_text) == (BSE, BSE_RAW)
    assert event.ex_date == EX


def test_the_booked_note_names_the_owner_ruling_and_the_issuer_line() -> None:
    (agreed,) = reconcile([_bse(), _nse()], single_source_policy=ACCEPT).reconciled
    assert sorted(a.source for a in agreed.sources) == [BSE, NSE]
    note = agreed.reconciliation_note or ""
    assert note.startswith("superseded (manual_actions.yaml, checked 2026-11-19)")
    assert f"{NSE} {NSE_RAW!r} is superseded" in note
    assert "PR240517.zip:An240517.txt line 3253" in note
    assert "owner's ruling of 2026-11-16 (HUMAN_DECISIONS.md D42" in note
    assert agreed.cross_verified


def test_without_the_ruling_coralfinac_is_still_a_mismatch() -> None:
    """No general rule resolves different money; only the ruling does."""
    result = reconcile([_bse(), _nse()], single_source_policy=ACCEPT, superseded=())
    assert result.reconciled == ()
    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]


def test_the_split_and_the_next_dividend_reconcile_as_before() -> None:
    """The ruling touches 2017-09-06 only; NSE's 2018 record already says Re 0.20 on FV 2."""
    july18 = date(2018, 7, 20)
    rows = [
        _bse(),
        _nse(),
        _row(BSE, "0.2", "Dividend - Rs. - 0.2000", ex=july18),
        _row(NSE, "0.20", "Annual General Meeting/Dividend- Re 0.20 Per Share", ex=july18),
    ]
    assert sorted(a for a in _booked(rows) if a is not None) == [Decimal("0.2"), Decimal("0.2")]


@pytest.mark.parametrize(
    ("rows", "missing"),
    [
        ([_bse(), _nse("0.20")], "nse_corp_actions record(s) state UNSPECIFIED 1"),
        ([_bse()], "nse_corp_actions record(s) state UNSPECIFIED 1"),
        ([_bse("1"), _nse()], "bse_corp_actions record(s) state 0.20"),
        ([_nse()], "bse_corp_actions record(s) state 0.20"),
    ],
    ids=["nse-corrected", "nse-gone", "bse-changed", "bse-gone"],
)
def test_a_ruled_day_whose_records_changed_raises(
    rows: list[CorporateAction], missing: str
) -> None:
    with pytest.raises(ReconcileError, match=re.escape(missing)):
        reconcile(rows, single_source_policy=ACCEPT)


def test_the_same_disagreement_on_another_isin_still_flags() -> None:
    other = "INE002A01018"
    result = reconcile([_bse(isin=other), _nse(isin=other)], single_source_policy=ACCEPT)
    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]


# ── the loader's rules for an L0-cited issuer document ─────────────────────────────────────


def _row_of(document: dict[str, Any]) -> dict[str, Any]:
    (row,) = [r for r in document["superseded_dividends"] if r["isin"] == CORAL]
    return cast(dict[str, Any], row)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"url": "https://example.invalid/x.pdf"}, "not both"),
        ({"line": None}, "1-based line"),
        ({"quote": None}, "l0_key and quote"),
    ],
    ids=["url-and-l0", "no-line", "no-quote"],
)
def test_a_malformed_l0_issuer_document_is_refused(
    tmp_path: Path, patch: dict[str, object], message: str
) -> None:
    document = yaml.safe_load(MANUAL_ACTIONS_PATH.read_text(encoding="utf-8"))
    _row_of(document)["issuer_document"].update(patch)
    path = tmp_path / "manual_actions.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ManualActionsError, match=message):
        load_manual_actions(path)


def test_a_malformed_owner_ruling_is_refused(tmp_path: Path) -> None:
    document = yaml.safe_load(MANUAL_ACTIONS_PATH.read_text(encoding="utf-8"))
    del _row_of(document)["owner_ruling"]["recorded_in"]
    path = tmp_path / "manual_actions.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ManualActionsError, match="recorded_in"):
        load_manual_actions(path)
