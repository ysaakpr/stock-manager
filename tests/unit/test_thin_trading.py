"""The thinly-traded label (backlog M13.8, owner ruling 2026-11-01): how often an ISIN trades.

Every test here is built to fail if its rule is inverted: a sparse name must be labelled and a dense
one must not; a capture hole must neither extend a gap nor count as a no-trade session; a session
before a venue's first bar is not a no-trade session; a bar on either venue makes a session traded;
a reissue's predecessor bars count for its successor; and a gap a sibling ISIN traded through is an
identity seam, reported and never labelled.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.clock import IST
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.quality.thin_trading import (
    MIN_OBSERVED_SESSIONS,
    THIN_GAP_SESSIONS,
    THIN_RATIO_BELOW,
    WINDOW_SESSIONS,
    IsinActivity,
    ThinTradingError,
    ThinTradingLabel,
    VenueActivity,
    label_isin,
    resolve_successors,
    trailing_sessions,
)
from dataplatform.scheduler.registry import (
    EOD_PIPELINE,
    M17_FUND_MANAGERS,
    PAPER_SESSION,
    SHARE_BASIS_GUARD,
    THIN_TRADING_LABEL,
    Job,
    default_registry,
)

AS_OF = date(2026, 11, 3)
ISIN = "INE000A01011"
SESSIONS = trailing_sessions(trading_calendar(), AS_OF, WINDOW_SESSIONS)
WINDOW = SESSIONS[1:]
ALL = frozenset(WINDOW)
LONG_AGO = date(2010, 1, 4)


def venue(
    traded: set[date] | frozenset[date],
    *,
    exchange: str = "NSE",
    first_bar: date = LONG_AGO,
    series: str = "EQ",
    holes: frozenset[date] = frozenset(),
) -> VenueActivity:
    return VenueActivity(
        exchange=exchange,
        traded=frozenset(traded),
        first_bar=first_bar,
        last_series=series,
        holes=holes,
    )


def label(
    *venues: VenueActivity,
    captured: dict[str, frozenset[date]] | None = None,
    sibling: frozenset[date] = frozenset(),
) -> ThinTradingLabel:
    out = label_isin(
        IsinActivity(isin=ISIN, venues=venues, listing_status="ACTIVE", sibling_traded=sibling),
        as_of=AS_OF,
        sessions=SESSIONS,
        captured=captured or {"NSE": ALL, "BSE": ALL},
    )
    assert out is not None
    return out


def test_the_window_is_the_trailing_sessions_ending_at_as_of() -> None:
    assert len(WINDOW) == WINDOW_SESSIONS
    assert WINDOW[-1] == AS_OF
    assert SESSIONS[0] < WINDOW[0]
    with pytest.raises(ThinTradingError, match="not an NSE session"):
        trailing_sessions(trading_calendar(), date(2026, 11, 1), WINDOW_SESSIONS)  # a Sunday


def test_a_name_that_trades_every_session_is_not_thin() -> None:
    lab = label(venue(ALL))
    assert (lab.sessions_observed, lab.sessions_traded, lab.traded_ratio) == (250, 250, Decimal(1))
    assert lab.longest_gap_sessions == 0
    assert not lab.thin and lab.reasons == ()


def test_a_sparse_name_is_thin_on_its_ratio() -> None:
    """Traded one session in three: the ratio is below 0.8 and it is labelled; no gap reaches 20."""
    lab = label(venue({d for i, d in enumerate(WINDOW) if i % 3 == 0}))
    assert lab.traded_ratio < THIN_RATIO_BELOW
    assert lab.longest_gap_sessions == 2
    assert lab.thin and lab.reasons == ("ratio",)


def test_the_ratio_threshold_is_strict_below() -> None:
    """Exactly 0.8 is not thin; one more missed session is. Fails if the comparison is inverted."""
    at = {d for i, d in enumerate(WINDOW) if i % 5 != 0}  # 200 of 250 = 0.8000, gaps of one
    assert label(venue(at)).traded_ratio == Decimal("0.8000")
    assert not label(venue(at)).thin
    below = at - {max(at)}
    assert label(venue(below)).thin


def test_one_long_gap_is_thin_even_when_the_ratio_is_high() -> None:
    """A dense name with one 20-session gap is labelled on the gap; 19 is not."""
    for length, thin in ((THIN_GAP_SESSIONS, True), (THIN_GAP_SESSIONS - 1, False)):
        gap = set(WINDOW[100 : 100 + length])
        lab = label(venue(ALL - gap))
        assert lab.longest_gap_sessions == length
        assert lab.longest_gap_from == WINDOW[100]
        assert lab.longest_gap_to == WINDOW[100 + length - 1]
        # Calendar days run from the bar before the gap to the bar after it.
        assert lab.longest_gap_days == (WINDOW[100 + length] - WINDOW[99]).days
        assert lab.traded_ratio >= THIN_RATIO_BELOW
        assert lab.thin is thin
        assert lab.reasons == (("gap",) if thin else ())


def test_a_capture_hole_is_not_a_no_trade_gap() -> None:
    """Thirty sessions our capture lacks, inside a name that traded every session it was seen: no
    gap, no thin label, and the holes are counted. Fails if a hole is read as a no-trade day."""
    holes = frozenset(WINDOW[50:80])
    lab = label(venue(ALL - holes), captured={"NSE": ALL - holes, "BSE": ALL})
    assert lab.capture_holes == 30
    assert lab.sessions_observed == 220
    assert lab.traded_ratio == Decimal(1)
    assert lab.longest_gap_sessions == 0
    assert not lab.thin


def test_an_isins_own_hole_is_not_a_no_trade_gap_either() -> None:
    """A per-ISIN hole (an L1-refused row, a PR-bundle trade our bhavcopy lacks) behaves alike."""
    holes = frozenset(WINDOW[10:40])
    lab = label(venue(ALL - holes, holes=holes))
    assert (lab.capture_holes, lab.longest_gap_sessions, lab.thin) == (30, 0, False)


def test_a_hole_breaks_a_gap_rather_than_extending_it() -> None:
    """15 no-trade sessions, one hole, 15 more: two gaps of 15, not one of 31, so not thin."""
    gap = set(WINDOW[100:131])
    hole = WINDOW[115]
    lab = label(venue(ALL - gap), captured={"NSE": ALL - {hole}, "BSE": ALL})
    assert lab.capture_holes == 1
    assert lab.longest_gap_sessions == 15
    assert not lab.thin


def test_sessions_before_the_first_bar_are_not_no_trade_sessions() -> None:
    """A name listed 60 sessions ago that has traded every session since is not thin."""
    listed = WINDOW[-60]
    lab = label(venue(set(WINDOW[-60:]), first_bar=listed))
    assert lab.window_start == listed
    assert (lab.sessions_observed, lab.traded_ratio, lab.thin) == (60, Decimal(1), False)


def test_too_few_sessions_is_insufficient_history_not_thin() -> None:
    listed = WINDOW[-(MIN_OBSERVED_SESSIONS - 1)]
    lab = label(venue({listed}, first_bar=listed))
    assert lab.insufficient_history
    assert not lab.thin


def test_an_open_gap_runs_to_as_of() -> None:
    """A name that stopped trading 30 sessions ago: the gap is current and labelled."""
    lab = label(venue(set(WINDOW[:-30])))
    assert lab.current_gap_sessions == 30
    assert lab.longest_gap_to == AS_OF
    assert lab.longest_gap_days == (AS_OF - WINDOW[-31]).days
    assert lab.last_traded == WINDOW[-31]
    assert lab.thin and "gap" in lab.reasons


def test_a_bar_on_either_venue_makes_the_session_traded() -> None:
    """NSE dark for 100 sessions while BSE traded daily: the ISIN traded, it is not thin, and the
    venue ratios still show NSE alone. Fails if the venues are read separately."""
    nse_gap = set(WINDOW[:100])
    lab = label(venue(ALL - nse_gap), venue(ALL, exchange="BSE", series="B"))
    assert lab.traded_ratio == Decimal(1)
    assert lab.nse_traded_ratio == Decimal("0.6000")
    assert lab.bse_traded_ratio == Decimal(1)
    assert not lab.thin


def test_a_hole_on_one_venue_is_a_hole_when_neither_traded() -> None:
    sparse = {d for i, d in enumerate(WINDOW) if i % 2 == 0}
    holes = frozenset(d for d in WINDOW if d not in sparse)
    lab = label(
        venue(sparse),
        venue(sparse, exchange="BSE", series="B"),
        captured={"NSE": ALL, "BSE": ALL - holes},
    )
    assert lab.capture_holes == len(holes)
    assert lab.traded_ratio == Decimal(1)
    assert not lab.thin


def test_trade_to_trade_is_the_latest_series_on_any_venue() -> None:
    assert label(venue(ALL, series="BE")).trade_to_trade
    assert label(venue(ALL, exchange="BSE", series="XT")).trade_to_trade
    assert not label(venue(ALL, series="EQ"), venue(ALL, exchange="BSE", series="A")).trade_to_trade
    # The label never thins a dense name for being trade-to-trade: that is a status, not a gap.
    assert not label(venue(ALL, series="BZ")).thin


def test_a_gap_a_sibling_isin_traded_through_is_an_identity_seam() -> None:
    """A reissue the lineage table lacks: the old ISIN stops, a sibling carries on. Reported as a
    seam (ours) and never labelled thin. Fails if the seam is ignored or always set."""
    stopped = venue(set(WINDOW[:-60]))
    seam = label(stopped, sibling=frozenset(WINDOW[-60:]))
    assert seam.identity_seam and not seam.thin and seam.reasons == ()
    # A sparse successor still marks the seam: one trade inside the gap, none alongside.
    assert label(stopped, sibling=frozenset({WINDOW[-5]})).identity_seam
    real = label(stopped)
    assert not real.identity_seam and real.thin


def test_a_sibling_that_trades_alongside_is_no_seam() -> None:
    """A second share class trading on the same sessions (a DVR) is not a reissue: the gap stays
    a real no-trade gap. Fails if any same-issuer trade in the gap is taken as a seam."""
    stopped = venue(set(WINDOW[:-60]))
    alongside = label(stopped, sibling=frozenset(WINDOW))
    assert not alongside.identity_seam and alongside.thin


def test_no_bar_on_or_before_as_of_is_no_label() -> None:
    later = IsinActivity(isin=ISIN, venues=(venue(set(), first_bar=AS_OF + timedelta(days=1)),))
    assert label_isin(later, as_of=AS_OF, sessions=SESSIONS, captured={"NSE": ALL}) is None


def test_lineage_resolves_to_the_end_of_the_chain() -> None:
    edges = [("INE001A01011", "INE001A01029"), ("INE001A01029", "INE001A01037")]
    assert resolve_successors(edges) == {
        "INE001A01011": "INE001A01037",
        "INE001A01029": "INE001A01037",
    }


def test_a_fan_out_or_a_cycle_is_never_stitched_wrongly() -> None:
    fan = [("INE002A01011", "INE002A01029"), ("INE002A01011", "INE00XA01017")]
    assert resolve_successors(fan) == {}
    cycle = [("INE003A01011", "INE003A01029"), ("INE003A01029", "INE003A01011")]
    resolved = resolve_successors(cycle)
    assert set(resolved) == {"INE003A01011", "INE003A01029"}  # terminates


def test_label_json_carries_no_float() -> None:
    payload = label(venue(ALL)).to_json()
    assert payload["traded_ratio"] == "1.0000"
    assert not any(isinstance(v, float) for v in payload.values())


# ── when it runs, and who may read it ─────────────────────────────────────────────────────────


def _next_fire(job: Job, after: datetime) -> datetime:
    fire: datetime = job.trigger(IST).get_next_fire_time(None, after)
    return fire


@pytest.mark.parametrize("day", [date(2026, 11, 2) + timedelta(days=i) for i in range(5)])
def test_the_job_runs_after_the_evening_chain_and_ends_before_m17(day: date) -> None:
    midnight = datetime(day.year, day.month, day.day, tzinfo=IST)
    fire = _next_fire(THIN_TRADING_LABEL, midnight)
    assert fire.date() == day and (fire.hour, fire.minute) == (20, 15)
    eod = _next_fire(EOD_PIPELINE, midnight)
    guard = _next_fire(SHARE_BASIS_GUARD, midnight)
    assert eod + EOD_PIPELINE.timeout <= fire
    assert guard + SHARE_BASIS_GUARD.timeout <= fire
    latest_end = fire + THIN_TRADING_LABEL.latest_start + THIN_TRADING_LABEL.timeout
    assert latest_end < _next_fire(PAPER_SESSION, midnight)
    assert latest_end < _next_fire(M17_FUND_MANAGERS, midnight)
    assert "thin_trading_label" in default_registry()
    assert THIN_TRADING_LABEL.covers == () and THIN_TRADING_LABEL.sync_sources == ()


#: Packages under `analyst/` that may read the label. `analyst/owner_portfolio` (M19, owner-approved
#: 2026-11-06) is the review of the owner's own self-managed Kite account: no M17 manager, backtest
#: or paper book reads it — `tests/unit/test_owner_portfolio_invariants.py` fails if
#: `analyst.fundmanager`, `analyst.commons` or `backtest` ever import it — so the label reaching it
#: changes no mandate hash and no simulated book.
_LABEL_READERS: Final = ("analyst/owner_portfolio/",)


def test_nothing_m17_or_the_backtest_reads_imports_the_label() -> None:
    """The label must not reach the M17 managers (their mandate hash would change; that needs a
    pre-registration amendment) or the backtest/paper books. Fails the day anything under
    `analyst/` or `backtest/` names it, outside the owner's portfolio review (`_LABEL_READERS`)."""
    root = Path(__file__).resolve().parents[2]
    offenders = [
        rel
        for package in ("analyst", "backtest")
        for path in (root / package).rglob("*.py")
        if "thin_trading" in path.read_text(encoding="utf-8")
        and not (rel := path.relative_to(root).as_posix()).startswith(_LABEL_READERS)
    ]
    assert offenders == []
