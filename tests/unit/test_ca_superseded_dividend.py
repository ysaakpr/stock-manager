"""M2.3: an owner ruling that one feed's dividend record is stale (BEML, 2026-09-22).

BEML's board recommended a ₹0.55 final on 2026-05-29, then on 2026-08-07 a revised ₹12.28
"inclusive of" the ₹0.55 (the company's letter, cited in `manual_actions.yaml`). NSE carries
₹12.28; BSE still carries ₹0.55. The owner ruled (2026-11-01): one final of ₹12.28, booked once —
never BSE's ₹0.55 alone, and never ₹0.55 on top.

The ruling is a `superseded_dividends` row: it names BSE's record exactly and the amount NSE's
record must state, and reconciliation books NSE's record with BSE's as evidence. These tests pin
the booked money (so booking the stale record, alone or added, fails), the loud failure when the
store no longer holds what the ruling names, and that it touches no other ISIN or day. The rows
are the live store's own (ISIN, ex-date, verbatim raw text). Offline: no network, no database.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

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
BEML: Final = "INE258A01024"
EX: Final = date(2026, 9, 22)
ACCEPT: Final = SingleSourcePolicy.ACCEPT
LETTER: Final = "https://www.bemlindia.in/wp-content/uploads/2026/08/Dividend_SE_signed.pdf"


def _row(
    source: str,
    amount: str,
    raw: str,
    *,
    kind: DividendKind = DividendKind.UNSPECIFIED,
    isin: str = BEML,
    ex: date = EX,
) -> CorporateAction:
    return CorporateAction(
        isin=isin,
        source=source,
        ex_date=ex,
        action_type=ActionType.DIVIDEND,
        terms=DividendTerms(dividend_kind=kind, amount_inr=Decimal(amount)),
        raw_text=raw,
        knowable_date=ex,
        l0_key=f"{source}/x",
        component=kind.value,
    )


def _bse(amount: str = "0.55", **kw: object) -> CorporateAction:
    raw = f"Final Dividend - Rs. - {Decimal(amount):.4f}"
    return _row(BSE, amount, raw, kind=DividendKind.FINAL, **kw)  # type: ignore[arg-type]


def _nse(amount: str = "12.28", **kw: object) -> CorporateAction:
    return _row(NSE, amount, f"Dividend - Rs {amount} Per Share", **kw)  # type: ignore[arg-type]


def _ruling() -> tuple[SupersededDividend, ...]:
    return load_manual_actions().superseded


def _booked(rows: Sequence[CorporateAction], **kw: object) -> list[Decimal | None]:
    """What the total return reinvests: the collapse of the rows `reconcile` marked reconciled."""
    result = reconcile(rows, single_source_policy=ACCEPT, **kw)  # type: ignore[arg-type]
    marked = [a for r in result.reconciled for a in r.sources]
    return [
        a.terms.amount_inr
        for a in collapse_reconciled_rows(marked, **kw)  # type: ignore[arg-type]
        if isinstance(a.terms, DividendTerms)
    ]


# ── the curated row ─────────────────────────────────────────────────────────────────────────


def test_the_file_supersedes_bses_record_and_books_nses_amount() -> None:
    (ruling,) = _ruling()
    assert (ruling.isin, ruling.ex_date) == (BEML, EX)
    assert (ruling.feed, ruling.superseded_kind, ruling.superseded_amount_inr) == (
        BSE,
        DividendKind.FINAL,
        Decimal("0.55"),
    )
    assert (ruling.booked_feed, ruling.amount_inr) == (NSE, Decimal("12.28"))
    assert ruling.issuer.url == LETTER and ruling.issuer.dated == date(2026, 8, 7)
    assert "inclusive of the dividend of Rs. 0.55" in ruling.issuer.quote
    # The letter is cited by URL and date in the note, beside the L0 sources that exist.
    assert LETTER in ruling.note and "2026-08-07" in ruling.note
    keys = [s.l0_key for s in ruling.sources]
    assert keys[0].startswith(f"{NSE}/")
    assert "nse_pr_bundle/2026/08/PR070826.zip" in keys


# ── BEML books ₹12.28, exactly once ─────────────────────────────────────────────────────────


def test_beml_reconciles_to_twelve_twenty_eight_once() -> None:
    """Fails if BSE's ₹0.55 is booked — alone (wrong amount) or on top (two bookings)."""
    rows = [_bse(), _nse()]
    result = reconcile(rows, single_source_policy=ACCEPT)
    assert result.queue == ()
    (agreed,) = result.reconciled
    assert agreed.terms == DividendTerms(
        dividend_kind=DividendKind.UNSPECIFIED, amount_inr=Decimal("12.28")
    )
    assert _booked(rows) == [Decimal("12.28")]


