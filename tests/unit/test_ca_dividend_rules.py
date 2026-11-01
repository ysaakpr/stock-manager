# ruff: noqa: E501 — the case tables quote the live store's verbatim lines, one case per line.
"""M2.3: the three owner-approved rules for dividends the feeds state consistently (2026-11-01).

27 dividend RATIO_MISMATCH flags were open after #152/#153. Two exchanges disagreeing is
flagged, never silently resolved; the owner approved three rules that resolve a disagreement
**only when the amounts are consistent**:

* **A, silence yields.** One feed's record states no amount — NSE's blank `Dividend - Rs  Per
  Share`, a bare `Interim Dividend`, or a typo the parser refuses (`Re 0..30`, `Rs 0 .70`) — and
  the other's states one. The amount comes from the other exchange, never from the typo.
* **B, parts add up.** One feed itemises (₹10 + special ₹7.5), the other files one record of
  their exact `Decimal` sum. The single record is booked; the parts never beside it.
* **C, name only.** Equal stated money, different kind labels (SPECIAL / INTERIM / FINAL).

Plus Patanjali (INE619A01035 2026-08-21): NSE's second Interim record, dropped by the store's
one-row-per-kind key, restored from NSE's own L0 by a curated `manual_actions.yaml` row.

Every case below is the live store's own row: ISIN, ex-date and verbatim raw text from the L0
payloads. Each rule has its negative — inconsistent money stays a `RATIO_MISMATCH` — and each
positive asserts the *booked amount*, so a rule inverted to keep the wrong side fails here.
Offline: no network, no database.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.corpactions import (
    ActionType,
    ReconciliationReason,
    SingleSourcePolicy,
    collapse_reconciled_rows,
    reconcile,
)
from dataplatform.corpactions.manual_actions import (
    MANUAL_SOURCE,
    ManualActionsError,
    load_manual_actions,
)
from dataplatform.corpactions.parse_terms import parse_purpose, states_dividend_amount
from dataplatform.corpactions.taxonomy import DividendKind, DividendTerms
from dataplatform.ingest.corp_actions import CorporateAction

NSE: Final = "nse_corp_actions"
BSE: Final = "bse_corp_actions"
U, F, S, INT = (
    DividendKind.UNSPECIFIED,
    DividendKind.FINAL,
    DividendKind.SPECIAL,
    DividendKind.INTERIM,
)


def _row(
    isin: str,
    source: str,
    ex: date,
    kind: DividendKind,
    amount: str | None,
    raw: str,
    *,
    component: str | None = None,
) -> CorporateAction:
    data: dict[str, object] = {
        "isin": isin,
        "source": source,
        "ex_date": ex,
        "action_type": ActionType.DIVIDEND,
        "terms": DividendTerms(
            dividend_kind=kind, amount_inr=None if amount is None else Decimal(amount)
        ),
        "raw_text": raw,
        "knowable_date": ex,
        "l0_key": f"{source}/x",
    }
    if component is not None:
        data["component"] = component
    return CorporateAction.model_validate(data)


def _booked(rows: list[CorporateAction], **kw: object) -> list[Decimal | None]:
    """The amounts the factor chain and total return would count for these rows, reconciled.

    Runs the pass, keeps the rows it marked reconciled, and folds them as `load_reconciled_actions`
    does — so this is what reaches the book, not just what the pass returned.
    """
    result = reconcile(rows, single_source_policy=SingleSourcePolicy.ACCEPT)
    marked = {
        (a.source, a.component, a.ex_date)
        for r in result.reconciled
        for a in r.sources
        if a.source != MANUAL_SOURCE
    }
    reconciled_rows = [
        a
        for a in rows
        if (a.source, a.component, a.ex_date) in marked and a.source != MANUAL_SOURCE
    ]
    restored = [a for a in rows if a.source == MANUAL_SOURCE]
    out: list[Decimal | None] = []
    for action in collapse_reconciled_rows(reconciled_rows, restored=restored, **kw):  # type: ignore[arg-type]
        assert isinstance(action.terms, DividendTerms)
        out.append(action.terms.amount_inr)
    return out


# ── rule A: silence yields ─────────────────────────────────────────────────────────────────────

#: (ISIN, ex-date, silent feed's (source, kind, raw), stated feed's (source, kind, amount, raw))
_SILENT: Final = [
    ("INE121E01018", date(2017, 6, 30), (NSE, U, "Annual General Meeting/ Dividend - Re 0.0 Per Share"), (BSE, U, "0.5", "Dividend - Rs. - 0.5000")),
    ("INE180C01042", date(2017, 7, 5), (NSE, U, "Annual General Meeting/Dividend - Re 0..30 Per Share"), (BSE, U, "0.3", "Dividend - Rs. - 0.3000")),
    ("INE012A01025", date(2020, 3, 27), (BSE, F, "Final Dividend"), (NSE, U, "14", "Dividend - Rs 14 Per Share")),
    ("INE573A01042", date(2020, 9, 14), (NSE, U, "Dividend - Rs 0 .70 Per Share"), (BSE, F, "0.7", "Final Dividend - Rs. - 0.7000")),
    ("INE068L01024", date(2021, 2, 12), (NSE, INT, "Interim Dividend"), (BSE, INT, "1", "Interim Dividend - Rs. - 1.0000")),
    ("INE054A01027", date(2022, 3, 8), (NSE, INT, "Interim Dividend"), (BSE, INT, "2.5", "Interim Dividend - Rs. - 2.5000")),
    ("INE205A01025", date(2022, 3, 9), (NSE, INT, "Interim Dividend"), (BSE, INT, "13", "Interim Dividend - Rs. - 13.0000")),
    ("INE0FS801015", date(2022, 8, 12), (NSE, U, "Annual General Meeting/Dividend - Rs  Per Share"), (BSE, F, "0.85", "Final Dividend - Rs. - 0.8500")),
    ("INE228I01012", date(2022, 9, 22), (NSE, U, "Annual General Meeting/Dividend - Rs  Per Share"), (BSE, F, "0.5", "Final Dividend - Rs. - 0.5000")),
    ("INE083C01022", date(2023, 6, 23), (NSE, U, "Annual General Meeting/Dividend - Rs  Per Share"), (BSE, F, "4", "Final Dividend - Rs. - 4.0000")),
    ("INE256C01024", date(2023, 9, 1), (NSE, U, "Annual General Meeting/Dividend - Rs  Per Share"), (BSE, F, "3.25", "Final Dividend - Rs. - 3.2500")),
    ("INE076B01028", date(2024, 7, 15), (NSE, U, "Annual General Meeting/Dividend - Rs  Per Sh/"), (BSE, F, "4", "Final Dividend - Rs. - 4.0000")),
    ("INE976R01033", date(2025, 3, 19), (NSE, INT, "Interim Dividend - Rs Per 0.50 Share"), (BSE, INT, "0.5", "Interim Dividend - Rs. - 0.5000")),
]  # fmt: skip


@pytest.mark.parametrize(("isin", "ex", "silent", "stated"), _SILENT, ids=[c[0] for c in _SILENT])
def test_silence_yields_to_the_stated_amount(
    isin: str,
    ex: date,
    silent: tuple[str, DividendKind, str],
    stated: tuple[str, DividendKind, str, str],
) -> None:
    """Fails if the rule is removed (a RATIO_MISMATCH again) or inverted (the silent side's
    terms win and the booked amount is `None`)."""
    quiet = _row(isin, silent[0], ex, silent[1], None, silent[2])
    loud = _row(isin, stated[0], ex, stated[1], stated[2], stated[3])

    result = reconcile([quiet, loud])

    assert result.queue == ()
    (agreed,) = result.reconciled
    assert isinstance(agreed.terms, DividendTerms)
    assert agreed.terms.amount_inr == Decimal(stated[2])
    assert agreed.reconciliation_note is not None
    assert agreed.reconciliation_note.startswith("silence yields")
    assert f"amount taken from {stated[0]}" in agreed.reconciliation_note
    assert _booked([quiet, loud]) == [Decimal(stated[2])]


@pytest.mark.parametrize("raw", sorted({c[2][2] for c in _SILENT}))
def test_the_parser_still_never_guesses_the_silent_lines_amount(raw: str) -> None:
    """The amount comes from the other exchange: the parser reads none out of these lines."""
    action = parse_purpose(raw, source=NSE).action
    assert action is not None and isinstance(action.terms, DividendTerms)
    assert action.terms.amount_inr is None
    assert not states_dividend_amount(raw)


def test_a_line_the_parser_left_bare_for_two_amounts_is_not_silence() -> None:
    """`Rs 5 & Rs 10` is stored without an amount because it states two; that is a statement
    against BSE's ₹5, not silence. Fails if rule A trusts the parsed terms alone."""
    ex = date(2022, 3, 8)
    raw = "Interim Dividend - Rs 5 Per Share & Rs 10 Per Share"
    assert states_dividend_amount(raw)
    nse = _row("INE054A01027", NSE, ex, INT, None, raw)
    bse = _row("INE054A01027", BSE, ex, INT, "5", "Interim Dividend - Rs. - 5.0000")

    result = reconcile([nse, bse])

    assert result.reconciled == ()
    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]


