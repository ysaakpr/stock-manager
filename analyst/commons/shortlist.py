"""A10 · M17.2 — the mechanical shortlist: the frozen rule every manager and control book reads.

Pre-registration §3 fixes the rule, and this module is it. Each universe name (the M17.1 universe
sheet) is scored on four factors:

- **12-1 momentum.** ``close(t-21) / close(t-252) - 1`` on adjusted closes, in NSE sessions. This
  is the classical definition, the one `backtest.run` uses (``_SWING_MOM_LONG``,
  ``_SWING_MOM_SHORT``): a year back to a month back, skipping the most recent month. Both closes
  must print on exactly those sessions.
- **20-session relative strength against NIFTY 500.** ``(1 + r) / (1 + r_index) - 1``. ``r`` is the
  sheet's ``return_4w``, the name's 20-session adjusted return. ``r_index`` is NIFTY 500's
  20-session return to its latest level on or before the session. A lagging level is named in a
  gap. The rank is the same either way, because the index return is common to every name.
- **Earnings surprise.** The M16.3 leg, reused and not re-derived:
  :class:`backtest.policies.earnings_surprise.EarningsSurprisePanel` over the same PIT filings. It
  is the standardised unexpected earnings inside its 63-session window, 0 after it, and undefined
  for a name with no signal.
- **Liquidity.** The natural log of the sheet's 20-session median traded value.

Each factor becomes a percentile rank over the universe: ``(r - 1) / (n - 1)`` on average-tie
ranks among the ``n`` names where it is defined. Higher is better on all four. A name where a
factor is undefined takes the mean rank, 1/2, which is exactly the mean of the defined ranks, so a
missing factor neither helps nor hurts it. The composite is the equal-weight mean of the four ranks.
The top :data:`SHORTLIST_SIZE` by composite form the shortlist, and ties are broken by ISIN.

**Frozen.** :data:`SHORTLIST_RULE_HASH` is the sha256 of the rule's own source: the functions
below, the M16.3 functions it calls, and its constants. It feeds
`analyst.fundmanager.mandate_hash`, so a changed rule is a new manager (pre-registration §7). The
hash is computed when the module is imported. It never comes from a value someone typed.

**Point in time (invariant #7).** Every lake read crosses :meth:`PitContext.admit` as of the
session: the session calendar, the two adjusted closes, the NIFTY 500 levels and the filings. A
future-dated record raises. A missing source becomes a :class:`Gap`, and its factor is undefined
for every name. Nothing is imputed.

What it never does: read a wall clock, key on a symbol, hold a manager's view, or import
`analyst.fundmanager`. The shortlist is a mechanical ranking of facts, not an opinion.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from analyst.commons.sheets import (
    CommonsSheets,
    CommonsSource,
    Gap,
    IndexLevel,
    SourceUnavailableError,
    UniverseRow,
    canonical_bytes,
)
from backtest.policies import earnings_surprise as _m16_3
from backtest.policies.earnings_surprise import EarningsSurprisePanel
from dataplatform.clock import Clock
from dataplatform.logging import get_logger
from dataplatform.query import Dataset, PitContext

__all__ = [
    "MOMENTUM_LONG_SESSIONS",
    "MOMENTUM_SHORT_SESSIONS",
    "RS_INDEX_SERIES",
    "RS_INDEX_STALE_DAYS",
    "RS_SESSIONS",
    "SHORTLIST_RULE_HASH",
    "SHORTLIST_SIZE",
    "SHORTLIST_VERSION",
    "Shortlist",
    "ShortlistEntry",
    "ShortlistFactors",
    "build_shortlist",
    "log_liquidity",
    "momentum_12_1",
    "percentile_ranks",
    "rank_shortlist",
    "relative_strength",
    "shortlist_rule_source",
]

_LOG = get_logger(__name__)

#: Versioned identity of the shortlist layout. It is part of the digest.
SHORTLIST_VERSION: Final = "commons-shortlist/1"
#: Names on the shortlist per session (pre-registration §3).
SHORTLIST_SIZE: Final = 40
#: The 12-1 lags in NSE sessions: a year back, a month back (`backtest.run._SWING_MOM_*`).
MOMENTUM_LONG_SESSIONS: Final = 252
MOMENTUM_SHORT_SESSIONS: Final = 21
#: The relative-strength window, in NSE sessions, and the index it is measured against.
RS_SESSIONS: Final = 20
RS_INDEX_SERIES: Final = "IN.NSE.NIFTY_500.CLOSE"
#: An index level older than this many calendar days is not used (the market sheet's rule).
RS_INDEX_STALE_DAYS: Final = 7
#: Sessions the calendar must hold: the session and the 252 before it.
_CALENDAR_SESSIONS: Final = MOMENTUM_LONG_SESSIONS + 1

_CONTEXT: Final = Context(prec=28, rounding=ROUND_HALF_EVEN)
_SCORE: Final = Decimal("0.00000001")  # 8 dp, as every swing feature is quantised
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_HALF: Final = Decimal("0.5")
#: The factors, in composite order. Each is ranked ascending: a higher value ranks higher.
_FACTORS: Final = ("momentum_12_1", "relative_strength_20", "earnings_surprise", "log_liquidity")


# ── the rule: pure functions, hashed ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ShortlistFactors:
    """One universe name's four raw factor values. ``None`` is undefined, never zero."""

    isin: str
    momentum_12_1: Decimal | None
    relative_strength_20: Decimal | None
    earnings_surprise: Decimal | None
    log_liquidity: Decimal | None


