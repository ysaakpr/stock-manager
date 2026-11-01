"""D7: the share-basis guard — a split or consolidation the feeds missed never reaches a decision.

AvenuesAI consolidated ten Re 1 shares into one Rs 10 share on 2026-10-13. NSE stated the terms
and the record date in its book-closure file (``bc12102026.csv``: ``CNSLDATNRE1 TO RS10``) and left
the ex-date blank; its corporate-action API, queried by ex-date, therefore never returned the
event; BSE's feed has been blocked since 2026-09-24; the bc file was read only into an L1 dataset
nothing promotes into a factor; the reissue (INE483S01020 -> INE483S01038) spanned a seven-day
suspension, so the continuity check called the x9.40 step `LONG_GAP`; and the price-implied
detector only reads short gaps. The stitched series stepped x9.40 with nothing flagging it, and
a human had to find it. The owner's ruling (2026-11-01): *the system should have a way* — adjust
when the evidence is sufficient, otherwise fail closed and show it on Ops.

This module is that way. Each run, in the scheduled pipeline (`share_basis_guard` job):

1. **Stated terms (bc).** Every face-value change in NSE's bc member since T+1 settlement
   (`ADMIT_FROM`, 2023-01-27), resolved to an ISIN on its broadcast session. The ex-date is the
   stated one, or derived by the one rule (`corpactions.ex_date_rule`: record date, T+1, a
   session) and marked so; a row with neither is reported `NOT_DERIVED` and left to step 3. A
   row a recorded or curated split/bonus already covers (within `ADMITTED_YIELD_DAYS`) is
   `RECORDED`. Otherwise L1's own close-to-close step across the ex-date must agree with the
   stated ratio within `PRICE_AGREEMENT` — then it is **admitted** as a factor, otherwise
   **quarantined**.
2. **Reissues with a face-value change.** For every D2 lineage edge whose two ISINs' face values
   differ in the identity master, the raw step across the reissue, after every recorded and
   admitted factor, must be ~1. If it is not, and dividing by the face-value ratio makes it so,
   that ratio is **admitted** as a factor dated the successor's first session; if the price
   disagrees with the face values too, the edge is **quarantined**.
3. **The continuity scan** (`quality.l2_continuity`, now with `SHARE_BASIS`) over L2, after the
   ISINs whose admitted factors changed have been rebuilt: every step across a reissue, or across
   a long gap at a clean multiple, that nothing explains is **quarantined**.
4. **Fail closed.** The quarantines go to the guard's state (`corpactions.share_basis_state`),
   which `dataplatform.query.default_price_quarantine` reads: every decision-facing reader
   withholds the ISIN's bars before the step (the D22 mechanism). Each one is an ERROR on
   `/status/quality` naming the ISIN, the step and the reason (`l2_continuity` for scan steps,
   `share_basis_guard` for a stated ratio the price contradicts). Both clear themselves on the
   first run after a corporate-action row explains the step.

**Why the price must agree before anything is admitted.** A factor rewrites every earlier bar
of the ISIN, so a wrong one is worse than none. The terms are the exchange's own, but the
symbol-to-ISIN resolution, the ex-date derivation and the master's face values are all ours; the
price step is the independent witness. `PRICE_AGREEMENT` is one daily band (20%) either way:
across a suspension the market moves, and the nearest wrong reading of a stated ratio is at least
2x away (x10 vs x5 for a Re 1 -> Rs 10 consolidation read as Rs 2 -> Rs 10), so a band of 0.8 to
1.25 cannot admit the wrong one. AvenuesAI: 130.05 / (13.83 x 10) = 0.940.

**Never from price alone.** No factor is ever derived from a price step: a suspension can hide a
real move. Price only ever confirms terms a source states, or refuses them.

Determinism: the decision core (`decide`) is pure; `gather` reads L0, L1 and Postgres; `run_guard`
orders the writes (state, L2 rebuild, findings). Offline: never fetches.

    uv run python -m dataplatform.quality.share_basis --dry-run   # report, write nothing
    uv run python -m dataplatform.quality.share_basis             # admit, rebuild, quarantine
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from dataplatform.corpactions.bc_terms import face_value_change
from dataplatform.corpactions.ex_date_rule import T1_SETTLEMENT_FROM, derive_ex_date
from dataplatform.corpactions.factors import PRICE_EVENT_TYPES, FactorChain, with_events
from dataplatform.corpactions.share_basis_state import (
    BC_SOURCE,
    REISSUE_SOURCE,
    AdmissionRule,
    AdmittedAction,
    QuarantineWindow,
    ShareBasisState,
)
from dataplatform.corpactions.taxonomy import FaceValueTerms
from dataplatform.logging import get_logger
from dataplatform.quality.sentinel import FindingScope, QualityFinding, finding_fingerprint

if TYPE_CHECKING:
    from dataplatform.clock import Clock
    from dataplatform.ingest.corp_actions import CorporateAction
    from dataplatform.quality.l2_continuity import L2ContinuityReport
    from dataplatform.store.db import Connection

__all__ = [
    "ADMITTED_YIELD_DAYS",
    "ADMIT_FROM",
    "GUARD_CHECK",
    "PRICE_AGREEMENT",
    "BcFaceValueRow",
    "Decision",
    "Outcome",
    "PriceBar",
    "Reissue",
    "Verdict",
    "decide",
    "guard_findings",
    "run_guard",
]

_LOG = get_logger(__name__)

#: The `quality_flag.check_name` of the guard's own findings (stated terms the price contradicts).
GUARD_CHECK: Final = "share_basis_guard"

#: The first ex-date a bc line is admitted for: T+1 settlement, when the ex-date rule holds.
#: Earlier history is the feeds', the curated file's and the implied detector's.
ADMIT_FROM: Final = T1_SETTLEMENT_FROM

#: Close-to-close agreement, after the factor, for terms to be admitted (module docstring).
PRICE_AGREEMENT: Final = (Decimal("0.8"), Decimal("1.25"))

#: How near a recorded or curated price event must be for it to cover a bc line or reissue.
ADMITTED_YIELD_DAYS: Final = 7

#: Series a bc line is resolved for: the equity series the master may be asked about.
_EQUITY_SERIES: Final = frozenset({"EQ", "BE", "BZ", "SM", "ST"})

#: How far before a reissue a bc line may sit and still be its terms.
_REISSUE_BC_WINDOW: Final = timedelta(days=45)


class Outcome(StrEnum):
    """What the guard did with one bc line or one reissue."""

    ADMITTED = "ADMITTED"
    RECORDED = "RECORDED"
    EXPLAINED = "EXPLAINED"
    QUARANTINED = "QUARANTINED"
    NOT_DERIVED = "NOT_DERIVED"
    PENDING = "PENDING"
    NO_PRICES = "NO_PRICES"


@dataclass(frozen=True, slots=True)
class BcFaceValueRow:
    """One bc line stating a face-value change, resolved to the ISIN that traded on its session."""

    isin: str
    symbol: str
    purpose: str
    terms: FaceValueTerms
    record_date: date | None
    stated_ex_date: date | None
    knowable_date: date
    l0_key: str


@dataclass(frozen=True, slots=True)
class PriceBar:
    """One NSE EQ close from L1, with the exchange's own previous close for that session."""

    isin: str
    trade_date: date
    close: Decimal
    prev_close: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Reissue:
    """A D2 lineage edge with the master's face values for its two ISINs (None when unknown)."""

    predecessor: str
    successor: str
    effective_date: date
    face_value_before: Decimal | None = None
    face_value_after: Decimal | None = None

    @property
    def face_value_ratio(self) -> Decimal | None:
        """The price factor the face values imply (after / before), when both known and differ."""
        before, after = self.face_value_before, self.face_value_after
        if before is None or after is None or before <= 0 or after <= 0 or before == after:
            return None
        return after / before


