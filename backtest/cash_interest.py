"""X2: idle cash earns interest — RBI repo rate less 50 bp, accrued daily, credited monthly.

Until this module a backtest's uninvested cash earned nothing, so every strategy that sits partly
in cash — a regime filter out of the market, a rebalance that leaves a residue, the settlement gap
after a sale — was scored against an account that does not exist: a real investor's idle money
sits in a sweep deposit or a liquid fund earning roughly the policy rate. This is a *measurement*
fix, not a strategy lever, so it has exactly one number beyond the facts in ``repo_rates.yaml``:
the fixed :data:`HAIRCUT`. Nothing here is tuned, swept or configured per run.

**The convention (stated, because each part is a choice):**

- **What earns:** *settled* cash only — the broker's spendable cash at the end of each session,
  less any sale proceeds the broker released early for the next session's fills
  (``SimBroker.interest_bearing_cash``). Proceeds still in settlement earn nothing until the
  settlement-era cycle (T+2 before 2023, T+1 since) pays them out. A buy's cash stops earning on its
  fill session.
- **Day count:** Actual/365 — every calendar day (weekends and holidays included) earns
  ``balance * (repo - 0.50%) / 365`` at the rate in force *that day*, on the balance left at the end
  of the last session on or before it. A mid-month rate change therefore splits the month's accrual
  at the change date by construction.
- **Credit:** monthly. Interest accrued on the days of a calendar month is credited to cash, in one
  amount rounded to the paisa, on the first session the walk visits in a later month — spendable
  from that session, like a dividend. The FY of that session is the FY the income is taxed in
  (``backtest.tax``: income from other sources, at the investor's slab rate).
- **The last month is not credited.** Interest accrued since the last monthly credit when the run
  ends is forfeited: the walk does not know which session is its last, and crediting a partial
  month on the terminal date would need every runner to say so. The bias is conservative — at most
  one month's interest understated.

**Opt-in, explicit, recorded.** A run accrues interest only inside :func:`accrue_cash_interest`
(the same process-wide switch shape as ``backtest.book_actions.book_corporate_actions``). The run
specification — and so its digest and its persisted summary — then carries a ``cash_interest`` key
(:func:`current_cash_interest_identity`); a run outside the switch carries no key, so every run made
before this module keeps the digest it had. The campaign turns it on by default and says so at the
top of every report.

What this module never does: read a clock, take a float, guess a rate for a day
``repo_rates.yaml`` does not cover (it raises :class:`RepoRateCoverageError`), or pay interest on
money the account does not yet have.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import structlog
import yaml

__all__ = [
    "HAIRCUT",
    "REPO_RATES_PATH",
    "CashInterestAccrual",
    "CashInterestError",
    "InterestCredit",
    "Provenance",
    "RepoRateChange",
    "RepoRateCoverageError",
    "RepoRateSchedule",
    "accrue_cash_interest",
    "add_cash_interest_flag",
    "cash_interest_unless",
    "current_cash_interest",
    "current_cash_interest_identity",
    "describe_cash_interest",
    "load_repo_rate_schedule",
]

_log = structlog.get_logger(__name__)

REPO_RATES_PATH: Final[Path] = Path(__file__).with_name("repo_rates.yaml")

#: Idle cash earns the repo rate less this, per annum. Fixed: a change here is a change of
#: measurement and is reviewed as code, never passed in.
HAIRCUT: Final[Decimal] = Decimal("0.0050")

_DAYS_IN_YEAR: Final[Decimal] = Decimal("365")
_PAISA: Final[Decimal] = Decimal("0.01")
_HUNDRED: Final[Decimal] = Decimal("100")
_ZERO: Final[Decimal] = Decimal("0")


class CashInterestError(Exception):
    """The interest schedule or accrual cannot proceed as stated. Fails loud (CLAUDE.md)."""


class RepoRateCoverageError(CashInterestError, LookupError):
    """A day outside the schedule's coverage was asked for a rate. Never defaulted."""


class Provenance(StrEnum):
    """How a row was sourced (``repo_rates.yaml``'s header defines both)."""

    VERIFIED = "verified"
    RECONSTRUCTED = "reconstructed"


# ── the schedule ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RepoRateChange:
    """One change of the policy repo rate: in force from ``effective_from`` until the next row."""

    effective_from: date
    #: The repo rate as a ratio (6.25% is ``Decimal("0.0625")``).
    repo_rate: Decimal
    provenance: Provenance
    source: str


