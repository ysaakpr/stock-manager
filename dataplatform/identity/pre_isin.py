"""Resolve NSE pre-ISIN (E1, before 2011-06-22) bhavcopy rows to an ISIN — only where it is proved.

The NSE bhavcopy published no ISIN column until 2011-06-22 (era E1, `dataplatform.ingest.nse.eras`).
ISIN is the only join key (invariant #2), so W1 retained those 1.65M rows in quarantine. This module
admits some of them, and the whole design is about what it refuses to admit.

**Why a current-day listing cannot be the resolver.** "Symbol X today is ISIN Y, so X in 2008 was
Y" is wrong in exactly the cases a backtest cares about: a company delisted before today is absent,
symbols are recycled, renames move a company onto a new symbol, and a face-value change re-issues
the ISIN under an unchanged symbol (`20MICRONS` is `INE144J01019` in 2011, `INE144J01027` in 2016).

**The resolver instead walks the exchange's own chain backwards from a row that carries an ISIN.**
Every NSE bhavcopy row states `PREVCLOSE` — the exchange's own record of *this security's* previous
close. On 2011-06-22 every row has an ISIN. For a symbol trading that day, its 2011-06-21 row is the
same security exactly when that day's `PREVCLOSE` equals the 2011-06-21 `CLOSE`; and the same test
links 06-21 to 06-20, and so on back to 2006. Each step is admitted only when all of these hold:

1. **Continuity.** At most `max_gap` sessions with no row between the two rows (a suspension may
   pause the chain; a long silence is where a symbol gets handed to someone else).
2. **The exchange's own link.** `PREVCLOSE(later) == CLOSE(earlier)` exactly. A difference is an
   *adjustment break* (the exchange adjusts PREVCLOSE on an ex-date, catalogue §A8): a bonus or a
   rights issue keeps the ISIN, but a split or consolidation re-issues it. Breaks are handled by
   the ISIN's own serial digits (rule 4).
3. **No symbol event in between.** NSE's `symbolchange.csv`: a rename *into* the symbol inside the
   step means the earlier row belongs to the old symbol (the walk follows the rename to it, and
   tests the link across it); a rename *out of* the symbol inside the step means the symbol was
   vacated and the earlier row is someone else's (the walk stops).
4. **The ISIN existed then.** An Indian equity ISIN is `IN` + `E` + 4-char issuer + `01` (equity) +
   2-digit serial + check digit, and the serial counts re-issues: `…0101x` is the issuer's first
   equity ISIN and has never been re-issued, so no step back in time can cross its creation (every
   `isin_lineage` edge in the store goes from a lower serial to a higher one). For such an ISIN an
   adjustment break is admitted when it is downward, small-gapped and corroborated by a stored
   corporate action of the issuer in the step. For a re-issued ISIN (serial ≥ 02) a break may *be*
   the re-issue, so the walk stops at it unless a stored action shows a bonus/rights/dividend on
   that ex-date and no split or face-value change; it also stops at any stored split/face-value
   change of the issuer, link or no link. Rows within `reissue_settlement` sessions after such a
   boundary are refused too: the exchange kept printing the old ISIN for a session or two after a
   split's ex-date. (`require_reissue_witness` is stricter still — nothing admitted for a re-issued
   ISIN unless a stored split/FV change dates the re-issue; off by default, see the gate note.)
5. **An ordinary session.** The exchange did not always adjust PREVCLOSE on an ex-date (GRUH,
   2012-07-24: PREVCLOSE 754.75, CLOSE 157.10, new ISIN the next session), so a session whose
   CLOSE/PREVCLOSE leaves `[min_session_move, max_session_move]` is a boundary and is answered
   exactly as an adjustment break is.

Measured on the ISIN era with the ISINs hidden (`pre_isin_promote validate`, anchors at 2013-01-01,
2014-07-01 and 2016-01-01): 0 wrong of 550,363 / 992,280 / 1,414,788 admitted rows. Without rule 5
the same run admitted 707 / 3,239 / 7,560 wrong rows; without the settlement margin, 39 / 60 / 111.

A row reached by two chains naming different ISINs is admitted by neither. Every row not admitted
keeps an enumerated `PreIsinReason`, so the quarantine states *why* rather than *that*.

What it does: a pure function over evidence — rows, symbol changes, stored corporate actions.
What it assumes: `rows` hold every chain-series row for the sessions it covers (a hole in the input
looks like a gap, which only ever stops a chain), and that ISIN-bearing rows are as published.
What it never does: read a current-day listing, match on company names, infer a rename from price
coincidence, fetch, or write. It never assigns an ISIN that was not stated by the exchange on a row
the chain reached.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

__all__ = [
    "AUXILIARY_SERIES",
    "CHAIN_SERIES",
    "ActionEvidence",
    "ChainRow",
    "ChainSummary",
    "PreIsinReason",
    "Rename",
    "Resolution",
    "ResolveReport",
    "ResolverConfig",
    "is_first_issue",
    "issuer_of",
    "resolve",
]

#: Series whose rows form a security's daily chain: the normal market and its trade-to-trade
#: variants. They are mutually exclusive on a session (measured on 2011-06-22..2016-09-01: BE and
#: BZ never coexist with EQ for one symbol) and all carry the equity ISIN.
CHAIN_SERIES: Final[frozenset[str]] = frozenset({"EQ", "BE", "BZ"})

#: Series that trade the *same* equity ISIN alongside the chain row — block deals, the
#: institutional window, and BT. Measured in the ISIN era: IL 1,012/1,012 and BL 507/507 rows carry
#: the EQ row's ISIN whenever an EQ row exists. They ride on the session's admitted chain row and
#: are never a chain link themselves (their PREVCLOSE is not the security's close history).
AUXILIARY_SERIES: Final[frozenset[str]] = frozenset({"BL", "IL", "BT"})

#: Stored corporate-action types that change face value and so re-issue the ISIN.
_REISSUING: Final[frozenset[str]] = frozenset({"SPLIT", "FACE_VALUE_CHANGE"})

#: Stored corporate-action types that adjust PREVCLOSE on an unchanged ISIN.
_NON_REISSUING: Final[frozenset[str]] = frozenset({"BONUS", "RIGHTS", "DIVIDEND"})


class PreIsinReason(StrEnum):
    """Why a pre-ISIN row was, or was not, admitted. Stored verbatim in the quarantine reason."""

    #: Admitted: reached by one chain whose every step passed the four rules.
    RESOLVED = "resolved"
    #: The series is not an equity chain/auxiliary series (debentures, warrants, DRs, …: they carry
    #: their own ISINs, which no E1 row can be linked to).
    SERIES_OUT_OF_SCOPE = "series_out_of_scope"
    #: No chain ever reached the row's symbol: it stopped trading (or was renamed without a
    #: recorded change) before any row that states an ISIN. The survivorship-free remainder.
    NO_ISIN_WITNESS = "no_isin_witness"
    #: A chain reached the symbol but stopped before this row; the stop's own reason is recorded
    #: on the chain and in the run report.
    CHAIN_GAP = "chain_gap"
    CHAIN_ADJUSTMENT_BREAK = "chain_adjustment_break"
    CHAIN_DISCONTINUITY = "chain_discontinuity"
    CHAIN_SYMBOL_VACATED = "chain_symbol_vacated"
    CHAIN_REISSUE = "chain_reissue"
    REISSUE_UNDATED = "reissue_undated"
    AMBIGUOUS_SERIES = "ambiguous_series"
    #: Two chains reached the row with different ISINs.
    CONFLICTING_CLAIMS = "conflicting_claims"
    #: An auxiliary-series row on a session whose chain row was not admitted.
    NO_CHAIN_ROW_THAT_SESSION = "no_chain_row_that_session"


@dataclass(frozen=True, slots=True)
class ChainRow:
    """The evidence one bhavcopy row contributes: who, when, and the two prices that link it.

    `isin` is the ISIN the row *stated* — `None` for every E1 row, set for the ISIN-era rows that
    anchor chains.
    """

    symbol: str
    series: str
    trade_date: date
    close: Decimal
    prev_close: Decimal
    isin: str | None = None


@dataclass(frozen=True, slots=True, order=True)
class Rename:
    """NSE `symbolchange.csv`: `old` became `new`, in force from `effective` inclusive."""

    effective: date
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class ActionEvidence:
    """One stored corporate action: the issuer it concerns, its ex-date and its type."""

    issuer: str
    ex_date: date
    action_type: str


@dataclass(frozen=True, slots=True)
class ResolverConfig:
    """The resolver's tunables. Every one only ever widens what is *refused* as it tightens."""

    #: Sessions with no row a step may skip (a suspension) before the chain stops.
    max_gap: int = 20
    #: For a first-issue ISIN, the widest gap across which an adjustment break may be admitted.
    max_gap_on_break: int = 5
    #: Lowest PREVCLOSE/CLOSE ratio admitted across a break (a 1:19 bonus is 0.05).
    min_break_ratio: Decimal = Decimal("0.04")
    #: The band a session's CLOSE/PREVCLOSE must sit in to be an ordinary market move; outside it
    #: the session is a boundary (a 2:1 split or 1:1 bonus with PREVCLOSE left unadjusted reads
    #: ~0.5, a 3:2 split ~0.67, a 1:2 consolidation ~2). Price bands are ±20% for most names.
    min_session_move: Decimal = Decimal("0.7")
    max_session_move: Decimal = Decimal("1.45")
    #: Admit rows of a re-issued ISIN only when a stored split/FV change dates the re-issue.
    require_reissue_witness: bool = False
    #: Sessions after a re-issue boundary whose rows are refused. Measured in the ISIN era: on a
    #: split the exchange adjusts PREVCLOSE on the ex-date but keeps publishing the *old* ISIN for
    #: one to two sessions more (TITAN 2011-06-23, CRISIL 2011-09-28/29), so a boundary found by a
    #: price break or an ex-date is up to two sessions early. Rows inside the margin are refused.
    reissue_settlement: int = 5
    #: How many sessions after the first ISIN-era session a symbol's first ISIN row may come and
    #: still anchor a chain. Beyond `max_gap` the gap rule refuses the step anyway.
    anchor_window: int = 20


