"""DQ-5 — membership history: the backward walk, the PIT rule, and `pit_universe` over it.

Three properties, each written so the plausible wrong implementation fails:

1. **The walk reconstructs the past from the anchor.** Crossing an inclusion backward removes the
   company, crossing an exclusion restores it, crossing a reissue renames it to the ISIN that
   traded then — and a step that does not reconcile is a `Residual`, never silently absorbed.
2. **The PIT rule: effective on D *and* knowable by D.** A change announced after it took effect
   is invisible until its announcement; a change effective after the anchor appears on its date
   and not before; a date before `coverage_start` is `None`, never the oldest reconstructed set.
3. **`pit_universe(index_slugs=…)` reads the history for past dates.** Today's snapshot names a
   company that joined last year; a universe for two years ago must not contain it, and must
   contain the company it replaced — the survivorship leak this task exists to close.

Pure and offline: histories are built from hand-set anchors and events, written to `tmp_path`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pytest

from dataplatform.identity.master import ListingStatus
from dataplatform.ingest.index_changes import ChangeAction, IndexChangeEvent
from dataplatform.ingest.index_history import (
    HistoryBuild,
    ImmutableHistoryError,
    IndexHistory,
    ResidualKind,
    ResolvedEvent,
    SymbolEvidence,
    _apply_revocations,
    _apply_voidings,
    _supersede,
    members_asof,
    read_membership_history,
    reconstruct_index,
    write_membership_history,
)
from dataplatform.ingest.indices import ConstituentRow, ConstituentSnapshot, write_constituents_l1
from dataplatform.query.universe import (
    InMemoryListingCalendar,
    ListingWindow,
    index_membership_asof,
    pit_universe,
)

A: Final = "INE002A01018"
B: Final = "INE237A01028"
C: Final = "INE423A01024"  # joins the index on 2024-06-28
D: Final = "INE040A01034"  # leaves the index on 2024-06-28
OLD: Final = "INE467B01029"  # pre-reissue ISIN of the same company as NEW
NEW: Final = "INE467B01037"

ANCHOR: Final = date(2024, 12, 31)
COVERAGE: Final = date(2023, 1, 2)


def _anchor(
    members: tuple[str, ...], *, as_of: date = ANCHOR, slug: str = "nifty50"
) -> ConstituentSnapshot:
    return ConstituentSnapshot(
        index_slug=slug,
        index_name=slug,
        as_of=as_of,
        rows=tuple(
            ConstituentRow(
                isin=i, symbol=f"S{n}", series="EQ", company_name=f"Co {n}", industry="X"
            )
            for n, i in enumerate(sorted(members))
        ),
        source="nifty_index_constituents",
    )


def _event(
    isin: str,
    action: ChangeAction,
    effective: date,
    announced: date,
    *,
    slug: str = "nifty50",
    release: str = "ind_prs01012024.pdf",
) -> ResolvedEvent:
    return ResolvedEvent(
        IndexChangeEvent(
            index_slug=slug,
            action=action,
            company_name=f"Co {isin}",
            symbol=f"SYM{isin[-4:]}",
            effective=effective,
            announced=announced,
            release=release,
        ),
        isin,
    )


def _history(**kwargs: object) -> IndexHistory:
    params: dict[str, object] = {
        "anchor": _anchor((A, B, C)),
        "anchor_l0_key": "nifty_index_constituents/2024-12-31/x.csv",
        "events": (
            _event(C, ChangeAction.INCLUDE, date(2024, 6, 28), date(2024, 5, 20)),
            _event(D, ChangeAction.EXCLUDE, date(2024, 6, 28), date(2024, 5, 20)),
        ),
        "coverage_start": COVERAGE,
        "expected_size": 3,
    }
    params.update(kwargs)
    return reconstruct_index("nifty50", **params)  # type: ignore[arg-type]


# ── 1. the walk ────────────────────────────────────────────────────────────────────────────────


def test_crossing_a_change_backward_restores_the_earlier_membership() -> None:
    history = _history()
    assert history.members_on(date(2024, 6, 27)) == frozenset({A, B, D})
    assert history.members_on(date(2024, 6, 28)) == frozenset({A, B, C})
    assert history.members_on(ANCHOR) == frozenset({A, B, C})
    assert [r for r in history.residuals if r.kind is ResidualKind.COUNT] == []


def test_a_change_before_its_effective_date_is_not_applied_early() -> None:
    """Inverting the effective-date comparison would hand D's seat to C a day early."""
    history = _history()
    on_eve = history.members_on(date(2024, 6, 27))
    assert on_eve is not None
    assert C not in on_eve and D in on_eve


def test_a_reissue_renames_the_member_to_the_isin_that_traded_then() -> None:
    history = _history(
        anchor=_anchor((A, B, NEW)),
        events=(),
        reissues=((OLD, NEW, date(2024, 3, 1)),),
    )
    assert history.members_on(date(2024, 2, 29)) == frozenset({A, B, OLD})
    assert history.members_on(date(2024, 3, 1)) == frozenset({A, B, NEW})


