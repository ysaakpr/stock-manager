"""M18.2: the console's Data page and ISIN lookup — offline, over one in-memory fixture.

One stub connection holds `sync_state`, the per-source aggregate, `quality_flag`, `archive_bundle`
and the D2/D3 identity rows; it is served to the real app through the same dependency overrides,
so the JSON `/status/*` and the HTML `/console/data` are asked about the *same* rows. The
acceptance criteria, one block each:

1. for a fixture date the sync board shows exactly the states `/status/sync` returns (parity);
2. the gaps and quality panels match `/status/gaps` and `/status/quality` over the same fixture;
3. an ISIN lookup shows master, symbol history, lineage and corporate actions with factors; a
   symbol search lists candidate ISINs and never reads data by symbol;
4. the M18.1 shared rules hold here too: read-only, no external URL, no secret, IST.

The real SQL is exercised against Postgres in `tests/integration/test_console_data.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.status.api import app, clock_source, db_connection
from dataplatform.status.api import settings_source as api_settings_source
from dataplatform.status.console import READ_METHODS, router
from dataplatform.status.console.data_render import Rag, source_health, sync_rag
from dataplatform.status.models import SourceStatusOut
from dataplatform.status.queries_data import (
    is_isin,
    latest_session,
    sort_sources_worst_first,
)
from dataplatform.status.sync_state import SyncState

NOW: Final = datetime(2026, 10, 10, 20, 0, tzinfo=IST)  # a Saturday
FRIDAY: Final = date(2026, 10, 9)  # the latest session before NOW
AT: Final = datetime(2026, 10, 9, 18, 30, tzinfo=IST)

#: Every planted secret carries this marker, so "no secret leaked" is one substring check.
PLANT: Final = "PLANTEDdd"
#: Test fakes, assembled from fragments so the secret scanner does not read them as credentials.
_USERINFO: Final = ":".join(("trading", f"{PLANT}pw1"))
_PW: Final = "=".join(("password", f"{PLANT}pw2"))
PLANTED_ERROR: Final = f"connect postgresql://{_USERINFO}@db:5432/trading failed; {_PW}"

ALPHA: Final = "alpha_feed"
BETA: Final = "beta_feed"
GAMMA: Final = "gamma_feed"
SCREENER: Final = "screener_company_fundamentals"  # DECLINED in the checked-in register

SURVIVOR: Final = "INE047A01021"
RETIRED: Final = "INE047A01013"
OLD_ACME: Final = "INE000A01016"
NEW_ACME: Final = "INE999Z01011"


# ── the fixture ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Sync:
    source: str
    day: date
    state: str
    attempts: int = 1
    retryable: bool = True
    error: str | None = None
    unit: str = ""
    updated_at: datetime = AT


def _sync_rows() -> list[Sync]:
    d = date
    return [
        Sync(ALPHA, d(2026, 10, 5), "PUBLISHED"),
        Sync(ALPHA, d(2026, 10, 6), "PUBLISHED"),
        Sync(ALPHA, d(2026, 10, 7), "FAILED", attempts=3, error=PLANTED_ERROR),
        Sync(ALPHA, d(2026, 10, 8), "NORMALIZED"),
        Sync(ALPHA, FRIDAY, "PUBLISHED"),  # no L1 partition: PUBLISHED but the lake lost it
        Sync(BETA, d(2026, 10, 5), "GAP"),  # a GAP on a trading day: a real miss
        Sync(BETA, FRIDAY, "PENDING"),
        Sync(GAMMA, FRIDAY, "FAILED", attempts=2, error="HTTP 503 from the exchange"),
        # Stored in UTC, as Postgres hands timestamptz back to a UTC session: 13:00Z = 18:30 IST.
        Sync(SCREENER, FRIDAY, "VALIDATED", updated_at=datetime(2026, 10, 9, 13, 0, tzinfo=UTC)),
    ]


def _source_aggregate() -> list[tuple[Any, ...]]:
    """`_SOURCE_STATUS_SQL`'s rows: source, last_success_date, last_success_at, latest_date,
    pending, fetched, validated, normalized, published, failed, gap, streak, last failure
    date/error/retryable."""
    return [
        (ALPHA, FRIDAY, AT, FRIDAY, 0, 0, 0, 1, 3, 1, 0, 0, date(2026, 10, 7), PLANTED_ERROR, True),
        (BETA, None, None, FRIDAY, 1, 0, 0, 0, 0, 0, 1, 0, None, None, None),
        (GAMMA, date(2026, 9, 30), AT - timedelta(days=9), FRIDAY,
         0, 0, 0, 0, 5, 3, 0, 3, FRIDAY, "HTTP 503 from the exchange", True),
        (SCREENER, date(2026, 8, 7), AT - timedelta(days=63), FRIDAY,
         0, 0, 1, 0, 1, 0, 0, 0, None, None, None),
    ]  # fmt: skip


#: id, logical_date, check_name, severity, isin, source, observed, threshold, detail, raised_at
FLAGS: Final = [
    (7, FRIDAY, "price_jump", "ERROR", SURVIVOR, ALPHA, Decimal("0.4200"), Decimal("0.2"),
     {"note": f"jump vs prior close; {_PW}"}, NOW - timedelta(hours=2)),
    (6, FRIDAY, "volume_outlier", "WARN", None, ALPHA, Decimal("12.5"), Decimal("5"),
     {}, NOW - timedelta(hours=3)),
    (5, date(2026, 10, 2), "price_jump", "ERROR", NEW_ACME, ALPHA, None, None,
     {"n": 1}, NOW - timedelta(days=8)),
]  # fmt: skip

#: logical_date, schema_version, bundle_path, manifest_sha256, file_count, total_bytes, published
ARCHIVES: Final = [
    (FRIDAY, "1", "archives/2026/10/09", "ab" * 32, 4, 3 * 1024 * 1024, AT + timedelta(hours=1)),
    (date(2026, 10, 8), "1", "archives/2026/10/08", "cd" * 32, 4, 2048, AT - timedelta(hours=23)),
]

SECURITIES: Final = {
    SURVIVOR: (SURVIVOR, "GRASIM INDUSTRIES LTD", "NSE", "ACTIVE", Decimal("2.000000"),
               date(2016, 10, 7), None, None, AT),
    OLD_ACME: (OLD_ACME, "ACME OLD LTD", "NSE", "DELISTED", Decimal("10"), date(2010, 1, 4),
               date(2015, 3, 31), None, AT),
    NEW_ACME: (NEW_ACME, "ACME NEW LTD", "NSE", "ACTIVE", Decimal("1"), date(2018, 6, 1), None,
               None, AT),
}  # fmt: skip

#: isin → (exchange, symbol, series, valid_from, valid_to, source)
SYMBOLS: Final = [
    (SURVIVOR, "NSE", "GRASIM", "EQ", date(2016, 10, 7), None, "nse_equity_l"),
    (SURVIVOR, "BSE", "GRASIM", None, date(2016, 10, 7), None, "bse_scrip"),
    (OLD_ACME, "NSE", "ACME", "EQ", date(2010, 1, 4), date(2015, 3, 31), "nse_equity_l"),
    (NEW_ACME, "NSE", "ACME", "EQ", date(2018, 6, 1), None, "nse_equity_l"),
]

#: predecessor, successor, effective, detected_by, confidence, gap, symbol, corroborating
LINEAGE: Final = [
    (RETIRED, SURVIVOR, date(2016, 10, 7), "L1_CONTIGUITY", "CORROBORATED", 0, "GRASIM", "SPLIT"),
]

#: isin → id, ex_date, type, terms, dividend, record, knowable, source, reconciled, raw, filed_vs
ACTIONS: Final = {
    SURVIVOR: [
        (11, date(2024, 8, 2), "DIVIDEND", {}, Decimal("10.000000"), date(2024, 8, 2),
         date(2024, 7, 20), "nse_ca", True, "DIVIDEND - RS 10 PER SHARE", None),
        (10, date(2016, 10, 7), "SPLIT", {"from": 10, "to": 2}, None, date(2016, 10, 7),
         date(2016, 9, 1), "nse_ca", True, f"FV SPLIT RS10 TO RS2 {_PW}", RETIRED),
    ],
}  # fmt: skip

#: isin → ex_date, price, qty, cum_price, cum_qty, ca_id, structural_break, computed_at
FACTORS: Final = {
    SURVIVOR: [
        (date(2024, 8, 2), Decimal("0.995000000000000"), Decimal("1"), Decimal("0.995"),
         Decimal("1"), 11, False, AT),
        (date(2016, 10, 7), Decimal("0.200000000000000"), Decimal("5"), Decimal("0.199"),
         Decimal("5"), 10, False, AT),
    ],
}  # fmt: skip

INVALIDATIONS: Final = {SURVIVOR: [(3, "corporate action recompute", None, AT)]}


class _Result:
    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


@dataclass
class StubConnection:
    """Answers the Data page's queries from the rows above, and records every one it is asked.

    Matched on what each query selects; anything else fails loudly, so a new query on the page
    cannot be silently answered with nothing.
    """

    sync: list[Sync] = field(default_factory=_sync_rows)
    fail_on: str | None = None
    seen: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def _sync_tuple(self, row: Sync) -> tuple[Any, ...]:
        first = row.updated_at - timedelta(minutes=5)
        return (row.source, row.unit, row.day, row.state, row.attempts, row.retryable, row.error,
                None, None, first, row.updated_at)  # fmt: skip

    def rollback(self) -> None:
        pass

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Result:
        text = " ".join(sql.split())
        self.seen.append((text, tuple(params)))
        if self.fail_on is not None and self.fail_on in text:
            raise RuntimeError(f"relation is unreadable: {PLANTED_ERROR}")
        return _Result(self._answer(text, tuple(params)))

    def _answer(self, text: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        ordered = sorted(self.sync, key=lambda r: (r.day, r.source, r.unit))
        if "FROM sync_state WHERE logical_date = %s ORDER BY source, unit" in text:
            (day,) = params
            return [self._sync_tuple(r) for r in sorted(ordered, key=lambda r: r.source)
                    if r.day == day]  # fmt: skip
        if text == "SELECT DISTINCT source FROM sync_state ORDER BY source":
            return [(s,) for s in sorted({r.source for r in self.sync})]
        if "FROM sync_state WHERE logical_date BETWEEN %s AND %s" in text:
            start, end, *extra = params
            wanted = set(extra[0]) if extra else None
            units_only = "unit <> ''" in text
            return [
                self._sync_tuple(r)
                for r in ordered
                if start <= r.day <= end
                and (wanted is None or r.source in wanted)
                and (not units_only or r.unit)
            ]
        if text.startswith("WITH agg AS"):
            return _source_aggregate()
        if "FROM quality_flag WHERE NOT resolved GROUP BY severity" in text:
            counts: dict[str, int] = {}
            for flag in FLAGS:
                counts[flag[3]] = counts.get(flag[3], 0) + 1
            return sorted(counts.items())
        if "FROM quality_flag WHERE NOT resolved GROUP BY check_name, severity" in text:
            midnight, week_ago = params
            groups: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
            for flag in FLAGS:
                groups.setdefault((flag[2], flag[3]), []).append(flag)
            return [
                (check, sev, len(rows), min(r[1] for r in rows), max(r[1] for r in rows),
                 sum(r[9] >= midnight for r in rows), sum(r[9] >= week_ago for r in rows))
                for (check, sev), rows in sorted(groups.items(), key=lambda g: (-len(g[1]), g[0]))
            ]  # fmt: skip
        if "FROM quality_flag WHERE NOT resolved ORDER BY raised_at DESC" in text:
            (limit,) = params
            return sorted(FLAGS, key=lambda f: (f[9], f[0]), reverse=True)[:limit]
        if "FROM quality_flag WHERE logical_date = %s AND severity = 'ERROR'" in text:
            day, sources = params
            return [(sum(1 for f in FLAGS if f[1] == day and f[3] == "ERROR"
                         and (f[5] is None or f[5] in sources)),)]  # fmt: skip
        if text == "SELECT count(*) FROM archive_bundle":
            return [(len(ARCHIVES),)]
        if "FROM archive_bundle ORDER BY logical_date DESC" in text:
            (limit,) = params
            return sorted(ARCHIVES, key=lambda a: a[0], reverse=True)[:limit]
        if "FROM security_master WHERE isin = %s" in text:
            (isin,) = params
            return [SECURITIES[isin]] if isin in SECURITIES else []
        if "FROM symbol_history WHERE isin = %s" in text:
            (isin,) = params
            return [row[1:] for row in SYMBOLS if row[0] == isin]
        if text.startswith("WITH RECURSIVE back"):
            isin, again = params
            assert isin == again
            return _chain(isin)
        if "FROM corporate_actions WHERE isin = %s" in text:
            return list(ACTIONS.get(params[0], []))
        if "FROM adjustment_factors WHERE isin = %s" in text:
            return list(FACTORS.get(params[0], []))
        if "FROM l2_invalidation WHERE isin = %s AND NOT resolved" in text:
            return list(INVALIDATIONS.get(params[0], []))
        if "WHERE upper(h.symbol) = upper(%s)" in text:
            symbol, limit = params
            hits = [
                (row[0], SECURITIES[row[0]][1], SECURITIES[row[0]][3], *row[1:6])
                for row in SYMBOLS
                if row[2].upper() == symbol.upper()
            ]
            return sorted(hits, key=lambda h: (h[6], h[0]), reverse=True)[:limit]
        raise AssertionError(f"unexpected query against the stub connection: {text}")


def _chain(isin: str) -> list[tuple[Any, ...]]:
    """The linear chain through `isin`, walked both ways like the recursive CTE."""
    found: set[tuple[Any, ...]] = set()
    frontier = {isin}
    while frontier:
        nxt = set()
        for edge in LINEAGE:
            if (edge[1] in frontier or edge[0] in frontier) and edge not in found:
                found.add(edge)
                nxt |= {edge[0], edge[1]}
        frontier = nxt - frontier
    return sorted(found, key=lambda e: (e[2], e[0]))


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """An L1 lake holding alpha_feed's partitions for 10-05 and 10-06, and not for Friday."""
    root = tmp_path / "lake"
    for day in ("2026-10-05", "2026-10-06"):
        partition = root / "L1" / ALPHA / f"date={day}"
        partition.mkdir(parents=True)
        (partition / "part.parquet").write_bytes(b"PAR1-fixture")
    return root


