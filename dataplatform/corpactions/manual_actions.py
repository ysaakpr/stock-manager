"""Curated, sourced corporate actions no feed carries — and the price moves that are not actions.

Both corporate-action feeds the platform ingests cover listed equity and reach back only so far,
so some events that move an ISIN's share basis are missing from the store: a 2013 bonus before
either feed's history, an ETF unit split (no equity feed lists a mutual-fund unit), or a split
filed under an ISIN a reissue retired. `corpactions.implied` recovers those L1 itself shows as a
clean multiple with the volume moving with it. The rest — a bonus on a day the market also fell
10%, a bonus and a split on one ex-date, a split whose ex-day printed at the +20% band — leave an
unexplained step in L2 that the `l2_continuity` check fails on. Their terms *are* published, in
the exchange's daily book-closure file (``Bc<ddmmyy>.csv`` in the NSE PR bundle, already in L0)
and its announcement text. They are transcribed by hand into ``manual_actions.yaml`` beside this
module, each next to the quoted line that states it, and read back here.

Two kinds of row:

* **`actions`** — a SPLIT or BONUS (composed into the factor chain by the L2 materializer, so the
  adjusted series carries no step for it) or a DEMERGER / SCHEME_OF_ARRANGEMENT (a structural
  break: the level series keeps the gap by convention, the return series bridges it, and the
  continuity check classifies the step `STRUCTURAL`).
* **`explained_moves`** — a step that is a genuine market move, not a corporate action (the
  2013 NSEL crisis at Financial Technologies, YES Bank's 2020 moratorium). These get no factor:
  inventing one would erase a real loss from every backtest. They are an explicit allowlist the
  continuity check honours on the exact ISIN and session only, each with its reason and source.
* **Restored dividends** — a DIVIDEND row in `actions` is a record one CA feed *did* publish,
  in its own L0 payload, that the store's one-row-per-kind key could not hold: Patanjali's NSE
  filed two Interim records on 2026-08-21 (₹0.80 and ₹1.50), and only the first reached
  `corporate_actions`. It names the `feed` it restores, and its first source is that feed's L0
  object. It never becomes a factor and is never booked on its own: reconciliation stands it on
  its feed's side (`reconcile._feed`) so the event it completes can be matched — there, ₹0.80 +
  ₹1.50 against BSE's one ₹2.30 record — and the collapse drops it unless it completed one. It
  is kept apart from the price events (`ManualActions.dividends`), so no L2 path that composes
  `actions` into a factor chain ever sees it.
* **Unsourced steps** — rows in the same list whose kind is not `MARKET_MOVE`: a share-basis step
  whose terms no L0 object states. `UNSOURCED_ACTION` is one an exchange record shows to be a
  corporate action (EIH's 2006 ex-date lists a second, blank-purpose event beside its bonus);
  `UNSOURCED_PRICE_STEP` is one only the exchange's own bhavcopy evidences (Shah Alloys opened
  at half its prior close, and no corporate-action record of 2006 survives in L0). Neither gets
  a factor: that would mean inventing a ratio. Unlike a market move, the step is *not* a
  return, so the level series is wrong across it. The continuity check therefore classes it
  `UNSOURCED` with a WARN finding rather than calling it explained, and the query layer
  quarantines the ISIN's bars before the step (`unsourced_windows`, D22) so no backtest or
  decision holds or signals across it. It is the row to replace when a source arrives.

**Why a reviewed file and not rows in ``corporate_actions``** — the same reason as
`merger_terms`: the table's rows are produced by parsers from L0 and reconciled across two
exchanges; a hand transcription is neither, and putting it there would make a reconciled-looking
row nobody's parser can re-derive (invariant #1). A YAML file is reviewed in the diff and
re-derivable from the L0 objects it names. Every curated action carries `MANUAL_SOURCE` as its
source, so it can never be mistaken for a feed's.

**PIT.** `knowable_date` is the dissemination date of the earliest L0 source stating both the
terms and the ex-date, never the day it was transcribed. L2 back-adjustment is retroactive by
design (§4.3); nothing on the decision path reads this module.

**A derived ex-date (T+1).** One term may be filled, by one rule: a SPLIT (a consolidation is a
SPLIT with the face value rising) whose source states the terms and a record date but leaves the
ex-date blank may carry ``ex_date_derived: T1_RECORD_DATE`` instead of an ``ex_date``. The loader
then sets the ex-date to the record date (`corpactions.ex_date_rule`: T+1 settlement, ex = record,
for a record date that is a session on or after 2023-01-27) and keeps the rule on the row
(`CuratedAction.ex_date_rule`), so a derived date can never pass for a stated one. A row that
states an ex-date *and* names the rule, or whose record date is before 2023-01-27 or not a
session, is refused.

What this module never does: fetch, infer a ratio, or fill a missing term other than that one
ex-date. An event no source states stays out of the file, and its step stays UNEXPLAINED until one
does.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml

from dataplatform.corpactions.ex_date_rule import ExDateRule, derive_ex_date
from dataplatform.corpactions.merger_terms import TermSource, parse_term_sources
from dataplatform.corpactions.taxonomy import (
    ActionType,
    DividendKind,
    DividendTerms,
    FaceValueTerms,
    RatioTerms,
    Terms,
    UnquantifiedTerms,
)
from dataplatform.ingest.models import ISIN_PATTERN

if TYPE_CHECKING:
    from dataplatform.ingest.corp_actions import CorporateAction

__all__ = [
    "MANUAL_ACTIONS_PATH",
    "MANUAL_SOURCE",
    "MARKET_MOVE",
    "UNSOURCED_KINDS",
    "CuratedAction",
    "ExplainedMove",
    "ManualActions",
    "ManualActionsError",
    "default_manual_actions",
    "load_manual_actions",
]

MANUAL_ACTIONS_PATH: Final[Path] = Path(__file__).with_name("manual_actions.yaml")

#: The source id a curated action carries — never a feed's, so it can never be mistaken for one.
MANUAL_SOURCE: Final = "manual_curated"

#: The action types a curated row may state. Price events get a factor; the rest are breaks.
_PRICE_EVENTS: Final = frozenset({ActionType.SPLIT, ActionType.BONUS})
_BREAKS: Final = frozenset({ActionType.DEMERGER, ActionType.SCHEME_OF_ARRANGEMENT})

#: The feeds whose dropped dividend records a DIVIDEND row may restore. Literal ids rather than the
#: ingest modules' constants: this module sits below `ingest` and must not import it at load time.
_RESTORABLE_FEEDS: Final = frozenset({"nse_corp_actions", "bse_corp_actions"})

#: The one explained-move kind that is a real change in price, and so needs no quarantine.
MARKET_MOVE: Final = "MARKET_MOVE"

#: Share-basis steps whose terms no L0 object states (see the module docstring). No factor is
#: applied (this file never infers a ratio); the step is reported and its pre-step bars quarantined.
#: `UNSOURCED_ACTION`: an exchange record shows the event. `UNSOURCED_PRICE_STEP`: only the
#: exchange's price file does.
UNSOURCED_KINDS: Final = frozenset({"UNSOURCED_ACTION", "UNSOURCED_PRICE_STEP"})

#: What an explained move may be classified as; a closed set on purpose.
_MOVE_KINDS: Final = frozenset({MARKET_MOVE}) | UNSOURCED_KINDS

_ISIN: Final = re.compile(ISIN_PATTERN)


class ManualActionsError(ValueError):
    """The curated file is malformed — a missing source or check date, a bad ISIN, a float ratio."""


@dataclass(frozen=True, slots=True)
class CuratedAction:
    """One hand-transcribed corporate action, with where it is stated and when that was checked.

    `isin` is the ISIN whose L2 partition carries the event: the lineage survivor when the
    ex-date bars sit under an ISIN a reissue retired, which `filed_against_isin` then names.
    """

    isin: str
    company: str
    action_type: ActionType
    terms: Terms
    ex_date: date
    knowable_date: date
    checked: date
    sources: tuple[TermSource, ...]
    record_date: date | None = None
    filed_against_isin: str | None = None
    note: str | None = None
    #: A restored dividend only: the feed whose record it is, and its component in that feed's
    #: day (`INTERIM#2` for a second Interim the key could not hold beside the first).
    feed: str | None = None
    component: str = ""
    #: Set only when the source left the ex-date blank and it was derived from the stated record
    #: date (`corpactions.ex_date_rule`); `None` means the source stated `ex_date` itself.
    ex_date_rule: ExDateRule | None = None

    @property
    def is_price_event(self) -> bool:
        return self.action_type in _PRICE_EVENTS

    @property
    def is_restored_dividend(self) -> bool:
        return self.action_type is ActionType.DIVIDEND

    def as_action(self) -> CorporateAction:
        """This row as a `CorporateAction` under `MANUAL_SOURCE`, for the factor math."""
        from dataplatform.ingest.corp_actions import CorporateAction, dividend_component

        first = self.sources[0]
        return CorporateAction(
            isin=self.isin,
            filed_against_isin=self.filed_against_isin,
            ex_date=self.ex_date,
            action_type=self.action_type,
            terms=self.terms,
            source=MANUAL_SOURCE,
            raw_text=first.quote,
            knowable_date=self.knowable_date,
            record_date=self.record_date,
            source_ref=MANUAL_ACTIONS_PATH.name,
            l0_key=first.l0_key,
            component=self.component or dividend_component(self.terms),
            ex_date_rule=None if self.ex_date_rule is None else self.ex_date_rule.value,
        )


@dataclass(frozen=True, slots=True)
class ExplainedMove:
    """A documented step on `trade_date`: a real move (`MARKET_MOVE`) or an unsourced basis step."""

    isin: str
    company: str
    trade_date: date
    kind: str
    reason: str
    checked: date
    sources: tuple[TermSource, ...]

    @property
    def is_unsourced(self) -> bool:
        """A share-basis step with unstated terms, not a real move (`UNSOURCED_KINDS`)."""
        return self.kind in UNSOURCED_KINDS


@dataclass(frozen=True, slots=True)
class ManualActions:
    """The whole curated file, indexed by ISIN."""

    actions: tuple[CuratedAction, ...]
    explained_moves: tuple[ExplainedMove, ...]
    #: Restored dividend records (module docstring), kept out of `actions` so no factor path sees
    #: them; reconciliation reads them through `restored_dividends`.
    dividends: tuple[CuratedAction, ...] = ()
    _by_isin: Mapping[str, tuple[CuratedAction, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_isin: dict[str, list[CuratedAction]] = {}
        for row in self.actions:
            by_isin.setdefault(row.isin, []).append(row)
        object.__setattr__(self, "_by_isin", {k: tuple(v) for k, v in by_isin.items()})

    def actions_for(self, isin: str) -> tuple[CuratedAction, ...]:
        """`isin`'s curated actions in ex-date order; empty for the ~all ISINs that have none."""
        return self._by_isin.get(isin, ())

    def restored_dividends(
        self,
        *,
        isin: str | None = None,
        ex_after: date | None = None,
        ex_through: date | None = None,
    ) -> tuple[CorporateAction, ...]:
        """The restored dividend records as `CorporateAction`s under `MANUAL_SOURCE`.

        Bounded like `reconcile.load_reconciled_actions` (``ex_after < ex_date <= ex_through``,
        one ISIN when given), so the collapse sees exactly the restored records beside the rows it
        loaded. What it never does: hand one to a factor — that is the caller's contract, and
        `reconcile` keeps it by never booking a restored record on its own.
        """
        return tuple(
            row.as_action()
            for row in self.dividends
            if (isin is None or row.isin == isin)
            and (ex_after is None or row.ex_date > ex_after)
            and (ex_through is None or row.ex_date <= ex_through)
        )

    def structural_dates(self) -> dict[str, tuple[date, ...]]:
        """ISIN → ex-dates of its curated structural breaks (demergers, schemes)."""
        out: dict[str, list[date]] = {}
        for row in self.actions:
            if not row.is_price_event:
                out.setdefault(row.isin, []).append(row.ex_date)
        return {k: tuple(v) for k, v in out.items()}

    def explained_dates(self) -> dict[str, tuple[date, ...]]:
        """ISIN → the sessions whose step is a documented market move (never an unsourced step)."""
        return self._move_dates(lambda move: not move.is_unsourced)

    def unsourced_dates(self) -> dict[str, tuple[date, ...]]:
        """ISIN → the sessions of its unsourced share-basis steps (`UNSOURCED_KINDS`)."""
        return self._move_dates(lambda move: move.is_unsourced)

    def unsourced_windows(self) -> dict[str, date]:
        """ISIN → its first admissible session: the latest unsourced step's own session (D22).

        Every bar of the ISIN dated *before* this session sits on the far side of a share-basis
        step no factor removes, so a reader that admits it can hold or signal across a phantom
        return. The step session itself is admitted: it is the first bar of the basis the ISIN
        trades on from then on. The query layer (`dataplatform.query.PriceQuarantine`) applies it.
        """
        return {isin: max(days) for isin, days in self.unsourced_dates().items()}

    def _move_dates(self, keep: Callable[[ExplainedMove], bool]) -> dict[str, tuple[date, ...]]:
        out: dict[str, list[date]] = {}
        for move in self.explained_moves:
            if keep(move):
                out.setdefault(move.isin, []).append(move.trade_date)
        return {k: tuple(v) for k, v in out.items()}