def test_an_inclusion_the_later_set_does_not_hold_is_a_residual() -> None:
    history = _history(
        events=(_event(D, ChangeAction.INCLUDE, date(2024, 6, 28), date(2024, 5, 20)),)
    )
    kinds = {r.kind for r in history.residuals}
    assert ResidualKind.INCLUDE_NOT_HELD_LATER in kinds


def test_a_count_off_the_index_size_is_a_residual() -> None:
    history = _history(
        events=(_event(C, ChangeAction.INCLUDE, date(2024, 6, 28), date(2024, 5, 20)),)
    )
    counts = [r for r in history.residuals if r.kind is ResidualKind.COUNT]
    assert counts and counts[0].detail == "2 members, expected 3"


def test_a_member_is_clipped_to_its_first_appearance() -> None:
    """A spin-off walked back past its own listing is clipped, and the clip is reported."""
    listed = date(2024, 8, 21)
    evidence = SymbolEvidence((), first_sessions={A: COVERAGE, B: COVERAGE, C: listed})
    history = _history(events=(), evidence=evidence)
    assert C not in (history.members_on(date(2024, 8, 20)) or frozenset())
    assert C in (history.members_on(listed) or frozenset())
    assert ResidualKind.ENTRY_BEFORE_FIRST_SESSION in {r.kind for r in history.residuals}


def test_a_reconstruction_that_disagrees_with_a_stored_snapshot_is_reported() -> None:
    history = _history(snapshots={date(2024, 9, 1): frozenset({A, B, D})})
    assert ResidualKind.SNAPSHOT_MISMATCH in {r.kind for r in history.residuals}
    clean = _history(snapshots={date(2024, 9, 1): frozenset({A, B, C})})
    assert ResidualKind.SNAPSHOT_MISMATCH not in {r.kind for r in clean.residuals}


# ── 2. the PIT rule ────────────────────────────────────────────────────────────────────────────


def test_a_change_announced_after_it_took_effect_is_invisible_until_announced() -> None:
    late = (
        _event(C, ChangeAction.INCLUDE, date(2024, 6, 28), date(2024, 7, 2)),
        _event(D, ChangeAction.EXCLUDE, date(2024, 6, 28), date(2024, 7, 2)),
    )
    history = _history(events=late)
    assert C not in (history.members_on(date(2024, 7, 1)) or frozenset())
    assert C in (history.members_on(date(2024, 7, 2)) or frozenset())


def test_a_change_effective_after_the_anchor_appears_on_its_date_only() -> None:
    future = (
        _event(D, ChangeAction.INCLUDE, date(2025, 3, 28), date(2024, 12, 20)),
        _event(A, ChangeAction.EXCLUDE, date(2025, 3, 28), date(2024, 12, 20)),
    )
    history = _history(events=future)
    assert history.members_on(date(2025, 3, 27)) == frozenset({A, B, C})
    assert history.members_on(date(2025, 3, 28)) == frozenset({B, C, D})


def test_before_coverage_start_the_history_answers_nothing() -> None:
    history = _history()
    assert history.members_on(date(2023, 1, 1)) is None
    assert history.members_on(COVERAGE) == frozenset({A, B, D})


# ── revocations, voidings, postponements ───────────────────────────────────────────────────────


def _raw(
    symbol: str, action: ChangeAction, effective: date, announced: date, release: str
) -> IndexChangeEvent:
    return IndexChangeEvent(
        index_slug="nifty500",
        action=action,
        company_name=symbol,
        symbol=symbol,
        effective=effective,
        announced=announced,
        release=release,
    )


def test_a_revocation_withdraws_the_announced_change() -> None:
    planned = _raw("IREDA", ChangeAction.INCLUDE, date(2024, 3, 28), date(2024, 2, 28), "a.pdf")
    revoked = _raw(
        "IREDA", ChangeAction.REVOKE_INCLUDE, date(2024, 3, 28), date(2024, 3, 19), "b.pdf"
    )
    assert _apply_revocations([planned, revoked]) == []


def test_a_revocation_cannot_withdraw_a_change_already_in_effect() -> None:
    done = _raw("IDEA", ChangeAction.EXCLUDE, date(2024, 3, 1), date(2024, 2, 1), "a.pdf")
    late = _raw("IDEA", ChangeAction.REVOKE_EXCLUDE, date(2024, 9, 30), date(2024, 9, 25), "b.pdf")
    assert _apply_revocations([done, late]) == [done]


def test_a_postponed_change_keeps_only_its_final_date() -> None:
    first = _raw("JIOFIN", ChangeAction.EXCLUDE, date(2023, 8, 31), date(2023, 8, 25), "a.pdf")
    final = _raw("JIOFIN", ChangeAction.EXCLUDE, date(2023, 9, 7), date(2023, 8, 30), "b.pdf")
    assert _supersede([first, final]) == [final]
    years_apart = _raw(
        "JIOFIN", ChangeAction.EXCLUDE, date(2025, 9, 30), date(2025, 8, 22), "c.pdf"
    )
    assert len(_supersede([first, years_apart])) == 2