def momentum_12_1(base_12: Decimal | None, base_1: Decimal | None) -> Decimal | None:
    """``base_1 / base_12 - 1``: the t-12m .. t-1m return. Undefined without both closes."""
    if base_12 is None or base_1 is None or base_12 <= _ZERO:
        return None
    return (base_1 / base_12 - _ONE).quantize(_SCORE)


def relative_strength(stock_return: Decimal | None, index_return: Decimal | None) -> Decimal | None:
    """``(1 + r) / (1 + r_index) - 1``: the name's return over the index's, as a growth ratio."""
    if stock_return is None or index_return is None or index_return <= -_ONE:
        return None
    return ((_ONE + stock_return) / (_ONE + index_return) - _ONE).quantize(_SCORE)


def log_liquidity(median_traded_value: Decimal) -> Decimal | None:
    """``ln(median traded value)``. Undefined for a zero median, which a floor of 0 admits."""
    if median_traded_value <= _ZERO:
        return None
    return median_traded_value.ln().quantize(_SCORE)


def percentile_ranks(values: Mapping[str, Decimal | None]) -> dict[str, Decimal]:
    """Each ISIN's percentile rank in ``[0, 1]``, higher value higher rank.

    Ranks are average ranks over ties, ``(r - 1) / (n - 1)`` among the ``n`` defined values, so the
    defined ranks have mean exactly 1/2. An undefined value takes that mean, 1/2. A single defined
    value is 1/2 too: there is nothing to rank it against.
    """
    defined = sorted((v, isin) for isin, v in values.items() if v is not None)
    n = len(defined)
    out = dict.fromkeys(values, _HALF)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and defined[j + 1][0] == defined[i][0]:
            j += 1
        # Positions i..j (0-based) tie: their average 0-based rank is (i + j) / 2.
        rank = _HALF if n == 1 else (Decimal(i + j) / 2) / Decimal(n - 1)
        for k in range(i, j + 1):
            out[defined[k][1]] = rank.quantize(_SCORE)
        i = j + 1
    return out


def rank_shortlist(
    factors: Sequence[ShortlistFactors], *, size: int = SHORTLIST_SIZE
) -> tuple[ShortlistEntry, ...]:
    """The top ``size`` names by the equal-weight composite of the four percentile ranks.

    What it does: ranks each factor over ``factors`` (:func:`percentile_ranks`), averages the four
    ranks, and returns the best ``size`` names, composite descending, ties broken by ISIN.
    What it assumes: ``factors`` is the session's whole universe, one entry per ISIN.
    What it never does: read anything, or treat an undefined factor as a zero.
    """
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")
    isins = [f.isin for f in factors]
    if len(set(isins)) != len(isins):
        raise ValueError("a name appears twice in the shortlist input")
    with localcontext(_CONTEXT):
        ranks = {
            name: percentile_ranks({f.isin: getattr(f, name) for f in factors}) for name in _FACTORS
        }
        scored = []
        for f in factors:
            legs = [ranks[name][f.isin] for name in _FACTORS]
            composite = (sum(legs, _ZERO) / Decimal(len(legs))).quantize(_SCORE)
            scored.append((composite, f, legs))
        scored.sort(key=lambda s: (-s[0], s[1].isin))
        return tuple(
            ShortlistEntry(
                position=position,
                isin=f.isin,
                momentum_12_1=f.momentum_12_1,
                relative_strength_20=f.relative_strength_20,
                earnings_surprise=f.earnings_surprise,
                log_liquidity=f.log_liquidity,
                rank_momentum_12_1=legs[0],
                rank_relative_strength_20=legs[1],
                rank_earnings_surprise=legs[2],
                rank_log_liquidity=legs[3],
                composite=composite,
            )
            for position, (composite, f, legs) in enumerate(scored[:size], start=1)
        )


