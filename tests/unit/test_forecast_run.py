"""The maturity gate: a fitted model may only ever have seen targets that had already happened.

A fitted policy has one failure mode that no amount of `ctx.pit.admit` catches, because it happens
before a record is ever built: the *coefficients* are told the future. The features in this adapter
cannot reach forward — every window frame is `ROWS BETWEEN n PRECEDING AND CURRENT ROW` — but the
training targets are forward returns by construction, so the entire discipline is the rule that a
pair enters the accumulator only on the session its target window closes.

That rule is what these tests pin, with a stub cursor rather than a lake, so the arithmetic of the
gate is asserted directly and not inferred from a return number. The boundary case is the
interesting one: a target realised *on* the session is knowable on it and must be included, while
one realised the very next session must not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal

import pytest

from backtest.forecast import MIN_OBSERVATIONS
from backtest.forecast_run import _L1ForecastData, _Row
from backtest.policies.momentum_v2 import RegimeReading
from backtest.run import BacktestError

START = date(2020, 1, 1)


def _sessions(n: int) -> list[date]:
    return [START + timedelta(days=i) for i in range(n)]


def _isins(n: int) -> list[str]:
    return [f"INE{i:03d}A01010" for i in range(1, n + 1)]


class _StubCursor:
    """Serves fixed rows per session and enforces the same forward-only contract as the real one."""

    def __init__(self, rows_by_session: Mapping[date, Sequence[_Row]]) -> None:
        self._rows = {day: tuple(rows) for day, rows in rows_by_session.items()}
        self._last: date | None = None

    def take(self, session: date) -> list[_Row]:
        if self._last is not None and session < self._last:
            raise BacktestError("the feature cursor cannot go back")
        self._last = session
        return list(self._rows.get(session, ()))

    def close(self) -> None: ...


class _StubRegime:
    """A flat market: the index sits exactly on its own average, so market_state is 0."""

    def reading(self, as_of: date) -> RegimeReading:
        return RegimeReading(
            index_level=Decimal("1000"),
            moving_average=Decimal("1000"),
            knowable_date=as_of,
        )


class _StubReader:
    """Only `closes_on` is reached in these tests, and only for the policy's marks."""

    def closes_on(self, session: date) -> Mapping[str, Decimal]:
        return {}


def _row(session: date, isin: str, *, target_date: date | None, target: float | None) -> _Row:
    """A fully-featured row whose values vary with the ISIN so the cross-section has ranks."""
    seed = float(int(isin[3:6]))
    return _Row(
        session=session,
        isin=isin,
        price=Decimal("100"),
        mom_12_1=seed,
        mom_1=-seed,
        mom_6=seed / 2.0,
        high_prox=0.5 + seed / 1000.0,
        vol_63=0.2 + seed / 1000.0,
        deliv_ratio=1.0 + seed / 100.0,
        turnover_ratio=1.0 + seed / 50.0,
        target_date=target_date,
        target_return=target,
    )


def _data(rows_by_session: Mapping[date, Sequence[_Row]], *, horizon: int) -> _L1ForecastData:
    return _L1ForecastData(
        _StubReader(),  # type: ignore[arg-type]
        _StubCursor(rows_by_session),  # type: ignore[arg-type]
        _StubRegime(),  # type: ignore[arg-type]
        horizon=horizon,
        universe_filter=None,
    )


def _observations(data: _L1ForecastData) -> int:
    """Pairs the accumulator has been given — the number the whole module exists to bound."""
    return data._accumulator.observations


# ── the gate ─────────────────────────────────────────────────────────────────────────────────────


def test_a_pair_enters_the_fit_only_when_its_target_has_happened() -> None:
    """Walking forward, the accumulator holds exactly the pairs whose target date has passed.

    Ten sessions, one name, each row's target realised three sessions later. After session `s` the
    fit must hold one pair for every target date at or before `s` — no more, which would be a leak,
    and no fewer, which would be data thrown away.
    """
    days = _sessions(10)
    isin = _isins(2)  # two names so the cross-section has a rank
    rows = {
        day: [
            _row(
                day,
                name,
                target_date=days[index + 3] if index + 3 < len(days) else None,
                target=0.05,
            )
            for name in isin
        ]
        for index, day in enumerate(days)
    }
    data = _data(rows, horizon=3)

    seen: list[int] = []
    for day in days:
        data.signal(day)
        seen.append(_observations(data))

    # Session 0,1,2: nothing has matured. Session 3: the pair from session 0 (two names). Then two
    # more per session, until the rows near the end carry no target at all.
    assert seen == [0, 0, 0, 2, 4, 6, 8, 10, 12, 14]