@pytest.fixture
def settings(lake: Path, tmp_path: Path) -> Settings:
    return Settings(data_root=lake, backup_root=tmp_path / "backups")


def _client(settings: Settings, conn: StubConnection, now: datetime = NOW) -> TestClient:
    app.dependency_overrides[api_settings_source] = lambda: settings
    app.dependency_overrides[clock_source] = lambda: FrozenClock(now)
    app.dependency_overrides[db_connection] = lambda: conn
    return TestClient(app)


@pytest.fixture
def conn() -> StubConnection:
    return StubConnection()


@pytest.fixture
def client(settings: Settings, conn: StubConnection) -> Iterator[TestClient]:
    with _client(settings, conn) as live:
        yield live
    app.dependency_overrides.clear()


def _attrs(html: str, name: str, value: str = "data-state") -> dict[str, str]:
    return dict(re.findall(rf'<tr {name}="([^"]+)" {value}="([^"]+)"', html))


def _panel(html: str, panel: str) -> str:
    start = html.index(f'<div class="panel" id="{panel}"')
    end = html.find('<div class="panel" id=', start + 1)
    return html[start:] if end < 0 else html[start:end]


DATA_PATHS: Final = (
    "/console/data",
    f"/console/data?date={FRIDAY.isoformat()}&from=2026-10-01&to=2026-10-09",
    f"/console/data/isin?q={SURVIVOR}",
    f"/console/data/isin?q={RETIRED}",
    "/console/data/isin?q=acme",
    "/console/data/isin",
)


