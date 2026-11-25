"""Link a company's filing history to its live ISIN where L1 contiguity cannot see the link (M18.1).

NSE's filing feeds state a company's *original* ISIN, and `filing_rekey` moves those rows to the
live one by walking `isin_lineage`. Two shapes have no edge to walk, and 27 NIFTY 500 names had no
results date, announcement or shareholding row in the M17 dossiers because of them
(owner ruling 2026-11-23, HUMAN_DECISIONS D46):

* **A reissue before the NSE ISIN era.** The bhavcopy printed no ISIN before 2011-06-22
  (`nse.eras.ISIN_ERA_START`), so a face-value split in 2010 (KOTAKBANK INE237A01010 →
  INE237A01028) left no contiguity to derive an edge from. `pre_era_lineage.yaml` lists each such
  link; this command proves it and writes it as a MANUAL `isin_lineage` edge.
* **A filing under a debenture ISIN** (HUDCO files as its bond INE031A07873). Not a reissue, so
  never an edge: `filing_aliases.yaml` maps it for the filing resolver alone
  (`filing_isin.FilingAlias`). This command proves each entry and writes nothing for it.

    uv run python -m dataplatform.identity.link_filing_history            # dry run: verdicts, plan
    uv run python -m dataplatform.identity.link_filing_history --apply    # one transaction

**What a pre-era link must show (`judge_pre_era`), every input read back out of L0, L1 and the
store:** both are equity ISINs of one issuer code, the successor's serial one above the old one
(one reissue, one edge); every L1 board-meeting and announcement row stated under the old ISIN
names the entry's NSE symbol; the old ISIN is no security in the master, has no symbol window and
no edge; no L1 price row (any exchange) and no `EQUITY_L` capture ever carries it; the successor's
first NSE session in L1 is before the ISIN era and is the entry's effective date (the lineage's
own definition of an edge's date), the symbol's first ISIN-era session trades as the successor
only, and the chain from the successor reaches an ACTIVE survivor whose open NSE symbol is the
entry's; a stored pre-era split or consolidation on that chain, if any, is unique and falls at most
`MAX_SETTLEMENT_SESSIONS` before the effective date. No other edge may already lead into the
successor. A successor the master does not know is registered first, as DELISTED, only when it is
a chain's retired middle with L1 EQ bars (`lineage.plan_registrations`, as a rebuild would).

**A filing alias must show (`judge_alias`):** the equity is the issuer code's only ACTIVE equity
in the master and its open NSE symbol is the entry's; the filed ISIN is no security in the master,
has no window and no edge; every L1 board-meeting and announcement row stated under it names the
symbol, and so does every L1 price row carrying it (NSE printed the bond under the issuer's
symbol); the entry's effective date is the equity's first NSE session in L1.

**What it never does:** fetch, touch any ISIN the files do not name, write in the dry run (READ ONLY
transaction), or write anything unless *every* entry in both files passes: a name that cannot be
proved is taken out of the file, never applied around. A store already holding every edge is a
no-op; one holding a different edge for a named ISIN is refused.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Final

import duckdb
import yaml

from dataplatform.clock import Clock, SystemClock
from dataplatform.identity.filing_isin import (
    FILING_ALIASES_PATH,
    FilingAlias,
    load_filing_aliases,
)
from dataplatform.identity.lineage import (
    LINEAGE_BACKFILL,
    EqPresence,
    LineageEdge,
    ManualEdge,
    plan_registrations,
    read_eq_presence,
)
from dataplatform.identity.master import IdentityError, IdentityStore, Security
from dataplatform.ingest.nse.eras import ISIN_ERA_START
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection
from dataplatform.store.l2 import open_connection, register_raw_view
from dataplatform.store.paths import Layer, layer_root

__all__ = [
    "COMMAND",
    "MAX_SETTLEMENT_SESSIONS",
    "PRE_ERA_PATH",
    "AliasFacts",
    "LinkPlan",
    "PreEraFacts",
    "PreEraLink",
    "apply_plan",
    "gather_alias_facts",
    "gather_pre_era_facts",
    "judge_alias",
    "judge_pre_era",
    "load_pre_era_links",
    "main",
    "plan_links",
]

_LOG = get_logger(__name__)

COMMAND: Final = "link_filing_history"
#: The checked-in pre-era reissues (module docstring).
PRE_ERA_PATH: Final[Path] = Path(__file__).parent / "pre_era_lineage.yaml"
#: NSE sessions a stored split's ex-date may precede the successor's first L1 session by. The
#: pre-ISIN resolver refuses rows for a session or two after a split (`pre_isin`), and on the 21
#: corroborated links of 2026-11-25 the distance is 4 to 6.
MAX_SETTLEMENT_SESSIONS: Final = 10

_EQUITY_TYPE: Final = "01"
_ISSUER_LEN: Final = 7
_FILING_DATASETS: Final = ("board_meetings", "announcements")


class LinkRefusedError(IdentityError):
    """An entry failed its proof, or the store is not in a shape this command plans for."""


@dataclass(frozen=True, slots=True)
class PreEraLink:
    """One `pre_era_lineage.yaml` entry: a reissue before the ISIN era, written as a MANUAL edge."""

    symbol: str
    old_isin: str
    successor_isin: str
    effective_date: date
    provenance: str
    note: str


@dataclass(frozen=True, slots=True)
class PreEraFacts:
    """What L0, L1 and the store say about one pre-era link; `judge_pre_era` decides on these."""

    #: Symbols on the L1 board-meeting and announcement rows stated under the old ISIN.
    filing_symbols: Mapping[str, int]
    old_in_master: bool
    old_windows: int
    #: Every stored edge naming the old ISIN, as (predecessor, successor, effective, detected_by).
    old_edges: tuple[tuple[str, str, date, str], ...]
    #: Predecessors of stored edges into the successor.
    successor_predecessors: tuple[str, ...]
    old_price_rows: int
    old_in_equity_list: int
    successor_first_nse: date | None
    #: ISINs the symbol trades as on its first NSE session on or after the ISIN era's start, of
    #: every security type; the judge compares the equity ones.
    symbol_isins_at_era_start: tuple[str, ...]
    #: The chain from the successor to its survivor, successor first.
    chain: tuple[str, ...]
    survivor_status: str | None
    survivor_symbol: str | None
    #: Stored SPLIT/CONSOLIDATION ex-dates on the chain before the ISIN era.
    pre_era_splits: tuple[date, ...]
    #: NSE sessions in L1 from the unique pre-era split's ex-date to the effective date.
    split_to_effective_sessions: int | None
    successor_in_master: bool


@dataclass(frozen=True, slots=True)
class AliasFacts:
    """What L1 and the store say about one filing alias; `judge_alias` decides on these."""

    filing_symbols: Mapping[str, int]
    filed_in_master: bool
    filed_windows: int
    filed_edges: int
    equity_status: str | None
    equity_symbol: str | None
    #: Every ACTIVE equity ISIN of the issuer code in the master.
    issuer_active_equities: tuple[str, ...]
    #: Symbols on L1 price rows carrying the filed ISIN.
    filed_price_symbols: Mapping[str, int]
    equity_first_nse: date | None


def _is_equity(isin: str) -> bool:
    return isin.startswith("INE") and isin[_ISSUER_LEN : _ISSUER_LEN + 2] == _EQUITY_TYPE


def _serial(isin: str) -> int | None:
    digits = isin[_ISSUER_LEN + 2 : _ISSUER_LEN + 4]
    return int(digits) if digits.isdigit() else None


def load_pre_era_links(path: Path = PRE_ERA_PATH) -> tuple[PreEraLink, ...]:
    """The entries of ``path``; fails loud on a malformed file. Truth is `judge_pre_era`'s job."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise ValueError(f"{path}: expected a version-1 mapping")
    entries = doc.get("links") or []
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'links' must be a list")
    out: list[PreEraLink] = []
    for e in entries:
        if not isinstance(e, dict) or not isinstance(e.get("effective_date"), date):
            raise ValueError(f"{path}: every link is a mapping with a date effective_date")
        out.append(
            PreEraLink(
                symbol=str(e["symbol"]),
                old_isin=str(e["old_isin"]),
                successor_isin=str(e["successor_isin"]),
                effective_date=e["effective_date"],
                provenance=str(e["provenance"]),
                note=str(e.get("note", "")),
            )
        )
    for key in ("old_isin", "successor_isin", "symbol"):
        values = [getattr(link, key) for link in out]
        if len(set(values)) != len(values):
            raise ValueError(f"{path}: a {key} appears twice")
    return tuple(out)


