"""A10 · M17.9 — the frozen base-rate table: how often each screen's names beat NIFTY 500.

Study §3: every candidate memo starts from its cell's base rate and must justify each move away
from it. The table is computed from **history** and holds no LLM output, so it is a shared fact.

**What a cell is.** Screen membership (S1-S4, the composite shortlist, or none of them) x cap
tier (large, mid, small, and ``unranked`` past size rank 500) x regime (RISK_ON, NEUTRAL,
RISK_OFF) x horizon (5, 20 and 60 sessions). Each cell holds ``n``, P(beat NIFTY 500) (the share
of observations whose excess return is positive), the median excess return, its 25th and 75th
percentiles and the interquartile range. A cell with ``n < 30`` is marked ``thin``. ``ALL`` in the
tier or the regime position pools that dimension, so a thin cell has a coarser one to fall back
on. A name on two screens on one session counts in both.

**One observation** is (session ``t``, name): the name is in the PIT universe on ``t`` (prints in
``EQ`` on ``t``, and its median traded value over the 20 sessions to ``t``, on the sessions it
traded, is at least ₹1 cr — the universe sheet's rule), and its excess return over ``h`` sessions
is ``close(t+h) / close(t) - 1`` minus NIFTY 500's ``level(t+h) / level(t) - 1``, on adjusted
closes. A name that stops printing before ``t+h`` takes its last close after ``t``; one that never
prints again after ``t`` is dropped, never counted at a zero return. The screens are exactly
`analyst.commons.screens`' rules over `analyst.commons.features`, and the composite is
`analyst.commons.shortlist.rank_shortlist` over its four factors. Every input of a screen on
``t`` is dated on or before ``t``: each window ends at ``t``, the SUE panel reads only filings
filed by ``t``, and the regime reads only levels released by ``t``.

**Sessions.** One observation date every :data:`SAMPLE_EVERY` NSE sessions from the first session
on or after ``start``, while ``t + 60`` is still on or before ``end``. Weekly sampling keeps the
5-session horizon free of overlap and every horizon's ``n`` closer to its count of independent
outcomes. ``end`` bounds every number read, so the table does not move as the lake grows.

**Deviations from the study, stated.** (1) The study names a "NIFTY 500 TRI proxy"; the lake holds
no NIFTY 500 TRI, so the excess is over the NIFTY 500 price index (`analyst.commons.regime`),
against split/bonus-adjusted closes that carry no dividends either. (2) History has no GSM/ESM,
price-band or integrity-event record before 2026-09, so the historical universe is the floor
alone; the Amendment 1 (b) exclusions apply to the live universe only. (3) The M16.3 SUE starts
in 2021-04 (thirteen quarters must accumulate), so S4 and the composite's surprise leg are empty
or partial before then; the counts show it. (4) Ranks past 500 are a fourth tier, ``unranked``,
so every universe name has a cell.

**Frozen.** The digest is the sha256 of the canonical table (every cell, the parameters, the
screens and shortlist rule hashes); ``built_at`` is outside it. The same lake and range rebuild
the same digest. The file is written as ``base_rates_<start>_<end>_<digest12>.json`` under
``<data_root>/commons/base_rates/`` (gitignored), and :func:`load_table` refuses a file that does
not reproduce its digest, or that is not the digest it was asked for.

**The build** is one ranged job over the whole range, never one invocation per date. Check
``uptime`` and running drivers first (CLAUDE.md), then::

    mkdir -p ~/campaign/m17
    nohup uv run python -m analyst.commons.base_rates build \\
        --start 2016-10-01 --end 2026-09-30 \\
        --data-root /home/ubuntu/stock-manager/data \\
        > ~/campaign/m17/base-rates-$(date -u +%Y-%m-%d).log 2>&1 &

It reads a year of sessions at a time (plus the 260-session look-back and the 60-session
horizon) to bound memory, and logs one line per year.

What it never does: read a wall clock for a date, call a model, or import `analyst.fundmanager`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from analyst.commons.features import (
    WINDOW_SESSIONS,
    NameFeatures,
    align,
    earnings_features,
    level_on_or_before,
    name_features,
)
from analyst.commons.inputs import PriceBar, ScreenSource
from analyst.commons.regime import REGIME_INDEX_SERIES, RegimeIndex, RegimeState
from analyst.commons.screens import (
    SCREENS_RULE_HASH,
    s1_trend_leaders,
    s2_volume_breakout,
    s3_pullback,
    s4_earnings_momentum,
)
from analyst.commons.sheets import (
    EquityBar,
    _median_traded_value,
    _size_ranks,
    canonical_bytes,
)
from analyst.commons.shortlist import (
    SHORTLIST_RULE_HASH,
    ShortlistFactors,
    log_liquidity,
    momentum_12_1,
    rank_shortlist,
    relative_strength,
)
from backtest.cap_tiers import tier_for_rank
from backtest.policies.earnings_surprise import EarningsSurprisePanel
from dataplatform.clock import Clock
from dataplatform.logging import get_logger
from dataplatform.query import PitContext, PitError

__all__ = [
    "ALL",
    "BASE_RATE_VERSION",
    "HORIZONS",
    "REGIME_KEYS",
    "SAMPLE_EVERY",
    "SCREEN_KEYS",
    "THIN_N",
    "TIER_KEYS",
    "BaseRateCell",
    "BaseRateParameters",
    "BaseRateTable",
    "Observation",
    "build_base_rate_table",
    "cell_stats",
    "load_table",
    "main",
    "observe_session",
    "table_from_observations",
    "write_table",
]

_LOG = get_logger(__name__)

BASE_RATE_VERSION: Final = "commons-base-rates/1"
HORIZONS: Final[tuple[int, ...]] = (5, 20, 60)
SAMPLE_EVERY: Final = 5
THIN_N: Final = 30
ALL: Final = "ALL"
SCREEN_KEYS: Final[tuple[str, ...]] = ("S1", "S2", "S3", "S4", "COMPOSITE", "NONE")
TIER_KEYS: Final[tuple[str, ...]] = ("large", "mid", "small", "unranked")
REGIME_KEYS: Final[tuple[str, ...]] = tuple(s.value for s in RegimeState)
DEFAULT_START: Final = date(2016, 10, 1)
DEFAULT_END: Final = date(2026, 9, 30)
DEFAULT_FLOOR_INR: Final = Decimal(10_000_000)
DEFAULT_SERIES: Final = "EQ"
MEDIAN_LOOKBACK: Final = 20
#: The whole calendar is read once; this bounds it (2006 to 2026 is about 5,000 sessions).
_CALENDAR_MAX: Final = 8_000
_RS_SESSIONS: Final = 20
_COMPOSITE_SIZE: Final = 40

_CONTEXT: Final = Context(prec=28, rounding=ROUND_HALF_EVEN)
_Q6: Final = Decimal("0.000001")
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BaseRateParameters(_Model):
    """What a table was built over. Part of the digest."""

    start: date
    end: date
    series: str
    floor_inr: Decimal
    median_lookback_sessions: int
    sample_every: int
    horizons: tuple[int, ...]
    thin_n: int
    composite_size: int


class BaseRateCell(_Model):
    """One bucket: screen x tier x regime x horizon. ``thin`` when ``n < 30``."""

    screen: str
    tier: str
    regime: str
    horizon: int
    n: int
    p_beat: Decimal | None
    median_excess: Decimal | None
    q25_excess: Decimal | None
    q75_excess: Decimal | None
    iqr_excess: Decimal | None
    thin: bool

    @property
    def cell_id(self) -> str:
        return cell_id(self.screen, self.tier, self.regime, self.horizon)


def cell_id(screen: str, tier: str, regime: str, horizon: int) -> str:
    """The citable id of one cell, e.g. ``S1.large.RISK_ON.h20``."""
    return f"{screen}.{tier}.{regime}.h{horizon}"


class BaseRateTable(_Model):
    """The frozen table and the digest that names it (module docstring)."""

    version: str
    screens_rule_hash: str
    shortlist_rule_hash: str
    parameters: BaseRateParameters
    sessions_sampled: int
    first_session: date | None
    last_session: date | None
    observations: int
    cells: tuple[BaseRateCell, ...]
    digest: str
    built_at: datetime

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"digest", "built_at"})

    @staticmethod
    def digest_of(body: Mapping[str, Any]) -> str:
        return hashlib.sha256(canonical_bytes(dict(body))).hexdigest()

    def verify(self) -> None:
        """Recompute the digest; ``ValueError`` if it does not reproduce."""
        expected = self.digest_of(self.body())
        if expected != self.digest:
            raise ValueError(
                f"base-rate table {self.digest[:12]} does not reproduce its digest: "
                f"recomputed {expected[:12]}"
            )

    def cell(self, screen: str, tier: str, regime: str, horizon: int) -> BaseRateCell:
        """The cell for the key; ``KeyError`` for a key outside the table."""
        wanted = cell_id(screen, tier, regime, horizon)
        for found in self.cells:
            if found.cell_id == wanted:
                return found
        raise KeyError(wanted)


# ── statistics ───────────────────────────────────────────────────────────────────────────────────


def _quantile(ordered: Sequence[Decimal], p: Decimal) -> Decimal:
    """Linear interpolation between closest ranks (numpy's default), on sorted values."""
    pos = p * Decimal(len(ordered) - 1)
    lo = int(pos)
    frac = pos - Decimal(lo)
    if lo + 1 >= len(ordered):
        return ordered[-1]
    return ordered[lo] + (ordered[lo + 1] - ordered[lo]) * frac


def cell_stats(
    screen: str, tier: str, regime: str, horizon: int, excess: Sequence[Decimal]
) -> BaseRateCell:
    """One cell from its excess returns. An empty cell has ``n = 0`` and no statistics."""
    with localcontext(_CONTEXT):
        n = len(excess)
        if n == 0:
            return BaseRateCell(
                screen=screen,
                tier=tier,
                regime=regime,
                horizon=horizon,
                n=0,
                p_beat=None,
                median_excess=None,
                q25_excess=None,
                q75_excess=None,
                iqr_excess=None,
                thin=True,
            )
        ordered = sorted(excess)
        q25 = _quantile(ordered, Decimal("0.25"))
        q75 = _quantile(ordered, Decimal("0.75"))
        return BaseRateCell(
            screen=screen,
            tier=tier,
            regime=regime,
            horizon=horizon,
            n=n,
            p_beat=(Decimal(sum(1 for x in ordered if x > _ZERO)) / Decimal(n)).quantize(_Q6),
            median_excess=_quantile(ordered, Decimal("0.5")).quantize(_Q6),
            q25_excess=q25.quantize(_Q6),
            q75_excess=q75.quantize(_Q6),
            iqr_excess=(q75 - q25).quantize(_Q6),
            thin=n < THIN_N,
        )


# ── one session's observations ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Observation:
    """One (session, name): its screens, its tier, the regime, and its excess per horizon.

    A slotted dataclass, not a model: a decade holds about half a million of them.
    """

    session: date
    isin: str
    screens: tuple[str, ...]
    tier: str
    regime: str | None
    excess: tuple[tuple[int, Decimal], ...]


def _forward_close(slots: Sequence[PriceBar | None], i: int, h: int) -> Decimal | None:
    """The close ``h`` sessions after slot ``i``, or the last close after ``i`` before it."""
    for j in range(min(i + h, len(slots) - 1), i, -1):
        bar = slots[j]
        if bar is not None:
            return bar.close
    return None


def observe_session(
    i: int,
    *,
    calendar: Sequence[date],
    raw: Mapping[str, Mapping[date, EquityBar]],
    aligned: Mapping[str, Sequence[PriceBar | None]],
    sectors: Mapping[str, str | None],
    panel: EarningsSurprisePanel,
    regime_index: RegimeIndex,
    levels: Sequence[tuple[date, Decimal]],
    floor_inr: Decimal,
    horizons: Sequence[int] = HORIZONS,
    composite_size: int = _COMPOSITE_SIZE,
) -> list[Observation]:
    """Every universe name's observation on ``calendar[i]`` (module docstring).

    ``raw`` holds every name's raw bars (the floor and the size rank read the whole market);
    ``aligned`` the universe names' adjusted bars on ``calendar``. Inputs to the screens are cut
    at ``calendar[i]``; only the outcome reads past it. A window whose last bar is not dated on
    the session raises :class:`PitError`.
    """
    session = calendar[i]
    with localcontext(_CONTEXT):
        window = calendar[max(0, i - WINDOW_SESSIONS + 1) : i + 1]
        medians = _median_traded_value(raw, window[-MEDIAN_LOOKBACK:])
        sizes = _size_ranks(raw, window)
        universe = sorted(
            isin
            for isin, by_date in raw.items()
            if session in by_date and medians.get(isin, _ZERO) >= floor_inr and isin in aligned
        )
        features: dict[str, NameFeatures] = {}
        for isin in universe:
            slots = aligned[isin][max(0, i - WINDOW_SESSIONS + 1) : i + 1]
            last = slots[-1] if slots else None
            if last is not None and last.trade_date != session:
                raise PitError(f"{isin} window ends {last.trade_date}, not {session}")
            f = name_features(isin, slots)
            if f is not None:
                features[isin] = f
        eligible = [features[k] for k in sorted(features)]
        known = [lv for lv in levels if lv[0] <= session]
        earnings = [
            e
            for f in eligible
            if (
                e := earnings_features(
                    f.isin,
                    aligned[f.isin][max(0, i - WINDOW_SESSIONS + 1) : i + 1],
                    window,
                    panel=panel,
                    index_levels=known,
                )
            )
            is not None
        ]
        on: dict[str, set[str]] = defaultdict(set)
        for key, entries in (
            ("S1", s1_trend_leaders(eligible, sectors)),
            ("S2", s2_volume_breakout(eligible)),
            ("S3", s3_pullback(eligible)),
            ("S4", s4_earnings_momentum(earnings)),
        ):
            for entry in entries:
                on[entry.isin].add(key)

        index_now = level_on_or_before(known, session)
        index_then = (
            level_on_or_before(known, calendar[i - _RS_SESSIONS]) if i >= _RS_SESSIONS else None
        )
        index_return = (
            index_now / index_then - _ONE
            if index_now is not None and index_then is not None and index_then > _ZERO
            else None
        )
        factors = []
        for f in eligible:
            slots = aligned[f.isin]
            base_12 = slots[i - 252] if i >= 252 else None
            base_1 = slots[i - 21] if i >= 21 else None
            back = next(
                (b for b in reversed(slots[max(0, i - 260) : i - _RS_SESSIONS + 1]) if b),
                None,
            )
            r20 = f.close / back.close - _ONE if back is not None and back.close > _ZERO else None
            factors.append(
                ShortlistFactors(
                    isin=f.isin,
                    momentum_12_1=momentum_12_1(
                        base_12.close if base_12 else None, base_1.close if base_1 else None
                    ),
                    relative_strength_20=relative_strength(r20, index_return),
                    earnings_surprise=panel.value(f.isin, session),
                    log_liquidity=log_liquidity(medians[f.isin]),
                )
            )
        composite = rank_shortlist(factors, size=composite_size) if factors else ()
        for listed in composite:
            on[listed.isin].add("COMPOSITE")

        above50 = [f for f in eligible if f.sma50 is not None]
        above200 = [f for f in eligible if f.sma200 is not None]
        regime = regime_index.reading(
            session,
            breadth_above_sma50=(
                Decimal(sum(1 for f in above50 if f.close > (f.sma50 or _ZERO)))
                / Decimal(len(above50))
                if above50
                else None
            ),
            breadth_above_sma200=(
                Decimal(sum(1 for f in above200 if f.close > (f.sma200 or _ZERO)))
                / Decimal(len(above200))
                if above200
                else None
            ),
        ).state

        out: list[Observation] = []
        for f in eligible:
            excess: list[tuple[int, Decimal]] = []
            for h in horizons:
                if i + h >= len(calendar):
                    continue
                later = _forward_close(aligned[f.isin], i, h)
                index_later = level_on_or_before(levels, calendar[i + h])
                if later is None or index_later is None or index_now is None or index_now <= 0:
                    continue
                excess.append((h, (later / f.close - _ONE) - (index_later / index_now - _ONE)))
            rank = sizes.get(f.isin)
            tier = tier_for_rank(rank) if rank is not None else None
            out.append(
                Observation(
                    session=session,
                    isin=f.isin,
                    screens=tuple(sorted(on[f.isin])) or ("NONE",),
                    tier=tier.value if tier is not None else "unranked",
                    regime=regime.value if regime is not None else None,
                    excess=tuple(excess),
                )
            )
        return out


def table_from_observations(
    observations: Sequence[Observation],
    *,
    parameters: BaseRateParameters,
    sessions: Sequence[date],
    clock: Clock,
) -> BaseRateTable:
    """Bucket ``observations`` into every cell of the product (empty cells included)."""
    buckets: dict[tuple[str, str, str, int], list[Decimal]] = defaultdict(list)
    for obs in observations:
        for h, value in obs.excess:
            for screen in obs.screens:
                for tier in (obs.tier, ALL):
                    for regime in (obs.regime, ALL) if obs.regime is not None else (ALL,):
                        buckets[(screen, tier, regime, h)].append(value)
    cells = tuple(
        cell_stats(screen, tier, regime, h, buckets.get((screen, tier, regime, h), ()))
        for screen in SCREEN_KEYS
        for tier in (*TIER_KEYS, ALL)
        for regime in (*REGIME_KEYS, ALL)
        for h in parameters.horizons
    )
    ordered = sorted(set(sessions))
    draft = BaseRateTable(
        version=BASE_RATE_VERSION,
        screens_rule_hash=SCREENS_RULE_HASH,
        shortlist_rule_hash=SHORTLIST_RULE_HASH,
        parameters=parameters,
        sessions_sampled=len(ordered),
        first_session=ordered[0] if ordered else None,
        last_session=ordered[-1] if ordered else None,
        observations=len(observations),
        cells=cells,
        digest="0" * 64,
        built_at=clock.now(),
    )
    return draft.model_copy(update={"digest": BaseRateTable.digest_of(draft.body())})


# ── the ranged build ─────────────────────────────────────────────────────────────────────────────


def sample_sessions(
    calendar: Sequence[date], *, start: date, end: date, every: int, horizon: int
) -> list[int]:
    """Calendar positions of the observation dates (module docstring, "Sessions")."""
    positions = [k for k, day in enumerate(calendar) if start <= day <= end]
    if not positions:
        return []
    last = max(k for k, day in enumerate(calendar) if day <= end)
    return [k for k in positions[::every] if k + horizon <= last]


def build_base_rate_table(
    source: ScreenSource,
    *,
    start: date,
    end: date,
    clock: Clock,
    series: str = DEFAULT_SERIES,
    floor_inr: Decimal = DEFAULT_FLOOR_INR,
    sample_every: int = SAMPLE_EVERY,
    sectors: Mapping[str, str | None] | None = None,
) -> BaseRateTable:
    """Build the table over ``[start, end]`` in one ranged pass (module docstring).

    What it does: reads the calendar, NIFTY 500 and the filings once as of ``end``, then a year of
    sample sessions at a time: the whole market's raw bars (for the floor and the size rank) and
    the universe's adjusted bars, the observations of each sample session, and finally the cells.
    What it assumes: ``source`` is the lake; ``sectors`` (industry by ISIN, for S1's bonus) is the
    classification the caller has. History has no dated industry file before 2026-09, so the
    default is none: no name earns the bonus, and that is stated in the parameters' absence of a
    sector map rather than guessed.
    What it never does: read past ``end``, or call anything but ``source``.
    """
    if sample_every < 1 or start > end:
        raise ValueError("sample_every must be >= 1 and start <= end")
    pit = PitContext(as_of=end)
    horizon = max(HORIZONS)
    calendar = sorted(set(pit.admit(source.sessions(end, _CALENDAR_MAX))))
    samples = sample_sessions(calendar, start=start, end=end, every=sample_every, horizon=horizon)
    raw_levels = pit.admit(source.index_levels([REGIME_INDEX_SERIES], end))
    late = sum(1 for lv in raw_levels if lv.knowable_date > lv.session)
    # A level released after its own session (a revision, a late capture) is never used: the
    # regime of a past session may only read what was released by it.
    levels = sorted(
        (lv.session, lv.close)
        for lv in raw_levels
        if lv.series_id == REGIME_INDEX_SERIES and lv.knowable_date <= lv.session
    )
    regime_index = RegimeIndex(levels)
    facts = pit.admit(source.filings(end))
    panel = EarningsSurprisePanel(facts, calendar)
    _LOG.info(
        "commons.base_rates.start",
        start=start.isoformat(),
        end=end.isoformat(),
        sessions=len(calendar),
        samples=len(samples),
        levels=len(levels),
        late_levels=late,
        facts=len(facts),
    )
    by_year: dict[int, list[int]] = defaultdict(list)
    for k in samples:
        by_year[calendar[k].year].append(k)
    observations: list[Observation] = []
    for year in sorted(by_year):
        positions = by_year[year]
        lo = max(0, positions[0] - WINDOW_SESSIONS + 1)
        hi = min(len(calendar) - 1, positions[-1] + horizon)
        block = calendar[lo : hi + 1]
        raw_bars = pit.admit(source.equity_bars(block, series))
        raw: dict[str, dict[date, EquityBar]] = defaultdict(dict)
        for bar in raw_bars:
            raw[bar.isin][bar.trade_date] = bar
        members: set[str] = set()
        for k in positions:
            window = calendar[max(0, k - WINDOW_SESSIONS + 1) : k + 1]
            medians = _median_traded_value(raw, window[-MEDIAN_LOOKBACK:])
            members |= {
                isin
                for isin, by_date in raw.items()
                if calendar[k] in by_date and medians.get(isin, _ZERO) >= floor_inr
            }
        bars = pit.admit(source.price_bars(frozenset(members), block, series))
        aligned = align(bars, block)
        for k in positions:
            observations.extend(
                observe_session(
                    k - lo,
                    calendar=block,
                    raw=raw,
                    aligned=aligned,
                    sectors=sectors or {},
                    panel=panel,
                    regime_index=regime_index,
                    levels=levels,
                    floor_inr=floor_inr,
                )
            )
        _LOG.info(
            "commons.base_rates.year",
            year=year,
            samples=len(positions),
            members=len(members),
            observations=len(observations),
            state="BUILT",
        )
        del raw, raw_bars, bars, aligned
    parameters = BaseRateParameters(
        start=start,
        end=end,
        series=series,
        floor_inr=floor_inr,
        median_lookback_sessions=MEDIAN_LOOKBACK,
        sample_every=sample_every,
        horizons=HORIZONS,
        thin_n=THIN_N,
        composite_size=_COMPOSITE_SIZE,
    )
    table = table_from_observations(
        observations,
        parameters=parameters,
        sessions=[calendar[k] for k in samples],
        clock=clock,
    )
    _LOG.info(
        "commons.base_rates.built",
        digest=table.digest,
        observations=table.observations,
        sessions=table.sessions_sampled,
        thin=sum(1 for c in table.cells if c.thin),
        cells=len(table.cells),
    )
    return table


# ── storage ──────────────────────────────────────────────────────────────────────────────────────


def table_dir(data_root: Path) -> Path:
    return data_root / "commons" / "base_rates"


def write_table(table: BaseRateTable, out_dir: Path) -> Path:
    """Write ``table`` as canonical JSON named by its range and digest. Never overwrites."""
    table.verify()
    p = table.parameters
    path = out_dir / f"base_rates_{p.start:%Y%m%d}_{p.end:%Y%m%d}_{table.digest[:12]}.json"
    payload = json.dumps(table.model_dump(mode="json"), sort_keys=True, indent=1) + "\n"
    out_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if load_table(path).digest != table.digest:
            raise FileExistsError(f"{path} holds a different table")
        return path
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return path


def load_table(path: Path, *, expected_digest: str | None = None) -> BaseRateTable:
    """Read a frozen table; ``ValueError`` unless it reproduces its digest and the expected one."""
    table = BaseRateTable.model_validate(json.loads(path.read_text(encoding="utf-8")))
    table.verify()
    if expected_digest is not None and table.digest != expected_digest:
        raise ValueError(f"{path.name} is table {table.digest[:12]}, not {expected_digest[:12]}")
    return table


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """``build``: the ranged job of the module docstring. ``verify PATH``: re-check a file."""
    parser = argparse.ArgumentParser(prog="python -m analyst.commons.base_rates")
    sub = parser.add_subparsers(dest="verb", required=True)
    build = sub.add_parser("build", help="build and freeze the table over one range")
    build.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    build.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    build.add_argument("--data-root", type=Path, required=True)
    build.add_argument("--out", type=Path, default=None)
    check = sub.add_parser("verify", help="re-verify a frozen table file")
    check.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.verb == "verify":
        table = load_table(args.path)
        print(f"{args.path.name}: digest {table.digest} reproduces")
        return 0

    from analyst.commons.sources import LakeCommonsSource  # the lake adapter, only for the CLI
    from dataplatform.clock import SystemClock

    clock = SystemClock()
    with LakeCommonsSource(clock=clock, data_root=args.data_root) as source:
        table = build_base_rate_table(source, start=args.start, end=args.end, clock=clock)
    path = write_table(table, args.out or table_dir(args.data_root))
    print(f"{path}: digest {table.digest}, {table.observations} observations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
