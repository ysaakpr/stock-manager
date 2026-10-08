"""M16.2 — NSE sectoral index levels, point-in-time, and the industry → index mapping table.

The data side of the momentum v2 industry gate (:mod:`backtest.policies.industry_gate`):

* :func:`load_sector_index_map` reads the checked-in, reviewed table
  ``backtest/sector_index_map.yaml`` — which sectoral index each industry of the ratified
  classification maps to, and why; which industries are unmapped, and why — and refuses a table
  that does not cover the classification exactly, or was written against a different one.
* :class:`SectorIndexLevels` holds each mapped index's published closes from ``L1/pr_index_eod``,
  chaining the ids NSE published one series under (the 2015-11-09 CNX → Nifty rename). Its one
  read, :meth:`SectorIndexLevels.level_on_or_before`, answers *as of a decision date*: a row whose
  ``publication_date`` is after that date is never returned, whatever session it is for.
* :meth:`SectorIndexLevels.momentum` turns two such levels into the 6-1 reading the gate ranks,
  dated by the later publication date so ``ctx.pit.admit`` re-checks it.
* :class:`IndustryGatedData` wraps any momentum v2 data source and adds the two reads the gate
  needs, so the ungated path is not touched at all.

What it never does: backfill a series before its first published level, carry a level forward
past :data:`MAX_STALE_DAYS`, or join on a symbol — the classification is keyed by ISIN.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pyarrow.dataset as pads
import yaml

from backtest.policies.industry_gate import SKIP_MONTHS, TOTAL_MONTHS, IndexMomentum
from backtest.policies.momentum_v2 import MomentumV2Data, MomentumV2Record, RegimeReading
from backtest.rails import RATIFIED_SECTOR_SOURCE, SectorMap, ratified_sector_map
from dataplatform.ingest.pr_bundle_l1 import INDEX_EOD_DATASET
from dataplatform.query.pit import Dataset
from dataplatform.store.paths import Layer, layer_root

__all__ = [
    "MAX_STALE_DAYS",
    "SECTOR_INDEX_MAP_PATH",
    "IndustryGatedData",
    "SectorIndexError",
    "SectorIndexLevel",
    "SectorIndexLevels",
    "SectorIndexMap",
    "load_sector_index_map",
]

_REPO_ROOT: Final = Path(__file__).resolve().parent.parent
SECTOR_INDEX_MAP_PATH: Final = Path(__file__).resolve().parent / "sector_index_map.yaml"

#: Calendar days per month for the reference dates: 30, the step ``backtest.run`` uses for its
#: skip-month point, so "one month back" means the same thing for the stock and the sector signal.
_MONTH_DAYS: Final = 30
#: A reference level older than this many calendar days before its reference date is not used: the
#: series has stopped (or not yet started) publishing there. Longest gap in any mapped series in the
#: store is 6 days (Diwali/holiday runs); 10 leaves room without ever bridging a dead series.
MAX_STALE_DAYS: Final = 10


class SectorIndexError(ValueError):
    """The mapping table or the stored levels are inconsistent — refused, never patched over."""


# ── the mapping table ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SectorIndexMap:
    """Industry → sectoral index, each index's published ids, and the ISIN classification.

    ``industry_index`` names every industry of the classification; its value is ``None`` for an
    unmapped one. ``aliases`` maps each index to the ``pr_index_eod`` ids of its one series.
    ``sectors`` is the ratified ISIN → industry classification itself; ``sha256`` is the table
    file's own hash, so a run can record exactly which reviewed table it used.
    """

    industry_index: Mapping[str, str | None]
    aliases: Mapping[str, tuple[str, ...]]
    sectors: SectorMap = field(repr=False)
    sha256: str

    @property
    def indices(self) -> tuple[str, ...]:
        """Every index some industry maps to, sorted."""
        return tuple(sorted(self.aliases))

    @property
    def unmapped_industries(self) -> tuple[str, ...]:
        """Industries whose names the gate never admits, sorted."""
        return tuple(sorted(i for i, idx in self.industry_index.items() if idx is None))

    def index_of(self, isin: str) -> str | None:
        """The index ``isin``'s industry maps to; ``None`` if unmapped or not classified."""
        industry = self.sectors.by_isin.get(isin)
        return None if industry is None else self.industry_index.get(industry)


