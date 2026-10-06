"""Promote the NSE pre-ISIN era (2006-01-02 .. 2011-06-21) into `prices_raw`, where it is proved.

``uv run python -m dataplatform.ingest.pre_isin_promote promote --from 2006-01-02 --to 2011-06-21``

W1 fetched every E1 bhavcopy into L0 and enumerated each row into `prices_raw_quarantine` under
`isin_column_absent`, because the era has no ISIN column and ISIN is the only join key (invariant
#2). This driver re-derives those sessions from L0 through the identity resolver
(`dataplatform.identity.pre_isin`): a row the resolver admits becomes a `prices_raw` row carrying
the ISIN its chain proved, and every other row stays in quarantine under
`isin_column_absent:<reason>`, so the partition still accounts for every row the payload held.

What it reads, all offline: the E1 payloads and the first ISIN-era sessions (the chain anchors) from
L0, NSE's `symbolchange.csv` from its latest L0 snapshot, and the stored `corporate_actions` from
Postgres (read-only — one SELECT). What it writes: the E1 dates' `prices_raw` and
`prices_raw_quarantine` partitions, under `--data-root` (default: the configured lake). Nothing
else: no `sync_state` row (this is a re-derivation of a derived store from published bytes, the same
stance `price_rebuild` takes), no identity table, no L2.

What it never does: fetch, read a current-day listing, or write a row the resolver did not admit.
`total_trades` is NULL on every row it writes — the era did not publish TOTALTRADES.

`validate` runs the identical resolver on the ISIN era with the ISINs hidden (anchors at each
`--cutoff`, chains walking back to 2011-06-22) and counts every admitted row whose hidden ISIN
differs from the one the resolver chose. That is the false-admission rate measured against the
exchange's own published answer.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import get_settings
from dataplatform.identity.ingest import parse_symbol_changes
from dataplatform.identity.pre_isin import (
    ActionEvidence,
    ChainRow,
    PreIsinReason,
    Rename,
    ResolverConfig,
    ResolveReport,
    issuer_of,
    resolve,
)
from dataplatform.ingest.models import PreIsinPriceRow, PriceRow, UnidentifiedRow
from dataplatform.ingest.nse import bhavcopy, bhavcopy_legacy, eras
from dataplatform.logging import get_logger
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.l1 import write_prices_raw, write_unidentified_quarantine
from dataplatform.store.schemas import PriceQuarantineReason

__all__ = [
    "AnchorRows",
    "PromotionStats",
    "load_actions",
    "load_anchor_rows",
    "load_pre_isin_rows",
    "load_renames",
    "main",
    "promote",
    "quarantine_reason",
    "validate",
]

_LOG = get_logger(__name__)

_LEGACY: Final = bhavcopy_legacy.LEGACY_SOURCE_ID
_SYMBOL_CHANGES: Final = "nse_symbol_changes"

#: How many ISIN-era sessions are read for anchors. Must exceed `ResolverConfig.anchor_window`.
_ANCHOR_SESSIONS: Final = 30


def quarantine_reason(reason: PreIsinReason) -> str:
    """The `prices_raw_quarantine.reason` an unadmitted E1 row is filed under."""
    return f"{PriceQuarantineReason.ISIN_COLUMN_ABSENT_PREFIX}{reason.value}"


@dataclass(frozen=True, slots=True)
class AnchorRows:
    """ISIN-era rows read for the resolver: chain evidence, with the ISIN each row stated."""

    rows: tuple[ChainRow, ...]
    sessions: tuple[date, ...]


@dataclass(slots=True)
class PromotionStats:
    """What one promotion did, per year: rows and rupee value admitted and refused, by reason."""

    sessions: int = 0
    rows_by_year: dict[int, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    value_by_year: dict[int, dict[str, Decimal]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(Decimal))
    )
    reasons: Counter[str] = field(default_factory=Counter)
    chain_stops: dict[str, int] = field(default_factory=dict)
    boundary_links: Counter[str] = field(default_factory=Counter)
    failures: list[tuple[date, str]] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        """The report a gate note quotes, as plain JSON."""
        years: dict[str, object] = {}
        for year in sorted(self.rows_by_year):
            rows = self.rows_by_year[year]
            value = self.value_by_year[year]
            total = value["resolved"] + value["quarantined"]
            years[str(year)] = {
                "rows_resolved": rows["resolved"],
                "rows_quarantined": rows["quarantined"],
                "value_resolved_inr": str(value["resolved"]),
                "value_total_inr": str(total),
                "value_coverage": float(value["resolved"] / total) if total else 0.0,
            }
        return {
            "sessions": self.sessions,
            "years": years,
            "reasons": dict(self.reasons.most_common()),
            "chain_stops": self.chain_stops,
            "boundary_links": dict(self.boundary_links),
            "failures": [(d.isoformat(), why) for d, why in self.failures],
        }


# ── evidence loading ─────────────────────────────────────────────────────────────────────────


def _legacy_refs(l0: L0Store, start: date, end: date) -> list[L0Ref]:
    """The stored legacy bhavcopies in `[start, end]`, one per session, ascending."""
    return [
        ref
        for ref in l0.iter_refs(_LEGACY, start=start, end=end)
        if ref.filename.lower().endswith("bhav.csv.zip")
    ]


def load_pre_isin_rows(
    l0: L0Store, start: date, end: date
) -> dict[date, tuple[PreIsinPriceRow, ...]]:
    """Every E1 session's priced rows in `[start, end]`, read back from L0 (re-checksummed)."""
    out: dict[date, tuple[PreIsinPriceRow, ...]] = {}
    for ref in _legacy_refs(l0, start, min(end, eras.PRE_ISIN_ERA_LAST_SESSION)):
        out[ref.logical_date] = bhavcopy_legacy.parse_pre_isin_prices_l0(l0, ref)
    return out