# ── judging: pure functions of the facts ────────────────────────────────────────────────────────


def judge_pre_era(link: PreEraLink, facts: PreEraFacts) -> list[str]:
    """Every reason ``link`` is not proved by ``facts``; empty when it is (module docstring)."""
    old, new = link.old_isin, link.successor_isin
    out: list[str] = []
    if not (_is_equity(old) and _is_equity(new)):
        out.append("both ISINs must be equity shares (INE…01…)")
    if old[:_ISSUER_LEN] != new[:_ISSUER_LEN]:
        out.append("different issuer codes: another company, not a reissue")
    so, sn = _serial(old), _serial(new)
    if so is None or sn is None or sn != so + 1:
        out.append(f"successor serial {sn} is not old serial {so} + 1: not one reissue")
    if not facts.filing_symbols:
        out.append("no L1 filing is stated under the old ISIN")
    elif set(facts.filing_symbols) != {link.symbol}:
        out.append(
            f"filings under the old ISIN name {dict(facts.filing_symbols)}, not {link.symbol}"
        )
    if facts.old_in_master:
        out.append("the old ISIN is a security in the master")
    if facts.old_windows:
        out.append(f"the old ISIN has {facts.old_windows} symbol window(s)")
    planned = (old, new, link.effective_date, "MANUAL")
    if any(edge != planned for edge in facts.old_edges):
        out.append(f"another edge already names the old ISIN: {facts.old_edges}")
    if set(facts.successor_predecessors) - {old}:
        out.append(f"another edge already leads into the successor: {facts.successor_predecessors}")
    if facts.old_price_rows:
        out.append(f"{facts.old_price_rows} L1 price row(s) carry the old ISIN")
    if facts.old_in_equity_list:
        out.append(f"{facts.old_in_equity_list} EQUITY_L capture(s) list the old ISIN")
    first = facts.successor_first_nse
    if first is None:
        out.append("the successor has no NSE row in L1")
    else:
        if first >= ISIN_ERA_START:
            out.append(f"the successor first trades {first}, inside the ISIN era: not pre-era")
        if first != link.effective_date:
            out.append(
                f"effective {link.effective_date} is not the successor's first session {first}"
            )
    # Equity ISINs only: the symbol's own debentures trade under it too (BRITANNIA's bonus
    # debenture INE216A07045 on 2011-06-22), and a bond is no rival claim on the equity's history.
    equities = tuple(i for i in facts.symbol_isins_at_era_start if _is_equity(i))
    if equities != (new,):
        out.append(
            f"{link.symbol} trades as equity {equities} at the ISIN era's start, not {new} alone"
        )
    if facts.survivor_status != "ACTIVE":
        out.append(f"the chain's survivor {facts.chain[-1:]} is not ACTIVE in the master")
    if facts.survivor_symbol != link.symbol:
        out.append(f"the survivor's open NSE symbol is {facts.survivor_symbol}, not {link.symbol}")
    if len(facts.pre_era_splits) > 1:
        out.append(
            f"{len(facts.pre_era_splits)} pre-era splits on the chain: which one is ambiguous"
        )
    elif facts.pre_era_splits:
        gap = facts.split_to_effective_sessions
        if gap is None or not 0 <= gap <= MAX_SETTLEMENT_SESSIONS:
            out.append(
                f"the stored split ex {facts.pre_era_splits[0]} is {gap} sessions from the "
                f"effective date, outside 0..{MAX_SETTLEMENT_SESSIONS}"
            )
    return out