# ── 1. the sync board is /status/sync ──────────────────────────────────────────────────────


def test_the_default_date_is_the_latest_trading_session(client: TestClient) -> None:
    assert latest_session(trading_calendar(), NOW.date()) == FRIDAY
    html = client.get("/console/data").text
    assert f'id="sync" data-date="{FRIDAY.isoformat()}"' in html
    assert "(the latest trading session, by default)" in html
    assert "SESSION, data expected" in html


def test_the_sync_board_shows_exactly_the_states_status_sync_returns(client: TestClient) -> None:
    for day in (FRIDAY, date(2026, 10, 5), date(2026, 10, 7), date(2026, 10, 4)):
        payload = client.get("/status/sync", params={"date": day.isoformat()}).json()
        expected = {row["source"]: row["state"] for row in payload["rows"]}
        page = client.get("/console/data", params={"date": day.isoformat()}).text
        assert _attrs(_panel(page, "sync"), "data-sync-source") == expected, day
    friday = _attrs(client.get("/console/data").text, "data-sync-source")
    assert friday == {ALPHA: "PUBLISHED", BETA: "PENDING", GAMMA: "FAILED", SCREENER: "VALIDATED"}


def test_a_date_with_no_rows_says_so(client: TestClient) -> None:
    html = client.get("/console/data", params={"date": "2026-10-04"}).text
    assert "no sync_state row for 2026-10-04" in html
    assert "WEEKEND, no data owed" in html


