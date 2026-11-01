"""The share-basis guard (`quality.share_basis`) and the pieces it stands on, on real L0 excerpts.

Fixtures (`tests/fixtures/share_basis/`) are verbatim lines of NSE's book-closure files from L0
and NSE EQ closes from L1:

* AvenuesAI 2026 — ``CNSLDATNRE1 TO RS10``, record 2026-10-13, EX_DT blank, reissued across a
  seven-day suspension (13.83 -> 130.05, x9.40). The case the guard exists for.
* Kaushalya 2024 — ``CNSLDATNRS10 TO RS1000``, record 2024-01-12, EX_DT blank: an older T+1
  reissue the guard admits on its own (8.70 -> 775.05 against x100).
* Shekhawati 2024 — ``CNSLDATN RE 1 TO RS 10``, EX_DT blank, but the next EQ close is 21 months
  later at x25: stated terms the price contradicts, so it fails closed.
* Signet 2018 — ``CONSOLIDATION-RE1 TO RS10``, EX_DT blank in the T+2 era: never derived.

Every rule is tested both ways, and the ones that touch a factor fail if the factor is inverted.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.corpactions.bc_terms import face_value_change
from dataplatform.corpactions.ex_date_rule import ExDateRule, derive_ex_date, nse_session
from dataplatform.corpactions.factors import FactorChain, with_events
from dataplatform.corpactions.share_basis_state import (
    BC_SOURCE,
    REISSUE_SOURCE,
    AdmissionRule,
    AdmittedAction,
    QuarantineWindow,
    ShareBasisState,
    current_state,
    load_state,
    write_state,
)
from dataplatform.corpactions.taxonomy import ActionType, FaceValueTerms, RatioTerms
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.ingest.nse.pr_bundle.bc import parse_bc
from dataplatform.quality.l2_continuity import (
    AdjustedStep,
    L2ContinuityReport,
    StepClass,
    classify_steps,
    findings,
)
from dataplatform.quality.share_basis import (
    BcFaceValueRow,
    Decision,
    Outcome,
    PriceBar,
    Reissue,
    decide,
    guard_findings,
    scan_quarantines,
)
from dataplatform.query.price_quarantine import PriceQuarantine
from dataplatform.store.l2 import _curated_for_chain, curated_actions

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "share_basis"

AVENUES_OLD, AVENUES_NEW = "INE483S01020", "INE483S01038"
KAUSHALYA_OLD, KAUSHALYA_NEW = "INE234I01010", "INE234I01028"
SHEKHAWATI_OLD, SHEKHAWATI_NEW = "INE268L01020", "INE268L01046"
SIGNET = "INE529F01035"

_LINEAGE = {
    AVENUES_OLD: (AVENUES_NEW, date(2026, 10, 19)),
    KAUSHALYA_OLD: (KAUSHALYA_NEW, date(2024, 3, 2)),
    SHEKHAWATI_OLD: (SHEKHAWATI_NEW, date(2025, 8, 11)),
}


_FIRST_SYMBOL = {
    "bc12102026.csv": "CCAVENUE",
    "Bc080818.csv": "SIGNET",
    "Bc090124.csv": "KAUSHALYA",
    "Bc270824.csv": "SHEKHAWATI",
}


def _survivor(isin: str) -> str:
    return _LINEAGE[isin][0] if isin in _LINEAGE else isin


def _chain(isin: str) -> Sequence[str]:
    return (*sorted(p for p, (s, _) in _LINEAGE.items() if s == isin), isin)


def _bc(member: str, *, isin: str, knowable: date, symbol: str | None = None) -> BcFaceValueRow:
    """The fixture's bc line (of `symbol`, default the first) through the real parser and reader."""
    symbol = symbol or _FIRST_SYMBOL[member]
    text = "\n".join(
        line for line in (FIXTURES / member).read_text().splitlines() if not line.startswith("#")
    )
    [row] = [
        r
        for r in parse_bc(text.encode("latin-1"), filename=member, knowable_date=knowable)
        if r.symbol == symbol
    ]
    terms = face_value_change(row.purpose)
    assert terms is not None
    return BcFaceValueRow(
        isin=isin,
        symbol=row.symbol,
        purpose=row.purpose,
        terms=terms,
        record_date=row.record_date,
        stated_ex_date=row.ex_date,
        knowable_date=row.knowable_date,
        l0_key=f"nse_pr_bundle/{member}",
    )


