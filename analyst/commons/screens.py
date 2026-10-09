"""A10 · M17.9 — Commons screens S1-S5, the regime and the exclusions, built once per session.

Pre-registration §8 Amendment 1 (a) and (b); the rules are study §2 (screens), §4 (regime). The
screens list facts and ranks; they never say "buy". Each ranked screen holds at most
:data:`SCREEN_SIZE` names, score descending, ties broken by ISIN. Every feature is defined once in
`analyst.commons.features`, so the base-rate table evaluates exactly these rules on history.

**S1 Trend leaders.** Close > SMA200, the SMA200 rising over 21 sessions (above the SMA200 of 21
sessions earlier), and close >= 0.85 x the 52-week closing high. Score: the mean of two
cross-sectional z-scores, of ``ret_6_1 / vol_252`` and of ``ret_12_1 / vol_252`` (the Nifty
Momentum 30 construction; z-scores over every eligible name where both are defined, population
stdev), plus :data:`S1_SECTOR_BONUS` for a name whose industry's median ``ret_6_1`` ranks in the
top half of industries. The study names a bonus and not its size; 0.5 z is this module's choice,
stated here and frozen in the rule hash.

**S2 Volume breakout.** The close is the highest close of the last 60 sessions; the session's
volume is at least 2 x the median volume of the 50 sessions before it; the 5-session return is
below 15 %; and ATR(10) / ATR(50), both struck on the session before, is below 0.8. Score: the
volume ratio.

**S3 Pullback in a leader.** ``ret_6_1`` in the top quintile of eligible names (percentile rank
>= 0.8) and close > SMA200; the highest close of the last 20 sessions was set 3-10 sessions ago and
the close is 4-12 % below it; the mean volume since that high is below the 20-session median
volume; and the close is within 3 % of its SMA20 or its SMA50. Score: ``ret_6_1``.

**S4 Earnings momentum.** The latest quarter's day 0 (the first session on or after its first
filing, PIT XBRL) is within the last 30 sessions, the session included; its M16.3 SUE is in the top
quintile of eligible names with a SUE; the earnings-announcement return over [-1, +1] minus NIFTY
500's is positive; and day 0's volume is at least 2 x the median of the 50 sessions before it.
Score: the SUE percentile plus the EAR percentile, both ranked among the names that pass.

**S5 Event watch.** Unranked facts about eligible names knowable in the last
:data:`S5_EVENT_SESSIONS` sessions: bulk and block deals (client, side, quantity, price and the
share of equity, from the latest PIT share count), and announcements the frozen event-watch
table matches (`analyst.commons.events`). A results board-meeting intimation is listed when the
meeting it names falls in the next :data:`S5_MEETING_SESSIONS` sessions of the published NSE
calendar, or, when it names no date, when it was itself made in the last 5 sessions.

**The universe** is the M17.1 sheet's, less the Amendment 1 (b) exclusions
(`analyst.commons.exclusions`). ``buys_blocked`` carries the stale-list rule to M17.4.
The composite shortlist is unchanged and is not re-read here.

**Point in time (invariant #7).** Every read crosses :meth:`PitContext.admit` as of the session:
the calendar, the bars, NIFTY 500, the filings, the announcements, the surveillance lists, the
band list and the deals. A future-dated record raises :class:`~dataplatform.query.PitError`.

**Frozen.** :data:`SCREENS_RULE_HASH` is the sha256 of the rules' source, the features', the
regime's and the exclusions' source, the constants, and the keyword file's digest.

What it never does: read a wall clock, key on a symbol, hold a manager's view, or import
`analyst.fundmanager`.
"""

from __future__ import annotations

