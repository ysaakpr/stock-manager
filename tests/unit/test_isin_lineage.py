"""D2 ISIN lineage: the derivation and the resolver, offline.

The derivation is a pure function of L1 spans, so every case here is stated as spans rather than
as a lake. What each test pins down is one way the derivation could be wrong in a manner that
would look right in aggregate — a spliced pair of concurrent share classes, a dividend promoted to
an explanation, a chain resolved only one hop.
"""

from __future__ import annotations

from datetime import date

import pytest

from dataplatform.corpactions.taxonomy import ActionType
from dataplatform.identity.lineage import (
    IsinSpan,
    LineageEdge,
    LineageResolver,
    corroborating_type,
    derive_edges,
)

# A session calendar dense enough to count gaps in: every weekday of October 2021.
_SESSIONS = tuple(d for d in (date(2021, 10, day) for day in range(1, 32)) if d.weekday() < 5)

# IRCTC's real reissue, the case the golden suite gets wrong.
_OLD = "INE335Y01012"
_NEW = "INE335Y01020"


def _span(isin: str, first: date, last: date, symbol: str = "IRCTC") -> IsinSpan:
    return IsinSpan(isin=isin, first_date=first, last_date=last, first_symbol=symbol)


def test_a_sequential_reissue_within_an_issuer_is_one_edge() -> None:
    edges = derive_edges(
        [
            _span(_OLD, date(2021, 10, 1), date(2021, 10, 28)),
            _span(_NEW, date(2021, 10, 29), date(2021, 10, 29)),
        ],
        _SESSIONS,
        {},
    )
    assert len(edges) == 1
    assert edges[0].predecessor_isin == _OLD
    assert edges[0].successor_isin == _NEW
    assert edges[0].effective_date == date(2021, 10, 29)
    assert edges[0].gap_sessions == 0
    assert edges[0].confidence == "DERIVED"


def test_concurrent_spans_are_not_a_reissue() -> None:
    """Two live share classes under one issuer overlap in time; splicing them would be a fiction."""
    edges = derive_edges(
        [
            _span(_OLD, date(2021, 10, 1), date(2021, 10, 29)),
            _span(_NEW, date(2021, 10, 15), date(2021, 10, 29)),
        ],
        _SESSIONS,
        {},
    )
    assert edges == ()


def test_different_issuers_never_join() -> None:
    edges = derive_edges(
        [
            _span("INE335Y01012", date(2021, 10, 1), date(2021, 10, 28)),
            _span("INE999Z01011", date(2021, 10, 29), date(2021, 10, 29)),
        ],
        _SESSIONS,
        {},
    )
    assert edges == ()


def test_corroboration_is_matched_on_the_retired_isin() -> None:
    """The exchange files the split against the ISIN it is retiring — that is the whole bug."""
    edges = derive_edges(
        [
            _span(_OLD, date(2021, 10, 1), date(2021, 10, 28)),
            _span(_NEW, date(2021, 10, 29), date(2021, 10, 29)),
        ],
        _SESSIONS,
        {(_OLD, date(2021, 10, 29)): ActionType.SPLIT},
    )
    assert edges[0].corroborating_action is ActionType.SPLIT
    assert edges[0].confidence == "CORROBORATED"


def test_an_action_dated_to_the_predecessors_last_session_still_corroborates() -> None:
    """NSE dates IRCTC's split 28-Oct while the new ISIN goes live 29-Oct.

    Both label one event. Matching only the successor's first session left 115 of 445 real
    reissues DERIVED — the canonical IRCTC case among them.
    """
    edges = derive_edges(
        [
            _span(_OLD, date(2021, 10, 1), date(2021, 10, 28)),
            _span(_NEW, date(2021, 10, 29), date(2021, 10, 29)),
        ],
        _SESSIONS,
        {(_OLD, date(2021, 10, 28)): ActionType.SPLIT},
    )
    assert edges[0].corroborating_action is ActionType.SPLIT


def test_an_action_two_sessions_off_the_boundary_does_not_corroborate() -> None:
    """The window is the boundary pair and no wider; beyond it, unrelated actions creep in."""
    edges = derive_edges(
        [
            _span(_OLD, date(2021, 10, 1), date(2021, 10, 28)),
            _span(_NEW, date(2021, 10, 29), date(2021, 10, 29)),
        ],
        _SESSIONS,
        {(_OLD, date(2021, 10, 27)): ActionType.SPLIT},
    )
    assert edges[0].confidence == "DERIVED"


def test_an_action_filed_against_the_survivor_does_not_corroborate() -> None:
    edges = derive_edges(
        [
            _span(_OLD, date(2021, 10, 1), date(2021, 10, 28)),
            _span(_NEW, date(2021, 10, 29), date(2021, 10, 29)),
        ],
        _SESSIONS,
        {(_NEW, date(2021, 10, 29)): ActionType.SPLIT},
    )
    assert edges[0].confidence == "DERIVED"


