"""A10 · M17.1 — the production :class:`~analyst.commons.sheets.CommonsSource`, over the lake.

Each read goes through the store reader or query-layer surface that already owns that data. No
read fetches, and no read writes:

- **Sessions and raw bars.** L1 ``prices_raw``, NSE only, one series, read per session partition.
  The unsourced-step quarantine (D22, `dataplatform.query.default_price_quarantine`) is applied in
  SQL, the same predicate `backtest.run` uses.
- **Adjusted closes.** The raw close is the base. The L2 back-adjusted close from
  `QueryService.adjusted_series`, pinned to NSE, is laid over it wherever L2 has the session.
  `backtest.run._AdjustedCloseSource` does the same: L2 holds only names with a non-identity
  factor chain, so for every other name the raw close *is* the adjusted close. A factored name
  whose L2 stops before the session is adjusted up to L2's last session. If a corporate action
  went ex after that, it shows in the returns until L2 is rebuilt. That is the lake's state, and
  this reader does not hide it.
- **Index levels, India VIX, the repo rate.** L1 ``macro_series``, with the same rule as
  `macro_series.read_latest`: per observation, the latest release on or before the session. The
  read is pushed down to the requested series ids. An unfiltered `read_latest` decodes every one
  of about a million facts, about 30 s, to keep a handful of series.
- **Surveillance lists and the industry classification.** These are only in L0, as daily
  snapshots (`dataplatform.ingest.daily_snapshot`). The latest snapshot dated on or before the
  session is read through `L0Store.get`, which re-checksums it. The classification is parsed by
  `indices.parse_constituents`, the parser the constituents pipeline uses for this file shape.
- **Filings.** L1 ``pit_fundamentals``, company level, the concepts `compute_metrics` reads, filed
  on or before the session. This is the read `backtest.run._read_pit_facts` makes.
- **Announcements.** L1 ``announcements`` through `announcements.iter_l1`, NSE rows only. Each
  row is dated by its dissemination timestamp in IST.

A source that is absent raises :class:`~analyst.commons.sheets.SourceUnavailableError`, naming
the dataset and the reason. The builder turns that into a gap.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pyarrow.dataset as pads

from analyst.commons.sheets import (
    AdjustedClose,
    AnnouncementRecord,
    EquityBar,
    FilingFact,
    IndexLevel,
    MacroReading,
    SectorAssignment,
    SourceUnavailableError,
    SurveillanceEntry,
)
from dataplatform.clock import IST, Clock
from dataplatform.identity.master import Exchange
from dataplatform.ingest.announcements import ANNOUNCEMENTS_DATASET, iter_l1
from dataplatform.ingest.indices import parse_constituents
from dataplatform.ingest.models import ISIN_PATTERN
from dataplatform.ingest.xbrl.models import Nature
from dataplatform.logging import get_logger
from dataplatform.query import (
    AdjustedSeriesRequest,
    Dataset,
    QueryService,
    default_price_quarantine,
)
from dataplatform.query.fundamentals_metrics import CONCEPTS_USED
from dataplatform.store.l0 import L0Store
from dataplatform.store.l2 import PRICES_ADJUSTED_DATASET, open_connection
from dataplatform.store.macro_series import MACRO_SERIES_DATASET
from dataplatform.store.paths import (
    Layer,
    l2_isin_partition_path,
    layer_root,
    partition_date_of,
)
from dataplatform.store.pit_fundamentals import PIT_FUNDAMENTALS_DATASET
from dataplatform.store.schemas import PRICES_RAW_DATASET

__all__ = ["INDUSTRY_SOURCE", "SURVEILLANCE_SOURCES", "LakeCommonsSource"]

_LOG = get_logger(__name__)

#: The L0 daily-snapshot source holding each surveillance list.
SURVEILLANCE_SOURCES: Final[dict[str, str]] = {
    "ASM": "nse_asm_list",
    "GSM": "nse_gsm_list",
    "ESM": "nse_esm_list",
}
#: NSE's Nifty Total Market constituent list, which carries the industry of every member.
INDUSTRY_SOURCE: Final = "nse_industry_classification"
#: How far back a daily snapshot is looked for. The builder's own staleness rule is tighter.
_SNAPSHOT_SEARCH_DAYS: Final = 14
_NSE_ANNOUNCEMENTS: Final = "nse_announcements"
_ISIN: Final = re.compile(ISIN_PATTERN)


class LakeCommonsSource:
    """`CommonsSource` over the local lake (module docstring).

    ``clock`` exists only because `L0Store` takes one. Nothing here reads it for a date: every
    read is bounded by the session it is asked about.
    """

    def __init__(self, *, clock: Clock, data_root: Path | None = None) -> None:
        self._data_root = data_root
        self._l0 = L0Store(clock=clock, data_root=data_root)
        self._quarantine = default_price_quarantine()
        self._admits = self._quarantine.sql_admits()
        self._con = open_connection()

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> LakeCommonsSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── prices ───────────────────────────────────────────────────────────────────────────────

    def _price_partitions(self, through: date) -> list[tuple[date, Path]]:
        root = layer_root(Layer.L1, data_root=self._data_root) / PRICES_RAW_DATASET
        if not root.is_dir():
            raise SourceUnavailableError(PRICES_RAW_DATASET, f"no dataset at {root}")
        out: list[tuple[date, Path]] = []
        for child in root.iterdir():
            try:
                day = partition_date_of(child)
            except ValueError:
                continue
            part = child / "part.parquet"
            if day <= through and part.is_file():
                out.append((day, part))
        out.sort()
        return out

    def sessions(self, through: date, count: int) -> Dataset[date]:
        partitions = self._price_partitions(through)
        found: list[date] = []
        end = len(partitions)
        # Walk back in chunks. A partition with BSE rows only (the 2006-2011 backfill) is not
        # an NSE session, so the partition count alone cannot be trusted.
        while end > 0 and len(found) < count:
            start = max(0, end - (count - len(found)) - 16)
            files = [str(p) for _, p in partitions[start:end]]
            rows = self._con.execute(
                "SELECT DISTINCT trade_date FROM read_parquet($files) "
                "WHERE exchange = 'NSE' AND series = 'EQ' AND close > 0",
                {"files": files},
            ).fetchall()
            found.extend(row[0] for row in rows)
            end = start
        sessions = sorted(set(found))[-count:]
        if not sessions:
            raise SourceUnavailableError(PRICES_RAW_DATASET, f"no NSE session through {through}")
        return Dataset.declaring("commons.sessions", sessions, knowable_date=lambda d: d)

    def _files_for(self, sessions: Sequence[date]) -> list[str]:
        wanted = set(sessions)
        last = max(wanted)
        return [str(p) for day, p in self._price_partitions(last) if day in wanted]

    def equity_bars(self, sessions: Sequence[date], series: str) -> Dataset[EquityBar]:
        files = self._files_for(sessions)
        if not files:
            raise SourceUnavailableError(PRICES_RAW_DATASET, "no partition for the window")
        rows = self._con.execute(
            "SELECT isin, trade_date, close, total_traded_qty, total_traded_value, deliv_qty, "
            "deliv_pct FROM read_parquet($files) "
            f"WHERE exchange = 'NSE' AND series = $series AND close > 0 AND {self._admits} "
            "ORDER BY isin, trade_date",
            {"files": files, "series": series},
        ).fetchall()
        bars = [
            EquityBar(
                isin=str(isin),
                trade_date=trade_date,
                close=Decimal(close),
                traded_qty=int(qty),
                traded_value=Decimal(value),
                deliv_qty=None if dq is None else int(dq),
                deliv_pct=None if dp is None else Decimal(dp),
            )
            for isin, trade_date, close, qty, value, dq, dp in rows
        ]
        _LOG.info("commons.source.equity_bars", sessions=len(files), bars=len(bars))
        return Dataset.declaring("commons.equity_bars", bars, knowable_date=lambda b: b.trade_date)

    def adjusted_closes(
        self, isins: frozenset[str], sessions: Sequence[date]
    ) -> Dataset[AdjustedClose]:
        files = self._files_for(sessions)
        if not files:
            raise SourceUnavailableError(PRICES_RAW_DATASET, "no partition for the window")
        rows = self._con.execute(
            "SELECT isin, trade_date, close FROM read_parquet($files) "
            f"WHERE exchange = 'NSE' AND close > 0 AND isin IN (SELECT unnest($isins)) "
            f"AND {self._admits} ORDER BY isin, trade_date, series",
            {"files": files, "isins": sorted(isins)},
        ).fetchall()
        closes: dict[tuple[str, date], Decimal] = {}
        for isin, trade_date, close in rows:
            # One NSE close per session: a name that moved series mid-window prints once a day.
            closes.setdefault((str(isin), trade_date), Decimal(close))
        overlaid = 0
        window = set(sessions)
        with QueryService(data_root=self._data_root, con=self._con) as service:
            for isin in sorted(isins):
                partition = l2_isin_partition_path(
                    PRICES_ADJUSTED_DATASET, isin, data_root=self._data_root
                )
                if not partition.exists():
                    continue
                series = service.adjusted_series(
                    AdjustedSeriesRequest(
                        isin=isin, start=min(window), end=max(window), primary=Exchange.NSE
                    )
                )
                for point in series.points:
                    key = (isin, point.trade_date)
                    if point.fell_back or key not in closes:
                        continue
                    closes[key] = point.adj_close
                    overlaid += 1
        _LOG.info("commons.source.adjusted_closes", isins=len(isins), overlaid=overlaid)
        records = [AdjustedClose(isin, day, close) for (isin, day), close in sorted(closes.items())]
        return Dataset.declaring(
            "commons.adjusted_closes", records, knowable_date=lambda r: r.trade_date
        )

    # ── macro: index levels, India VIX, repo rate ────────────────────────────────────────────

    def _macro(self, series_ids: Sequence[str], through: date) -> list[MacroReading]:
        root = layer_root(Layer.L1, data_root=self._data_root) / MACRO_SERIES_DATASET
        if not root.is_dir():
            raise SourceUnavailableError(MACRO_SERIES_DATASET, f"no dataset at {root}")
        table = pads.dataset(root, format="parquet", partitioning="hive").to_table(
            columns=[
                "series_id",
                "period_start",
                "period_end",
                "release_date",
                "revision_seq",
                "value",
            ],
            filter=pads.field("series_id").isin(list(series_ids))
            & (pads.field("release_date") <= through),
        )
        # macro_series.read_latest's rule: per observation, the latest release, then revision.
        latest: dict[tuple[Any, ...], tuple[tuple[date, int], MacroReading]] = {}
        for row in table.to_pylist():
            key = (row["series_id"], row["period_start"], row["period_end"])
            rank = (row["release_date"], row["revision_seq"])
            current = latest.get(key)
            if current is None or rank > current[0]:
                latest[key] = (
                    rank,
                    MacroReading(
                        series_id=row["series_id"],
                        period_end=row["period_end"],
                        value=Decimal(row["value"]),
                        knowable_date=row["release_date"],
                    ),
                )
        return sorted(
            (reading for _, reading in latest.values()),
            key=lambda r: (r.series_id, r.period_end),
        )

    def index_levels(self, series_ids: Sequence[str], through: date) -> Dataset[IndexLevel]:
        levels = [
            IndexLevel(r.series_id, r.period_end, r.value, r.knowable_date)
            for r in self._macro(series_ids, through)
        ]
        return Dataset.declaring(
            "commons.index_levels", levels, knowable_date=lambda r: r.knowable_date
        )

    def macro_readings(self, series_ids: Sequence[str], through: date) -> Dataset[MacroReading]:
        return Dataset.declaring(
            "commons.macro_readings",
            self._macro(series_ids, through),
            knowable_date=lambda r: r.knowable_date,
        )

    # ── L0 daily snapshots ───────────────────────────────────────────────────────────────────

    def _latest_snapshot(self, source: str, through: date) -> tuple[date, str, bytes]:
        refs = list(
            self._l0.iter_refs(
                source, start=through - timedelta(days=_SNAPSHOT_SEARCH_DAYS), end=through
            )
        )
        if not refs:
            raise SourceUnavailableError(
                source, f"no snapshot in the {_SNAPSHOT_SEARCH_DAYS} days to {through}"
            )
        ref = refs[-1]
        return ref.logical_date, ref.filename, self._l0.get(ref)

    def surveillance(self, stage: str, through: date) -> Dataset[SurveillanceEntry]:
        source = SURVEILLANCE_SOURCES.get(stage)
        if source is None:
            raise ValueError(f"unknown surveillance stage {stage!r}")
        listed, filename, payload = self._latest_snapshot(source, through)
        document = json.loads(payload.decode("utf-8"))
        entries = sorted(
            {
                SurveillanceEntry(isin=isin, stage=stage, knowable_date=listed)
                for isin in _surveillance_isins(document, filename=filename)
            },
            key=lambda e: e.isin,
        )
        return Dataset.declaring(
            f"commons.surveillance.{stage}", entries, knowable_date=lambda e: e.knowable_date
        )

    def sectors(self, through: date) -> Dataset[SectorAssignment]:
        listed, filename, payload = self._latest_snapshot(INDUSTRY_SOURCE, through)
        snapshot = parse_constituents(
            payload,
            index_slug="niftytotalmarket",
            index_name="NIFTY TOTAL MARKET",
            as_of=listed,
            filename=filename,
        )
        rows = [SectorAssignment(r.isin, r.industry, listed) for r in snapshot.rows]
        return Dataset.declaring("commons.sectors", rows, knowable_date=lambda r: r.knowable_date)

    # ── filings and announcements ────────────────────────────────────────────────────────────

    def filings(self, through: date) -> Dataset[FilingFact]:
        root = layer_root(Layer.L1, data_root=self._data_root) / PIT_FUNDAMENTALS_DATASET
        if not root.is_dir():
            raise SourceUnavailableError(PIT_FUNDAMENTALS_DATASET, f"no dataset at {root}")
        rows = self._con.execute(
            "SELECT isin, period_start, period_end, filing_date, filing_id, nature, concept, "
            "value FROM read_parquet($glob) WHERE segment IS NULL AND concept IN "
            "(SELECT unnest($concepts)) AND filing_date <= $through "
            "ORDER BY filing_date, isin, concept, filing_id, period_end",
            {
                "glob": str(root / "*" / "*.parquet"),
                "concepts": sorted(CONCEPTS_USED),
                "through": through,
            },
        ).fetchall()
        facts = [
            FilingFact(
                isin=str(isin),
                period_start=period_start,
                period_end=period_end,
                filing_date=filing_date,
                filing_id=str(filing_id),
                nature=Nature(nature),
                concept=str(concept),
                segment=None,
                value=Decimal(value),
            )
            for (
                isin,
                period_start,
                period_end,
                filing_date,
                filing_id,
                nature,
                concept,
                value,
            ) in rows
        ]
        return Dataset.declaring("commons.filings", facts, knowable_date=lambda f: f.filing_date)

    def announcements(self, start: date, through: date) -> Dataset[AnnouncementRecord]:
        root = layer_root(Layer.L1, data_root=self._data_root) / ANNOUNCEMENTS_DATASET
        if not root.is_dir():
            raise SourceUnavailableError(ANNOUNCEMENTS_DATASET, f"no dataset at {root}")
        records = sorted(
            {
                AnnouncementRecord(
                    isin=row.isin,
                    ref=row.source_ref or f"{row.ts.isoformat()}|{row.subject}",
                    knowable_date=row.ts.astimezone(IST).date(),
                )
                for row in iter_l1(start=start, end=through, data_root=self._data_root)
                if row.source == _NSE_ANNOUNCEMENTS
            },
            key=lambda r: (r.isin, r.knowable_date, r.ref),
        )
        return Dataset.declaring(
            "commons.announcements", records, knowable_date=lambda r: r.knowable_date
        )


def _surveillance_isins(document: object, *, filename: str) -> Iterator[str]:
    """The ISINs of an NSE surveillance report: ASM in long/short-term sections, GSM/ESM flat."""
    if isinstance(document, dict):
        sections = [document.get(part, {}) for part in ("longterm", "shortterm")]
        rows: list[Any] = []
        for section in sections:
            data = section.get("data") if isinstance(section, dict) else None
            if not isinstance(data, list):
                raise ValueError(f"{filename}: a surveillance section has no data list")
            rows.extend(data)
    elif isinstance(document, list):
        rows = document
    else:
        raise ValueError(f"{filename}: not a surveillance report")
    unkeyed = 0
    for row in rows:
        isin = row.get("isin") if isinstance(row, dict) else None
        if isinstance(isin, str) and _ISIN.fullmatch(isin):
            yield isin
        else:
            unkeyed += 1
    if unkeyed:
        # A row with no usable ISIN cannot be joined (invariant #2). It is counted, never guessed
        # from its symbol.
        _LOG.warning("commons.source.surveillance_unkeyed", file=filename, rows=unkeyed)