def _bars() -> dict[str, list[PriceBar]]:
    out: dict[str, list[PriceBar]] = {}
    lines = [line for line in (FIXTURES / "prices.csv").read_text().splitlines() if line[0] != "#"]
    for r in csv.DictReader(lines):
        out.setdefault(r["isin"], []).append(
            PriceBar(
                isin=r["isin"],
                trade_date=date.fromisoformat(r["trade_date"]),
                close=Decimal(r["close"]),
                prev_close=Decimal(r["prev_close"]) if r["prev_close"] else None,
            )
        )
    return out


def _decide(
    *,
    bc_rows: Sequence[BcFaceValueRow] = (),
    reissues: Sequence[Reissue] = (),
    recorded: Sequence[CorporateAction] = (),
    as_of: date = date(2026, 10, 30),
) -> Decision:
    return decide(
        bc_rows=bc_rows,
        reissues=reissues,
        recorded=recorded,
        bars=_bars(),
        survivor_of=_survivor,
        chain_of=_chain,
        is_session=nse_session,
        as_of=as_of,
    )


def _avenues_bc() -> BcFaceValueRow:
    return _bc("bc12102026.csv", isin=AVENUES_OLD, knowable=date(2026, 10, 12))


def _split(
    isin: str, ex: date, a: int, b: int, *, source: str = "nse_corp_actions"
) -> CorporateAction:
    return CorporateAction(
        isin=isin,
        ex_date=ex,
        action_type=ActionType.SPLIT,
        terms=FaceValueTerms(from_value=Decimal(a), to_value=Decimal(b)),
        source=source,
        raw_text="x",
        knowable_date=ex,
    )


# ── the bc face-value reader ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("purpose", "from_value", "to_value"),
    [
        ("CNSLDATNRE1 TO RS10", 1, 10),
        ("CNSLDATNRS10 TO RS1000   ", 10, 1000),
        ("CONSOLIDATION-RE1 TO RS10", 1, 10),
        ("FVSPLT FRM RS 10 TO RE 1", 10, 1),
        ("FV SPLT FRM RS 10 TO RS 2", 10, 2),
        ("BONUS 2:1/FVS RS.5TORE.1", 5, 1),
        ("BON-1:25/SPLIT RS10TORS.5", 10, 5),
        ("DIV-0.06/CON RE 1TO RS10", 1, 10),
        ("FV SPLIT RS10/- TO RE1/-", 10, 1),
    ],
)
def test_the_bc_face_value_shapes_read_as_stated(
    purpose: str, from_value: int, to_value: int
) -> None:
    terms = face_value_change(purpose)
    # Inverted (to/from swapped) a consolidation would read as a split: the factor's sign.
    assert terms == FaceValueTerms(from_value=Decimal(from_value), to_value=Decimal(to_value))


@pytest.mark.parametrize(
    "purpose",
    [
        "SPLDIV - RS 5 PER SH",  # a special dividend, not a split
        "CAP RED RS 10 TO RS 2",  # capital reduction: no re-basing of the holding
        "PART REDM FV 100 TO 50",  # debt partial redemption
        "FV SPLT FRM RS 10 TO",  # a term missing
        "BONUS 1:1",
        "AGM/DIV-SPL RS5+FIN RS10",
    ],
)
def test_what_is_not_a_stated_face_value_change_reads_as_nothing(purpose: str) -> None:
    assert face_value_change(purpose) is None


# ── item 1: a blank ex-date, derived only by the T+1 rule ────────────────────────────────────