def judge_alias(alias: FilingAlias, facts: AliasFacts) -> list[str]:
    """Every reason ``alias`` is not proved by ``facts``; empty when it is (module docstring).

    `FilingAlias` itself already refused an alias across issuer codes or onto a non-equity ISIN.
    """
    out: list[str] = []
    if facts.equity_status != "ACTIVE":
        out.append(f"the equity {alias.isin} is not ACTIVE in the master")
    if facts.issuer_active_equities != (alias.isin,):
        out.append(
            f"the issuer code's ACTIVE equities are {facts.issuer_active_equities}, not "
            f"{alias.isin} alone"
        )
    if facts.equity_symbol != alias.symbol:
        out.append(f"the equity's open NSE symbol is {facts.equity_symbol}, not {alias.symbol}")
    if facts.filed_in_master:
        out.append("the filed ISIN is a security in the master")
    if facts.filed_windows:
        out.append(f"the filed ISIN has {facts.filed_windows} symbol window(s)")
    if facts.filed_edges:
        out.append(f"the filed ISIN is named by {facts.filed_edges} lineage edge(s)")
    if not facts.filing_symbols:
        out.append("no L1 filing is stated under the filed ISIN")
    elif set(facts.filing_symbols) != {alias.symbol}:
        out.append(f"filings under the filed ISIN name {dict(facts.filing_symbols)}")
    if not facts.filed_price_symbols:
        out.append("no L1 price row carries the filed ISIN: nothing ties it to the issuer's symbol")
    elif set(facts.filed_price_symbols) != {alias.symbol}:
        out.append(f"the filed ISIN traded as {dict(facts.filed_price_symbols)}")
    if facts.equity_first_nse != alias.effective_date:
        out.append(
            f"effective {alias.effective_date} is not the equity's first NSE session "
            f"{facts.equity_first_nse}"
        )
    return out


