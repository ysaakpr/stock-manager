"""A10 · M17.7 — the M17 desk's production world: the lake, the listing record, the Commons build.

`backtest.fm_job` runs one session of the desk against an `M17World` and a `CommonsBuilder`;
this module is their production side, read from the local lake exactly as the backtests and the
M15.3 paper session read it:

- `LakeM17World` — the checked-in NSE holiday calendar (not the dates on disk: an order staged on
  today's session must still target tomorrow), the L1 raw bars for fills and marks
  (`backtest.run._L1Reader`/`_L1Market`, the paper session's reader), sectors from the NSE
  industry snapshot, corporate actions from the store, the upper-circuit reader
  (`backtest.fm_circuit.LakeCircuitMarket`), adjusted closes from the query service, the bench's
  TRI from ``L1/benchmark_tri``, and `readiness` — what the session's Commons build would still be
  waiting for: the index levels, L1 for the most liquid names, a corporate-action overlay that
  can price every action on them (`overlay_readiness`), and the bench's TRI. L2's lag behind L1
  is not waited for (M17.11): the Commons compose the lagged actions themselves.
- `LakeDelistedNames` — the `DelistedNames` the marks, the caps and the outcomes read: a name is
  delisted only when the **identity master's listing record** says its listing has ended (every
  exchange delisted it, `store_listing_calendar`); its last trade is its last L1 print on or
  before the session. A listed name that simply did not print is *not* delisted — a gap is a data
  fault and stays loud.
- `LakeCommonsBuilder` — the session's Commons: sheets, shortlist, screens and regime over one
  `LakeCommonsSource`, then filing digests scoped to the screens' names and the shortlist, the
  frozen base-rate table (`base_rates.load_frozen`), and the `ManagerCommons` the managers read.

What it never does: write to the lake, read past the session it is asked about, or build a
broker.
"""

from __future__ import annotations

import functools
import tempfile
import time as walltime
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from analyst.commons import (
    DigestStore,
    FetchError,
    FetchRequest,
    FetchResponse,
    InMemoryCommonsStore,
    InMemoryDigestStore,
    InMemoryShortlistStore,
    LakeCommonsSource,
    SnapshotStore,
    SourceUnavailableError,
    UniverseParameters,
    build_commons_sheets,
    build_digests,
    build_screens,
    build_shortlist,
    digest_for_isins,
)
from analyst.commons import base_rates as base_rate_tables
from analyst.commons.fetch import default_store_root
from analyst.commons.sheets import (
    ADJUSTED_LOOKBACK_SESSIONS,
    INDIA_VIX_SERIES,
    TREND_INDEX_SERIES,
    CommonsSource,
)
from analyst.commons.store import CommonsStore, ShortlistStore
from analyst.fundmanager.books import LastTraded
from analyst.fundmanager.controls import BenchmarkUnavailableError, LakeTriBenchmark
from analyst.fundmanager.job import STREAM_DRY, STREAM_LIVE, StreamJournal
from analyst.fundmanager.mandate import Roster, load_roster
from analyst.fundmanager.runtime import ManagerCommons
from analyst.llm import LLM, StubLLM
from analyst.monitor.interlock import GreenGate, StatusApiGate
from backtest.book_actions import BookActionSource
from backtest.fm_circuit import LakeCircuitMarket
from backtest.fm_job import (
    M17JobError,
    M17SessionResult,
    SessionCommons,
    WaitPolicy,
    desk_kill_switch,
    owed_m17_session,
    run_m17_session,
)
from backtest.paper_session import (
    InMemoryPaperSessionStore,
    PostgresPaperSessionStore,
    RecordingJournal,
)
from backtest.run import _L1Market, _L1Reader
from dataplatform.clock import Clock, SystemClock
from dataplatform.config import LlmProvider, Settings, get_settings
from dataplatform.identity.master import Exchange
from dataplatform.logging import get_logger
from dataplatform.query import AdjustedSeriesRequest, PitContext
from dataplatform.query.universe import ListingCalendar, ListingWindow
from dataplatform.store.l2_overlay import OverlayActionSource, OverlayKind, StoreOverlayActions
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SessionMarket, SlippageModel