def test_avenuesai_blank_ex_date_is_derived_as_the_record_date_and_admitted() -> None:
    row = _avenues_bc()
    assert row.stated_ex_date is None and row.record_date == date(2026, 10, 13)
    decision = _decide(bc_rows=[row])
    [action] = decision.admitted
    assert action.isin == AVENUES_NEW and action.filed_against_isin == AVENUES_OLD
    assert action.ex_date == date(2026, 10, 13)
    assert action.ex_date_rule == ExDateRule.T1_RECORD_DATE.value
    assert action.source == BC_SOURCE and action.rule == AdmissionRule.BC_STATED_TERMS
    # x10 on every earlier bar: 13.83 reads 138.30 against 130.05.
    chain = with_events(FactorChain(isin=AVENUES_NEW, rows=()), [action.as_action()])
    assert chain.price_factor_asof(date(2026, 10, 12)) == Decimal(10)
    assert action.as_action().ex_date_rule == "T1_RECORD_DATE"
    assert not decision.quarantined


def test_a_t2_era_blank_ex_date_is_never_derived() -> None:
    """Signet 2018: the same blank EX_DT, a record date before T+1 — no ex-date, no factor."""
    row = _bc("Bc080818.csv", isin=SIGNET, knowable=date(2018, 8, 8))
    assert row.stated_ex_date is None and row.record_date == date(2018, 8, 14)
    assert derive_ex_date(row.record_date, is_session=nse_session) is None
    decision = _decide(bc_rows=[row])
    assert not decision.admitted and not decision.quarantined
    [verdict] = decision.verdicts
    assert verdict.outcome is Outcome.NOT_DERIVED and "before T+1" in verdict.detail


def test_a_t1_rule_needs_a_session_record_date() -> None:
    saturday = date(2026, 10, 17)
    assert derive_ex_date(saturday, is_session=nse_session) is None
    assert derive_ex_date(date(2026, 10, 13), is_session=nse_session) is not None
    # A year the calendar does not cover is not a session: decline, never guess.
    assert nse_session(date(2099, 1, 5)) is False


def test_an_older_t1_reissue_is_admitted_on_its_own_terms() -> None:
    """Kaushalya 2024: x100 stated, 8.70 -> 775.05 five months later (residual 0.89)."""
    row = _bc("Bc090124.csv", isin=KAUSHALYA_OLD, knowable=date(2024, 1, 9))
    decision = _decide(bc_rows=[row])
    [action] = decision.admitted
    assert action.ex_date == date(2024, 1, 12) and action.ex_date_rule == "T1_RECORD_DATE"
    assert (action.from_value, action.to_value) == (Decimal(10), Decimal(1000))


def test_a_bc_line_a_feed_or_curated_row_already_states_is_left_to_it() -> None:
    recorded = [_split(AVENUES_NEW, date(2026, 10, 14), 1, 10, source="manual_curated")]
    decision = _decide(bc_rows=[_avenues_bc()], recorded=recorded)
    assert not decision.admitted
    assert [v.outcome for v in decision.verdicts] == [Outcome.RECORDED]


def test_stated_terms_the_price_contradicts_fail_closed() -> None:
    """Shekhawati: x10 stated, x25.35 traded across 21 months — quarantined, never admitted."""
    row = _bc("Bc270824.csv", isin=SHEKHAWATI_OLD, knowable=date(2024, 8, 27))
    decision = _decide(bc_rows=[row])
    assert not decision.admitted
    [window] = decision.quarantined
    assert window.isin == SHEKHAWATI_NEW and window.first_session == date(2025, 8, 11)
    assert window.kind == "stated_terms_disagree"
    [finding] = guard_findings(decision)
    assert finding.severity == "ERROR" and finding.isin == SHEKHAWATI_NEW
    assert finding.detail["quarantined_before"] == "2025-08-11"


def test_an_inverted_ratio_would_fail_closed_not_admit() -> None:
    """Read Re 1 -> Rs 10 backwards (a 10:1 split) and the residual is ~94: refused."""
    row = _avenues_bc()
    flipped = replace(row, terms=FaceValueTerms(from_value=Decimal(10), to_value=Decimal(1)))
    decision = _decide(bc_rows=[flipped])
    assert not decision.admitted and len(decision.quarantined) == 1


def test_an_ex_date_not_yet_traded_is_pending_not_admitted() -> None:
    # The evening of 2026-10-12: the line is broadcast, nothing has traded on the new basis yet.
    cut = date(2026, 10, 12)
    bars = {k: [b for b in v if b.trade_date <= cut] for k, v in _bars().items()}
    decision = decide(
        bc_rows=[_avenues_bc()],
        reissues=(),
        recorded=(),
        bars=bars,
        survivor_of=_survivor,
        chain_of=_chain,
        is_session=nse_session,
        as_of=cut,
    )
    assert not decision.admitted
    assert [v.outcome for v in decision.verdicts] == [Outcome.PENDING]