@dataclass(frozen=True, slots=True)
class Verdict:
    """One line of the guard's report: what was examined, and what it decided."""

    subject: str
    isin: str
    survivor: str
    on: date | None
    outcome: Outcome
    detail: str


@dataclass(frozen=True, slots=True)
class Decision:
    """`decide`'s whole output: the factors to admit, the quarantines, and the report."""

    admitted: tuple[AdmittedAction, ...] = ()
    quarantined: tuple[QuarantineWindow, ...] = ()
    verdicts: tuple[Verdict, ...] = ()


# ── the pure core ────────────────────────────────────────────────────────────────────────────


def decide(
    *,
    bc_rows: Iterable[BcFaceValueRow],
    reissues: Iterable[Reissue],
    recorded: Iterable[CorporateAction],
    bars: Mapping[str, Sequence[PriceBar]],
    survivor_of: Callable[[str], str],
    chain_of: Callable[[str], Sequence[str]],
    is_session: Callable[[date], bool],
    as_of: date | None = None,
) -> Decision:
    """Steps 1 and 2 of the module docstring: admit stated terms the price agrees with.

    `recorded` are the reconciled feed actions and the curated rows (any ISIN; each is keyed to
    its survivor here, and one event recorded under both ISINs of a reissue counts once); `bars`
    every NSE EQ close of the ISINs involved, by ISIN; `as_of` the latest session L1 holds, which
    tells an event still to trade (`PENDING`) from one whose chain never traded after it. Pure.

    What it never does: derive a ratio from a price, admit an ex-date before `ADMIT_FROM` from a
    bc line, or admit anything whose price step it cannot see on both sides.
    """
    by_survivor: dict[str, list[CorporateAction]] = {}
    counted: set[tuple[str, date, str, str]] = set()
    for event in recorded:
        if event.action_type not in PRICE_EVENT_TYPES:
            continue
        survivor = survivor_of(event.isin)
        # TAALTECH's 2026-09-22 split is in the store twice, BSE's row under the retired ISIN and
        # NSE's under the new one: one event, one factor.
        identity = (survivor, event.ex_date, event.action_type.value, event.terms.model_dump_json())
        if identity in counted:
            continue
        counted.add(identity)
        by_survivor.setdefault(survivor, []).append(event)
    admitted: list[AdmittedAction] = []
    quarantined: list[QuarantineWindow] = []
    verdicts: list[Verdict] = []

    seen: set[tuple[str, date | None, Decimal, Decimal]] = set()
    for row in sorted(bc_rows, key=lambda r: (r.knowable_date, r.isin, r.purpose)):
        # The same event under BE/BL/IQ, re-broadcast the next session, or filed under both ISINs
        # of the reissue it causes: one verdict, at its first broadcast.
        key = (survivor_of(row.isin), row.stated_ex_date or row.record_date, *_fv(row.terms))
        if key in seen:
            continue
        seen.add(key)
        verdict, action, window = _judge_bc(
            row, by_survivor, bars, survivor_of, chain_of, is_session, as_of
        )
        verdicts.append(verdict)
        if action is not None:
            admitted.append(action)
        if window is not None:
            quarantined.append(window)

    admitted_events = [a.as_action() for a in admitted]
    for edge in sorted(reissues, key=lambda e: (e.effective_date, e.successor)):
        survivor = survivor_of(edge.successor)
        known = [
            *by_survivor.get(survivor, ()),
            *(a for a in admitted_events if a.isin == survivor),
        ]
        edge_verdict, edge_action, edge_window = _judge_reissue(edge, survivor, known, bars)
        if edge_verdict is not None:
            verdicts.append(edge_verdict)
        if edge_action is not None:
            admitted.append(edge_action)
        if edge_window is not None:
            quarantined.append(edge_window)
    return Decision(tuple(admitted), tuple(quarantined), tuple(verdicts))