if TYPE_CHECKING:
    from dataplatform.ingest.calendar import TradingCalendar
    from dataplatform.query.service import QueryService
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "BENCH_SLUG",
    "M17_DATASETS",
    "READINESS_INDEX_SERIES",
    "READINESS_SAMPLE",
    "LakeCommonsBuilder",
    "LakeDelistedNames",
    "LakeM17World",
    "NoLiveFetcher",
    "cli_run",
    "index_level_gaps",
    "m17_provider",
    "overlay_readiness",
    "production_run",
]

_LOG = get_logger(__name__)

#: The datasets the M17 interlock asks the status API about: the session's NSE bars (fills, marks,
#: the Commons spine) and its delivery file (the sheets' delivery share) — the paper session's
#: interlock plus delivery.
M17_DATASETS: Final[tuple[str, ...]] = ("nse_bhavcopy", "nse_delivery")
#: The bench's TRI slug (`controls.BENCHMARK_INDEX_SLUGS`).
BENCH_SLUG: Final = "nifty500"
#: How many of the session's most liquid names the readiness probe samples (L1 and the overlay).
READINESS_SAMPLE: Final = 10
#: An unknown sector groups under this for the sector cap (conservative: one shared bucket).
UNKNOWN_SECTOR: Final = "UNKNOWN"
#: How far back the fill market's calendar starts before the session (headroom for restored
#: orders); it always runs to the end of the holiday calendar.
_FILL_CALENDAR_BACK: Final = timedelta(days=45)


# ── the listing record ───────────────────────────────────────────────────────────────────────────


class LakeDelistedNames:
    """`DelistedNames` over the identity master's listing windows and the L1 last print.

    What it does: for an ISIN whose listing window says it was delisted on or before the session
    (`ListingWindow.delisted_on`), returns its last NSE print on or before that session — the
    raw close and the adjusted close there. None for a name still listed, or one whose window is
    unknown (a gap, never assumed to be a delisting).
    """

    def __init__(
        self,
        listings: ListingCalendar,
        *,
        reader: _L1Reader,
        adjusted: Callable[[str, date], Decimal | None],
    ) -> None:
        self._windows: dict[str, ListingWindow] = {w.isin: w for w in listings.windows()}
        self._reader = reader
        self._adjusted = adjusted
        self._last: dict[date, Mapping[str, date]] = {}

    def last_traded(self, isin: str, session: date) -> LastTraded | None:
        window = self._windows.get(isin)
        if window is None or window.delisted_on is None or window.delisted_on > session:
            return None
        if session not in self._last:
            self._last[session] = self._reader.last_prints(session)
        day = self._last[session].get(isin)
        if day is None:
            return None
        raw = self._reader.closes_on(day).get(isin)
        if raw is None:
            restricted = self._reader.restricted_close(isin, day)
            raw = None if restricted is None else restricted[1]
        if raw is None:
            return None
        adjusted = self._adjusted(isin, day)
        return LastTraded(session=day, raw_close=raw, adjusted_close=adjusted or raw)


def store_listings(settings: Settings) -> ListingCalendar:
    """The identity master's listing windows (`store_listing_calendar`), read once."""
    from dataplatform.identity.master import IdentityStore
    from dataplatform.query.universe import store_listing_calendar
    from dataplatform.store.db import connection

    with connection(settings) as conn:
        return store_listing_calendar(IdentityStore(conn))


# ── the market a book reads ──────────────────────────────────────────────────────────────────────


