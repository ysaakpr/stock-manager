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

Migration 0019 (M17.2) adds the shortlist (``commons_shortlist_build``, ``commons_shortlist``),
keyed by ``(trading_date, shortlist_digest)`` and re-verified on read the same way, and the filing
digests (``commons_filing_digest``, one row per filing id, ever; ``commons_digest_run``, one row
per distinct run of a session). A digest already stored for a filing is never written again.

What this module never does: build a sheet, commit a transaction (the job owns it), or touch a
manager's book or journal.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Final, Protocol

from psycopg.types.json import Json

from analyst.commons.digests import DigestRun, FilingDigest
from analyst.commons.sheets import CommonsSheets, Gap, MarketSheet, UniverseRow
from analyst.commons.shortlist import Shortlist, ShortlistEntry
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = [
    "CommonsStore",
    "CommonsStoreError",
    "InMemoryCommonsStore",
    "InMemoryDigestStore",
    "InMemoryShortlistStore",
    "PostgresCommonsStore",
    "PostgresDigestStore",
    "PostgresShortlistStore",
    "ShortlistStore",
]

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


# ── M17.2: the shortlist and the filing digests (migration 0019) ─────────────────────────────────

#: Shortlist entry columns after the position, in `ShortlistEntry` field order.
_ENTRY_COLUMNS: Final[tuple[str, ...]] = tuple(
    name for name in ShortlistEntry.model_fields if name != "position"
)
_DIGEST_COLUMNS: Final[tuple[str, ...]] = tuple(FilingDigest.model_fields)


class ShortlistStore(Protocol):
    """Append-only storage for shortlists, keyed by session and shortlist digest."""

    def record(self, shortlist: Shortlist, *, recorded_at: datetime) -> bool:
        """Store ``shortlist``. Returns ``False`` when this exact shortlist is already stored."""

    def latest(self, trading_date: date) -> Shortlist | None:
        """The most recently recorded shortlist of ``trading_date``, verified, or ``None``."""


class InMemoryShortlistStore:
    """:class:`ShortlistStore` in a dict, for tests and dry runs. Same append-only contract."""

    def __init__(self) -> None:
        self._rows: dict[tuple[date, str], tuple[datetime, Shortlist]] = {}

    def record(self, shortlist: Shortlist, *, recorded_at: datetime) -> bool:
        shortlist.verify()
        key = (shortlist.trading_date, shortlist.shortlist_digest)
        if key in self._rows:
            return False
        self._rows[key] = (recorded_at, shortlist)
        return True

    def latest(self, trading_date: date) -> Shortlist | None:
        own = [(at, s) for (day, _), (at, s) in self._rows.items() if day == trading_date]
        own.sort(key=lambda pair: (pair[0], pair[1].shortlist_digest))
        return own[-1][1] if own else None


class PostgresShortlistStore:
    """:class:`ShortlistStore` over migration 0019. It never commits; the caller does."""

    __slots__ = ("_conn",)

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def record(self, shortlist: Shortlist, *, recorded_at: datetime) -> bool:
        shortlist.verify()
        inserted = self._conn.execute(
            "INSERT INTO commons_shortlist_build (trading_date, shortlist_digest, build_digest, "
            "shortlist_version, rule_hash, universe_size, coverage, gaps, shortlist_size, "
            "built_at, recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (trading_date, shortlist_digest) DO NOTHING RETURNING shortlist_digest",
            (
                shortlist.trading_date,
                shortlist.shortlist_digest,
                shortlist.build_digest,
                shortlist.shortlist_version,
                shortlist.rule_hash,
                shortlist.universe_size,
                Json(shortlist.coverage),
                Json([gap.model_dump(mode="json") for gap in shortlist.gaps]),
                len(shortlist.entries),
                shortlist.built_at,
                recorded_at,
            ),
        ).fetchone()
        if inserted is None:
            return False
        placeholders = ", ".join(["%s"] * (3 + len(_ENTRY_COLUMNS)))
        with self._conn.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO commons_shortlist (trading_date, shortlist_digest, position, "
                f"{', '.join(_ENTRY_COLUMNS)}) VALUES ({placeholders})",
                [
                    (
                        shortlist.trading_date,
                        shortlist.shortlist_digest,
                        entry.position,
                        *(getattr(entry, name) for name in _ENTRY_COLUMNS),
                    )
                    for entry in shortlist.entries
                ],
            )
        _LOG.info(
            "commons.store.shortlist_recorded",
            trading_date=shortlist.trading_date.isoformat(),
            shortlist_digest=shortlist.shortlist_digest,
            entries=len(shortlist.entries),
        )
        return True

    def latest(self, trading_date: date) -> Shortlist | None:
        build = self._conn.execute(
            "SELECT shortlist_digest, build_digest, shortlist_version, rule_hash, universe_size, "
            "coverage, gaps, shortlist_size, built_at FROM commons_shortlist_build "
            "WHERE trading_date = %s ORDER BY recorded_at DESC, shortlist_digest DESC LIMIT 1",
            (trading_date,),
        ).fetchone()
        if build is None:
            return None
        digest, build_digest, version, rule_hash, universe_size, coverage, gaps, size, at = build
        rows = self._conn.execute(
            f"SELECT position, {', '.join(_ENTRY_COLUMNS)} FROM commons_shortlist "
            "WHERE trading_date = %s AND shortlist_digest = %s ORDER BY position",
            (trading_date, digest),
        ).fetchall()
        if len(rows) != size:
            raise CommonsStoreError(
                f"shortlist {digest[:12]} records {size} entries but holds {len(rows)}"
            )
        shortlist = Shortlist(
            trading_date=trading_date,
            build_digest=build_digest,
            shortlist_version=version,
            rule_hash=rule_hash,
            universe_size=universe_size,
            coverage=coverage,
            entries=tuple(
                ShortlistEntry.model_validate(
                    dict(zip(("position", *_ENTRY_COLUMNS), row, strict=True))
                )
                for row in rows
            ),
            gaps=tuple(Gap.model_validate(gap) for gap in gaps),
            shortlist_digest=digest,
            built_at=at,
        )
        try:
            shortlist.verify()
        except ValueError as exc:
            raise CommonsStoreError(str(exc)) from exc
        return shortlist