def _judge_bc(
    row: BcFaceValueRow,
    by_survivor: Mapping[str, Sequence[CorporateAction]],
    bars: Mapping[str, Sequence[PriceBar]],
    survivor_of: Callable[[str], str],
    chain_of: Callable[[str], Sequence[str]],
    is_session: Callable[[date], bool],
    as_of: date | None,
) -> tuple[Verdict, AdmittedAction | None, QuarantineWindow | None]:
    survivor = survivor_of(row.isin)

    def verdict(outcome: Outcome, on: date | None, detail: str) -> Verdict:
        return Verdict(f"bc {row.purpose!r}", row.isin, survivor, on, outcome, detail)

    rule: str | None = None
    ex_date = row.stated_ex_date
    if ex_date is None:
        derived = derive_ex_date(row.record_date, is_session=is_session)
        if derived is None:
            why = (
                "no record date stated"
                if row.record_date is None
                else f"record date {row.record_date} is before T+1 settlement (2023-01-27)"
                if row.record_date < ADMIT_FROM
                else f"record date {row.record_date} is not a trading session"
            )
            return (
                verdict(Outcome.NOT_DERIVED, row.record_date, f"ex-date blank; {why}"),
                None,
                None,
            )
        ex_date, rule = derived.ex_date, derived.rule.value
    if ex_date < ADMIT_FROM:
        return verdict(Outcome.NOT_DERIVED, ex_date, "ex-date before T+1 settlement"), None, None
    near = [
        a
        for a in by_survivor.get(survivor, ())
        if abs((a.ex_date - ex_date).days) <= ADMITTED_YIELD_DAYS
    ]
    if near:
        stated = ", ".join(f"{a.action_type.value} {a.ex_date} ({a.source})" for a in near)
        return verdict(Outcome.RECORDED, ex_date, f"already recorded: {stated}"), None, None
    series = sorted(
        (b for isin in chain_of(survivor) for b in bars.get(isin, ())), key=lambda b: b.trade_date
    )
    before = [b for b in series if b.trade_date < ex_date]
    after = [b for b in series if b.trade_date >= ex_date]
    if not after and (as_of is None or ex_date > as_of):
        return verdict(Outcome.PENDING, ex_date, "the ex-date has not traded yet"), None, None
    if not after:
        detail = (
            "the chain never traded on or after the ex-date: the new ISIN has no lineage edge, "
            "so no partition stitches the step and there is nothing to adjust"
        )
        return verdict(Outcome.NO_PRICES, ex_date, detail), None, None
    if not before:
        return verdict(Outcome.NO_PRICES, ex_date, "no session before the ex-date"), None, None
    last, first = before[-1], after[0]
    factor = row.terms.to_value / row.terms.from_value
    known = _factor_between(
        survivor, by_survivor.get(survivor, ()), last.trade_date, first.trade_date
    )
    step = first.close / (last.close * known)
    residual = step / factor
    evidence = (
        f"{last.isin} {last.trade_date} {last.close} -> {first.isin} {first.trade_date} "
        f"{first.close}: x{step:.4f} raw against the stated x{factor.normalize()}, residual "
        f"{residual:.4f}{_prev_close_note(last, first, known * factor)}"
    )
    if _agrees(residual):
        action = AdmittedAction(
            isin=survivor,
            filed_against_isin=None if row.isin == survivor else row.isin,
            ex_date=ex_date,
            from_value=row.terms.from_value,
            to_value=row.terms.to_value,
            source=BC_SOURCE,
            rule=AdmissionRule.BC_STATED_TERMS,
            raw_text=row.purpose,
            knowable_date=row.knowable_date,
            record_date=row.record_date,
            l0_key=row.l0_key,
            ex_date_rule=rule,
            evidence=evidence,
        )
        detail = f"admitted ({rule or 'ex-date stated'}): {evidence}"
        return verdict(Outcome.ADMITTED, ex_date, detail), action, None
    window = QuarantineWindow(
        isin=survivor,
        first_session=first.trade_date,
        prev_date=last.trade_date,
        ratio=f"x{step:.4f}",
        kind="stated_terms_disagree",
        reason=f"bc {row.purpose!r} ({row.l0_key}) states x{factor.normalize()}; {evidence}",
    )
    return verdict(Outcome.QUARANTINED, ex_date, window.reason), None, window


