"""M14.3 (XBRL): a retry re-downloads an empty or truncated stored document; the one-time re-queue.

Offline (B8): the fetcher is a scripted stand-in that stores what it "downloads" through the real
`L0Store`, `sync_state` is an in-memory stand-in, and the index page and the document are captured
fixtures. Each test fails if its rule is inverted:

* a retry after an empty or truncated stored body downloads the document again — once per run —
  and stores it *beside* the old capture, which stays byte-for-byte as it was (invariant #1);
* a complete stored document is never downloaded again, even when the parser refuses it;
* a rebuild uses the newest capture and never fetches;
* a 404 for a document the index lists as `0 Bytes` records that statement, and only then;
* a re-queue item is driven once, journaled, and never again; `record_evidence` makes no request.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast
from zoneinfo import ZoneInfo

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest import fundamentals_backfill as fb
from dataplatform.ingest import fundamentals_requeue as rq
from dataplatform.ingest.fetcher import FetchHTTPError
from dataplatform.ingest.xbrl import parser
from dataplatform.status.sync_state import SyncState
from dataplatform.store.l0 import L0Ref, L0Store

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "xbrl" / "integrated"
GSS_DOC: Final = "INTEGRATED_FILING_INDAS_1461892_31052025112035_WEB.xml"
GSS_URL: Final = f"https://nsearchives.nseindia.com/corporate/xbrl/{GSS_DOC}"
GSS: Final = "INE871H01011"
FILED: Final = date(2025, 5, 31)
INDEX_FILE: Final = "integrated-filing-results_20250531_20250531_p01.json"
INDEX_KEY: Final = f"nse_integrated_filing_index/{FILED.isoformat()}/{INDEX_FILE}"
GOOD: Final = (FIXTURES / GSS_DOC).read_bytes()
#: The Q4 record: the index yields a Quarterly entry (IF97542) and an Annual one (IF97542-FY) over
#: the same document — so one run asks for it twice, which must cost at most one download.
RECORD: Final[dict[str, object]] = {
    "audited": "Audited",
    "broadcast_Date": "31-May-2025 23:20:35",
    "cmName": "GSS Infotech Limited",
    "consolidated": "Standalone",
    "creation_Date": "31-May-2025 23:20:36",
    "qe_Date": "31-MAR-2025",
    "seq_Id": "97542",
    "symbol": "GSS",
    "type": "Integrated Filing- Financials",
    "type_Sub": "Original",
    "xbrl": GSS_URL,
    "xbrlFileSize": "57.10 KB",
}
UNITS: Final = ("IF97542", "IF97542-FY")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; these tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


# ── stand-ins ────────────────────────────────────────────────────────────────────────────────


class _Fetcher:
    """Serves a scripted body per URL (None → 404), storing each through the real L0 store."""

    def __init__(self, l0: L0Store, bodies: dict[str, bytes | None]) -> None:
        self._l0 = l0
        self.bodies = bodies
        self.requests: list[tuple[str, str]] = []

    def fetch(
        self, source_id: str, url: str, logical_date: date, *, filename: str | None = None
    ) -> L0Ref:
        name = filename or url.rsplit("/", 1)[-1]
        self.requests.append((url, name))
        body = self.bodies[url]
        if body is None:
            raise FetchHTTPError(
                f"{source_id}: {url} returned 404, which no retry would change",
                status_code=404,
                url=url,
            )
        return self._l0.put(source_id, logical_date, name, body)


@dataclass
class _Row:
    state: SyncState
    retryable: bool = True
    last_error: str | None = None
    attempts: int = 1


@dataclass
class _Sync:
    rows: dict[tuple[str, date], _Row] = field(default_factory=dict)

    def get(self, source: str, logical_date: date) -> _Row | None:
        return self.rows.get((source, logical_date))

    def begin(self, source: str, logical_date: date) -> _Row:
        row = self.rows.get((source, logical_date))
        if row is None:
            row = self.rows[(source, logical_date)] = _Row(SyncState.PENDING, attempts=0)
        assert row.state is not SyncState.PUBLISHED, "a PUBLISHED row was re-begun"
        row.state, row.last_error, row.attempts = SyncState.PENDING, None, row.attempts + 1
        return row

    def _to(self, source: str, logical_date: date, state: SyncState) -> _Row:
        row = self.rows[(source, logical_date)]
        row.state = state
        return row

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> _Row:
        return self._to(source, logical_date, SyncState.FETCHED)

    def mark_validated(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.VALIDATED)

    def mark_normalized(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.NORMALIZED)

    def mark_published(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> _Row:
        row = self._to(source, logical_date, SyncState.FAILED)
        row.last_error, row.retryable = error, retryable
        return row

    def state(self, unit: str) -> _Row:
        return self.rows[(f"nse_xbrl_filing/{unit}", FILED)]


@dataclass
class _World:
    tmp: Path
    clock: FrozenClock
    l0: L0Store
    sync: _Sync

    def runner(self, bodies: dict[str, bytes | None], **kwargs: Any) -> tuple[Any, _Fetcher]:
        fetcher = _Fetcher(self.l0, bodies)
        runner = fb.FundamentalsBackfillRunner(
            fetcher=cast(Any, fetcher),
            l0=self.l0,
            sync=cast(Any, self.sync),
            universe={GSS},
            commit=lambda: None,
            data_root=self.tmp,
            resolve_isin=lambda symbol, on: GSS if symbol == "GSS" else None,
            **{"clock": self.clock, **kwargs},
        )
        return runner, fetcher

    def run(self, bodies: dict[str, bytes | None], **kwargs: Any) -> tuple[Any, _Fetcher]:
        """One nightly re-read of the stored index page: every not-yet-published entry driven."""
        runner, fetcher = self.runner(bodies, **kwargs)
        return runner.run([_index_unit()]), fetcher

    def captures(self) -> list[Path]:
        folder = self.l0.root / "nse_xbrl_filing" / "2025" / "05"
        return sorted(p for p in folder.iterdir() if not p.name.endswith(".meta.json"))


def _index_unit() -> fb.IndexUnit:
    return fb.IndexUnit(
        period=fb.Period.QUARTERLY,
        from_date=FILED,
        to_date=FILED,
        url="https://www.nseindia.com/unused",
        filename=INDEX_FILE,
        feed=fb.IndexFeed.INTEGRATED,
        page=1,
    )


def _page(record: dict[str, object]) -> bytes:
    return json.dumps({"data": [record], "size": 1000, "page": 0, "totalCount": 1}).encode()


@pytest.fixture
def world(tmp_path: Path) -> Iterator[_World]:
    clock = FrozenClock(datetime(2026, 11, 2, 2, 0, 5, tzinfo=IST))
    l0 = L0Store(clock=clock, data_root=tmp_path)
    sync = _Sync()
    l0.put("nse_integrated_filing_index", FILED, INDEX_FILE, _page(RECORD))
    # The index page is a done window: the run re-reads it from L0 and makes no index request.
    sync.rows[(_index_unit().state_source, FILED)] = _Row(SyncState.PUBLISHED)
    yield _World(tmp_path, clock, l0, sync)


# ── payload_defect ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "defect"),
    [
        (b"", "empty"),
        (b"  \n", "empty"),
        (GOOD[:32768], "truncated"),  # PARADEEP's shape: cut at a 32 KiB boundary
        (b"<xbrl><a x='1", "truncated"),  # SUDARCOLOR's: inside a tag
        (b"<xbrl>\xc3", "truncated"),
        (b"<xbrl><![CDATA[x", "truncated"),
        (GOOD, None),
        (b"<xbrl></a>", None),  # malformed, but whole: not the archive cutting it short
        (b"<html><body>no</body></html>", None),  # whole, and refused for its content later
    ],
)
def test_payload_defect_names_only_empty_and_cut_off_bodies(
    body: bytes, defect: str | None
) -> None:
    assert parser.payload_defect(body) == defect


def test_a_capture_name_is_the_original_stamped_in_ist() -> None:
    at = datetime(2026, 11, 1, 20, 30, 5, tzinfo=IST)
    assert fb.capture_filename(GSS_DOC, at) == GSS_DOC.replace(".xml", "_at20261101T203005.xml")
    utc = at.astimezone(ZoneInfo("UTC"))  # the same instant from a UTC clock: still named in IST
    assert fb.capture_filename("a.xml", utc) == "a_at20261101T203005.xml"
    assert fb.capture_filename("noext", at) == "noext_at20261101T203005"
    with pytest.raises(ValueError, match="tz-aware"):
        fb.capture_filename("a.xml", datetime(2026, 11, 1))


# ── the retry ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("broken", "error"),
    [(b"", "empty response body"), (GOOD[:32768], "body is not well-formed XML: unclosed token")],
)
def test_a_retry_after_an_empty_or_truncated_body_downloads_again_beside_it(
    world: _World, broken: bytes, error: str
) -> None:
    first, fetcher = world.run({GSS_URL: broken})
    assert first.filings_failed == 2 and first.filings_published == 0
    assert all(error in (world.sync.state(u).last_error or "") for u in UNITS)
    assert len(fetcher.requests) == 1  # the annual entry reused the bytes fetched minutes ago
    (original,) = world.captures()
    before = (original.read_bytes(), original.with_name(original.name + ".meta.json").read_bytes())

    world.clock.advance(timedelta(days=1))
    second, fetcher = world.run({GSS_URL: GOOD})

    assert fetcher.requests == [(GSS_URL, GSS_DOC.replace(".xml", "_at20261103T020005.xml"))]
    assert second.filings_refetched == 1 and second.filings_published == 2
    assert all(world.sync.state(u).state is SyncState.PUBLISHED for u in UNITS)
    # The old capture is untouched, byte for byte, sidecar included; the new one sits beside it.
    old, new = world.captures()
    assert old == original
    assert (old.read_bytes(), old.with_name(old.name + ".meta.json").read_bytes()) == before
    assert new.read_bytes() == GOOD


def test_a_document_broken_again_is_downloaded_once_a_run_and_each_copy_is_kept(
    world: _World,
) -> None:
    world.run({GSS_URL: b""})
    for day in (1, 2):
        world.clock.advance(timedelta(days=1))
        report, fetcher = world.run({GSS_URL: GOOD[: 1000 * day]})
        assert len(fetcher.requests) == 1, day  # not one per entry
        assert report.filings_failed == 2
    assert [p.stat().st_size for p in world.captures()] == [0, 1000, 2000]
    # A later run with the archive healed still heals it, from the newest broken copy.
    world.clock.advance(timedelta(days=1))
    report, fetcher = world.run({GSS_URL: GOOD})
    assert report.filings_published == 2 and len(fetcher.requests) == 1


def test_a_complete_document_is_never_downloaded_again(world: _World) -> None:
    """Reuse stays the rule for whole documents — including one the parser refuses."""
    world.l0.put("nse_xbrl_filing", FILED, GSS_DOC, b"<html><body>a portal page</body></html>")
    for _ in range(2):
        report, fetcher = world.run({GSS_URL: GOOD})
        assert fetcher.requests == []
        assert report.filings_failed == 2 and report.filings_refetched == 0
        world.clock.advance(timedelta(days=1))
    assert len(world.captures()) == 1


def test_a_good_stored_document_publishes_without_a_request(world: _World) -> None:
    world.l0.put("nse_xbrl_filing", FILED, GSS_DOC, GOOD)
    report, fetcher = world.run({GSS_URL: None})
    assert fetcher.requests == [] and report.filings_published == 2


def test_a_rebuild_uses_the_newest_capture_and_never_fetches(world: _World) -> None:
    world.run({GSS_URL: b""})
    world.clock.advance(timedelta(days=1))
    world.run({GSS_URL: GOOD})
    world.sync = _Sync(world.sync.rows.copy())
    for unit in UNITS:
        del world.sync.rows[(f"nse_xbrl_filing/{unit}", FILED)]
    report, fetcher = world.run({}, rebuild_from_l0=True)
    assert fetcher.requests == [] and report.filings_published == 2


# ── the zero-byte evidence ───────────────────────────────────────────────────────────────────


def test_a_404_for_a_document_the_index_lists_as_zero_bytes_records_the_statement(
    world: _World,
) -> None:
    world.l0.put(
        "nse_integrated_filing_index",
        FILED,
        INDEX_FILE.replace("p01", "p02"),
        _page({**RECORD, "xbrlFileSize": "0 Bytes"}),
    )
    unit = replace(_index_unit(), filename=INDEX_FILE.replace("p01", "p02"))
    world.sync.rows[(unit.state_source, FILED)] = _Row(SyncState.PUBLISHED)
    runner, _ = world.runner({GSS_URL: None})
    runner.run([unit])
    error = world.sync.state("IF97542").last_error
    assert error == (
        f"FetchHTTPError: nse_xbrl_filing: {GSS_URL} returned 404, which no retry would change"
        "; NSE's index lists this XBRL file as '0 Bytes'"
    )


def test_a_404_for_a_document_the_index_sizes_is_recorded_plainly(world: _World) -> None:
    world.run({GSS_URL: None})
    error = world.sync.state("IF97542").last_error or ""
    assert error.endswith("returned 404, which no retry would change")


# ── the one-time re-queue ────────────────────────────────────────────────────────────────────


@dataclass
class _Journal:
    rows: list[tuple[str, rq.RequeueOutcome, str | None]] = field(default_factory=list)

    def __call__(
        self, item: rq.RequeueItem, outcome: rq.RequeueOutcome, detail: str | None
    ) -> None:
        self.rows.append((item.unit, outcome, detail))


def _item(unit: str, action: rq.RequeueAction = rq.RequeueAction.REFETCH) -> rq.RequeueItem:
    return rq.RequeueItem("xbrl-recheck-test", action, unit, FILED, INDEX_KEY)


def _failed(world: _World, unit: str, error: str) -> None:
    world.sync.rows[(f"nse_xbrl_filing/{unit}", FILED)] = _Row(SyncState.FAILED, last_error=error)


def test_a_requeued_truncated_filing_is_downloaded_again_and_journaled(world: _World) -> None:
    world.l0.put("nse_xbrl_filing", FILED, GSS_DOC, GOOD[:32768])
    for unit in UNITS:
        _failed(world, unit, f"parse failed: {GSS_DOC}: body is not well-formed XML: ...")
    runner, fetcher = world.runner({GSS_URL: GOOD})
    journal = _Journal()
    report = fb.FundamentalsBackfillReport(index_requested=0)
    runner.requeue([_item(u) for u in UNITS], journal=journal, report=report)
    assert len(fetcher.requests) == 1  # the annual item reused the fresh download
    assert [(u, o) for u, o, _ in journal.rows] == [
        ("IF97542", rq.RequeueOutcome.PUBLISHED),
        ("IF97542-FY", rq.RequeueOutcome.PUBLISHED),
    ]


def test_a_requeue_skips_what_is_published_or_was_retried_tonight(world: _World) -> None:
    world.sync.rows[(f"nse_xbrl_filing/{UNITS[0]}", FILED)] = _Row(SyncState.PUBLISHED)
    runner, fetcher = world.runner({GSS_URL: None})
    runner.run([_index_unit()])  # tonight's window re-read retries the annual entry (404)
    journal = _Journal()
    report = fb.FundamentalsBackfillReport(index_requested=0)
    runner.requeue([_item(u) for u in UNITS] + [_item("IF1")], journal=journal, report=report)
    assert len(fetcher.requests) == 1  # the window's; the re-queue added none
    assert [(u, o) for u, o, _ in journal.rows] == [
        ("IF97542", rq.RequeueOutcome.ALREADY_PUBLISHED),
        ("IF97542-FY", rq.RequeueOutcome.ATTEMPTED_THIS_RUN),
        ("IF1", rq.RequeueOutcome.NOT_IN_INDEX),
    ]


def test_a_failed_recheck_is_journaled_failed_and_stays_failed(world: _World) -> None:
    _failed(world, "IF97542", "FetchHTTPError: nse_xbrl_filing: x returned 404, which ...")
    runner, fetcher = world.runner({GSS_URL: None})
    journal = _Journal()
    runner.requeue(
        [_item("IF97542")], journal=journal, report=fb.FundamentalsBackfillReport(index_requested=0)
    )
    assert len(fetcher.requests) == 1
    ((_, outcome, detail),) = journal.rows
    assert outcome is rq.RequeueOutcome.FAILED and detail is not None and "404" in detail
    assert world.sync.state("IF97542").state is SyncState.FAILED


def test_record_evidence_rewrites_only_the_matching_404_and_makes_no_request(
    world: _World,
) -> None:
    zero_key = f"nse_integrated_filing_index/{FILED.isoformat()}/zero.json"
    world.l0.put(
        "nse_integrated_filing_index",
        FILED,
        "zero.json",
        _page({**RECORD, "xbrlFileSize": "0 Bytes"}),
    )
    plain = f"FetchHTTPError: nse_xbrl_filing: {GSS_URL} returned 404, which no retry would change"
    _failed(world, "IF97542", plain)
    _failed(world, "IF97542-FY", "parse failed: something else")
    runner, fetcher = world.runner({GSS_URL: GOOD})
    journal = _Journal()
    evidence = rq.RequeueAction.RECORD_EVIDENCE
    items = [
        rq.RequeueItem("ev", evidence, "IF97542", FILED, zero_key),
        rq.RequeueItem("ev", evidence, "IF97542-FY", FILED, zero_key),
        rq.RequeueItem("ev2", evidence, "IF97542", FILED, INDEX_KEY),  # index states 57.10 KB
    ]
    runner.requeue(items, journal=journal, report=fb.FundamentalsBackfillReport(index_requested=0))
    assert fetcher.requests == []
    assert [o for _, o, _ in journal.rows] == [
        rq.RequeueOutcome.EVIDENCE_RECORDED,
        rq.RequeueOutcome.NO_EVIDENCE,
        rq.RequeueOutcome.NO_EVIDENCE,
    ]
    row = world.sync.state("IF97542")
    assert row.state is SyncState.FAILED and row.retryable
    assert row.last_error == plain + "; NSE's index lists this XBRL file as '0 Bytes'"
    assert world.sync.state("IF97542-FY").last_error == "parse failed: something else"


class _Conn:
    def __init__(self, consumed: list[tuple[str, str, date]]) -> None:
        self.consumed = consumed
        self.queries = 0

    def execute(self, sql: str, params: object = ()) -> Any:
        self.queries += 1
        rows = self.consumed

        class _Result:
            def fetchall(self) -> list[tuple[str, str, date]]:
                return rows

        return _Result()


def _register(tmp_path: Path, not_before: str) -> Path:
    path = tmp_path / "requeue.yaml"
    path.write_text(
        "version: 1\nrequeues:\n"
        f"  - id: once\n    action: refetch\n    not_before: {not_before}\n    basis: test\n"
        "    filings:\n"
        f"      - {{unit: IF97542, filing_date: 2025-05-31, index: {INDEX_KEY}}}\n"
        f"      - {{unit: IF97542-FY, filing_date: 2025-05-31, index: {INDEX_KEY}}}\n"
    )
    return path


def test_an_item_is_owed_from_not_before_until_it_is_journaled(tmp_path: Path) -> None:
    path = _register(tmp_path, "2026-11-02")
    early = _Conn([])
    assert rq.pending_items(cast(Any, early), date(2026, 11, 1), path=path) == ()
    assert early.queries == 0  # nothing due: no query at all
    due = rq.pending_items(cast(Any, _Conn([])), date(2026, 11, 2), path=path)
    assert [i.unit for i in due] == list(UNITS)
    half = _Conn([("once", "IF97542", FILED)])
    assert [i.unit for i in rq.pending_items(cast(Any, half), date(2026, 12, 1), path=path)] == [
        "IF97542-FY"
    ]


def test_a_register_naming_a_filing_twice_or_another_index_is_refused(tmp_path: Path) -> None:
    path = _register(tmp_path, "2026-11-02")
    path.write_text(path.read_text().replace("IF97542-FY", "IF97542"))
    with pytest.raises(rq.RequeueError, match="twice"):
        rq.load_requeues(path)
    path = _register(tmp_path, "2026-11-02")
    path.write_text(path.read_text().replace("nse_integrated_filing_index/", "nse_bhavcopy/"))
    with pytest.raises(rq.RequeueError, match="nse_integrated_filing_index"):
        rq.load_requeues(path)


def test_the_checked_in_register_is_the_owner_approved_bounded_set() -> None:
    items = rq.load_requeues()
    refetch = {i.unit for i in items if i.action is rq.RequeueAction.REFETCH}
    evidence = {i.unit for i in items if i.action is rq.RequeueAction.RECORD_EVIDENCE}
    # 7 other 404s + 5 empty/truncated bodies; 22 zero-byte 404s recorded before the statement.
    assert len(refetch) == 12 and len(evidence) == 22 and not refetch & evidence
    assert {"IF121347", "IF202489", "IF205418", "IF205837", "IF206642", "IF108019"} <= refetch
    # No July-outage filing (D32) is re-queued: those are accepted, not re-checked.
    assert not any(date(2025, 7, 3) <= i.filing_date <= date(2025, 7, 4) for i in items)
    assert all(i.index_ref[0] == "nse_integrated_filing_index" for i in items)


def test_the_requeue_bound_stays_under_a_campaign() -> None:
    assert rq.MAX_TOTAL_FILINGS < 200  # B1: >200 requests to one source is a human go