def test_a_target_realised_on_the_session_is_included_and_the_next_one_is_not() -> None:
    """The boundary: `target_date <= session` is knowable; `target_date == session + 1` is not.

    This is the off-by-one that would leak a whole horizon of future returns into every fit while
    looking, in every summary statistic, exactly like a working model.
    """
    days = _sessions(3)
    name = _isins(1)[0]
    rows = {
        days[0]: [_row(days[0], name, target_date=days[1], target=0.10)],
        days[1]: [_row(days[1], name, target_date=days[2], target=0.10)],
        days[2]: [],
    }
    data = _data(rows, horizon=1)

    data.signal(days[0])
    assert _observations(data) == 0, "a target realised tomorrow entered today's fit"

    data.signal(days[1])
    assert _observations(data) == 1, "a target realised on this session was withheld"


def test_a_row_whose_features_were_never_scored_contributes_nothing() -> None:
    """A pair can only be added if its own session's cross-section was cached, so it cannot guess.

    The features of a training pair are the *ranked* ones from its own session. If that session was
    never walked — the replay started later — there is nothing to rank against and the pair is
    dropped rather than reconstructed from a different universe.
    """
    days = _sessions(4)
    name = _isins(1)[0]
    rows = {
        # A row dated before the walk starts, whose target lands inside it.
        days[2]: [_row(days[2], name, target_date=days[3], target=0.10)],
        days[3]: [],
    }
    data = _data(rows, horizon=1)

    data.signal(days[2])
    data.signal(days[3])

    assert _observations(data) == 1  # only the pair whose own session was walked


def test_no_model_means_no_candidates_rather_than_unscored_ones() -> None:
    """Before the observation floor the session is scored empty, and the stat says so.

    The policy treats an empty cross-section as "hold what you have"
    (`tests/unit/test_forecast_daily.py`), so this is the state that makes that branch real: the
    adapter emits no records at all until a model exists.
    """
    days = _sessions(4)
    name = _isins(1)[0]
    rows = {
        day: [_row(day, name, target_date=days[index + 1] if index + 1 < 4 else None, target=0.1)]
        for index, day in enumerate(days)
    }
    data = _data(rows, horizon=1)

    for day in days:
        assert data.signal(day).records == ()

    assert _observations(data) < MIN_OBSERVATIONS
    assert data.stats.sessions_without_model == len(days)
    assert data.stats.sessions_with_model == 0
    assert data.stats.final_model is None


# ── the cursor's forward-only contract ───────────────────────────────────────────────────────────


def test_the_walk_cannot_go_backwards() -> None:
    """A backwards request raises instead of quietly returning nothing.

    The cursor is streamed, so a session already consumed cannot be re-read. Returning an empty
    cross-section for it would look like "no candidates today" — a wrong answer that trades.
    """
    days = _sessions(3)
    name = _isins(1)[0]
    data = _data({day: [_row(day, name, target_date=None, target=None)] for day in days}, horizon=1)

    data.signal(days[1])
    with pytest.raises(BacktestError, match="cannot go back"):
        data.signal(days[0])


def test_advancing_twice_on_one_session_is_idempotent() -> None:
    """`marks` and `signal` are both called per session; the second must not re-consume the cursor.

    The policy reads marks before candidates, so whichever call arrives first advances the walk and
    the other must find it already done — otherwise every session would mature its pairs twice.
    """
    days = _sessions(3)
    names = _isins(2)
    rows = {
        days[0]: [_row(days[0], n, target_date=days[1], target=0.05) for n in names],
        days[1]: [_row(days[1], n, target_date=days[2], target=0.05) for n in names],
        days[2]: [],
    }
    data = _data(rows, horizon=1)

    data.marks(days[0])
    data.signal(days[0])
    data.signal(days[0])
    assert _observations(data) == 0

    data.signal(days[1])
    data.signal(days[1])
    assert _observations(data) == 2, "the session's pairs were matured more than once"