class _LakeBookMarket:
    """`BookMarket` over L1: the session's EQ close (a held BE/BZ name's own close), its series,
    the industry sector, and EQ traded values over the calendar's sessions."""

    def __init__(
        self, reader: _L1Reader, calendar: TradingCalendar, sectors: Callable[[], Mapping[str, str]]
    ) -> None:
        self._reader = reader
        self._calendar = calendar
        self._sectors = sectors

    def close(self, isin: str, session: date) -> Decimal | None:
        close = self._reader.closes_on(session).get(isin)
        if close is not None:
            return close
        restricted = self._reader.restricted_close(isin, session)
        return None if restricted is None else restricted[1]

    def series(self, isin: str, session: date) -> str | None:
        if isin in self._reader.closes_on(session):
            return "EQ"
        restricted = self._reader.restricted_close(isin, session)
        return None if restricted is None else restricted[0]

    def sector(self, isin: str) -> str:
        return self._sectors().get(isin) or UNKNOWN_SECTOR

    def traded_values(
        self, isin: str, *, through: date, sessions: int
    ) -> Sequence[tuple[date, Decimal]]:
        days = self._calendar.expected_sessions(through - timedelta(days=sessions * 3), through)
        out: list[tuple[date, Decimal]] = []
        for day in days[-sessions:]:
            bar = self._reader.reference_bars_on(day).get(isin)
            if bar is not None:
                out.append((day, bar.traded_value))
        return out

    def sessions_between(self, start: date, end: date) -> int:
        if end <= start:
            return 0
        return len(self._calendar.expected_sessions(start + timedelta(days=1), end))


