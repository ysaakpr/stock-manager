"""M9.1 acceptance: the corporate-action backfill runner, end to end and offline.

Every acceptance criterion of the task is a test here, and every one runs offline (B8): the network
is a `RecordedTransport` scripted with the real checked-in CA fixtures, the L0 store is a temp dir,
and `sync_state` plus the `corporate_actions`/`adjustment_factors` tables are in-memory stand-ins
that speak exactly the SQL the writers issue. No socket is opened, and no Postgres is needed.

  1. `corporate_actions` holds real split/bonus/dividend rows for the window, keyed by ISIN, and the
     M2.4 recompute leaves `adjustment_factors` non-empty for the ratio-bearing names
     (`test_backfill_lands_reconciles_and_recomputes`).
  2. the runner is resumable — a second run re-fetches nothing and publishes nothing
     (`test_resume_skips_published_units`).
  3. a 403 spike parks the run with an enumerated cause and leaves later units untouched
     (`test_403_spike_parks_with_enumerated_cause`) — never a silent skip.
  4. neither feed drops a row: an unclassifiable purpose queues, an unresolvable scrip is surfaced
     (`test_unresolved_and_queued_are_surfaced`), and the plan is pure/offline
     (`test_plan_fills_both_url_shapes`, `test_dry_run_is_offline`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, cast

import pytest

from dataplatform.alerts import build_alerter
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.corpactions.taxonomy import ActionType
from dataplatform.identity.master import (
    Exchange,
    IdentityMaster,
    Listing,
    ListingStatus,
    Security,
    SymbolWindow,
)
from dataplatform.ingest import corp_actions_backfill as cab
from dataplatform.ingest.bse import corp_actions as bse_ca
from dataplatform.ingest.corp_actions import build_scrip_index
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
)
from dataplatform.ingest.nse import corp_actions as nse_ca
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncState
from dataplatform.store.db import Connection
from dataplatform.store.l0 import L0Store

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "corp_actions"
NSE_FIXTURE: Final = FIXTURES / "nse" / "2026-08-08" / "corporateActions.json"
BSE_FIXTURE: Final = FIXTURES / "bse" / "2026-08-08" / "defaultdata.json"

WARM_URL: Final = "https://www.nseindia.com/"
NOW: Final = datetime(2026, 9, 3, 18, 30, tzinfo=IST)
CLOCK: Final = FrozenClock(NOW)

FROM: Final = date(2023, 7, 1)
TO: Final = date(2024, 12, 31)

#: The universe both fixtures describe: (ISIN, NSE symbol, BSE scrip code).
UNIVERSE: Final = [
    ("INE002A01018", "RELIANCE", "500325"),
    ("INE009A01021", "INFY", "500209"),
    ("INE081A01020", "TATASTEEL", "500470"),
    ("INE075A01022", "WIPRO", "507685"),
    ("INE001A01036", "HDFC", "500010"),
]
KNOWN_SCRIPS: Final = sorted(scrip for _, _, scrip in UNIVERSE)
GHOST_SCRIP: Final = "999999"  # present in the BSE fixture, absent from the identity master


# ── in-memory identity master (offline) ────────────────────────────────────────────────────────


def _master() -> IdentityMaster:
    """A master seeded with the fixtures' universe: open NSE symbol windows + BSE scrip listings."""
    windows = [
        SymbolWindow(
            exchange=Exchange.NSE,
            symbol=symbol,
            valid_from=date(2000, 1, 1),
            valid_to=None,
            isin=isin,
            series="EQ",
            source="test",
        )
        for isin, symbol, _ in UNIVERSE
    ]
    securities = [
        Security(
            isin=isin,
            name=symbol,
            primary_exchange=Exchange.NSE,
            status=ListingStatus.ACTIVE,
            first_seen_date=date(2000, 1, 1),
        )
        for isin, symbol, _ in UNIVERSE
    ]
    listings = [
        Listing(
            isin=isin,
            exchange=Exchange.BSE,
            status=ListingStatus.ACTIVE,
            security_code=scrip,
            series="A",
        )
        for isin, _, scrip in UNIVERSE
    ]
    return IdentityMaster(windows, securities=securities, listings=listings)