def load_manual_actions(path: Path = MANUAL_ACTIONS_PATH) -> ManualActions:
    """Read and validate the curated file.

    Raises `ManualActionsError` on a row without a source or a `checked` date, a malformed ISIN,
    an action type outside SPLIT/BONUS/DEMERGER/SCHEME_OF_ARRANGEMENT/DIVIDEND, a non-positive or
    float ratio or amount, a DIVIDEND row whose `feed` is not a CA feed or whose first source is
    not that feed's L0 object, a duplicate `(isin, ex_date, action_type)` (a dividend: `(isin,
    ex_date, component)`) or `(isin, trade_date)`, or an explained move on the very session a
    curated action is dated — the file cannot say both.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ManualActionsError(f"{path}: expected a mapping at the top level")
    parsed = sorted(
        (_action(row) for row in document.get("actions") or ()),
        key=lambda a: (a.isin, a.ex_date, a.action_type.value, a.component),
    )
    actions = tuple(a for a in parsed if not a.is_restored_dividend)
    dividends = tuple(a for a in parsed if a.is_restored_dividend)
    seen_dividends: set[tuple[str, date, str]] = set()
    for d in dividends:
        if (d.isin, d.ex_date, d.component) in seen_dividends:
            raise ManualActionsError(f"{d.isin} dividend {d.component} on {d.ex_date} twice")
        seen_dividends.add((d.isin, d.ex_date, d.component))
    moves = tuple(
        sorted(
            (_move(row) for row in document.get("explained_moves") or ()),
            key=lambda m: (m.isin, m.trade_date),
        )
    )
    seen: set[tuple[str, date, str]] = set()
    for a in actions:
        key = (a.isin, a.ex_date, a.action_type.value)
        if key in seen:
            raise ManualActionsError(f"{a.isin} {a.action_type} on {a.ex_date} is listed twice")
        seen.add(key)
    dated = {(a.isin, a.ex_date) for a in actions}
    seen_moves: set[tuple[str, date]] = set()
    for m in moves:
        if (m.isin, m.trade_date) in seen_moves:
            raise ManualActionsError(f"{m.isin} move on {m.trade_date} is listed twice")
        if (m.isin, m.trade_date) in dated:
            raise ManualActionsError(
                f"{m.isin} {m.trade_date}: an explained move cannot share a curated action's date"
            )
        seen_moves.add((m.isin, m.trade_date))
    return ManualActions(actions=actions, explained_moves=moves, dividends=dividends)


@cache
def default_manual_actions() -> ManualActions:
    """The repo's curated file, loaded once per process (the L2 rebuild asks per ISIN)."""
    return load_manual_actions()