def test_red_amber_green_by_state(client: TestClient) -> None:
    rag = _attrs(client.get("/console/data").text, "data-sync-source", "data-state")
    assert rag  # the rows exist; now their colours
    colours = dict(
        re.findall(r'<tr data-sync-source="([^"]+)" data-state="[^"]+" data-rag="([^"]+)"',
                   client.get("/console/data").text)
    )  # fmt: skip
    assert colours == {ALPHA: "green", BETA: "amber", GAMMA: "red", SCREENER: "amber"}


def test_a_gap_is_green_only_on_a_day_the_exchange_was_shut() -> None:
    """Inverted check: a GAP filed on a trading day is a miss, never green."""
    assert sync_rag(SyncState.GAP, expects_data=False) == Rag.GREEN
    assert sync_rag(SyncState.GAP, expects_data=True) == Rag.RED
    assert sync_rag(SyncState.GAP, expects_data=None) == Rag.AMBER
    assert sync_rag(SyncState.FAILED, expects_data=False) == Rag.RED
    assert sync_rag(SyncState.PUBLISHED, expects_data=True) == Rag.GREEN


def test_a_malformed_date_is_a_400_page_and_an_empty_one_is_the_default(
    client: TestClient,
) -> None:
    bad = client.get("/console/data", params={"date": "2026-13-45"})
    assert bad.status_code == 400
    assert "is not a date" in bad.text
    empty = client.get("/console/data", params={"date": "", "from": "", "to": ""})
    assert empty.status_code == 200
    assert f'data-date="{FRIDAY.isoformat()}"' in empty.text