def test_gap_is_counted_in_trading_sessions_not_calendar_days() -> None:
    """A Friday-to-Monday handover is seamless; the weekend is not two missed sessions."""
    edges = derive_edges(
        [
            _span(_OLD, date(2021, 10, 1), date(2021, 10, 8)),  # Friday
            _span(_NEW, date(2021, 10, 11), date(2021, 10, 11)),  # Monday
        ],
        _SESSIONS,
        {},
    )
    assert edges[0].gap_sessions == 0


def test_a_real_suspension_is_counted_and_kept() -> None:
    edges = derive_edges(
        [
            _span(_OLD, date(2021, 10, 1), date(2021, 10, 8)),
            _span(_NEW, date(2021, 10, 20), date(2021, 10, 20)),
        ],
        _SESSIONS,
        {},
    )
    assert edges[0].gap_sessions == 7  # 11,12,13,14,15,18,19
    assert edges  # kept, not thresholded away


def _resolver(*pairs: tuple[str, str]) -> LineageResolver:
    return LineageResolver({p: (s, date(2021, 10, 29)) for p, s in pairs})


def test_survivor_walks_the_whole_chain() -> None:
    resolver = _resolver(("INE000A01011", "INE000A01029"), ("INE000A01029", "INE000A01037"))
    assert resolver.survivor_of("INE000A01011") == "INE000A01037"


def test_survivor_of_an_isin_never_reissued_is_itself() -> None:
    assert _resolver().survivor_of(_NEW) == _NEW


def test_chain_to_collects_the_inherited_history_oldest_first() -> None:
    resolver = _resolver(("INE000A01011", "INE000A01029"), ("INE000A01029", "INE000A01037"))
    assert resolver.chain_to("INE000A01037") == (
        "INE000A01011",
        "INE000A01029",
        "INE000A01037",
    )


def test_a_cycle_is_broken_rather_than_looped_forever() -> None:
    """A contradictory derivation must not hang the factor chain."""
    resolver = _resolver(("INE000A01011", "INE000A01029"), ("INE000A01029", "INE000A01011"))
    assert resolver.survivor_of("INE000A01011") in {"INE000A01011", "INE000A01029"}


def test_edges_are_ordered_by_effective_date() -> None:
    edges = derive_edges(
        [
            _span("INE111A01011", date(2021, 10, 1), date(2021, 10, 20)),
            _span("INE111A01029", date(2021, 10, 21), date(2021, 10, 29)),
            _span("INE222A01010", date(2021, 10, 1), date(2021, 10, 5)),
            _span("INE222A01028", date(2021, 10, 6), date(2021, 10, 29)),
        ],
        _SESSIONS,
        {},
    )
    assert [e.effective_date for e in edges] == [date(2021, 10, 6), date(2021, 10, 21)]


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Bonus 1:1", ActionType.BONUS),
        (
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
            ActionType.SPLIT,
        ),
        # A compound line names two events; the split is what reissued the ISIN.
        (
            "Bonus 1:5/Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per "
            "Share",
            ActionType.SPLIT,
        ),
        ("Bonus 2:1/Dividend- Rs 1.60 Per Share", ActionType.BONUS),
        ("Annual General Meeting/Dividend - Rs 10 Per Share", None),
        # One segment naming two types is ambiguous, not evidence.
        ("BONUS 1:1 AND FV SPLIT FROM RS.10/- TO RS.2/-", None),
        ("", None),
    ],
)
def test_corroborating_type_reads_compound_lines_one_event_at_a_time(
    subject: str, expected: ActionType | None
) -> None:
    assert corroborating_type(subject) is expected


@pytest.mark.parametrize("action", [ActionType.SPLIT, ActionType.BONUS])
def test_both_corroborating_types_are_accepted(action: ActionType) -> None:
    edges = derive_edges(
        [
            _span(_OLD, date(2021, 10, 1), date(2021, 10, 28)),
            _span(_NEW, date(2021, 10, 29), date(2021, 10, 29)),
        ],
        _SESSIONS,
        {(_OLD, date(2021, 10, 29)): action},
    )
    assert edges[0].corroborating_action is action


def test_confidence_tracks_the_action_on_the_edge_itself() -> None:
    bare = LineageEdge(_OLD, _NEW, date(2021, 10, 29), 0, "IRCTC", None)
    explained = LineageEdge(_OLD, _NEW, date(2021, 10, 29), 0, "IRCTC", ActionType.SPLIT)
    assert bare.confidence == "DERIVED"
    assert explained.confidence == "CORROBORATED"