def test_the_folded_event_is_nses_record() -> None:
    (event,) = collapse_reconciled_rows([_bse(), _nse()])
    assert (event.source, event.raw_text) == (NSE, "Dividend - Rs 12.28 Per Share")
    assert event.ex_date == EX


def test_both_rows_carry_the_ruling_so_bses_stale_note_is_replaced() -> None:
    """`persist_reconciliation` writes this note on every source row, BSE's 0.55 included."""
    (agreed,) = reconcile([_bse(), _nse()], single_source_policy=ACCEPT).reconciled
    assert sorted(a.source for a in agreed.sources) == [BSE, NSE]
    note = agreed.reconciliation_note or ""
    assert note.startswith("superseded (manual_actions.yaml")
    assert "'Final Dividend - Rs. - 0.5500' is superseded" in note
    assert LETTER in note and "single-source" not in note
    assert agreed.cross_verified


def test_without_the_ruling_beml_is_still_a_mismatch() -> None:
    """The general rules do not resolve different money; the ruling alone does."""
    result = reconcile([_bse(), _nse()], single_source_policy=ACCEPT, superseded=())
    assert result.reconciled == ()
    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]


def test_bemls_other_dividends_reconcile_as_before_beside_the_ruled_day() -> None:
    june = date(2026, 6, 5)
    rows = [
        _bse(),
        _nse(),
        _row(BSE, "2.3", "Interim Dividend - Rs. - 2.3000", kind=DividendKind.INTERIM, ex=june),
        _row(
            NSE, "2.30", "Interim Dividend - Rs 2.30 Per Share", kind=DividendKind.INTERIM, ex=june
        ),
    ]
    assert sorted(a for a in _booked(rows) if a is not None) == [Decimal("2.3"), Decimal("12.28")]


def test_a_pass_that_does_not_load_the_ruled_day_is_untouched() -> None:
    june = date(2026, 6, 5)
    rows = [
        _row(BSE, "2.3", "Interim Dividend - Rs. - 2.3000", kind=DividendKind.INTERIM, ex=june),
        _row(
            NSE, "2.30", "Interim Dividend - Rs 2.30 Per Share", kind=DividendKind.INTERIM, ex=june
        ),
    ]
    assert _booked(rows) == [Decimal("2.3")]


# ── it fails loudly when the store no longer holds what it names ───────────────────────────


@pytest.mark.parametrize(
    ("rows", "missing"),
    [
        ([_bse("0.60"), _nse()], "bse_corp_actions record(s) state FINAL 0.55"),
        ([_nse()], "bse_corp_actions record(s) state FINAL 0.55"),
        ([_bse(), _nse("12.30")], "nse_corp_actions record(s) state 12.28"),
        ([_bse()], "nse_corp_actions record(s) state 12.28"),
        (
            [
                _row(BSE, "0.55", "Interim Dividend - Rs. - 0.5500", kind=DividendKind.INTERIM),
                _nse(),
            ],
            "bse_corp_actions record(s) state FINAL 0.55",
        ),
    ],
    ids=["bse-corrected", "bse-gone", "nse-changed", "nse-gone", "bse-kind-differs"],
)
def test_a_ruled_day_whose_records_changed_raises(
    rows: list[CorporateAction], missing: str
) -> None:
    with pytest.raises(ReconcileError, match=re.escape(missing)):
        reconcile(rows, single_source_policy=ACCEPT)
    with pytest.raises(ReconcileError, match=re.escape(missing)):
        collapse_reconciled_rows(rows)