# ── item 2: a reissue with a face-value change ───────────────────────────────────────────────


def _avenues_edge(before: str = "1", after: str = "10") -> Reissue:
    return Reissue(
        predecessor=AVENUES_OLD,
        successor=AVENUES_NEW,
        effective_date=date(2026, 10, 19),
        face_value_before=Decimal(before),
        face_value_after=Decimal(after),
    )


def test_a_reissue_whose_price_agrees_with_its_face_values_is_admitted() -> None:
    """No bc line, no feed row: the master's Re 1 -> Rs 10 and x9.40 over seven days (0.94)."""
    decision = _decide(reissues=[_avenues_edge()])
    [action] = decision.admitted
    assert action.source == REISSUE_SOURCE and action.rule == AdmissionRule.REISSUE_FACE_VALUE
    assert action.ex_date == date(2026, 10, 19)
    assert (action.from_value, action.to_value) == (Decimal(1), Decimal(10))
    assert "PREV_CLOSE 138.30" in action.evidence and "(matches)" in action.evidence


def test_a_reissue_whose_price_disagrees_with_its_face_values_fails_closed() -> None:
    """The master says Re 1 -> Rs 5 (x5); the price moved x9.40: quarantine, no factor."""
    decision = _decide(reissues=[_avenues_edge(after="5")])
    assert not decision.admitted
    [window] = decision.quarantined
    assert window.kind == "reissue_face_value_disagrees"
    assert window.isin == AVENUES_NEW and window.first_session == date(2026, 10, 19)


def test_a_reissue_a_recorded_factor_explains_needs_nothing() -> None:
    recorded = [_split(AVENUES_NEW, date(2026, 10, 13), 1, 10, source="manual_curated")]
    decision = _decide(reissues=[_avenues_edge()], recorded=recorded)
    assert not decision.admitted and not decision.quarantined
    assert [v.outcome for v in decision.verdicts] == [Outcome.EXPLAINED]


def test_one_event_recorded_under_both_isins_counts_once() -> None:
    """TAALTECH's shape: BSE's row under the retired ISIN, NSE's under the new — one factor."""
    recorded = [
        _split(AVENUES_OLD, date(2026, 10, 13), 1, 10, source="bse_corp_actions"),
        _split(AVENUES_NEW, date(2026, 10, 13), 1, 10, source="nse_corp_actions"),
    ]
    decision = _decide(reissues=[_avenues_edge()], recorded=recorded)
    assert [v.outcome for v in decision.verdicts] == [Outcome.EXPLAINED]


def test_a_reissue_without_a_face_value_change_is_left_to_the_scan() -> None:
    decision = _decide(reissues=[_avenues_edge(after="1")])
    assert decision == Decision()


# ── item 3: the continuity scan's SHARE_BASIS class ──────────────────────────────────────────


def _step(prev: date, day: date, ratio: str, isin: str = AVENUES_NEW) -> AdjustedStep:
    r = Decimal(ratio)
    return AdjustedStep(
        isin=isin,
        exchange="NSE",
        prev_date=prev,
        trade_date=day,
        prev_close=Decimal("13.83"),
        close=Decimal("13.83") * r,
        ratio=r,
        tr_ratio=r,
        factor_changed=False,
    )


def _classify(
    *steps: AdjustedStep, reissues: dict[str, tuple[date, ...]] | None = None
) -> list[StepClass]:
    return [
        cls
        for _, cls in classify_steps(
            steps,
            structural_dates={},
            reissue_dates=reissues or {},
            threshold=Decimal(2),
            max_gap_days=5,
        )
    ]


def test_avenuesai_long_gap_step_across_its_reissue_is_share_basis_not_long_gap() -> None:
    step = _step(date(2026, 10, 12), date(2026, 10, 19), "9.4035")
    assert _classify(step, reissues={AVENUES_NEW: (date(2026, 10, 19),)}) == [StepClass.SHARE_BASIS]
    # Without the edge it is still a clean 10x across seven days.
    assert _classify(step) == [StepClass.SHARE_BASIS]


