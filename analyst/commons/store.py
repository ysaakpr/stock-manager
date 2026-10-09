"""A10 · M17.1 — where Commons builds are kept: append-only, keyed by session and build digest.

Migration 0018 holds three tables: ``commons_build``, ``commons_market_sheet`` and
``commons_universe_sheet``. Every row is keyed by ``(trading_date, build_digest)``, and universe
rows add the ISIN. Recording a build the store already holds (same session, same digest) does
nothing and reports so; that is the "same lake, same build" case. A build of the same session over
a changed lake has a new digest and is written beside the old one. Nothing is ever updated or
deleted, and the tables' 0001 ``reject_mutation`` triggers enforce it (invariant #12). A manager's
decision cites the build digest it read, and that build must stay readable as it was.

Every read re-verifies the digests (:meth:`CommonsSheets.verify`). A build that no longer hashes
to the digest it was recorded under raises. It is never served.

What this module never does: build a sheet, commit a transaction (the job owns it), or touch a
manager's book or journal.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Final, Protocol

from psycopg.types.json import Json

from analyst.commons.sheets import CommonsSheets, Gap, MarketSheet, UniverseRow
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = ["CommonsStore", "CommonsStoreError", "InMemoryCommonsStore", "PostgresCommonsStore"]

_LOG = get_logger(__name__)

#: Universe-sheet columns after the key, in `UniverseRow` field order.
_ROW_COLUMNS: Final[tuple[str, ...]] = tuple(
    name for name in UniverseRow.model_fields if name != "isin"
)


class CommonsStoreError(RuntimeError):
    """A stored build is inconsistent: it is missing a part, or does not reproduce its digest."""


class CommonsStore(Protocol):
    """Append-only storage for Commons builds."""

    def record(self, sheets: CommonsSheets, *, recorded_at: datetime) -> bool:
        """Store ``sheets``. Returns ``False`` when this exact build is already stored."""

    def get(self, trading_date: date, build_digest: str) -> CommonsSheets | None:
        """One stored build, verified, or ``None``."""

    def latest(self, trading_date: date) -> CommonsSheets | None:
        """The most recently recorded build of ``trading_date``, verified, or ``None``."""

    def digests(self, trading_date: date) -> tuple[str, ...]:
        """Every build digest stored for ``trading_date``, oldest record first."""


class InMemoryCommonsStore:
    """:class:`CommonsStore` in a dict, for tests and dry runs. Same append-only contract."""

    def __init__(self) -> None:
        self._builds: dict[tuple[date, str], tuple[datetime, CommonsSheets]] = {}

    def record(self, sheets: CommonsSheets, *, recorded_at: datetime) -> bool:
        sheets.verify()
        key = (sheets.trading_date, sheets.build_digest)
        if key in self._builds:
            return False
        self._builds[key] = (recorded_at, sheets)
        return True

    def get(self, trading_date: date, build_digest: str) -> CommonsSheets | None:
        found = self._builds.get((trading_date, build_digest))
        return None if found is None else found[1]

    def latest(self, trading_date: date) -> CommonsSheets | None:
        digests = self.digests(trading_date)
        return self.get(trading_date, digests[-1]) if digests else None

    def digests(self, trading_date: date) -> tuple[str, ...]:
        own = [(at, key[1]) for key, (at, _) in self._builds.items() if key[0] == trading_date]
        return tuple(digest for _, digest in sorted(own))


class PostgresCommonsStore:
    """:class:`CommonsStore` over migration 0018. It never commits; the caller does."""

    __slots__ = ("_conn",)

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def record(self, sheets: CommonsSheets, *, recorded_at: datetime) -> bool:
        sheets.verify()
        inserted = self._conn.execute(
            "INSERT INTO commons_build (trading_date, build_digest, market_digest, "
            "universe_digest, sheet_version, parameters, gaps, universe_size, built_at, "
            "recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (trading_date, build_digest) DO NOTHING RETURNING build_digest",
            (
                sheets.trading_date,
                sheets.build_digest,
                sheets.market_digest,
                sheets.universe_digest,
                sheets.sheet_version,
                Json(sheets.parameters),
                Json([gap.model_dump(mode="json") for gap in sheets.gaps]),
                len(sheets.universe),
                sheets.built_at,
                recorded_at,
            ),
        ).fetchone()
        if inserted is None:
            _LOG.info(
                "commons.store.already_recorded",
                trading_date=sheets.trading_date.isoformat(),
                build_digest=sheets.build_digest,
            )
            return False
        self._conn.execute(
            "INSERT INTO commons_market_sheet (trading_date, build_digest, sheet) "
            "VALUES (%s, %s, %s)",
            (
                sheets.trading_date,
                sheets.build_digest,
                Json(sheets.market.model_dump(mode="json")),
            ),
        )
        placeholders = ", ".join(["%s"] * (3 + len(_ROW_COLUMNS)))
        with self._conn.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO commons_universe_sheet (trading_date, build_digest, isin, "
                f"{', '.join(_ROW_COLUMNS)}) VALUES ({placeholders})",
                [
                    (
                        sheets.trading_date,
                        sheets.build_digest,
                        row.isin,
                        *(getattr(row, name) for name in _ROW_COLUMNS),
                    )
                    for row in sheets.universe
                ],
            )
        _LOG.info(
            "commons.store.recorded",
            trading_date=sheets.trading_date.isoformat(),
            build_digest=sheets.build_digest,
            universe=len(sheets.universe),
        )
        return True

    def get(self, trading_date: date, build_digest: str) -> CommonsSheets | None:
        build = self._conn.execute(
            "SELECT market_digest, universe_digest, sheet_version, parameters, gaps, "
            "universe_size, built_at FROM commons_build "
            "WHERE trading_date = %s AND build_digest = %s",
            (trading_date, build_digest),
        ).fetchone()
        if build is None:
            return None
        market_digest, universe_digest, version, parameters, gaps, size, built_at = build
        market = self._conn.execute(
            "SELECT sheet FROM commons_market_sheet WHERE trading_date = %s AND build_digest = %s",
            (trading_date, build_digest),
        ).fetchone()
        if market is None:
            raise CommonsStoreError(f"build {build_digest[:12]} has no market sheet")
        rows = self._conn.execute(
            f"SELECT isin, {', '.join(_ROW_COLUMNS)} FROM commons_universe_sheet "
            "WHERE trading_date = %s AND build_digest = %s ORDER BY isin",
            (trading_date, build_digest),
        ).fetchall()
        if len(rows) != size:
            raise CommonsStoreError(
                f"build {build_digest[:12]} records {size} universe rows but holds {len(rows)}"
            )
        sheets = CommonsSheets(
            trading_date=trading_date,
            sheet_version=version,
            parameters=parameters,
            market=MarketSheet.model_validate(market[0]),
            universe=tuple(_row_of(row) for row in rows),
            gaps=tuple(Gap.model_validate(gap) for gap in gaps),
            market_digest=market_digest,
            universe_digest=universe_digest,
            build_digest=build_digest,
            built_at=built_at,
        )
        try:
            sheets.verify()
        except ValueError as exc:
            raise CommonsStoreError(str(exc)) from exc
        return sheets

    def latest(self, trading_date: date) -> CommonsSheets | None:
        digests = self.digests(trading_date)
        return self.get(trading_date, digests[-1]) if digests else None

    def digests(self, trading_date: date) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT build_digest FROM commons_build WHERE trading_date = %s "
            "ORDER BY recorded_at, build_digest",
            (trading_date,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)


def _row_of(row: Sequence[Any]) -> UniverseRow:
    return UniverseRow.model_validate(dict(zip(("isin", *_ROW_COLUMNS), row, strict=True)))
