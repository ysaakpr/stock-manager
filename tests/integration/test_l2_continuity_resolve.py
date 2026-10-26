"""`persist_findings` with a resolve scope, against Postgres: stale flags close, others never do.

The live case this exists for: 14 `l2_continuity` ERROR flags raised at 22:21 IST on 2026-10-05,
after commit `29ce597` had curated all 14 steps, were still open on 2026-10-23 against a lake that
scanned 0 unexplained — nothing ever closed a flag a later scan stopped raising. The table below
is that shape: 14 stale step flags, the 3 unsourced WARNs a scan still raises, and flags no
`l2_continuity` scan may touch (another check's, and one about an ISIN outside a partial scan).

Runs against a scratch database created for the session and dropped afterwards, never the
developer's `trading` database; each test rolls back.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import Final

import psycopg
import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.quality.l2_continuity import (
    CHECK_NAME,
    AdjustedStep,
    L2ContinuityReport,
    StepClass,
    findings,
)
from dataplatform.quality.sentinel import (
    FindingScope,
    QualityFinding,
    finding_fingerprint,
    persist_findings,
)
from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.migrate import migrate

pytestmark = pytest.mark.integration

SCRATCH_DB = f"trading_l2_resolve_{os.getpid()}"
RAISED = FrozenClock(datetime(2026, 10, 5, 22, 21, tzinfo=IST))
LATER = FrozenClock(datetime(2026, 10, 23, 22, 54, tzinfo=IST))

#: The 14 curated steps' ISINs and sessions, as the live flags carry them.
STALE: Final = (
    ("INE018I01017", date(2016, 3, 9)),
    ("INE043K01029", date(2013, 9, 30)),
    ("INE069A01017", date(2016, 1, 20)),
    ("INE111B01023", date(2013, 8, 1)),
    ("INE247G01024", date(2013, 2, 25)),
    ("INE360C01024", date(2011, 12, 15)),
    ("INE429C01035", date(2017, 5, 25)),
    ("INE483S01020", date(2018, 9, 28)),
    ("INE510K01019", date(2014, 9, 16)),
    ("INE528G01035", date(2020, 3, 6)),
    ("INE904G01038", date(2013, 3, 21)),
    ("INE988E01036", date(2015, 6, 25)),
    ("INF789F1AHR6", date(2021, 2, 17)),
    ("INF846K01ZL0", date(2020, 7, 23)),
)
#: The three curated unsourced steps a scan still raises (EIH, Shah Alloys, JM Financial).
UNSOURCED: Final = (
    ("INE230A01023", date(2006, 9, 12)),
    ("INE640C01011", date(2006, 2, 13)),
    ("INE780C01023", date(2008, 9, 8)),
)


def _settings_for(dbname: str) -> Settings:
    return Settings(database_url=with_dbname(Settings().database_url, dbname))


@pytest.fixture(scope="session")
def scratch_settings() -> Iterator[Settings]:
    admin = _settings_for("postgres")
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        conn.close()

    migrate(_settings_for(SCRATCH_DB), clock=RAISED)
    yield _settings_for(SCRATCH_DB)

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def conn(scratch_settings: Settings) -> Iterator[Connection]:
    with connection(scratch_settings) as live:
        try:
            yield live
        finally:
            live.rollback()


def _step(isin: str, day: date) -> AdjustedStep:
    return AdjustedStep(
        isin=isin,
        exchange="NSE",
        prev_date=day.replace(day=1) if day.day > 1 else day,
        trade_date=day,
        prev_close=Decimal(100),
        close=Decimal(30),
        ratio=Decimal("0.3"),
        tr_ratio=Decimal("0.3"),
        factor_changed=False,
    )


def _report(
    unexplained: tuple[tuple[str, date], ...],
    unsourced: tuple[tuple[str, date], ...] = UNSOURCED,
    *,
    isins: frozenset[str] | None = None,
    threshold: Decimal = Decimal(2),
) -> L2ContinuityReport:
    return L2ContinuityReport(
        threshold=threshold,
        max_gap_days=5,
        partitions=3509,
        steps=(
            *((_step(i, d), StepClass.UNEXPLAINED) for i, d in unexplained),
            *((_step(i, d), StepClass.UNSOURCED) for i, d in unsourced),
        ),
        retired_partitions=(),
        isins=isins,
    )


def _other_check_flag(isin: str) -> QualityFinding:
    return QualityFinding(
        logical_date=date(2026, 10, 5),
        check_name="sentinel_move",
        severity="ERROR",
        isin=isin,
        source="prices_raw",
        fingerprint=finding_fingerprint("sentinel_move", isin, date(2026, 10, 5)),
    )


@pytest.fixture
def raised(conn: Connection) -> Connection:
    """The 2026-10-05 22:21 IST state: 14 step ERRORs, 3 unsourced WARNs, another check's flag."""
    persist_findings(conn, findings(_report(STALE), logical_date=RAISED.today()), clock=RAISED)
    persist_findings(conn, [_other_check_flag(STALE[0][0])], clock=RAISED)
    assert _open(conn) == {"ERROR": 14, "WARN": 3}
    return conn