def test_silence_on_a_busy_day_picks_no_partner() -> None:
    """A silent NSE record beside two BSE dividends could be either one's: no lone pair, so the
    rule does not choose, and the day stays queued."""
    ex = date(2022, 8, 12)
    rows = [
        _row("INE0FS801015", NSE, ex, U, None, "Annual General Meeting/Dividend - Rs  Per Share"),
        _row("INE0FS801015", BSE, ex, F, "0.85", "Final Dividend - Rs. - 0.8500"),
        _row("INE0FS801015", BSE, ex, S, "0.4", "Special Dividend - Rs. - 0.4000"),
    ]
    result = reconcile(rows, single_source_policy=SingleSourcePolicy.ACCEPT)
    assert result.reconciled == ()
    assert ReconciliationReason.RATIO_MISMATCH in {c.reason for c in result.queue}


def test_two_silent_records_have_nothing_to_yield_to() -> None:
    ex = date(2022, 3, 9)
    rows = [
        _row("INE205A01025", NSE, ex, INT, None, "Interim Dividend"),
        _row("INE205A01025", BSE, ex, F, None, "Final Dividend"),
    ]
    assert reconcile(rows).reconciled == ()


# ── rule B: parts add up ───────────────────────────────────────────────────────────────────────

#: (ISIN, ex-date, the whole (source, kind, amount, raw), the parts [(source, kind, amount, raw)])
_PARTS: Final = [
    ("INE002S01010", date(2016, 9, 15), (BSE, F, "17.5", "Final Dividend - Rs. - 17.5000"),
     [(NSE, U, "10", "Annual General Meeting / Dividend - Rs 10/- Per Share + Special Dividend - Rs 7.5/- Per Share"),
      (NSE, S, "7.5", "Annual General Meeting / Dividend - Rs 10/- Per Share + Special Dividend - Rs 7.5/- Per Share")]),
    ("INE029A01011", date(2017, 2, 28), (BSE, INT, "19.5", "Interim Dividend - Rs. - 19.5000"),
     [(NSE, INT, "15.5", "Interim Dividend Rs 15.50 Per Share + Special Dividend Rs 4/- Per Share"),
      (NSE, S, "4", "Interim Dividend Rs 15.50 Per Share + Special Dividend Rs 4/- Per Share")]),
    # The reverse: NSE files the one record, BSE itemises.
    ("INE418H01029", date(2019, 2, 15), (NSE, INT, "3.5", "Interim Dividend - Rs 3.50 Per Share"),
     [(BSE, INT, "2", "Interim Dividend - Rs. - 2.0000"),
      (BSE, S, "1.5", "Special Dividend - Rs. - 1.5000")]),
    ("INE059D01020", date(2024, 9, 23), (BSE, U, "10", "Dividend - Rs. - 10.0000"),
     [(NSE, U, "5", "Annual General Meeting/Dividend - Rs 5 Per Share/Special Dividend - Rs 5 Per Share"),
      (NSE, S, "5", "Annual General Meeting/Dividend - Rs 5 Per Share/Special Dividend - Rs 5 Per Share")]),
    ("INE304A01026", date(2026, 8, 7), (BSE, F, "3", "Final Dividend - Rs. - 3.0000"),
     [(NSE, U, "2", "Dividend - Rs 2 Per Share & Special Dividend Re 1 Per Share"),
      (NSE, S, "1", "Dividend - Rs 2 Per Share & Special Dividend Re 1 Per Share")]),
    ("INE718F01018", date(2026, 9, 11), (BSE, F, "10", "Final Dividend - Rs. - 10.0000"),
     [(NSE, U, "3", "Dividend - Rs 3 Per Share & Special Dividend Rs 7 Per Share"),
      (NSE, S, "7", "Dividend - Rs 3 Per Share & Special Dividend Rs 7 Per Share")]),
]  # fmt: skip

