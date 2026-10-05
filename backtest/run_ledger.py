"""X2: every backtest persists its fill ledger — so every run can be taxed after the fact.

``backtest.tax`` strikes an after-tax XIRR from a ``RunLedger`` and ``backtest.tax_report`` writes
one to disk, but until this module no runner kept what the ledger is made of: the fills went into
the book and were gone, so no after-tax figure existed for any run. This is the seam that keeps
them. ``backtest.run._AccountingBroker`` — the one class every driver walks through — hands each
fill here, and ``backtest.book_actions.BookActionApplier`` logs every dividend, split, bonus and
ISIN carry it applied; :func:`build_run_ledger` turns the two into a ``RunLedger`` at the end of
the run, fills through ``trades_from_fills`` so STT backs out exactly.

**Persisted where the caller says, keyed by what the run was asked to do.** A run's identity is its
*specification* — runner, window, parameters, universe, capital, rail policy, which corporate
actions were in force — rendered canonically (:func:`run_spec`) and hashed (:func:`run_digest`).
The digest exists before the run does, which is what lets a campaign skip a run it has already
finished. Under :func:`persist_run_ledgers` each run writes ``<dir>/ledgers/<digest>.json`` (the
tax module's writer: sorted keys, money as exact strings, so the same run writes the same bytes)
and ``<dir>/runs/<digest>.json`` (its pre-tax metrics, :class:`RunSummary`). Run outputs are not
lake data: the directory is the caller's, never ``data/``.

What the digest does not cover, and a reader must know: the code and the lake. The same
specification over a rebuilt lake or a changed engine is a different run with the same digest, so
a campaign directory is only ever resumed by the commit and lake that started it (the campaign
driver pins both in a manifest and refuses a mismatch).

What this module never does: read a clock, change the book, or compute tax.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from backtest.book_actions import (
    AppliedBookAction,
    AppliedCarry,
    AppliedCashExit,
    AppliedDividend,
    AppliedMerger,
    BookActionCalendar,
    BookActionSource,
    RescaleKind,
    current_signal_split_factors_identity,
)
from backtest.cash_interest import InterestCredit, current_cash_interest_identity
from backtest.nav import PRE_TAX, NavSeries, nav_file, write_nav
from backtest.tax import (
    BonusEvent,
    CorporateEvent,
    DividendCredit,
    InterestIncome,
    ReissueEvent,
    RunLedger,
    SplitEvent,
    TaxTrade,
)
from backtest.tax_report import (
    LedgerFormatError,
    read_run_ledger,
    trades_from_fills,
    write_run_ledger,
)
from backtest.xirr import Cashflow
from dataplatform.logging import get_logger
from dataplatform.store.paths import Layer, layer_root
from execution.broker import Fill, Side

__all__ = [
    "RunOutputLocationError",
    "RunSummary",
    "add_ledger_dir_flag",
    "build_run_ledger",
    "current_ledger_dir",
    "ledger_dir_unless",
    "ledger_path",
    "load_run",
    "persist_run",
    "persist_run_ledgers",
    "refuse_lake_location",
    "run_digest",
    "run_spec",
    "summary_path",
]

_LOG = get_logger(__name__)

#: Bumped when the specification's rendering changes, so an old directory's digests stop matching.
_SPEC_VERSION = "1"
_NIL = Decimal("0")


# ── where runs are persisted: one process-wide switch, like book_corporate_actions ────────────

_LEDGER_DIR: ContextVar[Path | None] = ContextVar("run_ledger_dir", default=None)


@contextmanager
def persist_run_ledgers(out_dir: Path | None) -> Iterator[None]:
    """Persist the ledger and summary of every backtest run inside the block under ``out_dir``.

    A ``ContextVar`` rather than a parameter for the reason ``book_corporate_actions`` is one: the
    runners are called from a dozen report functions, and threading a path through each would put
    the decision in a dozen places. ``None`` switches persistence off.
    """
    token = _LEDGER_DIR.set(out_dir)
    try:
        yield
    finally:
        _LEDGER_DIR.reset(token)


def current_ledger_dir() -> Path | None:
    """The directory :func:`persist_run_ledgers` put in force, or ``None``."""
    return _LEDGER_DIR.get()


class RunOutputLocationError(ValueError):
    """A run-output directory was pointed inside the lake. Run outputs are not lake data."""


def refuse_lake_location(out_dir: Path, data_root: Path | None) -> Path:
    """``out_dir`` resolved, or ``RunOutputLocationError`` if it lies inside the lake root.

    L0 is immutable and L1/L2 are rebuilt from it; a ledger written under ``data/`` would be
    swept into a rebuild, a backup or a lake copy as if it were market data.
    """
    root = (layer_root(Layer.L0, data_root=data_root).parent).resolve()
    resolved = out_dir.resolve()
    if resolved == root or root in resolved.parents:
        raise RunOutputLocationError(
            f"run outputs go outside the lake: {resolved} is inside the data root {root}"
        )
    return resolved


def add_ledger_dir_flag(parser: argparse.ArgumentParser) -> None:
    """Add ``--ledger-dir``: persist every run's fill ledger and summary under that directory."""
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        default=None,
        help="persist each run's fill ledger (ledgers/<digest>.json) and pre-tax summary "
        "(runs/<digest>.json) here; never inside the data lake",
    )


