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

**Interest on idle cash** (``backtest.cash_interest``). A row whose description starts with
``INTEREST`` is a monthly interest credit; :func:`interest_from_ledger_rows` reads it as an
``InterestIncome`` and :func:`run_ledger_from_backtest` carries both, so the fill adapter passes
such rows over knowing they are taxed, not dropped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import duckdb

from backtest.tax import (
    AfterTaxResult,
    BonusEvent,
    CorporateEvent,
    DividendCredit,
    FyTax,
    InterestIncome,
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
from dataplatform.logging import get_logger
from dataplatform.store.l1 import read_prices_raw
from dataplatform.store.l2 import open_connection, register_raw_view
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

_log = get_logger(__name__)

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
        if description.upper().startswith("INTEREST"):
            continue  # read by interest_from_ledger_rows, which run_ledger_from_backtest also calls
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


def interest_from_ledger_rows(rows: Iterable[Mapping[str, str]]) -> tuple[InterestIncome, ...]:
    """The ``INTEREST`` credit rows of a ``BookSnapshot.ledger``, as ``InterestIncome``."""
    return tuple(
        InterestIncome(date.fromisoformat(row["session"]), Decimal(row["credit"]))
        for row in rows
        if row["description"].upper().startswith("INTEREST")
    )


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
        interest=interest_from_ledger_rows(result.book.ledger),
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
    if ledger.interest:
        # Only when there is any: a ledger with no interest writes the bytes it always did.
        document["interest"] = [
            {"received": i.received.isoformat(), "amount": str(i.amount)} for i in ledger.interest
        ]
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
        interest=tuple(
            InterestIncome(date.fromisoformat(i["received"]), _money(i["amount"], "interest"))
            for i in doc.get("interest", [])
        ),
    )


# ── grandfathering FMV from L1 prices_raw (the as-quoted record) ───────────────────────────────

#: The one quote the FMV is read from. NSE only: legacy-era BSE rows were resolved to an ISIN
#: through today's scrip master, so a retired ISIN's BSE bar sits under its successor (HDFC Bank's
#: 31-01-2018 BSE row carries INE040A01034, the post-2019 ISIN) — reading it would price the wrong
#: instrument. EQ only: the rolling-settlement series the book trades.
_FMV_EXCHANGE: Final = Exchange.NSE.value
_FMV_SERIES: Final = "EQ"
#: The DuckDB view the FMV fallback scan registers on its own connection.
_FMV_RAW_VIEW: Final = "tax_fmv_prices_raw"