_ITC_LINE: Final = (
    "Dividend - Rs 35 Per Share & Special Dividend Of Rs 100 Per Share/ Special Dividend Of Rs 30"
)


_Rec = tuple[str, DividendKind, str, str]


def _parts_rows(
    isin: str, ex: date, whole: _Rec, parts: list[_Rec]
) -> tuple[CorporateAction, list[CorporateAction]]:
    w = _row(isin, whole[0], ex, whole[1], whole[2], whole[3])
    p = [_row(isin, src, ex, kind, amt, raw) for src, kind, amt, raw in parts]
    return w, p


@pytest.mark.parametrize(("isin", "ex", "whole", "parts"), _PARTS, ids=[c[0] for c in _PARTS])
def test_parts_that_add_up_to_the_whole_book_the_whole_once(
    isin: str, ex: date, whole: _Rec, parts: list[_Rec]
) -> None:
    """Fails if the rule is removed (a mismatch), or books the parts beside the whole (the money
    twice), or books the parts instead (two factors where one is exact)."""
    w, p = _parts_rows(isin, ex, whole, parts)

    result = reconcile([w, *p], single_source_policy=SingleSourcePolicy.ACCEPT)

    assert result.queue == ()
    (event,) = result.reconciled
    assert event.terms == w.terms
    assert len(event.sources) == 1 + len(p)
    assert event.reconciliation_note is not None
    assert event.reconciliation_note.startswith("parts add up")
    assert _booked([w, *p]) == [Decimal(whole[2])]