class LakeM17World:
    """`backtest.fm_job.M17World` over the local lake (module docstring). Close it when done."""

    def __init__(
        self,
        *,
        data_root: Path,
        settings: Settings | None,
        clock: Clock,
        listings: ListingCalendar | None,
        calendar: TradingCalendar | None = None,
        actions: Callable[[], OverlayActionSource] | None = None,
    ) -> None:
        if calendar is None:
            from dataplatform.ingest.calendar import trading_calendar

            calendar = trading_calendar()
        self._calendar = calendar
        self._sessions = calendar.expected_sessions(calendar.coverage_start, calendar.coverage_end)
        self._data_root = data_root
        self._settings = settings
        self._clock = clock
        self._reader = _L1Reader(data_root=data_root)
        self._service: QueryService | None = None
        self._commons = LakeCommonsSource(clock=clock, data_root=data_root)
        if actions is None and settings is not None:
            actions = functools.partial(StoreOverlayActions, settings)
        self._overlay_actions = actions
        self._sector_map: dict[str, str] | None = None
        self._sector_as_of: date | None = None
        self._actions: BookActionSource | None = None
        self._circuit: LakeCircuitMarket | None = None
        self._delisted = (
            None
            if listings is None
            else LakeDelistedNames(listings, reader=self._reader, adjusted=self.adjusted_close)
        )

    def close(self) -> None:
        self._commons.close()
        self._reader.close()
        if self._service is not None:
            self._service.close()

    # calendar
    def is_session(self, day: date) -> bool:
        return self._calendar.is_session(day)

    def sessions(self, start: date, end: date) -> Sequence[date]:
        return self._calendar.expected_sessions(start, end)

    def previous_session(self, day: date) -> date:
        index = bisect_left(self._sessions, day)
        if index == 0:
            raise M17JobError(f"no session before {day.isoformat()} in the holiday calendar")
        return self._sessions[index - 1]

    def next_session(self, day: date) -> date:
        index = bisect_right(self._sessions, day)
        if index >= len(self._sessions):
            raise M17JobError(
                f"no session after {day.isoformat()}: the holiday calendar ends "
                f"{self._calendar.coverage_end.isoformat()}"
            )
        return self._sessions[index]

    # the books
    def fill_market(self, held: Callable[[], Iterable[str]]) -> SessionMarket:
        today = self._clock.today()
        sessions = self._calendar.expected_sessions(
            today - _FILL_CALENDAR_BACK, self._calendar.coverage_end
        )
        return _L1Market(self._reader, sessions, held=held)

    def _sectors(self) -> Mapping[str, str]:
        today = self._clock.today()
        if self._sector_map is None or self._sector_as_of != today:
            try:
                rows = PitContext(today).admit(self._commons.sectors(today))
            except SourceUnavailableError as exc:
                _LOG.warning("fm_world.sectors_missing", detail=str(exc), state="UNKNOWN")
                rows = ()
            self._sector_map = {r.isin: r.sector for r in rows}
            self._sector_as_of = today
        return self._sector_map

    def book_market(self) -> _LakeBookMarket:
        return _LakeBookMarket(self._reader, self._calendar, self._sectors)

    def circuit(self) -> LakeCircuitMarket:
        if self._circuit is None:
            self._circuit = LakeCircuitMarket(clock=self._clock, data_root=self._data_root)
        return self._circuit

    def corporate_actions(self) -> BookActionSource:
        if self._actions is None:
            from backtest.book_actions import load_store_book_actions

            self._actions = load_store_book_actions(
                data_root=self._data_root, settings=self._settings
            )
        return self._actions

    def delisted(self) -> LakeDelistedNames | None:
        return self._delisted

    def _query(self) -> QueryService:
        if self._service is None:
            from dataplatform.query.service import QueryService

            self._service = QueryService(data_root=self._data_root)
        return self._service

    def adjusted_close(self, isin: str, session: date) -> Decimal | None:
        series = self._query().adjusted_series(
            AdjustedSeriesRequest(isin=isin, start=session, end=session, primary=Exchange.NSE)
        )
        for point in series.points:
            if point.trade_date == session:
                return point.adj_close
        return None

    def bench_levels(self, label: str, *, through: date, method: str | None) -> LakeTriBenchmark:
        return LakeTriBenchmark(label, through=through, method=method, data_root=self._data_root)

    # the data the Commons build waits for
    def readiness(self, session: date) -> tuple[str, ...]:
        missing = list(index_level_gaps(self._commons, session))
        sample = self._reader.most_liquid_on(session, READINESS_SAMPLE)
        if not sample:
            missing.append(f"L1 prices for {session.isoformat()}")
        else:
            index = bisect_right(self._sessions, session)
            window = self._sessions[max(0, index - ADJUSTED_LOOKBACK_SESSIONS) : index]
            if not window or window[-1] != session:
                window = [*window, session]
            # A fresh source and a fresh store read per poll: a reconciliation or a curation
            # that lands mid-wait must end the wait on the next poll, as a landed level does.
            actions = None if self._overlay_actions is None else self._overlay_actions()
            with LakeCommonsSource(
                clock=self._clock, data_root=self._data_root, actions=actions
            ) as probe:
                missing.extend(overlay_readiness(probe, frozenset(sample), window))
        try:
            if (
                LakeTriBenchmark(
                    "nifty500-tri-proxy", through=session, data_root=self._data_root
                ).level(session)
                is None
            ):
                missing.append(f"{BENCH_SLUG} TRI level for {session.isoformat()}")
        except BenchmarkUnavailableError as exc:
            missing.append(f"{BENCH_SLUG} TRI: {exc}")
        return tuple(missing)