# ── the rule's identity ──────────────────────────────────────────────────────────────────────────


def shortlist_rule_source() -> bytes:
    """The bytes the rule hash covers: its functions' source, M16.3's, and its constants.

    A docstring edit elsewhere in either module does not move it. A changed function body or a
    changed constant does.
    """
    parts: list[str] = [
        inspect.getsource(obj)
        for obj in (
            ShortlistFactors,
            momentum_12_1,
            relative_strength,
            log_liquidity,
            percentile_ranks,
            rank_shortlist,
            _factors_of,
            _momentum_bases,
            _index_return,
            _surprises,
            _m16_3._surprise_for,
            _m16_3.surprise_readings,
            _m16_3.EarningsSurprisePanel,
        )
    ]
    constants = {
        "version": SHORTLIST_VERSION,
        "size": SHORTLIST_SIZE,
        "momentum_long": MOMENTUM_LONG_SESSIONS,
        "momentum_short": MOMENTUM_SHORT_SESSIONS,
        "rs_sessions": RS_SESSIONS,
        "rs_index": RS_INDEX_SERIES,
        "rs_index_stale_days": RS_INDEX_STALE_DAYS,
        "factors": list(_FACTORS),
        "score_scale": str(_SCORE),
        "m16_3": {
            "active_sessions": _m16_3.ACTIVE_SESSIONS,
            "history_differences": _m16_3.HISTORY_DIFFERENCES,
            "concepts": sorted(_m16_3.CONCEPTS),
        },
    }
    return canonical_bytes({"source": parts, "constants": constants})


# ── outputs ──────────────────────────────────────────────────────────────────────────────────────


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ShortlistEntry(_Model):
    """One shortlisted name: its raw factors, its four percentile ranks and its composite."""

    position: int = Field(ge=1)
    isin: str
    momentum_12_1: Decimal | None
    relative_strength_20: Decimal | None
    earnings_surprise: Decimal | None
    log_liquidity: Decimal | None
    rank_momentum_12_1: Decimal
    rank_relative_strength_20: Decimal
    rank_earnings_surprise: Decimal
    rank_log_liquidity: Decimal
    composite: Decimal


class Shortlist(_Model):
    """One session's shortlist, the sheets build it read, and the digest that names it.

    ``coverage`` counts, per factor, the universe names where the factor is defined. A reader can
    then see a factor that fell back to the mean rank for most names. ``built_at`` is outside the
    digest.
    """

    trading_date: date
    build_digest: str
    shortlist_version: str
    rule_hash: str
    universe_size: int
    coverage: dict[str, int]
    entries: tuple[ShortlistEntry, ...]
    gaps: tuple[Gap, ...]
    shortlist_digest: str
    built_at: datetime

    @staticmethod
    def digest_of(
        *,
        trading_date: date,
        build_digest: str,
        shortlist_version: str,
        rule_hash: str,
        universe_size: int,
        coverage: Mapping[str, int],
        entries: Sequence[ShortlistEntry],
        gaps: Sequence[Gap],
    ) -> str:
        document: dict[str, Any] = {
            "trading_date": trading_date.isoformat(),
            "build_digest": build_digest,
            "shortlist_version": shortlist_version,
            "rule_hash": rule_hash,
            "universe_size": universe_size,
            "coverage": dict(coverage),
            "entries": [e.model_dump(mode="json") for e in entries],
            "gaps": [g.model_dump(mode="json") for g in gaps],
        }
        return hashlib.sha256(canonical_bytes(document)).hexdigest()

    def verify(self) -> None:
        """Recompute the digest and raise ``ValueError`` if the stored one disagrees."""
        expected = self.digest_of(
            trading_date=self.trading_date,
            build_digest=self.build_digest,
            shortlist_version=self.shortlist_version,
            rule_hash=self.rule_hash,
            universe_size=self.universe_size,
            coverage=self.coverage,
            entries=self.entries,
            gaps=self.gaps,
        )
        if expected != self.shortlist_digest:
            raise ValueError(
                f"shortlist {self.trading_date.isoformat()} {self.shortlist_digest[:12]} does not "
                f"reproduce its digest: recomputed {expected[:12]}"
            )


