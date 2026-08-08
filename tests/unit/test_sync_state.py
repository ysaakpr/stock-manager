"""M1.3: the §4.4 state machine's rules, with no database anywhere near them.

Everything the sync state machine actually *decides* is a pure function — which transitions exist,
what a failure preserves, what counts as an expected gap, what "green" means — so it is all tested
here, offline and fast. `tests/integration/test_status_sync.py` covers the same rules against
Postgres and the two endpoints that serve them.

The transition test is an exhaustive 7x7 matrix rather than a list of interesting cases: a table
of legal edges that is tested only where someone thought to look is a table with a hole in it, and
the hole is always the edge that lets a `PUBLISHED` date be re-fetched or a real miss be filed as
a holiday.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise

import pytest

from dataplatform.clock import IST
from dataplatform.ingest.calendar import CalendarCoverageError, DayKind, trading_calendar
from dataplatform.status.sync_state import (
    CLOSED_STATES,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    IllegalTransitionError,
    NotAGapError,
    SyncRecord,
    SyncState,
    evaluate_green,
    expected_gap_kind,
)

SOURCE = "nse_bhavcopy_udiff"

#: A real NSE session (Friday), so the calendar-backed assertions are about the actual exchange.
SESSION_DATE = date(2026, 8, 7)
WEEKEND_DATE = date(2026, 8, 8)  # Saturday
HOLIDAY_DATE = date(2026, 1, 26)  # Republic Day, a Monday
MUHURAT_DATE = date(2025, 10, 21)  # Diwali Laxmi Pujan — closed, but a bhavcopy exists
UNCOVERED_DATE = date(2011, 6, 1)  # before the holiday file's coverage begins

AT = datetime(2026, 8, 7, 18, 30, tzinfo=IST)
LATER = AT + timedelta(hours=1)


def record(state: SyncState, **overrides: object) -> SyncRecord:
    """A record parked in `state`, for testing one transition out of it."""
    fields: dict[str, object] = {
        "source": SOURCE,
        "logical_date": SESSION_DATE,
        "state": state,
        "updated_at": AT,
    }
    fields.update(overrides)
    return SyncRecord(**fields)  # type: ignore[arg-type]


def transition_kwargs(to_state: SyncState) -> dict[str, object]:
    """The extra arguments a transition to `to_state` requires — only FAILED needs any."""
    return {"error": "boom"} if to_state is SyncState.FAILED else {}


# ── acceptance 1: every legal transition works, every illegal one raises ─────────────────────


def test_the_transition_table_covers_every_state() -> None:
    """A state missing from the table would raise `KeyError`, not a readable refusal."""
    assert set(LEGAL_TRANSITIONS) == set(SyncState)
    assert set(TERMINAL_STATES) == {SyncState.PUBLISHED, SyncState.GAP}
    assert set(CLOSED_STATES) == {SyncState.PUBLISHED}
    assert all(state.is_terminal for state in TERMINAL_STATES)


def test_the_happy_path_is_the_one_in_the_plan() -> None:
    """§4.4 verbatim: PENDING -> FETCHED -> VALIDATED -> NORMALIZED -> PUBLISHED."""
    happy = [
        SyncState.PENDING,
        SyncState.FETCHED,
        SyncState.VALIDATED,
        SyncState.NORMALIZED,
        SyncState.PUBLISHED,
    ]
    for current, following in pairwise(happy):
        assert following in LEGAL_TRANSITIONS[current], f"{current} -> {following}"


@pytest.mark.parametrize("to_state", list(SyncState))
@pytest.mark.parametrize("from_state", list(SyncState))
def test_every_pair_of_states_matches_the_table(from_state: SyncState, to_state: SyncState) -> None:
    """The whole matrix: legal edges apply, illegal ones raise and name both states."""
    start = record(from_state)
    if to_state in LEGAL_TRANSITIONS[from_state]:
        moved = start.transition(to_state, at=LATER, **transition_kwargs(to_state))  # type: ignore[arg-type]
        assert moved.state is to_state
        assert moved.updated_at == LATER
        return

    assert not start.can_transition_to(to_state)
    with pytest.raises(IllegalTransitionError) as caught:
        start.transition(to_state, at=LATER, **transition_kwargs(to_state))  # type: ignore[arg-type]
    message = str(caught.value)
    assert from_state.value in message, message
    assert to_state.value in message, message
    assert SOURCE in message and SESSION_DATE.isoformat() in message, message
    assert caught.value.from_state is from_state
    assert caught.value.to_state is to_state


def test_published_is_closed_and_says_so() -> None:
    """`PUBLISHED -> FETCHED` must not read like a typo in the caller — it is a hard rule.

    L0 is immutable (invariant #1), so a published date can never yield different bytes; re-driving
    one would be the backfill runner re-fetching what it already has.
    """
    assert LEGAL_TRANSITIONS[SyncState.PUBLISHED] == frozenset()
    with pytest.raises(IllegalTransitionError, match="terminal and nothing leaves it"):
        record(SyncState.PUBLISHED).transition(SyncState.FETCHED, at=LATER)


def test_gap_is_closed_to_the_pipeline_but_re_openable_by_a_calendar_correction() -> None:
    """C.2's `Reconciliation.unexpected` is real: a wrong holiday entry must be recoverable.

    A strictly terminal GAP would strand that date forever, which is why the one edge exists —
    and why nothing else may leave GAP, so it cannot be used to skip the pipeline.
    """
    gap = record(SyncState.GAP)
    with pytest.raises(IllegalTransitionError) as caught:
        gap.transition(SyncState.FETCHED, at=LATER)
    assert "GAP" in str(caught.value) and "FETCHED" in str(caught.value)
    assert "legal from GAP: PENDING" in str(caught.value)

    reopened = gap.transition(SyncState.PENDING, at=LATER)
    assert reopened.state is SyncState.PENDING
    assert reopened.attempts == 1, "re-opening a gap starts a genuine first attempt"


def test_a_non_retryable_failure_refuses_the_retry_and_names_both_states() -> None:
    """`retryable` has to mean something, or a source telling us to stop gets hammered forever."""
    failed = record(SyncState.FAILED, retryable=False, last_error="404 for a date never published")
    assert not failed.can_transition_to(SyncState.PENDING)
    with pytest.raises(IllegalTransitionError) as caught:
        failed.transition(SyncState.PENDING, at=LATER)
    message = str(caught.value)
    assert "FAILED" in message and "PENDING" in message
    assert "non-retryable" in message
    assert "404 for a date never published" in message


def test_a_retryable_failure_goes_back_to_pending() -> None:
    failed = record(SyncState.FAILED, retryable=True, last_error="timeout", attempts=1)
    retried = failed.transition(SyncState.PENDING, at=LATER)
    assert retried.state is SyncState.PENDING
    assert retried.retryable is True


# ── acceptance 1, second half: attempts and last_error are preserved ─────────────────────────


def test_attempts_count_every_attempt_started_including_the_one_that_failed() -> None:
    """A fetch that broke on the first try must read `attempts == 1`, never 0."""
    first = record(SyncState.PENDING, attempts=1, first_attempt_at=AT)
    failed = first.transition(SyncState.FAILED, at=LATER, error="502 from nsearchives")
    assert failed.attempts == 1

    second = failed.transition(SyncState.PENDING, at=LATER)
    assert second.attempts == 2
    assert second.first_attempt_at == AT, "the first attempt's timestamp is never overwritten"


def test_last_error_survives_the_retry_and_the_eventual_success() -> None:
    """A date that took three goes to land is worth knowing about after it finally publishes."""
    row = record(SyncState.PENDING, attempts=1, first_attempt_at=AT)
    row = row.transition(SyncState.FAILED, at=LATER, error="connection reset")
    row = row.transition(SyncState.PENDING, at=LATER)
    assert row.last_error == "connection reset"

    row = row.transition(SyncState.FETCHED, at=LATER, checksum="abc123", l0_path="L0/x.zip")
    row = row.transition(SyncState.VALIDATED, at=LATER)
    row = row.transition(SyncState.NORMALIZED, at=LATER)
    row = row.transition(SyncState.PUBLISHED, at=LATER)

    assert row.state is SyncState.PUBLISHED
    assert row.attempts == 2
    assert row.last_error == "connection reset"
    assert row.checksum == "abc123", "the L0 checksum survives to the published row"
    assert row.l0_path == "L0/x.zip"


def test_a_later_failure_replaces_the_recorded_error() -> None:
    row = record(SyncState.PENDING, attempts=1)
    row = row.transition(SyncState.FAILED, at=LATER, error="first")
    row = row.transition(SyncState.PENDING, at=LATER)
    row = row.transition(SyncState.FAILED, at=LATER, error="second")
    assert row.last_error == "second"


def test_a_failure_must_say_why() -> None:
    """`fail loud and specific` (CLAUDE.md): a FAILED row with no reason is barely a log line."""
    row = record(SyncState.PENDING)
    for empty in (None, "", "   "):
        with pytest.raises(ValueError, match="needs a specific last_error"):
            row.transition(SyncState.FAILED, at=LATER, error=empty)


@pytest.mark.parametrize("to_state", [SyncState.FETCHED, SyncState.PENDING])
def test_failure_arguments_are_refused_on_a_non_failure(to_state: SyncState) -> None:
    """`mark_published(retryable=False)` would be nonsense; it is rejected rather than ignored."""
    row = record(SyncState.PENDING)
    with pytest.raises(ValueError, match="may only be passed with FAILED"):
        row.transition(to_state, at=LATER, error="why would this be here")
    with pytest.raises(ValueError, match="may only be passed with FAILED"):
        row.transition(to_state, at=LATER, retryable=False)


def test_a_non_failure_transition_clears_a_stale_non_retryable_flag() -> None:
    """Otherwise a `false` left over from an old failure would silently outlive its failure."""
    row = record(SyncState.FAILED, retryable=True, last_error="x").transition(
        SyncState.PENDING, at=LATER
    )
    assert row.retryable is True


# ── acceptance 2: GAP(expected) comes from the C.2 calendar, never from a real miss ──────────


def test_a_weekend_is_an_expected_gap() -> None:
    assert expected_gap_kind(WEEKEND_DATE, calendar=trading_calendar()) is DayKind.WEEKEND


def test_a_declared_holiday_is_an_expected_gap() -> None:
    assert expected_gap_kind(HOLIDAY_DATE, calendar=trading_calendar()) is DayKind.HOLIDAY


def test_a_trading_session_is_never_a_gap() -> None:
    """The criterion itself: a missing file on a session is a failure, not a holiday."""
    with pytest.raises(NotAGapError, match="real miss"):
        expected_gap_kind(SESSION_DATE, calendar=trading_calendar())


def test_a_muhurat_session_is_never_a_gap() -> None:
    """Muhurat is a declared holiday that still publishes a bhavcopy — the subtle real miss."""
    calendar = trading_calendar()
    assert calendar.classify(MUHURAT_DATE) is DayKind.MUHURAT
    with pytest.raises(NotAGapError):
        expected_gap_kind(MUHURAT_DATE, calendar=calendar)


def test_a_date_outside_the_calendar_is_not_quietly_a_gap() -> None:
    """ "Probably not a trading day" is exactly how a real miss gets filed as a holiday."""
    with pytest.raises(CalendarCoverageError):
        expected_gap_kind(UNCOVERED_DATE, calendar=trading_calendar())


# ── invariant #10: is_green's rule, decided without a database ───────────────────────────────


def published(source: str, logical_date: date = SESSION_DATE) -> SyncRecord:
    return SyncRecord(
        source=source, logical_date=logical_date, state=SyncState.PUBLISHED, updated_at=AT
    )


def in_state(source: str, state: SyncState) -> SyncRecord:
    return SyncRecord(source=source, logical_date=SESSION_DATE, state=state, updated_at=AT)


def test_green_when_every_dataset_is_published_and_nothing_is_flagged() -> None:
    status = evaluate_green(
        SESSION_DATE,
        ["a", "b"],
        {"a": published("a"), "b": published("b")},
        day_kind=DayKind.SESSION,
    )
    assert status.green is True
    assert bool(status) is True
    assert status.published == ("a", "b")


def test_not_green_when_a_dataset_has_no_row_at_all() -> None:
    status = evaluate_green(
        SESSION_DATE, ["a", "b"], {"a": published("a")}, day_kind=DayKind.SESSION
    )
    assert not status
    assert status.missing == ("b",)
    assert "no sync_state row for b" in status.reason


@pytest.mark.parametrize(
    "state",
    [SyncState.PENDING, SyncState.FETCHED, SyncState.VALIDATED, SyncState.NORMALIZED],
)
def test_not_green_while_a_dataset_is_still_in_flight(state: SyncState) -> None:
    """PUBLISHED is the only acceptable state — "nearly normalized" is still not readable."""
    status = evaluate_green(
        SESSION_DATE, ["a"], {"a": in_state("a", state)}, day_kind=DayKind.SESSION
    )
    assert not status
    assert status.not_published == (("a", state),)
    assert state.value in status.reason


def test_not_green_when_a_dataset_failed() -> None:
    status = evaluate_green(
        SESSION_DATE, ["a"], {"a": in_state("a", SyncState.FAILED)}, day_kind=DayKind.SESSION
    )
    assert not status


def test_not_green_when_an_error_quality_flag_is_open() -> None:
    """§4.4 asks for PUBLISHED *and* quality-green; published-but-wrong data is still red."""
    status = evaluate_green(
        SESSION_DATE,
        ["a"],
        {"a": published("a")},
        day_kind=DayKind.SESSION,
        open_error_flags=2,
    )
    assert not status
    assert "2 open ERROR-severity quality flag" in status.reason


def test_not_green_on_a_day_the_exchange_was_shut() -> None:
    status = evaluate_green(WEEKEND_DATE, ["a"], {}, day_kind=DayKind.WEEKEND)
    assert not status
    assert "WEEKEND" in status.reason


def test_not_green_outside_the_calendar_coverage() -> None:
    """An unknown date fails closed: whether data was owed for it cannot be established."""
    status = evaluate_green(UNCOVERED_DATE, ["a"], {"a": published("a")}, day_kind=None)
    assert not status
    assert "outside the trading calendar's coverage" in status.reason


def test_green_over_no_datasets_is_a_caller_error_not_a_vacuous_yes() -> None:
    """`all([]) is True` is how an interlock silently stops interlocking."""
    with pytest.raises(ValueError, match="vacuous"):
        evaluate_green(SESSION_DATE, [], {}, day_kind=DayKind.SESSION)


def test_duplicate_datasets_are_asked_about_once() -> None:
    status = evaluate_green(
        SESSION_DATE, ["a", "a"], {"a": published("a")}, day_kind=DayKind.SESSION
    )
    assert status.datasets == ("a",)
    assert status.green is True
