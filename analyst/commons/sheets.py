"""A10 · M17.1 — the market sheet and the universe sheet, built once per session.

Pre-registration §1 and §4 step 2: every M17 manager reads the same two sheets of facts, built
once per session after EOD is green. This module is the builder. It computes; it never fetches,
writes or judges. Storage is `analyst.commons.store`; the lake reads are behind
:class:`CommonsSource` (`analyst.commons.sources.LakeCommonsSource` is the production one).

**The market sheet.** NIFTY 50 and NIFTY 500 against their 50- and 200-session means. Breadth:
the share of the universe above its own 50-session mean, plus advancers and decliners. India VIX.
The 1/3/6-month returns of the NSE sectoral indices. Delivery-volume anomalies. The RBI policy
repo rate. Every field is optional. A source that is missing, stale (over a week old) or too
short is named in ``gaps`` with the reason, and the field is left ``None``. A level that is recent
but older than the session is kept under its own date, and ``gaps`` says it lags. Nothing is
imputed, carried forward to the session or substituted from another source.

**The universe sheet** (pre-registration §2). It has one row per ISIN that meets all of these:

- prints in the configured NSE series on the decision session;
- has a median traded value over the trailing ``median_lookback_sessions`` sessions at or above
  the floor;
- is not on an excluded surveillance list.

A flagged list (ASM) marks the row and never removes it. Rows are keyed by ISIN only
(invariant #2) and sorted by ISIN.

**Point in time (invariant #7).** Every input crosses
:meth:`dataplatform.query.PitContext.admit` as of the decision session before it is used. That
covers sessions, price bars, adjusted closes, index levels, macro readings, surveillance entries,
the sector classification, filings and announcements. A record knowable after the session raises
:class:`~dataplatform.query.PitError`. The build fails loudly; the row is not filtered out
quietly. `compute_metrics` runs the same refusal again on the filings.

**The interlock (invariant #10).** The injected :class:`~analyst.monitor.interlock.GreenGate` is
asked first. A red verdict raises :class:`CommonsRefusedError`, and so does a gate that cannot be
read: the exception propagates and is never treated as green. A decision session after the
injected clock's date is refused, and so is a session with no price bars. Prices are the sheet's
spine and cannot be a gap.

**Deterministic.** The decimal context is fixed. Every number is quantised to a stated scale.
Rows are ISIN-sorted, and canonical JSON (sorted keys, Decimals as strings) is hashed. The same
lake therefore gives byte-identical sheets and the same ``build_digest``. ``built_at`` comes from
the clock and is outside the digest.

What it never does: read a wall clock, key on a symbol, hold a recommendation or a rank from a
manager, or import `analyst.fundmanager` (`tests/unit/test_commons_isolation.py`).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from itertools import pairwise
from typing import Any, ClassVar, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from analyst.monitor.interlock import GreenGate
from backtest.cap_tiers import (
    CAP_TIER_IDENTITY,
    SIZE_LOOKBACK_SESSIONS,
    is_ranked_equity,
    tier_for_rank,
)
from dataplatform.clock import Clock
from dataplatform.ingest.xbrl.models import Nature
from dataplatform.logging import get_logger
from dataplatform.query import Dataset, PitContext
from dataplatform.query.fundamentals_metrics import FundamentalMetrics, compute_metrics

__all__ = [
    "ADJUSTED_LOOKBACK_SESSIONS",
    "ANNOUNCEMENT_SESSIONS",
    "BREADTH_MEAN_SESSIONS",
    "DELIVERY_ANOMALY_RATIO",
    "INDIA_VIX_SERIES",
    "POLICY_RATE_SERIES",
    "SECTOR_INDEX_SERIES",
    "SHEET_VERSION",
    "TREND_INDEX_SERIES",
    "AdjustedClose",
    "AnnouncementRecord",
    "BreadthReading",
    "CommonsRefusedError",
    "CommonsSheets",
    "CommonsSource",
    "DeliveryAnomalies",
    "DeliveryAnomaly",
    "EquityBar",
    "FilingFact",
    "Gap",
    "IndexLevel",
    "IndexTrend",
    "MacroReading",
    "MarketSheet",
    "PriceOverlayNote",
    "PriceOverlaySource",
    "ScalarReading",
    "SectorAssignment",
    "SectorReturn",
    "SourceUnavailableError",
    "SurveillanceEntry",
    "UniverseParameters",
    "UniverseRow",
    "build_commons_sheets",
    "canonical_bytes",
]

_LOG = get_logger(__name__)

#: Versioned identity of the sheet layout and rules. It is part of the digest, so a changed rule
#: can never reproduce an old build's digest.
SHEET_VERSION: Final = "commons-sheets/2"

#: The decimal context every computation runs in, so a caller's context cannot change a digit.
_CONTEXT: Final = Context(prec=28, rounding=ROUND_HALF_EVEN)
_PRICE: Final = Decimal("0.0001")
_RATIO: Final = Decimal("0.000001")
_MONEY: Final = Decimal("0.01")
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)

#: The macro-store series the market sheet reads (`dataplatform.store.macro_series` ids).
TREND_INDEX_SERIES: Final[tuple[str, ...]] = ("IN.NSE.NIFTY_50.CLOSE", "IN.NSE.NIFTY_500.CLOSE")
INDIA_VIX_SERIES: Final = "IN.NSE.INDIA_VIX.CLOSE"
POLICY_RATE_SERIES: Final = "IN.RBI.POLICY_REPO_RATE.RATE"
#: NSE's sectoral indices. The list is fixed: a sheet whose sector set moved with the data would
#: not be comparable across sessions.
SECTOR_INDEX_SERIES: Final[tuple[str, ...]] = (
    "IN.NSE.NIFTY_AUTO.CLOSE",
    "IN.NSE.NIFTY_BANK.CLOSE",
    "IN.NSE.NIFTY_CHEMICALS.CLOSE",
    "IN.NSE.NIFTY_CONSUMER_DURABLES.CLOSE",
    "IN.NSE.NIFTY_ENERGY.CLOSE",
    "IN.NSE.NIFTY_FINANCIAL_SERVICES.CLOSE",
    "IN.NSE.NIFTY_FMCG.CLOSE",
    "IN.NSE.NIFTY_HEALTHCARE_INDEX.CLOSE",
    "IN.NSE.NIFTY_IT.CLOSE",
    "IN.NSE.NIFTY_MEDIA.CLOSE",
    "IN.NSE.NIFTY_METAL.CLOSE",
    "IN.NSE.NIFTY_OIL_GAS.CLOSE",
    "IN.NSE.NIFTY_PHARMA.CLOSE",
    "IN.NSE.NIFTY_PRIVATE_BANK.CLOSE",
    "IN.NSE.NIFTY_PSU_BANK.CLOSE",
    "IN.NSE.NIFTY_REALTY.CLOSE",
)

#: Index trend windows, in the index's own published sessions.
_TREND_WINDOWS: Final = (50, 200)
#: Sector return look-backs (1, 3 and 6 months), in the index's own published sessions.
_SECTOR_WINDOWS: Final = (("1m", 21), ("3m", 63), ("6m", 126))
#: Universe return look-backs (1, 4, 13 and 52 weeks), in NSE sessions.
_RETURN_WINDOWS: Final = (("1w", 5), ("4w", 20), ("13w", 65), ("52w", 260))
#: Every universe price field fits inside this window: the 52-week return needs the close 260
#: sessions back, so the window is 261 sessions including the decision session.
ADJUSTED_LOOKBACK_SESSIONS: Final = 261
BREADTH_MEAN_SESSIONS: Final = 50
_VOLATILITY_RETURNS: Final = 20
#: The delivery baseline is the median of the preceding 20 sessions' delivered quantity. It needs
#: at least 10 of them to be a baseline.
_DELIVERY_BASELINE_SESSIONS: Final = 20
_DELIVERY_BASELINE_MIN: Final = 10
#: A name's delivered quantity at least this multiple of its baseline is an anomaly.
DELIVERY_ANOMALY_RATIO: Final = Decimal(3)
_DELIVERY_TOP: Final = 20
#: Announcements counted over the last 5 sessions, inclusive of the non-session days between them.
ANNOUNCEMENT_SESSIONS: Final = 5
#: A level, a reading or a surveillance list this many calendar days older than the decision
#: session is stale and becomes a gap. A long-weekend run is at most 4 days.
_STALE_DAYS: Final = 7


# ── errors ───────────────────────────────────────────────────────────────────────────────────────


class CommonsRefusedError(RuntimeError):
    """The session must not be built: red data, a future session, or no prices (invariant #10)."""


class SourceUnavailableError(RuntimeError):
    """A :class:`CommonsSource` read with nothing to return: not built, not captured, unreadable.

    The builder turns it into a named :class:`Gap`. It never becomes an empty answer that reads
    like "nothing happened".
    """

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason


# ── parameters ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UniverseParameters:
    """The universe screen of pre-registration §2, taken as an explicit argument.

    This mirrors `roster.yaml`'s ``universe`` block field for field. The job that builds the sheet
    copies the roster's values in, because this package may not import `analyst.fundmanager`.
    """

    series: str
    min_median_traded_value_inr: Decimal
    median_lookback_sessions: int
    excluded_surveillance: tuple[str, ...]
    flagged_surveillance: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.min_median_traded_value_inr, Decimal):
            raise TypeError("min_median_traded_value_inr must be a Decimal — money is never float")
        if self.min_median_traded_value_inr < _ZERO:
            raise ValueError("min_median_traded_value_inr must be >= 0")
        if not 0 < self.median_lookback_sessions <= SIZE_LOOKBACK_SESSIONS:
            raise ValueError(
                f"median_lookback_sessions must be in 1..{SIZE_LOOKBACK_SESSIONS}, "
                f"got {self.median_lookback_sessions}"
            )
        if not self.series.strip():
            raise ValueError("series must be named")
        if overlap := set(self.excluded_surveillance) & set(self.flagged_surveillance):
            raise ValueError(f"a surveillance stage cannot be excluded and flagged: {overlap}")

    def document(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "min_median_traded_value_inr": str(self.min_median_traded_value_inr),
            "median_lookback_sessions": self.median_lookback_sessions,
            "excluded_surveillance": sorted(self.excluded_surveillance),
            "flagged_surveillance": sorted(self.flagged_surveillance),
        }