import hashlib
import inspect
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from analyst.commons import exclusions as _exclusions
from analyst.commons import features as _features
from analyst.commons import regime as _regime
from analyst.commons.digests import AnnouncementText
from analyst.commons.events import (
    EVENT_KEYWORDS_DIGEST,
    KeywordTable,
    classify,
    load_event_keywords,
    meeting_date,
    subject_line,
)
from analyst.commons.exclusions import Exclusions, compute_exclusions
from analyst.commons.features import (
    WINDOW_SESSIONS,
    EarningsFeatures,
    NameFeatures,
    align,
    earnings_features,
    name_features,
)
from analyst.commons.inputs import DealRecord, PriceBandEntry, ScreenSource
from analyst.commons.regime import REGIME_INDEX_SERIES, RegimeIndex, RegimeReading
from analyst.commons.sheets import (
    CommonsRefusedError,
    CommonsSheets,
    FilingFact,
    Gap,
    SourceUnavailableError,
    SurveillanceEntry,
    canonical_bytes,
)
from analyst.commons.shortlist import Shortlist, percentile_ranks
from backtest.policies.earnings_surprise import EarningsSurprisePanel
from dataplatform.clock import Clock
from dataplatform.ingest.calendar import CalendarError, trading_calendar
from dataplatform.logging import get_logger
from dataplatform.query import Dataset, PitContext

__all__ = [
    "S1_NEAR_HIGH_MIN",
    "S1_SECTOR_BONUS",
    "S2_ATR_CONTRACTION_MAX",
    "S2_RET5_MAX",
    "S2_VOLUME_RATIO_MIN",
    "S3_DAYS_MAX",
    "S3_DAYS_MIN",
    "S3_MOMENTUM_QUANTILE",
    "S3_PULLBACK_MAX",
    "S3_PULLBACK_MIN",
    "S3_PULLBACK_VOLUME_MAX",
    "S3_SMA_BAND",
    "S4_DAY0_VOLUME_MIN",
    "S4_EAR_MIN",
    "S4_RESULTS_SESSIONS",
    "S4_SUE_QUANTILE",
    "S5_EVENT_SESSIONS",
    "S5_MEETING_SESSIONS",
    "SCREENS_RULE_HASH",
    "SCREENS_VERSION",
    "SCREEN_SIZE",
    "CommonsScreens",
    "EventFact",
    "EventKind",
    "Screen",
    "ScreenEntry",
    "build_screens",
    "latest_share_counts",
    "s1_trend_leaders",
    "s2_volume_breakout",
    "s3_pullback",
    "s4_earnings_momentum",
    "screens_rule_source",
]

_LOG = get_logger(__name__)

SCREENS_VERSION: Final = "commons-screens/1"
SCREEN_SIZE: Final = 15

S1_NEAR_HIGH_MIN: Final = Decimal("0.85")
S1_SECTOR_BONUS: Final = Decimal("0.5")
S2_VOLUME_RATIO_MIN: Final = Decimal(2)
S2_RET5_MAX: Final = Decimal("0.15")
S2_ATR_CONTRACTION_MAX: Final = Decimal("0.8")
S3_MOMENTUM_QUANTILE: Final = Decimal("0.8")
S3_PULLBACK_MIN: Final = Decimal("0.04")
S3_PULLBACK_MAX: Final = Decimal("0.12")
S3_DAYS_MIN: Final = 3
S3_DAYS_MAX: Final = 10
S3_PULLBACK_VOLUME_MAX: Final = Decimal(1)
S3_SMA_BAND: Final = Decimal("0.03")
S4_RESULTS_SESSIONS: Final = 30
S4_SUE_QUANTILE: Final = Decimal("0.8")
S4_EAR_MIN: Final = Decimal(0)
S4_DAY0_VOLUME_MIN: Final = Decimal(2)
S5_EVENT_SESSIONS: Final = 5
S5_MEETING_SESSIONS: Final = 10
#: How far ahead the published calendar is read: the integrity window's last session.
_FUTURE_SESSIONS: Final = _exclusions.INTEGRITY_EXCLUSION_SESSIONS

_CONTEXT: Final = Context(prec=28, rounding=ROUND_HALF_EVEN)
_Q: Final = Decimal("0.00000001")
_ZERO: Final = Decimal(0)
_HALF: Final = Decimal("0.5")
_TWO: Final = Decimal(2)
_PCT: Final = Decimal(100)


