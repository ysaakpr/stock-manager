"""D5: the request-scoped dependencies the status API and the operator console share.

Split out of `api.py` (M18.1) so the console router can declare the *same* dependency callables
the JSON endpoints declare, without importing the app module that mounts it. Sharing the callables
rather than copies is the point: a test (or the integration suite) that overrides
`db_connection` or `clock_source` on the app overrides it for `/status/*` and `/console/*` at once,
so a page and an endpoint read one connection, one clock and one settings object, and cannot
disagree. `api.py` re-exports every name here under its old spelling.

Settings, the clock (B10) and the connection are dependencies rather than module globals; nothing
here writes.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Annotated, Any

import psycopg
from fastapi import Depends

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.logging import get_logger
from dataplatform.quality.gap_schedule import RegistrySchedule
from dataplatform.quality.gaps import GapScanner, LakeL1Presence
from dataplatform.scheduler import read_heartbeat
from dataplatform.status.deviations import DeviationJudge, DeviationRegistry, default_deviations
from dataplatform.status.queries import HeartbeatRead, read_m17_journal, read_m17_roster
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection, connection

__all__ = [
    "ClockDep",
    "ConnDep",
    "DeviationsDep",
    "GapScannerDep",
    "HeartbeatDep",
    "JudgeDep",
    "M17JournalDep",
    "RosterDep",
    "SettingsDep",
    "SyncStoreDep",
    "clock_source",
    "db_connection",
    "deviation_judge",
    "deviation_registry",
    "gap_scanner",
    "heartbeat_source",
    "m17_journal_source",
    "m17_roster_source",
    "settings_source",
    "sync_store",
]

log = get_logger(__name__)


def settings_source() -> Settings:
    """The process settings, as a dependency so a test can point the API at a scratch database."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_source)]


def clock_source(settings: SettingsDep) -> Clock:
    """The clock the API measures heartbeat age against (B10 — never `datetime.now()`)."""
    return SystemClock(settings.tzinfo)


ClockDep = Annotated[Clock, Depends(clock_source)]


def db_connection(settings: SettingsDep) -> Iterator[Connection]:
    """One connection per request, closed on the way out.

    A dependency rather than a module-level pool: the status API is low-traffic by design (the
    daily loop calls it once a day), and one honest connection per request keeps a stale handle
    from outliving a database restart. The integration suite overrides it to reach a scratch DB.

    Never committed — nothing on this surface writes, so a request that ends without a commit has
    ended exactly the way every request here is meant to.
    """
    with connection(settings) as conn:
        yield conn


ConnDep = Annotated[Connection, Depends(db_connection)]


def sync_store(conn: ConnDep, clock: ClockDep) -> SyncStateStore:
    """The §4.4 state machine, wired to this request's connection and clock."""
    return SyncStateStore(conn, clock=clock)


SyncStoreDep = Annotated[SyncStateStore, Depends(sync_store)]


def deviation_registry() -> DeviationRegistry:
    """The checked-in accepted-deviation registry (loaded and validated once, at startup).

    A dependency so a test can hand the status surfaces a registry of its own. Display only: what
    it returns decides how an item is *shown*, never whether it is healthy.
    """
    return default_deviations()


DeviationsDep = Annotated[DeviationRegistry, Depends(deviation_registry)]


def deviation_judge(
    registry: DeviationsDep, store: SyncStoreDep, clock: ClockDep
) -> DeviationJudge:
    """The registry bound to this request's store, its connection, and the injected clock's today.

    Reads through the store's own connection rather than declaring `ConnDep`: an endpoint that
    reads only the store (`/status/sources`) must not gain a second dependency on the database
    because it shows deviation verdicts — a test, or a deployment, that overrides the store alone
    would otherwise reach for a database the route never needed.
    """
    return DeviationJudge(registry, store.connection, store, clock.today())


JudgeDep = Annotated[DeviationJudge, Depends(deviation_judge)]


def gap_scanner(conn: ConnDep, settings: SettingsDep, clock: ClockDep) -> GapScanner:
    """D7's gap report (M1.11), wired to this request's connection and the configured lake.

    The lake root comes from settings rather than from the process default so a test — and the
    container, whose `/data` is a bind mount — checks the L1 partitions it actually wrote. Moved
    here from `api.py` (M18.2) so the console's gaps panel declares the same callable.

    The scanner judges a pair with no row against the scheduler's crons (`RegistrySchedule`) at
    the injected instant: NOT_YET_DUE until the fire that covers its date has passed, and the
    configured sub-units of a unit-keyed source one by one.
    """
    return GapScanner(
        conn,
        l1_presence=LakeL1Presence(settings.data_root),
        schedule=RegistrySchedule(settings.tzinfo),
        now=clock.now(),
    )


GapScannerDep = Annotated[GapScanner, Depends(gap_scanner)]


def heartbeat_source(settings: SettingsDep) -> HeartbeatRead:
    """The freshest scheduler heartbeat, or why it could not be read.

    Deliberately catches `psycopg.Error` rather than only `OperationalError`: an unmigrated
    database answers the connection and then fails the query, and a `/health` that turned that
    into a 500 would hide the single most likely cause of a fresh deployment not working.
    """
    try:
        return HeartbeatRead(reachable=True, beat=read_heartbeat(settings))
    except psycopg.OperationalError as error:
        log.warning("health.database_unreachable", error=str(error).strip())
        return HeartbeatRead(reachable=False, error=str(error).strip())
    except psycopg.Error as error:
        log.warning("health.heartbeat_unreadable", error=str(error).strip())
        return HeartbeatRead(reachable=True, error=str(error).strip())


HeartbeatDep = Annotated[HeartbeatRead, Depends(heartbeat_source)]


def m17_roster_source() -> Any:
    """The M17 roster, as a dependency so a test can supply a smaller one."""
    return read_m17_roster()


RosterDep = Annotated[Any, Depends(m17_roster_source)]


def m17_journal_source(conn: ConnDep, settings: SettingsDep, roster: RosterDep) -> Sequence[Any]:
    """The M17 books' journal entries, in append order — a dependency so a test can supply them."""
    return read_m17_journal(conn, data_root=settings.data_root, roster=roster)


M17JournalDep = Annotated[Sequence[Any], Depends(m17_journal_source)]
