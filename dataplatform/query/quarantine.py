"""D4 structural quarantine of restated fundamentals (M7.2) — invariant #8, §7.

Invariant #8: *restated (Screener) fundamentals are monitoring-only and physically unreachable from
a backtest or a decision.* M7.1 already put restated data in its own store root (`RESTATED/…`), a
sibling of the PIT layers that `paths.py` cannot form a path under; M4.3 gave the PIT read its
`knowable_date` leak guard (invariant #7). This module is the third leg the spec asks for: it makes
the *reachability* structural, not procedural — a PIT/backtest query context is handed a catalog
that **has no entry for the restated store and no capability that reads it**, so a backtest cannot
reach restated numbers even when it tries deliberately (the acceptance-1 test).

The quarantine is a property of *which object you were handed*, not of a flag some reader must
remember to check:

* **Separate catalogs.** `backtest_catalog()` and `monitoring_catalog()` both open their own DuckDB
  connection and register the PIT fundamentals view on it. Only the monitoring catalog additionally
  registers the restated view and constructs the one `RestatedReader` that can touch the restated
  store. The backtest catalog holds `None` there and *cannot be made* to register the restated store
  — `_register_restated` is reached only down the monitoring path, and `read_restated`/`restated()`
  raise `QuarantineError` because the capability was never built. There is no method on a backtest
  catalog that returns restated data, and no argument that flips one on.

* **One capability, granted once.** `RestatedReader` is the sole object in the codebase (besides the
  store's own writer) that reads the restated lake through a decision-facing surface, and it is
  constructed *only* inside `monitoring_catalog`. A backtest context never holds one, so "read the
  restated store from a backtest" is not a guarded call that happens to fail — it is a call with no
  receiver.

* **Provenance travels with the datum.** Everything the monitoring reader returns is a
  `ProvenancedFundamental` carrying the `store` it came from and its `source` tag, so the T1/T2
  monitoring path (A5, M7.4) can record *which store each datum came from* in the journal — the
  restated numbers are labelled as restated wherever they surface (acceptance 2). This module keeps
  System 1 pure: it produces the provenance; the analyst-side journal (System 2) consumes it, so
  `dataplatform` never imports `analyst`.

The two contexts are the two legitimate readers of fundamentals: a `backtest_catalog` for the PIT /
backtest / break-condition path (invariant #7, #8 — restated absent), and a `monitoring_catalog` for
the T1/T2 monitoring path (restated present, provenance-tagged). `PitContext` in `pit.py` remains
the `as_of` leak guard over *any* PIT dataset; this module is the orthogonal store-reachability
guard the restated data specifically needs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

import duckdb

from dataplatform.config import get_settings
from dataplatform.logging import get_logger
from dataplatform.query.screen import QuarantineError
from dataplatform.store.l2 import open_connection
from dataplatform.store.paths import DEFAULT_PART_FILENAME, Layer, layer_root
from dataplatform.store.pit_fundamentals import PIT_FUNDAMENTALS_DATASET
from dataplatform.store.restated import (
    RESTATED_ROOT_NAME,
    SCREENER_FUNDAMENTALS_DATASET,
    RestatedStore,
    restated_dataset_dir,
)

__all__ = [
    "PIT_FUNDAMENTALS_VIEW",
    "RESTATED_FUNDAMENTALS_VIEW",
    "ProvenancedFundamental",
    "QuarantineError",
    "QueryContext",
    "RestatedReader",
    "StoreCatalog",
    "backtest_catalog",
    "monitoring_catalog",
]

_LOG = get_logger(__name__)

#: The DuckDB view name over the true point-in-time fundamentals store (M7.3). Registered on *both*
#: catalogs — PIT data is safe in a backtest.
PIT_FUNDAMENTALS_VIEW: Final = "pit_fundamentals"

#: The DuckDB view name over the restated (Screener) store (M7.1). Registered on the monitoring
#: catalog *only*; a backtest connection has no such table, which is invariant #8 at the SQL layer.
RESTATED_FUNDAMENTALS_VIEW: Final = "restated_fundamentals"


class QueryContext(StrEnum):
    """Which of the two legitimate fundamentals readers a catalog serves.

    The value is not a switch a caller flips to unlock restated data — it selects which catalog
    *builder* ran, and the builders differ in what capability they construct. `BACKTEST` never
    builds the restated reader; `MONITORING` does. So the context is a label on a structural
    difference, not the difference itself.
    """

    BACKTEST = "backtest"
    """PIT / backtest / break-condition path. Restated store absent (invariant #7, #8)."""

    MONITORING = "monitoring"
    """T1/T2 monitoring path (§5.4). Restated store present, every datum provenance-tagged."""


@dataclass(frozen=True, slots=True)
class ProvenancedFundamental:
    """One fundamental figure carrying the store it was read from — what the journal records.

    What it does: pair a restated figure with its `store` (the `RESTATED` root) and `source` tag, so
    a consumer that surfaces it into a T1/T2 evidence bundle can label *which store it came from*
    (invariant #8, acceptance 2). Money is `Decimal`.
    What it assumes: it was produced by a `RestatedReader` reading the restated lake — the `store`
    and `source` are copied off the stored row, not asserted by the caller.
    What it never does: hold a `knowable_date` (restated data has none, which is exactly why it is
    quarantined from PIT) or a float. It carries no adjusted price and no symbol — the key is the
    ISIN (invariant #2).

    `as_evidence_fields` returns a plain string mapping suitable for a journal evidence item's
    provenance detail; this module stays in System 1 and does not import the journal itself.
    """

    store: str
    source: str
    isin: str
    statement: str
    metric: str
    period: str
    value: Decimal
    l0_key: str

    def as_evidence_fields(self) -> Mapping[str, str]:
        """The provenance of this datum as strings, for a journal evidence item's `detail`.

        Every field a T1/T2 evidence bundle needs to say "this number is restated, from the RESTATED
        store, derived from this L0 payload" — no float, so it round-trips into a content-addressed
        snapshot byte-for-byte (§8.3.3).
        """
        return {
            "store": self.store,
            "source": self.source,
            "isin": self.isin,
            "statement": self.statement,
            "metric": self.metric,
            "period": self.period,
            "value": str(self.value),
            "l0_key": self.l0_key,
        }


class RestatedReader:
    """The sole decision-facing capability that reads the restated store.

    What it does: read one ISIN's restated fundamentals back out of the `RESTATED` lake and return
    them as `ProvenancedFundamental`s, each stamped with the store it came from.
    What it assumes: it was constructed by `monitoring_catalog`. Nothing else in the platform builds
    one against a decision context — a backtest catalog holds `None` in its place — so possessing a
    `RestatedReader` *is* the authorisation to read restated data.
    What it never does: exist on a backtest/PIT context. That absence is the structural half of
    invariant #8: the quarantine is enforced by the reader not being there, not by a runtime flag it
    checks.
    """

    __slots__ = ("_store",)

    def __init__(self, store: RestatedStore) -> None:
        self._store = store

    def __repr__(self) -> str:
        return f"{type(self).__name__}(store={self._store!r})"

    def read_isin(
        self, isin: str, *, dataset: str = SCREENER_FUNDAMENTALS_DATASET
    ) -> tuple[ProvenancedFundamental, ...]:
        """One ISIN's restated fundamentals, provenance-tagged; empty tuple if none are stored."""
        rows = self._store.read_isin(isin, dataset=dataset)
        provenanced = tuple(_to_provenanced(row) for row in rows)
        _LOG.info(
            "query.quarantine.restated_read",
            context=QueryContext.MONITORING.value,
            store=RESTATED_ROOT_NAME,
            isin=isin,
            rows=len(provenanced),
        )
        return provenanced


class StoreCatalog:
    """The catalog of fundamentals stores one query context may reach — its own DuckDB connection.

    What it does: own a DuckDB connection with the PIT fundamentals view registered, plus — for a
    monitoring context only — the restated view and a `RestatedReader`. `read_restated`/`restated`
    hand back restated data on a monitoring catalog and *raise* `QuarantineError` on a backtest one,
    where the capability was never built.
    What it assumes: it was built by `backtest_catalog` or `monitoring_catalog`; the public
    constructor exists but the two builders are the sanctioned entry points and set the invariant.
    What it never does: register the restated view or build a `RestatedReader` on a backtest
    context. Use it as a context manager (or call `close`) to release a connection it opened.

    The connection is the "separate catalog" the spec names: a backtest catalog's connection has no
    restated table, so even a hand-written SQL query cannot reach the restated lake through it.
    """

    __slots__ = ("_con", "_context", "_data_root", "_owns_con", "_restated", "_views")

    def __init__(
        self,
        context: QueryContext,
        *,
        data_root: Path | None = None,
        con: duckdb.DuckDBPyConnection | None = None,
    ) -> None:
        self._context = context
        self._data_root = get_settings().data_root if data_root is None else data_root
        self._con = open_connection() if con is None else con
        self._owns_con = con is None
        self._views: set[str] = set()
        self._restated: RestatedReader | None = None

        self._register_pit()
        if context is QueryContext.MONITORING:
            self._register_restated()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(context={self._context.value!r}, views={sorted(self._views)})"
        )

    def __enter__(self) -> StoreCatalog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the connection if this catalog opened it; a no-op for an adopted one."""
        if self._owns_con:
            self._con.close()

    @property
    def context(self) -> QueryContext:
        """Which reader this catalog serves."""
        return self._context

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        """The DuckDB connection this catalog registered its views on."""
        return self._con

    @property
    def views(self) -> frozenset[str]:
        """The view names registered on this catalog's connection.

        A backtest catalog lists the PIT view only; a monitoring catalog also lists the restated
        view. The difference *is* the quarantine: `RESTATED_FUNDAMENTALS_VIEW not in
        backtest_catalog().views` is invariant #8 stated as a fact about the catalog.
        """
        return frozenset(self._views)

    def restated(self) -> RestatedReader:
        """The restated reader — on a monitoring catalog; `QuarantineError` on a backtest one.

        The backtest branch is not a guard the reader checks after the fact: the reader was never
        constructed for this context, so there is nothing to hand back. Raising, rather than
        returning an empty reader, makes a deliberate attempt to read restated data from a backtest
        a loud failure (acceptance 1) instead of a silently empty result that hides the bug.
        """
        if self._restated is None:
            raise QuarantineError(
                f"the {self._context.value} query context has no path to the restated store: "
                f"restated (Screener) fundamentals are monitoring-only and physically unreachable "
                f"from a backtest or a decision (invariant #8, §7). This catalog registered no "
                f"restated view and holds no restated reader; use a monitoring_catalog() for T1/T2 "
                f"monitoring, or the PIT fundamentals store for a backtest."
            )
        return self._restated

    def read_restated(
        self, isin: str, *, dataset: str = SCREENER_FUNDAMENTALS_DATASET
    ) -> tuple[ProvenancedFundamental, ...]:
        """One ISIN's restated fundamentals — monitoring only; `QuarantineError` on a backtest."""
        return self.restated().read_isin(isin, dataset=dataset)

    def sql(self, query: str) -> list[tuple[object, ...]]:
        """Run a read-only query against this catalog's connection and return the rows.

        Exists so the "separate catalog" boundary is testable at the SQL layer: a query naming the
        restated view against a backtest connection raises a DuckDB catalog error, because that view
        is not registered there.
        """
        return self._con.execute(query).fetchall()

    def _register_pit(self) -> None:
        """Register the PIT fundamentals view — on both contexts (PIT data is backtest-safe)."""
        dataset_dir = layer_root(Layer.L1, data_root=self._data_root) / PIT_FUNDAMENTALS_DATASET
        files = sorted(dataset_dir.glob(f"date=*/{DEFAULT_PART_FILENAME}"))
        self._register_view(PIT_FUNDAMENTALS_VIEW, files)

    def _register_restated(self) -> None:
        """Register the restated view and build the reader — reached only on the monitoring path.

        This method is never called for a `BACKTEST` context (the constructor guards it), which is
        why a backtest catalog cannot end up with a restated view or reader: there is no code path
        from a backtest context to here.
        """
        dataset_dir = restated_dataset_dir(SCREENER_FUNDAMENTALS_DATASET, data_root=self._data_root)
        files = sorted(dataset_dir.glob(f"isin=*/{DEFAULT_PART_FILENAME}"))
        self._register_view(RESTATED_FUNDAMENTALS_VIEW, files)
        self._restated = RestatedReader(RestatedStore(data_root=self._data_root))

    def _register_view(self, view: str, files: Iterable[Path]) -> None:
        """Create a DuckDB view over `files`, empty-but-typed when there are none."""
        listed = tuple(files)
        relation = _parquet_relation(listed)
        self._con.execute(f"CREATE OR REPLACE VIEW {_ident(view)} AS {relation}")
        self._views.add(view)
        _LOG.info(
            "query.quarantine.view_registered",
            context=self._context.value,
            view=view,
            files=len(listed),
        )