def overlay_readiness(
    commons: LakeCommonsSource, sample: frozenset[str], window: Sequence[date]
) -> tuple[str, ...]:
    """The readiness probe's price half, past L1: is the overlay CA-correct for ``sample``?

    A wait reason for each sampled name with a corporate action the overlay could price once
    something lands (``UNCOMPUTABLE``: unquantified terms, an unreconciled split, a rebuild owed),
    and one when the corporate-action store cannot be read at all. Never one for L2's lag: the
    overlay composes the lagged actions, so how far L2 is behind is logged and left to the
    Commons' own informational gap. A demerger or rights issue (``BREAK``/``UNPRICED``) is not a
    wait reason either — the engine never prices one, so no wait ends it; the Commons exclude
    the name for the session instead.
    """
    session = window[-1]
    ends = commons.l2_ends(sample)
    behind = sorted(i for i, last in ends.items() if last is None or last < session)
    if behind:
        _LOG.info(
            "fm_world.l2_lag",
            session=session.isoformat(),
            behind=len(behind),
            sample=len(sample),
            earliest=min((ends[i] or date.min) for i in behind).isoformat(),
            note="informational: the Commons overlay composes the lagged corporate actions",
        )
    try:
        flags = commons.corporate_action_flags(sample, window)
    except SourceUnavailableError as exc:
        return (f"corporate-action overlay ({exc.source}): {exc.reason}",)
    missing: list[str] = []
    for isin, events in flags.items():
        waiting = [e for e in events if e.kind is OverlayKind.UNCOMPUTABLE]
        if waiting:
            missing.append(
                f"corporate action on {isin} the overlay cannot price yet: "
                + "; ".join(e.detail for e in waiting)
            )
        else:
            _LOG.info(
                "fm_world.ca_excluded_name",
                isin=isin,
                session=session.isoformat(),
                detail="; ".join(e.detail for e in events)[:300],
            )
    return tuple(missing)


#: The `macro_series` ids the readiness probe waits for — what `index_close_evening` lands (M17.10).
READINESS_INDEX_SERIES: Final[tuple[str, ...]] = (*TREND_INDEX_SERIES, INDIA_VIX_SERIES)


def index_level_gaps(commons: CommonsSource, session: date) -> tuple[str, ...]:
    """The readiness probe's index half: each awaited series whose level for ``session`` is absent.

    Read through `PitContext(session)`, so a level is satisfied only once it is knowable on the
    session (its `release_date` ≤ the session) and only by a level *for* the session — the evening
    job's release for D satisfies D, and nothing satisfies D before it lands. Re-read on every
    poll; nothing is cached, so a level landed mid-wait ends the wait on the next poll.
    """
    try:
        levels = PitContext(session).admit(commons.index_levels(READINESS_INDEX_SERIES, session))
    except SourceUnavailableError as exc:
        return (f"index levels: {exc}",)
    latest: dict[str, date] = {}
    for level in levels:
        latest[level.series_id] = max(latest.get(level.series_id, level.session), level.session)
    missing: list[str] = []
    for series in READINESS_INDEX_SERIES:
        last = latest.get(series)
        if last is None or last < session:
            missing.append(f"index level {series} (latest {'none' if last is None else last})")
    return tuple(missing)


# ── the Commons ──────────────────────────────────────────────────────────────────────────────────


def universe_parameters(roster: Roster) -> UniverseParameters:
    """The sheets' universe screen, copied field for field from the roster (every manager shares
    one ``universe`` block; a roster where they differ is refused)."""
    floors = {m.universe for m in roster.managers}
    if len(floors) != 1:
        raise M17JobError("the managers' universe blocks differ; one Commons cannot serve them")
    (floor,) = floors
    return UniverseParameters(
        series=floor.series,
        min_median_traded_value_inr=floor.min_median_traded_value_inr,
        median_lookback_sessions=floor.median_lookback_sessions,
        excluded_surveillance=tuple(floor.excluded_surveillance),
        flagged_surveillance=tuple(floor.flagged_surveillance),
    )