@dataclass(frozen=True, slots=True)
class Resolution:
    """The verdict on one row: an ISIN with the chain that proved it, or a reason with none."""

    symbol: str
    series: str
    trade_date: date
    reason: PreIsinReason
    isin: str | None = None
    #: The ISIN-era session and symbol the proving chain started from.
    anchor_date: date | None = None
    anchor_symbol: str | None = None


@dataclass(frozen=True, slots=True)
class ChainSummary:
    """One chain, for the run report: where it started, how far it reached, why it stopped.

    `boundary_link` is how the anchor's own row linked to the security's last pre-ISIN row:
    `exact` (PREVCLOSE equalled that CLOSE), `admitted_break`, or `None` when the chain admitted
    nothing. `stop` is `None` when the chain walked to the first session it was given.
    """

    isin: str
    anchor_symbol: str
    anchor_date: date
    admitted: int
    earliest: date | None
    boundary_link: str | None
    stop: str | None


@dataclass(frozen=True, slots=True)
class ResolveReport:
    """`resolve`'s output: a verdict per pre-ISIN row and a summary per chain."""

    resolutions: tuple[Resolution, ...]
    chains: tuple[ChainSummary, ...]

    def stops(self) -> dict[str, int]:
        """Chains per stop reason; `reached_start` for those that walked to the first session."""
        out: dict[str, int] = defaultdict(int)
        for chain in self.chains:
            out[chain.stop or "reached_start"] += 1
        return dict(out)