class Screen(StrEnum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScreenEntry(_Model):
    """One name on a ranked screen: its place and the score that put it there."""

    position: int = Field(ge=1)
    isin: str
    score: Decimal


# ── the rules: pure functions over features, hashed ──────────────────────────────────────────────


def _top(scores: Mapping[str, Decimal], size: int) -> tuple[ScreenEntry, ...]:
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:size]
    return tuple(
        ScreenEntry(position=k, isin=isin, score=score.quantize(_Q))
        for k, (isin, score) in enumerate(ordered, start=1)
    )


def _zscores(values: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Cross-sectional z-scores, population stdev. All 0 for one name or no spread: no signal."""
    n = len(values)
    if n == 0:
        return {}
    mean = sum(values.values(), _ZERO) / Decimal(n)
    spread = (sum(((v - mean) ** 2 for v in values.values()), _ZERO) / Decimal(n)).sqrt()
    if spread == _ZERO:
        return dict.fromkeys(values, _ZERO)
    return {isin: (v - mean) / spread for isin, v in values.items()}


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / _TWO


def s1_trend_leaders(
    features: Sequence[NameFeatures],
    sectors: Mapping[str, str | None],
    *,
    size: int = SCREEN_SIZE,
) -> tuple[ScreenEntry, ...]:
    """S1 over ``features`` (the eligible names), with ``sectors`` for the industry bonus."""
    with localcontext(_CONTEXT):
        legs6: dict[str, Decimal] = {}
        legs12: dict[str, Decimal] = {}
        for f in features:
            if f.ret_6_1 is None or f.ret_12_1 is None or f.vol_252 is None or f.vol_252 <= _ZERO:
                continue
            legs6[f.isin] = f.ret_6_1 / f.vol_252
            legs12[f.isin] = f.ret_12_1 / f.vol_252
        z6, z12 = _zscores(legs6), _zscores(legs12)
        by_sector: dict[str, list[Decimal]] = defaultdict(list)
        for f in features:
            sector = sectors.get(f.isin)
            if sector is not None and f.ret_6_1 is not None:
                by_sector[sector].append(f.ret_6_1)
        sector_rank = percentile_ranks({s: _median(v) for s, v in by_sector.items()})
        scores: dict[str, Decimal] = {}
        for f in features:
            if f.isin not in z6 or f.isin not in z12:
                continue
            if f.sma200 is None or f.sma200_prev is None or f.near_high is None:
                continue
            if not (
                f.close > f.sma200 and f.sma200 > f.sma200_prev and f.near_high >= S1_NEAR_HIGH_MIN
            ):
                continue
            sector = sectors.get(f.isin)
            bonus = (
                S1_SECTOR_BONUS
                if sector is not None and sector_rank.get(sector, _ZERO) >= _HALF
                else _ZERO
            )
            scores[f.isin] = (z6[f.isin] + z12[f.isin]) / _TWO + bonus
        return _top(scores, size)


def s2_volume_breakout(
    features: Sequence[NameFeatures], *, size: int = SCREEN_SIZE
) -> tuple[ScreenEntry, ...]:
    """S2 over ``features`` (the eligible names)."""
    scores: dict[str, Decimal] = {}
    for f in features:
        if (
            f.at_high_60 is True
            and f.volume_ratio_day0 is not None
            and f.volume_ratio_day0 >= S2_VOLUME_RATIO_MIN
            and f.ret_5 is not None
            and f.ret_5 < S2_RET5_MAX
            and f.atr_contraction is not None
            and f.atr_contraction < S2_ATR_CONTRACTION_MAX
        ):
            scores[f.isin] = f.volume_ratio_day0
    return _top(scores, size)


def s3_pullback(
    features: Sequence[NameFeatures], *, size: int = SCREEN_SIZE
) -> tuple[ScreenEntry, ...]:
    """S3 over ``features`` (the eligible names); the momentum quintile is among them."""
    momentum = percentile_ranks({f.isin: f.ret_6_1 for f in features if f.ret_6_1 is not None})
    scores: dict[str, Decimal] = {}
    for f in features:
        if f.ret_6_1 is None or momentum.get(f.isin, _ZERO) < S3_MOMENTUM_QUANTILE:
            continue
        if f.sma200 is None or not f.close > f.sma200:
            continue
        if f.days_since_high_20 is None or not S3_DAYS_MIN <= f.days_since_high_20 <= S3_DAYS_MAX:
            continue
        if f.pullback_pct is None or not S3_PULLBACK_MIN <= f.pullback_pct <= S3_PULLBACK_MAX:
            continue
        if f.pullback_volume_ratio is None or not f.pullback_volume_ratio < S3_PULLBACK_VOLUME_MAX:
            continue
        near = [d for d in (f.dist_sma20, f.dist_sma50) if d is not None and abs(d) <= S3_SMA_BAND]
        if not near:
            continue
        scores[f.isin] = f.ret_6_1
    return _top(scores, size)


def s4_earnings_momentum(
    earnings: Sequence[EarningsFeatures], *, size: int = SCREEN_SIZE
) -> tuple[ScreenEntry, ...]:
    """S4 over ``earnings`` (the eligible names' latest results); the SUE quintile is among them."""
    with localcontext(_CONTEXT):
        sue_rank = percentile_ranks({e.isin: e.sue for e in earnings})
        passing = [
            e
            for e in earnings
            if e.sessions_since_day0 < S4_RESULTS_SESSIONS
            and sue_rank[e.isin] >= S4_SUE_QUANTILE
            and e.ear is not None
            and e.ear > S4_EAR_MIN
            and e.day0_volume_ratio is not None
            and e.day0_volume_ratio >= S4_DAY0_VOLUME_MIN
        ]
        sue_pass = percentile_ranks({e.isin: e.sue for e in passing})
        ear_pass = percentile_ranks({e.isin: e.ear for e in passing})
        return _top({e.isin: sue_pass[e.isin] + ear_pass[e.isin] for e in passing}, size)


# ── S5: facts, unranked ──────────────────────────────────────────────────────────────────────────


class EventKind(StrEnum):
    ANNOUNCEMENT = "ANNOUNCEMENT"
    BULK_DEAL = "BULK_DEAL"
    BLOCK_DEAL = "BLOCK_DEAL"


class EventFact(_Model):
    """One S5 fact: a matched announcement or a bulk/block deal. Never ranked."""

    isin: str
    kind: EventKind
    categories: tuple[str, ...]
    knowable_date: date
    ref: str | None
    subject: str | None
    meeting_date: date | None
    client_name: str | None
    side: str | None
    quantity: int | None
    price: Decimal | None
    pct_equity: Decimal | None


def _announcement_events(
    rows: Sequence[AnnouncementText],
    *,
    eligible: frozenset[str],
    recent_after: date,
    session: date,
    meeting_until: date | None,
    keywords: KeywordTable,
) -> list[EventFact]:
    out: list[EventFact] = []
    for row in rows:
        if row.isin not in eligible:
            continue
        found = list(classify(row.subject, row.body, keywords).event_watch)
        if not found:
            continue
        recent = recent_after < row.knowable_date <= session
        categories = set(found)
        when: date | None = None
        if "results_board_meeting" in categories:
            when = meeting_date(subject_line(row.subject, row.body))
            upcoming = (
                when is not None and meeting_until is not None and session < when <= meeting_until
            )
            if not (upcoming or (when is None and recent)):
                categories.discard("results_board_meeting")
                when = None
        if not recent:
            # An older announcement is listed only as the intimation of an upcoming meeting.
            categories &= {"results_board_meeting"}
        if not categories:
            continue
        found = sorted(categories)
        out.append(
            EventFact(
                isin=row.isin,
                kind=EventKind.ANNOUNCEMENT,
                categories=tuple(sorted(found)),
                knowable_date=row.knowable_date,
                ref=row.ref,
                subject=subject_line(row.subject, row.body)[:300],
                meeting_date=when,
                client_name=None,
                side=None,
                quantity=None,
                price=None,
                pct_equity=None,
            )
        )
    return out


def _deal_events(
    deals: Sequence[DealRecord],
    *,
    eligible: frozenset[str],
    recent_after: date,
    shares: Mapping[str, Decimal],
) -> list[EventFact]:
    out: list[EventFact] = []
    for deal in deals:
        if deal.isin not in eligible or not deal.trade_date > recent_after:
            continue
        count = shares.get(deal.isin)
        kind = EventKind.BLOCK_DEAL if deal.deal_type.upper() == "BLOCK" else EventKind.BULK_DEAL
        out.append(
            EventFact(
                isin=deal.isin,
                kind=kind,
                categories=(kind.value.lower(),),
                knowable_date=deal.trade_date,
                ref=None,
                subject=None,
                meeting_date=None,
                client_name=deal.client_name,
                side=deal.side,
                quantity=deal.quantity,
                price=deal.price,
                pct_equity=(
                    (Decimal(deal.quantity) / count * _PCT).quantize(Decimal("0.0001"))
                    if count is not None and count > _ZERO
                    else None
                ),
            )
        )
    return out


def latest_share_counts(facts: Sequence[FilingFact]) -> dict[str, Decimal]:
    """Each ISIN's latest filed ``shares_outstanding`` (latest period, then latest filing)."""
    best: dict[str, tuple[tuple[date, date, str], Decimal]] = {}
    for fact in facts:
        if fact.concept != "shares_outstanding" or fact.value <= _ZERO:
            continue
        rank = (fact.period_end, fact.filing_date, fact.filing_id)
        held = best.get(fact.isin)
        if held is None or rank > held[0]:
            best[fact.isin] = (rank, fact.value)
    return {isin: value for isin, (_, value) in best.items()}


# ── the rule's identity ──────────────────────────────────────────────────────────────────────────


def screens_rule_source() -> bytes:
    """The bytes the rule hash covers: every rule function's source, the constants, the keywords."""
    parts = [
        inspect.getsource(obj)
        for obj in (
            _top,
            _zscores,
            _median,
            s1_trend_leaders,
            s2_volume_breakout,
            s3_pullback,
            s4_earnings_momentum,
            _announcement_events,
            _deal_events,
            latest_share_counts,
            percentile_ranks,
            _features.name_features,
            _features.earnings_features,
            _features._sma,
            _features._atr,
            _features._returns_stdev,
            _features._median,
            _features._stdev,
            _regime.RegimeIndex,
            _regime._decide,
            _exclusions.compute_exclusions,
            _exclusions.integrity_events,
        )
    ]
    constants = {
        "version": SCREENS_VERSION,
        "size": SCREEN_SIZE,
        "window": WINDOW_SESSIONS,
        "s1": [str(S1_NEAR_HIGH_MIN), str(S1_SECTOR_BONUS)],
        "s2": [str(S2_VOLUME_RATIO_MIN), str(S2_RET5_MAX), str(S2_ATR_CONTRACTION_MAX)],
        "s3": [
            str(S3_MOMENTUM_QUANTILE),
            str(S3_PULLBACK_MIN),
            str(S3_PULLBACK_MAX),
            S3_DAYS_MIN,
            S3_DAYS_MAX,
            str(S3_PULLBACK_VOLUME_MAX),
            str(S3_SMA_BAND),
        ],
        "s4": [
            S4_RESULTS_SESSIONS,
            str(S4_SUE_QUANTILE),
            str(S4_EAR_MIN),
            str(S4_DAY0_VOLUME_MIN),
            _features.EAR_BEFORE,
            _features.EAR_AFTER,
        ],
        "s5": [S5_EVENT_SESSIONS, S5_MEETING_SESSIONS],
        "regime": [
            _regime.REGIME_INDEX_SERIES,
            _regime.SMA_SESSIONS,
            _regime.SMA_RISING_LAG,
            _regime.VOL_SESSIONS,
            str(_regime.VOL_TOP_PERCENTILE),
            _regime.VOL_HISTORY_MIN,
            str(_regime.BREADTH_RISK_ON),
            _regime.RETURN_YEARS,
        ],
        "exclusions": [
            str(_exclusions.PRICE_BAND_MAX_PCT),
            _exclusions.INTEGRITY_EXCLUSION_SESSIONS,
            _exclusions.SURVEILLANCE_FRESH_SESSIONS,
        ],
        "delivery_min": _features.DELIVERY_MIN_SESSIONS,
        "keywords": EVENT_KEYWORDS_DIGEST,
    }
    return canonical_bytes({"source": parts, "constants": constants})


# ── outputs ──────────────────────────────────────────────────────────────────────────────────────


class CommonsScreens(_Model):
    """One session's screens, regime, exclusions and per-name features, and their digest.

    ``features`` and ``earnings`` cover every universe name, excluded or not, so a held name's
    dossier can still be built. ``eligible`` counts the names the screens ranked. ``built_at``
    is outside the digest.
    """

    trading_date: date
    build_digest: str
    shortlist_digest: str | None
    screens_version: str
    rule_hash: str
    regime: RegimeReading
    exclusions: Exclusions
    eligible: int
    s1: tuple[ScreenEntry, ...]
    s2: tuple[ScreenEntry, ...]
    s3: tuple[ScreenEntry, ...]
    s4: tuple[ScreenEntry, ...]
    s5: tuple[EventFact, ...]
    features: tuple[NameFeatures, ...]
    earnings: tuple[EarningsFeatures, ...]
    gaps: tuple[Gap, ...]
    screens_digest: str
    built_at: datetime

    def ranked(self, screen: Screen) -> tuple[ScreenEntry, ...]:
        return {Screen.S1: self.s1, Screen.S2: self.s2, Screen.S3: self.s3, Screen.S4: self.s4}[
            screen
        ]

    def screens_of(self, isin: str) -> tuple[str, ...]:
        """The screens ``isin`` is on: ranked ones by id, and ``S5`` for an event fact."""
        found = [s.value for s in Screen if any(e.isin == isin for e in self.ranked(s))]
        if any(e.isin == isin for e in self.s5):
            found.append("S5")
        return tuple(found)

    def digest_scope(self, shortlist: Shortlist | None) -> frozenset[str]:
        """The names the daily digest build covers: S1-S5 and the composite shortlist."""
        names = {e.isin for s in Screen for e in self.ranked(s)} | {e.isin for e in self.s5}
        if shortlist is not None:
            names |= {e.isin for e in shortlist.entries}
        return frozenset(names)

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"screens_digest", "built_at"})

    @staticmethod
    def digest_of(body: Mapping[str, Any]) -> str:
        return hashlib.sha256(canonical_bytes(dict(body))).hexdigest()

    def verify(self) -> None:
        """Recompute the digest and raise ``ValueError`` if the stored one disagrees."""
        expected = self.digest_of(self.body())
        if expected != self.screens_digest:
            raise ValueError(
                f"screens {self.trading_date.isoformat()} {self.screens_digest[:12]} does not "
                f"reproduce its digest: recomputed {expected[:12]}"
            )