@dataclass(slots=True)
class LakeCommonsBuilder:
    """`backtest.fm_job.CommonsBuilder` over the lake (module docstring)."""

    data_root: Path
    gate: GreenGate
    digest_llm: LLM
    digest_store: DigestStore
    commons_store: CommonsStore
    shortlist_store: ShortlistStore
    snapshots: SnapshotStore
    universe: UniverseParameters
    recorded_at: Clock
    digest_model: str
    source: LakeCommonsSource | None = None
    #: A fresh corporate-action store per build (M17.11); ``None`` leaves L2's lag raw, which the
    #: sheets then name in a gap.
    actions: Callable[[], OverlayActionSource] | None = None

    def close(self) -> None:
        if self.source is not None:
            self.source.close()
            self.source = None

    def build(self, session: date, *, clock: Clock) -> SessionCommons:
        timings: dict[str, float] = {}
        mark = walltime.perf_counter()

        def lap(stage: str) -> None:
            nonlocal mark
            now = walltime.perf_counter()
            timings[stage] = round(now - mark, 3)
            mark = now

        self.close()
        source = self.source = LakeCommonsSource(
            clock=clock,
            data_root=self.data_root,
            actions=None if self.actions is None else self.actions(),
        )
        sheets = build_commons_sheets(
            session, source=source, gate=self.gate, clock=clock, universe=self.universe
        )
        self.commons_store.record(sheets, recorded_at=self.recorded_at.now())
        lap("sheets")
        shortlist = build_shortlist(sheets, source=source, clock=clock)
        self.shortlist_store.record(shortlist, recorded_at=self.recorded_at.now())
        lap("shortlist")
        screens = build_screens(sheets, shortlist=shortlist, source=source, clock=clock)
        lap("screens")
        scope = {e.isin for e in shortlist.entries}
        for entries in (screens.s1, screens.s2, screens.s3, screens.s4):
            scope.update(e.isin for e in entries)
        scope.update(e.isin for e in screens.s5)
        run = build_digests(
            session,
            isins=frozenset(scope),
            source=source,
            llm=self.digest_llm,
            store=self.digest_store,
            gate=self.gate,
            clock=clock,
            model=self.digest_model,
        )
        lap("digests")
        table = base_rate_tables.load_frozen(self.data_root)
        lap("base_rates")
        manager = ManagerCommons.from_builds(
            sheets=sheets,
            screens=screens,
            shortlist=shortlist,
            base_rates=table,
            source=source,
            snapshots=self.snapshots,
            cost_model=CostModel(load_rate_card(), account_state="MH"),
            slippage=SlippageModel(),
            digests=functools.partial(
                digest_for_isins,
                session=session,
                source=source,
                llm=self.digest_llm,
                store=self.digest_store,
                gate=self.gate,
                clock=clock,
                model=self.digest_model,
            ),
        )
        gaps = tuple(
            f"{g.source}: {g.reason}" for g in (*sheets.gaps, *shortlist.gaps, *screens.gaps)
        )
        _LOG.info(
            "fm_world.commons_built",
            session=session.isoformat(),
            universe=len(sheets.universe),
            shortlist=len(shortlist.entries),
            digest_scope=len(scope),
            digested=len(run.digested),
            digests_cached=len(run.cached),
            digest_failures=len(run.failures),
            gaps=len(gaps),
            timings=timings,
        )
        return SessionCommons(
            shortlist=shortlist,
            manager=manager,
            sheets=sheets,
            screens=screens,
            gaps=gaps,
            timings=timings,
        )


class NoLiveFetcher:
    """The fetcher of a stub-LLM run: every web request is unfulfilled, loudly, never invented."""

    @property
    def name(self) -> str:
        return "none:stub-run"

    def fetch(self, request: FetchRequest) -> FetchResponse:
        raise FetchError(
            f"no live fetcher in a stub-LLM run; {request.kind.value} {request.target!r} is "
            "unfulfilled"
        )


# ── wiring ───────────────────────────────────────────────────────────────────────────────────────


def m17_provider(settings: Settings, *, stub: bool, memory: bool) -> LlmProvider:
    """The provider the M17 desk's managers, digests and fetcher run on (M17.12).

    What it does: ``--stub-llm`` forces `STUB`; otherwise it is ``settings.m17_llm_provider``
    (``M17_LLM_PROVIDER``, default the Claude CLI) — never the global ``llm_provider``, which
    the older analyst paper paths follow and which stays the stub by default.
    What it never does: let a persisted run decide with the stub. Every manager would fail its
    first call and the session would journal a desk of ``MANAGER_ERROR``s that look like a
    model outage; a non-``--memory`` run that resolves to `STUB` is refused here, before any
    connection opens or anything is journaled.
    """
    provider = LlmProvider.STUB if stub else settings.m17_llm_provider
    if provider is LlmProvider.STUB and not memory:
        how = "--stub-llm" if stub else "M17_LLM_PROVIDER=stub"
        raise M17JobError(
            f"the M17 desk resolved to the stub LLM ({how}) on a persisted run; a stub cannot "
            "decide, so nothing runs. Set M17_LLM_PROVIDER=claude_cli (the default), or rehearse "
            "with --memory"
        )
    return provider


