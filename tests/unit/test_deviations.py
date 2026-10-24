"""The accepted-deviation registry: it loads, refuses a malformed entry, and matches narrowly.

Every matcher is proved both ways — the known cause is ACCEPTED, and the same name with any other
failure is not — so widening a matcher to the target's name alone, or inverting it, fails here.
The judge reads its evidence through a fake store and connection: what is under test is the
decision, not the SQL (`tests/integration/test_deviations.py` runs the reads against Postgres).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from dataplatform.clock import IST
from dataplatform.status.deviations import (
    DEVIATIONS_PATH,
    DeviationJudge,
    DeviationKind,
    DeviationRegistry,
    DeviationRegistryError,
    DeviationStatus,
    ErrorPattern,
    GapItem,
    default_deviations,
    error_class,
    load_deviations,
)
from dataplatform.status.sync_state import SourceStatus, SyncRecord, SyncState, SyncStateStore
from dataplatform.store.db import Connection

TODAY = date(2026, 10, 23)
RUN = datetime(2026, 10, 17, 10, 0, tzinfo=IST)

# Failure texts as `sync_state.last_error` holds them (copied from the live rows' shapes).
BSE_403 = (
    "ForbiddenError: {source}: https://api.bseindia.com/BseIndiaAPI/api/X/w?a=1 returned 403 "
    "(1 of 3 inside 0:30:00); not retried — a refusal is answered by waiting or stopping"
)
BSE_SPIKE = (
    "ForbiddenSpikeError: ingestion from api.bseindia.com is hard-stopped after a 403 spike; "
    "this process will not request it again."
)
BSE_SPIKE_BARE = (
    "3 consecutive 403s from api.bseindia.com within 0:30:00 (latest: bse_corp_actions "
    "https://api.bseindia.com/BseIndiaAPI/api/DefaultData/w?x=1). Ingestion from this host is "
    "stopped for the life of this process."
)
SOFT_BLOCK = (
    "bse_scrip_master: expected a non-empty JSON array, got a list — an empty scrip master is a "
    "broken response, not news"
)
#: A new failure mode on an accepted source: still BSE, not the 403.
BSE_PARSE = "ParseError: bse_announcements: unexpected column 'NEWSSUB' in page 1"
#: The same 403 shape from a host nobody accepted.
NSE_403 = (
    "ForbiddenError: nse_announcements: https://www.nseindia.com/api/x returned 403 (1 of 3 "
    "inside 0:30:00); not retried"
)


def _row(
    source: str,
    day: date,
    error: str | None,
    *,
    state: SyncState = SyncState.FAILED,
    updated: datetime = RUN + timedelta(minutes=5),
) -> SyncRecord:
    return SyncRecord(
        source=source,
        logical_date=day,
        state=state,
        attempts=1,
        retryable=True,
        last_error=error,
        updated_at=updated,
    )


def _status(
    source: str, *, streak: int, success: bool = True, budget: int | None = None, **counts: int
) -> SourceStatus:
    return SourceStatus(
        source=source,
        last_success_date=date(2026, 9, 1) if success else None,
        last_success_at=None,
        latest_date=date(2026, 10, 22),
        lag_days=20,
        lag_sessions=15,
        failure_streak=streak,
        last_failure_date=date(2026, 10, 22) if streak else None,
        last_error=None,
        last_failure_retryable=True,
        counts={SyncState.FAILED: streak, **{SyncState(k): v for k, v in counts.items()}},
        max_lag_sessions=budget,
    )


@dataclass
class FakeStore:
    """The four `SyncStateStore` reads the judge makes, over in-memory rows."""

    rows: list[SyncRecord] = field(default_factory=list)
    statuses: list[SourceStatus] = field(default_factory=list)

    def failure_streak_rows(self, source: str) -> tuple[SyncRecord, ...]:
        mine = [r for r in self.rows if r.source.split("/")[0] == source]
        good = [r.logical_date for r in mine if r.state not in (SyncState.FAILED, SyncState.GAP)]
        floor = max(good, default=date.min)
        return tuple(r for r in mine if r.state is SyncState.FAILED and r.logical_date > floor)

    def rows_updated_between(
        self, sources: Sequence[str], start: datetime, end: datetime
    ) -> tuple[SyncRecord, ...]:
        return tuple(
            r
            for r in self.rows
            if r.source.split("/")[0] in sources and r.updated_at and start <= r.updated_at <= end
        )

    def get(self, source: str, logical_date: date) -> SyncRecord | None:
        return next(
            (r for r in self.rows if r.source == source and r.logical_date == logical_date), None
        )

    def source_statuses(self) -> tuple[SourceStatus, ...]:
        return tuple(self.statuses)


class _Result:
    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


@dataclass
class FakeConn:
    """`job_run`, `quality_flag` and `identity_reconciliation`, as the judge queries them."""

    attempts: Mapping[str, tuple[str, datetime, datetime | None, str | None]] = field(
        default_factory=dict
    )
    flags: Sequence[tuple[int, str | None, str | None, date, datetime]] = ()
    conflicts: Sequence[tuple[int, str, str, list[str]]] = ()
    queries: list[str] = field(default_factory=list)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Result:
        self.queries.append(sql)
        if "FROM job_run" in sql:
            hit = self.attempts.get(str(params[0]))
            return _Result([] if hit is None else [hit])
        if "FROM quality_flag" in sql:
            return _Result(self.flags)
        if "FROM identity_reconciliation" in sql:
            return _Result(self.conflicts)
        raise AssertionError(f"unexpected query: {sql}")


def _judge(
    registry: DeviationRegistry,
    *,
    store: FakeStore | None = None,
    conn: FakeConn | None = None,
    today: date = TODAY,
) -> DeviationJudge:
    return DeviationJudge(
        registry,
        cast(Connection, conn or FakeConn()),
        cast(SyncStateStore, store or FakeStore()),
        today,
    )


def _registry(tmp_path: Path, body: str) -> DeviationRegistry:
    path = tmp_path / "deviations.yaml"
    path.write_text(body, encoding="utf-8")
    return load_deviations(path)


_BASIS = """
    basis: {agreed_by: owner, agreed_on: 2026-10-12, recorded_in: ["HUMAN_DECISIONS.md D26"]}
    since: 2026-09-24"""


# ── the checked-in registry ───────────────────────────────────────────────────────────────


def test_the_checked_in_registry_loads_and_holds_the_bse_seeds() -> None:
    registry = load_deviations(DEVIATIONS_PATH)
    ids = {entry.id for entry in registry.deviations}
    assert {
        "bse-403-source-announcements",
        "bse-403-source-corp-actions",
        "bse-403-source-scrip-master",
        "bse-403-job-ca-refresh",
        "bse-403-job-announcements-capture",
        "bse-403-alert-announcements-streak",
        "bse-403-alert-corp-actions-streak",
        "bse-403-alert-scrip-master-streak",
        "bse-403-alert-ca-refresh",
        "bse-403-alert-announcements-capture",
    } <= ids
    for entry in registry.deviations:
        assert "D26" in entry.basis.summary(), entry.id
        assert entry.basis.agreed_by == "owner"
    assert default_deviations() == registry


def test_no_seed_targets_a_non_bse_item() -> None:
    """Only the BSE block is seeded: every target names a BSE source or a job with a BSE leg."""
    for entry in load_deviations().deviations:
        assert "bse" in entry.target or entry.target in (
            "ca_refresh",
            "announcements_capture",
            "job:ca_refresh:failed",
            "job:announcements_capture:failed",
        ), entry.id


# ── a malformed entry fails loud, naming itself ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (
            """
  - id: bad-pattern
    kind: source
    target: bse_announcements
    match: {errors: [{class: ForbiddenError, pattern: '(unclosed'}]}
    remark: x""",
            "does not compile",
        ),
        (
            """
  - id: class-only-job
    kind: job
    target: ca_refresh
    match: {error_classes: [CaRefreshError]}
    remark: x""",
            "a class alone",
        ),
        (
            """
  - id: unknown-field
    kind: source
    target: bse_announcements
    match: {errors: [{class: ForbiddenError, pattern: x}], name_only: true}
    remark: x""",
            "Extra inputs",
        ),
        (
            """
  - id: dangling-alert
    kind: alert
    target: ingest:bse_announcements:failed_streak
    match: {while_accepted: no-such-entry}
    remark: x""",
            "not an entry",
        ),
        (
            """
  - id: bad-gap-reason
    kind: gap
    target: bse_announcements
    match: {reasons: [WEEKEND], dates: [2026-10-10]}
    remark: x""",
            "not unexplained gap reasons",
        ),
        (
            """
  - id: unbounded-quality
    kind: quality
    target: ca_reconciliation
    match: {}
    remark: x""",
            "quality matcher needs",
        ),
        (
            """
  - id: bad-kind
    kind: everything
    target: bse_announcements
    match: {}
    remark: x""",
            "kind",
        ),
    ],
)
def test_a_malformed_entry_is_refused_by_name(tmp_path: Path, entry: str, message: str) -> None:
    with pytest.raises(DeviationRegistryError, match=message) as raised:
        _registry(tmp_path, f"version: 1\ndeviations:{entry}{_BASIS}\n")
    assert "deviations.yaml" in str(raised.value)


def test_a_malformed_entry_is_named_by_its_id(tmp_path: Path) -> None:
    body = f"""version: 1