class InMemoryDigestStore:
    """:class:`~analyst.commons.digests.DigestStore` in dicts. Same append-only contract."""

    def __init__(self) -> None:
        self._digests: dict[str, FilingDigest] = {}
        self._runs: dict[tuple[date, str], DigestRun] = {}

    def get(self, filing_id: str) -> FilingDigest | None:
        return self._digests.get(filing_id)

    def record(self, digest: FilingDigest, *, recorded_at: datetime) -> bool:
        if digest.filing_id in self._digests:
            return False
        self._digests[digest.filing_id] = digest
        return True

    def record_run(self, run: DigestRun, *, recorded_at: datetime) -> bool:
        key = (run.trading_date, run.run_digest)
        if key in self._runs:
            return False
        self._runs[key] = run
        return True

    def resume_after(self, before: date) -> date | None:
        earlier = [run for (day, _), run in self._runs.items() if day < before]
        clean = [run.trading_date for run in earlier if run.clean]
        if clean:
            return max(clean)
        return min((run.since for run in earlier), default=None)

    def runs(self) -> tuple[DigestRun, ...]:
        return tuple(self._runs[key] for key in sorted(self._runs))


class PostgresDigestStore:
    """:class:`~analyst.commons.digests.DigestStore` over migration 0019. Never commits."""

    __slots__ = ("_conn",)

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def get(self, filing_id: str) -> FilingDigest | None:
        row = self._conn.execute(
            f"SELECT {', '.join(_DIGEST_COLUMNS)} FROM commons_filing_digest WHERE filing_id = %s",
            (filing_id,),
        ).fetchone()
        if row is None:
            return None
        return FilingDigest.model_validate(dict(zip(_DIGEST_COLUMNS, row, strict=True)))

    def record(self, digest: FilingDigest, *, recorded_at: datetime) -> bool:
        values = [
            Json(digest.body.model_dump(mode="json"))
            if name == "body"
            else getattr(digest, name).value
            if name == "kind"
            else getattr(digest, name)
            for name in _DIGEST_COLUMNS
        ]
        inserted = self._conn.execute(
            f"INSERT INTO commons_filing_digest ({', '.join(_DIGEST_COLUMNS)}, recorded_at) "
            f"VALUES ({', '.join(['%s'] * (len(_DIGEST_COLUMNS) + 1))}) "
            "ON CONFLICT (filing_id) DO NOTHING RETURNING filing_id",
            (*values, recorded_at),
        ).fetchone()
        return inserted is not None

    def record_run(self, run: DigestRun, *, recorded_at: datetime) -> bool:
        inserted = self._conn.execute(
            "INSERT INTO commons_digest_run (trading_date, run_digest, since_date, filing_ids, "
            "digested, cached, failures, gaps, recorded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (trading_date, run_digest) DO NOTHING RETURNING run_digest",
            (
                run.trading_date,
                run.run_digest,
                run.since,
                Json(list(run.filing_ids)),
                Json(list(run.digested)),
                Json(list(run.cached)),
                Json([f.model_dump(mode="json") for f in run.failures]),
                Json([g.model_dump(mode="json") for g in run.gaps]),
                recorded_at,
            ),
        ).fetchone()
        return inserted is not None

    def resume_after(self, before: date) -> date | None:
        row = self._conn.execute(
            "SELECT max(trading_date) FILTER (WHERE failures = '[]'::jsonb), min(since_date) "
            "FROM commons_digest_run WHERE trading_date < %s",
            (before,),
        ).fetchone()
        if row is None:
            return None
        clean: date | None = row[0]
        earliest: date | None = row[1]
        return clean if clean is not None else earliest
