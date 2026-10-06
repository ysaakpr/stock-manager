"""D4: the unsourced-step price quarantine (M13.4, decision D22).

Three ISINs carry a share-basis step in their adjusted series that no factor removes: EIH on
2006-09-12, JM Financial on 2008-09-08, Shah Alloys on 2006-02-13
(`corpactions.manual_actions`, the `UNSOURCED_*` explained moves). Each was almost certainly a
split or bonus, but no L0 object states its terms, and a factor would mean inventing a ratio. So
the L2 level series falls 51-88% in one session where the holder lost nothing. A momentum rank
reads that as a collapse, a backtest that held the name marks it down, and a decision on either
side of the step compares two share bases.

The quarantine makes that unreachable. **Every bar of a quarantined ISIN dated before its
step's session is dropped** from what the decision-facing readers return. The step session itself
is kept, because it is the first bar of the basis the name trades on from then on. With the
pre-step bars gone:

* no backtest can buy the name before the step (no close, no fill bar, no listing window), so
  none can hold it across the step;
* no signal can span the step (a look-back reference before it finds no close), so none can rank
  on the phantom return;
* after the step the name is an ordinary, if short-history, security.

**Where it applies.** `QueryService` (every adjusted series and cross-section, the decision
path) filters its bars with `admits`. The backtest's own L1/L2 readers register their price views
through `register_raw_view` / `register_adjusted_view` here, and filter their per-session
partition reads with `sql_admits`. `quality.l2_continuity` does *not* read through it: it must
see the step to report it.

The windows come from `corpactions.ManualActions.unsourced_windows()` (the package's public
surface), so adding, or retiring, a row in the reviewed file is the only way to change them.
Nothing here reads a clock, fetches, or writes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from pathlib import Path
from typing import Final, Protocol

import duckdb

from dataplatform.corpactions import ManualActions, default_manual_actions
from dataplatform.ingest.models import ISIN_PATTERN
from dataplatform.store.l2 import register_adjusted_view as _register_adjusted_view
from dataplatform.store.l2 import register_raw_view as _register_raw_view

__all__ = ["PriceQuarantine", "default_price_quarantine"]

_ISIN: Final = re.compile(ISIN_PATTERN)
_IDENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


@dataclass(frozen=True, slots=True)
class PriceQuarantine:
    """ISIN → its first admissible session; every earlier bar of that ISIN is withheld.

    What it does: answer whether a bar may reach a decision (`admits`), as a SQL predicate too
    (`sql_admits`), and register raw or adjusted price views with the predicate applied.
    What it assumes: `first_sessions` came from the curated file (`from_manual_actions`). Every
    key is validated as an ISIN, which is what makes the literal SQL predicate safe to build.
    What it never does: drop a bar on or after the step, or touch an ISIN it does not name.
    """

    first_sessions: Mapping[str, date] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for isin, day in self.first_sessions.items():
            if not _ISIN.fullmatch(isin):
                raise ValueError(f"quarantine key {isin!r} is not an ISIN")
            if not isinstance(day, date):
                raise TypeError(f"{isin}: first session must be a date, got {type(day).__name__}")

    @classmethod
    def from_manual_actions(
        cls, curated: ManualActions, *, survivor_of: Callable[[str], str] | None = None
    ) -> PriceQuarantine:
        """The quarantine the curated file's unsourced steps call for (D22).

        With `survivor_of` (a D2 `LineageResolver.survivor_of`), each window is applied to the
        ISIN's survivor too: its L2 partition holds the retired ISIN's stitched pre-step bars
        under the survivor's ISIN. Where two windows land on one survivor the later one wins, the
        conservative side. Without a resolver, `store.l2.materialize_isin` refuses to stitch a
        quarantined ISIN into a survivor the file does not name, so the gap cannot arise silently.
        """
        windows = dict(curated.unsourced_windows())
        if survivor_of is not None:
            for isin, first in list(windows.items()):
                survivor = survivor_of(isin)
                if survivor != isin:
                    windows[survivor] = max(first, windows.get(survivor, first))
        return cls(first_sessions=windows)

    def admits(self, isin: str, trade_date: date) -> bool:
        """False only for a quarantined ISIN's bar dated before its step session."""
        first = self.first_sessions.get(isin)
        return first is None or trade_date >= first

    def sql_admits(self, *, isin_column: str = "isin", date_column: str = "trade_date") -> str:
        """`admits` as a DuckDB boolean expression over the named columns (`TRUE` when empty)."""
        for column in (isin_column, date_column):
            if not _IDENT.fullmatch(column):
                raise ValueError(f"{column!r} is not a plain column reference")
        if not self.first_sessions:
            return "TRUE"
        # Literals, not bound parameters, so the predicate drops into any query or view body.
        # Safe: every key matched ISIN_PATTERN in __post_init__ and every value is a `date`.
        terms = [
            f"({isin_column} <> '{isin}' OR {date_column} >= DATE '{day.isoformat()}')"
            for isin, day in sorted(self.first_sessions.items())
        ]
        return "(" + " AND ".join(terms) + ")"

    def register_raw_view(
        self, con: duckdb.DuckDBPyConnection, *, view: str, data_root: Path | None = None
    ) -> str:
        """`store.l2.register_raw_view`, with the quarantined bars filtered out of `view`."""
        return self._filtered(con, view, _register_raw_view, data_root)

    def register_adjusted_view(
        self, con: duckdb.DuckDBPyConnection, *, view: str, data_root: Path | None = None
    ) -> str:
        """`store.l2.register_adjusted_view`, with the quarantined bars filtered out of `view`."""
        return self._filtered(con, view, _register_adjusted_view, data_root)

    def _filtered(
        self,
        con: duckdb.DuckDBPyConnection,
        view: str,
        register: _Register,
        data_root: Path | None,
    ) -> str:
        if not _IDENT.fullmatch(view):
            raise ValueError(f"{view!r} is not a plain view name")
        base = register(con, view=f"{view}__unquarantined", data_root=data_root)
        columns = {str(row[0]) for row in con.execute(f"DESCRIBE {base}").fetchall()}
        # A cold lake registers an empty relation with no price columns: nothing to withhold.
        where = self.sql_admits() if {"isin", "trade_date"} <= columns else "TRUE"
        con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM {base} WHERE {where}")
        return view


class _Register(Protocol):
    """The shape of the two `store.l2` view registrars this module wraps."""

    def __call__(
        self, con: duckdb.DuckDBPyConnection, *, view: str, data_root: Path | None = None
    ) -> str: ...


@cache
def default_price_quarantine() -> PriceQuarantine:
    """The repo's quarantine, from the curated file, built once per process."""
    return PriceQuarantine.from_manual_actions(default_manual_actions())