def test_itcs_special_parts_meet_bses_special_beside_the_regular_pair() -> None:
    """INE182A01018 2025-07-09: the ₹35 regular pairs exactly; BSE's special ₹130 is NSE's
    ₹100 + ₹30. Booked: 35 and 130, each once."""
    ex = date(2025, 7, 9)
    rows = [
        _row("INE182A01018", BSE, ex, F, "35", "Final Dividend - Rs. - 35.0000"),
        _row("INE182A01018", BSE, ex, S, "130", "Special Dividend - Rs. - 130.0000"),
        _row("INE182A01018", NSE, ex, U, "35", _ITC_LINE),
        _row("INE182A01018", NSE, ex, S, "100", _ITC_LINE),
        _row("INE182A01018", NSE, ex, S, "30", _ITC_LINE, component="SPECIAL#2"),
    ]
    result = reconcile(rows, single_source_policy=SingleSourcePolicy.ACCEPT)
    assert result.queue == ()
    assert sorted(_booked(rows)) == [Decimal("35"), Decimal("130")]  # type: ignore[type-var]


@pytest.mark.parametrize("whole_amount", ["17.49", "17.51", "20"])
def test_parts_that_do_not_add_up_stay_a_mismatch(whole_amount: str) -> None:
    """Exact `Decimal` sum or no rule: a paisa off is different money."""
    w, p = _parts_rows(
        "INE002S01010", date(2016, 9, 15), (BSE, F, whole_amount, "Final"), _PARTS[0][3]
    )

    result = reconcile([w, *p], single_source_policy=SingleSourcePolicy.ACCEPT)

    assert result.reconciled == ()
    assert ReconciliationReason.RATIO_MISMATCH in {c.reason for c in result.queue}


def test_a_whole_that_is_not_alone_in_its_feed_is_no_whole() -> None:
    """BSE files 17.5 *and* another record that day: which of BSE's is NSE's sum is a choice,
    so the rule makes none."""
    w, p = _parts_rows("INE002S01010", date(2016, 9, 15), _PARTS[0][2], _PARTS[0][3])
    other = _row("INE002S01010", BSE, date(2016, 9, 15), S, "2", "Special Dividend - Rs. - 2.0000")

    result = reconcile([w, other, *p], single_source_policy=SingleSourcePolicy.ACCEPT)

    assert ReconciliationReason.RATIO_MISMATCH in {c.reason for c in result.queue}
    assert all(r.terms != w.terms for r in result.reconciled)


def test_a_part_with_no_stated_amount_is_no_part() -> None:
    w, p = _parts_rows("INE304A01026", date(2026, 8, 7), _PARTS[4][2], _PARTS[4][3])
    silent = p[1].model_copy(update={"terms": DividendTerms(dividend_kind=S)})
    result = reconcile([w, p[0], silent], single_source_policy=SingleSourcePolicy.ACCEPT)
    assert all(r.terms != w.terms for r in result.reconciled)