# ── per-scrip BSE payloads, split from the single fixture ───────────────────────────────────────


def _bse_payloads() -> dict[str, bytes]:
    """The BSE fixture split into one JSON array per scrip — the shape the per-scrip URL returns."""
    records = json.loads(BSE_FIXTURE.read_bytes())
    by_scrip: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_scrip.setdefault(str(record["scrip_code"]), []).append(record)
    return {scrip: json.dumps(rows).encode("utf-8") for scrip, rows in by_scrip.items()}


# ── transport + fetcher wiring (offline) ─────────────────────────────────────────────────────────


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_root=tmp_path)


def _fetcher(transport: RecordedTransport, settings: Settings) -> Fetcher:
    return Fetcher(
        transport=transport,
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        alerter=build_alerter(settings, clock=CLOCK),
        clock=CLOCK,
        register=load_register(),
        settings=settings,
        sleep=lambda _seconds: None,
    )


def _ok_transport(
    units: Sequence[cab.CaFetchUnit], bse_payloads: dict[str, bytes]
) -> RecordedTransport:
    """A transport serving each unit's fixture bytes as a 200, plus the NSE session warm-up."""
    script: dict[str, RecordedResponse | list[RecordedResponse]] = {
        WARM_URL: RecordedResponse(status_code=200, body=b"", headers={"content-type": "text/html"})
    }
    for unit in units:
        if unit.exchange is Exchange.NSE:
            body = NSE_FIXTURE.read_bytes()
        else:
            assert unit.scrip_code is not None
            body = bse_payloads.get(unit.scrip_code, b"[]")
        script[unit.url] = RecordedResponse(
            status_code=200, body=body, headers={"content-type": "application/json"}
        )
    return RecordedTransport(cast("Any", script))


def _runner(
    transport: RecordedTransport,
    *,
    settings: Settings,
    conn: _FakeConn,
    sync: _FakeSync,
    master: IdentityMaster,
) -> cab.CaBackfillRunner:
    return cab.CaBackfillRunner(
        fetcher=_fetcher(transport, settings),
        l0=L0Store(clock=CLOCK, data_root=settings.data_root),
        sync=cast("Any", sync),
        conn=cast("Connection", conn),
        commit=lambda: None,
        master=master,
        scrip_index=build_scrip_index(master),
        clock=CLOCK,
    )


# ── acceptance 1: rows land, reconcile, and the factor chain is recomputed ────────────────────────


def test_backfill_lands_reconciles_and_recomputes(tmp_path: Path) -> None:
    master = _master()
    register = load_register()
    plan = cab.build_plan(FROM, TO, KNOWN_SCRIPS, register=register, chunk_months=24)
    transport = _ok_transport(plan, _bse_payloads())
    conn = _FakeConn()
    sync = _FakeSync()

    report = _runner(
        transport, settings=_settings(tmp_path), conn=conn, sync=sync, master=master
    ).run(plan)

    # The plan is 1 NSE chunk + 5 BSE scrips, all published.
    assert report.requested == 6
    assert report.published == 6
    assert report.failed == 0
    assert not report.parked

    # corporate_actions holds the real actions from both feeds, keyed by ISIN: five per exchange.
    nse_rows = conn.actions_for_source(nse_ca.SOURCE_ID)
    bse_rows = conn.actions_for_source(bse_ca.SOURCE_ID)
    assert {r["isin"] for r in nse_rows} == {isin for isin, _, _ in UNIVERSE}
    assert {r["isin"] for r in bse_rows} == {isin for isin, _, _ in UNIVERSE}
    # Real split/bonus/dividend terms, not placeholders.
    tata = next(r for r in nse_rows if r["isin"] == "INE081A01020")
    assert tata["action_type"] == ActionType.SPLIT.value
    reliance = next(r for r in nse_rows if r["isin"] == "INE002A01018")
    assert reliance["action_type"] == ActionType.BONUS.value

    # Finalize reconciles the two feeds and recomputes the factor chain.
    finalize = cab.finalize_reconcile_and_recompute(cast("Connection", conn), clock=CLOCK)
    assert finalize.reconciled == 5  # all five actions agree across NSE and BSE
    assert finalize.queued == 0

    # adjustment_factors is non-empty exactly for the ratio-bearing names (split + bonus).
    assert finalize.factor_rows > 0
    assert conn.factor_rows_for("INE081A01020"), "the split must produce a factor row"
    assert conn.factor_rows_for("INE002A01018"), "the bonus must produce a factor row"