def test_a_long_gap_step_off_any_multiple_or_past_a_month_stays_long_gap() -> None:
    assert _classify(_step(date(2026, 10, 12), date(2026, 10, 19), "2.7")) == [StepClass.LONG_GAP]
    assert _classify(_step(date(2026, 8, 12), date(2026, 10, 19), "10")) == [StepClass.LONG_GAP]
    # A reissue makes it share-basis at any gap.
    assert _classify(
        _step(date(2026, 8, 12), date(2026, 10, 19), "2.7"),
        reissues={AVENUES_NEW: (date(2026, 10, 19),)},
    ) == [StepClass.SHARE_BASIS]


def test_a_share_basis_step_fails_the_check_with_an_error_naming_it() -> None:
    step = _step(date(2026, 10, 12), date(2026, 10, 19), "9.4035")
    report = L2ContinuityReport(
        threshold=Decimal(2),
        max_gap_days=5,
        partitions=1,
        steps=((step, StepClass.SHARE_BASIS),),
        retired_partitions=(),
        reissue_dates={AVENUES_NEW: (date(2026, 10, 19),)},
    )
    assert not report.passed
    [finding] = findings(report, logical_date=date(2026, 11, 1))
    assert finding.severity == "ERROR" and finding.isin == AVENUES_NEW
    assert finding.detail["kind"] == "share_basis_step"
    assert "reissue effective 2026-10-19" in str(finding.detail["reason"])
    [window] = scan_quarantines(report)
    assert window.first_session == date(2026, 10, 19) and window.isin == AVENUES_NEW


# ── item 4: the quarantine, and its clearing ─────────────────────────────────────────────────


def test_the_guard_quarantine_withholds_pre_step_bars_and_clears_with_the_ca_row(
    tmp_path: Path,
) -> None:
    """Fail closed, then clear: no factor -> quarantine; the CA row arrives -> admitted again."""
    shekhawati = _bc("Bc270824.csv", isin=SHEKHAWATI_OLD, knowable=date(2024, 8, 27))
    first = _decide(bc_rows=[shekhawati])
    write_state(
        ShareBasisState(admitted=first.admitted, quarantined=first.quarantined),
        data_root=tmp_path,
    )
    quarantine = PriceQuarantine().merged(current_state(data_root=tmp_path).windows())
    assert not quarantine.admits(SHEKHAWATI_NEW, date(2025, 8, 8))
    assert quarantine.admits(SHEKHAWATI_NEW, date(2025, 8, 11))
    assert quarantine.admits(AVENUES_NEW, date(2020, 1, 1))  # nobody else touched

    # A feed later records the event: the bc line is RECORDED, nothing quarantined, state clears.
    recorded = [_split(SHEKHAWATI_NEW, date(2024, 8, 28), 1, 10)]
    second = _decide(bc_rows=[shekhawati], recorded=recorded)
    assert not second.quarantined and not second.admitted
    write_state(
        ShareBasisState(admitted=second.admitted, quarantined=second.quarantined),
        data_root=tmp_path,
    )
    cleared = PriceQuarantine().merged(current_state(data_root=tmp_path).windows())
    assert cleared.admits(SHEKHAWATI_NEW, date(2025, 8, 8))
    # The guard's own ERROR is gone too: nothing left to raise, so the scoped persist closes it.
    assert guard_findings(second) == ()


def test_the_default_quarantine_reads_the_guard_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataplatform.query import price_quarantine

    window = QuarantineWindow(
        isin=AVENUES_NEW,
        first_session=date(2026, 10, 19),
        ratio="x9.4035",
        kind="share_basis_step",
        reason="test",
    )
    write_state(ShareBasisState(quarantined=(window,)), data_root=tmp_path)
    monkeypatch.setattr(
        price_quarantine, "current_state", lambda: current_state(data_root=tmp_path)
    )
    q = price_quarantine.default_price_quarantine()
    assert not q.admits(AVENUES_NEW, date(2026, 10, 12))
    assert q.admits(AVENUES_NEW, date(2026, 10, 19))
    # The curated D22 windows are still there.
    assert not q.admits("INE230A01023", date(2006, 9, 11))
    write_state(ShareBasisState(), data_root=tmp_path)
    assert price_quarantine.default_price_quarantine().admits(AVENUES_NEW, date(2026, 10, 12))