deviations:
  - id: bad-pattern-entry
    kind: source
    target: bse_announcements
    match: {{errors: [{{class: ForbiddenError, pattern: '(unclosed'}}]}}
    remark: x{_BASIS}
"""
    with pytest.raises(DeviationRegistryError, match="entry 'bad-pattern-entry'"):
        _registry(tmp_path, body)


def test_an_alert_must_point_at_the_entry_for_its_own_subject(tmp_path: Path) -> None:
    body = f"""version: 1
deviations:
  - id: src
    kind: source
    target: bse_scrip_master
    match: {{errors: [{{class: ForbiddenError, pattern: x}}]}}
    remark: x{_BASIS}
  - id: wrong-subject
    kind: alert
    target: ingest:bse_announcements:failed_streak
    match: {{while_accepted: src}}
    remark: x{_BASIS}
"""
    with pytest.raises(DeviationRegistryError, match="is about source 'bse_announcements'"):
        _registry(tmp_path, body)


def test_duplicate_ids_and_unknown_top_level_keys_are_refused(tmp_path: Path) -> None:
    entry = f"""
  - id: twice
    kind: source
    target: bse_announcements
    match: {{errors: [{{class: ForbiddenError, pattern: x}}]}}
    remark: x{_BASIS}"""
    with pytest.raises(DeviationRegistryError, match="duplicate deviation ids"):
        _registry(tmp_path, f"version: 1\ndeviations:{entry}{entry}\n")
    with pytest.raises(DeviationRegistryError, match="Extra inputs"):
        _registry(tmp_path, f"version: 1\nanchors: 1\ndeviations:{entry}\n")


# ── the error shape: class prefix, message, dates ─────────────────────────────────────────


def test_the_class_is_read_from_the_prefix_only() -> None:
    assert error_class(BSE_SPIKE)[0] == "ForbiddenSpikeError"
    assert error_class(BSE_SPIKE_BARE) == (None, BSE_SPIKE_BARE)
    # A lower-case source id and a colon is a message, not a class.
    assert error_class(SOFT_BLOCK)[0] is None


def test_an_unprefixed_pattern_never_matches_a_prefixed_text_and_back() -> None:
    bare = ErrorPattern.model_validate({"class": None, "pattern": r"\d+ consecutive 403s"})
    prefixed = ErrorPattern.model_validate(
        {"class": "ForbiddenSpikeError", "pattern": r"\d+ consecutive 403s"}
    )
    assert bare.matches(BSE_SPIKE_BARE) and not prefixed.matches(BSE_SPIKE_BARE)
    assert prefixed.matches("ForbiddenSpikeError: " + BSE_SPIKE_BARE)
    assert not bare.matches("ForbiddenSpikeError: " + BSE_SPIKE_BARE)
    assert not bare.matches("SomeNewError: " + BSE_SPIKE_BARE)


def test_a_dated_pattern_matches_inside_its_window_only() -> None:
    soft = ErrorPattern.model_validate(
        {
            "class": None,
            "pattern": "^bse_scrip_master: expected a non-empty JSON array",
            "dated_from": "2026-09-08",
            "dated_to": "2026-09-23",
        }
    )
    assert soft.matches(SOFT_BLOCK, date(2026, 9, 8)) and soft.matches(
        SOFT_BLOCK, date(2026, 9, 23)
    )
    assert not soft.matches(SOFT_BLOCK, date(2026, 9, 24))
    assert not soft.matches(SOFT_BLOCK, date(2026, 9, 7))
    assert not soft.matches(SOFT_BLOCK, None)


# ── sources: the whole streak must be the known cause ─────────────────────────────────────

SEEDS = load_deviations(DEVIATIONS_PATH)


def _streak_store(source: str, errors: Sequence[tuple[date, str]]) -> FakeStore:
    rows = [_row(source, date(2026, 9, 1), None, state=SyncState.PUBLISHED)]
    rows += [_row(source, day, text) for day, text in errors]
    return FakeStore(rows=rows)


def _days(n: int, *, last: date = date(2026, 10, 22)) -> list[date]:
    return [last - timedelta(days=i) for i in range(n)]


def test_a_bse_source_failing_only_on_the_403_is_accepted() -> None:
    source = "bse_announcements"
    store = _streak_store(source, [(d, BSE_403.format(source=source)) for d in _days(3)])
    store.rows.append(_row(source, date(2026, 10, 19), BSE_SPIKE))
    verdict = _judge(SEEDS, store=store).source(_status(source, streak=4))
    assert verdict is not None
    assert verdict.status is DeviationStatus.ACCEPTED
    assert verdict.id == "bse-403-source-announcements"
    assert "HUMAN_DECISIONS.md D26" in verdict.basis


def test_one_new_failure_in_the_streak_keeps_the_source_an_error() -> None:
    """Not name-only: the same BSE source with any other error in its streak is not accepted."""
    source = "bse_announcements"
    errors = [(d, BSE_403.format(source=source)) for d in _days(3)]
    errors.append((date(2026, 10, 19), BSE_PARSE))
    store = _streak_store(source, errors)
    assert _judge(SEEDS, store=store).source(_status(source, streak=4)) is None


def test_another_sources_403_is_not_this_sources_403() -> None:
    source = "bse_announcements"
    store = _streak_store(
        source, [(d, BSE_403.format(source="bse_corp_actions")) for d in _days(2)]
    )
    assert _judge(SEEDS, store=store).source(_status(source, streak=2)) is None


def test_an_unaccepted_source_with_the_same_error_is_not_accepted() -> None:
    """Inverted or name-blind logic would accept this: no entry targets nse_announcements."""
    source = "nse_announcements"
    store = _streak_store(source, [(d, NSE_403) for d in _days(2)])
    conn = FakeConn()
    assert _judge(SEEDS, store=store, conn=conn).source(_status(source, streak=2)) is None
    assert conn.queries == []  # no entry, no evidence read


@pytest.mark.parametrize(
    "status",
    [
        _status("bse_announcements", streak=0, success=False),  # never landed, no failures
        _status("bse_announcements", streak=0, budget=2),  # overdue, with no failures
        _status("bse_announcements", streak=2, FETCHED=1),  # a row stuck mid-pipeline
        _status("bse_announcements", streak=5),  # the summary and the rows disagree (2 rows)
    ],
)
def test_a_source_unhealthy_for_another_reason_is_not_accepted(status: SourceStatus) -> None:
    source = "bse_announcements"
    store = _streak_store(source, [(d, BSE_403.format(source=source)) for d in _days(2)])
    assert not status.healthy
    assert _judge(SEEDS, store=store).source(status) is None


def test_a_healthy_source_has_no_verdict() -> None:
    healthy = _status("bse_announcements", streak=0)
    assert healthy.healthy
    assert _judge(SEEDS).source(healthy) is None


def test_the_scrip_master_soft_block_is_accepted_in_its_window_only() -> None:
    source = "bse_scrip_master"
    errors = [(d, BSE_403.format(source=source)) for d in _days(2)]
    errors.append((date(2026, 9, 15), SOFT_BLOCK))
    store = _streak_store(source, errors)
    assert _judge(SEEDS, store=store).source(_status(source, streak=3)) is not None
    store.rows.append(_row(source, date(2026, 10, 5), SOFT_BLOCK))  # same text, outside window
    assert _judge(SEEDS, store=store).source(_status(source, streak=4)) is None


def test_the_unprefixed_spike_is_accepted_for_corp_actions_only() -> None:
    for source, accepted in (("bse_corp_actions", True), ("bse_announcements", False)):
        store = _streak_store(source, [(date(2026, 10, 17), BSE_SPIKE_BARE)])
        verdict = _judge(SEEDS, store=store).source(_status(source, streak=1))
        assert (verdict is not None) is accepted, source


def _with_review(registry: DeviationRegistry, entry_id: str, review_by: date) -> DeviationRegistry:
    entries = tuple(
        entry.model_copy(update={"review_by": review_by}) if entry.id == entry_id else entry
        for entry in registry.deviations
    )
    return registry.model_copy(update={"deviations": entries})


def test_past_review_by_the_verdict_is_expired_not_accepted() -> None:
    source = "bse_announcements"
    store = _streak_store(source, [(d, BSE_403.format(source=source)) for d in _days(2)])
    registry = _with_review(SEEDS, "bse-403-source-announcements", date(2026, 10, 22))
    verdict = _judge(registry, store=store).source(_status(source, streak=2))
    assert verdict is not None and verdict.status is DeviationStatus.EXPIRED
    assert not verdict.accepted
    on_the_day = _judge(registry, store=store, today=date(2026, 10, 22))
    assert on_the_day.source(_status(source, streak=2)) is not None
    assert on_the_day.source(_status(source, streak=2)).accepted  # type: ignore[union-attr]


def test_max_age_turns_an_old_retry_red(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        f"""version: 1
deviations:
  - id: xbrl-404-retrying
    kind: source
    target: nse_xbrl_filing
    match:
      errors: [{{class: FetchHTTPError, pattern: 'returned 404'}}]
      max_age_days: 3
    remark: retried for three days{_BASIS}