# ── row parsing ──────────────────────────────────────────────────────────────────────────────────


def _action(row: dict[str, Any]) -> CuratedAction:
    isin = _isin(row, "isin")
    try:
        action_type = ActionType(_text(row, "action_type"))
    except ValueError as exc:
        raise ManualActionsError(f"{isin}: {exc}") from exc
    terms: Terms
    if action_type is ActionType.SPLIT:
        terms = FaceValueTerms(
            from_value=_positive(row, "from_value"), to_value=_positive(row, "to_value")
        )
        if terms.from_value == terms.to_value:
            raise ManualActionsError(f"{isin}: a split's face values must differ")
    elif action_type is ActionType.BONUS:
        terms = RatioTerms(
            new_shares=_positive(row, "new_shares"), held_shares=_positive(row, "held_shares")
        )
    elif action_type in _BREAKS:
        terms = UnquantifiedTerms()
    elif action_type is ActionType.DIVIDEND:
        return _restored_dividend(row, isin)
    else:
        raise ManualActionsError(f"{isin}: {action_type} is not a type this file may state")
    filed = row.get("filed_against_isin")
    record_date = None if row.get("record_date") is None else _date(row, "record_date")
    ex_date, rule = _ex_date(row, isin, action_type, record_date)
    return CuratedAction(
        isin=isin,
        company=_text(row, "company"),
        action_type=action_type,
        terms=terms,
        ex_date=ex_date,
        knowable_date=_date(row, "knowable_date"),
        checked=_date(row, "checked"),
        sources=_sources(row),
        record_date=record_date,
        filed_against_isin=None if filed is None else _isin(row, "filed_against_isin"),
        note=row.get("note"),
        ex_date_rule=rule,
    )