# ── sources: worst first ───────────────────────────────────────────────────────────────────


def test_sources_are_status_sources_worst_first(client: TestClient) -> None:
    payload = client.get("/status/sources").json()
    healthy = {line["source"]: line["healthy"] for line in payload["sources"]}
    html = _panel(client.get("/console/data").text, "sources")
    page = dict(re.findall(r'<tr data-source="([^"]+)" data-health="[^"]+" data-healthy="(\w+)"',
                           html))  # fmt: skip
    assert page == {source: str(ok).lower() for source, ok in healthy.items()}
    order = re.findall(r'<tr data-source="([^"]+)"', html)
    assert order[0] == GAMMA  # the failure streak
    assert order.index(SCREENER) > order.index(BETA)  # declined is not red
    verdicts = _attrs(html, "data-source", "data-health")
    assert verdicts[GAMMA] == "FAILING"
    assert verdicts[BETA] == "NO_SUCCESS"
    assert verdicts[SCREENER] == "DECLINED"
    assert "2026-10-09 18:30:00 IST" in html  # alpha's last success


def test_sort_worst_first_puts_unhealthy_before_healthy() -> None:
    """Inverted check on the ordering: flip it and a failing source sinks below a healthy one."""

    def line(source: str, *, streak: int, lag: int | None, success: bool) -> SourceStatusOut:
        return SourceStatusOut(
            source=source, last_success_date=FRIDAY if success else None, last_success_at=None,
            latest_date=FRIDAY, lag_days=0, lag_sessions=lag, failure_streak=streak,
            last_failure_date=None, last_error=None, last_failure_retryable=None,
            max_lag_sessions=None, overdue=False, healthy=success and streak == 0,
            declined=None, counts={},
        )  # fmt: skip

    ordered = sort_sources_worst_first(
        [line("ok", streak=0, lag=0, success=True), line("bad", streak=4, lag=2, success=True),
         line("never", streak=0, lag=None, success=False)]
    )  # fmt: skip
    assert [x.source for x in ordered] == ["bad", "never", "ok"]
    assert source_health(ordered[0])[0] == "FAILING"
    assert source_health(ordered[-1]) == ("OK", Rag.GREEN)