""",
    )
    text = "FetchHTTPError: nse_xbrl_filing: https://x/y returned 404"
    young = _streak_store("nse_xbrl_filing", [(TODAY - timedelta(days=3), text)])
    old = _streak_store("nse_xbrl_filing", [(TODAY - timedelta(days=4), text)])
    status = _status("nse_xbrl_filing", streak=1)
    assert _judge(registry, store=young).source(status) is not None
    assert _judge(registry, store=old).source(status) is None


# ── jobs: the run's own legs decide ───────────────────────────────────────────────────────

CA_ERROR = (
    "CaRefreshError: CA refresh 2026-09-12..2026-10-17: NSE 1 published/0 already, 9 new actions; "
    "BSE 0 of 108 scrips, 0 new actions; 2 FAILED: BSE scrip 500083: ForbiddenError: ..."
)
FINISHED = RUN + timedelta(minutes=30)


def _ca_store(*extra: SyncRecord, nse_published: bool = True) -> FakeStore:
    rows = [
        _row("bse_corp_actions/500083", RUN.date(), BSE_403.format(source="bse_corp_actions")),
        _row("bse_corp_actions/500117", RUN.date(), BSE_SPIKE_BARE),
        _row("bse_corp_actions/500325", RUN.date(), None, state=SyncState.PUBLISHED),
        *extra,
    ]
    if nse_published:
        rows.append(_row("nse_corp_actions/refresh", RUN.date(), None, state=SyncState.PUBLISHED))
    return FakeStore(rows=rows)


def _ca_conn(state: str = "FAILED", error: str | None = CA_ERROR) -> FakeConn:
    return FakeConn(attempts={"ca_refresh": (state, RUN, FINISHED, error)})


def test_a_ca_refresh_whose_only_failures_are_bse_403s_is_accepted() -> None:
    verdict = _judge(SEEDS, store=_ca_store(), conn=_ca_conn()).job("ca_refresh", started_at=RUN)
    assert verdict is not None and verdict.accepted
    assert verdict.kind is DeviationKind.JOB


def test_a_failed_nse_leg_keeps_the_job_an_error() -> None:
    nse = _row("nse_corp_actions/refresh", RUN.date(), "TransportError: www.nseindia.com timed out")
    store = _ca_store(nse, nse_published=False)
    assert _judge(SEEDS, store=store, conn=_ca_conn()).job("ca_refresh") is None


def test_a_new_bse_failure_mode_keeps_the_job_an_error() -> None:
    bad = _row("bse_corp_actions/500999", RUN.date(), "KeyError: 'Ex_date'")
    assert _judge(SEEDS, store=_ca_store(bad), conn=_ca_conn()).job("ca_refresh") is None


def test_a_leg_left_mid_pipeline_keeps_the_job_an_error() -> None:
    stuck = _row("bse_corp_actions/500888", RUN.date(), None, state=SyncState.FETCHED)
    assert _judge(SEEDS, store=_ca_store(stuck), conn=_ca_conn()).job("ca_refresh") is None


def test_the_nse_refresh_must_have_landed_on_the_run_date() -> None:
    store = _ca_store(nse_published=False)
    assert _judge(SEEDS, store=store, conn=_ca_conn()).job("ca_refresh") is None


@pytest.mark.parametrize(
    ("state", "error"),
    [
        ("TIMED_OUT", CA_ERROR),  # overran its budget: not the known cause
        ("FAILED", CA_ERROR.replace("CaRefreshError", "OperationalError")),  # another exception
        ("FAILED", None),  # no recorded cause
        ("SUCCEEDED", None),  # nothing to accept
    ],
)
def test_only_the_known_exception_of_a_failed_run_is_accepted(
    state: str, error: str | None
) -> None:
    assert _judge(SEEDS, store=_ca_store(), conn=_ca_conn(state, error)).job("ca_refresh") is None


def test_a_run_with_no_failed_leg_is_not_explained_by_its_legs() -> None:
    """The run raised, but none of its legs failed: whatever broke it, it was not the 403."""
    store = FakeStore(
        rows=[
            _row("bse_corp_actions/500325", RUN.date(), None, state=SyncState.PUBLISHED),
            _row("nse_corp_actions/refresh", RUN.date(), None, state=SyncState.PUBLISHED),
        ]
    )
    assert _judge(SEEDS, store=store, conn=_ca_conn()).job("ca_refresh") is None


def test_a_verdict_about_another_attempt_is_not_this_lines() -> None:
    judge = _judge(SEEDS, store=_ca_store(), conn=_ca_conn())
    assert judge.job("ca_refresh", started_at=RUN + timedelta(days=7)) is None


def _ann_error(*legs: str) -> str:
    return (
        f"DailyCaptureError: announcements_capture: {', '.join(legs)}; 14 request(s) — 2 owed "
        "date(s) did not land; each is FAILED in sync_state and the next fire retries it"
    )


def _ann(day: str, state: str, source: str = "bse_announcements") -> str:
    return f"{source}@{day}={state}"


def _ann_store(*extra: SyncRecord) -> FakeStore:
    rows = [
        _row("bse_announcements", date(2026, 10, 21), BSE_SPIKE),
        _row("bse_announcements", date(2026, 10, 22), BSE_403.format(source="bse_announcements")),
        _row("nse_announcements", date(2026, 10, 22), None, state=SyncState.PUBLISHED),
        *extra,
    ]
    return FakeStore(rows=rows)


def _ann_conn(error: str) -> FakeConn:
    return FakeConn(attempts={"announcements_capture": ("FAILED", RUN, FINISHED, error)})


GOOD_ANN = _ann_error(
    _ann("2026-10-21", "ALREADY_PUBLISHED", "nse_announcements"),
    _ann("2026-10-21", "FAILED"),
    _ann("2026-10-22", "CAPTURED", "nse_announcements"),
    _ann("2026-10-22", "FAILED"),
)


def test_a_capture_whose_only_failed_legs_are_bse_403s_is_accepted() -> None:
    judge = _judge(SEEDS, store=_ann_store(), conn=_ann_conn(GOOD_ANN))
    verdict = judge.job("announcements_capture")
    assert verdict is not None and verdict.accepted
    assert verdict.remark == "Only the BSE leg fails (403); NSE announcements land nightly."


def test_a_failed_nse_leg_in_the_runs_own_account_keeps_it_an_error() -> None:
    """The NSE row may already be PUBLISHED (a re-fetch failure leaves it so): the text says."""
    error = _ann_error(
        _ann("2026-10-22", "FAILED", "nse_announcements"), _ann("2026-10-22", "FAILED")
    )
    assert (
        _judge(SEEDS, store=_ann_store(), conn=_ann_conn(error)).job("announcements_capture")
        is None
    )


@pytest.mark.parametrize(
    "error",
    [
        GOOD_ANN[:-40] + "…",  # truncated: the legs past the cut are unknown
        _ann_error(
            _ann("2026-10-22", "PROVISIONAL", "nse_announcements"), _ann("2026-10-22", "FAILED")
        ),
        "DailyCaptureError: announcements_capture: nothing owed; 0 request(s)",  # no legs at all
        _ann_error(_ann("2026-10-20", "FAILED")),  # a FAILED leg with no FAILED 403 row behind it
    ],
)
def test_a_capture_account_that_does_not_agree_keeps_it_an_error(error: str) -> None:
    assert (
        _judge(SEEDS, store=_ann_store(), conn=_ann_conn(error)).job("announcements_capture")
        is None
    )


# ── alerts follow their subject ───────────────────────────────────────────────────────────


def test_a_streak_alert_is_accepted_while_its_source_is() -> None:
    source = "bse_scrip_master"
    store = _streak_store(source, [(d, BSE_403.format(source=source)) for d in _days(3)])
    store.statuses = [_status(source, streak=3)]
    verdict = _judge(SEEDS, store=store).alert("ingest:bse_scrip_master:failed_streak")
    assert verdict is not None and verdict.accepted
    assert verdict.kind is DeviationKind.ALERT


def test_a_streak_alert_whose_source_has_a_new_error_is_not_accepted() -> None:
    source = "bse_scrip_master"
    store = _streak_store(source, [(date(2026, 10, 22), "ParseError: bse_scrip_master: bad")])
    store.statuses = [_status(source, streak=1)]
    assert _judge(SEEDS, store=store).alert("ingest:bse_scrip_master:failed_streak") is None


def test_a_job_alert_follows_the_jobs_newest_finished_attempt() -> None:
    accepted = _judge(SEEDS, store=_ca_store(), conn=_ca_conn())
    assert accepted.alert("job:ca_refresh:failed") is not None
    other = _judge(SEEDS, store=_ca_store(), conn=_ca_conn(error="KeyError: 'x'"))
    assert other.alert("job:ca_refresh:failed") is None


def test_an_alert_with_no_entry_is_not_accepted_and_costs_no_read() -> None:
    conn = FakeConn()
    assert _judge(SEEDS, conn=conn).alert("job:identity_refresh:failed") is None
    assert _judge(SEEDS, conn=conn).alert("quality:ca_reconciliation:red") is None
    assert conn.queries == []


def test_an_expired_subject_makes_the_alert_expired() -> None:
    source = "bse_scrip_master"
    store = _streak_store(source, [(d, BSE_403.format(source=source)) for d in _days(2)])
    store.statuses = [_status(source, streak=2)]
    registry = _with_review(SEEDS, "bse-403-source-scrip-master", date(2026, 10, 1))
    verdict = _judge(registry, store=store).alert("ingest:bse_scrip_master:failed_streak")
    assert verdict is not None and verdict.status is DeviationStatus.EXPIRED


# ── set-valued matchers: quality flags, identity conflicts, gaps ──────────────────────────


def test_a_quality_check_is_accepted_only_while_every_flag_is_in_the_set(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        f"""version: 1
deviations:
  - id: ca-recon-backlog
    kind: quality
    target: ca_reconciliation
    match:
      fingerprints: [aa, bb]
      raised_before: 2026-10-07T00:00:00+05:30
    remark: the pre-2026-10-07 backlog{_BASIS}