@dataclass(frozen=True, slots=True)
class RepoRateSchedule:
    """The dated repo-rate series over ``[coverage_from, coverage_through]``.

    What it assumes: ``changes`` ascend strictly and the first is in force on ``coverage_from``
    (the loader checks both). What it never does: answer for a day outside the coverage.
    """

    coverage_from: date
    coverage_through: date
    changes: tuple[RepoRateChange, ...]

    def change_for(self, day: date) -> RepoRateChange:
        """The row in force on ``day``; ``RepoRateCoverageError`` outside the coverage."""
        if not self.coverage_from <= day <= self.coverage_through:
            raise RepoRateCoverageError(
                f"no repo rate for {day.isoformat()}: repo_rates.yaml covers "
                f"{self.coverage_from.isoformat()}..{self.coverage_through.isoformat()}. Extend "
                "the schedule (and check the MPC's decisions since) rather than borrowing a rate"
            )
        in_force = self.changes[0]
        for change in self.changes:
            if change.effective_from > day:
                break
            in_force = change
        return in_force

    def repo_rate(self, day: date) -> Decimal:
        """The policy repo rate in force on ``day``, as a ratio."""
        return self.change_for(day).repo_rate

    def earning_rate(self, day: date) -> Decimal:
        """What idle settled cash earns per annum on ``day``: repo less :data:`HAIRCUT`."""
        return self.repo_rate(day) - HAIRCUT

    def to_document(self) -> dict[str, Any]:
        """The schedule as a canonical document — what its identity is hashed over."""
        return {
            "haircut": str(HAIRCUT),
            "coverage_from": self.coverage_from.isoformat(),
            "coverage_through": self.coverage_through.isoformat(),
            "changes": [[c.effective_from.isoformat(), str(c.repo_rate)] for c in self.changes],
        }

    def identity(self) -> str:
        """``repo-50bp:<sha256[:16]>`` over the rates and haircut — the run-spec value.

        Provenance and source text are left out: re-citing a row does not change a run.
        """
        canonical = json.dumps(self.to_document(), sort_keys=True, separators=(",", ":"))
        return f"repo-50bp:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def _text_decimal(value: object, where: str) -> Decimal:
    if not isinstance(value, str):
        raise CashInterestError(
            f"{where}: rates must be quoted strings in repo_rates.yaml so they parse exactly as "
            f"Decimal, got {value!r} ({type(value).__name__})"
        )
    return Decimal(value)


def _text_date(value: object, where: str) -> date:
    if not isinstance(value, str):
        raise CashInterestError(f"{where}: dates must be quoted ISO strings, got {value!r}")
    return date.fromisoformat(value)


def load_repo_rate_schedule(path: Path = REPO_RATES_PATH) -> RepoRateSchedule:
    """Parse and validate ``repo_rates.yaml``. Raises ``CashInterestError`` on any bad shape.

    Refuses: a float, an unsorted or duplicated date, a first row after ``coverage.from``, a row
    without a source, a provenance outside the two defined, and a repo rate at or below the
    haircut (the earning rate must be positive — a zero or negative rate would be a data error).
    """
    with path.open(encoding="utf-8") as handle:
        raw: Any = yaml.safe_load(handle)
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise CashInterestError(f"{path}: not a version-1 repo-rate schedule")
    coverage: Mapping[str, object] = raw["coverage"]
    start = _text_date(coverage["from"], "coverage.from")
    through = _text_date(coverage["through"], "coverage.through")
    if through < start:
        raise CashInterestError("coverage.through precedes coverage.from")
    if "confirmed_through" in coverage:
        # Carry-forward: past the last confirmed date only up to the eve of the next scheduled
        # MPC decision, because a decision is the only routine way the rate moves.
        confirmed = _text_date(coverage["confirmed_through"], "coverage.confirmed_through")
        if through < confirmed:
            raise CashInterestError("coverage.through precedes coverage.confirmed_through")
        if through > confirmed:
            if "next_mpc_decision" not in coverage:
                raise CashInterestError(
                    "coverage.through runs past confirmed_through without a next_mpc_decision"
                )
            decision = _text_date(coverage["next_mpc_decision"], "coverage.next_mpc_decision")
            if through >= decision:
                raise CashInterestError(
                    f"coverage.through {through.isoformat()} reaches the next MPC decision "
                    f"({decision.isoformat()}); add that decision's row before covering it"
                )
    changes: list[RepoRateChange] = []
    for index, row in enumerate(raw["changes"]):
        where = f"changes[{index}]"
        effective = _text_date(row["effective_from"], f"{where}.effective_from")
        pct = _text_decimal(row["repo_rate_pct"], f"{where}.repo_rate_pct")
        rate = pct / _HUNDRED
        if rate <= HAIRCUT:
            raise CashInterestError(f"{where}: repo rate {pct}% is not above the 50 bp haircut")
        source = row.get("source")
        if not isinstance(source, str) or not source.strip():
            raise CashInterestError(f"{where}: every row cites its source")
        try:
            provenance = Provenance(row["provenance"])
        except ValueError as error:
            raise CashInterestError(f"{where}: {error}") from error
        if changes and effective <= changes[-1].effective_from:
            raise CashInterestError(f"{where}: effective_from must ascend strictly")
        changes.append(RepoRateChange(effective, rate, provenance, source))
    if not changes:
        raise CashInterestError(f"{path}: no rate changes")
    if changes[0].effective_from > start:
        raise CashInterestError(
            f"the first row ({changes[0].effective_from.isoformat()}) must be in force on "
            f"coverage.from ({start.isoformat()})"
        )
    return RepoRateSchedule(start, through, tuple(changes))