# ── the builder: reads the lake, then applies the rule ───────────────────────────────────────────


def build_shortlist(sheets: CommonsSheets, *, source: CommonsSource, clock: Clock) -> Shortlist:
    """The session's shortlist over ``sheets``' universe.

    What it does: verifies ``sheets``, reads the session calendar, the two 12-1 closes, NIFTY 500
    and the filings through ``source`` (each admitted through the PIT guard as of the session),
    computes the four factors and applies :func:`rank_shortlist`. ``built_at`` comes from
    ``clock``.
    What it assumes: ``sheets`` was built for this session over the same lake (a green day; the
    sheets builder refuses a red one).
    What it never does: write, read past the session, or fill a missing factor. A missing source
    becomes a :class:`Gap` and its factor takes the mean rank for every name.
    """
    sheets.verify()
    session = sheets.trading_date
    pit = PitContext(as_of=session)
    gaps: list[Gap] = []
    with localcontext(_CONTEXT):
        calendar = _calendar(session, source, pit, gaps)
        universe = sheets.universe
        members = frozenset(r.isin for r in universe)
        bases = _momentum_bases(members, calendar, source, pit, gaps)
        index_return = _index_return(calendar, source, pit, gaps)
        surprises = _surprises(members, calendar, source, pit, gaps)
        factors = _factors_of(universe, bases=bases, index_return=index_return, surprises=surprises)
        entries = rank_shortlist(factors)
    coverage = {name: sum(1 for f in factors if getattr(f, name) is not None) for name in _FACTORS}
    ordered_gaps = tuple(sorted(gaps, key=lambda g: (g.source, g.reason)))
    digest = Shortlist.digest_of(
        trading_date=session,
        build_digest=sheets.build_digest,
        shortlist_version=SHORTLIST_VERSION,
        rule_hash=SHORTLIST_RULE_HASH,
        universe_size=len(universe),
        coverage=coverage,
        entries=entries,
        gaps=ordered_gaps,
    )
    _LOG.info(
        "commons.shortlist.built",
        trading_date=session.isoformat(),
        universe=len(universe),
        shortlisted=len(entries),
        gaps=len(ordered_gaps),
        shortlist_digest=digest,
    )
    return Shortlist(
        trading_date=session,
        build_digest=sheets.build_digest,
        shortlist_version=SHORTLIST_VERSION,
        rule_hash=SHORTLIST_RULE_HASH,
        universe_size=len(universe),
        coverage=coverage,
        entries=entries,
        gaps=ordered_gaps,
        shortlist_digest=digest,
        built_at=clock.now(),
    )


def _factors_of(
    universe: Sequence[UniverseRow],
    *,
    bases: Mapping[str, tuple[Decimal | None, Decimal | None]],
    index_return: Decimal | None,
    surprises: Mapping[str, Decimal | None],
) -> list[ShortlistFactors]:
    """The four factors of each universe row, from the sheet and the reads."""
    return [
        ShortlistFactors(
            isin=row.isin,
            momentum_12_1=momentum_12_1(*bases.get(row.isin, (None, None))),
            relative_strength_20=relative_strength(row.return_4w, index_return),
            earnings_surprise=surprises.get(row.isin),
            log_liquidity=log_liquidity(row.median_traded_value),
        )
        for row in universe
    ]


def _calendar(session: date, source: CommonsSource, pit: PitContext, gaps: list[Gap]) -> list[date]:
    try:
        sessions = sorted(set(pit.admit(source.sessions(session, _CALENDAR_SESSIONS))))
    except SourceUnavailableError as exc:
        gaps.append(Gap(source=exc.source, reason=exc.reason))
        return []
    if not sessions or sessions[-1] != session:
        gaps.append(Gap(source="sessions", reason=f"the calendar does not end on {session}"))
        return []
    return sessions


