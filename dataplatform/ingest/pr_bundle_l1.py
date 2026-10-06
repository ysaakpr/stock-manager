"""L1 for the PR bundle: four datasets of facts the platform had no other source for.

`data/L0/nse_pr_bundle` holds one `PR<DDMMYY>.zip` per session from 2010-01-04, each with 13-25
member reports, and until this module only `ffix`, `mcap` and `Bc` (for fund lineage) were read —
`bh` in memory by the H2 backtest, nothing to L1. The 2026-10-06 inventory
(`ops/gates/pr-bundle-inventory-2026-10-06.md`) measured every member kind and found three that
carry facts nothing else in the lake does. This module promotes them, additively, to four new L1
datasets, and writes nothing else — never `prices_raw`, never L2, never Postgres:

| dataset | member | what it holds | key |
|---|---|---|---|
| `pr_band_hits`      | `bh` | securities that hit the upper/lower daily band | ISIN |
| `pr_security_marks` | `Pd` | `CORP_IND` ex-marker, NIFTY 50 flag, 52-week range | ISIN |
| `pr_index_eod`      | `Pd` | every NSE index's OHLC, 52-week range, India VIX | index |
| `pr_ca_broadcasts`  | `Bc` | the CA book as broadcast that session | ISIN |

and `pr_bundle_quarantine`, where every security row that could not be given an ISIN goes with an
enumerated reason (`QuarantineReason`). **A row is never dropped**: per session and per member,
`rows == written + quarantined`, and the build report sums both.

**Identity goes through the identity module and nowhere else** (invariant #2). For each session:

1. `identity.SessionIdentity` — the exchange's own statement of which ISIN traded as
   `(symbol, series)` *on that session*, built from that session's NSE bhavcopy: the L1
   `prices_raw` partition, else the L0 `nse_bhavcopy_legacy` payload for the date. Only from
   `ISIN_BHAVCOPY_START` (2011-06-22): before it the bhavcopy prints no ISIN, so there is no
   exchange statement to read, whatever ISIN a derived `prices_raw` row may carry. A
   `(symbol, series)` the file stated twice is refused by `SessionIdentity`, never picked.
2. Only where the session says nothing, `IdentityMaster.try_resolve_in_force(symbol, session)` —
   the as-of symbol windows moved along `isin_lineage`. A master ambiguity is quarantined as
   `ambiguous_master` (and logged; it is not written to the reconciliation queue from here).

Same-session first, because a symbol is only dangerous across time: NSE recycles them, and the
master's windows are series-blind. Within one exchange-day `(symbol, series)` is NSE's own key.

**Point in time.** Every row carries `publication_date`, the bundle's own date
(`PrBundle.publication_date`, derived from the members' names, never a clock). For `bh` and `Pd`
it equals the session; for `Bc` it is the broadcast date, which is the whole value of the member.
Bundles `PrBundle` refuses to date (four, all measured) are reported and skipped — never dated by
their filename.

**Idempotent per `(dataset, date)`.** Rows are sorted on a total key, money is quantised to four
places (a lossy quantise raises), and each partition is written whole via a staging rename, so a
rebuild from L0 is byte-identical. A member absent from a bundle removes that dataset's partition
for the date; a member present with no rows writes an empty partition, because "no band hits
that session" is a fact and "not built" is not.

Offline by construction: reads L0 and L1 `prices_raw`, reads the identity master from Postgres
(read-only) in the CLI, and never fetches.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from dataplatform.clock import FrozenClock
from dataplatform.identity.master import (
    AmbiguousSymbolError,
    Exchange,
    IdentityMaster,
    IdentityStore,
    InMemoryReconciliationQueue,
)
from dataplatform.identity.session import SessionIdentity
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse import bhavcopy
from dataplatform.ingest.nse.pr_bundle.bc import BcRow, parse_bc_bundle
from dataplatform.ingest.nse.pr_bundle.bh import BandHitRow, parse_bh_bundle
from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID, MemberKind, PrBundle
from dataplatform.ingest.nse.pr_bundle.pd import PdIndexRow, PdSecurityRow, parse_pd_bundle
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import assert_raw_only, enforce_schema

__all__ = [
    "BAND_HITS_DATASET",
    "BAND_HITS_SCHEMA",
    "CA_BROADCASTS_DATASET",
    "CA_BROADCASTS_SCHEMA",
    "INDEX_EOD_DATASET",
    "INDEX_EOD_SCHEMA",
    "INDEX_IDS",
    "ISIN_BHAVCOPY_START",
    "MASTER_SERIES",
    "PR_DATASETS",
    "QUARANTINE_DATASET",
    "QUARANTINE_SCHEMA",
    "SECURITY_MARKS_DATASET",
    "SECURITY_MARKS_SCHEMA",
    "BuildReport",
    "QuarantineReason",
    "ResolvedVia",
    "SessionBuild",
    "SessionResolver",
    "SessionStatementRow",
    "build_session",
    "index_id_for",
    "load_session_identity",
    "rebuild_from_l0",
    "write_session",
]

_LOG = get_logger(__name__)

BAND_HITS_DATASET: Final = "pr_band_hits"
SECURITY_MARKS_DATASET: Final = "pr_security_marks"
INDEX_EOD_DATASET: Final = "pr_index_eod"
CA_BROADCASTS_DATASET: Final = "pr_ca_broadcasts"
QUARANTINE_DATASET: Final = "pr_bundle_quarantine"

#: The bhavcopy source whose L0 payload stands in for `prices_raw` before L1 begins (2011-06-22).
_LEGACY_BHAVCOPY_SOURCE: Final = "nse_bhavcopy_legacy"

#: The first session whose NSE bhavcopy prints an ISIN. Before it the archive's file is
#: `SYMBOL,SERIES,…,TIMESTAMP` with no ISIN column (measured on every 2010-2011 payload in L0), so
#: any ISIN a `prices_raw` row carries for an earlier date was assigned by this platform's own
#: resolution — not the exchange's statement, and not something `SessionIdentity` may be built
#: from. On 2026-10-06 such partitions appeared mid-build (another task backfilling 2006-2011),
#: which is how this boundary was found; earlier sessions therefore resolve through the master
#: alone, labelled `identity_master`.
ISIN_BHAVCOPY_START: Final = date(2011, 6, 22)

_Q4: Final = Decimal("0.0001")


class QuarantineReason(StrEnum):
    """Why a security row has no ISIN. Every quarantined row carries exactly one."""

    SYMBOL_UNRESOLVED = "symbol_unresolved"
    """The session's bhavcopy states no ISIN for `(symbol, series)`, and the master knows none."""
    NO_SESSION_STATEMENT = "no_session_statement"
    """No bhavcopy for the session in L1 or L0, and the master knows no ISIN for the symbol."""
    AMBIGUOUS_MASTER = "ambiguous_master"
    """The session said nothing and the master holds two ISINs for the symbol on that date."""