# ── the builder ──────────────────────────────────────────────────────────────────────────────────


def _admit[R](
    read: Callable[[], Dataset[R]], pit: PitContext, gaps: list[Gap], label: str
) -> tuple[R, ...] | None:
    try:
        return pit.admit(read())
    except SourceUnavailableError as exc:
        gaps.append(Gap(source=f"{label}:{exc.source}", reason=exc.reason))
        return None


def _next_sessions(session: date, count: int, gaps: list[Gap]) -> list[date]:
    """Up to ``count`` NSE sessions after ``session`` from the published holiday calendar.

    The calendar is read only as far as it covers. Fewer than ``count`` sessions is a gap: the
    integrity exclusions then carry no end date, and S5 lists no meeting past the coverage.
    """
    try:
        calendar = trading_calendar()
        if not calendar.covers(session):
            raise CalendarError(f"{session.isoformat()} is outside the calendar")
        end = min(date.fromordinal(session.toordinal() + 3 * count), calendar.coverage_end)
        found = [d for d in calendar.expected_sessions(session, end) if d > session][:count]
    except CalendarError as exc:
        gaps.append(Gap(source="nse_holidays", reason=f"next sessions unknown: {exc}"))
        return []
    if len(found) < count:
        gaps.append(
            Gap(
                source="nse_holidays",
                reason=f"the holiday calendar ends {calendar.coverage_end.isoformat()}: "
                f"{len(found)} of the next {count} sessions are known",
            )
        )
    return found


