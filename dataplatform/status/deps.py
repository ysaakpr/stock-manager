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
from dataplatform.scheduler import read_heartbeat
from dataplatform.status.queries import HeartbeatRead, read_m17_journal, read_m17_roster
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection, connection

__all__ = [
    "ClockDep",
    "ConnDep",
    "HeartbeatDep",
    "M17JournalDep",
    "RosterDep",
    "SettingsDep",
    "SyncStoreDep",
    "clock_source",
    "db_connection",
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
