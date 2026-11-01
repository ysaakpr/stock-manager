"""D2 lineage: how a rebuild's derived edges sit against the MANUAL ones (`merge_with_manual`).

The first MANUAL edge is AvenuesAI's reissue across a suspension, INE483S01020 -> INE483S01038
effective 2026-10-19, recorded by `repair_avenuesai` (#142). L1 contiguity derives the same pair, so
before this rule a rebuild wrote it a second time and stopped on the one-edge-per-predecessor index.
Every test here fails if the precedence is inverted: a derived edge preferred over a MANUAL one, a
conflict silently resolved either way, or an agreeing edge written twice. Pure; no I/O.
"""

from __future__ import annotations

from datetime import date

import pytest

from dataplatform.identity.lineage import (
    EqPresence,
    LineageConflictError,
    LineageConflictKind,
    LineageEdge,
    ManualEdge,
    merge_with_manual,
    plan_registrations,
)

_OLDEST = "INE483S01012"  # INFIBEAM to 2017-08-31
_OLD = "INE483S01020"  # 2017-09-01 .. 2026-10-12
_NEW = "INE483S01038"  # from 2026-10-19, after four sessions without the symbol
_OTHER = "INE483S01046"  # an ISIN of the same issuer the derivation might be fooled into
_SWITCH = date(2026, 10, 19)

AVENUESAI = ManualEdge(_OLD, _NEW, _SWITCH, "CCAVENUE")


def _edge(predecessor: str, successor: str, effective: date = _SWITCH) -> LineageEdge:
    return LineageEdge(
        predecessor_isin=predecessor,
        successor_isin=successor,
        effective_date=effective,
        gap_sessions=4,
        symbol_at_change="CCAVENUE",
        corroborating_action=None,
    )


EARLIER = _edge(_OLDEST, _OLD, date(2017, 9, 1))


def test_a_derived_edge_that_restates_the_manual_one_is_not_written_twice() -> None:
    merge = merge_with_manual([EARLIER, _edge(_OLD, _NEW)], [AVENUESAI])

    assert merge.derived == (EARLIER,)
    assert merge.agreeing == (_edge(_OLD, _NEW),)
    assert merge.conflicts == ()
    assert merge.overridden == ()


def test_the_manual_edge_is_kept_when_the_derivation_does_not_see_it() -> None:
    # The derivation as it stood on 2026-10-24: no edge out of the old ISIN at all.
    merge = merge_with_manual([EARLIER], [AVENUESAI])

    assert merge.derived == (EARLIER,)
    assert merge.conflicts == ()


def test_another_successor_for_the_manual_predecessor_is_a_conflict_not_a_choice() -> None:
    rival = _edge(_OLD, _OTHER)
    merge = merge_with_manual([EARLIER, rival], [AVENUESAI])

    assert rival not in merge.derived
    assert merge.agreeing == ()
    assert [(c.kind, c.derived, c.manual) for c in merge.conflicts] == [
        (LineageConflictKind.SUCCESSOR_DIFFERS, rival, AVENUESAI)
    ]
    assert merge.conflicts[0].isins == (_OLD, _NEW, _OTHER)


def test_the_same_pair_on_another_date_is_a_conflict() -> None:
    late = _edge(_OLD, _NEW, date(2026, 10, 20))
    merge = merge_with_manual([late], [AVENUESAI])

    assert merge.derived == merge.agreeing == ()
    assert [c.kind for c in merge.conflicts] == [LineageConflictKind.EFFECTIVE_DATE_DIFFERS]


def test_a_second_predecessor_into_the_manual_successor_is_a_conflict() -> None:
    intruder = _edge(_OTHER, _NEW)
    merge = merge_with_manual([intruder], [AVENUESAI])

    assert merge.derived == ()
    assert [c.kind for c in merge.conflicts] == [LineageConflictKind.SUCCESSOR_CLAIMED]


def test_a_derived_edge_that_closes_a_loop_through_manual_edges_is_a_conflict() -> None:
    backwards = _edge(_NEW, _OLD, date(2026, 10, 26))
    merge = merge_with_manual([backwards], [AVENUESAI])

    assert merge.derived == ()
    assert [(c.kind, c.manual) for c in merge.conflicts] == [
        (LineageConflictKind.CYCLE, AVENUESAI)
    ]


def test_a_conflict_decided_for_the_manual_edge_drops_the_derived_one() -> None:
    rival = _edge(_OLD, _OTHER)
    decided = {(_SWITCH, (_OLD, _NEW, _OTHER))}
    merge = merge_with_manual([rival], [AVENUESAI], decided=decided)

    assert merge.conflicts == ()
    assert merge.derived == ()
    assert [c.derived for c in merge.overridden] == [rival]


def test_a_decision_on_another_conflict_does_not_settle_this_one() -> None:
    rival = _edge(_OLD, _OTHER)
    decided = {(date(2026, 10, 20), (_OLD, _NEW, _OTHER))}
    merge = merge_with_manual([rival], [AVENUESAI], decided=decided)

    assert merge.overridden == ()
    assert len(merge.conflicts) == 1


def test_the_conflict_error_names_both_edges() -> None:
    conflict = merge_with_manual([_edge(_OLD, _OTHER)], [AVENUESAI]).conflicts
    with pytest.raises(LineageConflictError) as raised:
        raise LineageConflictError(conflict)
    text = str(raised.value)
    assert f"derived {_OLD} -> {_OTHER}" in text
    assert f"MANUAL {_OLD} -> {_NEW}" in text


def test_a_derived_edge_reaches_its_survivor_through_a_manual_edge() -> None:
    # `_OLD` is in no snapshot; only the MANUAL edge says where its chain ends. Without the MANUAL
    # link the chain past `_OLD` ends nowhere and the derived edge into it would be dropped.
    presence = {_OLD: EqPresence(_OLD, date(2017, 9, 1), date(2026, 10, 12), "CCAVENUE")}
    with_manual = plan_registrations([EARLIER], {_NEW}, presence, manual=[AVENUESAI])
    without = plan_registrations([EARLIER], {_NEW}, presence)

    assert with_manual.writable == (EARLIER,)
    assert [s.isin for s in with_manual.register] == [_OLD]
    assert without.writable == ()