def load_anchor_rows(
    l0: L0Store, start: date, *, sessions: int = _ANCHOR_SESSIONS, end: date | None = None
) -> AnchorRows:
    """ISIN-era rows from `start` for `sessions` sessions (or through `end`), as chain evidence."""
    rows: list[ChainRow] = []
    days: list[date] = []
    for ref in _legacy_refs(l0, start, end or start + timedelta(days=120)):
        if end is None and len(days) >= sessions:
            break
        parsed = bhavcopy.parse_l0_report(l0, ref)
        days.append(ref.logical_date)
        rows.extend(
            ChainRow(
                symbol=row.symbol,
                series=row.series,
                trade_date=row.trade_date,
                close=row.close,
                prev_close=row.prev_close,
                isin=row.isin,
            )
            for row in parsed.rows
        )
    return AnchorRows(rows=tuple(rows), sessions=tuple(days))


def load_renames(l0: L0Store) -> tuple[Rename, ...]:
    """NSE's renames from the latest `symbolchange.csv` snapshot in L0. Raises if there is none."""
    refs = list(l0.iter_refs(_SYMBOL_CHANGES))
    if not refs:
        raise FileNotFoundError(
            f"no {_SYMBOL_CHANGES} snapshot in L0; fetch one with "
            "`python -m dataplatform.ingest.identity_refresh` before resolving renames"
        )
    latest = refs[-1]
    text = l0.get(latest).decode("utf-8", errors="strict")
    _LOG.info(
        "pre_isin.renames_loaded",
        source=_SYMBOL_CHANGES,
        date=latest.logical_date.isoformat(),
        filename=latest.filename,
    )
    return tuple(
        Rename(effective=c.effective_date, old=c.old_symbol, new=c.new_symbol)
        for c in parse_symbol_changes(text)
    )


def load_actions(rows: Iterable[tuple[str, str | None, date, str]]) -> tuple[ActionEvidence, ...]:
    """`(isin, filed_against_isin, ex_date, action_type)` records as per-issuer evidence.

    An action filed against an older ISIN of a different issuer prefix counts for both issuers, so
    a merger's predecessor still sees its own events.
    """
    out: list[ActionEvidence] = []
    for isin, filed, ex_date, action_type in rows:
        out.append(ActionEvidence(issuer=issuer_of(isin), ex_date=ex_date, action_type=action_type))
        if filed and issuer_of(filed) != issuer_of(isin):
            out.append(
                ActionEvidence(issuer=issuer_of(filed), ex_date=ex_date, action_type=action_type)
            )
    return tuple(out)


