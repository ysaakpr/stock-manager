"""X2: the campaign's backtest windows — configured, not hard-coded, and checked against the lake.

The windows a campaign sweeps live in ``backtest/windows.yaml``. This module loads them and checks
the one thing a window's dates cannot state for themselves: that its **first decision date has the
full lookback every arm reads**, on history that can be joined by ISIN.

**Joinable history starts 2011-06-22.** L1 ``prices_raw`` begins there; the 2006-2011 bhavcopies
carry no ISIN and sit in ``prices_raw_quarantine``, which no backtest reads (ISIN is the only join
key, CLAUDE.md). A window that opened before its arms' lookback had filled would not fail — every
feature query drops a name with too few prints, the median-turnover screen takes the median of what
exists — it would quietly trade a thinner, differently-screened universe for its first year and
report it as the same strategy. So the full-span window's start is *derived*: the first session on
which the longest lookback any arm needs is satisfied (:func:`first_full_lookback_session`).

**Every lookback, from the constants the runners use** (:data:`LOOKBACKS`). Session-counted
windows need that many NSE sessions on or after the joinable start (a name cannot have more prints
than there were sessions); calendar-day windows need the lookback's first day on or after it.

The walk-forward split of the full span is two halves of equal *session* count — the replayed
sessions, not calendar days, are what each half's result is struck over — selection first.

What this module never does: read a wall clock, or read the quarantine.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from backtest.run import (
    _DEFAULT_LIQUIDITY_LOOKBACK_DAYS,
    _LOOKBACK_DAYS,
    _MONTH_DAYS,
    _REGIME_MA_DAYS,
    _SWING_HIGH_WINDOW,
    _SWING_MIN_HISTORY,
    _SWING_MOM_LONG,
    _VOL_MONTHS,
    _L1Reader,
)

__all__ = [
    "LOOKBACKS",
    "WINDOWS_PATH",
    "CampaignWindows",
    "Lookback",
    "LookbackUnit",
    "Window",
    "WindowError",
    "first_full_lookback_session",
    "load_windows",
    "split_in_half",
    "verify_windows",
]

#: The checked-in window configuration.
WINDOWS_PATH = Path(__file__).with_name("windows.yaml")


class WindowError(ValueError):
    """A configured window is malformed, or its first decision lacks the full lookback."""


class LookbackUnit(StrEnum):
    SESSIONS = "sessions"
    DAYS = "calendar days"


@dataclass(frozen=True, slots=True)
class Lookback:
    """One trailing window an arm reads on its first decision, and who reads it."""

    what: str
    arms: str
    unit: LookbackUnit
    length: int

    def first_session(self, sessions: Sequence[date], joinable_from: date) -> date | None:
        """The first session in ``sessions`` on which this lookback lies wholly in joinable data."""
        usable = [s for s in sessions if s >= joinable_from]
        if self.unit is LookbackUnit.SESSIONS:
            return usable[self.length - 1] if len(usable) >= self.length else None
        need = timedelta(days=self.length)
        return next((s for s in usable if s - need >= joinable_from), None)


#: Every trailing window a campaign arm reads, from the constants ``backtest.run`` uses. The swing
#: feature query's own frames (52-week high, 12-1, 63-session vol, 252-row turnover median) all sit
#: inside the 260-print minimum history it filters on; they are listed so the binding one is shown
#: against the rest rather than asserted.
LOOKBACKS: tuple[Lookback, ...] = (
    Lookback(
        "minimum prints before a name is scoreable (n >= 260)",
        "every swing arm",
        LookbackUnit.SESSIONS,
        _SWING_MIN_HISTORY,
    ),
    Lookback(
        "12-1 momentum leg: lag(252)", "every swing arm", LookbackUnit.SESSIONS, _SWING_MOM_LONG + 1
    ),
    Lookback("52-week high", "every swing arm", LookbackUnit.SESSIONS, _SWING_HIGH_WINDOW),
    Lookback(
        "regime index 200-session mean",
        "regime-gated arms, momentum v2",
        LookbackUnit.SESSIONS,
        _REGIME_MA_DAYS,
    ),
    Lookback(
        "median-turnover liquidity screen (trailing 365 days)",
        "every arm",
        LookbackUnit.DAYS,
        _DEFAULT_LIQUIDITY_LOOKBACK_DAYS,
    ),
    Lookback(
        "12-month / 12-1 momentum reference session",
        "naive momentum, momentum v2",
        LookbackUnit.DAYS,
        _LOOKBACK_DAYS,
    ),
    Lookback(
        "12 monthly returns for volatility sizing",
        "momentum v2",
        LookbackUnit.DAYS,
        _VOL_MONTHS * _MONTH_DAYS,
    ),
)


def first_full_lookback_session(
    sessions: Sequence[date], joinable_from: date, lookbacks: Sequence[Lookback] = LOOKBACKS
) -> tuple[date, Lookback]:
    """The first session on which every lookback is satisfied, and the lookback that binds it.

    Ties go to the first-listed lookback. Raises ``WindowError`` if the calendar is too short for
    some lookback ever to fill.
    """
    best: tuple[date, Lookback] | None = None
    for lookback in lookbacks:
        first = lookback.first_session(sessions, joinable_from)
        if first is None:
            raise WindowError(f"the calendar never fills the lookback for {lookback.what}")
        if best is None or first > best[0]:
            best = (first, lookback)
    if best is None:
        raise WindowError("no lookbacks to satisfy")
    return best


@dataclass(frozen=True, slots=True)
class Window:
    """A named backtest window: the first decision falls on or after ``start``; ``end`` is last."""

    name: str
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise WindowError(f"window {self.name}: end {self.end} is not after start {self.start}")

    @property
    def spec(self) -> str:
        return f"{self.start.isoformat()}:{self.end.isoformat()}"


@dataclass(frozen=True, slots=True)
class CampaignWindows:
    """The configured windows: the sweep windows and the walk-forward split of ``full``."""

    joinable_from: date
    sweeps: tuple[Window, ...]
    selection: Window
    verification: Window

    def named(self, name: str) -> Window:
        for window in (*self.sweeps, self.selection, self.verification):
            if window.name == name:
                return window
        raise WindowError(f"no window named {name!r}")

    @property
    def full(self) -> Window:
        return self.named("full")


def _date(raw: object, where: str) -> date:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        return date.fromisoformat(raw)
    raise WindowError(f"{where}: not a date: {raw!r}")


def _window(name: str, raw: Any) -> Window:
    if not isinstance(raw, dict):
        raise WindowError(f"window {name}: expected a mapping with start/end")
    return Window(
        name, _date(raw.get("start"), f"{name}.start"), _date(raw.get("end"), f"{name}.end")
    )


def load_windows(path: Path = WINDOWS_PATH) -> CampaignWindows:
    """Load the window configuration; ``WindowError`` on any malformed entry."""
    doc: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise WindowError(f"{path}: not a version-1 window configuration")
    sweeps = tuple(_window(name, raw) for name, raw in doc["windows"].items())
    names = [w.name for w in sweeps]
    if "full" not in names:
        raise WindowError(f"{path}: no 'full' window")
    walk = doc["walk_forward"]
    selection = _window("wf-selection", walk["selection"])
    verification = _window("wf-verification", walk["verification"])
    if selection.end >= verification.start:
        raise WindowError("walk-forward selection must close before verification opens")
    return CampaignWindows(
        joinable_from=_date(doc["joinable_l1_from"], "joinable_l1_from"),
        sweeps=sweeps,
        selection=selection,
        verification=verification,
    )


def split_in_half(window: Window, sessions: Sequence[date]) -> tuple[Window, Window]:
    """``window``'s sessions cut into two halves of equal count (selection, verification).

    An odd count leaves the extra session in the verification half.
    """
    inside = [s for s in sessions if window.start <= s <= window.end]
    if len(inside) < 2:
        raise WindowError(f"window {window.name} holds fewer than two sessions")
    half = len(inside) // 2
    return (
        Window("wf-selection", inside[0], inside[half - 1]),
        Window("wf-verification", inside[half], inside[-1]),
    )


def verify_windows(windows: CampaignWindows, sessions: Sequence[date]) -> list[str]:
    """Check the configuration against the lake calendar; return what was checked, or raise.

    * ``full`` starts on exactly the first full-lookback session (not merely after it — the task
      is the longest span, so a later start is a misconfiguration too);
    * every other window's first session is on or after it;
    * the walk-forward windows are exactly :func:`split_in_half` of ``full``.
    """
    first, binding = first_full_lookback_session(sessions, windows.joinable_from)
    lines = [
        f"joinable L1 from {windows.joinable_from.isoformat()}; first full-lookback session "
        f"{first.isoformat()}, bound by: {binding.what} ({binding.length} {binding.unit.value}; "
        f"{binding.arms})",
    ]
    for lookback in LOOKBACKS:
        at = lookback.first_session(sessions, windows.joinable_from)
        lines.append(
            f"  {lookback.what}: {lookback.length} {lookback.unit.value} -> "
            f"{at.isoformat() if at else 'never'}"
        )
    full = windows.full
    opened = next((s for s in sessions if s >= full.start), None)
    if opened != first:
        raise WindowError(
            f"full window's first session is {opened}, but the first full-lookback session is "
            f"{first.isoformat()} ({binding.what}); set full.start to it"
        )
    for window in (*windows.sweeps, windows.selection, windows.verification):
        opened = next((s for s in sessions if s >= window.start), None)
        if opened is None or opened < first:
            raise WindowError(
                f"window {window.name} opens on {opened}, before the first full-lookback session "
                f"{first.isoformat()} ({binding.what})"
            )
        lines.append(f"{window.name}: {window.start.isoformat()} -> {window.end.isoformat()}  ok")
    selection, verification = split_in_half(full, sessions)
    configured = (
        (windows.selection.start, windows.selection.end),
        (windows.verification.start, windows.verification.end),
    )
    derived = ((selection.start, selection.end), (verification.start, verification.end))
    if configured != derived:
        raise WindowError(
            f"walk-forward is not the equal-session split of full: configured {configured}, "
            f"derived {derived}"
        )
    count = len([s for s in sessions if full.start <= s <= full.end])
    lines.append(
        f"walk-forward: {count} sessions in full -> selection {selection.start}..{selection.end}, "
        f"verification {verification.start}..{verification.end}  ok"
    )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m backtest.windows [--data-root DIR]``: verify the configuration on the lake."""
    parser = argparse.ArgumentParser(
        prog="python -m backtest.windows",
        description="Verify the campaign windows' lookback against the lake calendar.",
    )
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=WINDOWS_PATH)
    args = parser.parse_args(argv)
    windows = load_windows(args.config)
    reader = _L1Reader(data_root=args.data_root)
    try:
        sessions = reader.all_sessions()
    finally:
        reader.close()
    try:
        lines = verify_windows(windows, sessions)
    except WindowError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