def backtest_catalog(
    *, data_root: Path | None = None, con: duckdb.DuckDBPyConnection | None = None
) -> StoreCatalog:
    """A PIT/backtest catalog: PIT fundamentals only, restated store unreachable (invariant #8).

    The catalog registers no restated view and constructs no restated reader, so a backtest cannot
    read restated fundamentals — not because a check refuses it, but because the capability is
    absent. This is the reader a break-condition evaluator and a replay/backtest step use.
    """
    return StoreCatalog(QueryContext.BACKTEST, data_root=data_root, con=con)


def monitoring_catalog(
    *, data_root: Path | None = None, con: duckdb.DuckDBPyConnection | None = None
) -> StoreCatalog:
    """A T1/T2 monitoring catalog: PIT *and* restated, every restated datum provenance-tagged.

    The one place in the platform that grants a `RestatedReader` to a decision-adjacent context, and
    it grants it to monitoring only. Reads carry their store, so the journal can record which store
    each datum came from (acceptance 2, §5.7's evidence pack).
    """
    return StoreCatalog(QueryContext.MONITORING, data_root=data_root, con=con)


def _to_provenanced(row: Mapping[str, object]) -> ProvenancedFundamental:
    """Turn one stored restated record into a provenance-tagged datum, stamping the store name."""
    value = row["value"]
    if not isinstance(value, Decimal):  # pragma: no cover - the parquet schema guarantees Decimal
        raise TypeError(f"restated value is {type(value).__name__}, not Decimal (money invariant)")
    return ProvenancedFundamental(
        store=RESTATED_ROOT_NAME,
        source=str(row["source"]),
        isin=str(row["isin"]),
        statement=str(row["statement"]),
        metric=str(row["metric"]),
        period=str(row["period"]),
        value=value,
        l0_key=f"{row['l0_source']}/{_iso(row['l0_logical_date'])}/{row['l0_filename']}",
    )


def _iso(value: object) -> str:
    """A stored date rendered ISO, whatever concrete date type the parquet reader returned."""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _parquet_relation(files: tuple[Path, ...]) -> str:
    """DuckDB relation SQL over `files`, or an empty-but-typed relation when there are none.

    Mirrors the lake's own view-registration rule (`store.l2`): a cold store must still register a
    view without error, so with no files the relation is a `SELECT … WHERE false` rather than a
    `read_parquet` over nothing.
    """
    if not files:
        return "SELECT * FROM (SELECT NULL) WHERE false"
    listed = ", ".join(f"'{_sql_literal(str(path))}'" for path in files)
    return f"SELECT * FROM read_parquet([{listed}])"


def _sql_literal(value: str) -> str:
    """Escape a single-quoted SQL string literal (a file path) so it cannot break out."""
    return value.replace("'", "''")


def _ident(name: str) -> str:
    """Quote a DuckDB identifier (a view name) so it cannot inject SQL."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'