def _open(conn: Connection, check: str = CHECK_NAME) -> dict[str, int]:
    rows = conn.execute(
        "SELECT severity, count(*) FROM quality_flag WHERE check_name = %s AND NOT resolved "
        "GROUP BY severity",
        (check,),
    ).fetchall()
    return {str(severity): int(n) for severity, n in rows}


def _rescan(conn: Connection, report: L2ContinuityReport) -> tuple[int, int, int]:
    counts = persist_findings(
        conn,
        findings(report, logical_date=LATER.today()),
        clock=LATER,
        resolve_scope=report.resolution_scope,
    )
    return counts.written, counts.skipped, counts.resolved


def test_a_full_rescan_closes_exactly_the_flags_it_no_longer_raises(raised: Connection) -> None:
    """The live recovery: 0 unexplained now, so the 14 close; the 3 WARNs still stand."""
    assert _rescan(raised, _report(())) == (0, 3, 14)
    assert _open(raised) == {"WARN": 3}
    assert _open(raised, "sentinel_move") == {"ERROR": 1}, "another check is never touched"
    raised_at = raised.execute(
        "SELECT DISTINCT raised_at FROM quality_flag WHERE check_name = %s AND NOT resolved",
        (CHECK_NAME,),
    ).fetchall()
    assert raised_at == [(RAISED.now(),)], "a flag still raised keeps its original raised_at"
    (resolution,) = raised.execute(
        "SELECT DISTINCT resolution FROM quality_flag WHERE resolved AND check_name = %s",
        (CHECK_NAME,),
    ).fetchone() or (None,)
    assert "superseded" in str(resolution)


def test_a_full_rescan_that_still_raises_a_step_keeps_its_flag(raised: Connection) -> None:
    assert _rescan(raised, _report(STALE[:1])) == (0, 4, 13)
    assert _open(raised) == {"ERROR": 1, "WARN": 3}


def test_a_partial_scan_never_closes_a_flag_outside_its_isins(raised: Connection) -> None:
    """A scan of two ISINs that finds nothing closes their two flags, and none of the other 15."""
    scope = frozenset(i for i, _ in STALE[:2])
    assert _rescan(raised, _report((), (), isins=scope)) == (0, 0, 2)
    assert _open(raised) == {"ERROR": 12, "WARN": 3}
    still_open = {
        str(r[0])
        for r in raised.execute(
            "SELECT isin FROM quality_flag WHERE check_name = %s AND NOT resolved", (CHECK_NAME,)
        ).fetchall()
    }
    assert not scope & still_open
    assert {i for i, _ in STALE[2:]} <= still_open


def test_a_partial_scan_that_still_raises_its_steps_closes_nothing(raised: Connection) -> None:
    """The inverted case: in scope but raised again, so open; out of scope, untouched."""
    scope = frozenset(i for i, _ in STALE[:2])
    assert _rescan(raised, _report(STALE[:2], (), isins=scope)) == (0, 2, 0)
    assert _open(raised) == {"ERROR": 14, "WARN": 3}


def test_a_non_default_scan_closes_nothing(raised: Connection) -> None:
    assert _rescan(raised, _report((), threshold=Decimal(3))) == (0, 3, 0)
    assert _open(raised) == {"ERROR": 14, "WARN": 3}


def test_without_a_scope_persist_findings_closes_nothing(raised: Connection) -> None:
    """The sentinel's and the L0 sweep's path, unchanged: a run over one day closes no flag."""
    counts = persist_findings(raised, [], clock=LATER)
    assert counts.resolved == 0
    assert _open(raised) == {"ERROR": 14, "WARN": 3}


def test_a_flag_about_no_isin_closes_only_on_a_full_scan(conn: Connection) -> None:
    market_wide = QualityFinding(
        logical_date=date(2026, 10, 5),
        check_name=CHECK_NAME,
        severity="ERROR",
        source="prices_adjusted",
        fingerprint=finding_fingerprint(CHECK_NAME, None, date(2026, 10, 5)),
    )
    persist_findings(conn, [market_wide], clock=RAISED)
    partial = FindingScope(check_name=CHECK_NAME, isins=frozenset({STALE[0][0]}))
    assert persist_findings(conn, [], clock=LATER, resolve_scope=partial).resolved == 0
    full = FindingScope(check_name=CHECK_NAME)
    assert persist_findings(conn, [], clock=LATER, resolve_scope=full).resolved == 1
