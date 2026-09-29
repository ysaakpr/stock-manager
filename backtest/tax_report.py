"""X2: after-tax report wiring — ledger adapters, L1 grandfathering prices, the markdown report.

``backtest.tax`` does the arithmetic on a ``RunLedger``; this module gets a ``RunLedger`` out of
whatever a run leaves behind and renders the result. Three sources, most exact first:

- ``trades_from_fills`` — ``Fill`` objects, which carry the cost model's line items, so STT (not
  deductible, Sec 48) is backed out exactly;
- ``trades_from_ledger_rows`` — the ``BookSnapshot.ledger`` rows a ``ReplayResult`` carries (the
  ``SimBroker`` cash ledger: ``"BUY 10 @ 123.45"`` with the net debit/credit). Only net cash is
  recorded there, so STT cannot be separated and the report says so;
- a persisted JSON ledger (``write_run_ledger`` / ``read_run_ledger``), which is what the CLI
  reads: ``uv run python -m backtest.tax_report LEDGER.json --slab-rate 0.30 ...``.

**Dependency on the run code (not edited here).** No backtest runner persists its fill ledger
today — sweeps write markdown only — so there is nothing on disk to measure. A runner that wants
an after-tax line calls ``run_ledger_from_backtest(result, terminal_prices=...)`` and
``write_run_ledger``; the terminal prices are the ones ``backtest.run._terminal_prices`` already
computes for the NAV.

**Dividend credits (the parallel dividend task).** A ledger row whose description starts with
``DIVIDEND`` is read as ``DividendCredit(isin, session, credit)`` — gross amount, credit date. Any
other description that is not a fill raises ``LedgerFormatError`` rather than being skipped: a row
this adapter does not understand is cash it would silently leave untaxed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backtest.tax import (
    AfterTaxResult,
    BonusEvent,
    CorporateEvent,
    DividendCredit,
    FyTax,
    InvestorProfile,
    MissingGrandfatheringPriceError,
    PaymentTiming,
    ReissueEvent,
    RunLedger,
    SplitEvent,
    TaxError,
    TaxSchedule,
    TaxTrade,
    compute_after_tax,
    fy_label,
    load_tax_schedule,
)
from backtest.xirr import Cashflow
from dataplatform.identity import Exchange
from dataplatform.query import AdjustedSeriesRequest, QueryService
from execution.broker import Fill, Side

if TYPE_CHECKING:
    from backtest.run import BacktestResult

__all__ = [
    "L1GrandfatheringPrices",
    "LedgerFormatError",
    "add_investor_flags",
    "investor_profile_from_args",
    "main",
    "read_run_ledger",
    "render_after_tax_report",
    "run_ledger_from_backtest",
    "trades_from_fills",
    "trades_from_ledger_rows",
    "write_run_ledger",
]

_ZERO = Decimal("0")
_FILL_ROW = re.compile(r"^(BUY|SELL) (\d+) @ (\S+)$")


class LedgerFormatError(TaxError):
    """A ledger row or persisted ledger file is not in a shape this adapter understands."""


# ── adapters ───────────────────────────────────────────────────────────────────────────────────


def trades_from_fills(fills: Iterable[Fill]) -> tuple[TaxTrade, ...]:
    """Fills -> tax trades, with STT separated exactly from the cost model's breakdown."""
    return tuple(
        TaxTrade(
            isin=f.isin,
            trade_date=f.session,
            side=f.side,
            quantity=f.quantity,
            net_amount=f.cost.net_amount,
            stt=f.cost.securities_transaction_tax,
            stt_known=True,
        )
        for f in fills
    )


def trades_from_ledger_rows(
    rows: Iterable[Mapping[str, str]],
) -> tuple[tuple[TaxTrade, ...], tuple[DividendCredit, ...]]:
    """``BookSnapshot.ledger`` rows -> (trades, dividend credits), in posting order.

    A fill row's description is ``"<BUY|SELL> <qty> @ <price>"`` (``SimBroker._post_ledger``);
    its cash is the debit (buy) or credit (sell), i.e. the net amount including every charge.
    STT is not separable here, so ``stt_known`` is False. Raises ``LedgerFormatError`` on any row
    it does not recognise.
    """
    trades: list[TaxTrade] = []
    dividends: list[DividendCredit] = []
    for row in rows:
        description = row["description"]
        session = date.fromisoformat(row["session"])
        if description.upper().startswith("DIVIDEND"):
            dividends.append(DividendCredit(row["isin"], session, Decimal(row["credit"])))
            continue
        match = _FILL_ROW.match(description)
        if match is None:
            raise LedgerFormatError(
                f"ledger row seq {row.get('seq')} has an unrecognised description {description!r}"
            )
        side = Side(match.group(1))
        amount = Decimal(row["debit"] if side is Side.BUY else row["credit"])
        trades.append(
            TaxTrade(
                isin=row["isin"],
                trade_date=session,
                side=side,
                quantity=int(match.group(2)),
                net_amount=amount,
                stt=_ZERO,
                stt_known=False,
            )
        )
    return tuple(trades), tuple(dividends)


