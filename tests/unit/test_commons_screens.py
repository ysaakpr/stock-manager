"""M17.9 — Commons screens S1-S5, the dossier, the regime and the Amendment 1 (b) exclusions.

What these tests pin, against the acceptance criteria:

1. **Every screen rule points the right way, at its threshold.** For each condition of S1-S4 a
   name sitting exactly on the threshold is in and one just past it is out (or the reverse for a
   strict bound), and a higher score lists first. Inverting a comparison, moving a threshold, or
   flipping a rank direction fails a case here. The features those rules read are pinned on
   constructed price paths, so an inverted feature formula fails too.
2. **PIT.** A bar, a filing or an announcement dated after the session raises `PitError` from
   the build; nothing is quietly filtered.
3. **Exclusions.** An integrity-event announcement excludes its ISIN on the session it became
   knowable and the 59 sessions after it, and not on the 61st. A GSM/ESM list older than the last
   five sessions, or missing, blocks every new BUY and is recorded as a gap. A price band of 5 %
   or less excludes; "No Band" and other series do not.
4. **Regime.** The three states of study §4, RISK-OFF first.
5. **Dossier.** Every field has a stable id that resolves to its value, and an unknown id is
   refused. F&O build-up is the four-way classification.

Offline: a synthetic world (`tests.unit.commons_screen_world`), no lake, no network.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from analyst.commons import (
    SCREENS_RULE_HASH,
    CommonsScreens,
    ExclusionReason,
    UnknownFieldError,
    build_dossiers,
    build_screens,
    load_event_keywords,
    resolve_field,
)
from analyst.commons.dossier import DOSSIER_FIELDS, OiBuildup, oi_buildup
from analyst.commons.exclusions import compute_exclusions
from analyst.commons.features import (
    EarningsFeatures,
    NameFeatures,
    earnings_features,
    name_features,
)
from analyst.commons.inputs import PriceBandEntry, PriceBar
from analyst.commons.regime import RegimeIndex, RegimeState
from analyst.commons.screens import (
    S1_NEAR_HIGH_MIN,
    S1_SECTOR_BONUS,
    SCREEN_SIZE,
    s1_trend_leaders,
    s2_volume_breakout,
    s3_pullback,
    s4_earnings_momentum,
    screens_rule_source,
)
from analyst.commons.sheets import FilingFact, SurveillanceEntry
from backtest.policies.earnings_surprise import EarningsSurprisePanel
from dataplatform.ingest.xbrl.models import Nature
from dataplatform.query import PitError
from tests.unit.commons_screen_world import (
    BANDED,
    BREAKOUT,
    CALENDAR,
    FLAGGED,
    FUTURE_SESSIONS,
    LEADER,
    SESSION,
    FakeScreenSource,
    ScreenWorld,
    ann,
    clock,
    pbar,
    screen_world,
    sheets_for,
)
from tests.unit.test_commons_sheets import _isin

KEYWORDS = load_event_keywords()
A, B, C, D = (_isin(n) for n in (601, 602, 603, 604))
EPS = Decimal("0.00000001")


def _nf(isin: str, **values: Any) -> NameFeatures:
    base: dict[str, Any] = dict.fromkeys(NameFeatures.model_fields)
    base.update(isin=isin, close=Decimal(100))
    base.update(values)
    return NameFeatures(**base)


# ── S1 ───────────────────────────────────────────────────────────────────────────────────────────


def _s1(isin: str, **values: Any) -> NameFeatures:
    ok: dict[str, Any] = {
        "ret_6_1": Decimal("0.20"),
        "ret_12_1": Decimal("0.40"),
        "vol_252": Decimal("0.02"),
        "sma200": Decimal(90),
        "sma200_prev": Decimal(85),
        "near_high": Decimal("0.95"),
    }
    ok.update(values)
    return _nf(isin, **ok)


def _s1_pair(**candidate: Any) -> list[NameFeatures]:
    """``A`` (the case under test) and a weaker ``B`` that passes, so z-scores have a spread."""
    return [_s1(A, **candidate), _s1(B, ret_6_1=Decimal("0.05"), ret_12_1=Decimal("0.10"))]


def _s1_isins(features: list[NameFeatures]) -> list[str]:
    return [e.isin for e in s1_trend_leaders(features, {})]


def test_s1_ranks_the_stronger_momentum_first() -> None:
    # A is listed second in ISIN order but has the stronger momentum on both legs.
    strong = _s1(B, ret_6_1=Decimal("0.30"), ret_12_1=Decimal("0.60"))
    weak = _s1(A, ret_6_1=Decimal("0.10"), ret_12_1=Decimal("0.20"))
    assert _s1_isins([weak, strong]) == [B, A]
    # Each leg points up on its own.
    six = _s1(B, ret_6_1=Decimal("0.30"), ret_12_1=Decimal("0.20"))
    twelve = _s1(B, ret_6_1=Decimal("0.10"), ret_12_1=Decimal("0.60"))
    for better in (six, twelve):
        assert _s1_isins([weak, better]) == [B, A]


def test_s1_divides_momentum_by_volatility() -> None:
    calm = _s1(B, vol_252=Decimal("0.01"))
    wild = _s1(A, vol_252=Decimal("0.04"))
    assert _s1_isins([wild, calm]) == [B, A]


def test_s1_needs_close_above_a_rising_sma200() -> None:
    assert A in _s1_isins(_s1_pair())
    assert A not in _s1_isins(_s1_pair(sma200=Decimal(100)))  # close == SMA200: not above
    assert A not in _s1_isins(_s1_pair(sma200=Decimal(110)))
    assert A not in _s1_isins(_s1_pair(sma200_prev=Decimal(90)))  # flat SMA200: not rising
    assert A not in _s1_isins(_s1_pair(sma200_prev=Decimal(95)))  # falling


def test_s1_needs_the_close_within_15_pct_of_the_52_week_high() -> None:
    assert Decimal("0.85") == S1_NEAR_HIGH_MIN
    assert A in _s1_isins(_s1_pair(near_high=Decimal("0.85")))
    assert A not in _s1_isins(_s1_pair(near_high=Decimal("0.84999999")))


def test_s1_gives_the_bonus_to_a_top_half_industry() -> None:
    # A and B are identical; A's industry has the higher median 6-1 return (C lifts it), B's
    # the lower (D drags it). C and D fail the trend filter but still count for their industry.
    names = [
        _s1(A),
        _s1(B),
        _s1(C, ret_6_1=Decimal("0.90"), sma200=Decimal(120)),
        _s1(D, ret_6_1=Decimal("-0.50"), sma200=Decimal(120)),
    ]
    sectors = {A: "Up", C: "Up", B: "Down", D: "Down"}
    entries = s1_trend_leaders(names, sectors)
    assert [e.isin for e in entries] == [A, B]
    assert Decimal("0.5") == S1_SECTOR_BONUS  # this module's frozen choice (the study names none)
    assert entries[0].score - entries[1].score == Decimal("0.5")
    # Swap the industries and the order swaps with them, against the ISIN tie-break.
    swapped = s1_trend_leaders(names, {A: "Down", D: "Down", B: "Up", C: "Up"})
    assert [e.isin for e in swapped] == [B, A]


def test_s1_needs_volatility_and_both_legs() -> None:
    assert A not in _s1_isins(_s1_pair(vol_252=Decimal(0)))
    assert A not in _s1_isins(_s1_pair(ret_12_1=None))


def test_a_ranked_screen_stops_at_fifteen_and_breaks_ties_by_isin() -> None:
    assert SCREEN_SIZE == 15
    names = [_isin(700 + k) for k in range(20)]
    same = [
        _nf(
            i,
            at_high_60=True,
            volume_ratio_day0=Decimal(3),
            ret_5=Decimal(0),
            atr_contraction=Decimal("0.5"),
        )
        for i in reversed(names)
    ]
    entries = s2_volume_breakout(same)
    assert [e.isin for e in entries] == sorted(names)[:15]
    assert [e.position for e in entries] == list(range(1, 16))


# ── S2 ───────────────────────────────────────────────────────────────────────────────────────────


def _s2(isin: str = A, **values: Any) -> NameFeatures:
    ok: dict[str, Any] = {
        "at_high_60": True,
        "volume_ratio_day0": Decimal(3),
        "ret_5": Decimal("0.05"),
        "atr_contraction": Decimal("0.5"),
    }
    ok.update(values)
    return _nf(isin, **ok)


@pytest.mark.parametrize(
    ("field", "inside", "outside"),
    [
        ("volume_ratio_day0", Decimal(2), Decimal(2) - EPS),
        ("ret_5", Decimal("0.15") - EPS, Decimal("0.15")),
        ("atr_contraction", Decimal("0.8") - EPS, Decimal("0.8")),
        ("at_high_60", True, False),
    ],
)
def test_s2_thresholds(field: str, inside: Any, outside: Any) -> None:
    assert [e.isin for e in s2_volume_breakout([_s2(**{field: inside})])] == [A]
    assert s2_volume_breakout([_s2(**{field: outside})]) == ()


def test_s2_ranks_by_volume_ratio() -> None:
    entries = s2_volume_breakout(
        [_s2(A, volume_ratio_day0=Decimal(2)), _s2(B, volume_ratio_day0=Decimal(5))]
    )
    assert [e.isin for e in entries] == [B, A]
    assert entries[0].score == Decimal(5)


# ── S3 ───────────────────────────────────────────────────────────────────────────────────────────


def _s3(isin: str = A, **values: Any) -> NameFeatures:
    ok: dict[str, Any] = {
        "ret_6_1": Decimal("0.50"),
        "sma200": Decimal(90),
        "days_since_high_20": 5,
        "pullback_pct": Decimal("0.08"),
        "pullback_volume_ratio": Decimal("0.7"),
        "dist_sma20": Decimal("0.01"),
        "dist_sma50": Decimal("0.10"),
    }
    ok.update(values)
    return _nf(isin, **ok)


def _laggards(count: int = 8) -> list[NameFeatures]:
    """Names with low 6-1 momentum that fail S3 anyway: the population of the quintile."""
    return [_nf(_isin(800 + k), ret_6_1=Decimal(k) / 100) for k in range(count)]


def _s3_isins(candidate: NameFeatures) -> list[str]:
    return [e.isin for e in s3_pullback([candidate, *_laggards()])]


@pytest.mark.parametrize(
    ("field", "inside", "outside"),
    [
        ("days_since_high_20", 3, 2),
        ("days_since_high_20", 10, 11),
        ("pullback_pct", Decimal("0.04"), Decimal("0.04") - EPS),
        ("pullback_pct", Decimal("0.12"), Decimal("0.12") + EPS),
        ("pullback_volume_ratio", Decimal(1) - EPS, Decimal(1)),
        ("sma200", Decimal(100) - EPS, Decimal(100)),
    ],
)
def test_s3_thresholds(field: str, inside: Any, outside: Any) -> None:
    assert _s3_isins(_s3(**{field: inside})) == [A]
    assert _s3_isins(_s3(**{field: outside})) == []


def test_s3_needs_the_close_near_its_sma20_or_sma50() -> None:
    assert _s3_isins(_s3(dist_sma20=Decimal("0.03"))) == [A]
    assert _s3_isins(_s3(dist_sma20=Decimal("-0.03"))) == [A]
    assert _s3_isins(_s3(dist_sma20=Decimal("0.03") + EPS)) == []
    assert _s3_isins(_s3(dist_sma20=Decimal("0.2"), dist_sma50=Decimal("-0.02"))) == [A]


def test_s3_takes_only_the_top_momentum_quintile() -> None:
    # Ten names: percentile ranks 0, 1/9 ... 1. Only the top two reach 0.8.
    top, second, third = (
        _s3(A, ret_6_1=Decimal("0.9")),
        _s3(B, ret_6_1=Decimal("0.8")),
        _s3(C, ret_6_1=Decimal("0.7")),
    )
    entries = s3_pullback([third, second, top, *_laggards(7)])
    assert [e.isin for e in entries] == [A, B]  # ranked by momentum, strongest first


# ── S4 ───────────────────────────────────────────────────────────────────────────────────────────


def _ef(isin: str = A, **values: Any) -> EarningsFeatures:
    ok: dict[str, Any] = {
        "sue": Decimal(3),
        "filing_date": SESSION - timedelta(days=10),
        "period_end": date(2026, 6, 30),
        "day0": SESSION - timedelta(days=10),
        "sessions_since_day0": 7,
        "ear": Decimal("0.03"),
        "day0_volume_ratio": Decimal(3),
    }
    ok.update(values)
    return EarningsFeatures(isin=isin, **ok)


def _sue_population() -> list[EarningsFeatures]:
    return [_ef(_isin(850 + k), sue=Decimal(k) / 10, ear=Decimal(-1)) for k in range(8)]


def _s4_isins(candidate: EarningsFeatures) -> list[str]:
    return [e.isin for e in s4_earnings_momentum([candidate, *_sue_population()])]


@pytest.mark.parametrize(
    ("field", "inside", "outside"),
    [
        ("sessions_since_day0", 29, 30),
        ("ear", EPS, Decimal(0)),
        ("day0_volume_ratio", Decimal(2), Decimal(2) - EPS),
    ],
)
def test_s4_thresholds(field: str, inside: Any, outside: Any) -> None:
    assert _s4_isins(_ef(**{field: inside})) == [A]
    assert _s4_isins(_ef(**{field: outside})) == []


def test_s4_takes_only_the_top_sue_quintile_and_ranks_sue_plus_ear() -> None:
    weak = _ef(A, sue=Decimal("0.05"))  # ranks among the laggards' SUEs: not top quintile
    assert [e.isin for e in s4_earnings_momentum([weak, *_sue_population()])] == []
    big_sue = _ef(B, sue=Decimal(9), ear=Decimal("0.02"))
    big_both = _ef(C, sue=Decimal(10), ear=Decimal("0.05"))
    entries = s4_earnings_momentum([big_sue, big_both, *_sue_population()])
    assert [e.isin for e in entries] == [C, B]


# ── features ─────────────────────────────────────────────────────────────────────────────────────


def _path(
    closes: list[Decimal], *, volumes: list[int] | None = None, rng: str = "0.01"
) -> list[PriceBar | None]:
    days = CALENDAR[-len(closes) :]
    return [
        pbar(A, day, c, rng=Decimal(rng), volume=(volumes[k] if volumes else 100_000))
        for k, (day, c) in enumerate(zip(days, closes, strict=True))
    ]


def test_features_on_a_rising_path_point_up() -> None:
    closes = [Decimal(100) + Decimal(k) for k in range(261)]
    f = name_features(A, _path(closes))
    assert f is not None
    assert f.ret_6_1 == (closes[-22] / closes[-127] - 1).quantize(EPS)
    assert f.ret_12_1 == (closes[-22] / closes[-253] - 1).quantize(EPS)
    assert f.ret_5 == (closes[-1] / closes[-6] - 1).quantize(EPS)
    assert f.sma200 is not None and f.sma200_prev is not None and f.sma200 > f.sma200_prev
    assert f.dist_sma200 is not None and f.dist_sma200 > 0
    assert f.near_high == 1 and f.from_high_252 == 0
    assert f.at_high_60 is True and f.days_since_high_20 == 0
    assert f.vol_252 is not None and f.vol_252 > 0


def test_a_missing_bar_leaves_a_window_feature_undefined() -> None:
    window = _path([Decimal(100) + Decimal(k) for k in range(261)])
    window[-100] = None
    f = name_features(A, window)
    assert f is not None and f.sma200 is None and f.vol_252 is None
    assert f.sma50 is not None  # a shorter window that avoids the hole is still defined
    assert name_features(A, [*window[:-1], None]) is None  # no bar on the session: no features


def test_atr_volume_ratios_and_the_contraction() -> None:
    closes = [Decimal(100)] * 70
    volumes = [100_000] * 69 + [300_000]
    f = name_features(A, _path(closes, volumes=volumes, rng="0.01"))
    assert f is not None
    assert f.atr14 == Decimal(2) and f.atr14_pct == Decimal("0.02")
    assert f.volume_ratio_day0 == Decimal(3)
    assert f.volume_ratio_5 == Decimal("1.4")  # (4 x 1 + 3) / 5
    wide = _path(closes[:59], rng="0.03")
    tight = _path(closes[:11], rng="0.005")
    joined = [*wide, *tight]
    days = CALENDAR[-len(joined) :]
    window = [replace(b, trade_date=d) for b, d in zip(joined, days, strict=True) if b is not None]
    g = name_features(A, window)
    assert g is not None and g.atr_contraction is not None and g.atr_contraction < Decimal("0.8")
    assert g.atr10_prev == Decimal(1) and g.atr50_prev is not None and g.atr50_prev > Decimal(1)


def test_the_pullback_is_dated_from_the_latest_20_session_high() -> None:
    closes = [Decimal(100) + Decimal(k) for k in range(30)] + [
        Decimal(125),
        Decimal(124),
        Decimal(123),
    ]
    f = name_features(A, _path(closes))
    assert f is not None
    assert f.high_20 == Decimal(129) and f.days_since_high_20 == 3
    assert f.pullback_pct == (1 - Decimal(123) / Decimal(129)).quantize(EPS)


def test_delivered_quantity_z_score_is_against_the_names_own_60_sessions() -> None:
    days = CALENDAR[-61:]
    window: list[PriceBar | None] = [
        pbar(A, d, Decimal(100), deliv=(90 if k % 2 else 110)) for k, d in enumerate(days[:-1])
    ]
    window.append(pbar(A, days[-1], Decimal(100), deliv=150))
    f = name_features(A, window)
    assert f is not None and f.deliv_z60 is not None and f.deliv_z60 > Decimal(4)
    window[-1] = pbar(A, days[-1], Decimal(100), deliv=50)
    g = name_features(A, window)
    assert g is not None and g.deliv_z60 is not None and g.deliv_z60 < Decimal(-4)


def _eps_facts(isin: str, eps: list[str], filed_last: date) -> list[FilingFact]:
    """Thirteen consecutive standalone quarters of EPS with a share count, the last filed then."""
    ends: list[date] = [date(2026, 6, 30)]
    while len(ends) < len(eps):
        first = ends[-1].replace(day=1)
        for _ in range(2):
            first = (first - timedelta(days=1)).replace(day=1)
        ends.append(first - timedelta(days=1))
    ends.reverse()
    out: list[FilingFact] = []
    for k, (end, value) in enumerate(zip(ends, eps, strict=True)):
        start = (end - timedelta(days=80)).replace(day=1)
        filed = filed_last if k == len(eps) - 1 else end + timedelta(days=40)
        for concept, v in (("eps_basic", value), ("shares_outstanding", "1000000")):
            out.append(
                FilingFact(
                    isin,
                    start,
                    end,
                    filed,
                    f"{isin}-{k}",
                    Nature.STANDALONE,
                    concept,
                    None,
                    Decimal(v),
                )
            )
    return out


def test_sue_and_ear_come_from_the_pit_filing_date() -> None:
    days = CALENDAR[-80:]
    day0 = days[-6]
    closes = [Decimal(100)] * 73 + [
        Decimal(100),
        Decimal(110),
        Decimal(110),
        Decimal(110),
        Decimal(110),
        Decimal(110),
        Decimal(110),
    ]
    volumes = [100_000] * 74 + [400_000] + [100_000] * 5
    window: list[PriceBar | None] = [
        pbar(A, d, c, volume=v) for d, c, v in zip(days, closes, volumes, strict=True)
    ]
    eps = [
        "1.0",
        "1.1",
        "1.0",
        "1.2",
        "1.1",
        "1.3",
        "1.2",
        "1.4",
        "1.3",
        "1.5",
        "1.4",
        "1.6",
        "3.0",
    ]
    facts = _eps_facts(A, eps, day0)
    panel = EarningsSurprisePanel(facts, days)
    flat_index = [(d, Decimal(1000)) for d in days]
    e = earnings_features(A, window, days, panel=panel, index_levels=flat_index)
    assert e is not None and e.sue > 0
    assert e.day0 == day0 and e.sessions_since_day0 == 5
    assert e.ear == Decimal("0.1") and e.day0_volume_ratio == Decimal(4)
    # The index rising more than the stock makes the EAR negative: it is an excess, not a return.
    hot = [(d, Decimal(1000) if d < day0 else Decimal(1300)) for d in days]
    hotter = earnings_features(A, window, days, panel=panel, index_levels=hot)
    assert hotter is not None and hotter.ear is not None and hotter.ear < 0
    # Before the filing there is no reading of this quarter.
    before = days.index(day0) - 1
    early = earnings_features(
        A,
        window[: before + 1],
        days[: before + 1],
        panel=panel,
        index_levels=flat_index[: before + 1],
    )
    assert early is None or early.filing_date < day0


# ── exclusions ───────────────────────────────────────────────────────────────────────────────────

STAGES = ("ESM", "GSM")


def _fresh_lists(day: date) -> dict[str, list[SurveillanceEntry] | None]:
    return {s: [SurveillanceEntry(isin=_isin(999), stage=s, knowable_date=day)] for s in STAGES}


def _exclude(calendar: list[date], **kwargs: Any) -> Any:
    args: dict[str, Any] = {
        "calendar": calendar,
        "series": "EQ",
        "stages": STAGES,
        "surveillance": _fresh_lists(calendar[-1]),
        "bands": [],
        "announcements": [ann(_isin(998), "1", calendar[0], "Trading Window")],
        "keywords": KEYWORDS,
    }
    args.update(kwargs)
    return compute_exclusions([A, B], **args)


def test_an_integrity_event_excludes_for_exactly_60_sessions() -> None:
    event_day = CALENDAR[150]
    event = ann(A, "77", event_day, "Resignation of Statutory Auditor")
    background = ann(_isin(998), "1", CALENDAR[0], "Trading Window")
    for offset, excluded in ((0, True), (1, True), (59, True), (60, False), (61, False)):
        calendar = CALENDAR[: 150 + offset + 1]
        result = _exclude(
            calendar,
            announcements=[background, event],
            future_sessions=CALENDAR[150 + offset + 1 :],
        )
        assert (A in result.isins) is excluded, offset
        if excluded:
            (row,) = result.excluded
            assert row.reasons == (ExclusionReason.INTEGRITY_EVENT,)
            assert row.events[0].categories == ("auditor_resignation",)
            assert row.events[0].excluded_through == CALENDAR[150 + 59]


def test_a_weekend_announcement_counts_from_the_next_session() -> None:
    monday = next(d for d in CALENDAR[100:] if d.weekday() == 0)
    i = CALENDAR.index(monday)
    saturday = monday - timedelta(days=2)
    event = ann(A, "78", saturday, "Resignation of Statutory Auditor")
    background = ann(_isin(998), "1", CALENDAR[0], "Trading Window")
    assert A in _exclude(CALENDAR[: i + 60], announcements=[background, event]).isins
    assert A not in _exclude(CALENDAR[: i + 61], announcements=[background, event]).isins


def test_a_non_integrity_announcement_does_not_exclude() -> None:
    event = ann(
        A, "79", CALENDAR[-3], "Appointment", "Appointment of Mr X as Chief Financial Officer"
    )
    assert _exclude(CALENDAR, announcements=[event]).isins == frozenset()


def test_a_stale_or_missing_surveillance_list_blocks_new_buys() -> None:
    fresh = _exclude(CALENDAR, surveillance=_fresh_lists(CALENDAR[-5]))
    assert not fresh.buys_blocked and fresh.block_reason is None
    stale = _exclude(CALENDAR, surveillance=_fresh_lists(CALENDAR[-6]))
    assert stale.buys_blocked and stale.block_reason is not None
    assert any(g.source == "surveillance" and "no new BUY" in g.reason for g in stale.gaps)
    one_missing = _exclude(
        CALENDAR, surveillance={"GSM": None, "ESM": _fresh_lists(CALENDAR[-1])["ESM"]}
    )
    assert one_missing.buys_blocked and one_missing.surveillance_lists["GSM"] is None
    assert "GSM list unavailable" in (one_missing.block_reason or "")


def test_a_fresh_list_excludes_its_members() -> None:
    lists = {s: [SurveillanceEntry(isin=B, stage=s, knowable_date=CALENDAR[-1])] for s in STAGES}
    result = _exclude(CALENDAR, surveillance=lists)
    (row,) = result.excluded
    assert row.isin == B and row.reasons == (ExclusionReason.SURVEILLANCE,)
    assert row.surveillance == STAGES


@pytest.mark.parametrize(
    ("band", "series", "excluded"),
    [
        (Decimal(2), "EQ", True),
        (Decimal(5), "EQ", True),
        (Decimal(10), "EQ", False),
        (None, "EQ", False),
        (Decimal(2), "BE", False),
    ],
)
def test_a_price_band_of_5_pct_or_less_excludes(
    band: Decimal | None, series: str, excluded: bool
) -> None:
    bands = [PriceBandEntry(isin=A, series=series, band_pct=band, knowable_date=CALENDAR[-1])]
    result = _exclude(CALENDAR, bands=bands)
    assert (A in result.isins) is excluded
    if excluded:
        assert result.excluded[0].band_pct == band


# ── the regime ───────────────────────────────────────────────────────────────────────────────────


def _levels(values: list[Decimal]) -> list[tuple[date, Decimal]]:
    days = [
        d
        for d in (date(2023, 1, 2) + timedelta(days=k) for k in range(len(values) * 2))
        if d.weekday() < 5
    ]
    return list(zip(days[: len(values)], values, strict=True))


def _rising(n: int = 600) -> list[Decimal]:
    return [
        Decimal(100) * Decimal("1.001") ** k * (1 + Decimal("0.002") * (k % 2)) for k in range(n)
    ]


def test_regime_risk_on_needs_a_rising_index_and_broad_breadth() -> None:
    levels = _levels(_rising())
    index, last = RegimeIndex(levels), levels[-1][0]
    on = index.reading(last, breadth_above_sma50=Decimal("0.5"))
    assert on.state is RegimeState.RISK_ON and on.sma200_rising is True
    assert index.reading(last, breadth_above_sma50=Decimal("0.49999")).state is RegimeState.NEUTRAL
    assert index.reading(last, breadth_above_sma50=None).state is None


def test_regime_risk_off_below_sma200_with_a_negative_24_month_return() -> None:
    up = [Decimal(100) + Decimal(k) / 4 for k in range(400)]
    down = [up[-1] - Decimal(k) for k in range(1, 201)]  # ends at 0.25 x 399 + 100 - 200 = ~ -0.25
    values = [max(v, Decimal(1)) for v in up + down]
    levels = _levels(values)
    reading = RegimeIndex(levels).reading(levels[-1][0], breadth_above_sma50=Decimal("0.9"))
    assert reading.return_24m is not None and reading.return_24m < 0
    assert reading.state is RegimeState.RISK_OFF
    assert "24-month" in reading.reason


def _calm_then(tail: list[Decimal]) -> list[tuple[date, Decimal]]:
    calm = [
        Decimal(100) * Decimal("1.002") ** k * (1 + Decimal("0.001") * (k % 2)) for k in range(500)
    ]
    return _levels(calm + tail)


def test_regime_risk_off_below_sma200_in_its_most_volatile_fifth() -> None:
    top = Decimal(100) * Decimal("1.002") ** 499
    # Ends on a low (k = 130 is even), below the SMA200 the calm top still lifts.
    choppy = [top * (Decimal("0.80") + Decimal("0.06") * (1 if k % 2 else -1)) for k in range(131)]
    levels = _calm_then(choppy)
    reading = RegimeIndex(levels).reading(levels[-1][0], breadth_above_sma50=Decimal("0.9"))
    assert reading.return_24m is not None and reading.return_24m > 0
    assert reading.vol_top_20 is True and reading.state is RegimeState.RISK_OFF


def test_regime_neutral_below_sma200_when_calm_and_up_over_two_years() -> None:
    top = Decimal(100) * Decimal("1.002") ** 499
    gentle = [top * Decimal("0.999") ** k for k in range(1, 131)]
    levels = _calm_then(gentle)
    reading = RegimeIndex(levels).reading(levels[-1][0], breadth_above_sma50=Decimal("0.9"))
    assert reading.index_close is not None and reading.sma200 is not None
    assert reading.index_close < reading.sma200
    assert reading.vol_top_20 is False and reading.state is RegimeState.NEUTRAL


def test_regime_reads_no_level_after_the_session_and_says_when_stale() -> None:
    levels = _levels(_rising())
    index = RegimeIndex(levels)
    mid = levels[300][0]
    assert index.reading(mid, breadth_above_sma50=Decimal("0.6")).index_session == mid
    stale = index.reading(levels[-1][0] + timedelta(days=30), breadth_above_sma50=Decimal("0.6"))
    assert stale.state is None and "within" in stale.reason


# ── the builder over the synthetic world ─────────────────────────────────────────────────────────


def _build(world: ScreenWorld | None = None, **kwargs: Any) -> CommonsScreens:
    world = world or screen_world()
    return build_screens(
        sheets_for(world),
        shortlist=None,
        source=FakeScreenSource(world, missing=kwargs.pop("missing", frozenset())),
        clock=kwargs.pop("clock", clock()),
        future_sessions=FUTURE_SESSIONS,
        **kwargs,
    )


@pytest.fixture(scope="module")
def built() -> CommonsScreens:
    return _build()


def test_the_build_lists_each_screen_and_its_facts(built: CommonsScreens) -> None:
    assert built.trading_date == SESSION and built.rule_hash == SCREENS_RULE_HASH
    assert built.s1[0].isin == LEADER
    assert [e.isin for e in built.s2] == [BREAKOUT]
    s5 = {(e.isin, e.categories) for e in built.s5}
    assert (LEADER, ("order_win",)) in s5 and (LEADER, ("bulk_deal",)) in s5
    meeting = next(e for e in built.s5 if e.isin == BREAKOUT)
    assert meeting.meeting_date == FUTURE_SESSIONS[3]
    deal = next(e for e in built.s5 if e.categories == ("bulk_deal",))
    assert deal.pct_equity == Decimal("1.0000")  # 100,000 of 10,000,000 shares
    assert built.regime.state is RegimeState.RISK_ON
    assert built.eligible == len(built.features) - 2


def test_excluded_names_are_on_no_screen(built: CommonsScreens) -> None:
    reasons = {e.isin: e.reasons for e in built.exclusions.excluded}
    assert reasons == {
        FLAGGED: (ExclusionReason.INTEGRITY_EVENT,),
        BANDED: (ExclusionReason.PRICE_BAND,),
    }
    screened = built.digest_scope(None)
    assert FLAGGED not in screened and BANDED not in screened
    # FLAGGED's trend is the strongest in the world: only the exclusion keeps it off S1.
    flagged = next(f.ret_12_1 for f in built.features if f.isin == FLAGGED)
    leader = next(f.ret_12_1 for f in built.features if f.isin == LEADER)
    assert flagged is not None and leader is not None and flagged > leader
    assert not built.exclusions.buys_blocked


def test_a_stale_gsm_list_blocks_buys_in_the_build() -> None:
    world = screen_world()
    world.surveillance["GSM"] = [
        replace(e, knowable_date=CALENDAR[-6]) for e in world.surveillance["GSM"]
    ]
    screens = _build(world)
    assert screens.exclusions.buys_blocked
    assert any(g.source == "surveillance" for g in screens.gaps)
    missing = screen_world()
    del missing.surveillance["ESM"]
    assert _build(missing).exclusions.buys_blocked


@pytest.mark.parametrize("kind", ["bar", "filing", "announcement", "deal", "band"])
def test_a_future_dated_input_trips_the_pit_guard(kind: str) -> None:
    world = screen_world()
    later = SESSION + timedelta(days=1)
    if kind == "bar":
        world.bars.append(pbar(LEADER, later, Decimal(500)))
    elif kind == "filing":
        world.filings.append(replace(world.filings[0], filing_date=later))
    elif kind == "announcement":
        world.announcements.append(ann(LEADER, "99", later, "Buyback"))
    elif kind == "deal":
        world.deals.append(replace(world.deals[0], trade_date=later))
    else:
        world.bands.append(replace(world.bands[0], knowable_date=later))
    with pytest.raises(PitError):
        _build(world)


def test_the_same_lake_builds_the_same_digest_whatever_the_clock(built: CommonsScreens) -> None:
    again = _build(clock=clock(SESSION, 23))
    assert again.screens_digest == built.screens_digest and again.built_at != built.built_at
    built.verify()
    with pytest.raises(ValueError, match="reproduce"):
        built.model_copy(update={"eligible": 1}).verify()


def test_a_missing_source_is_a_gap_not_an_empty_answer() -> None:
    screens = _build(missing=frozenset({"deals", "price_bands"}))
    sources = {g.source for g in screens.gaps}
    assert {"deals:deals", "price_bands:price_bands"} <= sources
    assert BANDED not in screens.exclusions.isins  # the band rule could not be applied
    assert not any(e.categories == ("bulk_deal",) for e in screens.s5)


def test_the_rule_hash_covers_the_thresholds_and_the_keywords() -> None:
    source = screens_rule_source()
    assert b'"0.85"' in source and b'"0.15"' in source and b'"0.8"' in source
    assert KEYWORDS.digest.encode() in source


def test_the_digest_scope_is_the_screens_and_the_shortlist(built: CommonsScreens) -> None:
    scope = built.digest_scope(None)
    assert {LEADER, BREAKOUT} <= scope
    assert scope == {e.isin for s in (built.s1, built.s2, built.s3, built.s4) for e in s} | {
        e.isin for e in built.s5
    }


# ── the dossier ──────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def dossiers(built: CommonsScreens) -> Any:
    world = screen_world()
    return build_dossiers(
        [LEADER, FLAGGED, LEADER],
        screens=built,
        sheets=sheets_for(world),
        source=FakeScreenSource(world),
    )


def test_every_field_has_a_stable_id_that_resolves(built: CommonsScreens, dossiers: Any) -> None:
    assert [d.isin for d in dossiers] == [LEADER, FLAGGED]
    leader = dossiers[0]
    assert set(leader.fields) == set(DOSSIER_FIELDS)
    for name in DOSSIER_FIELDS:
        assert (
            resolve_field(f"{LEADER}.{name}", dossiers=dossiers, screens=built)
            == leader.fields[name]
        )
    assert resolve_field(f"{LEADER}.ret_13w", dossiers=dossiers) == Decimal("0.05")
    assert resolve_field(f"{LEADER}.return_13w", dossiers=dossiers) == Decimal("0.05")
    features = next(f for f in built.features if f.isin == LEADER)
    assert resolve_field(f"{LEADER}.ret_12_1", dossiers=dossiers) == features.ret_12_1
    assert resolve_field("market.state", dossiers=dossiers, screens=built) is RegimeState.RISK_ON
    assert resolve_field(f"{LEADER}.screens", dossiers=dossiers) == "S1,S5"


def test_listed_facts_have_their_own_ids(dossiers: Any) -> None:
    leader = dossiers[0]
    order = resolve_field(f"{LEADER}.ann_9002", dossiers=dossiers)
    assert getattr(order, "categories", None) == ("order_win",)
    assert [i.field_id for i in leader.corporate_actions] == [f"{LEADER}.ca_1"]  # only the one due
    assert leader.corporate_actions[0].due_date == SESSION + timedelta(days=7)
    assert [i.field_id for i in leader.deals] == [f"{LEADER}.deal_1"]
    flagged = dossiers[1]
    assert (
        flagged.fields["excluded"] is True
        and flagged.fields["exclusion_reasons"] == "INTEGRITY_EVENT"
    )
    (event,) = flagged.announcements
    assert event.categories == ("auditor_resignation",)


@pytest.mark.parametrize(
    "field_id",
    [
        f"{LEADER}.no_such_field",
        f"{BREAKOUT}.ret_13w",  # a real name, but no dossier was built for it
        f"{LEADER}.ann_123456",
        "market.no_such_field",
        "INE.ret_13w",
        f"{LEADER}.RET_13W",
        "ret_13w",
    ],
)
def test_an_unknown_field_id_is_refused(
    built: CommonsScreens, dossiers: Any, field_id: str
) -> None:
    with pytest.raises(UnknownFieldError):
        resolve_field(field_id, dossiers=dossiers, screens=built)


def test_a_dossier_outside_the_universe_is_refused(built: CommonsScreens) -> None:
    world = screen_world()
    with pytest.raises(ValueError, match="universe"):
        build_dossiers(
            [_isin(999)], screens=built, sheets=sheets_for(world), source=FakeScreenSource(world)
        )


@pytest.mark.parametrize(
    ("spot", "oi", "expected"),
    [
        (Decimal(1), 10, OiBuildup.LONG_BUILDUP),
        (Decimal(-1), 10, OiBuildup.SHORT_BUILDUP),
        (Decimal(1), -10, OiBuildup.SHORT_COVERING),
        (Decimal(-1), -10, OiBuildup.LONG_UNWINDING),
        (Decimal(0), 10, OiBuildup.NEUTRAL),
        (Decimal(1), 0, OiBuildup.NEUTRAL),
    ],
)
def test_oi_buildup_is_the_four_way_classification(
    spot: Decimal, oi: int, expected: OiBuildup
) -> None:
    assert oi_buildup(spot, oi) is expected


def test_the_fo_block_reads_the_aggregates(dossiers: Any) -> None:
    fields = dossiers[0].fields
    assert fields["fo_listed"] is True and fields["fo_session"] == SESSION
    assert fields["fo_oi_buildup"] == "LONG_BUILDUP"  # spot 100 -> 101, OI +500
    assert fields["fo_pcr_oi"] == Decimal("0.9") and fields["fo_rollover_pct"] == Decimal("0.2")
    assert dossiers[1].fields["fo_listed"] is False