# ── inputs: what a CommonsSource returns, every record dated ─────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EquityBar:
    """One raw NSE bar in the universe series. ``close`` is the exchange's close, never adjusted."""

    isin: str
    trade_date: date
    close: Decimal
    traded_qty: int
    traded_value: Decimal
    deliv_qty: int | None
    deliv_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class AdjustedClose:
    """One split/bonus-adjusted close, the basis for every return, mean and volatility."""

    isin: str
    trade_date: date
    close: Decimal


@dataclass(frozen=True, slots=True)
class PriceOverlayNote:
    """What the L2-lag corporate-action overlay did for one ISIN, as of ``session`` (M17.11).

    ``EXCLUDED``: a corporate action inside the window that the L2 engine cannot price, so a
    return read across it would be the raw step; the sheets drop the ISIN for the session.
    ``ADJUSTED``: split/bonus factors composed onto the lagged sessions. ``LAGGING``: L2 ends
    before the session. ``reason`` names the actions or L2's last session.
    """

    EXCLUDED: ClassVar[str] = "EXCLUDED"
    ADJUSTED: ClassVar[str] = "ADJUSTED"
    LAGGING: ClassVar[str] = "LAGGING"

    isin: str
    session: date
    kind: str
    reason: str


@runtime_checkable
class PriceOverlaySource(Protocol):
    """A `CommonsSource` that can say what its adjusted prices did across corporate actions."""

    def price_overlay_notes(
        self, isins: frozenset[str], sessions: Sequence[date]
    ) -> Dataset[PriceOverlayNote]:
        """One dated note per ISIN the L2-lag overlay excluded, adjusted, or found lagging."""