def test_a_superseded_row_naming_a_record_that_does_not_exist_raises(tmp_path: Path) -> None:
    """The file's row edited to name BSE ₹0.56: the store has no such record, so nothing books."""
    document = yaml.safe_load(MANUAL_ACTIONS_PATH.read_text(encoding="utf-8"))
    (row,) = document["superseded_dividends"]
    row["supersedes"]["amount_inr"] = "0.56"
    path = tmp_path / "manual_actions.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    wrong = load_manual_actions(path).superseded
    with pytest.raises(ReconcileError, match="0 bse_corp_actions record"):
        reconcile([_bse(), _nse()], single_source_policy=ACCEPT, superseded=wrong)
    with pytest.raises(ReconcileError, match="0 bse_corp_actions record"):
        collapse_reconciled_rows([_bse(), _nse()], superseded=wrong)


def test_a_duplicated_stale_record_is_ambiguous_and_raises() -> None:
    twin = _bse().model_copy(update={"component": "FINAL#2"})
    with pytest.raises(ReconcileError, match="2 bse_corp_actions record"):
        reconcile([_bse(), twin, _nse()], single_source_policy=ACCEPT)


# ── it never applies to anything else ───────────────────────────────────────────────────────


def test_the_same_disagreement_on_another_isin_still_flags() -> None:
    other = "INE002A01018"
    result = reconcile([_bse(isin=other), _nse(isin=other)], single_source_policy=ACCEPT)
    assert result.reconciled == ()
    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]


def test_the_same_disagreement_on_another_beml_day_still_flags() -> None:
    other = date(2027, 9, 22)
    result = reconcile([_bse(ex=other), _nse(ex=other)], single_source_policy=ACCEPT)
    assert result.reconciled == ()
    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]


# ── the loader refuses a malformed ruling ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"issuer_document": None}, "issuer_document"),
        ({"issuer_document": {"url": "http://x", "dated": "2026-08-07", "quote": "q"}}, "https"),
        (
            {"supersedes": {"feed": "nse_pr_bundle", "dividend_kind": "FINAL", "amount_inr": "1"}},
            "feed",
        ),
        (
            {"supersedes": {"feed": BSE, "dividend_kind": "FINAL", "amount_inr": "12.28"}},
            "booked amount",
        ),
        ({"supersedes": {"feed": BSE, "dividend_kind": "BOGUS", "amount_inr": "0.55"}}, "BOGUS"),
        ({"supersedes": None}, "supersedes"),
        ({"amount_inr": 12.28}, "float"),
        ({"note": ""}, "note"),
        (
            {"supersedes": {"feed": NSE, "dividend_kind": "FINAL", "amount_inr": "0.55"}},
            "cite that feed",
        ),
    ],
    ids=[
        "no-issuer",
        "http-url",
        "not-a-ca-feed",
        "same-amounts",
        "bad-kind",
        "no-supersedes",
        "float-amount",
        "no-note",
        "first-source-not-booked-feed",
    ],
)
def test_a_malformed_ruling_is_refused(
    tmp_path: Path, patch: dict[str, object], message: str
) -> None:
    document = yaml.safe_load(MANUAL_ACTIONS_PATH.read_text(encoding="utf-8"))
    (row,) = document["superseded_dividends"]
    row.update(patch)
    path = tmp_path / "manual_actions.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ManualActionsError, match=message):
        load_manual_actions(path)


def test_a_ruling_listed_twice_is_refused(tmp_path: Path) -> None:
    document = yaml.safe_load(MANUAL_ACTIONS_PATH.read_text(encoding="utf-8"))
    document["superseded_dividends"] *= 2
    path = tmp_path / "manual_actions.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ManualActionsError, match="listed twice"):
        load_manual_actions(path)
