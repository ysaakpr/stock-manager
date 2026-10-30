"""Request counts: per vendor per calendar month, and per M17 session per capability.

Both are *reservations*: the router reserves before it sends, so two threads (or two processes)
can never both spend the last unit of a budget. A reservation that would overshoot is refused and
nothing is counted. A request that was sent counts whether or not it succeeded — the vendor saw it.

`PostgresUsageLedger` keeps the counts in migration 0021's two tables, so the budget holds across
processes and restarts, and `GET /status/websearch` reads the same rows. The reservation is one
conditional `UPDATE ... WHERE used + units <= budget RETURNING used` on an autocommit connection:
the row lock serialises concurrent reservers and each re-evaluates the condition, so the check
and the spend are one atomic step. `InMemoryUsageLedger` is the same contract for tests and
rehearsals, and both decide "does it fit" with the one function `models.within_budget`.

The month is the calendar month in IST (`Settings.timezone`) of the clock's `now()`: at 00:00 IST
on the 1st every vendor starts from zero, without anything being reset — a new month is a new row.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from threading import Lock
from typing import Protocol

from analyst.websearch.models import Capability, VendorName, within_budget
from dataplatform.store.db import Connection

__all__ = ["InMemoryUsageLedger", "PostgresUsageLedger", "UsageLedger"]


class UsageLedger(Protocol):
    """Where the counts live."""

    def reserve_month(
        self, vendor: VendorName, month: date, units: int, budget: int | None, *, at: datetime
    ) -> int | None:
        """Add `units` to the vendor's month if the total stays within `budget` (None: no cap).

        Returns the new month-to-date total, or None when it would not fit (nothing counted).
        """
        ...

    def reserve_session(
        self, session: date, capability: Capability, limit: int, *, at: datetime
    ) -> int | None:
        """Count one request against the session's cap; the new total, or None at the cap."""
        ...

    def month_usage(self, month: date) -> dict[VendorName, int]:
        """Month-to-date units per vendor (vendors never used this month are absent)."""
        ...


class InMemoryUsageLedger:
    """The ledger in a dict: tests, `--memory` rehearsals, and the live check."""

    def __init__(self) -> None:
        self._month: dict[tuple[VendorName, date], int] = {}
        self._session: dict[tuple[date, Capability], int] = {}
        self._lock = Lock()

    def reserve_month(
        self, vendor: VendorName, month: date, units: int, budget: int | None, *, at: datetime
    ) -> int | None:
        with self._lock:
            used = self._month.get((vendor, month), 0)
            if budget is not None and not within_budget(used, units, budget):
                return None
            self._month[(vendor, month)] = used + units
            return used + units

    def reserve_session(
        self, session: date, capability: Capability, limit: int, *, at: datetime
    ) -> int | None:
        with self._lock:
            used = self._session.get((session, capability), 0)
            if not within_budget(used, 1, limit):
                return None
            self._session[(session, capability)] = used + 1
            return used + 1

    def month_usage(self, month: date) -> dict[VendorName, int]:
        with self._lock:
            return {v: n for (v, m), n in self._month.items() if m == month}


_ENSURE_MONTH = (
    "INSERT INTO websearch_vendor_month (vendor, month, used, updated_at) "
    "VALUES (%s, %s, 0, %s) ON CONFLICT (vendor, month) DO NOTHING"
)
_RESERVE_MONTH = (
    "UPDATE websearch_vendor_month SET used = used + %(units)s, updated_at = %(at)s "
    "WHERE vendor = %(vendor)s AND month = %(month)s "
    "AND (%(budget)s::integer IS NULL OR used + %(units)s <= %(budget)s::integer) "
    "RETURNING used"
)
_ENSURE_SESSION = (
    "INSERT INTO websearch_session_usage (session, capability, used, updated_at) "
    "VALUES (%s, %s, 0, %s) ON CONFLICT (session, capability) DO NOTHING"
)
_RESERVE_SESSION = (
    "UPDATE websearch_session_usage SET used = used + 1, updated_at = %(at)s "
    "WHERE session = %(session)s AND capability = %(capability)s AND used + 1 <= %(limit)s "
    "RETURNING used"
)


class PostgresUsageLedger:
    """The ledger in Postgres (migration 0021), on its own autocommit connection.

    What it assumes: `connect` returns a fresh autocommit connection; one is opened lazily and
    reused under a lock, because the router may reserve from several fetch threads at once.
    What it never does: hold a transaction open across a vendor call, or decrement a count.
    """

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect
        self._conn: Connection | None = None
        self._lock = Lock()

    def _connection(self) -> Connection:
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
        return self._conn

    def reserve_month(
        self, vendor: VendorName, month: date, units: int, budget: int | None, *, at: datetime
    ) -> int | None:
        with self._lock:
            conn = self._connection()
            conn.execute(_ENSURE_MONTH, (vendor.value, month, at))
            row = conn.execute(
                _RESERVE_MONTH,
                {
                    "units": units,
                    "at": at,
                    "vendor": vendor.value,
                    "month": month,
                    "budget": budget,
                },
            ).fetchone()
            return None if row is None else int(row[0])

    def reserve_session(
        self, session: date, capability: Capability, limit: int, *, at: datetime
    ) -> int | None:
        with self._lock:
            conn = self._connection()
            conn.execute(_ENSURE_SESSION, (session, capability.value, at))
            row = conn.execute(
                _RESERVE_SESSION,
                {"at": at, "session": session, "capability": capability.value, "limit": limit},
            ).fetchone()
            return None if row is None else int(row[0])

    def month_usage(self, month: date) -> dict[VendorName, int]:
        with self._lock:
            rows = (
                self._connection()
                .execute(
                    "SELECT vendor, used FROM websearch_vendor_month WHERE month = %s", (month,)
                )
                .fetchall()
            )
        return {VendorName(str(vendor)): int(used) for vendor, used in rows}

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