# ── acceptance 2: resume ─────────────────────────────────────────────────────────────────────────


def test_resume_skips_published_units(tmp_path: Path) -> None:
    master = _master()
    register = load_register()
    plan = cab.build_plan(FROM, TO, KNOWN_SCRIPS, register=register, chunk_months=24)
    bse_payloads = _bse_payloads()
    settings = _settings(tmp_path)
    conn = _FakeConn()
    sync = _FakeSync()

    first = _runner(
        _ok_transport(plan, bse_payloads), settings=settings, conn=conn, sync=sync, master=master
    ).run(plan)
    assert first.published == 6

    # A second run over the same plan and checkpoint re-fetches nothing and publishes nothing.
    transport2 = _ok_transport(plan, bse_payloads)
    second = _runner(transport2, settings=settings, conn=conn, sync=sync, master=master).run(plan)
    assert second.published == 0
    assert second.skipped_published == 6
    assert transport2.requests == []  # not a single socket call, warm-up included


# ── acceptance 3: a 403 spike parks with an enumerated cause ─────────────────────────────────────


def test_403_spike_parks_with_enumerated_cause(tmp_path: Path) -> None:
    """Consecutive 403s trip the fetcher's spike hard stop; the run parks and leaves later units."""
    master = _master()
    register = load_register()
    settings = _settings(tmp_path)
    assert settings.http_forbidden_streak_limit == 3

    # BSE-only plan (no warm-up host in the way), every scrip a 403.
    bse_units = cab.build_bse_units(KNOWN_SCRIPS, register=register, anchor=FROM)
    script: dict[str, RecordedResponse | list[RecordedResponse]] = {
        unit.url: RecordedResponse(status_code=403, body=b"forbidden") for unit in bse_units
    }
    transport = RecordedTransport(cast("Any", script))
    conn = _FakeConn()
    sync = _FakeSync()

    report = _runner(transport, settings=settings, conn=conn, sync=sync, master=master).run(
        bse_units
    )

    assert report.parked
    assert report.park_reason is cab.ParkReason.FORBIDDEN_SPIKE
    assert report.park_detail is not None
    assert "FORBIDDEN_SPIKE" in report.park_detail
    # Three units attempted (the third trips the spike); the rest were never reached.
    assert report.failed == 3
    assert report.published == 0
    for unit in bse_units[3:]:
        assert sync.get(unit.state_source, unit.logical_date) is None
    # The tripping unit is recorded FAILED and non-retryable — the host is off-limits for the run.
    tripped = sync.get(bse_units[2].state_source, bse_units[2].logical_date)
    assert tripped is not None and tripped.state is SyncState.FAILED and not tripped.retryable
    # A parked run is never silent: the report renders the enumerated cause.
    rendered = cab.render_report(
        from_date=FROM, to_date=TO, universe_size=5, report=report, finalize=None
    )
    assert "PARKED" in rendered and "FORBIDDEN_SPIKE" in rendered


# ── acceptance 4: nothing is dropped; the plan is pure ───────────────────────────────────────────


def test_unresolved_and_queued_are_surfaced(tmp_path: Path) -> None:
    """The NSE AGM queues (unclassifiable), and the ghost scrip resolves to nothing — both kept."""
    master = _master()
    register = load_register()
    # Plan the ghost scrip too, so its unresolvable identity is exercised.
    plan = cab.build_plan(
        FROM, TO, [*KNOWN_SCRIPS, GHOST_SCRIP], register=register, chunk_months=24
    )
    transport = _ok_transport(plan, _bse_payloads())
    conn = _FakeConn()
    sync = _FakeSync()

    report = _runner(
        transport, settings=_settings(tmp_path), conn=conn, sync=sync, master=master
    ).run(plan)

    assert report.published == 7  # 1 NSE + 6 BSE scrips (5 known + ghost)
    assert report.queued >= 1  # the NSE "Annual General Meeting" subject
    assert report.unresolved >= 1  # the ghost scrip 999999 has no ISIN in the master