@dataclass(frozen=True, slots=True)
class IndexLevel:
    """One published index close. ``knowable_date`` is the date it was released, not its session."""

    series_id: str
    session: date
    close: Decimal
    knowable_date: date


@dataclass(frozen=True, slots=True)
class MacroReading:
    """One macro observation, such as India VIX or the repo rate, dated by its release."""

    series_id: str
    period_end: date
    value: Decimal
    knowable_date: date


@dataclass(frozen=True, slots=True)
class SurveillanceEntry:
    """One ISIN on one surveillance list (ASM, GSM or ESM), as of the list's own date."""

    isin: str
    stage: str
    knowable_date: date


@dataclass(frozen=True, slots=True)
class SectorAssignment:
    """One ISIN's industry from a dated NSE classification snapshot."""

    isin: str
    sector: str
    knowable_date: date


@dataclass(frozen=True, slots=True)
class FilingFact:
    """A PIT fundamentals fact in the shape `fundamentals_metrics.FactRow` reads."""

    isin: str
    period_start: date | None
    period_end: date
    filing_date: date
    filing_id: str
    nature: Nature
    concept: str
    segment: str | None
    value: Decimal


@dataclass(frozen=True, slots=True)
class AnnouncementRecord:
    """One exchange announcement: ``ref`` is the exchange's own id, ``knowable_date`` its date."""

    isin: str
    ref: str
    knowable_date: date


@runtime_checkable
class CommonsSource(Protocol):
    """Where the builder reads the lake. Every answer is a dated :class:`Dataset`.

    Each read covers sessions on or before the decision session and nothing after. The builder
    re-checks that through the PIT guard anyway. A read with nothing to give raises
    :class:`SourceUnavailableError`. It never returns an empty dataset that stands for "missing".
    """

    def sessions(self, through: date, count: int) -> Dataset[date]:
        """The last ``count`` NSE sessions on or before ``through``, ascending."""

    def equity_bars(self, sessions: Sequence[date], series: str) -> Dataset[EquityBar]:
        """Every NSE bar in ``series`` on ``sessions``."""

    def adjusted_closes(
        self, isins: frozenset[str], sessions: Sequence[date]
    ) -> Dataset[AdjustedClose]:
        """Split/bonus-adjusted closes for ``isins`` on ``sessions``."""

    def index_levels(self, series_ids: Sequence[str], through: date) -> Dataset[IndexLevel]:
        """Published closes of ``series_ids`` for sessions on or before ``through``."""

    def macro_readings(self, series_ids: Sequence[str], through: date) -> Dataset[MacroReading]:
        """Observations of ``series_ids`` released on or before ``through``."""

    def surveillance(self, stage: str, through: date) -> Dataset[SurveillanceEntry]:
        """The latest ``stage`` list (ASM/GSM/ESM) dated on or before ``through``."""

    def sectors(self, through: date) -> Dataset[SectorAssignment]:
        """The latest industry classification dated on or before ``through``."""

    def filings(self, through: date) -> Dataset[FilingFact]:
        """Every company-level PIT fundamentals fact filed on or before ``through``."""

    def announcements(self, start: date, through: date) -> Dataset[AnnouncementRecord]:
        """Every NSE announcement polled in ``[start, through]``."""


# ── outputs: the sheets ──────────────────────────────────────────────────────────────────────────


class _Sheet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Gap(_Sheet):
    """A source the sheet could not use, and why. The field it fed is ``None``."""

    source: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class IndexTrend(_Sheet):
    """An index's latest level against its 50- and 200-session means."""

    series_id: str
    session: date
    close: Decimal
    mean_50: Decimal | None
    mean_200: Decimal | None
    vs_mean_50: Decimal | None
    vs_mean_200: Decimal | None


class SectorReturn(_Sheet):
    """A sectoral index's 1, 3 and 6-month price returns, ending at its latest published level."""

    series_id: str
    session: date
    close: Decimal
    return_1m: Decimal | None
    return_3m: Decimal | None
    return_6m: Decimal | None


class ScalarReading(_Sheet):
    """One dated value, such as India VIX or the repo rate (in percent)."""

    series_id: str
    observed: date
    value: Decimal


class BreadthReading(_Sheet):
    """Universe breadth on the session.

    ``above_mean_share`` is the count above the 50-session mean divided by ``measured``, the
    number of names with a full 50-session window.
    """

    universe: int
    measured: int
    above_mean_50: int
    above_mean_share: Decimal | None
    advancers: int
    decliners: int
    unchanged: int


class DeliveryAnomaly(_Sheet):
    """One universe name whose delivered quantity is far above its own recent median."""

    isin: str
    deliv_qty: int
    baseline_qty: Decimal
    ratio: Decimal


class DeliveryAnomalies(_Sheet):
    """The names at or above the threshold: ``count`` of them, and the largest ``_DELIVERY_TOP``."""

    threshold: Decimal
    measured: int
    count: int
    top: tuple[DeliveryAnomaly, ...]


class MarketSheet(_Sheet):
    """The market-wide facts for one session. Every field is optional (module docstring)."""

    trading_date: date
    index_trends: tuple[IndexTrend, ...]
    breadth: BreadthReading | None
    india_vix: ScalarReading | None
    sector_returns: tuple[SectorReturn, ...]
    delivery_anomalies: DeliveryAnomalies | None
    policy_rate: ScalarReading | None