@dataclass(slots=True)
class _Chain:
    isin: str
    anchor: ChainRow
    admitted: list[ChainRow] = field(default_factory=list)
    stop: PreIsinReason | None = None
    stop_symbol: str | None = None
    boundary_link: str | None = None


def issuer_of(isin: str) -> str:
    """The issuer part of an Indian ISIN (`INE144J01027` → `INE144J`): stable across re-issues."""
    return isin[:7]


def is_first_issue(isin: str) -> bool:
    """Whether an ISIN is its issuer's first equity ISIN — `INE…01` + serial `01` — never re-issued.

    Only `INE` equity ISINs are classified; anything else (ETF units `INF…`, …) answers `False`
    and so gets the stricter re-issued treatment.
    """
    return isin.startswith("INE") and isin[7:9] == "01" and isin[9:11] == "01"


def resolve(
    rows: Iterable[ChainRow],
    *,
    first_isin_session: date,
    renames: Iterable[Rename] = (),
    actions: Iterable[ActionEvidence] = (),
    config: ResolverConfig | None = None,
) -> ResolveReport:
    """Resolve every row dated before `first_isin_session`; return verdicts and chain summaries.

    `rows` must include the ISIN-era rows that anchor the chains (from `first_isin_session` for
    `config.anchor_window` sessions) as well as the pre-ISIN rows. Rows dated on/after
    `first_isin_session` are evidence only and get no verdict.
    """
    cfg = config if config is not None else ResolverConfig()
    all_rows = list(rows)
    sessions = sorted({row.trade_date for row in all_rows})
    session_index = {day: i for i, day in enumerate(sessions)}
    first_idx = bisect_left(sessions, first_isin_session)

    chain_rows: dict[str, dict[date, list[ChainRow]]] = defaultdict(lambda: defaultdict(list))
    for row in all_rows:
        if row.series in CHAIN_SERIES:
            chain_rows[row.symbol][row.trade_date].append(row)
    dates_of = {symbol: sorted(by_day) for symbol, by_day in chain_rows.items()}

    into: dict[str, list[Rename]] = defaultdict(list)
    out_of: dict[str, list[Rename]] = defaultdict(list)
    for rename in sorted(renames):
        into[rename.new].append(rename)
        out_of[rename.old].append(rename)

    by_issuer: dict[str, list[ActionEvidence]] = defaultdict(list)
    for action in actions:
        by_issuer[action.issuer].append(action)

    chains = _anchor_chains(chain_rows, dates_of, sessions, first_idx, cfg)
    for chain in chains:
        _walk(chain, chain_rows, dates_of, session_index, into, out_of, by_issuer, cfg)
        if not is_first_issue(chain.isin):
            if chain.stop is not None:
                boundary = chain.admitted[-1].trade_date if chain.admitted else None
                if boundary is not None:
                    _settle(chain, boundary, sessions, cfg.reissue_settlement)
            if cfg.require_reissue_witness:
                _require_witness(chain, by_issuer, sessions, cfg.reissue_settlement)

    summaries = tuple(
        ChainSummary(
            isin=chain.isin,
            anchor_symbol=chain.anchor.symbol,
            anchor_date=chain.anchor.trade_date,
            admitted=len(chain.admitted),
            earliest=chain.admitted[-1].trade_date if chain.admitted else None,
            boundary_link=chain.boundary_link if chain.admitted else None,
            stop=chain.stop.value if chain.stop is not None else None,
        )
        for chain in chains
    )
    return ResolveReport(
        resolutions=_verdicts(all_rows, chains, first_isin_session), chains=summaries
    )