def _ex_date(
    row: dict[str, Any], isin: str, action_type: ActionType, record_date: date | None
) -> tuple[date, ExDateRule | None]:
    """The stated `ex_date`, or one derived by the named T+1 rule — never both, never a guess."""
    named = row.get("ex_date_derived")
    if named is None:
        return _date(row, "ex_date"), None
    try:
        rule = ExDateRule(str(named))
    except ValueError as exc:
        raise ManualActionsError(f"{isin}: ex_date_derived {named!r}: {exc}") from exc
    if row.get("ex_date") is not None:
        raise ManualActionsError(
            f"{isin}: a row states ex_date or derives it ({rule}), never both — a derived date "
            "must not look stated"
        )
    if action_type is not ActionType.SPLIT:
        raise ManualActionsError(f"{isin}: only a SPLIT may derive its ex-date, not {action_type}")
    if record_date is None:
        raise ManualActionsError(f"{isin}: {rule} needs the record_date the source states")
    # Lazy: the calendar lives in `ingest`, which this module must not import at load time.
    from dataplatform.ingest.calendar import trading_calendar

    derived = derive_ex_date(record_date, is_session=trading_calendar().is_session)
    if derived is None or derived.rule is not rule:
        raise ManualActionsError(
            f"{isin}: {rule} does not apply to record date {record_date} (before T+1 settlement "
            "on 2023-01-27, or not a trading session)"
        )
    return derived.ex_date, derived.rule