# ── 2. gaps and quality match their endpoints ──────────────────────────────────────────────


def test_the_gaps_panel_matches_status_gaps_over_the_same_range(client: TestClient) -> None:
    start, end = "2026-10-03", "2026-10-09"
    payload = client.get("/status/gaps", params={"from": start, "to": end}).json()
    expected = {f"{e['source']}|{e['date']}": e["reason"] for e in payload["unexplained"]}
    assert expected  # the fixture has misses, of several kinds
    assert {"FAILED", "NEVER_ATTEMPTED"} <= set(expected.values())
    html = client.get("/console/data", params={"date": end, "from": start, "to": end}).text
    gaps = _panel(html, "gaps")
    assert _attrs(gaps, "data-gap", "data-reason") == expected
    assert f"{payload['unexplained_total']} unexplained" in gaps
    assert f"{payload['pairs_examined']} pairs examined" in gaps
    assert f"{payload['complete']} complete" in gaps
    # Grouped by source: each source's heading once, its rows under it.
    headings = re.findall(r'<h3 class="mono" data-gap-source="([^"]+)"', gaps)
    assert headings == sorted({key.split("|")[0] for key in expected})
    assert gaps.index(f'data-gap-source="{ALPHA}"') < gaps.index(f'data-gap="{ALPHA}|2026-10-07"')
    assert gaps.index(f'data-gap-source="{BETA}"') < gaps.index(f'data-gap="{BETA}|2026-10-06"')


def test_the_default_gaps_range_is_the_week_ending_on_the_board_date(client: TestClient) -> None:
    gaps = _panel(client.get("/console/data").text, "gaps")
    assert 'data-from="2026-10-03" data-to="2026-10-09"' in gaps


def test_an_inverted_gaps_range_is_reported_in_the_panel(client: TestClient) -> None:
    response = client.get("/console/data", params={"from": "2026-10-09", "to": "2026-10-01"})
    assert response.status_code == 200
    gaps = _panel(response.text, "gaps")
    assert 'data-unreadable="true"' in gaps
    assert "is after to=2026-10-01" in gaps
    assert (
        client.get("/status/gaps", params={"from": "2026-10-09", "to": "2026-10-01"}).status_code
        == 400
    )


def test_the_quality_panel_matches_status_quality(client: TestClient) -> None:
    payload = client.get("/status/quality").json()
    html = _panel(client.get("/console/data").text, "quality")
    flags = _attrs(html, "data-flag", "data-severity")
    assert flags == {str(f["id"]): f["severity"] for f in payload["flags"]}
    assert list(flags) == [str(f["id"]) for f in payload["flags"]]  # newest first, same order
    assert f'data-open="{payload["open_total"]}"' in html
    for count in payload["counts"]:
        assert f"{count['severity']} {count['count']}" in html
    checks = re.findall(r'<tr data-check="([^"]+)"', html)
    assert checks == [c["check_name"] for c in payload["by_check"]]
    assert f'href="/console/data/isin?q={SURVIVOR}"' in html  # a flag's ISIN links to the lookup
    assert "0.42" in html and "2026-10-10 18:00:00 IST" in html


def test_a_panel_whose_read_fails_says_so_and_the_rest_still_render(
    settings: Settings,
) -> None:
    conn = StubConnection(fail_on="FROM quality_flag WHERE NOT resolved GROUP BY severity")
    with _client(settings, conn) as live:
        response = live.get("/console/data")
    app.dependency_overrides.clear()
    assert response.status_code == 200
    html = response.text
    assert 'id="quality" data-unreadable="true"' in html
    assert "unreadable: " in html and "RuntimeError: relation is unreadable" in html
    assert PLANT not in html
    assert 'data-unreadable="true"' not in _panel(html, "sync")
    assert _attrs(html, "data-sync-source")