def run_ledger_from_backtest(
    result: BacktestResult, *, terminal_prices: Mapping[str, Decimal]
) -> RunLedger:
    """A ``RunLedger`` from a finished ``BacktestResult`` (read-only).

    The external stream is the runners' one opening deposit on ``result.start``; the fills are the
    ``SimBroker`` ledger rows in ``result.book``. Refuses (``LedgerFormatError``) if the lots
    rebuilt from those fills would not reproduce the book's closing holdings — that mismatch means
    a corporate action moved shares without a ledger row, and the tax on it would be wrong.
    """
    trades, dividends = trades_from_ledger_rows(result.book.ledger)
    held: dict[str, int] = {}
    for trade in trades:
        sign = 1 if trade.side is Side.BUY else -1
        held[trade.isin] = held.get(trade.isin, 0) + sign * trade.quantity
    book: dict[str, int] = {}
    for row in (*result.book.holdings, *result.book.positions):
        book[row["isin"]] = book.get(row["isin"], 0) + int(row["quantity"])
    rebuilt = {isin: qty for isin, qty in held.items() if qty}
    if rebuilt != book:
        raise LedgerFormatError(
            "fills do not reproduce the closing book (a corporate action without a ledger row?): "
            f"rebuilt {sorted(rebuilt.items())[:5]} vs book {sorted(book.items())[:5]}"
        )
    return RunLedger(
        source=f"{result.policy} {result.start.isoformat()}..{result.terminal.isoformat()}",
        trades=trades,
        external_flows=(Cashflow(result.start, -result.opening_cash),),
        terminal_date=result.terminal,
        terminal_nav=result.final_nav,
        terminal_prices=dict(terminal_prices),
        dividends=dividends,
    )


# ── persistence ────────────────────────────────────────────────────────────────────────────────


def write_run_ledger(ledger: RunLedger, path: Path) -> None:
    """Persist ``ledger`` as JSON — money as exact strings, dates ISO, keys sorted."""
    events: list[dict[str, str]] = []
    for event in ledger.corporate_events:
        row: dict[str, str]
        if isinstance(event, ReissueEvent):
            row = {"kind": "reissue", "from_isin": event.from_isin}
        elif isinstance(event, SplitEvent):
            row = {
                "kind": "split",
                "numerator": str(event.numerator),
                "denominator": str(event.denominator),
            }
        else:
            row = {
                "kind": "bonus",
                "new_shares": str(event.new_shares),
                "held_shares": str(event.held_shares),
            }
        row["isin"] = event.isin
        row["ex_date"] = event.ex_date.isoformat()
        resulting = getattr(event, "resulting_quantity", None)
        if resulting is not None:
            row["resulting_quantity"] = str(resulting)
        events.append(row)
    document = {
        "version": 1,
        "source": ledger.source,
        "terminal_date": ledger.terminal_date.isoformat(),
        "terminal_nav": str(ledger.terminal_nav),
        "terminal_prices": {k: str(v) for k, v in sorted(ledger.terminal_prices.items())},
        "external_flows": [
            {"date": f.when.isoformat(), "amount": str(f.amount)} for f in ledger.external_flows
        ],
        "trades": [
            {
                "isin": t.isin,
                "trade_date": t.trade_date.isoformat(),
                "side": t.side.value,
                "quantity": str(t.quantity),
                "net_amount": str(t.net_amount),
                "stt": str(t.stt),
                "stt_known": "true" if t.stt_known else "false",
            }
            for t in ledger.trades
        ],
        "dividends": [
            {"isin": d.isin, "received": d.received.isoformat(), "amount": str(d.amount)}
            for d in ledger.dividends
        ],
        "corporate_events": events,
    }
    # Written beside the target and renamed over it, so a run killed mid-write leaves no partial
    # ledger for a resumed campaign to mistake for a finished one.
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    partial.replace(path)