# ── the accrual ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InterestCredit:
    """One monthly interest credit: ``amount`` paid into cash on session ``credited``.

    ``period_start``..``period_end`` (inclusive) are the calendar days whose accrual it pays.
    """

    credited: date
    amount: Decimal
    period_start: date
    period_end: date

    @property
    def description(self) -> str:
        """The ledger row text — ``INTEREST`` first, which the tax-ledger adapter keys on."""
        return f"INTEREST {self.period_start.isoformat()}..{self.period_end.isoformat()}"


def _month(day: date) -> tuple[int, int]:
    return day.year, day.month


class CashInterestAccrual:
    """Accrues interest day by day on a session-end balance and releases it month by month.

    Drive it once per session, in order: :meth:`credit_due` at the top of the session (it accrues
    every calendar day since the last session on that session's closing balance, then returns the
    credit for any whole month now behind the walk), and :meth:`close_session` after the fills with
    the balance that earns from tonight.

    What it assumes: sessions arrive strictly forward. What it never does: accrue on a day before
    the first :meth:`close_session`, or credit a month the walk has not left.
    """

    def __init__(self, schedule: RepoRateSchedule) -> None:
        self._schedule = schedule
        self._last: date | None = None
        self._balance: Decimal = _ZERO
        #: Exact (unrounded) accrual per calendar month not yet credited, with its first/last day.
        self._pending: dict[tuple[int, int], list[Any]] = {}
        self.credits: list[InterestCredit] = []

    @property
    def accrued_uncredited(self) -> Decimal:
        """Interest accrued but not yet credited (exact) — forfeited if the run ends here."""
        return sum((entry[0] for entry in self._pending.values()), _ZERO)

    def _accrue_through(self, before: date) -> None:
        """Accrue each calendar day in ``[last session, before)`` on the last closing balance."""
        if self._last is None:
            return
        day = self._last
        while day < before:
            month = _month(day)
            daily = self._balance * self._schedule.earning_rate(day) / _DAYS_IN_YEAR
            entry = self._pending.setdefault(month, [_ZERO, day, day])
            entry[0] += daily
            entry[2] = day
            day += timedelta(days=1)

    def credit_due(self, session: date) -> InterestCredit | None:
        """Accrue up to ``session`` and return what is creditable on it (``None`` if nothing).

        Every month strictly before ``session``'s month is paid in one amount, rounded half-up to
        the paisa. A month that accrued nothing (no cash was held) yields no credit.
        """
        if self._last is not None and session <= self._last:
            raise CashInterestError(
                f"interest accrues forward: {session.isoformat()} after {self._last.isoformat()}"
            )
        self._accrue_through(session)
        due = sorted(m for m in self._pending if m < _month(session))
        if not due:
            return None
        amount = sum((self._pending[m][0] for m in due), _ZERO)
        start = min(self._pending[m][1] for m in due)
        end = max(self._pending[m][2] for m in due)
        for month in due:
            del self._pending[month]
        paid = amount.quantize(_PAISA, rounding=ROUND_HALF_UP)
        if paid <= _ZERO:
            return None
        credit = InterestCredit(session, paid, start, end)
        self.credits.append(credit)
        _log.info(
            "cash_interest.credited",
            session=session.isoformat(),
            amount=str(paid),
            period_start=start.isoformat(),
            period_end=end.isoformat(),
        )
        return credit

    def to_document(self) -> dict[str, Any]:
        """The accrual's carried state — what a forward runner persists between sessions.

        The last session, the balance earning since it and every month accrued but not yet
        credited (exact, unrounded), as strings. Past ``credits`` are not state: each is already a
        ledger line where it was paid. ``from_document`` continues the walk exactly.
        """
        return {
            "last": None if self._last is None else self._last.isoformat(),
            "balance": str(self._balance),
            "pending": [
                [
                    f"{year:04d}-{month:02d}",
                    str(entry[0]),
                    entry[1].isoformat(),
                    entry[2].isoformat(),
                ]
                for (year, month), entry in sorted(self._pending.items())
            ],
        }

    @classmethod
    def from_document(
        cls, schedule: RepoRateSchedule, document: Mapping[str, Any]
    ) -> CashInterestAccrual:
        """An accrual that continues from ``document`` (``to_document``) under ``schedule``."""
        accrual = cls(schedule)
        last = document["last"]
        accrual._last = None if last is None else date.fromisoformat(last)
        accrual._balance = _text_decimal(document["balance"], "cash interest balance")
        for month, amount, first, through in document["pending"]:
            year, number = (int(part) for part in str(month).split("-"))
            accrual._pending[(year, number)] = [
                _text_decimal(amount, f"cash interest pending {month}"),
                date.fromisoformat(first),
                date.fromisoformat(through),
            ]
        return accrual

    def close_session(self, session: date, balance: Decimal) -> None:
        """Record ``balance`` — settled cash at the end of ``session`` — as what earns from now.

        Checks ``session`` against the coverage at once, so a run that walks off the schedule fails
        on its first uncovered session rather than at the next month's credit.
        """
        if not isinstance(balance, Decimal):
            raise TypeError("balance must be a Decimal — money is never float (CLAUDE.md)")
        if balance < _ZERO:
            raise CashInterestError(f"settled cash cannot be negative, got {balance}")
        self._schedule.change_for(session)
        self._last = session
        self._balance = balance