def load_sector_index_map(
    path: Path = SECTOR_INDEX_MAP_PATH, *, sectors: SectorMap | None = None
) -> SectorIndexMap:
    """Read and verify the reviewed mapping table.

    Refuses (``SectorIndexError``) a table whose classification hash is not the ratified one, an
    industry missing from or unknown to the classification, a mapped index with no aliases, a
    declared index nothing maps to, or one id aliased to two indices. ``sectors`` defaults to the
    ratified classification (hash-verified by ``backtest.rails``).
    """
    raw = path.read_bytes()
    doc: dict[str, Any] = yaml.safe_load(raw)
    classification = sectors if sectors is not None else ratified_sector_map()
    declared = doc["classification"]
    if sectors is None and (
        _REPO_ROOT / declared["source"] != RATIFIED_SECTOR_SOURCE
        or declared["sha256"] != classification.sha256
    ):
        raise SectorIndexError(
            f"{path.name} was written against {declared['source']} ({declared['sha256'][:12]}), "
            f"not the ratified classification ({classification.sha256[:12]}) — re-review it"
        )
    aliases: dict[str, tuple[str, ...]] = {}
    owner: dict[str, str] = {}
    for index, spec in doc["indices"].items():
        ids = tuple(spec["aliases"])
        if not ids:
            raise SectorIndexError(f"index {index!r} declares no pr_index_eod id")
        for index_id in ids:
            if index_id in owner:
                raise SectorIndexError(f"id {index_id!r} aliased to {owner[index_id]} and {index}")
            owner[index_id] = index
        aliases[index] = ids
    industry_index: dict[str, str | None] = {}
    for industry, spec in doc["industries"].items():
        target = spec["index"]
        if not str(spec.get("why", "")).strip():
            raise SectorIndexError(f"industry {industry!r} has no stated reason")
        if target is not None and target not in aliases:
            raise SectorIndexError(f"industry {industry!r} maps to undeclared index {target!r}")
        industry_index[industry] = target
    classified = set(classification.by_isin.values())
    if missing := classified - industry_index.keys():
        raise SectorIndexError(f"industries with no mapping decision: {sorted(missing)}")
    if unknown := industry_index.keys() - classified:
        raise SectorIndexError(f"industries not in the classification: {sorted(unknown)}")
    if idle := aliases.keys() - {i for i in industry_index.values() if i is not None}:
        raise SectorIndexError(f"indices declared but mapped from no industry: {sorted(idle)}")
    return SectorIndexMap(
        industry_index=industry_index,
        aliases=aliases,
        sectors=classification,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


# ── the levels ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SectorIndexLevel:
    """One published close of one sectoral index: its session and when it was published."""

    index: str
    session: date
    publication_date: date
    close: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.close, Decimal):
            raise TypeError("close must be a Decimal — never float (CLAUDE.md)")
        if self.close <= 0:
            raise ValueError(f"{self.index} close on {self.session} must be positive")
        if self.publication_date < self.session:
            raise ValueError(f"{self.index}: published {self.publication_date} before its session")