def _judge_reissue(
    edge: Reissue,
    survivor: str,
    known_events: Sequence[CorporateAction],
    bars: Mapping[str, Sequence[PriceBar]],
) -> tuple[Verdict | None, AdmittedAction | None, QuarantineWindow | None]:
    factor = edge.face_value_ratio
    if factor is None:
        return None, None, None  # no face-value change: the continuity scan watches this edge

    def verdict(outcome: Outcome, on: date | None, detail: str) -> Verdict:
        subject = f"reissue {edge.predecessor} -> {edge.successor} {edge.effective_date}"
        return Verdict(subject, edge.successor, survivor, on, outcome, detail)

    before = [b for b in bars.get(edge.predecessor, ()) if b.trade_date < edge.effective_date]
    after = [b for b in bars.get(edge.successor, ()) if b.trade_date >= edge.effective_date]
    if not before or not after:
        return (
            verdict(Outcome.NO_PRICES, edge.effective_date, "no L1 close on one side"),
            None,
            None,
        )
    last, first = max(before, key=lambda b: b.trade_date), min(after, key=lambda b: b.trade_date)
    known = _factor_between(survivor, known_events, last.trade_date, first.trade_date)
    step = first.close / (last.close * known)
    evidence = (
        f"{last.isin} {last.trade_date} {last.close} -> {first.isin} {first.trade_date} "
        f"{first.close}: x{step:.4f} after recorded factors (x{known.normalize()}); face value "
        f"{edge.face_value_before.normalize() if edge.face_value_before else '?'} -> "
        f"{edge.face_value_after.normalize() if edge.face_value_after else '?'} implies "
        f"x{factor.normalize()}{_prev_close_note(last, first, known * factor)}"
    )
    if _agrees(step):
        return verdict(Outcome.EXPLAINED, first.trade_date, evidence), None, None
    if _agrees(step / factor):
        assert edge.face_value_before is not None and edge.face_value_after is not None
        action = AdmittedAction(
            isin=survivor,
            filed_against_isin=None if edge.successor == survivor else edge.successor,
            ex_date=first.trade_date,
            from_value=edge.face_value_before,
            to_value=edge.face_value_after,
            source=REISSUE_SOURCE,
            rule=AdmissionRule.REISSUE_FACE_VALUE,
            raw_text=(
                f"face value {edge.face_value_before.normalize()} ({edge.predecessor}) -> "
                f"{edge.face_value_after.normalize()} ({edge.successor}), identity master"
            ),
            knowable_date=first.trade_date,
            evidence=evidence,
        )
        return verdict(Outcome.ADMITTED, first.trade_date, f"admitted: {evidence}"), action, None
    window = QuarantineWindow(
        isin=survivor,
        first_session=first.trade_date,
        prev_date=last.trade_date,
        ratio=f"x{step:.4f}",
        kind="reissue_face_value_disagrees",
        reason=f"reissue {edge.predecessor} -> {edge.successor}: {evidence}",
    )
    return verdict(Outcome.QUARANTINED, first.trade_date, window.reason), None, window