def build_screens(
    sheets: CommonsSheets,
    *,
    shortlist: Shortlist | None,
    source: ScreenSource,
    clock: Clock,
    keywords: KeywordTable | None = None,
    future_sessions: Sequence[date] | None = None,
) -> CommonsScreens:
    """The session's screens S1-S5, regime and exclusions over ``sheets``' universe.

    What it does: verifies ``sheets`` (and ``shortlist``, if given, against the same build),
    reads every input through ``source`` and the PIT guard as of the session, applies the
    exclusions, then the four ranked screens, S5, and the regime. ``built_at`` comes from
    ``clock``. ``future_sessions`` (the next NSE sessions) defaults to the published holiday
    calendar.
    What it assumes: ``sheets`` was built for this session over the same lake (a green day).
    What it never does: write, read past the session, or fill a missing input. A missing source
    is a :class:`Gap`; missing bars refuse the build (:class:`CommonsRefusedError`).
    """
    sheets.verify()
    session = sheets.trading_date
    if shortlist is not None:
        shortlist.verify()
        if shortlist.build_digest != sheets.build_digest:
            raise ValueError("the shortlist was built on a different sheets build")
    table = keywords if keywords is not None else load_event_keywords()
    pit = PitContext(as_of=session)
    gaps: list[Gap] = []
    universe = sheets.parameters["universe"]
    series = str(universe["series"])
    stages = tuple(str(s) for s in universe["excluded_surveillance"])
    rows = {r.isin: r for r in sheets.universe}
    members = sorted(rows)

    with localcontext(_CONTEXT):
        try:
            calendar = sorted(set(pit.admit(source.sessions(session, WINDOW_SESSIONS))))
        except SourceUnavailableError as exc:
            raise CommonsRefusedError(f"no session calendar: {exc}") from exc
        if not calendar or calendar[-1] != session:
            raise CommonsRefusedError(f"{session.isoformat()} is not an NSE session in the lake")
        try:
            bars = pit.admit(source.price_bars(frozenset(members), calendar, series))
        except SourceUnavailableError as exc:
            raise CommonsRefusedError(f"no price bars: {exc}") from exc
        aligned = align(bars, calendar)
        features = {
            isin: f
            for isin in members
            if isin in aligned and (f := name_features(isin, aligned[isin])) is not None
        }

        levels_read = _admit(
            lambda: source.index_levels([REGIME_INDEX_SERIES], session), pit, gaps, "regime"
        )
        levels = sorted(
            (lv.session, lv.close)
            for lv in levels_read or ()
            if lv.series_id == REGIME_INDEX_SERIES and lv.session <= session
        )
        facts = _admit(lambda: source.filings(session), pit, gaps, "earnings") or ()
        member_set = frozenset(members)
        panel = EarningsSurprisePanel((f for f in facts if f.isin in member_set), calendar)
        earnings = {
            isin: e
            for isin in features
            if (
                e := earnings_features(
                    isin, aligned[isin], calendar, panel=panel, index_levels=levels
                )
            )
            is not None
        }

        future = (
            list(future_sessions)
            if future_sessions is not None
            else _next_sessions(session, _FUTURE_SESSIONS, gaps)
        )
        window_open = (
            calendar[-_exclusions.INTEGRITY_EXCLUSION_SESSIONS - 1]
            if len(calendar) > _exclusions.INTEGRITY_EXCLUSION_SESSIONS
            else calendar[0]
        )
        announcements = _admit(
            lambda: source.announcement_texts(
                date.fromordinal(window_open.toordinal() + 1), session
            ),
            pit,
            gaps,
            "announcements",
        )
        surveillance: dict[str, Sequence[SurveillanceEntry] | None] = {}
        for stage in stages:
            surveillance[stage] = _admit(
                lambda stage=stage: source.surveillance(stage, session),  # type: ignore[misc]
                pit,
                gaps,
                "surveillance",
            )
        bands: tuple[PriceBandEntry, ...] | None = _admit(
            lambda: source.price_bands(session), pit, gaps, "price_bands"
        )
        exclusions = compute_exclusions(
            members,
            calendar=calendar,
            series=series,
            stages=stages,
            surveillance=surveillance,
            bands=bands,
            announcements=announcements,
            keywords=table,
            future_sessions=future,
        )
        excluded = exclusions.isins
        eligible = [features[i] for i in sorted(features) if i not in excluded]
        eligible_set = frozenset(f.isin for f in eligible)

        recent_after = (
            calendar[-S5_EVENT_SESSIONS - 1] if len(calendar) > S5_EVENT_SESSIONS else date.min
        )
        deals = _admit(lambda: source.deals(recent_after, session), pit, gaps, "deals") or ()
        meeting_until = (
            future[S5_MEETING_SESSIONS - 1] if len(future) >= S5_MEETING_SESSIONS else None
        )
        events = _announcement_events(
            announcements or (),
            eligible=eligible_set,
            recent_after=recent_after,
            session=session,
            meeting_until=meeting_until,
            keywords=table,
        ) + _deal_events(
            deals,
            eligible=eligible_set,
            recent_after=recent_after,
            shares=latest_share_counts(facts),
        )
        events.sort(
            key=lambda e: (e.knowable_date, e.isin, e.kind.value, e.ref or "", e.client_name or "")
        )

        sectors = {isin: rows[isin].sector for isin in members}
        s1 = s1_trend_leaders(eligible, sectors)
        s2 = s2_volume_breakout(eligible)
        s3 = s3_pullback(eligible)
        s4 = s4_earnings_momentum([earnings[f.isin] for f in eligible if f.isin in earnings])

        above200 = [f for f in eligible if f.sma200 is not None]
        breadth200 = (
            Decimal(sum(1 for f in above200 if f.close > (f.sma200 or _ZERO)))
            / Decimal(len(above200))
            if above200
            else None
        )
        breadth = sheets.market.breadth
        vix = sheets.market.india_vix
        regime = RegimeIndex(levels).reading(
            session,
            breadth_above_sma50=breadth.above_mean_share if breadth is not None else None,
            breadth_above_sma200=breadth200,
            india_vix=vix.value if vix is not None else None,
        )
        if regime.state is None:
            gaps.append(Gap(source="regime", reason=regime.reason))

    all_gaps = tuple(sorted({*gaps, *exclusions.gaps}, key=lambda g: (g.source, g.reason)))
    draft: dict[str, Any] = {
        "trading_date": session,
        "build_digest": sheets.build_digest,
        "shortlist_digest": shortlist.shortlist_digest if shortlist is not None else None,
        "screens_version": SCREENS_VERSION,
        "rule_hash": SCREENS_RULE_HASH,
        "regime": regime,
        "exclusions": exclusions,
        "eligible": len(eligible),
        "s1": s1,
        "s2": s2,
        "s3": s3,
        "s4": s4,
        "s5": tuple(events),
        "features": tuple(features[i] for i in sorted(features)),
        "earnings": tuple(earnings[i] for i in sorted(earnings)),
        "gaps": all_gaps,
        "screens_digest": "0" * 64,
        "built_at": clock.now(),
    }
    provisional = CommonsScreens(**draft)
    digest = CommonsScreens.digest_of(provisional.body())
    screens = provisional.model_copy(update={"screens_digest": digest})
    _LOG.info(
        "commons.screens.built",
        trading_date=session.isoformat(),
        universe=len(members),
        eligible=len(eligible),
        excluded=len(exclusions.excluded),
        buys_blocked=exclusions.buys_blocked,
        regime=regime.state.value if regime.state is not None else None,
        s1=len(s1),
        s2=len(s2),
        s3=len(s3),
        s4=len(s4),
        s5=len(events),
        gaps=len(all_gaps),
        screens_digest=digest,
    )
    return screens


#: The sha256 of :func:`screens_rule_source`, struck once every function it covers exists.
SCREENS_RULE_HASH: Final = hashlib.sha256(screens_rule_source()).hexdigest()
