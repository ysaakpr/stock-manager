"""A10 · M17.1 — the production :class:`~analyst.commons.sheets.CommonsSource`, over the lake.

Each read goes through the store reader or query-layer surface that already owns that data. No
read fetches, and no read writes:

- **Sessions and raw bars.** L1 ``prices_raw``, NSE only, one series, read per session partition.
  The unsourced-step quarantine (D22, `dataplatform.query.default_price_quarantine`) is applied in
  SQL, the same predicate `backtest.run` uses.
- **Adjusted closes.** The raw close is the base. The L2 back-adjusted close from
  `QueryService.adjusted_series`, pinned to NSE, is laid over it wherever L2 has the session.
  `backtest.run._AdjustedCloseSource` does the same: a name with no partition has an identity
  chain, so its raw close *is* its adjusted close.
- **The L2 lag (M17.11).** L2 is extended only by the weekly drain, so it ends days before the
  session. With a corporate-action store wired (``actions``), every split or bonus that went ex
  after a name's last L2 session and is knowable by the window's last session is composed with
  the L2 engine's own factors (`dataplatform.store.l2_overlay`): the raw sessions after L2 and
  the L2 history before them come back as a rebuild will write them. An action the engine cannot
  price (a demerger, a merger or scheme, a rights issue, an unquantified or unreconciled split, a
  split L2's span covers without its factor) is not hidden either: `price_overlay_notes` names
  the ISIN, and the sheets keep it out of the universe for the session. Without a store, the
  lagged sessions are raw, and `price_overlay_notes` says so.
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
  row is dated by its dissemination timestamp in IST. The digests (M17.2) read the same rows with
  their stored text (`announcement_texts`), and every fact of each results filing in a date range
  (`results_filings`). Neither fetches an attachment.

A source that is absent raises :class:`~analyst.commons.sheets.SourceUnavailableError`, naming
the dataset and the reason. The builder turns that into a gap.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pyarrow.dataset as pads

from analyst.commons.digests import AnnouncementText
from analyst.commons.inputs import (
    CorporateActionNotice,
    DealRecord,
    FoReading,
    PriceBandEntry,
    PriceBar,
)
from analyst.commons.sheets import (
    AdjustedClose,
    AnnouncementRecord,
    EquityBar,
    FilingFact,
    IndexLevel,
    MacroReading,
    PriceOverlayNote,
    SectorAssignment,
    SourceUnavailableError,
    SurveillanceEntry,
)
from dataplatform.clock import IST, Clock
from dataplatform.corpactions.manual_actions import default_manual_actions
from dataplatform.identity.master import Exchange
from dataplatform.ingest.announcements import ANNOUNCEMENTS_DATASET, iter_l1
from dataplatform.ingest.indices import parse_constituents
from dataplatform.ingest.models import ISIN_PATTERN
from dataplatform.ingest.nse.deals import DEALS_DATASET
from dataplatform.ingest.pr_bundle_l1 import CA_BROADCASTS_DATASET, load_session_identity
from dataplatform.ingest.xbrl.models import Nature
from dataplatform.logging import get_logger
from dataplatform.query import (
    AdjustedSeriesRequest,
    Dataset,
    QueryService,
    default_price_quarantine,
)
from dataplatform.query.fundamentals_metrics import CONCEPTS_USED
from dataplatform.store.fo_aggregates import UnderlyingKind
from dataplatform.store.fo_aggregates import read_l2 as read_fo_aggregates
from dataplatform.store.l0 import L0Store
from dataplatform.store.l2 import (
    PRICES_ADJUSTED_DATASET,
    AdjustedBar,
    RawBar,
    open_connection,
)
from dataplatform.store.l2_overlay import (
    EXCLUDING_KINDS,
    L2Tail,
    OverlayActionSource,
    OverlayError,
    OverlayEvent,
    OverlayKind,
    composed_in_l2,
    l2_tails,
    overlay_bars,
    plan_events,
    stale_l2_events,
)
from dataplatform.store.macro_series import MACRO_SERIES_DATASET
from dataplatform.store.paths import (
    Layer,
    l2_isin_partition_path,
    layer_root,
    partition_date_of,
)
from dataplatform.store.pit_fundamentals import PIT_FUNDAMENTALS_DATASET
from dataplatform.store.schemas import PRICES_RAW_DATASET

if TYPE_CHECKING:
    from dataplatform.ingest.corp_actions import CorporateAction

__all__ = [
    "INDUSTRY_SOURCE",
    "PRICE_BANDS_SOURCE",
    "SURVEILLANCE_SOURCES",
    "LakeCommonsSource",
]

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
#: The L0 daily snapshot of NSE's per-security operative price band (``sec_list_YYYYMMDD.csv``).
PRICE_BANDS_SOURCE: Final = "nse_price_bands"
_PRICE_BANDS_HEADER: Final = ("Symbol", "Series", "Security Name", "Band", "Remarks")
#: How far back a session's own bhavcopy statement is looked for when a band list is dated on a
#: non-session day.
_IDENTITY_SEARCH_DAYS: Final = 5
_ISIN: Final = re.compile(ISIN_PATTERN)


@dataclass(frozen=True, slots=True)
class _IsinOverlay:
    """One ISIN's corporate-action overlay for one window: L2's tail, what is composed, what is
    flagged. ``applied`` is empty whenever ``flags`` holds an ``UNCOMPUTABLE`` guard failure."""

    tail: L2Tail | None
    applied: tuple[CorporateAction, ...] = ()
    flags: tuple[OverlayEvent, ...] = ()


_NO_OVERLAY: Final = _IsinOverlay(tail=None)


class LakeCommonsSource:
    """`CommonsSource` over the local lake (module docstring).

    ``clock`` exists only because `L0Store` takes one. Nothing here reads it for a date: every
    read is bounded by the session it is asked about. ``actions`` is the corporate-action store
    the L2-lag overlay reads (M17.11); ``None`` leaves the lagged sessions raw.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        data_root: Path | None = None,
        actions: OverlayActionSource | None = None,
    ) -> None:
        self._data_root = data_root
        self._l0 = L0Store(clock=clock, data_root=data_root)
        self._quarantine = default_price_quarantine()
        self._admits = self._quarantine.sql_admits()
        self._con = open_connection()
        self._actions = actions
        self._tails: dict[str, L2Tail | None] = {}
        self._overlays: dict[tuple[str, date, date], _IsinOverlay] = {}

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
            "SELECT isin, trade_date, close, series, open, high, low, total_traded_qty "
            "FROM read_parquet($files) "
            f"WHERE exchange = 'NSE' AND close > 0 AND isin IN (SELECT unnest($isins)) "
            f"AND {self._admits} ORDER BY isin, trade_date, series",
            {"files": files, "isins": sorted(isins)},
        ).fetchall()
        closes: dict[tuple[str, date], Decimal] = {}
        raw: dict[str, dict[date, RawBar]] = {}
        for isin, trade_date, close, series, open_, high, low, qty in rows:
            # One NSE close per session: a name that moved series mid-window prints once a day.
            closes.setdefault((str(isin), trade_date), Decimal(close))
            bar = _raw_bar(str(isin), trade_date, close, open_, high, low, qty)
            # The overlay recomputes from the bar L2 is built from: EQ wins over another series.
            by_day = raw.setdefault(str(isin), {})
            if series == "EQ" or trade_date not in by_day:
                by_day[trade_date] = bar
        window = sorted(set(sessions))
        overlays = self._overlay(isins, window)
        overlaid = adjusted = 0
        with QueryService(data_root=self._data_root, con=self._con) as service:
            for isin in sorted(isins):
                l2 = self._l2_window(service, isin, window)
                for day, held in l2.items():
                    key = (isin, day)
                    if key in closes:
                        closes[key] = held.adj_close
                        overlaid += 1
                state = overlays.get(isin, _NO_OVERLAY)
                if state.applied and isin in raw:
                    for out in overlay_bars(
                        isin, events=state.applied, raw_bars=tuple(raw[isin].values()), l2_bars=l2
                    ):
                        key = (isin, out.trade_date)
                        if key in closes and out.adjusted:
                            closes[key] = out.adj_close
                            adjusted += 1
        _LOG.info(
            "commons.source.adjusted_closes",
            isins=len(isins),
            overlaid=overlaid,
            ca_adjusted_isins=sum(1 for i in isins if overlays.get(i, _NO_OVERLAY).applied),
            ca_adjusted_closes=adjusted,
            ca_flagged=sum(1 for i in isins if overlays.get(i, _NO_OVERLAY).flags),
        )
        records = [AdjustedClose(isin, day, close) for (isin, day), close in sorted(closes.items())]
        return Dataset.declaring(
            "commons.adjusted_closes", records, knowable_date=lambda r: r.trade_date
        )

    # ── the L2-lag corporate-action overlay (M17.11) ─────────────────────────────────────────

    def _l2_window(
        self, service: QueryService, isin: str, window: Sequence[date]
    ) -> dict[date, AdjustedBar]:
        """``isin``'s NSE L2 bars on the window's span (no venue fallback), by date."""
        partition = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=self._data_root)
        if not partition.exists():
            return {}
        series = service.adjusted_series(
            AdjustedSeriesRequest(isin=isin, start=window[0], end=window[-1], primary=Exchange.NSE)
        )
        return {
            p.trade_date: AdjustedBar(
                isin=isin,
                exchange=p.exchange.value,
                trade_date=p.trade_date,
                adj_open=p.adj_open,
                adj_high=p.adj_high,
                adj_low=p.adj_low,
                adj_close=p.adj_close,
                adj_volume=p.adj_volume,
                tr_close=p.tr_close,
                cum_price_factor=p.cum_price_factor,
                cum_qty_factor=p.cum_qty_factor,
            )
            for p in series.points
            if not p.fell_back
        }

    def _overlay(self, isins: Iterable[str], window: Sequence[date]) -> dict[str, _IsinOverlay]:
        """Each ISIN's overlay for the window ``[window[0], window[-1]]``, as of its last session.

        Raises `SourceUnavailableError` when the corporate-action store cannot be read: without
        it no lagged session can be called CA-correct.
        """
        lo, as_of = window[0], window[-1]
        wanted = sorted(set(isins))
        todo = [i for i in wanted if (i, lo, as_of) not in self._overlays]
        if todo:
            unknown = [i for i in todo if i not in self._tails]
            if unknown:
                found = l2_tails(self._con, unknown, data_root=self._data_root)
                for isin in unknown:
                    self._tails[isin] = found.get(isin)
            if self._actions is None:
                for isin in todo:
                    self._overlays[(isin, lo, as_of)] = _IsinOverlay(tail=self._tails[isin])
            else:
                try:
                    store = self._actions.ex_between(lo, as_of)
                except SourceUnavailableError:
                    raise
                except Exception as exc:  # any store failure: the overlay cannot be trusted
                    raise SourceUnavailableError(
                        "corporate_actions", f"the corporate-action store is unreadable: {exc}"
                    ) from exc
                recorded, unreconciled = store.by_isin()
                curated = {
                    row.isin
                    for row in default_manual_actions().actions
                    if lo < row.ex_date <= as_of
                }
                with QueryService(data_root=self._data_root, con=self._con) as service:
                    for isin in todo:
                        has_events = isin in recorded or isin in unreconciled or isin in curated
                        self._overlays[(isin, lo, as_of)] = (
                            self._plan(
                                service,
                                isin,
                                window,
                                recorded.get(isin, []),
                                unreconciled.get(isin, []),
                            )
                            if has_events
                            else _IsinOverlay(tail=self._tails[isin])
                        )
        return {i: self._overlays[(i, lo, as_of)] for i in wanted}

    def _plan(
        self,
        service: QueryService,
        isin: str,
        window: Sequence[date],
        recorded: Sequence[CorporateAction],
        unreconciled: Sequence[CorporateAction],
    ) -> _IsinOverlay:
        lo, as_of = window[0], window[-1]
        tail = self._tails[isin]
        flags: list[OverlayEvent] = []
        applied: tuple[CorporateAction, ...] = ()
        if tail is None or tail.last < as_of:
            after = lo if tail is None else max(lo, tail.last)
            events = plan_events(
                isin, recorded=recorded, unreconciled=unreconciled, after=after, as_of=as_of
            )
            applied = tuple(
                e.action for e in events if e.kind is OverlayKind.APPLIED and e.action is not None
            )
            flags.extend(e for e in events if e.kind in EXCLUDING_KINDS)
            if tail is not None and applied:
                try:
                    composed_in_l2(isin, applied, tail)
                except OverlayError as exc:
                    first = applied[0]
                    flags.append(
                        OverlayEvent(
                            isin=isin,
                            ex_date=first.ex_date,
                            action_type=first.action_type,
                            knowable_date=first.knowable_date,
                            kind=OverlayKind.UNCOMPUTABLE,
                            detail=str(exc),
                        )
                    )
                    applied = ()
        if tail is not None and tail.last > lo:
            # An action ex inside L2's own span, ingested after L2 was built over it, leaves the
            # raw step in L2 until the drain rebuilds it.
            span = plan_events(
                isin,
                recorded=recorded,
                unreconciled=(),
                after=lo,
                as_of=min(tail.last, as_of),
            )
            if any(e.kind is OverlayKind.APPLIED for e in span):
                flags.extend(stale_l2_events(isin, span, self._l2_window(service, isin, window)))
        for flag in flags:
            _LOG.warning(
                "commons.source.ca_flagged",
                isin=isin,
                as_of=as_of.isoformat(),
                ex_date=flag.ex_date.isoformat(),
                action_type=flag.action_type.value,
                kind=flag.kind.value,
                detail=flag.detail[:200],
            )
        return _IsinOverlay(tail=tail, applied=applied, flags=tuple(flags))

    def corporate_action_flags(
        self, isins: frozenset[str], sessions: Sequence[date]
    ) -> dict[str, tuple[OverlayEvent, ...]]:
        """Each of ``isins`` the overlay cannot make CA-correct on the window, with its events.

        The kinds say whether waiting can help: ``UNCOMPUTABLE`` (a curation, a reconciliation
        or a rebuild fixes it) against ``BREAK`` / ``UNPRICED`` (the engine never prices them).
        Raises `SourceUnavailableError` as `price_overlay_notes` does.
        """
        if self._actions is None:
            raise SourceUnavailableError(
                "corporate_actions",
                "no corporate-action store is wired; sessions after L2's last are raw L1 closes",
            )
        if not isins:
            return {}
        overlays = self._overlay(isins, sorted(set(sessions)))
        return {isin: state.flags for isin, state in sorted(overlays.items()) if state.flags}

    def l2_ends(self, isins: frozenset[str]) -> dict[str, date | None]:
        """Each ISIN's last NSE session in L2 (``None``: no partition)."""
        unknown = [i for i in sorted(isins) if i not in self._tails]
        if unknown:
            found = l2_tails(self._con, unknown, data_root=self._data_root)
            for isin in unknown:
                self._tails[isin] = found.get(isin)
        return {
            isin: None if (tail := self._tails[isin]) is None else tail.last
            for isin in sorted(isins)
        }

    def price_overlay_notes(
        self, isins: frozenset[str], sessions: Sequence[date]
    ) -> Dataset[PriceOverlayNote]:
        """What the L2-lag overlay did for each of ``isins`` on the window ``sessions``.

        One ``EXCLUDED`` note per ISIN with a corporate action the overlay cannot price inside
        its window, one ``ADJUSTED`` note per ISIN it composed a factor for, one ``LAGGING`` note
        per ISIN whose L2 ends before the window's last session. Every note is dated that session.
        Raises `SourceUnavailableError` when no corporate-action store is wired or it is
        unreadable: then the lagged sessions are raw, and nothing may call them CA-correct.
        """
        if self._actions is None:
            raise SourceUnavailableError(
                "corporate_actions",
                "no corporate-action store is wired; sessions after L2's last are raw L1 closes",
            )
        if not isins:
            return Dataset.declaring("commons.price_overlay", [], knowable_date=lambda n: n.session)
        window = sorted(set(sessions))
        as_of = window[-1]
        notes: list[PriceOverlayNote] = []
        for isin, state in sorted(self._overlay(isins, window).items()):
            if state.flags:
                reason = "; ".join(f.detail for f in state.flags)
                notes.append(PriceOverlayNote(isin, as_of, PriceOverlayNote.EXCLUDED, reason))
            elif state.applied:
                reason = "; ".join(
                    f"{a.action_type.value} ex {a.ex_date.isoformat()}" for a in state.applied
                )
                notes.append(PriceOverlayNote(isin, as_of, PriceOverlayNote.ADJUSTED, reason))
            if state.tail is None or state.tail.last < as_of:
                last = "no L2 partition" if state.tail is None else f"L2 ends {state.tail.last}"
                notes.append(PriceOverlayNote(isin, as_of, PriceOverlayNote.LAGGING, last))
        return Dataset.declaring("commons.price_overlay", notes, knowable_date=lambda n: n.session)

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

    # ── M17.2: the filing texts the digests read ─────────────────────────────────────────────

    def announcement_texts(self, start: date, through: date) -> Dataset[AnnouncementText]:
        """NSE announcements disseminated in ``[start, through]`` (IST), with their stored text.

        Partitions are chosen by poll date in the same range: a disclosure is polled on or after
        the day it was disseminated, so none dated in the range sits in an earlier partition.
        """
        root = layer_root(Layer.L1, data_root=self._data_root) / ANNOUNCEMENTS_DATASET
        if not root.is_dir():
            raise SourceUnavailableError(ANNOUNCEMENTS_DATASET, f"no dataset at {root}")
        rows: dict[str, AnnouncementText] = {}
        for row in iter_l1(start=start, end=through, data_root=self._data_root):
            day = row.ts.astimezone(IST).date()
            if row.source != _NSE_ANNOUNCEMENTS or not start <= day <= through:
                continue
            ref = row.source_ref or f"{row.ts.isoformat()}|{row.subject}"
            rows.setdefault(
                ref,
                AnnouncementText(
                    isin=row.isin,
                    ref=ref,
                    ts=row.ts,
                    knowable_date=day,
                    category=row.category,
                    subject=row.subject,
                    body=row.body,
                    attachment_ref=row.attachment_ref,
                ),
            )
        records = sorted(rows.values(), key=lambda r: (r.knowable_date, r.ref))
        return Dataset.declaring(
            "commons.announcement_texts", records, knowable_date=lambda r: r.knowable_date
        )

    def results_filings(self, start: date, through: date) -> Dataset[FilingFact]:
        """Every company-level PIT fact filed in ``[start, through]``, every concept."""
        root = layer_root(Layer.L1, data_root=self._data_root) / PIT_FUNDAMENTALS_DATASET
        if not root.is_dir():
            raise SourceUnavailableError(PIT_FUNDAMENTALS_DATASET, f"no dataset at {root}")
        files: list[str] = []
        for child in sorted(root.iterdir()):
            try:
                day = partition_date_of(child)
            except ValueError:
                continue
            if start <= day <= through:
                files.extend(str(p) for p in sorted(child.glob("*.parquet")))
        if not files:
            return Dataset.declaring(
                "commons.results_filings", [], knowable_date=lambda f: f.filing_date
            )
        rows = self._con.execute(
            "SELECT isin, period_start, period_end, filing_date, filing_id, nature, concept, "
            "value FROM read_parquet($files) WHERE segment IS NULL "
            "AND filing_date BETWEEN $start AND $through "
            "ORDER BY filing_date, filing_id, isin, concept, period_end",
            {"files": files, "start": start, "through": through},
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
        return Dataset.declaring(
            "commons.results_filings", facts, knowable_date=lambda f: f.filing_date
        )

    # ── M17.9: what the screens, the dossier and the base-rate table read ────────────────────

    def price_bars(
        self, isins: frozenset[str], sessions: Sequence[date], series: str
    ) -> Dataset[PriceBar]:
        """Adjusted OHLCV in ``series`` for ``isins`` on ``sessions``: raw base, L2 laid over.

        The same rule as :meth:`adjusted_closes`, for the whole bar: the raw L1 bar is the base,
        and where L2 holds the name's NSE bar for the session (not a fallback from another venue)
        its adjusted high, low, close and volume replace the raw ones. Delivered quantity takes
        the bar's quantity factor. A raw bar with no high or low takes its close for both.
        """
        files = self._files_for(sessions)
        if not files:
            raise SourceUnavailableError(PRICES_RAW_DATASET, "no partition for the window")
        if not isins:
            return Dataset.declaring("commons.price_bars", [], knowable_date=lambda b: b.trade_date)
        rows = self._con.execute(
            "SELECT isin, trade_date, high, low, close, total_traded_qty, total_traded_value, "
            "deliv_qty, deliv_pct FROM read_parquet($files) "
            "WHERE exchange = 'NSE' AND series = $series AND close > 0 "
            f"AND isin IN (SELECT unnest($isins)) AND {self._admits} ORDER BY isin, trade_date",
            {"files": files, "series": series, "isins": sorted(isins)},
        ).fetchall()
        base: dict[tuple[str, date], PriceBar] = {}
        for isin, day, high, low, close, qty, value, dq, dp in rows:
            raw_close = Decimal(close)
            key = (str(isin), day)
            base.setdefault(
                key,
                PriceBar(
                    isin=str(isin),
                    trade_date=day,
                    high=Decimal(high) if high is not None and high > 0 else raw_close,
                    low=Decimal(low) if low is not None and low > 0 else raw_close,
                    close=raw_close,
                    volume=Decimal(int(qty)),
                    raw_close=raw_close,
                    traded_value=Decimal(value),
                    size=raw_close * int(qty),
                    deliv_qty=None if dq is None else Decimal(int(dq)),
                    deliv_pct=None if dp is None else Decimal(dp),
                ),
            )
        overlaid = adjusted = 0
        window = sorted(set(sessions))
        # The raw bars, kept before L2 replaces them: the overlay recomputes from these.
        raw: dict[str, list[RawBar]] = {}
        for (isin, day), bar in sorted(base.items()):
            raw.setdefault(isin, []).append(
                RawBar(
                    isin=isin,
                    exchange="NSE",
                    trade_date=day,
                    open=bar.raw_close,
                    high=bar.high,
                    low=bar.low,
                    close=bar.raw_close,
                    volume=int(bar.volume),
                )
            )
        overlays = self._overlay(raw, window)
        with QueryService(data_root=self._data_root, con=self._con) as service:
            for isin in sorted(raw):
                l2 = self._l2_window(service, isin, window)
                state = overlays.get(isin, _NO_OVERLAY)
                replaced: dict[date, tuple[Decimal, Decimal, Decimal, Decimal, Decimal]] = {
                    day: (b.adj_high, b.adj_low, b.adj_close, b.adj_volume, b.cum_qty_factor)
                    for day, b in l2.items()
                }
                overlaid += sum(1 for day in replaced if (isin, day) in base)
                if state.applied:
                    for out in overlay_bars(
                        isin, events=state.applied, raw_bars=raw[isin], l2_bars=l2
                    ):
                        if out.adjusted:
                            replaced[out.trade_date] = (
                                out.adj_high,
                                out.adj_low,
                                out.adj_close,
                                out.adj_volume,
                                out.cum_qty_factor,
                            )
                            adjusted += 1
                for day, (high, low, close, volume, factor) in replaced.items():
                    key = (isin, day)
                    raw_bar = base.get(key)
                    if raw_bar is None:
                        continue
                    base[key] = PriceBar(
                        isin=isin,
                        trade_date=day,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                        raw_close=raw_bar.raw_close,
                        traded_value=raw_bar.traded_value,
                        size=raw_bar.size,
                        deliv_qty=None if raw_bar.deliv_qty is None else raw_bar.deliv_qty * factor,
                        deliv_pct=raw_bar.deliv_pct,
                    )
        _LOG.info(
            "commons.source.price_bars",
            sessions=len(files),
            bars=len(base),
            overlaid=overlaid,
            ca_adjusted_isins=sum(1 for i in raw if overlays.get(i, _NO_OVERLAY).applied),
            ca_adjusted_bars=adjusted,
            ca_flagged=sum(1 for i in raw if overlays.get(i, _NO_OVERLAY).flags),
        )
        return Dataset.declaring(
            "commons.price_bars",
            [base[key] for key in sorted(base)],
            knowable_date=lambda b: b.trade_date,
        )

    def concept_facts(self, concepts: frozenset[str], through: date) -> Dataset[FilingFact]:
        """Every company-level PIT fact of ``concepts`` filed on or before ``through``."""
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
                "concepts": sorted(concepts),
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
        return Dataset.declaring(
            "commons.concept_facts", facts, knowable_date=lambda f: f.filing_date
        )

    def price_bands(self, through: date) -> Dataset[PriceBandEntry]:
        """The newest NSE ``sec_list`` dated on or before ``through``, each row on its ISIN.

        The file names securities by symbol and series. Each is placed on its ISIN by the
        exchange's own bhavcopy statement for the list's date (`load_session_identity`, the path
        the PR-bundle rows take), or, when the list is dated on a non-session day, for the latest
        session before it. A row that statement does not name is counted and logged, never
        guessed.
        """
        listed, filename, payload = self._latest_snapshot(PRICE_BANDS_SOURCE, through)
        identity = None
        for back in range(_IDENTITY_SEARCH_DAYS + 1):
            identity = load_session_identity(
                listed - timedelta(days=back), l0=self._l0, data_root=self._data_root
            )
            if identity is not None:
                break
        if identity is None:
            raise SourceUnavailableError(
                PRICE_BANDS_SOURCE,
                f"no NSE session statement on or before {listed} to place {filename}",
            )
        reader = csv.reader(io.StringIO(payload.decode("utf-8-sig")))
        header = tuple(cell.strip() for cell in next(reader, ()))
        if header != _PRICE_BANDS_HEADER:
            raise SourceUnavailableError(
                PRICE_BANDS_SOURCE, f"{filename}: unexpected header {header}"
            )
        entries: dict[tuple[str, str], PriceBandEntry] = {}
        unresolved = 0
        for row in reader:
            if len(row) < len(_PRICE_BANDS_HEADER):
                continue
            symbol, series, band = row[0].strip(), row[1].strip().upper(), row[3].strip()
            isin = identity.try_resolve(symbol, series, identity.trade_date, exchange=Exchange.NSE)
            if isin is None:
                unresolved += 1
                continue
            band_pct = None if band.lower() == "no band" else Decimal(band)
            entries.setdefault(
                (isin, series),
                PriceBandEntry(isin=isin, series=series, band_pct=band_pct, knowable_date=listed),
            )
        _LOG.info(
            "commons.source.price_bands",
            file=filename,
            listed=listed.isoformat(),
            statement=identity.trade_date.isoformat(),
            entries=len(entries),
            unresolved=unresolved,
        )
        return Dataset.declaring(
            "commons.price_bands",
            [entries[key] for key in sorted(entries)],
            knowable_date=lambda e: e.knowable_date,
        )

    def _l1_partitions(self, dataset: str, start: date, through: date) -> list[str]:
        root = layer_root(Layer.L1, data_root=self._data_root) / dataset
        if not root.is_dir():
            raise SourceUnavailableError(dataset, f"no dataset at {root}")
        files: list[str] = []
        for child in sorted(root.iterdir()):
            try:
                day = partition_date_of(child)
            except ValueError:
                continue
            if start <= day <= through:
                files.extend(str(p) for p in sorted(child.glob("*.parquet")))
        return files

    def deals(self, start: date, through: date) -> Dataset[DealRecord]:
        """Every NSE bulk and block deal traded in ``[start, through]`` with an ISIN."""
        files = self._l1_partitions(DEALS_DATASET, start, through)
        rows = (
            self._con.execute(
                "SELECT isin, deal_type, trade_date, client_name, side, quantity, price "
                "FROM read_parquet($files) WHERE isin IS NOT NULL "
                "AND trade_date BETWEEN $start AND $through "
                "ORDER BY trade_date, isin, deal_type, client_name, side, quantity, price",
                {"files": files, "start": start, "through": through},
            ).fetchall()
            if files
            else []
        )
        records = [
            DealRecord(
                isin=str(isin),
                deal_type=str(kind),
                trade_date=day,
                client_name=str(client or ""),
                side=str(side),
                quantity=int(qty),
                price=Decimal(price),
            )
            for isin, kind, day, client, side, qty, price in rows
        ]
        return Dataset.declaring("commons.deals", records, knowable_date=lambda d: d.trade_date)

    def fo_readings(self, sessions: Sequence[date]) -> Dataset[FoReading]:
        """The L2 ``fo_aggregates`` of every stock underlier with an ISIN, on ``sessions``."""
        readings: list[FoReading] = []
        found = 0
        for day in sorted(set(sessions)):
            try:
                rows = read_fo_aggregates(day, data_root=self._data_root)
            except FileNotFoundError:
                continue
            found += 1
            readings.extend(
                FoReading(
                    isin=row.isin,
                    trade_date=row.trade_date,
                    spot=row.spot,
                    total_oi=row.total_oi,
                    total_oi_change=row.total_oi_change,
                    pcr_oi=row.pcr_oi,
                    rollover_pct=row.rollover_pct,
                )
                for row in rows
                if row.underlying_kind is UnderlyingKind.STOCK and row.isin is not None
            )
        if not found:
            raise SourceUnavailableError("fo_aggregates", "no partition for the sessions asked")
        return Dataset.declaring(
            "commons.fo_readings", readings, knowable_date=lambda r: r.trade_date
        )

    def corporate_actions(self, start: date, through: date) -> Dataset[CorporateActionNotice]:
        """Every corporate-action broadcast (L1 ``pr_ca_broadcasts``) knowable in the range."""
        files = self._l1_partitions(CA_BROADCASTS_DATASET, start, through)
        rows = (
            self._con.execute(
                "SELECT isin, purpose, ex_date, record_date, knowable_date "
                "FROM read_parquet($files) WHERE isin IS NOT NULL "
                "AND knowable_date BETWEEN $start AND $through "
                "ORDER BY knowable_date, isin, purpose, ex_date, record_date",
                {"files": files, "start": start, "through": through},
            ).fetchall()
            if files
            else []
        )
        records = sorted(
            {
                CorporateActionNotice(
                    isin=str(isin),
                    purpose=str(purpose),
                    ex_date=ex_date,
                    record_date=record_date,
                    knowable_date=knowable,
                )
                for isin, purpose, ex_date, record_date, knowable in rows
            },
            key=lambda n: (n.knowable_date, n.isin, n.purpose, n.ex_date or date.min),
        )
        return Dataset.declaring(
            "commons.corporate_actions", records, knowable_date=lambda n: n.knowable_date
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


def _raw_bar(
    isin: str,
    day: date,
    close: Any,
    open_: Any,
    high: Any,
    low: Any,
    qty: Any,
) -> RawBar:
    """An L1 row as the `RawBar` L2 is built from; a missing open, high or low takes the close."""
    price = Decimal(close)

    def leg(value: Any) -> Decimal:
        return Decimal(value) if value is not None and value > 0 else price

    return RawBar(
        isin=isin,
        exchange="NSE",
        trade_date=day,
        open=leg(open_),
        high=leg(high),
        low=leg(low),
        close=price,
        volume=int(qty or 0),
    )
