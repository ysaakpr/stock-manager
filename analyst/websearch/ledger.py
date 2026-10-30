"""Request counts: per vendor per month, all vendors per month, and per M17 session.

Every count is a *reservation*: the router reserves before it sends, so two threads (or two
processes) can never both spend the last unit of a budget. A reservation that would overshoot is
refused and nothing is counted. A request that was sent counts whether or not it succeeded — the
vendor saw it.

`reserve_call` checks two caps at once, atomically: the owner's monthly total across all vendors
(`WEBSEARCH_MONTHLY_REQUEST_CAP`, one per call) and the vendor's own monthly budget (in its billing
unit). The direct fetch passes no caps: it is counted per vendor for the status page, and never in
the total, because it is not a vendor request.

`PostgresUsageLedger` keeps the counts in migration 0021's tables, so the caps hold across
processes and restarts, and `GET /status/websearch` reads the same rows. One reservation is one
transaction: lock-and-bump the month's total row (`requests + 1 <= cap`), then the vendor's row
(`used + units <= budget`); if either refuses, the transaction rolls back and nothing is counted.
`InMemoryUsageLedger` is the same contract for tests and rehearsals, and both decide "does it fit"
with the one function `models.within_budget`.

The month is the calendar month in IST of the clock's `now()`: at 00:00 IST on the 1st every
count starts from zero, without anything being reset — a new month is a new row.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from threading import Lock
from typing import Protocol

from psycopg import sql

from analyst.websearch.models import Capability, VendorName, within_budget
from dataplatform.store.db import Connection

__all__ = ["InMemoryUsageLedger", "PostgresUsageLedger", "Reservation", "UsageLedger"]


class Reservation(StrEnum):
    """What `reserve_call` decided. Only `OK` counted anything."""

    OK = "ok"
    OVER_VENDOR_BUDGET = "over_vendor_budget"
    OVER_MONTHLY_CAP = "over_monthly_cap"


class UsageLedger(Protocol):
    """Where the counts live."""

    def reserve_call(
        self,
        vendor: VendorName,
        month: date,
        units: int,
        budget: int | None,
        monthly_cap: int | None,
        *,
        at: datetime,
    ) -> Reservation:
        """Count one call of `units` against the vendor's `budget` (None: uncapped) and one
        request against the all-vendor `monthly_cap` (None: not part of the total)."""
        ...

    def reserve_session(
        self, session: date, capability: Capability, limit: int, *, at: datetime
    ) -> int | None:
        """Count one request against the session's cap; the new total, or None at the cap."""
        ...

    def month_usage(self, month: date) -> dict[VendorName, int]:
        """Month-to-date units per vendor (vendors never used this month are absent)."""
        ...

    def month_requests(self, month: date) -> dict[VendorName, int]:
        """Month-to-date calls per vendor."""
        ...

    def month_total(self, month: date) -> int:
        """Month-to-date vendor requests, all vendors together (the direct fetch excluded)."""
        ...


class InMemoryUsageLedger:
    """The ledger in a dict: tests, `--memory` rehearsals, and the live check."""

    def __init__(self) -> None:
        self._units: dict[tuple[VendorName, date], int] = {}
        self._requests: dict[tuple[VendorName, date], int] = {}
        self._total: dict[date, int] = {}
        self._session: dict[tuple[date, Capability], int] = {}
        self._lock = Lock()

    def reserve_call(
        self,
        vendor: VendorName,
        month: date,
        units: int,
        budget: int | None,
        monthly_cap: int | None,
        *,
        at: datetime,
    ) -> Reservation:
        with self._lock:
            total = self._total.get(month, 0)
            if monthly_cap is not None and not within_budget(total, 1, monthly_cap):
                return Reservation.OVER_MONTHLY_CAP
            used = self._units.get((vendor, month), 0)
            if budget is not None and not within_budget(used, units, budget):
                return Reservation.OVER_VENDOR_BUDGET
            self._units[(vendor, month)] = used + units
            self._requests[(vendor, month)] = self._requests.get((vendor, month), 0) + 1
            if monthly_cap is not None:
                self._total[month] = total + 1
            return Reservation.OK

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
            return {v: n for (v, m), n in self._units.items() if m == month}

    def month_requests(self, month: date) -> dict[VendorName, int]:
        with self._lock:
            return {v: n for (v, m), n in self._requests.items() if m == month}

    def month_total(self, month: date) -> int:
        with self._lock:
            return self._total.get(month, 0)


_ENSURE_TOTAL = (
    "INSERT INTO websearch_month_total (month, requests, updated_at) VALUES (%s, 0, %s) "
    "ON CONFLICT (month) DO NOTHING"
)
_RESERVE_TOTAL = (
    "UPDATE websearch_month_total SET requests = requests + 1, updated_at = %(at)s "
    "WHERE month = %(month)s AND requests + 1 <= %(cap)s RETURNING requests"
)
_ENSURE_MONTH = (
    "INSERT INTO websearch_vendor_month (vendor, month, used, requests, updated_at) "
    "VALUES (%s, %s, 0, 0, %s) ON CONFLICT (vendor, month) DO NOTHING"
)
_RESERVE_MONTH = (
    "UPDATE websearch_vendor_month SET used = used + %(units)s, requests = requests + 1, "
    "updated_at = %(at)s WHERE vendor = %(vendor)s AND month = %(month)s "
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


class _RefusedError(Exception):
    """Rolls a reservation's transaction back, carrying why."""

    def __init__(self, outcome: Reservation) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


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

    def reserve_call(
        self,
        vendor: VendorName,
        month: date,
        units: int,
        budget: int | None,
        monthly_cap: int | None,
        *,
        at: datetime,
    ) -> Reservation:
        with self._lock:
            conn = self._connection()
            try:
                with conn.transaction():
                    if monthly_cap is not None:
                        conn.execute(_ENSURE_TOTAL, (month, at))
                        row = conn.execute(
                            _RESERVE_TOTAL, {"at": at, "month": month, "cap": monthly_cap}
                        ).fetchone()
                        if row is None:
                            raise _RefusedError(Reservation.OVER_MONTHLY_CAP)
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
                    if row is None:
                        raise _RefusedError(Reservation.OVER_VENDOR_BUDGET)
            except _RefusedError as refused:
                return refused.outcome
            return Reservation.OK

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

    def _per_vendor(self, column: str, month: date) -> dict[VendorName, int]:
        with self._lock:
            rows = (
                self._connection()
                .execute(
                    sql.SQL(
                        "SELECT vendor, {} FROM websearch_vendor_month WHERE month = %s"
                    ).format(sql.Identifier(column)),
                    (month,),
                )
                .fetchall()
            )
        return {VendorName(str(vendor)): int(n) for vendor, n in rows}

    def month_usage(self, month: date) -> dict[VendorName, int]:
        return self._per_vendor("used", month)

    def month_requests(self, month: date) -> dict[VendorName, int]:
        return self._per_vendor("requests", month)

    def month_total(self, month: date) -> int:
        with self._lock:
            row = (
                self._connection()
                .execute("SELECT requests FROM websearch_month_total WHERE month = %s", (month,))
                .fetchone()
            )
        return 0 if row is None else int(row[0])

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