class UniverseRow(_Sheet):
    """One investable ISIN's facts on the session.

    Prices and returns use adjusted closes. ``close`` is the raw exchange close. Ratios are plain
    fractions (0.05 = 5 %). ``cap_tier`` is the M13 liquidity-rank tier (large/mid/small); it is
    ``None`` past rank 500 or without a full size window. ``asm`` is ``None`` when the ASM list
    was unavailable.
    """

    isin: str
    close: Decimal
    return_1w: Decimal | None
    return_4w: Decimal | None
    return_13w: Decimal | None
    return_52w: Decimal | None
    from_52w_high: Decimal | None
    volatility_20: Decimal | None
    median_traded_value: Decimal
    cap_tier: str | None
    size_rank: int | None
    sector: str | None
    asm: bool | None
    delivery_pct: Decimal | None
    revenue_ttm_growth: Decimal | None
    pat_ttm_growth: Decimal | None
    roe: Decimal | None
    pe_ttm: Decimal | None
    sector_median_pe: Decimal | None
    pe_vs_sector: Decimal | None
    filing_date: date | None
    announcements_5s: int | None


def canonical_bytes(document: Any) -> bytes:
    """Canonical JSON: sorted keys, no whitespace, ASCII. Decimals and dates are strings already."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class CommonsSheets(_Sheet):
    """One session's build: both sheets, their gaps, the parameters, and the digests that name them.

    ``build_digest`` hashes everything except ``built_at``. Two builds of the same lake are
    therefore the same build, and the store records it once.
    """

    trading_date: date
    sheet_version: str
    parameters: dict[str, Any]
    market: MarketSheet
    universe: tuple[UniverseRow, ...]
    gaps: tuple[Gap, ...]
    market_digest: str
    universe_digest: str
    build_digest: str
    built_at: datetime

    @staticmethod
    def digests(
        *,
        trading_date: date,
        sheet_version: str,
        parameters: Mapping[str, Any],
        market: MarketSheet,
        universe: Sequence[UniverseRow],
        gaps: Sequence[Gap],
    ) -> tuple[str, str, str]:
        """``(market_digest, universe_digest, build_digest)`` over the canonical bytes."""
        market_digest = _sha256(canonical_bytes(market.model_dump(mode="json")))
        universe_digest = _sha256(canonical_bytes([r.model_dump(mode="json") for r in universe]))
        build_digest = _sha256(
            canonical_bytes(
                {
                    "trading_date": trading_date.isoformat(),
                    "sheet_version": sheet_version,
                    "parameters": dict(parameters),
                    "market_digest": market_digest,
                    "universe_digest": universe_digest,
                    "gaps": [g.model_dump(mode="json") for g in gaps],
                }
            )
        )
        return market_digest, universe_digest, build_digest

    def verify(self) -> None:
        """Recompute the digests and raise ``ValueError`` if any stored one disagrees."""
        expected = self.digests(
            trading_date=self.trading_date,
            sheet_version=self.sheet_version,
            parameters=self.parameters,
            market=self.market,
            universe=self.universe,
            gaps=self.gaps,
        )
        if expected != (self.market_digest, self.universe_digest, self.build_digest):
            raise ValueError(
                f"commons build {self.trading_date.isoformat()} {self.build_digest[:12]} does "
                f"not reproduce its digests: recomputed {expected[2][:12]}"
            )


# ── the builder ──────────────────────────────────────────────────────────────────────────────────


def build_commons_sheets(
    session: date,
    *,
    source: CommonsSource,
    gate: GreenGate,
    clock: Clock,
    universe: UniverseParameters,
) -> CommonsSheets:
    """Build the market sheet and universe sheet for decision session ``session``.

    What it does: asks the interlock first and refuses a red day. Every input is read through
    ``source`` and admitted through the PIT guard as of ``session``. It then computes both sheets
    and digests them. ``built_at`` comes from ``clock``.
    What it assumes: ``session`` is an NSE session, after that session's EOD.
    What it never does: write anything, read past ``session``, or fill a missing value. A missing
    source becomes a :class:`Gap`. A future-dated record raises
    :class:`~dataplatform.query.PitError`. A red day, a session after ``clock.today()``, or a
    session with no bars raises :class:`CommonsRefusedError`.
    """
    if session > clock.today():
        raise CommonsRefusedError(
            f"{session.isoformat()} is after today ({clock.today().isoformat()}); a session is "
            "built after its EOD, never before"
        )
    verdict = gate(session)
    if not verdict:
        raise CommonsRefusedError(f"data is red for {session.isoformat()}: {verdict.reason}")
    with localcontext(_CONTEXT):
        return _Build(session, source=source, universe=universe).run(clock.now())


class _Build:
    """One build's working state. Each method fills one part, and gaps accrue as it goes."""

    def __init__(
        self, session: date, *, source: CommonsSource, universe: UniverseParameters
    ) -> None:
        self.session = session
        self.source = source
        self.params = universe
        self.pit = PitContext(as_of=session)
        self.gaps: list[Gap] = []
        self._flagged_sets: dict[str, set[str] | None] = {}

    def gap(self, source: str, reason: str) -> None:
        self.gaps.append(Gap(source=source, reason=reason))

    def run(self, built_at: datetime) -> CommonsSheets:
        calendar = self._calendar()
        bars = self._bars(calendar)
        sizes = _size_ranks(bars, calendar)
        floor_window = calendar[-self.params.median_lookback_sessions :]
        medians = _median_traded_value(bars, floor_window)
        priced = {isin for isin, by_date in bars.items() if self.session in by_date}
        liquid = {
            isin
            for isin in priced
            if (median := medians.get(isin)) is not None
            and median >= self.params.min_median_traded_value_inr
        }
        excluded, flagged = self._surveillance()
        members = self._corporate_action_exclusions(sorted(liquid - (excluded or set())), calendar)
        closes = self._adjusted(frozenset(members), calendar)
        sectors = self._sectors()
        metrics = self._fundamentals(members, bars)
        announcements = self._announcements(calendar)

        rows = self._universe_rows(
            members,
            bars=bars,
            closes=closes,
            calendar=calendar,
            medians=medians,
            sizes=sizes,
            sectors=sectors,
            flagged=flagged,
            metrics=metrics,
            announcements=announcements,
        )
        market = MarketSheet(
            trading_date=self.session,
            index_trends=self._index_trends(),
            breadth=_breadth(members, closes, calendar),
            india_vix=self._scalar(INDIA_VIX_SERIES, "india_vix"),
            sector_returns=self._sector_returns(),
            delivery_anomalies=self._delivery(members, bars, calendar),
            policy_rate=self._scalar(POLICY_RATE_SERIES, "policy_rate"),
        )
        gaps = tuple(sorted(self.gaps, key=lambda g: (g.source, g.reason)))
        parameters = {
            "universe": self.params.document(),
            "cap_tier_identity": CAP_TIER_IDENTITY,
            "stale_days": _STALE_DAYS,
            "delivery_anomaly_ratio": str(DELIVERY_ANOMALY_RATIO),
            "sector_index_series": list(SECTOR_INDEX_SERIES),
            "trend_index_series": list(TREND_INDEX_SERIES),
        }
        market_digest, universe_digest, build_digest = CommonsSheets.digests(
            trading_date=self.session,
            sheet_version=SHEET_VERSION,
            parameters=parameters,
            market=market,
            universe=rows,
            gaps=gaps,
        )
        _LOG.info(
            "commons.sheets.built",
            trading_date=self.session.isoformat(),
            universe=len(rows),
            gaps=len(gaps),
            build_digest=build_digest,
        )
        return CommonsSheets(
            trading_date=self.session,
            sheet_version=SHEET_VERSION,
            parameters=parameters,
            market=market,
            universe=rows,
            gaps=gaps,
            market_digest=market_digest,
            universe_digest=universe_digest,
            build_digest=build_digest,
            built_at=built_at,
        )

    # ── reads (each through the PIT guard) ───────────────────────────────────────────────────

    def _calendar(self) -> list[date]:
        try:
            sessions = self.pit.admit(
                self.source.sessions(self.session, ADJUSTED_LOOKBACK_SESSIONS)
            )
        except SourceUnavailableError as exc:
            raise CommonsRefusedError(f"no session calendar: {exc}") from exc
        calendar = sorted(set(sessions))
        if not calendar or calendar[-1] != self.session:
            raise CommonsRefusedError(
                f"{self.session.isoformat()} has no NSE prices in the lake; there is nothing to "
                "build a sheet on"
            )
        return calendar

    def _bars(self, calendar: Sequence[date]) -> dict[str, dict[date, EquityBar]]:
        window = calendar[-SIZE_LOOKBACK_SESSIONS:]
        try:
            records = self.pit.admit(self.source.equity_bars(window, self.params.series))
        except SourceUnavailableError as exc:
            raise CommonsRefusedError(f"no price bars: {exc}") from exc
        out: dict[str, dict[date, EquityBar]] = defaultdict(dict)
        for bar in records:
            seen = out[bar.isin].get(bar.trade_date)
            if seen is not None and seen != bar:
                raise ValueError(f"{bar.isin} has two bars on {bar.trade_date.isoformat()}")
            out[bar.isin][bar.trade_date] = bar
        if not any(self.session in by_date for by_date in out.values()):
            raise CommonsRefusedError(
                f"no {self.params.series} bar prints on {self.session.isoformat()}"
            )
        return dict(out)

    def _adjusted(
        self, members: frozenset[str], calendar: Sequence[date]
    ) -> dict[str, dict[date, Decimal]] | None:
        if not members:
            return {}
        try:
            records = self.pit.admit(self.source.adjusted_closes(members, calendar))
        except SourceUnavailableError as exc:
            self.gap(exc.source, exc.reason)
            return None
        out: dict[str, dict[date, Decimal]] = defaultdict(dict)
        for record in records:
            if record.isin in members:
                out[record.isin][record.trade_date] = record.close
        return dict(out)

    def _corporate_action_exclusions(
        self, members: list[str], calendar: Sequence[date]
    ) -> list[str]:
        """``members`` less every ISIN the price overlay could not make CA-correct (M17.11).

        Each one dropped is a gap naming the action, so no return, mean or screen is read across
        a raw step. The L2 lag itself is one informational gap. A source that cannot report on
        its overlay leaves the members as they are, and says so in a gap.
        """
        if not members or not isinstance(self.source, PriceOverlaySource):
            return members
        try:
            notes = self.pit.admit(self.source.price_overlay_notes(frozenset(members), calendar))
        except SourceUnavailableError as exc:
            self.gap(exc.source, exc.reason)
            return members
        dropped: set[str] = set()
        adjusted = 0
        lagging: list[PriceOverlayNote] = []
        for note in notes:
            if note.kind == PriceOverlayNote.EXCLUDED:
                dropped.add(note.isin)
                self.gap(f"corporate_action:{note.isin}", f"excluded this session: {note.reason}")
            elif note.kind == PriceOverlayNote.ADJUSTED:
                adjusted += 1
            elif note.kind == PriceOverlayNote.LAGGING:
                lagging.append(note)
        if lagging:
            self.gap(
                "prices_adjusted",
                f"L2 ends before {self.session.isoformat()} for {len(lagging)} of {len(members)} "
                f"members; the L1 sessions after it carry the engine's split/bonus factors "
                f"({adjusted} names adjusted, {len(dropped)} excluded)",
            )
        return [m for m in members if m not in dropped]

    def _surveillance(self) -> tuple[set[str] | None, dict[str, bool]]:
        """The excluded ISINs (``None`` if unknown), and whether each flagged list is known."""
        excluded: set[str] | None = set()
        for stage in sorted(self.params.excluded_surveillance):
            entries = self._stage(stage)
            if entries is None:
                excluded = None
            elif excluded is not None:
                excluded |= entries
        if excluded is None:
            self.gap(
                "surveillance",
                "an excluded list is unavailable; GSM/ESM names could not be removed from the "
                "universe this session",
            )
        self._flagged_sets = {
            stage: self._stage(stage) for stage in sorted(self.params.flagged_surveillance)
        }
        return excluded, {s: v is not None for s, v in self._flagged_sets.items()}

    def _stage(self, stage: str) -> set[str] | None:
        try:
            entries = self.pit.admit(self.source.surveillance(stage, self.session))
        except SourceUnavailableError as exc:
            self.gap(f"surveillance:{stage}", exc.reason)
            return None
        if entries and (self.session - max(e.knowable_date for e in entries)).days > _STALE_DAYS:
            self.gap(f"surveillance:{stage}", f"latest list is older than {_STALE_DAYS} days")
            return None
        return {e.isin for e in entries if e.stage == stage}

    def _sectors(self) -> dict[str, str] | None:
        try:
            rows = self.pit.admit(self.source.sectors(self.session))
        except SourceUnavailableError as exc:
            self.gap(exc.source, exc.reason)
            return None
        return {row.isin: row.sector for row in rows}

    def _fundamentals(
        self, members: Sequence[str], bars: Mapping[str, Mapping[date, EquityBar]]
    ) -> dict[str, FundamentalMetrics] | None:
        try:
            facts = self.pit.admit(
                Dataset.declaring(
                    "commons.filings",
                    self.source.filings(self.session).records,
                    knowable_date=lambda fact: fact.filing_date,
                )
            )
        except SourceUnavailableError as exc:
            self.gap(exc.source, exc.reason)
            return None
        wanted = set(members)
        # P/E is struck on the raw close times the filer's share count. Both are in the current
        # share basis, so an adjusted close would mis-state it (`fundamentals_metrics`).
        prices = {isin: bars[isin][self.session].close for isin in wanted}
        return compute_metrics(
            (f for f in facts if f.isin in wanted), as_of=self.session, prices=prices
        )

    def _announcements(self, calendar: Sequence[date]) -> dict[str, int] | None:
        if len(calendar) <= ANNOUNCEMENT_SESSIONS:
            self.gap("announcements", "fewer sessions than the announcement window")
            return None
        start = calendar[-ANNOUNCEMENT_SESSIONS - 1]
        try:
            rows = self.pit.admit(self.source.announcements(start, self.session))
        except SourceUnavailableError as exc:
            self.gap(exc.source, exc.reason)
            return None
        refs: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            if row.knowable_date > start:
                refs[row.isin].add(row.ref)
        return {isin: len(found) for isin, found in refs.items()}

    def _levels(self, series_ids: Sequence[str], label: str) -> dict[str, list[IndexLevel]]:
        try:
            levels = self.pit.admit(self.source.index_levels(series_ids, self.session))
        except SourceUnavailableError as exc:
            self.gap(label, exc.reason)
            return {}
        out: dict[str, dict[date, IndexLevel]] = defaultdict(dict)
        for level in levels:
            if level.session <= self.session:
                out[level.series_id][level.session] = level
        return {sid: [by[s] for s in sorted(by)] for sid, by in out.items()}

    def _fresh_series(
        self, series: dict[str, list[IndexLevel]], series_id: str
    ) -> list[IndexLevel]:
        levels = series.get(series_id, [])
        if not levels:
            self.gap(series_id, "no published level on or before the session")
            return []
        if (self.session - levels[-1].session).days > _STALE_DAYS:
            self.gap(series_id, f"latest level {levels[-1].session.isoformat()} is stale")
            return []
        if levels[-1].session != self.session:
            self.gap(
                series_id,
                f"latest level is {levels[-1].session.isoformat()}, not the session",
            )
        return levels

    def _index_trends(self) -> tuple[IndexTrend, ...]:
        series = self._levels(TREND_INDEX_SERIES, "index_levels")
        out: list[IndexTrend] = []
        for series_id in TREND_INDEX_SERIES:
            levels = self._fresh_series(series, series_id)
            if not levels:
                continue
            closes = [level.close for level in levels]
            means: dict[int, Decimal | None] = {}
            for window in _TREND_WINDOWS:
                means[window] = _mean(closes[-window:]) if len(closes) >= window else None
                if means[window] is None:
                    self.gap(series_id, f"fewer than {window} levels for the {window}-session mean")
            last = closes[-1]
            out.append(
                IndexTrend(
                    series_id=series_id,
                    session=levels[-1].session,
                    close=_q(last, _PRICE),
                    mean_50=_qn(means[50], _PRICE),
                    mean_200=_qn(means[200], _PRICE),
                    vs_mean_50=_qn(_rel(last, means[50]), _RATIO),
                    vs_mean_200=_qn(_rel(last, means[200]), _RATIO),
                )
            )
        return tuple(out)

    def _sector_returns(self) -> tuple[SectorReturn, ...]:
        series = self._levels(SECTOR_INDEX_SERIES, "sector_indices")
        out: list[SectorReturn] = []
        for series_id in SECTOR_INDEX_SERIES:
            levels = self._fresh_series(series, series_id)
            if not levels:
                continue
            last = levels[-1].close
            returns: dict[str, Decimal | None] = {}
            for label, back in _SECTOR_WINDOWS:
                then = levels[-1 - back].close if len(levels) > back else None
                returns[label] = _qn(_rel(last, then), _RATIO)
            out.append(
                SectorReturn(
                    series_id=series_id,
                    session=levels[-1].session,
                    close=_q(last, _PRICE),
                    return_1m=returns["1m"],
                    return_3m=returns["3m"],
                    return_6m=returns["6m"],
                )
            )
        return tuple(out)

    def _scalar(self, series_id: str, label: str) -> ScalarReading | None:
        try:
            readings = self.pit.admit(self.source.macro_readings((series_id,), self.session))
        except SourceUnavailableError as exc:
            self.gap(label, exc.reason)
            return None
        own = [r for r in readings if r.series_id == series_id and r.period_end <= self.session]
        if not own:
            self.gap(label, f"no {series_id} observation on or before the session")
            return None
        latest = max(own, key=lambda r: (r.period_end, r.knowable_date))
        # India VIX is a session level, so a stale one is a gap. The repo rate is a state observed
        # on a capture day and stays in force until the next observation (rbi_rates), so it is
        # never stale by age.
        if label == "india_vix" and (self.session - latest.period_end).days > _STALE_DAYS:
            self.gap(label, f"latest level {latest.period_end.isoformat()} is stale")
            return None
        if label == "india_vix" and latest.period_end != self.session:
            self.gap(label, f"latest level is {latest.period_end.isoformat()}, not the session")
        return ScalarReading(
            series_id=series_id, observed=latest.period_end, value=_q(latest.value, _PRICE)
        )

    def _delivery(
        self,
        members: Sequence[str],
        bars: Mapping[str, Mapping[date, EquityBar]],
        calendar: Sequence[date],
    ) -> DeliveryAnomalies | None:
        prior = calendar[-_DELIVERY_BASELINE_SESSIONS - 1 : -1]
        found: list[DeliveryAnomaly] = []
        measured = 0
        for isin in members:
            today = bars[isin][self.session].deliv_qty
            if today is None:
                continue
            history = [
                q
                for s in prior
                if (b := bars[isin].get(s)) is not None and (q := b.deliv_qty) is not None
            ]
            if len(history) < _DELIVERY_BASELINE_MIN:
                continue
            baseline = _median([Decimal(q) for q in history])
            if baseline <= _ZERO:
                continue
            measured += 1
            ratio = Decimal(today) / baseline
            if ratio >= DELIVERY_ANOMALY_RATIO:
                found.append(
                    DeliveryAnomaly(
                        isin=isin,
                        deliv_qty=today,
                        baseline_qty=_q(baseline, _MONEY),
                        ratio=_q(ratio, _RATIO),
                    )
                )
        if members and measured == 0:
            self.gap("delivery", "no universe name has delivery data with a baseline")
            return None
        found.sort(key=lambda a: (-a.ratio, a.isin))
        return DeliveryAnomalies(
            threshold=DELIVERY_ANOMALY_RATIO,
            measured=measured,
            count=len(found),
            top=tuple(found[:_DELIVERY_TOP]),
        )

    # ── the universe rows ────────────────────────────────────────────────────────────────────

    def _universe_rows(
        self,
        members: Sequence[str],
        *,
        bars: Mapping[str, Mapping[date, EquityBar]],
        closes: Mapping[str, Mapping[date, Decimal]] | None,
        calendar: Sequence[date],
        medians: Mapping[str, Decimal],
        sizes: Mapping[str, int],
        sectors: Mapping[str, str] | None,
        flagged: Mapping[str, bool],
        metrics: Mapping[str, FundamentalMetrics] | None,
        announcements: Mapping[str, int] | None,
    ) -> tuple[UniverseRow, ...]:
        asm_known = flagged.get("ASM", False)
        asm_set = self._flagged_sets.get("ASM") or set()
        pe_by_sector: dict[str, list[Decimal]] = defaultdict(list)
        if metrics is not None and sectors is not None:
            for isin in members:
                pe = _metric(metrics.get(isin), "pe_ttm")
                sector = sectors.get(isin)
                if pe is not None and pe > _ZERO and sector is not None:
                    pe_by_sector[sector].append(pe)
        sector_median = {s: _median(v) for s, v in pe_by_sector.items()}

        rows: list[UniverseRow] = []
        for isin in members:
            bar = bars[isin][self.session]
            own = closes.get(isin) if closes is not None else None
            series = _own_closes(own, calendar) if own is not None else None
            returns: dict[str, Decimal | None] = {}
            for label, back in _RETURN_WINDOWS:
                returns[label] = (
                    _qn(_rel(series[-1][1], _close_at(series, calendar[-1 - back])), _RATIO)
                    if series and len(calendar) > back
                    else None
                )
            high = max((c for _, c in series), default=None) if series else None
            rank = sizes.get(isin)
            tier = tier_for_rank(rank) if rank is not None else None
            fm = metrics.get(isin) if metrics is not None else None
            sector = sectors.get(isin) if sectors is not None else None
            pe = _metric(fm, "pe_ttm")
            peer = sector_median.get(sector) if sector is not None else None
            rows.append(
                UniverseRow(
                    isin=isin,
                    close=_q(bar.close, _PRICE),
                    return_1w=returns["1w"],
                    return_4w=returns["4w"],
                    return_13w=returns["13w"],
                    return_52w=returns["52w"],
                    from_52w_high=(
                        _qn(_rel(series[-1][1], high), _RATIO)
                        if series and len(calendar) >= ADJUSTED_LOOKBACK_SESSIONS
                        else None
                    ),
                    volatility_20=_qn(_volatility(own, calendar), _RATIO) if own else None,
                    median_traded_value=_q(medians[isin], _MONEY),
                    cap_tier=tier.value if tier is not None else None,
                    size_rank=rank,
                    sector=sector,
                    asm=(isin in asm_set) if asm_known else None,
                    delivery_pct=_qn(bar.deliv_pct, _RATIO),
                    revenue_ttm_growth=_qn(_metric(fm, "revenue_ttm_yoy"), _RATIO),
                    pat_ttm_growth=_qn(_metric(fm, "earnings_ttm_yoy"), _RATIO),
                    roe=_qn(_metric(fm, "roe"), _RATIO),
                    pe_ttm=_qn(pe, _RATIO),
                    sector_median_pe=_qn(peer, _RATIO),
                    pe_vs_sector=(
                        _qn(pe / peer, _RATIO)
                        if pe is not None and peer is not None and pe > _ZERO
                        else None
                    ),
                    filing_date=fm.knowable_date if fm is not None else None,
                    announcements_5s=(
                        announcements.get(isin, 0) if announcements is not None else None
                    ),
                )
            )
        return tuple(rows)