def _actions_from_db() -> tuple[ActionEvidence, ...]:
    """The stored corporate actions, read-only."""
    with connection(get_settings()) as conn:
        cur = conn.execute(
            "SELECT isin, filed_against_isin, ex_date, action_type FROM corporate_actions"
        )
        rows = [(str(r[0]), r[1] and str(r[1]), r[2], str(r[3])) for r in cur.fetchall()]
        conn.rollback()
    return load_actions(rows)


# ── promotion ────────────────────────────────────────────────────────────────────────────────


def _chain_rows(pre: dict[date, tuple[PreIsinPriceRow, ...]]) -> list[ChainRow]:
    return [
        ChainRow(
            symbol=row.symbol,
            series=row.series,
            trade_date=row.trade_date,
            close=row.close,
            prev_close=row.prev_close,
        )
        for day in sorted(pre)
        for row in pre[day]
    ]


def promote(
    pre: dict[date, tuple[PreIsinPriceRow, ...]],
    anchors: AnchorRows,
    *,
    renames: Sequence[Rename],
    actions: Sequence[ActionEvidence],
    data_root: Path | None,
    config: ResolverConfig | None = None,
    dry_run: bool = False,
) -> PromotionStats:
    """Resolve the E1 sessions in `pre` and write each one's `prices_raw` + quarantine partitions.

    Every row of every session lands in exactly one of the two partitions, which is what lets the
    report's per-year resolved + quarantined equal the rows the payloads held.
    """
    report = resolve(
        [*_chain_rows(pre), *anchors.rows],
        first_isin_session=eras.ISIN_ERA_START,
        renames=renames,
        actions=actions,
        config=config,
    )
    verdict = {(r.symbol, r.series, r.trade_date): r for r in report.resolutions}
    stats = PromotionStats(chain_stops=report.stops())
    stats.boundary_links.update(c.boundary_link or "none" for c in report.chains)

    for day in sorted(pre):
        prices: list[PriceRow] = []
        refused: list[UnidentifiedRow] = []
        reasons: list[str] = []
        for row in pre[day]:
            res = verdict[(row.symbol, row.series, row.trade_date)]
            bucket = "resolved" if res.isin is not None else "quarantined"
            stats.rows_by_year[day.year][bucket] += 1
            stats.value_by_year[day.year][bucket] += row.total_traded_value
            stats.reasons[res.reason.value] += 1
            if res.isin is not None:
                prices.append(
                    PriceRow(
                        isin=res.isin,
                        symbol=row.symbol,
                        series=row.series,
                        trade_date=row.trade_date,
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        close=row.close,
                        last=row.last,
                        prev_close=row.prev_close,
                        total_traded_qty=row.total_traded_qty,
                        total_traded_value=row.total_traded_value,
                        total_trades=None,
                    )
                )
            else:
                refused.append(
                    UnidentifiedRow(
                        symbol=row.symbol,
                        series=row.series,
                        trade_date=row.trade_date,
                        stated_isin="",
                        line=row.line,
                    )
                )
                reasons.append(quarantine_reason(res.reason))
        stats.sessions += 1
        if dry_run:
            continue
        try:
            if prices:
                write_prices_raw(
                    prices,
                    unidentified_rows=refused,
                    unidentified_reasons=reasons,
                    data_root=data_root,
                )
            else:
                write_unidentified_quarantine(
                    refused, trade_date=day, data_root=data_root, reasons=reasons
                )
        except Exception as exc:  # one session's write must not end a five-year walk
            stats.failures.append((day, f"{type(exc).__name__}: {exc}"))
            _LOG.error("pre_isin.session_failed", date=day.isoformat(), error=str(exc))
            continue
        _LOG.info(
            "pre_isin.session_promoted",
            source=_LEGACY,
            date=day.isoformat(),
            resolved=len(prices),
            quarantined=len(refused),
            state="PUBLISHED",
        )
    return stats


# ── validation on the ISIN era ───────────────────────────────────────────────────────────────