def test_the_state_round_trips_and_an_absent_file_is_empty(tmp_path: Path) -> None:
    assert load_state(data_root=tmp_path) == ShareBasisState()
    action = _decide(bc_rows=[_avenues_bc()]).admitted[0]
    state = ShareBasisState(admitted=(action,))
    write_state(state, data_root=tmp_path)
    first = (tmp_path / "L2" / "share_basis" / "state.json").read_bytes()
    assert load_state(data_root=tmp_path) == state
    write_state(state, data_root=tmp_path)
    assert (tmp_path / "L2" / "share_basis" / "state.json").read_bytes() == first


# ── composition: L2 adjusts by an admitted factor, and it yields to a recorded one ────────────


def _admitted(ex: date) -> AdmittedAction:
    return AdmittedAction(
        isin=AVENUES_NEW,
        filed_against_isin=AVENUES_OLD,
        ex_date=ex,
        from_value=Decimal(1),
        to_value=Decimal(10),
        source=BC_SOURCE,
        rule=AdmissionRule.BC_STATED_TERMS,
        raw_text="CNSLDATNRE1 TO RS10",
        knowable_date=date(2026, 10, 12),
        ex_date_rule="T1_RECORD_DATE",
        evidence="test",
    )


def test_l2_composes_the_admitted_factor_for_the_survivor_chain(tmp_path: Path) -> None:
    write_state(ShareBasisState(admitted=(_admitted(date(2026, 10, 13)),)), data_root=tmp_path)
    found = [
        a
        for a in _curated_for_chain(AVENUES_NEW, (AVENUES_OLD, AVENUES_NEW), data_root=tmp_path)
        if a.source == BC_SOURCE
    ]
    assert [(a.isin, a.ex_date, a.ex_date_rule) for a in found] == [
        (AVENUES_NEW, date(2026, 10, 13), "T1_RECORD_DATE")
    ]


def test_an_admitted_factor_yields_to_a_recorded_event_within_a_week() -> None:
    admitted = _admitted(date(2026, 10, 13)).as_action()
    feed_a_day_later = _split(AVENUES_NEW, date(2026, 10, 14), 1, 10)
    assert curated_actions(AVENUES_NEW, [feed_a_day_later], [admitted]) == ()
    far = _split(AVENUES_NEW, date(2026, 10, 13) - timedelta(days=30), 1, 10)
    assert curated_actions(AVENUES_NEW, [far], [admitted]) == (admitted,)
    bonus = CorporateAction(
        isin=AVENUES_NEW,
        ex_date=date(2026, 10, 15),
        action_type=ActionType.BONUS,
        terms=RatioTerms(new_shares=Decimal(1), held_shares=Decimal(1)),
        source="nse_corp_actions",
        raw_text="x",
        knowable_date=date(2026, 10, 15),
    )
    # Any recorded share-basis event that near stands in for it: fail to the feed, never double.
    assert curated_actions(AVENUES_NEW, [bonus], [admitted]) == ()


def test_a_recorded_demerger_inside_the_gap_explains_the_step() -> None:
    """Raymond 2025: 1564.30 on 05-13, 619.20 on 05-28, demerger ex 05-14 inside the gap."""
    raymond = "INE301A01014"
    step = _step(date(2025, 5, 13), date(2025, 5, 28), "0.3958", isin=raymond)
    inside = classify_steps(
        [step],
        structural_dates={raymond: (date(2025, 5, 14),)},
        threshold=Decimal(2),
        max_gap_days=5,
    )
    assert [c for _, c in inside] == [StepClass.STRUCTURAL]
    # A demerger months away explains nothing: the clean x2.5 over 15 days is a share-basis step.
    far = classify_steps(
        [step],
        structural_dates={raymond: (date(2025, 3, 1),)},
        threshold=Decimal(2),
        max_gap_days=5,
    )
    assert [c for _, c in far] == [StepClass.SHARE_BASIS]