def _factor_between(
    survivor: str, events: Iterable[CorporateAction], after: date, through: date
) -> Decimal:
    """The product of the price factors of `events` ex in ``(after, through]``; 1 for none."""
    inside = [
        e.model_copy(update={"isin": survivor})
        for e in events
        if after < e.ex_date <= through and e.action_type in PRICE_EVENT_TYPES
    ]
    if not inside:
        return Decimal(1)
    chain = with_events(FactorChain(isin=survivor, rows=()), inside)
    return chain.price_factor_asof(after) / chain.price_factor_asof(through)


def _agrees(residual: Decimal) -> bool:
    low, high = PRICE_AGREEMENT
    return low <= residual <= high


def _prev_close_note(last: PriceBar, first: PriceBar, factor: Decimal) -> str:
    """The exchange's own adjusted previous close, when it printed one, as corroboration."""
    if first.prev_close is None or last.close == 0:
        return ""
    printed = first.prev_close / last.close
    return f"; NSE printed PREV_CLOSE {first.prev_close} = x{printed:.4f} of {last.close}" + (
        " (matches)" if abs(printed / factor - 1) <= Decimal("0.005") else ""
    )


def _identity(action: AdmittedAction) -> tuple[str, date, Decimal, Decimal, str]:
    """What makes two admitted factors the same factor for L2 (not their evidence text)."""
    return (action.isin, action.ex_date, action.from_value, action.to_value, action.source)


def _fv(terms: FaceValueTerms) -> tuple[Decimal, Decimal]:
    return terms.from_value, terms.to_value


# ── findings ─────────────────────────────────────────────────────────────────────────────────


def guard_findings(decision: Decision) -> tuple[QualityFinding, ...]:
    """ERROR per quarantine the guard itself raised (stated terms the price contradicts)."""
    out: list[QualityFinding] = []
    for window in decision.quarantined:
        out.append(
            QualityFinding(
                logical_date=window.first_session,
                check_name=GUARD_CHECK,
                severity="ERROR",
                isin=window.isin,
                source="prices_adjusted",
                detail={
                    "kind": window.kind,
                    "prev_date": None if window.prev_date is None else window.prev_date.isoformat(),
                    "step": window.ratio,
                    "reason": window.reason,
                    "quarantined_before": window.first_session.isoformat(),
                },
                fingerprint=finding_fingerprint(
                    f"{GUARD_CHECK}:{window.kind}", window.isin, window.first_session
                ),
            )
        )
    return tuple(out)


def scan_quarantines(report: L2ContinuityReport) -> tuple[QuarantineWindow, ...]:
    """A quarantine per `SHARE_BASIS` step the continuity scan found (step 3)."""
    from dataplatform.quality.l2_continuity import share_basis_reason

    return tuple(
        QuarantineWindow(
            isin=step.isin,
            first_session=step.trade_date,
            prev_date=step.prev_date,
            ratio=f"x{step.ratio:.4f}",
            kind="share_basis_step",
            reason=share_basis_reason(step, report.reissue_dates.get(step.isin, ())),
        )
        for step in report.share_basis
    )