# ── quarantine and archives ────────────────────────────────────────────────────────────────


def test_quarantine_with_no_partitions_says_nothing_was_looked_at(client: TestClient) -> None:
    payload = client.get("/status/quarantine").json()
    html = _panel(client.get("/console/data").text, "quarantine")
    assert payload["partitions_read"] == 0
    assert f'data-rows="{payload["rows"]}"' in html
    assert "nothing was looked at, not nothing found" in html
    assert f"Quarantine {payload['from_date']} … {payload['to_date']}" in html


def test_archives_list_size_and_date_linking_to_the_endpoint(client: TestClient) -> None:
    html = _panel(client.get("/console/data").text, "archives")
    assert re.findall(r'<tr data-archive="([^"]+)"', html) == ["2026-10-09", "2026-10-08"]
    assert '<a href="/archives?date=2026-10-09">2026-10-09</a>' in html
    assert "3.0 MiB" in html and "2.0 KiB" in html
    assert "2026-10-09 19:30:00 IST" in html
    assert "newest 2 of 2" in html


# ── 3. ISIN lookup; a symbol lists candidates and never joins ──────────────────────────────


def test_an_isin_lookup_shows_master_symbols_lineage_actions_and_factors(
    client: TestClient, conn: StubConnection
) -> None:
    html = client.get("/console/data/isin", params={"q": SURVIVOR.lower()}).text
    assert f'id="isin" data-isin="{SURVIVOR}"' in html
    master = _panel(html, "security")
    assert "GRASIM INDUSTRIES LTD" in master and "state-ACTIVE" in master
    assert ">2</td>" in master  # face value 2.000000 rendered as the Decimal it is
    assert re.findall(r'<tr data-symbol="([^"]+)"', _panel(html, "symbols")) == ["GRASIM"] * 2
    lineage = _panel(html, "lineage")
    assert f'data-lineage="{RETIRED}|{SURVIVOR}"' in lineage
    assert "CORROBORATED" in lineage and "SPLIT" in lineage
    actions = _panel(html, "actions")
    assert re.findall(r'<tr data-ca="(\d+)" data-type="([A-Z_]+)"', actions) == [
        ("11", "DIVIDEND"), ("10", "SPLIT"),
    ]  # fmt: skip
    assert "filed against" in actions and RETIRED in actions
    assert "0.2</td>" in actions and "0.199</td>" in actions  # the split's own factors
    assert "10</td>" in actions  # the dividend, a Decimal
    factors = _panel(html, "factors")
    assert re.findall(r'<tr data-factor="([^"]+)"', factors) == ["2024-08-02", "2016-10-07"]
    assert "0.995" in factors
    pending = _panel(html, "invalidations")
    assert 'data-invalidation="3"' in pending and "whole series" in pending
    # Every query was keyed on the ISIN, and on nothing else.
    lookups = [params for _, params in conn.seen]
    assert lookups and all(set(params) == {SURVIVOR} for params in lookups)


def test_a_retired_isin_is_not_in_the_master_but_its_lineage_shows(client: TestClient) -> None:
    html = client.get("/console/data/isin", params={"q": RETIRED}).text
    assert "not in security_master" in html
    assert f'data-lineage="{RETIRED}|{SURVIVOR}"' in _panel(html, "lineage")
    assert f'href="/console/data/isin?q={SURVIVOR}"' in html


def test_a_symbol_search_lists_candidate_isins_and_reads_nothing_by_symbol(
    client: TestClient, conn: StubConnection
) -> None:
    html = client.get("/console/data/isin", params={"q": "acme"}).text
    panel = _panel(html, "candidates")
    assert re.findall(r'<tr data-candidate="([^"]+)"', panel) == [NEW_ACME, OLD_ACME]
    assert 'data-isins="2"' in panel
    assert f'href="/console/data/isin?q={NEW_ACME}"' in panel
    assert f'href="/console/data/isin?q={OLD_ACME}"' in panel
    assert "ACME NEW LTD" in panel and "state-DELISTED" in panel
    # The symbol reached exactly one query — the candidate list over symbol_history — and no
    # action, factor, invalidation or price table was read at all.
    assert len(conn.seen) == 1
    sql, params = conn.seen[0]
    assert "FROM symbol_history h" in sql and params[0] == "acme"
    for table in ("corporate_actions", "adjustment_factors", "l2_invalidation", "prices", "L1"):
        assert table not in sql
    for panel_id in ("security", "actions", "factors", "lineage"):
        assert f'id="{panel_id}"' not in html