# ── rule C: name only ──────────────────────────────────────────────────────────────────────────

_NAMES: Final = [
    ("INE177A01018", date(2018, 5, 24), (INT, "Interim Dividend - Rs. - 202.0000"), (S, "Special Dividend- Rs 202 Per Share"), "202"),
    ("INE100A01010", date(2019, 11, 4), (INT, "Interim Dividend - Rs. - 12.5000"), (S, "Special Dividend - Rs 12.5 Per Sh"), "12.5"),
    ("INE347G01014", date(2019, 11, 7), (S, "Special Dividend - Rs. - 5.5000"), (INT, "Interim Dividend - Rs 5.50 Per Share"), "5.5"),
    ("INE386A01023", date(2023, 4, 26), (F, "Final Dividend - Rs. - 8.2500"), (INT, "Interim Dividend - Rs  8.25 Per Share"), "8.25"),
]  # fmt: skip


@pytest.mark.parametrize(("isin", "ex", "bse", "nse", "amount"), _NAMES, ids=[c[0] for c in _NAMES])
def test_the_same_money_under_different_names_reconciles_on_the_amount(
    isin: str,
    ex: date,
    bse: tuple[DividendKind, str],
    nse: tuple[DividendKind, str],
    amount: str,
) -> None:
    """Fails if the rule is removed (a mismatch) or the note drops what either side said."""
    rows = [
        _row(isin, BSE, ex, bse[0], amount, bse[1]),
        _row(isin, NSE, ex, nse[0], amount, nse[1]),
    ]

    result = reconcile(rows)

    assert result.queue == ()
    (agreed,) = result.reconciled
    assert isinstance(agreed.terms, DividendTerms)
    assert agreed.terms.amount_inr == Decimal(amount)
    note = agreed.reconciliation_note or ""
    assert note.startswith("name only")
    assert f"{BSE} said {bse[0].value}" in note and f"{NSE} said {nse[0].value}" in note
    assert _booked(rows) == [Decimal(amount)]


def test_different_names_and_different_money_stay_a_mismatch() -> None:
    """Ingersoll's ₹202 special against an interim of ₹200: the names were never the problem."""
    ex = date(2018, 5, 24)
    rows = [
        _row("INE177A01018", BSE, ex, INT, "200", "Interim Dividend - Rs. - 200.0000"),
        _row("INE177A01018", NSE, ex, S, "202", "Special Dividend- Rs 202 Per Share"),
    ]
    result = reconcile(rows)
    assert result.reconciled == ()
    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]


def test_beml_and_coralfinac_still_flag_under_the_rules() -> None:
    """The two genuine conflicts: different money, whatever the kinds. No rule resolves them.

    CORALFINAC stays with a human. BEML's is settled only by the owner's ruling in
    `manual_actions.yaml` (2026-11-01; `test_ca_superseded_dividend`), so it is judged here
    without that ruling: the rules alone must still flag it.
    """
    beml = [
        _row("INE258A01024", BSE, date(2026, 9, 22), F, "0.55", "Final Dividend - Rs. - 0.5500"),
        _row("INE258A01024", NSE, date(2026, 9, 22), U, "12.28", "Dividend - Rs 12.28 Per Share"),
    ]
    coral = [
        _row("INE558D01021", BSE, date(2017, 9, 6), U, "0.2", "Dividend - Rs. - 0.2000"),
        _row(
            "INE558D01021",
            NSE,
            date(2017, 9, 6),
            U,
            "1",
            "Annual General Meeting/Dividend - Re 1/- Per Share",
        ),
    ]
    for rows in (beml, coral):
        result = reconcile(rows, single_source_policy=SingleSourcePolicy.ACCEPT, superseded=())
        assert result.reconciled == ()
        assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]
    # The file's rulings do not reach CORALFINAC: it flags with them loaded too.
    result = reconcile(coral, single_source_policy=SingleSourcePolicy.ACCEPT)
    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]


# ── Patanjali: NSE's second Interim, restored from its own L0 ──────────────────────────────────

PATANJALI: Final = "INE619A01035"
PATANJALI_EX: Final = date(2026, 8, 21)


def _patanjali(bse_amount: str = "2.3") -> list[CorporateAction]:
    return [
        _row(PATANJALI, NSE, PATANJALI_EX, INT, "0.8", "Interim Dividend - Re 0.80 Per Share"),
        _row(
            PATANJALI,
            BSE,
            PATANJALI_EX,
            INT,
            bse_amount,
            f"Interim Dividend - Rs. - {Decimal(bse_amount):.4f}",
        ),
    ]