""",
    )
    old = datetime(2026, 10, 1, tzinfo=IST)
    known = [(1, "aa", "INE1", date(2001, 1, 1), old), (2, "bb", None, date(2002, 1, 1), old)]
    assert _judge(registry, conn=FakeConn(flags=known)).quality("ca_reconciliation") is not None
    new_print = [*known, (3, "cc", "INE3", date(2003, 1, 1), old)]
    assert _judge(registry, conn=FakeConn(flags=new_print)).quality("ca_reconciliation") is None
    late = [(1, "aa", "INE1", date(2001, 1, 1), datetime(2026, 10, 8, tzinfo=IST))]
    assert _judge(registry, conn=FakeConn(flags=late)).quality("ca_reconciliation") is None
    assert _judge(registry, conn=FakeConn(flags=[])).quality("ca_reconciliation") is None
    assert _judge(registry, conn=FakeConn(flags=known)).quality("l2_continuity") is None


def test_an_identity_conflict_set_accepts_known_conflicts_only(tmp_path: Path) -> None:
    registry = _registry(
        tmp_path,
        f"""version: 1
deviations:
  - id: bse-identity-conflicts
    kind: job
    target: identity_refresh
    match:
      error_classes: [RuntimeError]
      error_pattern: '\\d+ conflict\\(s\\)$'
      identity_conflicts: {{ids: [1, 2], exchange: BSE, detected_by: INGEST}}
    remark: ten parked BSE conflicts{_BASIS}