def test_an_unknown_symbol_lists_nothing(client: TestClient) -> None:
    html = client.get("/console/data/isin", params={"q": "NOSUCH"}).text
    assert "no symbol_history window has the symbol NOSUCH" in html


def test_isin_shape() -> None:
    assert is_isin(SURVIVOR)
    assert not is_isin("GRASIM")
    assert not is_isin(SURVIVOR.lower())
    assert not is_isin(SURVIVOR + "0")


def test_the_lookup_with_no_query_is_the_form_alone(
    client: TestClient, conn: StubConnection
) -> None:
    html = client.get("/console/data/isin").text
    assert '<input type="text" name="q"' in html
    assert conn.seen == []


# ── 4. the M18.1 shared rules, on these pages ──────────────────────────────────────────────


def test_the_data_routes_are_registered_on_the_console_router_read_only() -> None:
    paths: dict[str, set[str]] = {}
    for route in router.routes:
        assert isinstance(route, APIRoute)
        assert route.methods is not None
        paths[route.path] = set(route.methods)
    assert {"/console/data", "/console/data/isin"} <= set(paths)
    for path in ("/console/data", "/console/data/isin"):
        assert paths[path] <= set(READ_METHODS)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_the_data_pages_refuse_a_write(client: TestClient, method: str) -> None:
    for path in DATA_PATHS:
        assert client.request(method, path).status_code == 405, (method, path)


def test_the_nav_marks_data_built_and_current(client: TestClient) -> None:
    html = client.get("/console/data").text
    assert '<a href="/console/data" class="current">Data</a>' in html
    assert "Not built yet" not in html


def test_a_planted_credential_never_appears(client: TestClient) -> None:
    for path in DATA_PATHS:
        response = client.get(path)
        assert response.status_code == 200, path
        assert PLANT not in response.text, f"{path} leaked a planted credential"
    # The masked text is still there to diagnose from.
    html = client.get("/console/data", params={"date": "2026-10-07"}).text
    assert "postgresql://***@db:5432/trading failed" in html


_URL_ATTR: Final = re.compile(r'\b(?:src|href|hx-get|hx-post|action|integrity)="([^"]*)"')


def test_no_data_page_references_an_external_url(client: TestClient) -> None:
    for path in DATA_PATHS:
        html = client.get(path).text
        assert "http://" not in html and "https://" not in html, path
        assert "url(" not in html, path
        for value in _URL_ATTR.findall(html):
            if value.startswith("sha384-"):
                continue
            assert value.startswith("/") and not value.startswith("//"), (path, value)


_STAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?P<tail>.{0,4})")


def test_every_timestamp_is_ist_and_utc_rows_are_converted(client: TestClient) -> None:
    seen = 0
    for path in DATA_PATHS:
        for match in _STAMP.finditer(client.get(path).text):
            seen += 1
            assert match.group(0).endswith(" IST"), (path, match.group(0))
    assert seen > 20
    board = _panel(client.get("/console/data").text, "sync")
    row = board[board.index(f'data-sync-source="{SCREENER}"') :]
    assert "2026-10-09 18:30:00 IST" in row.split("</tr>")[0]
    assert "13:00:00" not in board
    # A stuck pair's gap detail quotes an isoformat() instant; it is shown in IST, labelled.
    assert "stuck at NORMALIZED since 2026-10-09 18:30:00 IST" in client.get("/console/data").text


def test_the_page_answers_at_the_injected_instant(settings: Settings, conn: StubConnection) -> None:
    monday = NOW + timedelta(days=2, hours=1)
    with _client(settings, conn, monday) as live:
        html = live.get("/console/data").text
    app.dependency_overrides.clear()
    assert "as of 2026-10-12 21:00:00 IST" in html
    assert 'data-date="2026-10-12"' in html  # Monday is itself a session