# ── the process-wide switch the drivers read ───────────────────────────────────────────────────

_CURRENT: ContextVar[RepoRateSchedule | None] = ContextVar("cash_interest", default=None)


@contextmanager
def accrue_cash_interest(schedule: RepoRateSchedule | None) -> Iterator[None]:
    """Run the enclosed backtest(s) with idle settled cash earning ``schedule``'s rate - 50 bp.

    ``None`` switches it off (the pre-X2-fix accounting, zero on idle cash).
    """
    token = _CURRENT.set(schedule)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_cash_interest() -> RepoRateSchedule | None:
    """The schedule :func:`accrue_cash_interest` put in force, or ``None`` (interest off)."""
    return _CURRENT.get()


def current_cash_interest_identity() -> str | None:
    """The run-spec value for the interest in force, or ``None`` when off (no spec key)."""
    schedule = _CURRENT.get()
    return None if schedule is None else schedule.identity()


def describe_cash_interest(on: bool) -> str:
    """The one-line statement every report header carries."""
    if on:
        return (
            "Idle cash: settled cash earns RBI repo - 0.50% p.a. (Actual/365, daily, credited "
            "monthly; unsettled proceeds earn nothing; the final partial month is forfeited), "
            "taxed at slab as income from other sources."
        )
    return "Idle cash: earns **0%** (cash interest switched off for this run)."


def add_cash_interest_flag(parser: argparse.ArgumentParser, *, default: bool) -> None:
    """Add ``--cash-interest`` / ``--no-cash-interest`` with the entry point's own default."""
    parser.add_argument(
        "--cash-interest",
        dest="cash_interest",
        action=argparse.BooleanOptionalAction,
        default=default,
        help="idle settled cash earns RBI repo - 0.50%% p.a., credited monthly and taxed at slab "
        f"(default: {'on' if default else 'off'})",
    )


def cash_interest_unless(args: argparse.Namespace) -> AbstractContextManager[None]:
    """The context a CLI runs in: interest on if ``--cash-interest`` was in force, else off."""
    if not getattr(args, "cash_interest", False):
        return nullcontext()
    return accrue_cash_interest(load_repo_rate_schedule())