# ── the seam: L0, L1, Postgres in; state, L2, quality_flag out ───────────────────────────────


@dataclass(slots=True)
class GuardReport:
    """What one run did, for the job's log, the CLI and the sweep."""

    bc_rows: int = 0
    reissues_checked: int = 0
    admitted: list[AdmittedAction] = field(default_factory=list)
    quarantined: list[QuarantineWindow] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    rebuilt: list[str] = field(default_factory=list)
    scan_share_basis: int = 0
    scan_passed: bool = True

    def to_json(self) -> dict[str, object]:
        return {
            "bc_rows": self.bc_rows,
            "reissues_checked": self.reissues_checked,
            "admitted": [json.loads(a.model_dump_json()) for a in self.admitted],
            "quarantined": [json.loads(q.model_dump_json()) for q in self.quarantined],
            "verdicts": [
                {**asdict(v), "on": None if v.on is None else v.on.isoformat()}
                for v in self.verdicts
            ],
            "rebuilt": self.rebuilt,
            "scan_share_basis": self.scan_share_basis,
            "scan_passed": self.scan_passed,
        }


def gather_bc_rows(
    *, since: date, through: date, data_root: Path | None, master: object, clock: Clock
) -> tuple[BcFaceValueRow, ...]:
    """Every equity bc line stating a face-value change, broadcast in ``[since, through]``.

    Each is resolved to the ISIN the exchange's own bhavcopy shows trading under that symbol on
    the broadcast session, else the master's in-force window (`pr_bundle_l1.SessionResolver`, the
    same resolution the L1 `pr_ca_broadcasts` build uses). An unresolvable line is logged and
    skipped: with no ISIN there is nothing it could adjust (invariant #2).
    """
    from dataplatform.identity.master import IdentityMaster
    from dataplatform.ingest.models import ParseError
    from dataplatform.ingest.nse.pr_bundle.bc import parse_bc_bundle
    from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID, MemberKind, PrBundle
    from dataplatform.ingest.pr_bundle_l1 import SessionResolver, load_session_identity
    from dataplatform.store.l0 import L0Store

    assert master is None or isinstance(master, IdentityMaster)
    l0 = L0Store(clock=clock, data_root=data_root)
    out: list[BcFaceValueRow] = []
    for ref in l0.iter_refs(PR_BUNDLE_SOURCE_ID, start=since, end=through):
        try:
            with PrBundle(l0.get(ref), filename=ref.filename) as bundle:
                if not bundle.has(MemberKind.BC):
                    continue
                session = bundle.publication_date
                rows = [
                    (r, terms)
                    for r in parse_bc_bundle(bundle, l0_key=ref.key)
                    if r.series.strip().upper() in _EQUITY_SERIES
                    and (terms := face_value_change(r.purpose)) is not None
                ]
        except ParseError as exc:
            _LOG.warning(
                "share_basis.bc_unreadable", l0_key=ref.key, error=str(exc), state="SKIPPED"
            )
            continue
        if not rows:
            continue
        resolver = SessionResolver(
            session=session,
            statement=load_session_identity(session, l0=l0, data_root=data_root),
            master=master,
        )
        for row, terms in rows:
            isin, how = resolver.resolve(row.symbol, row.series)
            if isin is None:
                _LOG.warning(
                    "share_basis.bc_unresolved",
                    l0_key=ref.key,
                    symbol=row.symbol,
                    series=row.series,
                    purpose=row.purpose,
                    reason=str(how),
                    state="SKIPPED",
                )
                continue
            out.append(
                BcFaceValueRow(
                    isin=isin,
                    symbol=row.symbol,
                    purpose=row.purpose,
                    terms=terms,
                    record_date=row.record_date,
                    stated_ex_date=row.ex_date,
                    knowable_date=row.knowable_date,
                    l0_key=ref.key,
                )
            )
    return tuple(out)