def test_the_march_2020_review_is_void_except_nifty_50() -> None:
    voided = _raw(
        "ALKYLAMINE",
        ChangeAction.INCLUDE,
        date(2020, 3, 27),
        date(2020, 3, 19),
        "ind_prs19032020.pdf",
    )
    kept = voided.model_copy(update={"index_slug": "nifty50"})
    untouched = voided.model_copy(update={"effective": date(2020, 3, 19)})
    assert _apply_voidings([voided, kept, untouched], ["ind_prs13052020.pdf"]) == [kept, untouched]
    # Only once the voiding release itself is in L0.
    assert _apply_voidings([voided], []) == [voided]


# ── L1: write once, read back ──────────────────────────────────────────────────────────────────


def _build(history: IndexHistory, as_of: date = ANCHOR) -> HistoryBuild:
    return HistoryBuild(
        as_of=as_of,
        listing_l0_key="nifty_index_press_releases/x/listing.html",
        releases_considered=1,
        releases_in_l0=1,
        releases_missing=(),
        histories={"nifty50": history},
        events=(),
    )


def test_a_history_round_trips_through_l1(tmp_path: Path) -> None:
    history = _history()
    write_membership_history(_build(history), data_root=tmp_path)
    back = read_membership_history("nifty50", data_root=tmp_path)
    assert back is not None
    assert back.coverage_start == COVERAGE
    assert back.members_on(date(2024, 6, 27)) == frozenset({A, B, D})
    found = members_asof("nifty50", date(2024, 6, 28), data_root=tmp_path)
    assert found is not None and found[0] == frozenset({A, B, C})
    assert members_asof("nifty50", date(2022, 12, 30), data_root=tmp_path) is None
    assert members_asof("nifty100", date(2024, 6, 28), data_root=tmp_path) is None


def test_a_stored_build_is_never_rewritten(tmp_path: Path) -> None:
    write_membership_history(_build(_history()), data_root=tmp_path)
    write_membership_history(_build(_history()), data_root=tmp_path)  # identical: a no-op
    with pytest.raises(ImmutableHistoryError):
        write_membership_history(_build(_history(events=())), data_root=tmp_path)


# ── 3. pit_universe reads the history, and nothing from the future leaks into the past ─────────


def _lake(tmp_path: Path) -> Path:
    """History anchored 2024-12-31 (C replaced D on 2024-06-28) + today's snapshot (A, B, C)."""
    write_membership_history(_build(_history()), data_root=tmp_path)
    write_constituents_l1(_anchor((A, B, C), as_of=date(2024, 12, 31)), data_root=tmp_path)
    return tmp_path


def _calendar() -> InMemoryListingCalendar:
    return InMemoryListingCalendar(
        tuple(
            ListingWindow(
                isin=i, listed_from=date(2015, 1, 1), delisted_on=None, status=ListingStatus.ACTIVE
            )
            for i in (A, B, C, D)
        )
    )


def test_a_past_universe_holds_the_members_of_then_not_of_today(tmp_path: Path) -> None:
    lake = _lake(tmp_path)
    then = date(2024, 3, 15)
    membership = index_membership_asof(["nifty50"], then, data_root=lake)
    universe = pit_universe(then, _calendar(), index_membership=membership, index_slugs=["nifty50"])
    assert C not in universe, "a company that joined on 2024-06-28 leaked into 2024-03-15"
    assert D in universe, "the company C replaced must be in the 2024-03-15 universe"
    assert universe.isins == frozenset({A, B, D})


def test_the_universe_switches_on_the_effective_date(tmp_path: Path) -> None:
    lake = _lake(tmp_path)
    assert index_membership_asof(["nifty50"], date(2024, 6, 27), data_root=lake) == frozenset(
        {A, B, D}
    )
    assert index_membership_asof(["nifty50"], date(2024, 6, 28), data_root=lake) == frozenset(
        {A, B, C}
    )


def test_before_coverage_the_index_contributes_nothing_not_todays_list(tmp_path: Path) -> None:
    lake = _lake(tmp_path)
    assert index_membership_asof(["nifty50"], date(2022, 6, 1), data_root=lake) == frozenset()


def test_a_snapshot_newer_than_the_anchor_wins(tmp_path: Path) -> None:
    lake = _lake(tmp_path)
    write_constituents_l1(_anchor((A, B, D), as_of=date(2025, 2, 1)), data_root=lake)
    assert index_membership_asof(["nifty50"], date(2025, 2, 3), data_root=lake) == frozenset(
        {A, B, D}
    )
    # …but not for a date before that snapshot.
    assert index_membership_asof(["nifty50"], date(2025, 1, 15), data_root=lake) == frozenset(
        {A, B, C}
    )


def test_query_service_pit_universe_uses_the_history(tmp_path: Path) -> None:
    from dataplatform.query.service import QueryService

    lake = _lake(tmp_path)
    with QueryService(data_root=lake) as svc:
        universe = svc.pit_universe(date(2024, 3, 15), _calendar(), index_slugs=["nifty50"])
    assert universe.isins == frozenset({A, B, D})