def _money(raw: object, where: str) -> Decimal:
    if not isinstance(raw, str):
        raise LedgerFormatError(f"{where}: money must be a string, got {raw!r}")
    return Decimal(raw)


def read_run_ledger(path: Path) -> RunLedger:
    """Load a ledger written by ``write_run_ledger``; ``LedgerFormatError`` on a bad shape."""
    doc: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise LedgerFormatError(f"{path}: not a version-1 run ledger")
    events: list[CorporateEvent] = []
    for e in doc["corporate_events"]:
        resulting = int(e["resulting_quantity"]) if "resulting_quantity" in e else None
        if e["kind"] == "split":
            events.append(
                SplitEvent(
                    e["isin"],
                    date.fromisoformat(e["ex_date"]),
                    int(e["numerator"]),
                    int(e["denominator"]),
                    resulting,
                )
            )
        elif e["kind"] == "bonus":
            events.append(
                BonusEvent(
                    e["isin"],
                    date.fromisoformat(e["ex_date"]),
                    int(e["new_shares"]),
                    int(e["held_shares"]),
                    resulting,
                )
            )
        elif e["kind"] == "reissue":
            events.append(ReissueEvent(e["isin"], date.fromisoformat(e["ex_date"]), e["from_isin"]))
        else:
            raise LedgerFormatError(f"unsupported corporate event kind {e['kind']!r}")
    return RunLedger(
        source=str(doc["source"]),
        terminal_date=date.fromisoformat(doc["terminal_date"]),
        terminal_nav=_money(doc["terminal_nav"], "terminal_nav"),
        terminal_prices={k: _money(v, k) for k, v in doc["terminal_prices"].items()},
        external_flows=tuple(
            Cashflow(date.fromisoformat(f["date"]), _money(f["amount"], "flow"))
            for f in doc["external_flows"]
        ),
        trades=tuple(
            TaxTrade(
                isin=t["isin"],
                trade_date=date.fromisoformat(t["trade_date"]),
                side=Side(t["side"]),
                quantity=int(t["quantity"]),
                net_amount=_money(t["net_amount"], "net_amount"),
                stt=_money(t["stt"], "stt"),
                stt_known=t["stt_known"] == "true",
            )
            for t in doc["trades"]
        ),
        dividends=tuple(
            DividendCredit(d["isin"], date.fromisoformat(d["received"]), _money(d["amount"], "div"))
            for d in doc["dividends"]
        ),
        corporate_events=tuple(events),
    )


# ── grandfathering FMV from L1 via the public query surface ─────────────────────────────────


class L1GrandfatheringPrices:
    """Sec 55(2)(ac) FMVs read from the lake through ``QueryService`` (the public query surface).

    The statute's FMV is the **highest price quoted** on a recognised exchange on 31-01-2018 (or,
    if the share did not trade that day, on the last day before it that it did) — the day's high,
    not its close. Each exchange's own bar is read (``primary`` pinned, fall-back bars discarded),
    the raw high recovered as ``adj_high / cum_price_factor`` (L2 stores ``raw x factor``), and the
    higher of the two exchanges taken. Raw, because lots carry the shares of their own date and a
    later split is applied to the lot, not to this price.
    """

    def __init__(self, service: QueryService, *, fmv_date: date, lookback_days: int = 30) -> None:
        self._service = service
        self._fmv_date = fmv_date
        self._lookback = timedelta(days=lookback_days)
        self._cache: dict[str, Decimal] = {}

    def fmv_per_share(self, isin: str) -> Decimal:
        cached = self._cache.get(isin)
        if cached is not None:
            return cached
        highs: list[Decimal] = []
        for exchange in (Exchange.NSE, Exchange.BSE):
            series = self._service.adjusted_series(
                AdjustedSeriesRequest(
                    isin=isin,
                    start=self._fmv_date - self._lookback,
                    end=self._fmv_date,
                    primary=exchange,
                )
            )
            own = [p for p in series.points if p.exchange == exchange and not p.fell_back]
            if own:
                last = max(own, key=lambda p: p.trade_date)
                highs.append(last.adj_high / last.cum_price_factor)
        if not highs:
            raise MissingGrandfatheringPriceError(
                f"no L1 bar for {isin} on or within {self._lookback.days} days before "
                f"{self._fmv_date.isoformat()} on either exchange"
            )
        self._cache[isin] = max(highs)
        return self._cache[isin]


# ── the report ─────────────────────────────────────────────────────────────────────────────────