class SectorIndexLevels:
    """Each mapped index's published closes, read only as of a decision date.

    Built from :class:`SectorIndexLevel` rows (a test) or from the store (:meth:`from_l1`). Refuses
    two rows for one index and session — two aliases overlapping, or a duplicated partition — since
    picking one silently would make the reading depend on read order.
    """

    def __init__(self, levels: Iterable[SectorIndexLevel]) -> None:
        by_index: dict[str, dict[date, SectorIndexLevel]] = {}
        for level in levels:
            series = by_index.setdefault(level.index, {})
            if level.session in series:
                raise SectorIndexError(f"{level.index} has two levels for {level.session}")
            series[level.session] = level
        self._series = {
            index: [series[s] for s in sorted(series)] for index, series in by_index.items()
        }
        self._sessions = {
            index: [level.session for level in rows] for index, rows in self._series.items()
        }

    @classmethod
    def from_l1(
        cls, index_map: SectorIndexMap, *, through: date, data_root: Path | None = None
    ) -> SectorIndexLevels:
        """The mapped indices' rows of ``L1/pr_index_eod`` for sessions on or before ``through``."""
        dataset_dir = layer_root(Layer.L1, data_root=data_root) / INDEX_EOD_DATASET
        if not dataset_dir.is_dir():
            raise SectorIndexError(f"no {INDEX_EOD_DATASET} dataset at {dataset_dir}")
        canonical = {i: index for index, ids in index_map.aliases.items() for i in ids}
        table = pads.dataset(dataset_dir, format="parquet", partitioning="hive").to_table(
            columns=["session", "publication_date", "index_id", "close"],
            filter=(pads.field("index_id").isin(list(canonical)))
            & (pads.field("session") <= through),
        )
        return cls(
            SectorIndexLevel(
                index=canonical[row["index_id"]],
                session=row["session"],
                publication_date=row["publication_date"],
                close=row["close"],
            )
            for row in table.to_pylist()
        )

    def first_session(self, index: str) -> date | None:
        """The first session ``index`` has a level for, or ``None`` if it has none."""
        sessions = self._sessions.get(index)
        return sessions[0] if sessions else None

    def level_on_or_before(
        self, index: str, reference: date, *, as_of: date
    ) -> SectorIndexLevel | None:
        """The latest level for a session on or before ``reference`` *published* by ``as_of``.

        The PIT guard: a row whose ``publication_date`` is after ``as_of`` is refused and the
        search falls back to the previous one. ``None`` when no published level lies within
        :data:`MAX_STALE_DAYS` before ``reference`` — before the series starts, or after it stops.
        """
        sessions = self._sessions.get(index)
        if not sessions:
            return None
        series = self._series[index]
        floor = reference - timedelta(days=MAX_STALE_DAYS)
        position = bisect_right(sessions, reference) - 1
        while position >= 0 and series[position].session >= floor:
            level = series[position]
            if level.publication_date <= as_of:
                return level
            position -= 1
        return None

    def momentum(self, index: str, as_of: date) -> IndexMomentum | None:
        """``index``'s 6-1 return as of ``as_of``, or ``None`` without both published levels.

        Six months is ``6 x 30`` calendar days back and one month ``30``, each resolved to the last
        level published by ``as_of``. An index first published after the six-month reference has
        no reading — it becomes rankable six months after its first level, never by backfill.
        """
        start = self.level_on_or_before(
            index, as_of - timedelta(days=_MONTH_DAYS * TOTAL_MONTHS), as_of=as_of
        )
        end = self.level_on_or_before(
            index, as_of - timedelta(days=_MONTH_DAYS * SKIP_MONTHS), as_of=as_of
        )
        if start is None or end is None:
            return None
        return IndexMomentum(
            index=index,
            momentum=end.close / start.close - Decimal(1),
            start_session=start.session,
            start_level=start.close,
            end_session=end.session,
            end_level=end.close,
            knowable_date=max(start.publication_date, end.publication_date),
        )

    def readings(self, indices: Iterable[str], as_of: date) -> tuple[IndexMomentum, ...]:
        """Every index in ``indices`` with a 6-1 reading as of ``as_of``, in index order."""
        found = (self.momentum(index, as_of) for index in sorted(indices))
        return tuple(reading for reading in found if reading is not None)


# ── the data seam ────────────────────────────────────────────────────────────────────────────────


class IndustryGatedData:
    """A momentum v2 data source plus the industry gate's two reads (``IndustryGateData``).

    Delegates ``is_rebalance`` / ``signal`` / ``regime`` to ``inner`` untouched, so a gated run sees
    exactly the ungated candidate set before the gate narrows it; only constructed for a run with
    ``industry_gate`` on, so an ungated run never loads a level.
    """

    def __init__(
        self, inner: MomentumV2Data, levels: SectorIndexLevels, index_map: SectorIndexMap
    ) -> None:
        self._inner = inner
        self._levels = levels
        self._map = index_map

    def is_rebalance(self, session: date) -> bool:
        return self._inner.is_rebalance(session)

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        return self._inner.signal(as_of)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        return self._inner.regime(as_of)

    def sector_index_momentum(self, as_of: date) -> Dataset[IndexMomentum]:
        return Dataset.declaring(
            f"sector_index_momentum@{as_of.isoformat()}",
            self._levels.readings(self._map.indices, as_of),
            knowable_date=lambda reading: reading.knowable_date,
        )

    def sector_index_of(self, isin: str) -> str | None:
        return self._map.index_of(isin)
