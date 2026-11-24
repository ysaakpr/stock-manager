"""Repair an ISIN reissue the weekly identity refresh stored as ambiguous, after re-reading L0.

The 07:00 IST `identity_refresh` places a reissue's boundary itself when it has evidence: an
`isin_lineage` edge, or the `EQUITY_L` series agreeing with the bhavcopy (`L0EquityListSeries`).
When neither answers (`identity.reissue.no_boundary`) it stores the new ISIN's windows from the
original listing date beside the still-open old one and queues the overlap. Every resolve of the
symbol raises from then on, and nothing the ingest does deletes those rows. The runbook's remedy is
a MANUAL edge; the ingest leaves a stored listing-date window alone, so the edge alone changes
nothing. `repair_avenuesai` (PR #142) did the whole repair once, for one hard-coded pair. This is
the same repair for any pair, given only the two ISINs, the switch session and the snapshot:

    uv run python -m dataplatform.identity.repair_reissue \\
        --old INE00HZ01011 --new INE00HZ01029 --effective 2026-11-16 \\
        --snapshot-date 2026-11-21                                   # dry run: print the plan
    uv run python -m dataplatform.identity.repair_reissue ... --apply   # one transaction

First case: BN Agrochem (NSE BNAGROCHEM), INE00HZ01011 → INE00HZ01029 on a ₹10 → ₹1 split ex
2026-11-16. The last old capture was the Saturday 2026-11-14 one, so the series' bhavcopy check
found no session between it and Monday's switch and refused (`session_disagrees`,
`before_isins=None`), and the 2026-11-21 refresh stored the listing-date window. That check now
reaches back past a non-session capture to the last session's bhavcopy (2026-11-24), so this shape
resolves in the refresh itself; the command remains for a reissue the series cannot evidence.

What it requires of L0 before it plans anything (`read_switch`; every payload re-checksummed):

* both ISINs share the issuer code (`isin[:7]`): a different one is another company taking the
  symbol, not a reissue;
* the bhavcopy of `--effective` trades exactly one symbol as the new ISIN, and that symbol as the
  new ISIN only;
* no stored session before `--effective` trades the new ISIN, none from it trades the old one,
  and the symbol never trades as a third ISIN. Both ISINs in one session is refused;
* the last session before `--effective` that trades the symbol shows the old ISIN only, with at
  most `MAX_GAP_SESSIONS` stored sessions between them (a suspension) in which it does not trade;
* the `EQUITY_L` captures: the one dated that last old session lists the old ISIN, the ones up to
  the switch list it or omit the symbol, and every one from `--effective` to `--snapshot-date`
  (both present) lists the new ISIN. A capture that lists the new ISIN early is refused;
* a face-value change between those two captures is explained by an L0 `nse_corp_actions` record
  for the old ISIN with that ex-date and those face values, and no record contradicts it.

Then it reads the store (`derive_reissue`) and plans through `repair_avenuesai.plan_repair`, so
the guarantees are the same ones, enforced by the same code: one transaction; a MANUAL
`isin_lineage` edge with its evidence in every resolution; delete only the new ISIN's windows from
before the switch, and only if the `--snapshot-date` ingest wrote them; re-derive the snapshot
from L0 with the edge and require one close and one insert, on these two ISINs, to be the only
change to the whole master; resolve the pair's open reconciliation rows (the INGEST rows must be
exactly the stale windows'); refuse any other store shape. A repaired store is a no-op.

What it never does: fetch (every input is read back out of L0), touch a BSE window (BSE is
parked), touch an ISIN other than the two given, or write in the dry run, which runs in a READ
ONLY transaction.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.corpactions.parse_terms import parse_purpose
from dataplatform.corpactions.taxonomy import FaceValueTerms
from dataplatform.identity.ingest import (
    NSE_EQUITY_LIST_SOURCE,
    EquityListRow,
    ReissueEvidence,
    equity_list_filename,
    parse_equity_list,
)
from dataplatform.identity.master import Exchange, IdentityError
from dataplatform.identity.repair_avenuesai import (
    EvidenceLine,
    ExpectedConflict,
    Reissue,
    RepairCounts,
    RepairPlan,
    RepairRefusedError,
    StaleWindow,
    apply_repair,
    plan_repair,
)
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "COMMAND",
    "LOOKBACK_DAYS",
    "MAX_GAP_SESSIONS",
    "L0Switch",
    "apply_reissue_repair",
    "derive_reissue",
    "main",
    "plan_reissue_repair",
    "read_switch",
    "resolution_reason",
]

_log = get_logger(__name__)

COMMAND: Final = "repair_reissue"
_EXCHANGE: Final = Exchange.NSE
_UDIFF_SOURCE: Final = "nse_bhavcopy_udiff"
_CA_SOURCE: Final = "nse_corp_actions"

#: Calendar days before the switch scanned for the last old session.
LOOKBACK_DAYS: Final = 45
#: Stored sessions the symbol may miss between its last old and first new trade (a suspension).
#: AvenuesAI missed 4. More than this and the two trades are not proven to be one security.
MAX_GAP_SESSIONS: Final = 10


@dataclass(frozen=True, slots=True)
class L0Switch:
    """What L0 says about one reissue, as `read_switch` established it."""

    old_isin: str
    new_isin: str
    symbol: str
    last_old_session: date
    switch: date
    gap_sessions: int
    old_face_value: Decimal
    new_face_value: Decimal
    #: The face-value change's ex-date and terms, when the face value changed; else `None`.
    face_value_action: str | None
    evidence: tuple[EvidenceLine, ...]


def read_switch(
    l0: L0Store, *, old_isin: str, new_isin: str, effective: date, snapshot_date: date
) -> L0Switch:
    """Re-read the reissue out of L0; raise `RepairRefusedError` unless it says exactly this.

    What it does: the checks in the module docstring, over the L0 cash bhavcopies from
    `LOOKBACK_DAYS` before `effective` to `snapshot_date`, the dated `EQUITY_L` captures from the
    last old session to `snapshot_date`, and the `nse_corp_actions` payloads stored from
    `effective` on.
    What it assumes: L0 holds one bhavcopy per session; a session with no file is not counted.
    What it never does: open a socket, or infer the symbol from anything but the switch session.
    """
    from dataplatform.ingest.nse.bhavcopy_udiff import parse_l0 as parse_udiff_l0

    if old_isin == new_isin or old_isin[:7] != new_isin[:7]:
        raise RepairRefusedError(
            f"{old_isin} -> {new_isin}: not one issuer code; a reissue keeps isin[:7]"
        )
    if effective > snapshot_date:
        raise RepairRefusedError(f"switch {effective} is after the snapshot {snapshot_date}")

    sessions: list[tuple[L0Ref, dict[str, frozenset[str]]]] = []
    for ref in sorted(
        l0.iter_refs(
            _UDIFF_SOURCE, start=effective - timedelta(days=LOOKBACK_DAYS), end=snapshot_date
        ),
        key=_ref_order,
    ):
        traded: dict[str, set[str]] = {}
        for row in parse_udiff_l0(l0, ref):
            traded.setdefault(row.symbol.strip().upper(), set()).add(row.isin)
        sessions.append((ref, {k: frozenset(v) for k, v in traded.items()}))
    dates = [ref.logical_date for ref, _ in sessions]
    if dates.count(effective) != 1:
        raise RepairRefusedError(f"L0 holds {dates.count(effective)} bhavcopies for {effective}")

    on_switch = dict(sessions[dates.index(effective)][1])
    carriers = sorted(sym for sym, isins in on_switch.items() if new_isin in isins)
    if len(carriers) != 1:
        raise RepairRefusedError(
            f"bhavcopy {effective} trades {new_isin} under {carriers or 'no symbol'}; need one"
        )
    symbol = carriers[0]

    problems: list[str] = []
    last_old: date | None = None
    for ref, by_symbol in sessions:
        day = ref.logical_date
        mine: frozenset[str] = by_symbol.get(symbol, frozenset())
        if day < effective:
            if any(new_isin in isins for isins in by_symbol.values()):
                problems.append(f"{ref.key}: {new_isin} trades before the switch")
            if mine and mine != {old_isin}:
                problems.append(f"{ref.key}: {symbol} trades as {sorted(mine)}, not old only")
            elif mine:
                last_old = day
        else:
            if any(old_isin in isins for isins in by_symbol.values()):
                problems.append(f"{ref.key}: {old_isin} trades on or after the switch")
            if mine and mine != {new_isin}:
                problems.append(f"{ref.key}: {symbol} trades as {sorted(mine)}, not new only")
    if last_old is None:
        problems.append(
            f"no session in the {LOOKBACK_DAYS} days before {effective} trades {symbol} as "
            f"{old_isin}"
        )
        raise RepairRefusedError("L0 does not evidence the switch:\n  " + "\n  ".join(problems))
    gap = sum(1 for day in dates if last_old < day < effective)
    if gap > MAX_GAP_SESSIONS:
        problems.append(f"{gap} sessions without {symbol} between {last_old} and {effective}")

    lines = [
        EvidenceLine(ref.key, ref.sha256, tuple(sorted(by_symbol.get(symbol, frozenset()))))
        for ref, by_symbol in sessions
        if last_old <= ref.logical_date <= effective
    ]

    captures = sorted(
        (
            ref
            for ref in l0.iter_refs(NSE_EQUITY_LIST_SOURCE, start=last_old, end=snapshot_date)
            if ref.filename == equity_list_filename(ref.logical_date)
        ),
        key=_ref_order,
    )
    listed: dict[date, list[EquityListRow]] = {}
    for ref in captures:
        rows = [
            row for row in parse_equity_list(l0.get(ref).decode("utf-8")) if row.symbol == symbol
        ]
        listed[ref.logical_date] = rows
        isins = tuple(sorted(row.isin for row in rows))
        lines.append(EvidenceLine(ref.key, ref.sha256, isins))
        day = ref.logical_date
        if day == last_old and isins != (old_isin,):
            problems.append(f"{ref.key}: {symbol} listed as {isins}, not old only")
        elif last_old < day < effective and isins not in ((old_isin,), ()):
            problems.append(f"{ref.key}: {symbol} listed as {isins} before the switch")
        elif day >= effective and isins != (new_isin,):
            problems.append(f"{ref.key}: {symbol} listed as {isins}, not new only")
    for need in (last_old, effective, snapshot_date):
        if need not in listed:
            problems.append(f"no EQUITY_L capture in L0 for {need}")
    if problems:
        raise RepairRefusedError("L0 does not evidence the switch:\n  " + "\n  ".join(problems))

    old_fv = listed[last_old][0].face_value_inr
    new_fv = listed[effective][0].face_value_inr
    if old_fv is None or new_fv is None:
        raise RepairRefusedError(
            f"EQUITY_L does not state {symbol}'s face value on {last_old} and {effective}"
        )
    action: str | None = None
    if old_fv != new_fv:
        action, ca_lines = _face_value_action(
            l0,
            isins=(old_isin, new_isin),
            effective=effective,
            old_face_value=old_fv,
            new_face_value=new_fv,
        )
        lines.extend(ca_lines)
    return L0Switch(
        old_isin=old_isin,
        new_isin=new_isin,
        symbol=symbol,
        last_old_session=last_old,
        switch=effective,
        gap_sessions=gap,
        old_face_value=old_fv,
        new_face_value=new_fv,
        face_value_action=action,
        evidence=tuple(lines),
    )


def _face_value_action(
    l0: L0Store,
    *,
    isins: tuple[str, str],
    effective: date,
    old_face_value: Decimal,
    new_face_value: Decimal,
) -> tuple[str, list[EvidenceLine]]:
    """The NSE face-value action that explains the change: ex `effective`, old → new face value.

    Every face-value record for either ISIN in the payloads stored from `effective` on must say
    exactly that, and there must be at least one. Raises `RepairRefusedError` otherwise.
    """
    agreeing: list[EvidenceLine] = []
    problems: list[str] = []
    for ref in sorted(l0.iter_refs(_CA_SOURCE, start=effective), key=_ref_order):
        for record in _ca_records(l0.get(ref)):
            if str(record.get("isin", "")).strip() not in isins:
                continue
            subject = str(record.get("subject", ""))
            outcome = parse_purpose(subject, source=_CA_SOURCE)
            terms = None if outcome.action is None else outcome.action.terms
            if not isinstance(terms, FaceValueTerms):
                continue
            ex_date = _ca_date(record.get("exDate"))
            if (ex_date, terms.from_value, terms.to_value) == (
                effective,
                old_face_value,
                new_face_value,
            ):
                agreeing.append(EvidenceLine(ref.key, ref.sha256, (str(record["isin"]),)))
            else:
                problems.append(f"{ref.key}: {subject!r} ex {ex_date} contradicts the switch")
    if not agreeing:
        problems.append(
            f"face value {old_face_value} -> {new_face_value} with no {_CA_SOURCE} record ex "
            f"{effective} in L0"
        )
    if problems:
        raise RepairRefusedError("L0 does not explain the face value:\n  " + "\n  ".join(problems))
    action = f"face value ₹{old_face_value} → ₹{new_face_value} ex {effective.isoformat()}"
    return action, agreeing


def _ca_records(payload: bytes) -> list[Mapping[str, Any]]:
    document = json.loads(payload.decode("utf-8"), parse_float=str, parse_int=str)
    if isinstance(document, Mapping) and "data" in document:
        document = document["data"]
    if not isinstance(document, list):
        raise RepairRefusedError(f"{_CA_SOURCE} payload is not a JSON array")
    return [record for record in document if isinstance(record, Mapping)]


def _ca_date(value: object) -> date | None:
    try:
        return datetime.strptime(str(value).strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def derive_reissue(conn: Connection, switch: L0Switch, *, snapshot_date: date) -> Reissue:
    """The `Reissue` `plan_repair` checks the store against, read from the store itself.

    What it does: the old ISIN's NSE windows (its last, under the symbol, as it stood before the
    repair: open), the new ISIN's NSE windows from before the switch as the stale ones, and one
    INGEST conflict per stale window, on its start. `plan_repair` then refuses unless the stale
    rows were written by the `snapshot_date` ingest, the INGEST rows are exactly those, the
    re-derivation moves exactly this boundary, and nothing else claims the symbols.
    What it never does: write, or decide anything `plan_repair` does not check again.
    """
    rows = conn.execute(
        "SELECT id, isin, symbol, valid_from, valid_to, source FROM symbol_history "
        "WHERE exchange = %s AND isin = ANY(%s) ORDER BY valid_from, symbol, id",
        (_EXCHANGE.value, [switch.old_isin, switch.new_isin]),
    ).fetchall()
    old = [
        (str(symbol), valid_from, valid_to)
        for _, isin, symbol, valid_from, valid_to, _ in rows
        if isin == switch.old_isin
    ]
    if not old:
        raise RepairRefusedError(f"{switch.old_isin} has no NSE window")
    symbol, start, end = old[-1]
    if (
        symbol != switch.symbol
        or not start < switch.last_old_session
        or end not in (None, switch.switch - timedelta(days=1))
    ):
        raise RepairRefusedError(
            f"{switch.old_isin}'s last NSE window is {symbol} [{start}..{end or 'open'}], not "
            f"{switch.symbol} open over {switch.last_old_session}"
        )
    old[-1] = (symbol, start, None)
    stale = tuple(
        StaleWindow(str(symbol), valid_from, valid_to, str(source), int(row_id))
        for row_id, isin, symbol, valid_from, valid_to, source in rows
        if isin == switch.new_isin and valid_from < switch.switch
    )
    return Reissue(
        old_isin=switch.old_isin,
        new_isin=switch.new_isin,
        symbol=switch.symbol,
        last_old_session=switch.last_old_session,
        switch=switch.switch,
        gap_sessions=switch.gap_sessions,
        old_windows=tuple(old),
        stale=stale,
        conflicts=tuple(ExpectedConflict(s.symbol, s.valid_from) for s in stale),
    )


def resolution_reason(switch: L0Switch, *, snapshot_date: date, note: str | None = None) -> str:
    """The `identity_reconciliation.resolution` text: the evidence and what the repair did."""
    face_value = (
        f"; {switch.face_value_action} (L0 {_CA_SOURCE})" if switch.face_value_action else ""
    )
    reason = (
        f"{COMMAND}: ISIN reissued {switch.old_isin} -> {switch.new_isin} under NSE "
        f"{switch.symbol}: last old session {switch.last_old_session.isoformat()}, "
        f"{switch.gap_sessions} session(s) without {switch.symbol}, first new session "
        f"{switch.switch.isoformat()} (L0 {_UDIFF_SOURCE} and {NSE_EQUITY_LIST_SOURCE})"
        f"{face_value}. MANUAL isin_lineage edge recorded; the listing-date window(s) the "
        f"{snapshot_date.isoformat()} ingest stored on {switch.new_isin} deleted; "
        f"{switch.old_isin} {switch.symbol} closed "
        f"{(switch.switch - timedelta(days=1)).isoformat()}; {switch.new_isin} {switch.symbol} "
        f"from {switch.switch.isoformat()}."
    )
    return f"{reason} {note.strip()}" if note and note.strip() else reason


def plan_reissue_repair(
    conn: Connection,
    *,
    l0: L0Store,
    old_isin: str,
    new_isin: str,
    effective: date,
    snapshot_date: date,
    equity_list: str,
    symbol_changes: str,
    evidence: ReissueEvidence | None = None,
    note: str | None = None,
) -> RepairPlan:
    """Check L0, read the store, and plan the repair without writing. Raises on any surprise.

    What it assumes: the caller's transaction; `equity_list`/`symbol_changes` are the
    `snapshot_date` files the stale rows came from.
    """
    switch = read_switch(
        l0, old_isin=old_isin, new_isin=new_isin, effective=effective, snapshot_date=snapshot_date
    )
    return plan_repair(
        conn,
        l0=l0,
        equity_list=equity_list,
        symbol_changes=symbol_changes,
        snapshot_date=snapshot_date,
        reissue=derive_reissue(conn, switch, snapshot_date=snapshot_date),
        evidence=evidence,
        evidence_lines=switch.evidence,
        reason=resolution_reason(switch, snapshot_date=snapshot_date, note=note),
        command=COMMAND,
    )


def apply_reissue_repair(conn: Connection, plan: RepairPlan, *, clock: Clock) -> RepairCounts:
    """`repair_avenuesai.apply_repair`: every row count checked against the plan. No commit."""
    return apply_repair(conn, plan, clock=clock)


def _ref_order(ref: L0Ref) -> date:
    return ref.logical_date


def _date_arg(text: str) -> date:
    return date.fromisoformat(text)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the plan; with `--apply`, execute it in one transaction and commit."""
    from dataplatform.config import get_settings
    from dataplatform.identity.ingest import L0EquityListSeries, read_snapshot_from_l0
    from dataplatform.logging import configure_logging
    from dataplatform.store.db import connection

    parser = argparse.ArgumentParser(
        prog=f"identity.{COMMAND}", description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument("--old", required=True, help="the retired ISIN")
    parser.add_argument("--new", required=True, help="the ISIN that replaced it")
    parser.add_argument(
        "--effective", required=True, type=_date_arg, help="first session as the new ISIN"
    )
    parser.add_argument(
        "--snapshot-date",
        required=True,
        type=_date_arg,
        help="the identity snapshot whose ingest stored the listing-date windows",
    )
    parser.add_argument("--note", help="appended to every resolution (who approved, follow-ups)")
    parser.add_argument(
        "--apply", action="store_true", help="execute and commit (default: dry run, read only)"
    )
    args = parser.parse_args(argv)
    configure_logging()

    clock = SystemClock()
    settings = get_settings()
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    equity_list, changes = read_snapshot_from_l0(args.snapshot_date, store=l0)
    with connection(settings) as conn:
        if args.apply:
            # Held to commit: no ingest or resolve can change the three tables between the plan
            # being checked and it being written. Reads (the dry run, resolvers) still proceed.
            conn.execute(
                "LOCK TABLE symbol_history, identity_reconciliation, isin_lineage "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
        else:
            conn.execute("SET TRANSACTION READ ONLY")
        try:
            plan = plan_reissue_repair(
                conn,
                l0=l0,
                old_isin=args.old,
                new_isin=args.new,
                effective=args.effective,
                snapshot_date=args.snapshot_date,
                equity_list=equity_list,
                symbol_changes=changes,
                evidence=L0EquityListSeries(l0),
                note=args.note,
            )
            print("\n".join(plan.describe()))
            if not args.apply:
                conn.rollback()
                print("dry run: nothing written (READ ONLY transaction). Re-run with --apply.")
                return 0
            counts = apply_reissue_repair(conn, plan, clock=clock)
        except IdentityError as error:
            conn.rollback()
            print(f"{COMMAND} refused: {error}", file=sys.stderr)
            return 2
        conn.commit()
    print(
        f"applied: {counts.edges} edge, {counts.deleted} deleted, {counts.inserted} inserted, "
        f"{counts.closed} closed, {counts.resolved} reconciliation row(s) resolved"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