def ledger_dir_unless(args: argparse.Namespace) -> AbstractContextManager[None]:
    """The context a CLI runs in: persistence under ``--ledger-dir`` if given, else off."""
    out_dir: Path | None = getattr(args, "ledger_dir", None)
    if out_dir is None:
        return nullcontext()
    return persist_run_ledgers(refuse_lake_location(out_dir, getattr(args, "data_root", None)))


def ledger_path(out_dir: Path, digest: str) -> Path:
    """Where the run with ``digest`` keeps its fill ledger."""
    return out_dir / "ledgers" / f"{digest}.json"


def summary_path(out_dir: Path, digest: str) -> Path:
    """Where the run with ``digest`` keeps its pre-tax metrics."""
    return out_dir / "runs" / f"{digest}.json"


# ── identity ───────────────────────────────────────────────────────────────────────────────────


def _render(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return value
    return repr(value)  # frozen dataclasses of Decimals/ints/bools: a stable, exact rendering


def _actions_identity(source: BookActionSource | None) -> str:
    if source is None:
        return "off"
    if isinstance(source, BookActionCalendar):
        counts = ",".join(f"{k}={v}" for k, v in source.counts().items())
        terms = source.merger_terms_identity()
        suffix = "" if terms is None else f";{terms}"
        return f"calendar[{len(source)}]:{counts}{suffix}"
    return type(source).__name__


def run_spec(
    runner: str,
    *,
    start: date,
    end: date,
    opening_cash: Decimal,
    book_actions: BookActionSource | None,
    **fields: object,
) -> dict[str, str]:
    """The canonical specification of one run — every input that decides its result.

    ``fields`` are the runner's own parameters (policy parameters, universe, rail-policy digest,
    benchmark slug, signal basis); each is rendered exactly (``repr`` of a frozen dataclass,
    ``str`` of a Decimal) so two specs are equal iff the runs were asked the same thing.

    Interest on idle cash (``backtest.cash_interest``) adds a ``cash_interest`` key naming the rate
    schedule when it is in force, and nothing when it is off — so every run specified before
    interest existed keeps the digest it was persisted under, and an on run never resumes one.
    The swing signal's pre-seam split factors (``backtest.book_actions.signal_split_factors``) add a
    ``signal_split_factors`` key the same way: a run made without them never shares a digest with
    one made with them (X2: the fold campaign once did, and one digest replayed two ways).
    """
    spec = {
        "spec_version": _SPEC_VERSION,
        "runner": runner,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "opening_cash": str(opening_cash),
        "book_actions": _actions_identity(book_actions),
    }
    interest = current_cash_interest_identity()
    if interest is not None:
        spec["cash_interest"] = interest
    signal_factors = current_signal_split_factors_identity()
    if signal_factors is not None:
        spec["signal_split_factors"] = signal_factors
    for key, value in sorted(fields.items()):
        spec[key] = _render(value)
    return spec


def run_digest(spec: Mapping[str, str]) -> str:
    """sha256 over the canonical JSON of ``spec`` — the key a run is persisted under."""
    canonical = json.dumps(dict(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── the ledger ─────────────────────────────────────────────────────────────────────────────────


def _ratio(numerator: Decimal, denominator: Decimal) -> tuple[int, int]:
    exact = Fraction(numerator) / Fraction(denominator)
    return exact.numerator, exact.denominator


def _tax_events(
    applied: Iterable[AppliedBookAction],
) -> tuple[tuple[DividendCredit, ...], tuple[CorporateEvent, ...], tuple[TaxTrade, ...]]:
    """The applier's log in the tax module's terms, in the order the books applied it.

    A share swap is a rescale of the old ISIN's lots followed by a carry into the survivor: each
    lot keeps its acquisition date and its cost, which is the amalgamation rule (Sec 47(vii): not
    a transfer; Sec 2(42A) Expl. 1(b)(c): held from the original purchase). A cash exit is a sale
    of every lot at the exit price, with no charges and no STT — the tender is off-market.
    """
    dividends: list[DividendCredit] = []
    events: list[CorporateEvent] = []
    exits: list[TaxTrade] = []
    for action in applied:
        if isinstance(action, AppliedDividend):
            dividends.append(DividendCredit(action.isin, action.session, action.amount))
        elif isinstance(action, AppliedCarry):
            events.append(ReissueEvent(action.isin, action.ex_date, action.from_isin))
        elif isinstance(action, AppliedMerger):
            numerator, denominator = _ratio(action.numerator, action.denominator)
            events.append(
                SplitEvent(
                    action.from_isin, action.ex_date, numerator, denominator, action.new_quantity
                )
            )
            events.append(ReissueEvent(action.isin, action.ex_date, action.from_isin))
        elif isinstance(action, AppliedCashExit):
            exits.append(
                TaxTrade(
                    isin=action.isin,
                    trade_date=action.ex_date,
                    side=Side.SELL,
                    quantity=action.quantity,
                    net_amount=action.amount,
                    stt=_NIL,  # an off-market tender in the exit window carries no STT
                    stt_known=True,
                )
            )
        elif action.kind is RescaleKind.SPLIT:
            numerator, denominator = _ratio(action.numerator, action.denominator)
            events.append(
                SplitEvent(action.isin, action.ex_date, numerator, denominator, action.new_quantity)
            )
        else:
            # The book's bonus multiple is (new + held) / held; the tax lot wants new : held.
            new, held = _ratio(action.numerator - action.denominator, action.denominator)
            events.append(BonusEvent(action.isin, action.ex_date, new, held, action.new_quantity))
    return tuple(dividends), tuple(events), tuple(exits)


def _replayed_quantities(ledger: RunLedger) -> dict[str, int]:
    """Per-ISIN share counts the ledger's trades and events leave — the reconciliation side."""
    held: dict[str, int] = {}
    timeline: list[tuple[date, int, int, object]] = [
        (e.ex_date, 0, i, e) for i, e in enumerate(ledger.corporate_events)
    ]
    timeline += [(t.trade_date, 1, i, t) for i, t in enumerate(ledger.trades)]
    for _, _, _, item in sorted(timeline, key=lambda row: row[:3]):
        if isinstance(item, ReissueEvent):
            held[item.isin] = held.get(item.isin, 0) + held.pop(item.from_isin, 0)
        elif isinstance(item, SplitEvent | BonusEvent):
            if held.get(item.isin, 0) and item.resulting_quantity is not None:
                held[item.isin] = item.resulting_quantity
        elif isinstance(item, TaxTrade):
            sign = 1 if item.side is Side.BUY else -1
            held[item.isin] = held.get(item.isin, 0) + sign * item.quantity
    return {isin: qty for isin, qty in held.items() if qty}


def build_run_ledger(
    *,
    source: str,
    fills: Sequence[Fill],
    applied: Sequence[AppliedBookAction],
    external_flows: Sequence[Cashflow],
    terminal_date: date,
    terminal_nav: Decimal,
    terminal_prices: Mapping[str, Decimal],
    closing_quantities: Mapping[str, int],
    interest: Sequence[InterestCredit] = (),
) -> RunLedger:
    """A finished run's ``RunLedger``: its fills, its dividend credits and its share-count events.

    Assumes ``fills`` are in the order the broker filled them and ``applied`` in the order the
    books applied it. Raises ``LedgerFormatError`` when the ledger does not reproduce
    ``closing_quantities`` share for share — a ledger that disagrees with the book would tax shares
    the account never held, or leave untaxed ones it did. ``interest`` is every monthly credit of
    interest on idle cash, taxed as income from other sources in the FY it was credited.
    """
    dividends, events, exits = _tax_events(applied)
    ledger = RunLedger(
        source=source,
        trades=(*trades_from_fills(fills), *exits),
        external_flows=tuple(external_flows),
        terminal_date=terminal_date,
        terminal_nav=terminal_nav,
        terminal_prices={isin: terminal_prices[isin] for isin in sorted(closing_quantities)},
        dividends=dividends,
        corporate_events=events,
        interest=tuple(InterestIncome(c.credited, c.amount) for c in interest),
    )
    rebuilt = _replayed_quantities(ledger)
    book = {isin: qty for isin, qty in closing_quantities.items() if qty}
    if rebuilt != book:
        drift = sorted(
            (isin, rebuilt.get(isin, 0), book.get(isin, 0))
            for isin in set(rebuilt) | set(book)
            if rebuilt.get(isin, 0) != book.get(isin, 0)
        )
        raise LedgerFormatError(
            f"{source}: the ledger does not reproduce the closing book "
            f"(isin, ledger, book): {drift[:5]}"
        )
    return ledger


# ── the pre-tax metrics a report needs without re-running ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One finished run's pre-tax figures — what a resumed campaign renders from, never a replay.

    Carries no wall-clock figure, so the file is as deterministic as the run.
    """

    digest: str
    spec: Mapping[str, str]
    policy: str
    start: date
    terminal: date
    sessions: int
    xirr: Decimal
    max_drawdown: Decimal
    excess: Decimal
    benchmark_xirr: Decimal
    benchmark_name: str
    total_charges: Decimal
    final_nav: Decimal
    round_trips: int
    median_hold_days: int
    replay_digest: str
    #: Where the benchmark series came from (``backtest.run``'s ``published_tri`` /
    #: ``m3.9_computed_tri`` / ``l1_proxy``). ``None`` on a summary persisted before it was kept —
    #: a report then says "not recorded" rather than guessing which series it was.
    benchmark_source: str | None = None
    #: ``RAIL_BLOCK`` count per breached rail over the run (``backtest.rails.rail_blocks_by_rail``);
    #: ``None`` on a summary persisted before it was kept, ``{}`` when no rail blocked anything.
    rail_blocks: Mapping[str, int] | None = None

    def to_document(self) -> dict[str, Any]:
        optional: dict[str, Any] = {}
        if self.benchmark_source is not None:
            optional["benchmark_source"] = self.benchmark_source
        if self.rail_blocks is not None:
            optional["rail_blocks"] = dict(sorted(self.rail_blocks.items()))
        return {
            **optional,
            "version": 1,
            "digest": self.digest,
            "spec": dict(sorted(self.spec.items())),
            "policy": self.policy,
            "start": self.start.isoformat(),
            "terminal": self.terminal.isoformat(),
            "sessions": self.sessions,
            "xirr": str(self.xirr),
            "max_drawdown": str(self.max_drawdown),
            "excess": str(self.excess),
            "benchmark_xirr": str(self.benchmark_xirr),
            "benchmark_name": self.benchmark_name,
            "total_charges": str(self.total_charges),
            "final_nav": str(self.final_nav),
            "round_trips": self.round_trips,
            "median_hold_days": self.median_hold_days,
            "replay_digest": self.replay_digest,
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> RunSummary:
        if doc.get("version") != 1:
            raise LedgerFormatError("not a version-1 run summary")
        return cls(
            digest=str(doc["digest"]),
            spec={str(k): str(v) for k, v in doc["spec"].items()},
            policy=str(doc["policy"]),
            start=date.fromisoformat(doc["start"]),
            terminal=date.fromisoformat(doc["terminal"]),
            sessions=int(doc["sessions"]),
            xirr=Decimal(doc["xirr"]),
            max_drawdown=Decimal(doc["max_drawdown"]),
            excess=Decimal(doc["excess"]),
            benchmark_xirr=Decimal(doc["benchmark_xirr"]),
            benchmark_name=str(doc["benchmark_name"]),
            total_charges=Decimal(doc["total_charges"]),
            final_nav=Decimal(doc["final_nav"]),
            round_trips=int(doc["round_trips"]),
            median_hold_days=int(doc["median_hold_days"]),
            replay_digest=str(doc["replay_digest"]),
            benchmark_source=(
                str(doc["benchmark_source"]) if doc.get("benchmark_source") is not None else None
            ),
            rail_blocks=(
                {str(k): int(v) for k, v in doc["rail_blocks"].items()}
                if doc.get("rail_blocks") is not None
                else None
            ),
        )


def persist_run(
    spec: Mapping[str, str],
    ledger: RunLedger,
    summary: RunSummary | None,
    *,
    nav: Sequence[tuple[date, Decimal]] = (),
) -> Path | None:
    """Write the run's ledger (and NAV path, and summary) under the directory in force.

    Returns ``None`` when persistence is off. The ledger and the NAV path (``backtest.nav``, only
    when the runner sampled one) are written first and the summary last, each atomically: a summary
    on disk means the rest is complete, which is the one fact :func:`load_run` resumes on.
    """
    out_dir = current_ledger_dir()
    if out_dir is None:
        return None
    digest = run_digest(spec)
    path = ledger_path(out_dir, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_run_ledger(ledger, path)
    if nav:
        write_nav(NavSeries(digest, PRE_TAX, tuple(nav)), nav_file(out_dir, digest))
    if summary is not None:
        target = summary_path(out_dir, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        partial.write_text(
            json.dumps(summary.to_document(), indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        partial.replace(target)
    _LOG.info("run_ledger.persisted", digest=digest, runner=spec.get("runner"), path=str(path))
    return path


def load_run(out_dir: Path, digest: str) -> tuple[RunSummary, RunLedger] | None:
    """The persisted summary and ledger of run ``digest``, or ``None`` if it has not finished."""
    summary_file, ledger_file = summary_path(out_dir, digest), ledger_path(out_dir, digest)
    if not (summary_file.is_file() and ledger_file.is_file()):
        return None
    summary = RunSummary.from_document(json.loads(summary_file.read_text(encoding="utf-8")))
    if summary.digest != digest:
        raise LedgerFormatError(f"{summary_file}: carries digest {summary.digest}, not {digest}")
    return summary, read_run_ledger(ledger_file)