# ── pure helpers ─────────────────────────────────────────────────────────────────────────────────


def _q(value: Decimal, scale: Decimal) -> Decimal:
    out = value.quantize(scale)
    return out.copy_abs() if out.is_zero() else out


def _qn(value: Decimal | None, scale: Decimal) -> Decimal | None:
    return None if value is None else _q(value, scale)


def _rel(now: Decimal, then: Decimal | None) -> Decimal | None:
    """``now / then - 1``, or ``None`` without a positive base."""
    if then is None or then <= _ZERO:
        return None
    return now / then - _ONE


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, _ZERO) / Decimal(len(values))


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def _median_disc(values: Sequence[Decimal]) -> Decimal:
    """``quantile_disc(0.5)``: the lower middle observation, the rule the M9.3 floor and M13 use."""
    ordered = sorted(values)
    return ordered[(len(ordered) + 1) // 2 - 1]


def _metric(metrics: FundamentalMetrics | None, name: str) -> Decimal | None:
    if metrics is None:
        return None
    value = getattr(metrics, name)
    return value if isinstance(value, Decimal) else None


def _median_traded_value(
    bars: Mapping[str, Mapping[date, EquityBar]], window: Sequence[date]
) -> dict[str, Decimal]:
    """Each name's median traded value over ``window``, counting only the sessions it traded."""
    out: dict[str, Decimal] = {}
    for isin, by_date in bars.items():
        traded = [b.traded_value for s in window if (b := by_date.get(s)) and b.traded_value > 0]
        if traded:
            out[isin] = _median_disc(traded)
    return out


def _size_ranks(
    bars: Mapping[str, Mapping[date, EquityBar]], calendar: Sequence[date]
) -> dict[str, int]:
    """The M13 size rank: median close x quantity over 126 sessions, among equities printing today.

    This is the same measure and ordering as `backtest.cap_tiers` (``LiquidityRankTiers`` and
    ``assign_tiers``): descending size, ties broken by ISIN. It is recomputed here from the bars
    this build already admitted, so the rank uses exactly the admitted inputs.
    """
    session = calendar[-1]
    window = calendar[-SIZE_LOOKBACK_SESSIONS:]
    sizes: dict[str, Decimal] = {}
    for isin, by_date in bars.items():
        if session not in by_date or not is_ranked_equity(isin):
            continue
        traded = [
            b.close * b.traded_qty
            for s in window
            if (b := by_date.get(s)) and b.close > 0 and b.traded_qty > 0
        ]
        if traded:
            sizes[isin] = _median_disc(traded)
    ordered = sorted(
        ((isin, v) for isin, v in sizes.items() if v > 0), key=lambda item: (-item[1], item[0])
    )
    return {isin: rank for rank, (isin, _) in enumerate(ordered, start=1)}


def _own_closes(
    closes: Mapping[date, Decimal], calendar: Sequence[date]
) -> list[tuple[date, Decimal]] | None:
    """A name's adjusted closes in the window, ascending; ``None`` if it has none today."""
    series = [(s, closes[s]) for s in calendar if s in closes]
    if not series or series[-1][0] != calendar[-1]:
        return None
    return series


def _close_at(series: Sequence[tuple[date, Decimal]], reference: date) -> Decimal | None:
    """The name's last close on or before ``reference``; ``None`` if it had not printed by then."""
    found: Decimal | None = None
    for session, close in series:
        if session > reference:
            break
        found = close
    return found


def _volatility(closes: Mapping[date, Decimal], calendar: Sequence[date]) -> Decimal | None:
    """The sample stdev of the last 20 daily returns. It needs a print on all 21 sessions."""
    window = calendar[-_VOLATILITY_RETURNS - 1 :]
    if len(window) <= _VOLATILITY_RETURNS or any(s not in closes for s in window):
        return None
    prices = [closes[s] for s in window]
    returns = [b / a - _ONE for a, b in pairwise(prices)]
    mean = _mean(returns)
    variance = sum(((r - mean) ** 2 for r in returns), _ZERO) / Decimal(len(returns) - 1)
    return variance.sqrt()


def _breadth(
    members: Sequence[str],
    closes: Mapping[str, Mapping[date, Decimal]] | None,
    calendar: Sequence[date],
) -> BreadthReading | None:
    if closes is None:
        return None
    session = calendar[-1]
    window = calendar[-BREADTH_MEAN_SESSIONS:]
    previous = calendar[-2] if len(calendar) > 1 else None
    measured = above = advancers = decliners = unchanged = 0
    for isin in members:
        own = closes.get(isin, {})
        today = own.get(session)
        if today is None:
            continue
        if len(window) == BREADTH_MEAN_SESSIONS and all(s in own for s in window):
            measured += 1
            if today > _mean([own[s] for s in window]):
                above += 1
        before = own.get(previous) if previous is not None else None
        if before is not None:
            if today > before:
                advancers += 1
            elif today < before:
                decliners += 1
            else:
                unchanged += 1
    return BreadthReading(
        universe=len(members),
        measured=measured,
        above_mean_50=above,
        above_mean_share=_q(Decimal(above) / Decimal(measured), _RATIO) if measured else None,
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
    )