def _llm(settings: Settings, provider: LlmProvider) -> LLM:
    if provider is LlmProvider.STUB:
        return StubLLM()
    from analyst.llm import build_llm

    return build_llm(settings, provider=provider)


def _fetcher(provider: LlmProvider) -> Any:
    if provider is not LlmProvider.CLAUDE_CLI:
        return NoLiveFetcher()
    from analyst.commons.fetch import ClaudeWebFetcher

    return ClaudeWebFetcher()


def _wire(
    *,
    settings: Settings,
    clock: Clock,
    session: date | None,
    dry_run: bool,
    start: date | None,
    memory: bool,
    stub_llm: bool,
    no_wait: bool,
    scratch: Path | None,
    stack: ExitStack,
    entries: Callable[[], Sequence[Any]] | None = None,
) -> M17SessionResult:
    from analyst.fundmanager.digest import DIGEST_DIR

    provider = m17_provider(settings, stub=stub_llm, memory=memory)
    _LOG.info("fm_world.provider", provider=provider.value, memory=memory, dry_run=dry_run)
    roster = load_roster()
    data_root = settings.data_root
    listings: ListingCalendar | None
    try:
        listings = store_listings(settings)
    except Exception as exc:  # the listing record is needed for delisted holdings only
        _LOG.warning("fm_world.listings_unavailable", detail=f"{type(exc).__name__}: {exc}")
        listings = None
    world = LakeM17World(data_root=data_root, settings=settings, clock=clock, listings=listings)
    stack.callback(world.close)
    owed = session or owed_m17_session(world, clock.now())
    if owed is None:
        raise M17JobError(f"no trading session owed at {clock.now().isoformat()}")
    if start is not None and start != owed:
        raise M17JobError(
            f"--start {start.isoformat()} is not the session this run decides ({owed.isoformat()})"
        )
    llm = _llm(settings, provider)
    fetcher = _fetcher(provider)
    gate = StatusApiGate(datasets=M17_DATASETS, clock=clock, settings=settings)
    stream = STREAM_DRY if dry_run else STREAM_LIVE
    if memory:
        root = scratch or Path(tempfile.mkdtemp(prefix="m17-"))
        sink: Any = RecordingJournal()
        store: Any = InMemoryPaperSessionStore()
        digest_store: DigestStore = InMemoryDigestStore()
        commons_store: CommonsStore = InMemoryCommonsStore()
        shortlist_store: ShortlistStore = InMemoryShortlistStore()
        snapshots = SnapshotStore(root / "fetch", clock=clock)
        switch = desk_kill_switch(root, clock=clock)
        digest_dir = root / "digest"
        reader: Callable[[], Sequence[Any]] | None = lambda: list(sink.entries)  # noqa: E731
        conn = None
    else:
        from analyst.commons import (
            PostgresCommonsStore,
            PostgresDigestStore,
            PostgresShortlistStore,
        )
        from analyst.journal import EVIDENCE_DIRNAME, EvidenceStore, Journal
        from dataplatform.store.db import connection

        conn = stack.enter_context(connection(settings))
        sink = Journal(conn, clock=clock, evidence=EvidenceStore(data_root / EVIDENCE_DIRNAME))
        store = PostgresPaperSessionStore(conn)
        digest_store = PostgresDigestStore(conn)
        commons_store = PostgresCommonsStore(conn)
        shortlist_store = PostgresShortlistStore(conn)
        snapshots = SnapshotStore(default_store_root(data_root), clock=clock)
        switch = desk_kill_switch(data_root, clock=clock)
        digest_dir = DIGEST_DIR / "dry" if dry_run else DIGEST_DIR
        journal_for_cases = StreamJournal(sink, stream)
        _ensure_cases(conn, roster, journal_for_cases, clock)
        reader = entries or functools.partial(
            _read_stream, conn, data_root, roster, journal_for_cases
        )
    journal = StreamJournal(sink, stream)
    builder = LakeCommonsBuilder(
        data_root=data_root,
        gate=gate,
        digest_llm=llm,
        digest_store=digest_store,
        commons_store=commons_store,
        shortlist_store=shortlist_store,
        snapshots=snapshots,
        universe=universe_parameters(roster),
        recorded_at=clock,
        digest_model=roster.managers[0].models.digest,
        actions=functools.partial(StoreOverlayActions, settings),
    )
    stack.callback(builder.close)
    result = run_m17_session(
        owed,
        world=world,
        commons=builder,
        store=store,
        journal=journal,
        gate=gate,
        llm=llm,
        fetcher=fetcher,
        clock=clock,
        kill_switch=switch,
        roster=roster,
        start=start is not None,
        wait=WaitPolicy(max_wait=timedelta(0)) if no_wait else WaitPolicy(),
        digest_dir=digest_dir,
        entries_reader=reader,
    )
    if conn is not None:
        conn.commit()
    return result