def _pct(value: Decimal) -> str:
    return f"{value * 100:.2f}%"


def _rupees(value: Decimal) -> str:
    return f"₹{value:,.2f}"


def _buckets(buckets: Mapping[Decimal, Decimal]) -> str:
    live = {r: v for r, v in buckets.items() if v}
    if not live:
        return "0"
    return " + ".join(f"{_rupees(v)}@{_pct(r)}" for r, v in sorted(live.items()))


def _fy_table(rows: Sequence[FyTax]) -> list[str]:
    lines = [
        "| FY | STCG gross | LTCG gross | STCL | LTCL | Exempt LTCG (10(38)) | B/F used | "
        "112A exemption | Taxable STCG | Taxable LTCG | Dividends | Tax | Surcharge | Cess | "
        "Total | C/F ST / LT | Paid on |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for f in rows:
        lines.append(
            f"| {fy_label(f.fy)} | {_buckets(f.stcg_gross)} | {_buckets(f.ltcg_gross)} | "
            f"{_rupees(f.st_loss)} | {_rupees(f.lt_loss)} | {_rupees(f.exempt_ltcg_net)} | "
            f"{_rupees(f.brought_forward_used)} | {_rupees(f.exemption_used)} | "
            f"{_buckets(f.stcg_taxable)} | {_buckets(f.ltcg_taxable)} | {_rupees(f.dividends)} | "
            f"{_rupees(f.cg_tax + f.dividend_tax)} | {_rupees(f.surcharge)} | {_rupees(f.cess)} | "
            f"{_rupees(f.total)} | {_rupees(f.carried_forward_st)} / "
            f"{_rupees(f.carried_forward_lt)} | {f.payment_date.isoformat()} |"
        )
    return lines


def render_after_tax_report(result: AfterTaxResult, schedule: TaxSchedule) -> str:
    """The after-tax report: assumptions first, then XIRRs, per-FY tax, and rate provenance."""
    p = result.profile
    cess = "schedule (dated, see table)" if p.cess_override is None else _pct(p.cess_override)
    gf = sum(1 for r in (*result.realisations, *result.deemed_realisations) if r.grandfathered)
    lines = [
        f"# After-tax returns — {result.source}",
        "",
        "## Investor assumptions (explicit — nothing below is defaulted)",
        "",
        f"- **Residency / status:** {p.residency} (resident individual; not HUF, NRI or company).",
        f"- **Slab rate** (dividends from FY2020-21): {_pct(p.slab_rate)} "
        "before surcharge and cess.",
        f"- **Surcharge:** {_pct(p.cg_surcharge_rate)} on 111A/112A tax; "
        f"{_pct(p.dividend_surcharge_rate)} on dividend tax.",
        f"- **Cess:** {cess}.",
        f"- **Tax payment date:** `{p.payment_timing.value}` — each FY's tax is an investor "
        "outflow on that date, paid from outside the book (the walk and its NAV are untouched).",
        "- Basic exemption limit assumed exhausted by other income; no Sec 87A rebate; returns "
        "filed on time so losses carry forward (Sec 80). "
        "Income/tax not rounded per Secs 288A/288B.",
        "- Every trade is an STT-paid delivery trade in a listed equity share "
        "(Secs 111A/112A/10(38)).",
        "- Where the Act prescribes no order, losses and the 112A exemption absorb the "
        "highest-rate gains first and brought-forward losses are used oldest-first "
        "(taxpayer-favourable).",
        "- STT excluded from cost/consideration (Sec 48): "
        + (
            "**yes, exactly from the fills.**"
            if result.stt_known
            else "**NO — the ledger records only net cash, so STT was treated as deductible; "
            "tax is understated by roughly the STT's share of each gain.**"
        ),
        f"- Dividends credited in this run: **{result.dividends_credited}**"
        + (
            " — dividend tax is zero because the run credited none, not because dividends are "
            "untaxed."
            if result.dividends_credited == 0
            else "."
        ),
        f"- Lots grandfathered under Sec 55(2)(ac): {gf}. Buybacks: none modelled (exchange sells "
        "are ordinary transfers; the 01-10-2024 deemed-dividend rule touches only tendered "
        "buybacks, which the backtest never does).",
        "",
        "## Returns",
        "",
        "| Measure | XIRR |",
        "|---|---:|",
        f"| Pre-tax | {_pct(result.pre_tax_xirr)} |",
        "| After tax — realised gains only (holdings pre-tax) | "
        + (
            f"{_pct(result.after_tax_xirr_realised)} |"
            if result.after_tax_xirr_realised is not None
            else f"n/a — {result.realised_xirr_error} |"
        ),
        f"| After tax — deemed liquidation on {result.terminal_date.isoformat()} | "
        + (
            f"{_pct(result.after_tax_xirr_liquidated)} |"
            if result.after_tax_xirr_liquidated is not None
            else f"n/a — {result.liquidation_error} |"
        ),
        "",
        f"Total tax on realised gains: {_rupees(result.total_tax)}; including the deemed "
        "liquidation: "
        + (
            _rupees(result.total_tax_liquidated)
            if result.liquidation_error is None
            else "n/a (see above)"
        )
        + f". Terminal NAV (pre-tax): {_rupees(result.terminal_nav)}.",
        "",
        "## Tax per financial year (realised)",
        "",
        *_fy_table(result.fy_taxes),
        "",
        "## Tax per financial year (with deemed liquidation)",
        "",
        *_fy_table(result.fy_taxes_liquidated),
        "",
        "## Rate provenance",
        "",
        f"Schedule coverage: {schedule.coverage_from.isoformat()} to "
        f"{schedule.coverage_through.isoformat()} (a date outside raises). Grandfathering: lots "
        f"acquired on or before {schedule.grandfather_acquired_on_or_before.isoformat()}, FMV = "
        f"highest price on {schedule.grandfather_fmv_date.isoformat()} "
        f"({schedule.grandfather_citation}). Losses: {schedule.loss_citation}; carry forward "
        f"{schedule.loss_carry_forward_years} years.",
        "",
        "| Item | From | Value | Provenance | Citation |",
        "|---|---|---|---|---|",
        *(f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in schedule.provenance_rows()),
        "",
    ]
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────


def add_investor_flags(parser: argparse.ArgumentParser) -> None:
    """The investor assumptions every after-tax CLI takes — all required, none defaulted.

    Shared by this CLI, ``backtest.sweep``, ``backtest.verdict`` and ``backtest.campaign`` so the
    four state the same assumptions under the same names. A missing flag is an argparse error:
    an after-tax figure struck on an assumption nobody stated is the defect this refuses.
    """
    group = parser.add_argument_group("investor (resident individual; every flag required)")
    group.add_argument(
        "--slab-rate",
        type=Decimal,
        required=True,
        help="marginal slab rate dividends are taxed at from FY2020-21, as a ratio (e.g. 0.30)",
    )
    group.add_argument(
        "--cg-surcharge",
        type=Decimal,
        required=True,
        help="surcharge on Sec 111A/112A tax, as a ratio (e.g. 0.15)",
    )
    group.add_argument(
        "--dividend-surcharge",
        type=Decimal,
        required=True,
        help="surcharge on dividend tax, as a ratio",
    )
    group.add_argument(
        "--payment",
        choices=[t.value for t in PaymentTiming],
        required=True,
        help="when each FY's tax is paid: fy_end (31 Mar) or self_assessment (31 Jul after)",
    )


def investor_profile_from_args(args: argparse.Namespace) -> InvestorProfile:
    """The ``InvestorProfile`` the flags of :func:`add_investor_flags` state."""
    return InvestorProfile(
        residency="resident_individual",
        slab_rate=args.slab_rate,
        cg_surcharge_rate=args.cg_surcharge,
        dividend_surcharge_rate=args.dividend_surcharge,
        payment_timing=PaymentTiming(args.payment),
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backtest.tax_report",
        description="After-tax XIRR for a persisted run ledger (resident individual).",
    )
    parser.add_argument("ledger", type=Path, help="run ledger JSON written by write_run_ledger")
    # No defaults on purpose: every investor assumption is stated by whoever runs the report.
    add_investor_flags(parser)
    parser.add_argument("--report", type=Path, help="write the markdown report here")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Tax a persisted ledger, reading grandfathering FMVs from the lake via ``QueryService``."""
    args = _parse_args(argv)
    ledger = read_run_ledger(args.ledger)
    schedule = load_tax_schedule()
    profile = investor_profile_from_args(args)
    with QueryService() as service:
        fmv = L1GrandfatheringPrices(service, fmv_date=schedule.grandfather_fmv_date)
        result = compute_after_tax(ledger, profile, schedule=schedule, fmv=fmv)
    report = render_after_tax_report(result, schedule)
    if args.report is not None:
        args.report.write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
