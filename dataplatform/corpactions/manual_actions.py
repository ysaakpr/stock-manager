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

**Why a reviewed file and not rows in ``corporate_actions``** — the same reason as
`merger_terms`: the table's rows are produced by parsers from L0 and reconciled across two
exchanges; a hand transcription is neither, and putting it there would make a reconciled-looking
row nobody's parser can re-derive (invariant #1). A YAML file is reviewed in the diff and
re-derivable from the L0 objects it names. Every curated action carries `MANUAL_SOURCE` as its
source, so it can never be mistaken for a feed's.

**PIT.** `knowable_date` is the dissemination date of the earliest L0 source stating both the
terms and the ex-date, never the day it was transcribed. L2 back-adjustment is retroactive by
design (§4.3); nothing on the decision path reads this module.

What this module never does: fetch, infer a ratio, or fill a missing term. An event no source
states stays out of the file, and its step stays UNEXPLAINED until one does.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml

from dataplatform.corpactions.merger_terms import TermSource, parse_term_sources
from dataplatform.corpactions.taxonomy import (
    ActionType,
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

#: What an explained move may be classified as. One kind today; a closed set on purpose.
_MOVE_KINDS: Final = frozenset({"MARKET_MOVE"})

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

    @property
    def is_price_event(self) -> bool:
        return self.action_type in _PRICE_EVENTS

    def as_action(self) -> CorporateAction:
        """This row as a `CorporateAction` under `MANUAL_SOURCE`, for the factor math."""
        from dataplatform.ingest.corp_actions import CorporateAction

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
        )


@dataclass(frozen=True, slots=True)
class ExplainedMove:
    """A step on `trade_date` that is a real move in the security's price, not a missing action."""

    isin: str
    company: str
    trade_date: date
    kind: str
    reason: str
    checked: date
    sources: tuple[TermSource, ...]


@dataclass(frozen=True, slots=True)
class ManualActions:
    """The whole curated file, indexed by ISIN."""

    actions: tuple[CuratedAction, ...]
    explained_moves: tuple[ExplainedMove, ...]
    _by_isin: Mapping[str, tuple[CuratedAction, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_isin: dict[str, list[CuratedAction]] = {}
        for row in self.actions:
            by_isin.setdefault(row.isin, []).append(row)
        object.__setattr__(self, "_by_isin", {k: tuple(v) for k, v in by_isin.items()})

    def actions_for(self, isin: str) -> tuple[CuratedAction, ...]:
        """`isin`'s curated actions in ex-date order; empty for the ~all ISINs that have none."""
        return self._by_isin.get(isin, ())

    def structural_dates(self) -> dict[str, tuple[date, ...]]:
        """ISIN → ex-dates of its curated structural breaks (demergers, schemes)."""
        out: dict[str, list[date]] = {}
        for row in self.actions:
            if not row.is_price_event:
                out.setdefault(row.isin, []).append(row.ex_date)
        return {k: tuple(v) for k, v in out.items()}

    def explained_dates(self) -> dict[str, tuple[date, ...]]:
        """ISIN → the sessions whose step is a documented market move."""
        out: dict[str, list[date]] = {}
        for move in self.explained_moves:
            out.setdefault(move.isin, []).append(move.trade_date)
        return {k: tuple(v) for k, v in out.items()}


def load_manual_actions(path: Path = MANUAL_ACTIONS_PATH) -> ManualActions:
    """Read and validate the curated file.

    Raises `ManualActionsError` on a row without a source or a `checked` date, a malformed ISIN,
    an action type outside SPLIT/BONUS/DEMERGER/SCHEME_OF_ARRANGEMENT, a non-positive or float
    ratio, a duplicate `(isin, ex_date, action_type)` or `(isin, trade_date)`, or an explained
    move on the very session a curated action is dated — the file cannot say both.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ManualActionsError(f"{path}: expected a mapping at the top level")
    actions = tuple(
        sorted(
            (_action(row) for row in document.get("actions") or ()),
            key=lambda a: (a.isin, a.ex_date, a.action_type.value),
        )
    )
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
    return ManualActions(actions=actions, explained_moves=moves)


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
    else:
        raise ManualActionsError(f"{isin}: {action_type} is not a type this file may state")
    filed = row.get("filed_against_isin")
    return CuratedAction(
        isin=isin,
        company=_text(row, "company"),
        action_type=action_type,
        terms=terms,
        ex_date=_date(row, "ex_date"),
        knowable_date=_date(row, "knowable_date"),
        checked=_date(row, "checked"),
        sources=_sources(row),
        record_date=None if row.get("record_date") is None else _date(row, "record_date"),
        filed_against_isin=None if filed is None else _isin(row, "filed_against_isin"),
        note=row.get("note"),
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