# ── gathering: L0, L1 and the store, read only ──────────────────────────────────────────────────


def _filing_symbols(
    con: duckdb.DuckDBPyConnection, isins: Sequence[str], data_root: Path | None
) -> dict[str, Counter[str]]:
    """Per stated ISIN, the symbols on its L1 board-meeting and announcement rows."""
    out: dict[str, Counter[str]] = {i: Counter() for i in isins}
    for ds in _FILING_DATASETS:
        files = sorted(
            str(p)
            for p in (layer_root(Layer.L1, data_root=data_root) / ds).glob("date=*/part.parquet")
        )
        if not files:
            continue
        rows = con.execute(
            "SELECT coalesce(isin_as_filed, isin) AS stated, symbol, count(*) "
            "FROM read_parquet(?, union_by_name = true) "
            "WHERE coalesce(isin_as_filed, isin) IN (SELECT unnest(?)) GROUP BY 1, 2",
            [files, list(isins)],
        ).fetchall()
        for stated, symbol, n in rows:
            out[str(stated)][str(symbol)] += int(n)
    return out


def _equity_list_hits(isins: Sequence[str], data_root: Path | None) -> Counter[str]:
    """How many L0 `EQUITY_L` captures list each ISIN (a plain text match on the CSV)."""
    hits: Counter[str] = Counter()
    root = layer_root(Layer.L0, data_root=data_root) / "nse_equity_list"
    for path in sorted(root.glob("*/*/EQUITY_L_*.csv")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for isin in isins:
            if isin in text:
                hits[isin] += 1
    return hits


class _Store:
    """The handful of read-only store queries the facts need."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self.master: dict[str, str] = {
            str(i): str(s)
            for i, s in conn.execute("SELECT isin, status FROM security_master").fetchall()
        }
        self.edges: tuple[tuple[str, str, date, str], ...] = tuple(
            (str(p), str(s), eff, str(by))
            for p, s, eff, by in conn.execute(
                "SELECT predecessor_isin, successor_isin, effective_date, detected_by "
                "FROM isin_lineage ORDER BY predecessor_isin"
            ).fetchall()
        )
        self.forward = {p: s for p, s, _, _ in self.edges}

    def windows(self, isin: str) -> int:
        row = self._conn.execute(
            "SELECT count(*) FROM symbol_history WHERE isin = %s", (isin,)
        ).fetchone()
        return int(row[0]) if row else 0

    def open_nse_symbol(self, isin: str) -> str | None:
        rows = self._conn.execute(
            "SELECT symbol FROM symbol_history WHERE isin = %s AND exchange = 'NSE' "
            "AND valid_to IS NULL",
            (isin,),
        ).fetchall()
        return str(rows[0][0]) if len(rows) == 1 else None

    def chain(self, isin: str) -> tuple[str, ...]:
        out = [isin]
        while (nxt := self.forward.get(out[-1])) is not None and nxt not in out:
            out.append(nxt)
        return tuple(out)

    def pre_era_splits(self, chain: Sequence[str]) -> tuple[date, ...]:
        rows = self._conn.execute(
            "SELECT DISTINCT ex_date FROM corporate_actions WHERE isin = ANY(%s) "
            "AND action_type IN ('SPLIT', 'CONSOLIDATION') AND ex_date < %s ORDER BY ex_date",
            (list(chain), ISIN_ERA_START),
        ).fetchall()
        return tuple(r[0] for r in rows)


def gather_pre_era_facts(
    conn: Connection,
    links: Sequence[PreEraLink],
    *,
    data_root: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, PreEraFacts]:
    """`PreEraFacts` per link's old ISIN, from L0, L1 and the store. Reads only."""
    if not links:
        return {}
    con = open_connection() if con is None else con
    view = register_raw_view(con, data_root=data_root)
    store = _Store(conn)
    olds = [link.old_isin for link in links]
    filings = _filing_symbols(con, olds, data_root)
    listed = _equity_list_hits(olds, data_root)
    out: dict[str, PreEraFacts] = {}
    for link in links:
        old, new = link.old_isin, link.successor_isin
        (old_rows,) = con.execute(
            f"SELECT count(*) FROM {view} WHERE isin = ?", [old]
        ).fetchone() or (0,)
        (first,) = con.execute(
            f"SELECT min(trade_date) FROM {view} WHERE isin = ? AND exchange = 'NSE'", [new]
        ).fetchone() or (None,)
        at_start = con.execute(
            f"SELECT DISTINCT isin FROM {view} WHERE exchange = 'NSE' AND symbol = ? AND "
            f"trade_date = (SELECT min(trade_date) FROM {view} WHERE exchange = 'NSE' "
            f"AND symbol = ? AND trade_date >= ?) ORDER BY isin",
            [link.symbol, link.symbol, ISIN_ERA_START],
        ).fetchall()
        chain = store.chain(new)
        splits = store.pre_era_splits(chain)
        gap: int | None = None
        if len(splits) == 1 and first is not None:
            (n,) = con.execute(
                f"SELECT count(DISTINCT trade_date) FROM {view} WHERE exchange = 'NSE' "
                f"AND trade_date >= ? AND trade_date < ?",
                [splits[0], first],
            ).fetchone() or (0,)
            gap = int(n) if splits[0] <= first else -1
        survivor = chain[-1]
        out[old] = PreEraFacts(
            filing_symbols=dict(filings.get(old, Counter())),
            old_in_master=old in store.master,
            old_windows=store.windows(old),
            old_edges=tuple(e for e in store.edges if old in (e[0], e[1])),
            successor_predecessors=tuple(p for p, s, _, _ in store.edges if s == new),
            old_price_rows=int(old_rows),
            old_in_equity_list=listed[old],
            successor_first_nse=first,
            symbol_isins_at_era_start=tuple(str(r[0]) for r in at_start),
            chain=chain,
            survivor_status=store.master.get(survivor),
            survivor_symbol=store.open_nse_symbol(survivor),
            pre_era_splits=splits,
            split_to_effective_sessions=gap,
            successor_in_master=new in store.master,
        )
    return out


def gather_alias_facts(
    conn: Connection,
    aliases: Sequence[FilingAlias],
    *,
    data_root: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, AliasFacts]:
    """`AliasFacts` per alias's filed ISIN, from L1 and the store. Reads only."""
    if not aliases:
        return {}
    con = open_connection() if con is None else con
    view = register_raw_view(con, data_root=data_root)
    store = _Store(conn)
    filings = _filing_symbols(con, [a.filed_isin for a in aliases], data_root)
    out: dict[str, AliasFacts] = {}
    for alias in aliases:
        filed = alias.filed_isin
        prices = con.execute(
            f"SELECT symbol, count(*) FROM {view} WHERE isin = ? GROUP BY 1", [filed]
        ).fetchall()
        (first,) = con.execute(
            f"SELECT min(trade_date) FROM {view} WHERE isin = ? AND exchange = 'NSE'",
            [alias.isin],
        ).fetchone() or (None,)
        issuer = alias.isin[:_ISSUER_LEN]
        out[filed] = AliasFacts(
            filing_symbols=dict(filings.get(filed, Counter())),
            filed_in_master=filed in store.master,
            filed_windows=store.windows(filed),
            filed_edges=sum(1 for e in store.edges if filed in (e[0], e[1])),
            equity_status=store.master.get(alias.isin),
            equity_symbol=store.open_nse_symbol(alias.isin),
            issuer_active_equities=tuple(
                sorted(
                    i
                    for i, status in store.master.items()
                    if i.startswith(issuer) and _is_equity(i) and status == "ACTIVE"
                )
            ),
            filed_price_symbols={str(s): int(n) for s, n in prices},
            equity_first_nse=first,
        )
    return out


# ── plan and apply ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LinkPlan:
    """What `apply_plan` writes: retired middles to register, then the MANUAL edges."""

    links: tuple[PreEraLink, ...]
    aliases: tuple[FilingAlias, ...]
    refusals: Mapping[str, tuple[str, ...]]
    register: tuple[Security, ...]
    edges: tuple[PreEraLink, ...]
    already_applied: tuple[PreEraLink, ...] = field(default=())

    @property
    def refused(self) -> bool:
        return any(self.refusals.values())

    def describe(self) -> list[str]:
        lines = [
            f"{COMMAND}: {len(self.links)} pre-era link(s), {len(self.aliases)} filing alias(es)"
        ]
        for link in self.links:
            why = self.refusals.get(link.old_isin, ())
            verdict = "REFUSED: " + "; ".join(why) if why else "proved"
            lines.append(
                f"  link  {link.symbol:<11} {link.old_isin} -> {link.successor_isin} "
                f"eff {link.effective_date}  {verdict}"
            )
        for alias in self.aliases:
            why = self.refusals.get(alias.filed_isin, ())
            verdict = "REFUSED: " + "; ".join(why) if why else "proved"
            lines.append(
                f"  alias {alias.symbol:<11} {alias.filed_isin} -> {alias.isin} "
                f"from {alias.effective_date}  {verdict}"
            )
        for sec in self.register:
            lines.append(
                f"  register {sec.isin} DELISTED ({sec.name}, {sec.first_seen_date} .. "
                f"{sec.last_seen_date}) as {LINEAGE_BACKFILL}"
            )
        for link in self.edges:
            lines.append(
                f"  insert isin_lineage {link.old_isin} -> {link.successor_isin} "
                f"{link.effective_date} MANUAL DERIVED gap 0 {link.symbol}"
            )
        if self.already_applied and not self.edges:
            lines.append(
                f"  already applied: {len(self.already_applied)} edge(s) stored, nothing to do"
            )
        return lines


def plan_links(
    conn: Connection,
    links: Sequence[PreEraLink],
    aliases: Sequence[FilingAlias],
    *,
    pre_era: Mapping[str, PreEraFacts],
    alias_facts: Mapping[str, AliasFacts],
    presence: Mapping[str, EqPresence],
) -> LinkPlan:
    """Judge every entry and plan the writes. Refuses nothing itself: `LinkPlan.refused` says."""
    refusals: dict[str, tuple[str, ...]] = {}
    for link in links:
        refusals[link.old_isin] = tuple(judge_pre_era(link, pre_era[link.old_isin]))
    for alias in aliases:
        refusals[alias.filed_isin] = tuple(judge_alias(alias, alias_facts[alias.filed_isin]))
    stored = {(p, s) for p, s, _ in _stored_edges(conn)}
    done = tuple(link for link in links if (link.old_isin, link.successor_isin) in stored)
    todo = tuple(link for link in links if link not in done)
    known = {str(r[0]) for r in conn.execute("SELECT isin FROM security_master").fetchall()}
    chain_links = [ManualEdge(p, s, eff, "") for p, s, eff in _stored_edges(conn)]
    reg = plan_registrations(
        [
            LineageEdge(
                link.old_isin, link.successor_isin, link.effective_date, 0, link.symbol, None
            )
            for link in todo
        ],
        known,
        presence,
        manual=chain_links,
    )
    for edge, reason in reg.skipped:
        refusals[edge.predecessor_isin] = (
            *refusals.get(edge.predecessor_isin, ()),
            f"successor {edge.successor_isin} is unknown and not registrable: {reason.value}",
        )
    return LinkPlan(
        links=tuple(links),
        aliases=tuple(aliases),
        refusals=refusals,
        register=reg.register,
        edges=todo,
        already_applied=done,
    )


def _stored_edges(conn: Connection) -> list[tuple[str, str, date]]:
    return [
        (str(p), str(s), eff)
        for p, s, eff in conn.execute(
            "SELECT predecessor_isin, successor_isin, effective_date FROM isin_lineage"
        ).fetchall()
    ]


def apply_plan(conn: Connection, plan: LinkPlan, *, clock: Clock) -> tuple[int, int]:
    """Register the middles and insert the edges on ``conn``; return (registered, inserted).

    Refuses the whole plan if any entry was refused. The caller owns the transaction and commits.
    """
    if plan.refused:
        bad = sorted(k for k, v in plan.refusals.items() if v)
        raise LinkRefusedError(f"{len(bad)} entr(y/ies) not proved, nothing written: {bad}")
    registered = IdentityStore(conn, clock=clock).register_inferred(
        plan.register, registered_by=LINEAGE_BACKFILL
    )
    if registered != len(plan.register):
        raise LinkRefusedError(f"registered {registered} of {len(plan.register)} middles")
    inserted = 0
    for link in plan.edges:
        rows = conn.execute(
            "INSERT INTO isin_lineage (predecessor_isin, successor_isin, effective_date, "
            "detected_by, confidence, gap_sessions, symbol_at_change, corroborating_action, "
            "computed_at) VALUES (%s, %s, %s, 'MANUAL', 'DERIVED', 0, %s, NULL, %s) "
            "ON CONFLICT DO NOTHING RETURNING predecessor_isin",
            (link.old_isin, link.successor_isin, link.effective_date, link.symbol, clock.now()),
        ).fetchall()
        if len(rows) != 1:
            raise LinkRefusedError(f"edge {link.old_isin} -> {link.successor_isin} not inserted")
        inserted += 1
        _LOG.info(
            "identity.link_filing_history.edge",
            old=link.old_isin,
            successor=link.successor_isin,
            effective=link.effective_date.isoformat(),
            symbol=link.symbol,
            provenance=link.provenance,
        )
    return registered, inserted


def _middles(links: Iterable[PreEraLink], known: set[str]) -> list[str]:
    return sorted({link.successor_isin for link in links} - known)


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m dataplatform.identity.link_filing_history [--apply]`; see the module docstring."""
    from dataplatform.config import get_settings
    from dataplatform.logging import configure_logging
    from dataplatform.store.db import connection

    parser = argparse.ArgumentParser(
        prog=f"identity.{COMMAND}", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--apply", action="store_true", help="execute and commit (default: dry run, read only)"
    )
    parser.add_argument("--links", type=Path, default=PRE_ERA_PATH)
    parser.add_argument("--aliases", type=Path, default=FILING_ALIASES_PATH)
    args = parser.parse_args(argv)
    configure_logging()
    settings = get_settings()
    clock: Clock = SystemClock()
    links = load_pre_era_links(args.links)
    aliases = load_filing_aliases(args.aliases)
    with connection(settings) as conn:
        if args.apply:
            # Held to commit: no rebuild or ingest can change these between the check and the write.
            conn.execute("LOCK TABLE security_master, isin_lineage IN SHARE ROW EXCLUSIVE MODE")
        else:
            conn.execute("SET TRANSACTION READ ONLY")
        try:
            con = open_connection()
            known = {str(r[0]) for r in conn.execute("SELECT isin FROM security_master").fetchall()}
            presence = read_eq_presence(
                _middles(links, known), con=con, data_root=settings.data_root
            )
            plan = plan_links(
                conn,
                links,
                aliases,
                pre_era=gather_pre_era_facts(conn, links, data_root=settings.data_root, con=con),
                alias_facts=gather_alias_facts(
                    conn, aliases, data_root=settings.data_root, con=con
                ),
                presence=presence,
            )
            print("\n".join(plan.describe()))
            if not args.apply:
                conn.rollback()
                print("dry run: nothing written (READ ONLY transaction). Re-run with --apply.")
                return 1 if plan.refused else 0
            registered, inserted = apply_plan(conn, plan, clock=clock)
        except IdentityError as error:
            conn.rollback()
            print(f"{COMMAND} refused: {error}", file=sys.stderr)
            return 2
        conn.commit()
    print(f"applied: {registered} middle(s) registered, {inserted} MANUAL edge(s) inserted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