def gather_reissues(
    conn: Connection, edges: Iterable[tuple[str, str, date]]
) -> tuple[Reissue, ...]:
    """Each lineage edge with the master's face values (NSE listing first, then the master)."""
    rows = conn.execute(
        "SELECT m.isin, coalesce("
        "  (SELECT l.face_value_inr FROM exchange_listing l WHERE l.isin = m.isin "
        "   AND l.exchange = 'NSE'),"
        "  (SELECT max(l.face_value_inr) FROM exchange_listing l WHERE l.isin = m.isin),"
        "  m.face_value_inr) FROM security_master m"
    ).fetchall()
    face = {str(isin): None if fv is None else Decimal(fv) for isin, fv in rows}
    return tuple(
        Reissue(
            predecessor=pred,
            successor=succ,
            effective_date=day,
            face_value_before=face.get(pred),
            face_value_after=face.get(succ),
        )
        for pred, succ, day in edges
    )


def gather_bars(isins: Iterable[str], *, data_root: Path | None) -> dict[str, list[PriceBar]]:
    """Every NSE EQ close of `isins` in L1 `prices_raw`, one DuckDB pass, by ISIN and date."""
    from dataplatform.store.l2 import open_connection
    from dataplatform.store.paths import Layer, layer_root

    wanted = sorted(set(isins))
    root = layer_root(Layer.L1, data_root=data_root) / "prices_raw"
    files = sorted(str(p) for p in root.glob("date=*/*.parquet"))
    out: dict[str, list[PriceBar]] = {}
    if not wanted or not files:
        return out
    con = open_connection()
    try:
        rows = con.execute(
            "SELECT isin, trade_date, close, prev_close FROM read_parquet($files) "
            "WHERE exchange = 'NSE' AND series = 'EQ' AND list_contains($isins, isin) "
            "ORDER BY isin, trade_date",
            {"files": files, "isins": wanted},
        ).fetchall()
    finally:
        con.close()
    for isin, day, close, prev in rows:
        if close is None or Decimal(close) <= 0:
            continue
        out.setdefault(str(isin), []).append(
            PriceBar(
                isin=str(isin),
                trade_date=day,
                close=Decimal(close),
                prev_close=None if prev is None else Decimal(prev),
            )
        )
    return out


def latest_l1_session(*, data_root: Path | None) -> date | None:
    """The newest `prices_raw` partition's session, or None for a cold lake."""
    from dataplatform.store.paths import Layer, layer_root

    root = layer_root(Layer.L1, data_root=data_root) / "prices_raw"
    days = [
        date.fromisoformat(p.name.removeprefix("date=")) for p in root.glob("date=*") if p.is_dir()
    ]
    return max(days, default=None)