class ResolvedVia(StrEnum):
    """Which identity source placed a row on its ISIN."""

    SESSION = "session_bhavcopy"
    MASTER = "identity_master"


def _dec(precision: int = 20) -> pa.DataType:
    return pa.decimal128(precision, 4)


BAND_HITS_SCHEMA: Final = pa.schema(
    [
        pa.field("session", pa.date32(), nullable=False),
        pa.field("publication_date", pa.date32(), nullable=False),
        pa.field("isin", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("series", pa.string(), nullable=False),
        pa.field("security_name", pa.string(), nullable=False),
        pa.field("side", pa.string(), nullable=False),
        pa.field("resolved_via", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=False),
    ]
)

SECURITY_MARKS_SCHEMA: Final = pa.schema(
    [
        pa.field("session", pa.date32(), nullable=False),
        pa.field("publication_date", pa.date32(), nullable=False),
        pa.field("isin", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("series", pa.string(), nullable=False),
        pa.field("security_name", pa.string(), nullable=False),
        pa.field("mkt", pa.string(), nullable=False),
        pa.field("section", pa.string(), nullable=True),
        pa.field("nifty50_flag", pa.bool_(), nullable=True),
        pa.field("corp_ind", pa.string(), nullable=True),
        pa.field("prev_close", _dec(), nullable=True),
        pa.field("close", _dec(), nullable=False),
        pa.field("trades", pa.int64(), nullable=True),
        pa.field("hi_52wk_published", _dec(), nullable=True),
        pa.field("lo_52wk_published", _dec(), nullable=True),
        pa.field("resolved_via", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=False),
    ]
)

INDEX_EOD_SCHEMA: Final = pa.schema(
    [
        pa.field("session", pa.date32(), nullable=False),
        pa.field("publication_date", pa.date32(), nullable=False),
        pa.field("index_id", pa.string(), nullable=False),
        pa.field("index_name", pa.string(), nullable=False),
        pa.field("prev_close", _dec(), nullable=True),
        pa.field("open", _dec(), nullable=True),
        pa.field("high", _dec(), nullable=True),
        pa.field("low", _dec(), nullable=True),
        pa.field("close", _dec(), nullable=False),
        pa.field("traded_value", _dec(28), nullable=True),
        pa.field("traded_qty", pa.int64(), nullable=True),
        pa.field("trades", pa.int64(), nullable=True),
        pa.field("hi_52wk_published", _dec(), nullable=True),
        pa.field("lo_52wk_published", _dec(), nullable=True),
        pa.field("l0_key", pa.string(), nullable=False),
    ]
)

CA_BROADCASTS_SCHEMA: Final = pa.schema(
    [
        pa.field("knowable_date", pa.date32(), nullable=False),
        pa.field("isin", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("series", pa.string(), nullable=False),
        pa.field("security_name", pa.string(), nullable=False),
        pa.field("purpose", pa.string(), nullable=False),
        pa.field("record_date", pa.date32(), nullable=True),
        pa.field("book_closure_start", pa.date32(), nullable=True),
        pa.field("book_closure_end", pa.date32(), nullable=True),
        pa.field("ex_date", pa.date32(), nullable=True),
        pa.field("no_delivery_start", pa.date32(), nullable=True),
        pa.field("no_delivery_end", pa.date32(), nullable=True),
        pa.field("resolved_via", pa.string(), nullable=False),
        pa.field("l0_key", pa.string(), nullable=False),
    ]
)

QUARANTINE_SCHEMA: Final = pa.schema(
    [
        pa.field("session", pa.date32(), nullable=False),
        pa.field("member", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("series", pa.string(), nullable=False),
        pa.field("security_name", pa.string(), nullable=False),
        pa.field("reason", pa.string(), nullable=False),
        pa.field("detail", pa.string(), nullable=True),
        pa.field("l0_key", pa.string(), nullable=False),
    ]
)

#: Dataset → schema, in write order. Every schema is checked raw-only (invariant #3) at import.
PR_DATASETS: Final[Mapping[str, pa.Schema]] = {
    BAND_HITS_DATASET: BAND_HITS_SCHEMA,
    SECURITY_MARKS_DATASET: SECURITY_MARKS_SCHEMA,
    INDEX_EOD_DATASET: INDEX_EOD_SCHEMA,
    CA_BROADCASTS_DATASET: CA_BROADCASTS_SCHEMA,
    QUARANTINE_DATASET: QUARANTINE_SCHEMA,
}
for _schema in PR_DATASETS.values():
    assert_raw_only(_schema)

#: Which member feeds which dataset — the partition is removed for a date whose bundle lacks it.
_MEMBER_OF: Final[Mapping[str, MemberKind]] = {
    BAND_HITS_DATASET: MemberKind.BH,
    SECURITY_MARKS_DATASET: MemberKind.PD,
    INDEX_EOD_DATASET: MemberKind.PD,
    CA_BROADCASTS_DATASET: MemberKind.BC,
}

#: Stable ids for the indices NSE renamed, so a series survives the 2013-03-04 (`S&P CNX` →
#: `CNX`) and 2015-11-09 (`CNX` → `Nifty`) renames. Each pair was checked in the inventory: the
#: first session under the new name prints the last session's close under the old one as its
#: `PREV_CL_PR`. Every other index's id is its normalised name — no guessed lineage.
INDEX_IDS: Final[Mapping[str, str]] = {
    "S&P CNX NIFTY": "NIFTY 50",
    "CNX NIFTY": "NIFTY 50",
    "NIFTY 50": "NIFTY 50",
    "CNX NIFTY JUNIOR": "NIFTY NEXT 50",
    "NIFTY NEXT 50": "NIFTY NEXT 50",
    "BANK NIFTY": "NIFTY BANK",
    "NIFTY BANK": "NIFTY BANK",
    "CNX IT": "NIFTY IT",
    "NIFTY IT": "NIFTY IT",
    "CNX 100": "NIFTY 100",
    "NIFTY 100": "NIFTY 100",
    "CNX 200": "NIFTY 200",
    "NIFTY 200": "NIFTY 200",
    "S&P CNX 500": "NIFTY 500",
    "CNX 500": "NIFTY 500",
    "NIFTY 500": "NIFTY 500",
    "INDIA VIX": "INDIA VIX",
}


def index_id_for(index_name: str) -> str:
    """The stable id of a published index name: the rename lineage above, else the name itself
    with whitespace collapsed — **case kept**.

    Case is not folded for an unmapped name because NSE has reused a name in another case for a
    different series: `Nifty Midcap 100` (2015-11-09..2016-03-31) last closed at 12,752.60, and
    `NIFTY MIDCAP 100` (from 2018-04-02) first printed a previous close of 18,757.00. Folding them
    would splice two indices into one series.
    """
    collapsed = " ".join(index_name.split())
    return INDEX_IDS.get(collapsed.upper(), collapsed)


# ── identity ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionStatementRow:
    """One `(isin, symbol, series, trade_date)` the session's bhavcopy stated."""

    isin: str
    symbol: str
    series: str
    trade_date: date


def load_session_identity(
    session: date, *, l0: L0Store, data_root: Path | None = None
) -> SessionIdentity | None:
    """The session's NSE bhavcopy statement, from L1 `prices_raw` or else the L0 legacy payload.

    `None` when neither holds the session, and always `None` before `ISIN_BHAVCOPY_START`.
    What it never does: read another session's file, or treat an ISIN this platform derived as
    one the exchange stated.
    """
    if session < ISIN_BHAVCOPY_START:
        return None
    path = l1_partition_path("prices_raw", session, data_root=data_root)
    statements: list[SessionStatementRow] = []
    if path.exists():
        table = pq.read_table(
            path,
            columns=["isin", "symbol", "series", "trade_date", "exchange"],
            filters=[("exchange", "=", Exchange.NSE.value)],
        )
        statements = [
            SessionStatementRow(
                isin=str(r["isin"]),
                symbol=str(r["symbol"]),
                series=str(r["series"]),
                trade_date=r["trade_date"],
            )
            for r in table.to_pylist()
            if r["trade_date"] == session
        ]
    if not statements:
        for ref in l0.iter_refs(_LEGACY_BHAVCOPY_SOURCE, start=session, end=session):
            try:
                parsed = bhavcopy.parse_l0(l0, ref)
            except ParseError as exc:
                _LOG.warning(
                    "pr_bundle_l1.session_bhavcopy_unreadable",
                    source=_LEGACY_BHAVCOPY_SOURCE,
                    date=session.isoformat(),
                    error=str(exc),
                    state="SKIPPED",
                )
                continue
            statements.extend(
                SessionStatementRow(
                    isin=row.isin, symbol=row.symbol, series=row.series, trade_date=row.trade_date
                )
                for row in parsed
                if row.trade_date == session
            )
    if not statements:
        return None
    return SessionIdentity.from_statements(statements, exchange=Exchange.NSE, trade_date=session)


#: Series the master may be asked about. Its windows are series-blind and built from the equity
#: list, so for a symbol it answers the equity ISIN whatever the series — right for EQ/BE/BZ and
#: the SME SM/ST, wrong for the same issuer's debenture series (IFCI `NE`, catalogue §A7). A
#: debt, gold-bond, MF or ETF row is therefore placed only by its own session's statement.
MASTER_SERIES: Final[frozenset[str]] = frozenset({"EQ", "BE", "BZ", "SM", "ST"})


@dataclass(slots=True)
class SessionResolver:
    """`(symbol, series)` → ISIN for one session: the session's statement, then the master.

    The master is consulted only where the statement is silent, and only for `MASTER_SERIES`.
    """

    session: date
    statement: SessionIdentity | None
    master: IdentityMaster | None

    def resolve(
        self, symbol: str, series: str
    ) -> tuple[str, ResolvedVia] | tuple[None, QuarantineReason]:
        """The ISIN and how it was found, or `None` and the quarantine reason. Never a guess."""
        if self.statement is not None:
            stated = self.statement.try_resolve(symbol, series, self.session, exchange=Exchange.NSE)
            if stated is not None:
                return stated, ResolvedVia.SESSION
        if self.master is not None and series.strip().upper() in MASTER_SERIES:
            try:
                isin = self.master.try_resolve_in_force(symbol, self.session)
            except AmbiguousSymbolError:
                return None, QuarantineReason.AMBIGUOUS_MASTER
            if isin is not None:
                return isin, ResolvedVia.MASTER
        if self.statement is None:
            return None, QuarantineReason.NO_SESSION_STATEMENT
        return None, QuarantineReason.SYMBOL_UNRESOLVED


# ── one session ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class MemberCount:
    """Per member, per session: `rows == written + quarantined`, and how rows were placed."""

    rows: int = 0
    written: int = 0
    quarantined: int = 0
    via: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class SessionBuild:
    """Everything one bundle contributes to L1, before anything touches disk."""

    session: date
    l0_key: str
    records: dict[str, list[dict[str, object]]]
    present: set[str]
    counts: dict[str, MemberCount]
    failures: list[tuple[str, str]]


def build_session(bundle: PrBundle, *, l0_key: str, resolver: SessionResolver) -> SessionBuild:
    """Parse `bh`, `Pd` and `Bc` from one opened bundle and resolve every security row.

    What it does: one record per published row, to its dataset or to the quarantine. A member
    that fails to parse is recorded in `failures` (and logged FAILED) and contributes nothing,
    while the other members of the bundle still build. What it never does: drop a row, resolve
    across sessions, or date a row by anything but the bundle.
    """
    session = bundle.publication_date
    if resolver.session != session:
        raise ValueError(
            f"resolver is for {resolver.session.isoformat()}, bundle is {session.isoformat()}"
        )
    build = SessionBuild(
        session=session,
        l0_key=l0_key,
        records={name: [] for name in PR_DATASETS},
        present=set(),
        counts={
            "bh": MemberCount(),
            "pd": MemberCount(),
            "pd_index": MemberCount(),
            "bc": MemberCount(),
        },
        failures=[],
    )

    def quarantine(
        member: str, symbol: str, series: str, name: str, reason: str, detail: str | None
    ) -> None:
        build.records[QUARANTINE_DATASET].append(
            {
                "session": session,
                "member": member,
                "symbol": symbol,
                "series": series,
                "security_name": name,
                "reason": reason,
                "detail": detail,
                "l0_key": l0_key,
            }
        )

    if bundle.has(MemberKind.BH):
        try:
            hits = parse_bh_bundle(bundle, l0_key=l0_key).rows
        except ParseError as exc:
            _fail(build, "bh", exc)
        else:
            build.present.add(BAND_HITS_DATASET)
            _band_hits(build, hits, resolver, quarantine)

    if bundle.has(MemberKind.PD):
        try:
            pd_file = parse_pd_bundle(bundle)
        except ParseError as exc:
            _fail(build, "pd", exc)
        else:
            build.present.update({SECURITY_MARKS_DATASET, INDEX_EOD_DATASET})
            _index_rows(build, pd_file.index_rows)
            _security_marks(build, pd_file.security_rows, resolver, quarantine)

    if bundle.has(MemberKind.BC):
        try:
            actions = parse_bc_bundle(bundle, l0_key=l0_key)
        except ParseError as exc:
            _fail(build, "bc", exc)
        else:
            build.present.add(CA_BROADCASTS_DATASET)
            _broadcasts(build, actions, resolver, quarantine)

    if build.records[QUARANTINE_DATASET]:
        build.present.add(QUARANTINE_DATASET)
    return build


class _Quarantine(Protocol):
    def __call__(
        self, member: str, symbol: str, series: str, name: str, reason: str, detail: str | None
    ) -> None: ...


def _fail(build: SessionBuild, member: str, exc: ParseError) -> None:
    build.failures.append((member, str(exc)))
    _LOG.error(
        "pr_bundle_l1.member_failed",
        source=PR_BUNDLE_SOURCE_ID,
        date=build.session.isoformat(),
        member=member,
        error=str(exc),
        state="FAILED",
    )


def _band_hits(
    build: SessionBuild,
    hits: Sequence[BandHitRow],
    resolver: SessionResolver,
    quarantine: _Quarantine,
) -> None:
    count = build.counts["bh"]
    for hit in hits:
        count.rows += 1
        isin, how = resolver.resolve(hit.symbol, hit.series)
        if isin is None:
            count.quarantined += 1
            count.reasons[how] += 1
            quarantine("bh", hit.symbol, hit.series, hit.security_name, how, hit.side.value)
            continue
        count.written += 1
        count.via[how] += 1
        build.records[BAND_HITS_DATASET].append(
            {
                "session": hit.session,
                "publication_date": hit.publication_date,
                "isin": isin,
                "symbol": hit.symbol,
                "series": hit.series,
                "security_name": hit.security_name,
                "side": hit.side.value,
                "resolved_via": how.value,
                "l0_key": build.l0_key,
            }
        )


def _index_rows(build: SessionBuild, rows: Sequence[PdIndexRow]) -> None:
    count = build.counts["pd_index"]
    for row in rows:
        count.rows += 1
        count.written += 1
        build.records[INDEX_EOD_DATASET].append(
            {
                "session": row.session,
                "publication_date": build.session,
                "index_id": index_id_for(row.index_name),
                "index_name": row.index_name,
                "prev_close": _q(row.prev_close),
                "open": _q(row.open),
                "high": _q(row.high),
                "low": _q(row.low),
                "close": _q(row.close),
                "traded_value": _q(row.traded_value),
                "traded_qty": row.traded_qty,
                "trades": row.trades,
                "hi_52wk_published": _q(row.hi_52wk),
                "lo_52wk_published": _q(row.lo_52wk),
                "l0_key": build.l0_key,
            }
        )


def _security_marks(
    build: SessionBuild,
    rows: Sequence[PdSecurityRow],
    resolver: SessionResolver,
    quarantine: _Quarantine,
) -> None:
    count = build.counts["pd"]
    for row in rows:
        count.rows += 1
        isin, how = resolver.resolve(row.symbol, row.series)
        if isin is None:
            count.quarantined += 1
            count.reasons[how] += 1
            quarantine("pd", row.symbol, row.series, row.security_name, how, row.corp_ind)
            continue
        count.written += 1
        count.via[how] += 1
        build.records[SECURITY_MARKS_DATASET].append(
            {
                "session": row.session,
                "publication_date": build.session,
                "isin": isin,
                "symbol": row.symbol,
                "series": row.series,
                "security_name": row.security_name,
                "mkt": row.mkt,
                "section": row.section,
                "nifty50_flag": row.nifty50_flag,
                "corp_ind": row.corp_ind,
                "prev_close": _q(row.prev_close),
                "close": _q(row.close),
                "trades": row.trades,
                "hi_52wk_published": _q(row.hi_52wk),
                "lo_52wk_published": _q(row.lo_52wk),
                "resolved_via": how.value,
                "l0_key": build.l0_key,
            }
        )


def _broadcasts(
    build: SessionBuild,
    actions: Sequence[BcRow],
    resolver: SessionResolver,
    quarantine: _Quarantine,
) -> None:
    count = build.counts["bc"]
    for action in actions:
        count.rows += 1
        isin, how = resolver.resolve(action.symbol, action.series)
        if isin is None:
            count.quarantined += 1
            count.reasons[how] += 1
            ex = action.ex_date.isoformat() if action.ex_date else ""
            quarantine(
                "bc",
                action.symbol,
                action.series,
                action.security_name,
                how,
                f"{ex}|{action.purpose}",
            )
            continue
        count.written += 1
        count.via[how] += 1
        build.records[CA_BROADCASTS_DATASET].append(
            {
                "knowable_date": action.knowable_date,
                "isin": isin,
                "symbol": action.symbol,
                "series": action.series,
                "security_name": action.security_name,
                "purpose": action.purpose,
                "record_date": action.record_date,
                "book_closure_start": action.book_closure_start,
                "book_closure_end": action.book_closure_end,
                "ex_date": action.ex_date,
                "no_delivery_start": action.no_delivery_start,
                "no_delivery_end": action.no_delivery_end,
                "resolved_via": how.value,
                "l0_key": build.l0_key,
            }
        )


def _q(value: Decimal | None) -> Decimal | None:
    """Quantise to the datasets' four places; a value that would lose digits raises."""
    if value is None:
        return None
    quantised = value.quantize(_Q4, rounding=ROUND_HALF_UP)
    if quantised != value:
        raise ValueError(f"{value} does not fit four decimal places without loss")
    return quantised


# ── disk ─────────────────────────────────────────────────────────────────────────────────────


def write_session(build: SessionBuild, *, data_root: Path | None = None) -> dict[str, int]:
    """Write every dataset partition this session produced; remove those its bundle lacks.

    Returns dataset → rows written. Deterministic: sorted on every column, fixed decimal scale,
    whole-file staging rename.
    """
    written: dict[str, int] = {}
    for dataset, schema in PR_DATASETS.items():
        path = l1_partition_path(dataset, build.session, data_root=data_root)
        if dataset not in build.present:
            if path.exists():
                path.unlink()
                _LOG.info(
                    "pr_bundle_l1.partition_removed",
                    dataset=dataset,
                    date=build.session.isoformat(),
                    state="REMOVED",
                )
            continue
        records = sorted(build.records[dataset], key=_sort_key)
        table = pa.Table.from_pylist(records, schema=schema)
        enforce_schema(table, schema, dataset=dataset)
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(f".{path.name}.partial")
        pq.write_table(table, staging, compression="snappy", version="2.6")
        staging.replace(path)
        written[dataset] = len(records)
    return written


def _sort_key(record: Mapping[str, object]) -> tuple[str, ...]:
    return tuple("" if value is None else str(value) for value in record.values())


# ── the whole corpus ─────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class YearCount:
    """One member's year: rows, written, quarantined by reason, placed by source."""

    rows: int = 0
    written: int = 0
    quarantined: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    via: Counter[str] = field(default_factory=Counter)
    eq_rows: int = 0
    eq_written: int = 0


@dataclass(slots=True)
class BuildReport:
    """What a rebuild did, per member and year — the numbers the gate report quotes."""

    bundles: int = 0
    built: int = 0
    undated: list[tuple[str, str]] = field(default_factory=list)
    sessions_without_statement: list[str] = field(default_factory=list)
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    by_member: dict[str, dict[int, YearCount]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(YearCount))
    )

    def to_json(self) -> dict[str, object]:
        return {
            "bundles": self.bundles,
            "built": self.built,
            "undated": self.undated,
            "sessions_without_statement": self.sessions_without_statement,
            "failures": self.failures,
            "by_member": {
                member: {
                    str(year): {
                        "rows": c.rows,
                        "written": c.written,
                        "quarantined": c.quarantined,
                        "reasons": dict(c.reasons),
                        "via": dict(c.via),
                        "eq_rows": c.eq_rows,
                        "eq_written": c.eq_written,
                    }
                    for year, c in sorted(years.items())
                }
                for member, years in sorted(self.by_member.items())
            },
        }


def rebuild_from_l0(
    *,
    start: date,
    end: date,
    l0: L0Store,
    master: IdentityMaster | None,
    data_root: Path | None = None,
) -> BuildReport:
    """Rebuild every PR-bundle L1 dataset for the bundles L0 holds in `[start, end]`.

    Each payload is re-hashed on read (`L0Store.get`, invariant #1). A bundle `PrBundle` will not
    date is reported in `undated` and skipped. Single-process and sequential by design: it is a
    one-off backfill that must not compete with a running campaign for the box.
    """
    report = BuildReport()
    for ref in l0.iter_refs(PR_BUNDLE_SOURCE_ID, start=start, end=end):
        report.bundles += 1
        try:
            bundle = PrBundle(l0.get(ref), filename=ref.filename)
        except ParseError as exc:
            report.undated.append((ref.logical_date.isoformat(), str(exc)[:240]))
            _LOG.warning(
                "pr_bundle_l1.bundle_undated",
                source=PR_BUNDLE_SOURCE_ID,
                date=ref.logical_date.isoformat(),
                filename=ref.filename,
                error=str(exc),
                state="SKIPPED",
            )
            continue
        with bundle:
            session = bundle.publication_date
            statement = load_session_identity(session, l0=l0, data_root=data_root)
            if statement is None:
                report.sessions_without_statement.append(session.isoformat())
            resolver = SessionResolver(session=session, statement=statement, master=master)
            build = build_session(bundle, l0_key=ref.key, resolver=resolver)
        write_session(build, data_root=data_root)
        report.built += 1
        _tally(report, build)
        _LOG.info(
            "pr_bundle_l1.session_built",
            source=PR_BUNDLE_SOURCE_ID,
            date=session.isoformat(),
            **{f"{m}_rows": c.rows for m, c in build.counts.items()},
            **{f"{m}_quarantined": c.quarantined for m, c in build.counts.items()},
            failures=len(build.failures),
            state="WRITTEN",
        )
    return report


def _tally(report: BuildReport, build: SessionBuild) -> None:
    year = build.session.year
    for member, count in build.counts.items():
        if count.rows == 0 and member not in ("bh", "bc"):
            continue
        tally = report.by_member[member][year]
        tally.rows += count.rows
        tally.written += count.written
        tally.quarantined += count.quarantined
        tally.reasons.update(count.reasons)
        tally.via.update(count.via)
    for dataset, member in (
        (BAND_HITS_DATASET, "bh"),
        (SECURITY_MARKS_DATASET, "pd"),
        (CA_BROADCASTS_DATASET, "bc"),
    ):
        tally = report.by_member[member][year]
        tally.eq_written += sum(1 for r in build.records[dataset] if r["series"] == "EQ")
        tally.eq_rows += sum(1 for r in build.records[dataset] if r["series"] == "EQ") + sum(
            1
            for r in build.records[QUARANTINE_DATASET]
            if r["member"] == member and r["series"] == "EQ"
        )
    for member, message in build.failures:
        report.failures.append((build.session.isoformat(), member, message[:300]))


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m dataplatform.ingest.pr_bundle_l1 --data-root … --from … --to … --report …`."""
    from dataplatform.config import get_settings
    from dataplatform.store.db import connection

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--no-master",
        action="store_true",
        help="resolve through the session statement only (tests, dry runs)",
    )
    args = parser.parse_args(argv)

    clock = FrozenClock(args.end)
    l0 = L0Store(clock=clock, data_root=args.data_root)
    print(f"pr_bundle_l1: L0 root {l0.root}", file=sys.stderr)
    master: IdentityMaster | None = None
    if not args.no_master:
        with connection(get_settings()) as conn:
            # An in-memory queue: this backfill reads identity and must not write to it.
            master = IdentityStore(conn, clock=clock).load_master(
                queue=InMemoryReconciliationQueue()
            )
    report = rebuild_from_l0(
        start=args.start, end=args.end, l0=l0, master=master, data_root=args.data_root
    )
    args.report.write_text(json.dumps(report.to_json(), indent=1, sort_keys=True))
    print(
        f"pr_bundle_l1: {report.built} of {report.bundles} bundles built, "
        f"{len(report.undated)} undated, {len(report.failures)} member failures",
        file=sys.stderr,
    )
    return 0 if not report.failures else 3


if __name__ == "__main__":
    sys.exit(main())