def _ensure_cases(conn: Any, roster: Roster, journal: StreamJournal, clock: Clock) -> None:
    """Every M17 book's stream is a ``case_`` row (the journal's foreign key): PAPER, made once."""
    now = clock.now()
    for book in roster.books:
        conn.execute(
            "INSERT INTO case_ (case_id, title, state, funding_mode, theme, created_at, "
            "updated_at) VALUES (%s, %s, 'ACTIVE', 'PAPER', 'M17', %s, %s) "
            "ON CONFLICT (case_id) DO NOTHING",
            (
                journal.case_id(book.id),
                f"M17 {book.kind.value.lower()} {book.id} ({journal.stream})",
                now,
                now,
            ),
        )


def _read_stream(
    conn: Any, data_root: Path, roster: Roster, journal: StreamJournal
) -> Sequence[Any]:
    from analyst.journal import EVIDENCE_DIRNAME, EvidenceStore, Journal, JournalFilter

    reader = Journal(conn, evidence=EvidenceStore(data_root / EVIDENCE_DIRNAME))
    entries = [
        e
        for book in roster.books
        for e in reader.entries(JournalFilter(case_id=journal.case_id(book.id)))
    ]
    return sorted(entries, key=lambda e: e.id)


def production_run(
    context: JobContext, *, dry_run: bool, start: date | None, session: date | None
) -> M17SessionResult:
    """The scheduler's run: Postgres journal and ledger, the lake, ``M17_LLM_PROVIDER``'s LLM."""
    with ExitStack() as stack:
        return _wire(
            settings=context.settings,
            clock=context.clock,
            session=session,
            dry_run=dry_run,
            start=start,
            memory=False,
            stub_llm=False,
            no_wait=False,
            scratch=None,
            stack=stack,
        )


def cli_run(
    *,
    session: date | None,
    dry_run: bool,
    start: date | None,
    memory: bool,
    stub_llm: bool,
    no_wait: bool,
    scratch: Path,
) -> M17SessionResult:
    """`backtest.fm_job.main`'s run (see there)."""
    settings = get_settings()
    if not memory and not dry_run and start is None:
        _LOG.info("fm_world.cli_live", note="a live run from a shell")
    with ExitStack() as stack:
        return _wire(
            settings=settings,
            clock=SystemClock(),
            session=session,
            dry_run=dry_run,
            start=start,
            memory=memory,
            stub_llm=stub_llm,
            no_wait=no_wait,
            scratch=scratch,
            stack=stack,
        )