def _anchor_chains(
    chain_rows: Mapping[str, Mapping[date, list[ChainRow]]],
    dates_of: Mapping[str, list[date]],
    sessions: Sequence[date],
    first_idx: int,
    cfg: ResolverConfig,
) -> list[_Chain]:
    """One chain per symbol whose first ISIN-era chain row falls inside the anchor window."""
    window_end = sessions[min(first_idx + cfg.anchor_window, len(sessions) - 1)]
    first_day = sessions[first_idx] if first_idx < len(sessions) else None
    chains: list[_Chain] = []
    if first_day is None:
        return chains
    for symbol, days in dates_of.items():
        start = bisect_left(days, first_day)
        if start >= len(days) or days[start] > window_end:
            continue
        candidates = chain_rows[symbol][days[start]]
        isins = {row.isin for row in candidates}
        if len(candidates) != 1 or None in isins:
            continue  # two chain series in one session, or an ISIN-era row without an ISIN
        anchor = candidates[0]
        assert anchor.isin is not None
        chains.append(_Chain(isin=anchor.isin, anchor=anchor))
    return chains


def _walk(
    chain: _Chain,
    chain_rows: Mapping[str, Mapping[date, list[ChainRow]]],
    dates_of: Mapping[str, list[date]],
    session_index: Mapping[date, int],
    into: Mapping[str, list[Rename]],
    out_of: Mapping[str, list[Rename]],
    by_issuer: Mapping[str, list[ActionEvidence]],
    cfg: ResolverConfig,
) -> None:
    """Step back from the anchor, one admitted row at a time, until a rule refuses the step."""
    cursor = chain.anchor
    first_issue = is_first_issue(chain.isin)
    issuer = issuer_of(chain.isin)
    while True:
        handle = _predecessor(cursor, dates_of, into)
        if handle is None:
            chain.stop = None  # walked to the symbol's first row in the input: nothing to refuse
            return
        prev_rows = chain_rows[handle[0]][handle[1]]
        if len(prev_rows) != 1:
            chain.stop, chain.stop_symbol = PreIsinReason.AMBIGUOUS_SERIES, handle[0]
            return
        prev = prev_rows[0]
        if _vacated(prev, cursor, out_of):
            chain.stop, chain.stop_symbol = PreIsinReason.CHAIN_SYMBOL_VACATED, prev.symbol
            return
        gap = session_index[cursor.trade_date] - session_index[prev.trade_date] - 1
        if gap > cfg.max_gap:
            chain.stop, chain.stop_symbol = PreIsinReason.CHAIN_GAP, prev.symbol
            return
        step_actions = [
            a for a in by_issuer.get(issuer, ()) if prev.trade_date < a.ex_date <= cursor.trade_date
        ]
        reissue_in_step = any(a.action_type in _REISSUING for a in step_actions)
        if not first_issue and reissue_in_step:
            chain.stop, chain.stop_symbol = PreIsinReason.CHAIN_REISSUE, prev.symbol
            return
        if cursor.prev_close != prev.close and not _break_admitted(
            _ratio(cursor.prev_close, prev.close),
            cursor,
            gap,
            step_actions,
            first_issue=first_issue,
            cfg=cfg,
        ):
            chain.stop, chain.stop_symbol = PreIsinReason.CHAIN_ADJUSTMENT_BREAK, prev.symbol
            return
        jump = _ratio(cursor.close, cursor.prev_close)
        if (
            not first_issue
            and not (cfg.min_session_move <= jump <= cfg.max_session_move)
            and not _break_admitted(
                jump, cursor, gap, step_actions, first_issue=first_issue, cfg=cfg
            )
        ):
            # The exchange did not always adjust PREVCLOSE on an ex-date: GRUH's 2012-07-24 row
            # reads PREVCLOSE 754.75 (yesterday's close, unadjusted) and CLOSE 157.10, and the
            # next session carries a new ISIN. A session "move" outside any plausible band is the
            # same boundary as an adjustment break and is answered the same way.
            chain.stop, chain.stop_symbol = PreIsinReason.CHAIN_DISCONTINUITY, prev.symbol
            return
        if prev.isin is not None and prev.isin != chain.isin:
            # Only possible when the input mixes eras: the chain reached a row stating another
            # ISIN. That is the evidence that the symbol was re-issued, not a link to follow.
            chain.stop, chain.stop_symbol = PreIsinReason.CHAIN_REISSUE, prev.symbol
            return
        if prev.isin is None:
            if not chain.admitted:
                chain.boundary_link = (
                    "exact" if cursor.prev_close == prev.close else "admitted_break"
                )
            chain.admitted.append(prev)
        cursor = prev