""",
    )
    error = "RuntimeError: identity refresh 2026-10-17: 2 file(s) in L0, 2 conflict(s)"
    attempts = {"identity_refresh": ("FAILED", RUN, FINISHED, error)}
    known = [
        (1, "BSE", "INGEST", ["INE1"]),
        (2, "BSE", "INGEST", ["INE2"]),
        (28, "NSE", "RESOLVE", ["X"]),
    ]
    conn = FakeConn(attempts=attempts, conflicts=known)
    assert _judge(registry, conn=conn).job("identity_refresh") is not None
    new = [*known, (29, "BSE", "INGEST", ["INE9"])]
    conn = FakeConn(attempts=attempts, conflicts=new)
    assert _judge(registry, conn=conn).job("identity_refresh") is None
    other = {"identity_refresh": ("FAILED", RUN, FINISHED, "RuntimeError: L0 unreadable")}
    assert (
        _judge(registry, conn=FakeConn(attempts=other, conflicts=known)).job("identity_refresh")
        is None
    )


def test_a_gap_is_accepted_on_its_own_error_and_reason() -> None:
    judge = _judge(SEEDS)
    day = date(2026, 10, 20)
    ok = GapItem("bse_announcements", day, "FAILED", BSE_SPIKE)
    assert judge.gap(ok) is not None and judge.gap(ok).accepted  # type: ignore[union-attr]
    assert judge.gap(GapItem("bse_announcements", day, "NEVER_ATTEMPTED", None)) is None
    assert judge.gap(GapItem("bse_announcements", day, "FAILED", BSE_PARSE)) is None
    assert judge.gap(GapItem("nse_announcements", day, "FAILED", BSE_SPIKE)) is None