def _restored_dividend(row: dict[str, Any], isin: str) -> CuratedAction:
    """A DIVIDEND row: one feed's own record the store's key dropped, in that feed's words."""
    feed = _text(row, "feed")
    if feed not in _RESTORABLE_FEEDS:
        raise ManualActionsError(f"{isin}: feed {feed!r} is not one of {sorted(_RESTORABLE_FEEDS)}")
    try:
        kind = DividendKind(_text(row, "dividend_kind"))
    except ValueError as exc:
        raise ManualActionsError(f"{isin}: {exc}") from exc
    terms = DividendTerms(dividend_kind=kind, amount_inr=_positive(row, "amount_inr"))
    component = str(row.get("component") or kind.value)
    if component.split("#", 1)[0] != kind.value:
        raise ManualActionsError(f"{isin}: component {component!r} does not name {kind.value}")
    sources = _sources(row)
    if not sources[0].l0_key.startswith(f"{feed}/"):
        raise ManualActionsError(
            f"{isin}: a restored {feed} record must cite that feed's L0 object first, "
            f"not {sources[0].l0_key!r}"
        )
    return CuratedAction(
        isin=isin,
        company=_text(row, "company"),
        action_type=ActionType.DIVIDEND,
        terms=terms,
        ex_date=_date(row, "ex_date"),
        knowable_date=_date(row, "knowable_date"),
        checked=_date(row, "checked"),
        sources=sources,
        record_date=None if row.get("record_date") is None else _date(row, "record_date"),
        note=row.get("note"),
        feed=feed,
        component=component,
    )


def _move(row: dict[str, Any]) -> ExplainedMove:
    kind = _text(row, "kind")
    if kind not in _MOVE_KINDS:
        raise ManualActionsError(f"{row.get('isin')}: kind {kind!r} is not one of {_MOVE_KINDS}")
    return ExplainedMove(
        isin=_isin(row, "isin"),
        company=_text(row, "company"),
        trade_date=_date(row, "trade_date"),
        kind=kind,
        reason=" ".join(_text(row, "reason").split()),
        checked=_date(row, "checked"),
        sources=_sources(row),
    )


def _sources(row: dict[str, Any]) -> tuple[TermSource, ...]:
    try:
        return parse_term_sources(row)
    except ValueError as exc:
        raise ManualActionsError(str(exc)) from exc


def _isin(row: dict[str, Any], key: str) -> str:
    value = str(row.get(key) or "")
    if not _ISIN.fullmatch(value):
        raise ManualActionsError(f"{key} {value!r} is not an ISIN")
    return value


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not value:
        raise ManualActionsError(f"{row.get('isin')}: {key} is required")
    return str(value)


def _date(row: dict[str, Any], key: str) -> date:
    value = row.get(key)
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ManualActionsError(f"{row.get('isin')}: {key} {value!r}: {exc}") from exc
    raise ManualActionsError(f"{row.get('isin')}: {key} is required")


def _positive(row: dict[str, Any], key: str) -> Decimal:
    value = row.get(key)
    if isinstance(value, float):
        raise ManualActionsError(f"{row.get('isin')}: {key} must be a string or int, not float")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ManualActionsError(f"{row.get('isin')}: {key} {value!r} is not a number") from exc
    if not number.is_finite() or number <= 0:
        raise ManualActionsError(f"{row.get('isin')}: {key} must be positive, got {value!r}")
    return number