def validate(
    rows: Sequence[ChainRow],
    *,
    cutoffs: Sequence[date],
    renames: Sequence[Rename],
    actions: Sequence[ActionEvidence],
    config: ResolverConfig | None = None,
) -> dict[str, object]:
    """Run the resolver on ISIN-era rows with ISINs hidden before each cutoff; count mistakes.

    `rows` are ISIN-era rows with their published ISINs. For each cutoff, rows before it lose their
    ISIN, rows from it on keep theirs (they are the anchors), and every admitted row is compared to
    the ISIN the exchange actually published for it.
    """
    truth = {(r.symbol, r.series, r.trade_date): r.isin for r in rows}
    out: dict[str, object] = {}
    for cutoff in cutoffs:
        horizon = cutoff.replace(year=cutoff.year + 1)
        hidden = [
            ChainRow(r.symbol, r.series, r.trade_date, r.close, r.prev_close, None)
            if r.trade_date < cutoff
            else r
            for r in rows
            if r.trade_date < horizon
        ]
        report: ResolveReport = resolve(
            hidden, first_isin_session=cutoff, renames=renames, actions=actions, config=config
        )
        admitted = [r for r in report.resolutions if r.isin is not None]
        wrong = [r for r in admitted if truth[(r.symbol, r.series, r.trade_date)] != r.isin]
        out[cutoff.isoformat()] = {
            "rows_judged": len(report.resolutions),
            "rows_admitted": len(admitted),
            "rows_wrong": len(wrong),
            "wrong_sample": [
                (
                    r.symbol,
                    r.series,
                    r.trade_date.isoformat(),
                    r.isin,
                    truth[(r.symbol, r.series, r.trade_date)],
                )
                for r in wrong[:10]
            ],
            "chain_stops": report.stops(),
        }
        _LOG.info(
            "pre_isin.validated",
            cutoff=cutoff.isoformat(),
            admitted=len(admitted),
            wrong=len(wrong),
        )
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Offline: L0 and a read-only Postgres SELECT, never the network."""
    parser = argparse.ArgumentParser(prog="pre_isin_promote", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("promote", "validate"):
        cmd = sub.add_parser(name)
        cmd.add_argument(
            "--data-root",
            type=Path,
            default=None,
            help="lake whose L1 is written (default: settings)",
        )
        cmd.add_argument(
            "--l0-root", type=Path, default=None, help="lake whose L0 is read (default: settings)"
        )
        cmd.add_argument("--report", type=Path, default=None, help="write the JSON report here")
        cmd.add_argument(
            "--require-reissue-witness",
            action="store_true",
            help="admit a re-issued ISIN's rows only after a stored split/FV change dates it",
        )
    promote_cmd = sub.choices["promote"]
    promote_cmd.add_argument(
        "--from", dest="start", type=date.fromisoformat, default=date(2006, 1, 2)
    )
    promote_cmd.add_argument(
        "--to", dest="end", type=date.fromisoformat, default=eras.PRE_ISIN_ERA_LAST_SESSION
    )
    promote_cmd.add_argument(
        "--dry-run", action="store_true", help="resolve and report, write nothing"
    )
    sub.choices["validate"].add_argument(
        "--cutoffs",
        default="2013-01-01,2014-07-01,2016-01-01",
        help="comma-separated ISIN-era dates to anchor at",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    clock: Clock = SystemClock()
    l0 = L0Store(clock=clock, data_root=args.l0_root or settings.data_root)
    data_root = args.data_root if args.data_root is not None else settings.data_root
    config = ResolverConfig(require_reissue_witness=args.require_reissue_witness)
    renames = load_renames(l0)
    actions = _actions_from_db()

    result: dict[str, object]
    if args.command == "promote":
        if args.end >= eras.ISIN_ERA_START or args.start > args.end:
            print(
                f"error: range must lie in the pre-ISIN era (to {eras.PRE_ISIN_ERA_LAST_SESSION})",
                file=sys.stderr,
            )
            return 2
        pre = load_pre_isin_rows(l0, args.start, args.end)
        anchors = load_anchor_rows(l0, eras.ISIN_ERA_START)
        stats = promote(
            pre,
            anchors,
            renames=renames,
            actions=actions,
            data_root=data_root,
            config=config,
            dry_run=args.dry_run,
        )
        result = stats.as_json()
        rc = 1 if stats.failures else 0
    else:
        cutoffs = [date.fromisoformat(c) for c in args.cutoffs.split(",")]
        anchors = load_anchor_rows(
            l0, eras.ISIN_ERA_START, end=max(cutoffs).replace(year=max(cutoffs).year + 1)
        )
        result = validate(
            anchors.rows, cutoffs=cutoffs, renames=renames, actions=actions, config=config
        )
        rc = 1 if any(v["rows_wrong"] for v in result.values() if isinstance(v, dict)) else 0

    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