def test_plan_fills_both_url_shapes() -> None:
    register = load_register()
    # A 24-month chunk collapses the 18-month window to one NSE date-range fetch.
    plan = cab.build_plan(FROM, TO, KNOWN_SCRIPS, register=register, chunk_months=24)
    nse = [u for u in plan if u.exchange is Exchange.NSE]
    bse = [u for u in plan if u.exchange is Exchange.BSE]

    # The NSE URL carries the filled DD-MM-YYYY range (per-date feed) and no leftover placeholder.
    assert len(nse) == 1
    assert "from_date=01-07-2023" in nse[0].url
    assert "to_date=31-12-2024" in nse[0].url
    assert "{DD-MM-YYYY}" not in nse[0].url
    # A default (12-month) chunk splits the same window in two — the plan is per-date, not one shot.
    assert len(cab.build_nse_units(FROM, TO, register=register)) == 2
    # One BSE unit per scrip (per-scrip feed), each carrying its scrip code, no placeholder left.
    assert len(bse) == len(KNOWN_SCRIPS)
    assert {u.scrip_code for u in bse} == set(KNOWN_SCRIPS)
    for unit in bse:
        assert unit.scrip_code is not None and f"scripcode={unit.scrip_code}" in unit.url
        assert "{SCRIP_CD}" not in unit.url