class L1GrandfatheringPrices:
    """Sec 55(2)(ac) FMVs read from L1 ``prices_raw`` — the price as quoted, under the ISIN quoted.

    The statute's FMV is the **highest price quoted** on a recognised exchange on 31-01-2018, the
    day's high. This reads the NSE EQ high on ``fmv_date`` for the ISIN asked — the ISIN the lot
    held on that date (``backtest.tax.match_lots`` moves a lot to a successor only for a reissue on
    or before it). Raw, never adjusted: the lot is in shares of its own date, and a split or bonus
    after the FMV date is applied to the lot's ``gf_units``, not to this price. L2 is the wrong
    store for this: a retired ISIN's adjusted history is stitched under its successor, so the ISIN
    a lot actually held on 31-01-2018 (HDFC Bank's INE040A01026) has no L2 partition at all.

    Fallback (Explanation (a)(ii) to Sec 55(2)(ac)): an ISIN that did not trade on ``fmv_date``
    takes the high of the last NSE EQ session before it on which it did — but only when the ISIN
    trades again afterwards, i.e. it was a live listing that was merely untraded that day. One that
    never trades after ``fmv_date`` had ceased (a merger, a delisting, or a reissue the ledger did
    not carry) and was not what the holder held on 31-01-2018; its last pre-cessation quote is a
    different instrument's price, so it raises ``MissingGrandfatheringPriceError`` naming why.

    What it never does: guess, read BSE, read L2, or write. A missing ``fmv_date`` partition (a
    lake gap, not a non-trading day) raises rather than falling back.
    """

    def __init__(
        self,
        *,
        fmv_date: date,
        data_root: Path | None = None,
        con: duckdb.DuckDBPyConnection | None = None,
    ) -> None:
        self._fmv_date = fmv_date
        self._data_root = data_root
        self._con = con
        self._days: dict[date, dict[str, Decimal]] = {}
        self._cache: dict[str, Decimal] = {}
        self._missing: dict[str, str] = {}

    def fmv_per_share(self, isin: str) -> Decimal:
        cached = self._cache.get(isin)
        if cached is not None:
            return cached
        if isin in self._missing:
            raise MissingGrandfatheringPriceError(self._missing[isin])
        try:
            price = self._resolve(isin)
        except MissingGrandfatheringPriceError as error:
            self._missing[isin] = str(error)
            raise
        self._cache[isin] = price
        return price

    def _resolve(self, isin: str) -> Decimal:
        day = self._fmv_date.isoformat()
        high = self._highs_on(self._fmv_date).get(isin)
        if high is not None:
            return high
        before, after = self._neighbours(isin)
        if before is None:
            raise MissingGrandfatheringPriceError(
                f"no 31-01-2018 FMV for {isin}: never quoted on {_FMV_EXCHANGE} {_FMV_SERIES} on "
                f"or before {day} in L1"
            )
        if after is None:
            raise MissingGrandfatheringPriceError(
                f"no 31-01-2018 FMV for {isin}: last quoted on {_FMV_EXCHANGE} {_FMV_SERIES} on "
                f"{before.isoformat()} and never after, so it had ceased before {day} — the share "
                "held that day is a successor the ledger did not carry onto the lot"
            )
        # Explanation (a)(ii): untraded on the FMV date, so the high of the last day it traded.
        fallback = self._highs_on(before).get(isin)
        if fallback is None:  # the scan saw it; the partition read must too
            raise MissingGrandfatheringPriceError(
                f"no 31-01-2018 FMV for {isin}: L1 scan found {before.isoformat()} but its "
                "partition holds no matching row"
            )
        _log.info("tax_report.fmv_fallback", isin=isin, fmv_date=day, quoted_on=before.isoformat())
        return fallback

    def _highs_on(self, day: date) -> dict[str, Decimal]:
        """Every ISIN's NSE EQ high in one session's partition (raises if it was never written)."""
        highs = self._days.get(day)
        if highs is None:
            try:
                rows = read_prices_raw(day, data_root=self._data_root)
            except FileNotFoundError as error:
                raise MissingGrandfatheringPriceError(
                    f"no L1 prices_raw partition for {day.isoformat()}: a lake gap, not a "
                    f"non-trading day ({error})"
                ) from error
            highs = {}
            for row in rows:
                if row["exchange"] == _FMV_EXCHANGE and row["series"] == _FMV_SERIES:
                    highs[str(row["isin"])] = Decimal(str(row["high"]))
            self._days[day] = highs
        return highs

    def _neighbours(self, isin: str) -> tuple[date | None, date | None]:
        """The ISIN's last NSE EQ session before ``fmv_date`` and its first one after it."""
        if self._con is None:
            self._con = open_connection()
            register_raw_view(self._con, view=_FMV_RAW_VIEW, data_root=self._data_root)
        found = self._con.execute(
            "SELECT max(trade_date) FILTER (WHERE trade_date < ?), "
            f"min(trade_date) FILTER (WHERE trade_date > ?) FROM {_FMV_RAW_VIEW} "
            "WHERE isin = ? AND exchange = ? AND series = ?",
            [self._fmv_date, self._fmv_date, isin, _FMV_EXCHANGE, _FMV_SERIES],
        ).fetchone()
        if found is None:
            return None, None
        before, after = found
        return before, after


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
        "112A exemption | Taxable STCG | Taxable LTCG | Dividends | Interest | Tax | Surcharge | "
        "Cess | Total | C/F ST / LT | Paid on |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for f in rows:
        lines.append(
            f"| {fy_label(f.fy)} | {_buckets(f.stcg_gross)} | {_buckets(f.ltcg_gross)} | "
            f"{_rupees(f.st_loss)} | {_rupees(f.lt_loss)} | {_rupees(f.exempt_ltcg_net)} | "
            f"{_rupees(f.brought_forward_used)} | {_rupees(f.exemption_used)} | "
            f"{_buckets(f.stcg_taxable)} | {_buckets(f.ltcg_taxable)} | {_rupees(f.dividends)} | "
            f"{_rupees(f.interest)} | {_rupees(f.cg_tax + f.dividend_tax + f.interest_tax)} | "
            f"{_rupees(f.surcharge)} | {_rupees(f.cess)} | "
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
        f"- Interest on idle cash credited in this run: **{result.interest_credits}** monthly "
        f"credit(s), {_rupees(result.interest_income)} gross"
        + (
            " — taxed at the slab rate as income from other sources in the FY credited, "
            f"surcharge at {_pct(p.dividend_surcharge_rate)} (the dividend band rate; exact for "
            "total income up to ₹2 crore)."
            if result.interest_credits
            else " (the run accrued none, or ran with cash interest off)."
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
    """Tax a persisted ledger, reading grandfathering FMVs from L1 ``prices_raw``."""
    args = _parse_args(argv)
    ledger = read_run_ledger(args.ledger)
    schedule = load_tax_schedule()
    profile = investor_profile_from_args(args)
    fmv = L1GrandfatheringPrices(fmv_date=schedule.grandfather_fmv_date)
    result = compute_after_tax(ledger, profile, schedule=schedule, fmv=fmv)
    report = render_after_tax_report(result, schedule)
    if args.report is not None:
        args.report.write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