def _predecessor(
    cursor: ChainRow,
    dates_of: Mapping[str, list[date]],
    into: Mapping[str, list[Rename]],
) -> tuple[str, date] | None:
    """The `(symbol, session)` of the same security's chain row before `cursor`, across a rename.

    The latest rename into `cursor.symbol` in force by `cursor.trade_date` bounds the search: rows
    of that symbol before it belong to whoever held the symbol then. When the symbol has no row
    since the rename, the security's previous row is under the old symbol, before the rename.
    """
    symbol, day = cursor.symbol, cursor.trade_date
    floor: date | None = None
    rename: Rename | None = None
    for candidate in into.get(symbol, ()):
        if candidate.effective <= day:
            floor, rename = candidate.effective, candidate
    days = dates_of.get(symbol, [])
    i = bisect_left(days, day) - 1
    if i >= 0 and (floor is None or days[i] >= floor):
        return symbol, days[i]
    if rename is None:
        return None
    old_days = dates_of.get(rename.old, [])
    j = bisect_left(old_days, rename.effective) - 1
    if j < 0:
        return None
    return rename.old, old_days[j]


def _vacated(prev: ChainRow, cursor: ChainRow, out_of: Mapping[str, list[Rename]]) -> bool:
    """Whether `prev.symbol` was renamed away to anything but `cursor.symbol` inside the step."""
    for rename in out_of.get(prev.symbol, ()):
        if prev.trade_date < rename.effective <= cursor.trade_date and rename.new != cursor.symbol:
            return True
    return False


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    """`numerator / denominator`, with a zero denominator read as an infinite (refused) ratio."""
    return numerator / denominator if denominator > 0 else Decimal("Infinity")