def _restored() -> tuple[CorporateAction, ...]:
    return load_manual_actions().restored_dividends(isin=PATANJALI)


def test_the_curated_row_restores_nses_own_record() -> None:
    curated = load_manual_actions()
    (row,) = [d for d in curated.dividends if d.isin == PATANJALI]
    assert row.feed == NSE and row.component == "INTERIM#2"
    assert row.terms == DividendTerms(dividend_kind=INT, amount_inr=Decimal("1.50"))
    assert row.sources[0].l0_key.startswith(f"{NSE}/")
    assert "Interim Dividend - Rs 1.50 Per Share" in row.sources[0].quote
    # Kept out of the price events every L2 path composes into a factor chain.
    assert all(a.action_type is not ActionType.DIVIDEND for a in curated.actions)
    (action,) = _restored()
    assert action.source == MANUAL_SOURCE and action.component == "INTERIM#2"


def test_patanjali_books_bses_two_thirty_once() -> None:
    """Fails without the curated row (0.80 against 2.30 is a mismatch) and if the restored 1.50
    or NSE's 0.80 is booked beside BSE's 2.30."""
    rows = [*_patanjali(), *_restored()]
    result = reconcile(rows, single_source_policy=SingleSourcePolicy.ACCEPT)
    assert result.queue == ()
    (event,) = result.reconciled
    assert event.terms == DividendTerms(dividend_kind=INT, amount_inr=Decimal("2.3"))
    assert MANUAL_SOURCE in event.source_ids
    assert _booked(rows) == [Decimal("2.3")]


def test_without_the_restored_record_patanjali_is_a_mismatch() -> None:
    result = reconcile(_patanjali(), single_source_policy=SingleSourcePolicy.ACCEPT)
    assert result.reconciled == ()
    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]


def test_a_restored_record_that_completes_nothing_is_never_booked() -> None:
    """BSE says 2.40: 0.80 + 1.50 is not it. The day stays disputed and the restored record is
    neither admitted on its own nor folded into the book."""
    rows = [*_patanjali("2.4"), *_restored()]
    result = reconcile(rows, single_source_policy=SingleSourcePolicy.ACCEPT)
    assert all(MANUAL_SOURCE not in r.source_ids for r in result.reconciled)
    assert ReconciliationReason.RATIO_MISMATCH in {c.reason for c in result.queue}
    assert Decimal("1.50") not in _booked(rows)


def test_a_restored_record_with_no_stored_sibling_is_left_out() -> None:
    """A store that never ingested that day (CI's, a test's) gets no flag and no dividend."""
    result = reconcile(_restored(), single_source_policy=SingleSourcePolicy.ACCEPT)
    assert result.reconciled == () and result.queue == ()
    assert collapse_reconciled_rows([], restored=_restored()) == ()


_DIV_ROW: Final = """\
actions:
  - isin: INE619A01035
    company: P
    action_type: DIVIDEND
    feed: nse_corp_actions
    dividend_kind: INTERIM
    amount_inr: "1.50"
    component: INTERIM#2
    ex_date: 2026-08-21
    knowable_date: 2026-09-03
    checked: 2026-11-01
    sources:
      - l0_key: nse_corp_actions/2025/09/x.json
        quote: q
explained_moves: []
"""


@pytest.mark.parametrize(
    ("patch", "bad"),
    [
        ("feed: nse_corp_actions", "feed: nse_announcements"),
        ("l0_key: nse_corp_actions/2025/09/x.json", "l0_key: bse_corp_actions/2016/09/x.json"),
        ("component: INTERIM#2", "component: FINAL#2"),
        ('amount_inr: "1.50"', "amount_inr: 1.5"),
        ("dividend_kind: INTERIM", "dividend_kind: BONUS"),
    ],
)
def test_a_malformed_restored_dividend_is_refused(tmp_path: Path, patch: str, bad: str) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(_DIV_ROW)
    assert len(load_manual_actions(good).dividends) == 1
    path = tmp_path / "bad.yaml"
    assert _DIV_ROW.count(patch) == 1
    path.write_text(_DIV_ROW.replace(patch, bad))
    with pytest.raises(ManualActionsError):
        load_manual_actions(path)
