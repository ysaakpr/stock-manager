"""M3.9.b — the benchmark-TRI backfill driver, offline (B8).

`test_benchmark_tri.py` proves the parser against frozen payloads. This proves the *runner* around
it: that one index goes `fetch -> L0 -> parse -> L1 -> sync` in that order, that what lands in L0 is
the exact bytes the endpoint sent with a receipt over them, that the POST body is the `cinfo`
envelope, that a second run of the same window fetches nothing, and that a failure is recorded on
the index's own sync row rather than swallowed.

Every response is scripted through `RecordedTransport` and sockets are monkeypatched out, so the
live campaign this driver performs is a driver run and this file is not it.
"""

from __future__ import annotations

import json
import shutil
import socket
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
    ScriptedOutcome,
)
from dataplatform.ingest.indices import (
    TRI_METHOD_COMPUTED,
    TRI_METHOD_PUBLISHED,
    SyncTracker,
    TriPoint,
    TriSeries,
    l0_tri_filename,
    parse_l0_tri_filename,
    read_tri_series,
    tri_state_source,
    tri_url,
    write_tri_l1,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.ingest.tri_backfill import (
    DEFAULT_INDEX_SET,
    EARLIEST_REQUESTED,
    IndexSpec,
    NoStoredPayloadError,
    already_published,
    rebuild_tri_from_l0,
    run_tri_backfill,
    stored_tri_payloads,
)
from dataplatform.status.sync_state import SyncRecord, SyncState
from dataplatform.store.l0 import L0Store
from tests.conftest import SettingsLoader
from tests.unit.test_indices import RecordingTracker

FIXTURES: Final = Path("tests/fixtures/nifty_indices/tri/2026")
NOW: Final = datetime(2026, 9, 8, 9, 15, tzinfo=IST)

WINDOW_START: Final = date(2021, 4, 1)
WINDOW_END: Final = date(2026, 3, 31)
NIFTY50: Final = IndexSpec(name="NIFTY 50", slug="nifty50")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """A socket here is a bug: the live fetch is a driver run, never a test (B8)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; the TRI backfill tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class SpyAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str, str]] = []

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        self.sent.append((severity, title, body, dedup_key))
        return AlertOutcome.SENT


@pytest.fixture
def register() -> SourceRegister:
    return load_register()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def tracker(clock: FrozenClock) -> RecordingTracker:
    return RecordingTracker(clock)


def _payload(slug: str) -> bytes:
    return (FIXTURES / f"tri_{slug}_20210401_20260331.json").read_bytes()


def _wire(
    script: dict[str, ScriptedOutcome | list[ScriptedOutcome]],
    *,
    clock: FrozenClock,
    settings: Settings,
    register: SourceRegister,
    data_root: Path,
) -> tuple[Fetcher, L0Store, RecordedTransport]:
    transport = RecordedTransport(script)
    l0 = L0Store(clock=clock, data_root=data_root)
    fetcher = Fetcher(
        transport=transport,
        l0=l0,
        alerter=SpyAlerter(),
        clock=clock,
        register=register,
        settings=settings,
        sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
    )
    return fetcher, l0, transport


@pytest.fixture
def settings(load_settings: SettingsLoader) -> Settings:
    return load_settings(None)


def _ok(slug: str) -> RecordedResponse:
    """A scripted success, `text/html` and all — which is what this source really answers with."""
    return RecordedResponse(body=_payload(slug), headers={"content-type": "text/html"})


# ── the happy path: one index, fetch -> L0 -> parse -> L1 -> sync ─────────────────────────────


def test_one_index_lands_in_l0_and_l1_and_reaches_published(
    clock: FrozenClock,
    settings: Settings,
    register: SourceRegister,
    tracker: RecordingTracker,
    tmp_path: Path,
) -> None:
    """The whole path, in order, for one index — and L1 carries the exchange's own levels."""
    fetcher, l0, transport = _wire(
        {tri_url(register): _ok("nifty50")},
        clock=clock,
        settings=settings,
        register=register,
        data_root=tmp_path,
    )
    outcomes = run_tri_backfill(
        fetcher=fetcher,
        l0=l0,
        tracker=tracker,
        indices=(NIFTY50,),
        start=WINDOW_START,
        end=WINDOW_END,
        data_root=tmp_path,
    )

    assert [outcome.points for outcome in outcomes] == [1239]
    assert outcomes[0].earliest == WINDOW_START
    assert outcomes[0].latest == date(2026, 3, 30)

    # L0 holds the payload byte-for-byte, under a name that carries the index and the window.
    stored = next((tmp_path / "L0" / "nifty_tri_history").rglob("tri_nifty50_*.json"))
    assert stored.read_bytes() == _payload("nifty50")
    assert (
        stored.with_suffix(".json.meta.json").exists()
        or (stored.parent / f"{stored.name}.meta.json").exists()
    )

    # L1 holds the published series, and the spot-check level round-trips exactly.
    series = read_tri_series("nifty50", WINDOW_END, method=TRI_METHOD_PUBLISHED, data_root=tmp_path)
    assert series is not None
    assert series.method == TRI_METHOD_PUBLISHED
    assert series.points[-1].tri_value == Decimal("33655.43")

    # The §4.4 transitions ran in order, on the per-index sync row, ending PUBLISHED.
    assert tracker.history == [
        SyncState.PENDING,
        SyncState.FETCHED,
        SyncState.VALIDATED,
        SyncState.NORMALIZED,
        SyncState.PUBLISHED,
    ]
    row = tracker.rows[(tri_state_source("nifty50"), WINDOW_END)]
    assert row.state is SyncState.PUBLISHED
    assert row.checksum

    # One request, and it is the POST with the cinfo envelope.
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.payload is not None
    assert json.loads(request.payload)["cinfo"] == (
        "{'name':'NIFTY 50','startDate':'01-Apr-2021',"
        "'endDate':'31-Mar-2026','indexName':'NIFTY 50'}"
    )
    assert "cookie" not in {name.lower() for name in request.headers}


def test_three_indices_each_get_their_own_sync_row(
    clock: FrozenClock,
    settings: Settings,
    register: SourceRegister,
    tracker: RecordingTracker,
    tmp_path: Path,
) -> None:
    """One register row serves every index, so the sync rows must be qualified per slug.

    Unqualified, all three would collide on one `(source, logical_date)` row and the second
    index's `begin` would be an illegal transition out of a terminal state.
    """
    fetcher, l0, _ = _wire(
        {tri_url(register): [_ok("nifty50"), _ok("niftyit"), _ok("niftycpse")]},
        clock=clock,
        settings=settings,
        register=register,
        data_root=tmp_path,
    )
    run_tri_backfill(
        fetcher=fetcher,
        l0=l0,
        tracker=tracker,
        indices=DEFAULT_INDEX_SET,
        start=WINDOW_START,
        end=WINDOW_END,
        data_root=tmp_path,
    )
    keys = set(tracker.rows)
    assert keys == {
        ("nifty_tri_history/nifty50", WINDOW_END),
        ("nifty_tri_history/niftyit", WINDOW_END),
        ("nifty_tri_history/niftycpse", WINDOW_END),
    }
    assert all(row.state is SyncState.PUBLISHED for row in tracker.rows.values())


# ── resume ────────────────────────────────────────────────────────────────────────────────────


def test_a_second_run_of_the_same_window_fetches_nothing(
    clock: FrozenClock,
    settings: Settings,
    register: SourceRegister,
    tracker: RecordingTracker,
    tmp_path: Path,
) -> None:
    """Resume is read off the L1 artefact, so re-running the driver is a true no-op."""
    fetcher, l0, transport = _wire(
        {tri_url(register): _ok("nifty50")},
        clock=clock,
        settings=settings,
        register=register,
        data_root=tmp_path,
    )
    kwargs: dict[str, Any] = {
        "fetcher": fetcher,
        "l0": l0,
        "tracker": tracker,
        "indices": (NIFTY50,),
        "start": WINDOW_START,
        "end": WINDOW_END,
        "data_root": tmp_path,
    }
    run_tri_backfill(**kwargs)
    assert len(transport.requests) == 1

    again = run_tri_backfill(**kwargs)
    assert len(transport.requests) == 1
    assert again[0].skipped is True
    assert "skipped" in again[0].line


def test_a_computed_series_on_disk_is_not_a_reason_to_skip(tmp_path: Path) -> None:
    """The estimate existing must never stop the real series being fetched.

    This is the mistake that produced the whole defect: §4.1's computed fallback was present and
    everything downstream treated it as the benchmark, so nothing ever went looking for the
    published series.
    """
    write_tri_l1(
        TriSeries(
            index_slug="nifty50",
            index_name="Nifty 50",
            method=TRI_METHOD_COMPUTED,
            points=(
                TriPoint(
                    index_slug="nifty50",
                    index_name="Nifty 50",
                    as_of=WINDOW_START,
                    tri_value=Decimal("14000.0000"),
                    method=TRI_METHOD_COMPUTED,
                ),
            ),
        ),
        data_root=tmp_path,
    )
    assert already_published(NIFTY50, WINDOW_START, tmp_path) is False


def test_a_shallower_stored_series_does_not_satisfy_a_deeper_window(
    clock: FrozenClock,
    settings: Settings,
    register: SourceRegister,
    tracker: RecordingTracker,
    tmp_path: Path,
) -> None:
    """A published series from 2021 must not skip a run asking back to 1990."""
    fetcher, l0, _ = _wire(
        {tri_url(register): _ok("nifty50")},
        clock=clock,
        settings=settings,
        register=register,
        data_root=tmp_path,
    )
    run_tri_backfill(
        fetcher=fetcher,
        l0=l0,
        tracker=tracker,
        indices=(NIFTY50,),
        start=WINDOW_START,
        end=WINDOW_END,
        data_root=tmp_path,
    )
    assert already_published(NIFTY50, WINDOW_START, tmp_path) is True
    assert already_published(NIFTY50, EARLIEST_REQUESTED, tmp_path) is False


# ── failure is recorded, not swallowed ────────────────────────────────────────────────────────


def test_the_stale_paths_html_fails_the_row_and_raises(
    clock: FrozenClock,
    settings: Settings,
    register: SourceRegister,
    tracker: RecordingTracker,
    tmp_path: Path,
) -> None:
    """A 200 carrying markup parks the sync row FAILED and re-raises — never a stored benchmark.

    `retryable` is False: the same request will return the same markup, so a retry is not a
    different outcome, it is the same one later.
    """
    fetcher, l0, _ = _wire(
        {
            tri_url(register): RecordedResponse(
                body=b"<!DOCTYPE html><html>home</html>",
                headers={"content-type": "text/html"},
            )
        },
        clock=clock,
        settings=settings,
        register=register,
        data_root=tmp_path,
    )
    with pytest.raises(Exception, match="markup, not JSON"):
        run_tri_backfill(
            fetcher=fetcher,
            l0=l0,
            tracker=tracker,
            indices=(NIFTY50,),
            start=WINDOW_START,
            end=WINDOW_END,
            data_root=tmp_path,
        )

    row = tracker.rows[(tri_state_source("nifty50"), WINDOW_END)]
    assert row.state is SyncState.FAILED
    assert row.retryable is False
    assert read_tri_series("nifty50", WINDOW_END, data_root=tmp_path) is None


def _tracker_protocol_is_satisfied_by_the_double(tracker: RecordingTracker) -> SyncTracker:
    """`mypy --strict` fails here if the driver's tracker contract drifts from the double."""
    return tracker


def _sync_record_is_used(record: SyncRecord) -> SyncState:
    return record.state


# ── --from-l0: re-deriving L1 from the immutable record, with no fetch ────────────────────────


def test_l0_filename_round_trips_through_its_parser() -> None:
    """`parse_l0_tri_filename` is the exact inverse of `l0_tri_filename`, underscores included.

    A slug may legally carry `_` (`TriPoint.index_slug` allows it), which is why the parser is not
    a `split("_")` — that would take the window apart in the wrong place and file a rebuild's
    output under a truncated index.
    """
    for slug in ("nifty50", "nifty_next_50"):
        name = l0_tri_filename(slug, WINDOW_START, WINDOW_END)
        assert parse_l0_tri_filename(name) == (slug, WINDOW_START, WINDOW_END)


@pytest.mark.parametrize(
    "filename",
    ["ind_nifty50list.csv", "tri_nifty50_20210401.json", "tri_nifty50_20211301_20260331.json"],
)
def test_a_name_that_is_not_an_l0_tri_payload_raises(filename: str) -> None:
    """A payload filed under an unreadable name stops the rebuild instead of being skipped."""
    with pytest.raises(Exception, match=filename):
        parse_l0_tri_filename(filename)


def test_rebuild_from_l0_reproduces_the_fetching_run_byte_for_byte(
    clock: FrozenClock,
    settings: Settings,
    register: SourceRegister,
    tracker: RecordingTracker,
    tmp_path: Path,
) -> None:
    """L0 determines L1: rebuilding into a second lake gives the same parquet bytes, no requests.

    This is the property that makes moving a campaign honest. `data/L0` is the immutable record and
    L1 is a derivation of it (invariant #1), so promoting a run to another lake may copy the *raw*
    payloads — their original receipts and all — and re-derive everything else, rather than copying
    a derived layer and hoping the two agree. If this ever stops holding, `cp -a` of L1 is the only
    remaining route and the runbook has to say so.
    """
    fetched_root, rebuilt_root = tmp_path / "fetched", tmp_path / "rebuilt"
    fetcher, l0, transport = _wire(
        {tri_url(register): [_ok("nifty50"), _ok("niftyit")]},
        clock=clock,
        settings=settings,
        register=register,
        data_root=fetched_root,
    )
    indices = (NIFTY50, IndexSpec(name="NIFTY IT", slug="niftyit"))
    run_tri_backfill(
        fetcher=fetcher,
        l0=l0,
        tracker=tracker,
        indices=indices,
        start=WINDOW_START,
        end=WINDOW_END,
        data_root=fetched_root,
    )
    requests_after_fetch = len(transport.requests)

    # The transfer: the raw payloads and their receipts, copied verbatim. Nothing derived moves.
    shutil.copytree(fetched_root / "L0", rebuilt_root / "L0")

    outcomes = rebuild_tri_from_l0(
        l0=L0Store(clock=clock, data_root=rebuilt_root),
        indices=indices,
        data_root=rebuilt_root,
    )

    assert [outcome.spec.slug for outcome in outcomes] == ["nifty50", "niftyit"]
    assert [outcome.points for outcome in outcomes] == [1239, 1239]
    assert all(not outcome.skipped for outcome in outcomes)
    assert outcomes[0].l0_key == f"nifty_tri_history/{WINDOW_END.isoformat()}/" + l0_tri_filename(
        "nifty50", WINDOW_START, WINDOW_END
    )

    fetched_l1, rebuilt_l1 = fetched_root / "L1", rebuilt_root / "L1"
    written = sorted(path.relative_to(rebuilt_l1) for path in rebuilt_l1.rglob("*.parquet"))
    assert written == sorted(path.relative_to(fetched_l1) for path in fetched_l1.rglob("*.parquet"))
    assert written, "the rebuild wrote no partitions at all"
    for relative in written:
        assert (rebuilt_l1 / relative).read_bytes() == (fetched_l1 / relative).read_bytes()

    # It costs nothing at the source and claims nothing about the sync row.
    assert len(transport.requests) == requests_after_fetch
    assert tracker.rows.keys() == {(tri_state_source(spec.slug), WINDOW_END) for spec in indices}


def test_rebuild_from_l0_refuses_an_index_it_has_no_payload_for(
    clock: FrozenClock, tmp_path: Path
) -> None:
    """An empty L0 is a stop, not a silent no-op — "no benchmark" must not look like "done"."""
    with pytest.raises(NoStoredPayloadError, match="niftycpse"):
        rebuild_tri_from_l0(
            l0=L0Store(clock=clock, data_root=tmp_path),
            indices=(IndexSpec(name="NIFTY CPSE", slug="niftycpse"),),
            data_root=tmp_path,
        )
    assert read_tri_series("niftycpse", WINDOW_END, data_root=tmp_path) is None


def test_stored_payloads_are_grouped_by_index_and_a_stranger_is_ignored(
    clock: FrozenClock,
    settings: Settings,
    register: SourceRegister,
    tracker: RecordingTracker,
    tmp_path: Path,
) -> None:
    """A lake may hold indices this run was not asked about; they are not rebuilt by accident."""
    fetcher, l0, _ = _wire(
        {tri_url(register): [_ok("nifty50"), _ok("niftycpse")]},
        clock=clock,
        settings=settings,
        register=register,
        data_root=tmp_path,
    )
    run_tri_backfill(
        fetcher=fetcher,
        l0=l0,
        tracker=tracker,
        indices=(NIFTY50, IndexSpec(name="NIFTY CPSE", slug="niftycpse")),
        start=WINDOW_START,
        end=WINDOW_END,
        data_root=tmp_path,
    )

    grouped = stored_tri_payloads(l0, (NIFTY50,))
    assert set(grouped) == {"nifty50"}
    assert [ref.filename for ref in grouped["nifty50"]] == [
        l0_tri_filename("nifty50", WINDOW_START, WINDOW_END)
    ]