def run_guard(
    conn: Connection,
    *,
    clock: Clock,
    data_root: Path | None = None,
    dry_run: bool = False,
    since: date = ADMIT_FROM - timedelta(days=60),
) -> GuardReport:
    """One whole guard run (module docstring, steps 1-4).

    Order matters and is fixed: decide from L0/L1/Postgres; write the admitted factors to the
    state (keeping the previous quarantines) and rebuild exactly the L2 partitions whose admitted
    set changed; scan L2; then write the state with this run's quarantines and persist the
    findings, closing the ones no longer raised. `dry_run` does everything but the writes.
    """
    from dataplatform.corpactions.manual_actions import default_manual_actions
    from dataplatform.corpactions.reconcile import load_reconciled_actions
    from dataplatform.corpactions.share_basis_state import load_state, write_state
    from dataplatform.identity.lineage import LineageStore
    from dataplatform.identity.master import IdentityStore, InMemoryReconciliationQueue
    from dataplatform.ingest.calendar import trading_calendar
    from dataplatform.quality import l2_continuity
    from dataplatform.quality.sentinel import persist_findings
    from dataplatform.store.l2 import open_connection, rebuild_isins

    today = clock.today()
    resolver = LineageStore(conn).load()
    edges = resolver.edges()
    master = IdentityStore(conn, clock=clock).load_master(queue=InMemoryReconciliationQueue())
    bc_rows = gather_bc_rows(
        since=since, through=today, data_root=data_root, master=master, clock=clock
    )
    reissues = gather_reissues(conn, edges)
    recorded = (
        *load_reconciled_actions(conn),
        *(a.as_action() for a in default_manual_actions().actions),
    )
    wanted: set[str] = set()
    for row in bc_rows:
        wanted.update(resolver.chain_to(resolver.survivor_of(row.isin)))
    for edge in reissues:
        if edge.face_value_ratio is not None:
            wanted.update((edge.predecessor, edge.successor))
    bars = gather_bars(wanted, data_root=data_root)
    decision = decide(
        bc_rows=bc_rows,
        reissues=reissues,
        recorded=recorded,
        bars=bars,
        survivor_of=resolver.survivor_of,
        chain_of=resolver.chain_to,
        is_session=trading_calendar().is_session,
        as_of=latest_l1_session(data_root=data_root),
    )
    report = GuardReport(
        bc_rows=len(bc_rows),
        reissues_checked=sum(1 for e in reissues if e.face_value_ratio is not None),
        admitted=list(decision.admitted),
        verdicts=list(decision.verdicts),
    )

    previous = load_state(data_root=data_root)
    # A partition is rebuilt when the factors admitted for it changed — added, dropped (a feed
    # has since published the event), or restated — and only then.
    changed = sorted(
        {
            key[0]
            for key in {_identity(a) for a in previous.admitted}
            ^ set(map(_identity, decision.admitted))
        }
    )
    if not dry_run:
        write_state(
            ShareBasisState(
                computed_at=clock.now(),
                admitted=decision.admitted,
                quarantined=previous.quarantined,
            ),
            data_root=data_root,
        )
        if changed:
            history = {s: resolver.chain_to(s) for s in changed}
            con = open_connection()
            try:
                rebuild_isins(
                    conn,
                    changed,
                    con=con,
                    data_root=data_root,
                    history_for=history,
                    survivor_of=resolver.survivor_of,
                )
            finally:
                con.close()
            report.rebuilt = list(changed)

    scan = l2_continuity.scan(
        conn, survivor_of=resolver.survivor_of, data_root=data_root, reissues=edges
    )
    report.scan_share_basis = len(scan.share_basis)
    report.scan_passed = scan.passed
    quarantined = (*decision.quarantined, *scan_quarantines(scan))
    report.quarantined = list(quarantined)
    if not dry_run:
        write_state(
            ShareBasisState(
                computed_at=clock.now(), admitted=decision.admitted, quarantined=quarantined
            ),
            data_root=data_root,
        )
        persist_findings(
            conn,
            l2_continuity.findings(scan, logical_date=today),
            clock=clock,
            resolve_scope=scan.resolution_scope,
        )
        persist_findings(
            conn,
            guard_findings(decision),
            clock=clock,
            resolve_scope=FindingScope(check_name=GUARD_CHECK),
        )
        conn.commit()
    _LOG.info(
        "share_basis.guard",
        bc_rows=report.bc_rows,
        reissues_checked=report.reissues_checked,
        admitted=len(report.admitted),
        quarantined=len(report.quarantined),
        rebuilt=len(report.rebuilt),
        scan_share_basis=report.scan_share_basis,
        dry_run=dry_run,
        state="RED" if report.quarantined else "GREEN",
    )
    return report


def run_share_basis_guard_job(context: object) -> None:
    """The scheduled job body: one `run_guard` against the configured lake and store."""
    from dataplatform.scheduler.registry import JobContext
    from dataplatform.store.db import connect

    assert isinstance(context, JobContext)
    with connect(context.settings) as conn:
        run_guard(conn, clock=context.clock, data_root=context.settings.data_root)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: run the guard (or `--dry-run` it) and print what it decided; `--report` writes JSON."""
    from dataplatform.clock import SystemClock
    from dataplatform.config import get_settings
    from dataplatform.store.db import connect

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="decide and report; write nothing")
    parser.add_argument("--since", type=date.fromisoformat, default=ADMIT_FROM - timedelta(days=60))
    parser.add_argument("--report", type=Path, default=None, help="write the full report as JSON")
    args = parser.parse_args(argv)
    settings = get_settings()
    with connect() as conn:
        report = run_guard(
            conn,
            clock=SystemClock(),
            data_root=settings.data_root,
            dry_run=args.dry_run,
            since=args.since,
        )
    for v in report.verdicts:
        print(f"{v.outcome.value:<12} {v.survivor} {v.on} {v.subject}: {v.detail}")
    for q in report.quarantined:
        print(f"QUARANTINE   {q.isin} before {q.first_session} {q.ratio} [{q.kind}] {q.reason}")
    print(
        f"bc_rows={report.bc_rows} reissues_checked={report.reissues_checked} "
        f"admitted={len(report.admitted)} quarantined={len(report.quarantined)} "
        f"rebuilt={len(report.rebuilt)} scan_share_basis={report.scan_share_basis}"
    )
    if args.report is not None:
        args.report.write_text(json.dumps(report.to_json(), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