def test_dry_run_is_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`isins_in_price_window` reads only partitions the calendar names, skipping absent ones."""
    seen: list[date] = []

    def fake_read(day: date, *, data_root: Path | None = None) -> tuple[dict[str, object], ...]:
        seen.append(day)
        if day == date(2024, 1, 1):
            raise FileNotFoundError("no partition")
        return ({"isin": "INE002A01018"}, {"isin": "INE009A01021"})

    monkeypatch.setattr(cab, "read_prices_raw", fake_read)
    from dataplatform.ingest.calendar import trading_calendar

    isins = cab.isins_in_price_window(
        trading_calendar(), date(2024, 1, 1), date(2024, 1, 5), data_root=tmp_path
    )
    assert isins == {"INE002A01018", "INE009A01021"}
    assert seen  # it did consult the calendar's expected dates


# ── in-memory stand-ins for sync_state and the CA/factor tables ─────────────────────────────────


class _SyncRow:
    """The slice of a `SyncRecord` the runner reads: its `state` and `retryable` flag."""

    def __init__(self, state: SyncState, *, retryable: bool = True) -> None:
        self.state = state
        self.retryable = retryable


class _FakeSync:
    """In-memory `SyncStateStore` stand-in: the happy-path transitions plus `mark_failed`.

    Keyed by `(source, logical_date)` exactly as the real store is, so the runner's resume check —
    "is this unit's row PUBLISHED?" — is a real round trip. It models only what the runner calls;
    an unknown call would be an `AttributeError`, which is the loud failure we want (B8).
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, date], _SyncRow] = {}

    def get(self, source: str, logical_date: date) -> _SyncRow | None:
        return self._rows.get((source, logical_date))

    def begin(self, source: str, logical_date: date) -> _SyncRow:
        row = _SyncRow(SyncState.PENDING)
        self._rows[(source, logical_date)] = row
        return row

    def _advance(self, source: str, logical_date: date, state: SyncState) -> _SyncRow:
        row = self._rows[(source, logical_date)]
        row.state = state
        return row

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> _SyncRow:
        return self._advance(source, logical_date, SyncState.FETCHED)

    def mark_validated(self, source: str, logical_date: date) -> _SyncRow:
        return self._advance(source, logical_date, SyncState.VALIDATED)

    def mark_normalized(self, source: str, logical_date: date) -> _SyncRow:
        return self._advance(source, logical_date, SyncState.NORMALIZED)

    def mark_published(self, source: str, logical_date: date) -> _SyncRow:
        return self._advance(source, logical_date, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> _SyncRow:
        row = self._rows.setdefault((source, logical_date), _SyncRow(SyncState.PENDING))
        row.state = SyncState.FAILED
        row.retryable = retryable
        return row


class _FakeCursor:
    """A cursor over a fixed result set — only `fetchone`/`fetchall`, which is all callers use."""

    def __init__(self, rows: list[tuple[Any, ...]], rowcount: int | None = None) -> None:
        self._rows = rows
        self.rowcount = len(rows) if rowcount is None else rowcount

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    """In-memory stand-in speaking exactly the SQL this pipeline issues: the `corporate_actions`
    write/read, the reconciliation marks and `quality_flag`, and the `adjustment_factors` /
    `l2_invalidation` recompute. It is not a Postgres emulator — an unrecognised statement raises,
    so a query that drifts fails loudly here. Cast to `Connection` at the call site (B8).
    """

    def __init__(self) -> None:
        self._ca: dict[tuple[str, date, str, str], dict[str, Any]] = {}
        self._flags: list[dict[str, Any]] = []
        self._factors: dict[tuple[str, date], dict[str, Any]] = {}
        self._invalidations: list[dict[str, Any]] = []
        self._next_id = 1

    # -- helpers the tests assert against -------------------------------------------------------

    def actions_for_source(self, source: str) -> list[dict[str, Any]]:
        return [row for row in self._ca.values() if row["source"] == source]

    def factor_rows_for(self, isin: str) -> list[dict[str, Any]]:
        return [v for (i, _), v in sorted(self._factors.items()) if i == isin]

    # -- the SQL seam ---------------------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> _FakeCursor:
        p = tuple(params)
        if sql.startswith("INSERT INTO corporate_actions"):
            return self._insert_ca(p)
        if sql.startswith("SELECT isin, ex_date, action_type, ratio_terms"):
            return self._select_ca(sql, p)
        if sql.startswith("UPDATE corporate_actions SET reconciled = true"):
            note, isin, ex_date, action_type, source = p
            row = self._ca.get((isin, ex_date, action_type, source))
            if row is not None:
                row["reconciled"], row["note"] = True, note
            return _FakeCursor([])
        if sql.startswith("UPDATE corporate_actions SET reconciled = false"):
            isin, ex_date, action_type, source = p
            row = self._ca.get((isin, ex_date, action_type, source))
            if row is not None:
                row["reconciled"] = False
            return _FakeCursor([])
        if "SELECT 1 FROM quality_flag" in sql:
            check_name, fingerprint = p
            hits = [
                f
                for f in self._flags
                if not f["resolved"]
                and f["check_name"] == check_name
                and f["detail"].get("fingerprint") == fingerprint
            ]
            return _FakeCursor([(1,)] if hits else [])
        if sql.startswith("UPDATE quality_flag SET resolved = true"):
            # Scoped supersede — see `persist_reconciliation`. Modelled rather than ignored so a
            # drift in that statement fails here rather than passing silently.
            assert isinstance(params, Mapping)
            keep, scope = set(params["fingerprints"]), set(params["isins"])
            closed = 0
            for flag in self._flags:
                if (
                    not flag["resolved"]
                    and flag["check_name"] == params["check_name"]
                    and flag.get("isin") in scope
                    and flag["detail"].get("fingerprint") not in keep
                ):
                    flag["resolved"] = True
                    closed += 1
            return _FakeCursor([], rowcount=closed)
        if "INSERT INTO quality_flag" in sql:
            _logical_date, check_name, _severity, _isin, _source, detail_json, _raised_at = p
            self._flags.append(
                {
                    "check_name": check_name,
                    "isin": _isin,
                    "detail": json.loads(detail_json),
                    "resolved": False,
                }
            )
            return _FakeCursor([])
        if sql.startswith("DELETE FROM adjustment_factors"):
            (isin,) = p
            for key in [k for k in self._factors if k[0] == isin]:
                del self._factors[key]
            return _FakeCursor([])
        if sql.startswith("INSERT INTO adjustment_factors"):
            (isin, ex_date, price_f, _qty_f, cum_p, _cum_q, _ca_id, structural, _computed_at) = p
            self._factors[(isin, ex_date)] = {
                "isin": isin,
                "ex_date": ex_date,
                "price_factor": price_f,
                "cum_price_factor": cum_p,
                "structural_break": structural,
            }
            return _FakeCursor([])
        if sql.startswith("SELECT 1 FROM l2_invalidation"):
            (isin,) = p
            hit = any(inv["isin"] == isin and not inv["resolved"] for inv in self._invalidations)
            return _FakeCursor([(1,)] if hit else [])
        if sql.startswith("INSERT INTO l2_invalidation"):
            (isin, _reason, _from_date, _requested_at) = p
            self._invalidations.append({"isin": isin, "resolved": False})
            return _FakeCursor([])
        raise AssertionError(f"_FakeConn does not know this SQL: {sql!r}")

    def rollback(self) -> None:  # the runner calls this between failed units
        return None

    def _insert_ca(self, p: tuple[Any, ...]) -> _FakeCursor:
        (
            isin,
            filed_against_isin,
            ex_date,
            action_type,
            ratio_terms_json,
            _dividend,
            record_date,
            announcement_date,
            knowable_date,
            source,
            source_ref,
            raw_text,
            l0_key,
            _recorded_at,
        ) = p
        key = (isin, ex_date, action_type, source)
        if key in self._ca:  # ON CONFLICT DO NOTHING
            return _FakeCursor([])
        self._ca[key] = {
            "isin": isin,
            "filed_against_isin": filed_against_isin,
            "ex_date": ex_date,
            "action_type": action_type,
            "ratio_terms": json.loads(ratio_terms_json),
            "record_date": record_date,
            "announcement_date": announcement_date,
            "knowable_date": knowable_date,
            "source": source,
            "source_ref": source_ref,
            "raw_text": raw_text,
            "l0_key": l0_key,
            "reconciled": False,
            "note": None,
        }
        row_id = self._next_id
        self._next_id += 1
        return _FakeCursor([(row_id,)])

    def _select_ca(self, sql: str, params: tuple[Any, ...]) -> _FakeCursor:
        reconciled_only = "reconciled = true" in sql
        isin_filter = params[0] if params else None
        rows: list[tuple[Any, ...]] = []
        for row in self._ca.values():
            if reconciled_only and not row["reconciled"]:
                continue
            if isin_filter is not None and row["isin"] != isin_filter:
                continue
            rows.append(
                (
                    row["isin"],
                    row["ex_date"],
                    row["action_type"],
                    row["ratio_terms"],
                    row["record_date"],
                    row["announcement_date"],
                    row["knowable_date"],
                    row["source"],
                    row["source_ref"],
                    row["raw_text"],
                    row["l0_key"],
                )
            )
        rows.sort(key=lambda r: (r[0], r[1], r[2], r[7]))
        return _FakeCursor(rows)


# ── the per-scrip endpoint, whose dates are shaped differently ──────────────────────────────────

#: A real per-scrip response, captured 2026-09-06. The frozen 2026-08-08 fixture above came from
#: the *empty*-scripcode form of the same endpoint, and the two spell `exdate` differently:
#: `2024-10-28T00:00:00` there, `20010426` here. Only the first had ever been captured, so the
#: first live per-scrip fetch refused all five scrips it was probed with.
BSE_PER_SCRIP_FIXTURE: Final = FIXTURES / "bse" / "2026-09-06" / "defaultdata_500325.json"


def test_the_per_scrip_endpoint_spells_exdate_without_separators() -> None:
    """The premise: this fixture must keep the compact shape, or the test below proves nothing."""
    records = json.loads(BSE_PER_SCRIP_FIXTURE.read_text())
    assert len(records) == 26
    assert records[0]["exdate"] == "20010426"
    assert records[0]["Ex_date"] == "26 Apr 2001"


def test_a_per_scrip_response_parses_and_both_ex_date_spellings_agree() -> None:
    """`Ex_date` and `exdate` must describe one day — the check this module was built around.

    It had never actually run: `exdate` failed to parse in this dialect, so the parser raised
    before it could compare the two. With the compact shape understood, 26 of RELIANCE's actions
    back to 2001 land, and the agreement check finally does its job on every one of them.
    """
    reliance = UNIVERSE[0][0]
    result = bse_ca.parse(
        BSE_PER_SCRIP_FIXTURE.read_bytes(),
        filename="defaultdata_500325.json",
        scrip_index=build_scrip_index(_master()),
        clock=CLOCK,
    )
    assert result.unresolved == ()
    # Every record lands. `queued` overlaps rather than partitions: a dividend whose rupee amount
    # the purpose string never states is still a real action, and also a question for a human.
    assert len(result.actions) == 26
    assert all(action.isin == reliance for action in result.actions)
    assert min(action.ex_date for action in result.actions) == date(2001, 4, 26)

    bonuses = [a for a in result.actions if a.action_type is ActionType.BONUS]
    assert len(bonuses) == 3, "RELIANCE's three 1:1 bonus issues"


#: ABB's per-scrip response, captured 2026-09-06. Record 2 (ex-date 2007-06-28) carries valid
#: dates and an empty `Purpose`. BSE publishes these; requiring the field cost the whole response,
#: which is how 34 of the first 160 scrips of the live campaign failed outright.
BSE_EMPTY_PURPOSE_FIXTURE: Final = (
    FIXTURES / "bse" / "2026-09-06" / "defaultdata_500002_EMPTY_PURPOSE.json"
)


def test_a_record_with_no_purpose_does_not_cost_the_scrip_its_other_actions() -> None:
    """One unusable row is queued for a human; the other 26 still land.

    The same stance the rest of the ingest takes — a record that cannot be *classified* is not an
    error, it is a question. Before this, an empty `Purpose` raised and the scrip contributed
    nothing at all.
    """
    records = json.loads(BSE_EMPTY_PURPOSE_FIXTURE.read_text())
    assert len(records) == 27
    assert records[2]["Purpose"] == "", "the fixture must keep the empty-purpose row"
    assert records[2]["exdate"] == "20070628"

    result = bse_ca.parse(
        BSE_EMPTY_PURPOSE_FIXTURE.read_bytes(),
        filename="defaultdata_500002.json",
        scrip_index={"500002": "INE117A01022"},
        clock=CLOCK,
    )
    assert len(result.actions) == 26
    assert any(entry.raw_text == "" for entry in result.queued), "the empty row reaches a human"
    assert all(action.isin == "INE117A01022" for action in result.actions)


# ── a parse failure must not cost the download twice ────────────────────────────────────────────


def test_a_unit_that_failed_on_a_parse_is_re_parsed_from_l0_not_re_fetched(tmp_path: Path) -> None:
    """The download and the parse are independent: the bytes are fetched once, parsed as often.

    A unit whose *parse* raised still has its payload in L0 — the fetch happened and
    `mark_fetched` recorded it. So the re-run after a parser fix must be a pure re-derivation.
    Before this, `_process` called the fetcher unconditionally and a morning of parser fixes cost
    a second campaign's worth of requests against a rate-limited host.
    """
    master = _master()
    register = load_register()
    plan = cab.build_plan(FROM, TO, KNOWN_SCRIPS, register=register, chunk_months=24)
    settings = _settings(tmp_path)
    conn = _FakeConn()
    sync = _FakeSync()

    # Serve every unit a well-formed *envelope* the parser cannot use: valid JSON, no records the
    # scrip index can resolve, and for BSE a record whose scrip code is unknown. The simplest
    # reliable refusal is a body that is not this format at all.
    def broken_transport() -> RecordedTransport:
        script: dict[str, Any] = {
            WARM_URL: RecordedResponse(
                status_code=200, body=b"", headers={"content-type": "text/html"}
            )
        }
        for unit in plan:
            script[unit.url] = RecordedResponse(
                status_code=200, body=b"{}", headers={"content-type": "application/json"}
            )
        return RecordedTransport(cast("Any", script))

    first = _runner(broken_transport(), settings=settings, conn=conn, sync=sync, master=master).run(
        plan
    )
    assert first.failed > 0, "the premise: these units must fail on the parse, not the fetch"
    assert first.l0_reused == 0
    fetched = len(plan)

    # The same plan again. Every unit is FAILED, so every one is retried — and every one of them
    # reads the bytes back off disk.
    transport2 = broken_transport()
    second = _runner(transport2, settings=settings, conn=conn, sync=sync, master=master).run(plan)
    assert second.l0_reused == fetched
    assert transport2.requests == [], "a re-parse must not touch the network"