def _break_admitted(
    ratio: Decimal,
    cursor: ChainRow,
    gap: int,
    step_actions: Sequence[ActionEvidence],
    *,
    first_issue: bool,
    cfg: ResolverConfig,
) -> bool:
    """Whether a price boundary (adjustment break or discontinuity) is a same-ISIN event."""
    if not (cfg.min_break_ratio <= ratio < 1):
        return False  # upward or extreme: a consolidation, or not this security's history
    if first_issue:
        # The ISIN cannot have changed (rule 4), so the only question is "same security?", and
        # the exchange answered it by adjusting *this* security's previous close.
        return gap <= cfg.max_gap_on_break
    on_ex_date = [a for a in step_actions if a.ex_date == cursor.trade_date]
    return (
        gap <= cfg.max_gap_on_break
        and any(a.action_type in _NON_REISSUING for a in on_ex_date)
        and not any(a.action_type in _REISSUING for a in step_actions)
    )


def _settle(chain: _Chain, boundary: date, sessions: Sequence[date], margin: int) -> None:
    """Refuse admitted rows within `margin` sessions on/after a re-issue `boundary`."""
    start = bisect_left(sessions, boundary)
    limit = sessions[start + margin] if start + margin < len(sessions) else None
    kept = [row for row in chain.admitted if limit is not None and row.trade_date >= limit]
    if len(kept) != len(chain.admitted):
        chain.admitted = kept
        if chain.stop is None:
            chain.stop = PreIsinReason.REISSUE_UNDATED


def _require_witness(
    chain: _Chain,
    by_issuer: Mapping[str, list[ActionEvidence]],
    sessions: Sequence[date],
    margin: int,
) -> None:
    """For a re-issued ISIN, keep only rows a stored split/FV change dates on or after.

    The latest stored re-issuing action at or before the anchor is the best dated witness of when
    the ISIN came to exist. Rows before it are dropped back to `REISSUE_UNDATED`; with no witness
    at all, the whole chain is.
    """
    witnesses = sorted(
        a.ex_date
        for a in by_issuer.get(issuer_of(chain.isin), ())
        if a.action_type in _REISSUING and a.ex_date <= chain.anchor.trade_date
    )
    if not witnesses:
        if chain.admitted:
            chain.admitted = []
            chain.stop = PreIsinReason.REISSUE_UNDATED
        return
    before = len(chain.admitted)
    chain.admitted = [row for row in chain.admitted if row.trade_date >= witnesses[-1]]
    _settle(chain, witnesses[-1], sessions, margin)
    if len(chain.admitted) != before:
        chain.stop = PreIsinReason.REISSUE_UNDATED


def _verdicts(
    all_rows: Sequence[ChainRow], chains: Sequence[_Chain], first_isin_session: date
) -> tuple[Resolution, ...]:
    """Turn admitted chain rows into per-row verdicts, refusing every row two ISINs claim."""
    claims: dict[tuple[str, date], set[str]] = defaultdict(set)
    proof: dict[tuple[str, date], _Chain] = {}
    reached: dict[str, PreIsinReason] = {}
    for chain in chains:
        for row in chain.admitted:
            claims[(row.symbol, row.trade_date)].add(chain.isin)
            proof[(row.symbol, row.trade_date)] = chain
        symbols = {chain.anchor.symbol, *(row.symbol for row in chain.admitted)}
        if chain.stop_symbol is not None:
            symbols.add(chain.stop_symbol)
        for symbol in symbols:
            if chain.stop is not None:
                reached.setdefault(symbol, chain.stop)

    out: list[Resolution] = []
    for row in all_rows:
        if row.trade_date >= first_isin_session:
            continue
        key = (row.symbol, row.trade_date)
        isins = claims.get(key, set())
        isin: str | None = None
        proving: _Chain | None = None
        if row.series not in CHAIN_SERIES and row.series not in AUXILIARY_SERIES:
            reason = PreIsinReason.SERIES_OUT_OF_SCOPE
        elif len(isins) > 1:
            reason = PreIsinReason.CONFLICTING_CLAIMS
        elif isins:
            reason, isin, proving = PreIsinReason.RESOLVED, next(iter(isins)), proof[key]
        elif row.series in AUXILIARY_SERIES:
            reason = PreIsinReason.NO_CHAIN_ROW_THAT_SESSION
        else:
            reason = reached.get(row.symbol, PreIsinReason.NO_ISIN_WITNESS)
        out.append(
            Resolution(
                symbol=row.symbol,
                series=row.series,
                trade_date=row.trade_date,
                reason=reason,
                isin=isin,
                anchor_date=proving.anchor.trade_date if proving is not None else None,
                anchor_symbol=proving.anchor.symbol if proving is not None else None,
            )
        )
    return tuple(out)