def _momentum_bases(
    members: frozenset[str],
    calendar: Sequence[date],
    source: CommonsSource,
    pit: PitContext,
    gaps: list[Gap],
) -> dict[str, tuple[Decimal | None, Decimal | None]]:
    """Each name's adjusted close on t-252 and on t-21, exactly on those sessions."""
    if not calendar:
        return {}
    if len(calendar) < _CALENDAR_SESSIONS:
        gaps.append(
            Gap(
                source="momentum_12_1",
                reason=f"{len(calendar)} sessions in the lake, 12-1 needs {_CALENDAR_SESSIONS}",
            )
        )
        return {}
    if not members:
        return {}
    t_12 = calendar[-1 - MOMENTUM_LONG_SESSIONS]
    t_1 = calendar[-1 - MOMENTUM_SHORT_SESSIONS]
    try:
        records = pit.admit(source.adjusted_closes(members, [t_12, t_1]))
    except SourceUnavailableError as exc:
        gaps.append(Gap(source=exc.source, reason=exc.reason))
        return {}
    closes = {(r.isin, r.trade_date): r.close for r in records}
    return {isin: (closes.get((isin, t_12)), closes.get((isin, t_1))) for isin in members}


def _index_return(
    calendar: Sequence[date], source: CommonsSource, pit: PitContext, gaps: list[Gap]
) -> Decimal | None:
    """NIFTY 500's 20-session return, ending at its latest level on or before the session.

    The return runs from the index's last level on or before the NSE session 20 before that
    latest level, to the latest level. A latest level older than the session is kept and named in
    a gap, as the market sheet keeps a lagging level (M17.1). It is never carried forward to the
    session. One older than :data:`RS_INDEX_STALE_DAYS` is not used.

    The rank does not depend on this value. ``(1 + r) / (1 + r_index)`` is the same increasing
    function of ``r`` for every name, so a lagging or missing index changes the reported relative
    strength, never the order.
    """
    if len(calendar) <= RS_SESSIONS:
        if calendar:
            gaps.append(Gap(source="relative_strength_20", reason="too few sessions"))
        return None
    session = calendar[-1]
    try:
        levels: Sequence[IndexLevel] = pit.admit(source.index_levels([RS_INDEX_SERIES], session))
    except SourceUnavailableError as exc:
        gaps.append(Gap(source=exc.source, reason=exc.reason))
        return None
    own = {lv.session: lv.close for lv in levels if lv.series_id == RS_INDEX_SERIES}
    published = sorted(day for day in own if day <= session)
    if not published or (session - published[-1]).days > RS_INDEX_STALE_DAYS:
        gaps.append(
            Gap(
                source=RS_INDEX_SERIES,
                reason=f"no level within {RS_INDEX_STALE_DAYS} days of {session}",
            )
        )
        return None
    latest = published[-1]
    ending = [day for day in calendar if day <= latest]
    if len(ending) <= RS_SESSIONS:
        gaps.append(Gap(source=RS_INDEX_SERIES, reason=f"too few sessions before {latest}"))
        return None
    start = ending[-1 - RS_SESSIONS]
    before = [day for day in published if day <= start]
    if not before or own[before[-1]] <= _ZERO:
        gaps.append(Gap(source=RS_INDEX_SERIES, reason=f"no usable level on or before {start}"))
        return None
    if latest != session:
        gaps.append(
            Gap(
                source=RS_INDEX_SERIES,
                reason=f"latest level is {latest}, not the session; its return ends there",
            )
        )
    return own[latest] / own[before[-1]] - _ONE


def _surprises(
    members: frozenset[str],
    calendar: Sequence[date],
    source: CommonsSource,
    pit: PitContext,
    gaps: list[Gap],
) -> dict[str, Decimal | None]:
    """The M16.3 earnings-surprise leg of each name on the session."""
    if not calendar or not members:
        return {}
    session = calendar[-1]
    try:
        facts = pit.admit(
            Dataset.declaring(
                "commons.filings",
                source.filings(session).records,
                knowable_date=lambda fact: fact.filing_date,
            )
        )
    except SourceUnavailableError as exc:
        gaps.append(Gap(source=exc.source, reason=exc.reason))
        return {}
    panel = EarningsSurprisePanel((f for f in facts if f.isin in members), calendar)
    return {isin: panel.value(isin, session) for isin in sorted(members)}


#: The sha256 of :func:`shortlist_rule_source`, lowercase hex: what `mandate_hash` takes. Struck
#: last, once every function it covers exists.
SHORTLIST_RULE_HASH: Final = hashlib.sha256(shortlist_rule_source()).hexdigest()
