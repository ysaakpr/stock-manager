"""D3: free-text corporate-action purpose strings → structured terms, or a human.

The exchanges publish a corporate action as one line of prose. `FV SPLIT FROM RS.10/- TO RS.2/-`,
`Stock  Split From Rs.10/- to Rs.2/-`, `BONUS 1:1`, `SCHEME OF ARRANGEMENT`. This module turns
those into `ParsedAction`s, and — the part that matters — refuses to turn anything else into one.

Why the refusal is the feature. A corporate action rewrites the entire adjusted history of an
ISIN (EXECUTION_PLAN §4.3); a parser that guesses on the 3% of strings it does not understand
produces a price series that is wrong in exactly the places nobody looks. So the parser is
deliberately narrow and loud: it classifies only what it recognises, extracts only numbers that
are actually written down, and everything else lands in `ManualEntryQueue` carrying the raw text
byte-for-byte. There is no `OTHER` type and no default ratio anywhere in this file.

Four things send a string to the queue:

  * `UNRECOGNISED_TYPE` — no known action keyword (`ANNUAL GENERAL MEETING`, an empty cell).
  * `AMBIGUOUS_TYPE` — two unrelated types in one line (`BONUS 1:1 AND FV SPLIT FROM RS.10 TO
    RS.2`). One row cannot be both, and picking one is the guess this design exists to prevent.
  * `TERMS_NOT_STATED` — type recognised, but the numbers that determine its adjustment factor
    are absent (`AMALGAMATION` with no exchange ratio, a rights ratio with no subscription price).
  * `TERMS_CONFLICTING` — the numbers are present and disagree with themselves or with the words
    (a "consolidation" whose face value falls, two dividend amounts in one line, a to-value of 0).

A queued string may still yield a `ParsedAction`: `AMALGAMATION` normalizes to MERGER with
`UnquantifiedTerms` *and* queues for its ratio, because knowing it is a merger is already worth
recording — a merger is a structural break whether or not the ratio is known. Only the first two
reasons produce no action at all.

Not queued: NAME_CHANGE, BUYBACK and DELISTING with missing details. None of them produces an
adjustment factor, so an absent name or tender price blocks nothing, and queueing them would bury
the rows that do block the factor chain.

The queue itself is an in-memory, append-only collector. Persisting it, and giving each entry an
ex-date, an ISIN and a timestamp, is M2.2's job — this module has no clock, no database and no
network.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from structlog.typing import FilteringBoundLogger

from dataplatform.corpactions.taxonomy import (
    ActionType,
    DividendKind,
    DividendTerms,
    ExchangeRatioTerms,
    FaceValueTerms,
    NameChangeTerms,
    ParsedAction,
    PriceTerms,
    RatioTerms,
    RightsTerms,
    Terms,
    UnquantifiedTerms,
    tidy,
)
from dataplatform.logging import get_logger

__all__ = [
    "CorporateActionNormalizer",
    "ManualEntryQueue",
    "ManualQueueEntry",
    "ManualQueueReason",
    "ParseOutcome",
    "classify",
    "parse_purpose",
]

_LOG: Final[FilteringBoundLogger] = get_logger(__name__)


class ManualQueueReason(StrEnum):
    """Why a human has to look at this string. Never a severity — every one of these blocks."""

    UNRECOGNISED_TYPE = "UNRECOGNISED_TYPE"
    """No known action keyword. Could be a novel action, could be a non-action calendar entry."""

    AMBIGUOUS_TYPE = "AMBIGUOUS_TYPE"
    """Two or more unrelated action types in one string; it must be split into rows by a human."""

    TERMS_NOT_STATED = "TERMS_NOT_STATED"
    """Type known, but the numbers the adjustment factor needs are not in the text."""

    TERMS_CONFLICTING = "TERMS_CONFLICTING"
    """Numbers are present and cannot be trusted — self-contradictory, duplicated, or invalid."""


class ManualQueueEntry(BaseModel):
    """One purpose string awaiting human terms entry, with everything needed to resolve it.

    `raw_text` is the exchange's string exactly as published — not stripped, not case-folded, not
    whitespace-collapsed. Anything less and the human is being asked about a string that was never
    published, and a later re-parse against an improved parser compares against the wrong input.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_text: str = Field(description="Verbatim source string, including any odd whitespace.")
    source: str = Field(min_length=1, description="Source register id, e.g. 'NSE' or 'BSE'.")
    reason: ManualQueueReason
    detail: str = Field(min_length=1, description="What specifically is missing or contradictory.")
    action_type: ActionType | None = Field(
        default=None, description="The type, when it was established despite the queueing."
    )
    candidate_types: tuple[ActionType, ...] = Field(
        default=(), description="For AMBIGUOUS_TYPE: the types the string matched."
    )


class ManualEntryQueue:
    """Append-only collector of purpose strings a human must resolve.

    What it does: accumulates `ManualQueueEntry`s in arrival order and logs each at warning, so a
    parser run that quietly stopped understanding a feed is visible without reading the queue.
    What it assumes: one queue per ingestion run; it is not shared across threads.
    What it never does: drop, deduplicate or rewrite an entry. Append-only is what makes "this
    string was never silently defaulted" checkable after the fact.
    """

    def __init__(self) -> None:
        self._entries: list[ManualQueueEntry] = []

    def add(self, entry: ManualQueueEntry) -> None:
        """Append one entry and log it."""
        self._entries.append(entry)
        _LOG.warning(
            "ca.purpose.queued",
            source=entry.source,
            reason=str(entry.reason),
            action_type=str(entry.action_type) if entry.action_type else None,
            detail=entry.detail,
            raw_text=entry.raw_text,
        )

    @property
    def entries(self) -> tuple[ManualQueueEntry, ...]:
        """Everything queued so far, in arrival order."""
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[ManualQueueEntry]:
        return iter(self._entries)


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """What one purpose string produced: an action, a queue entry, or both.

    The two are not alternatives. `action is None` exactly when the type itself could not be
    established; `queue_entry is not None` whenever a human must supply something.
    """

    raw_text: str
    action: ParsedAction | None
    queue_entry: ManualQueueEntry | None


# ── classification ───────────────────────────────────────────────────────────────────────────

#: One regex per action type, matched against the lower-cased, whitespace-collapsed string.
#: Deliberately keyword-driven rather than clever: a pattern that matches too much produces a
#: confidently wrong action, while one that matches too little produces a queue entry someone
#: reads. The asymmetry is the whole point.
_TYPE_PATTERNS: Final[tuple[tuple[ActionType, re.Pattern[str]], ...]] = (
    # `\bmerger\b` cannot match inside "demerger" — there is no word boundary between "de" and
    # "merger" — so the two patterns do not collide.
    (ActionType.SPLIT, re.compile(r"\bsplit\b|\bsub[\s-]?division\b|\bconsolidation\b")),
    (ActionType.BONUS, re.compile(r"\bbonus\b")),
    (ActionType.DIVIDEND, re.compile(r"\bdividend\b")),
    # "rights" only counts as a rights issue when it says so or is followed by a ratio; otherwise
    # "differential voting rights" would be one.
    (ActionType.RIGHTS, re.compile(r"\brights?\s+issue\b|\brights\b(?=\s*\d)")),
    (ActionType.MERGER, re.compile(r"\bmerger\b|\bamalgamation\b")),
    (ActionType.DEMERGER, re.compile(r"\bde[\s-]?merger\b|\bspin[\s-]?off\b")),
    (ActionType.SCHEME_OF_ARRANGEMENT, re.compile(r"\bscheme\s+of\s+arrangement\b")),
    (ActionType.DVR_CONVERSION, re.compile(r"\bdvr\b|\bdifferential\s+voting\s+rights?\b")),
    (ActionType.NAME_CHANGE, re.compile(r"\bchange\s+(?:in|of)\s+name\b|\bname\s+change\b")),
    # Requires a change verb: bare "face value" appears in dividend percentages and in split
    # descriptions, and must not classify either of them.
    (
        ActionType.FACE_VALUE_CHANGE,
        re.compile(r"\b(?:change|reduction|revision)\s+(?:in|of)\s+face\s+value\b"),
    ),
    (ActionType.BUYBACK, re.compile(r"\bbuy[\s-]?back\b")),
    (ActionType.DELISTING, re.compile(r"\bde[\s-]?list(?:ing|ed)\b")),
)

#: Umbrella type → the specific types that override it when both match the same string.
#: `SCHEME OF ARRANGEMENT-DEMERGER` is a demerger; `FV SPLIT` is a split, not a bare face-value
#: change. Anything not related this way and matching twice is genuinely ambiguous.
_UMBRELLA_OF: Final[dict[ActionType, frozenset[ActionType]]] = {
    ActionType.SCHEME_OF_ARRANGEMENT: frozenset(
        {ActionType.MERGER, ActionType.DEMERGER, ActionType.DVR_CONVERSION}
    ),
    ActionType.FACE_VALUE_CHANGE: frozenset({ActionType.SPLIT}),
}

#: Action types whose adjustment factor is undefined without terms. Missing terms on one of these
#: is a blocker; missing terms on any other type is merely missing detail.
_TERMS_REQUIRED: Final[frozenset[ActionType]] = frozenset(
    {
        ActionType.SPLIT,
        ActionType.BONUS,
        ActionType.DIVIDEND,
        ActionType.RIGHTS,
        ActionType.MERGER,
        ActionType.DEMERGER,
        ActionType.SCHEME_OF_ARRANGEMENT,
        ActionType.DVR_CONVERSION,
        ActionType.FACE_VALUE_CHANGE,
    }
)


def classify(text: str) -> tuple[ActionType, ...]:
    """The action types a purpose string names, after umbrella types yield to specific ones.

    Returns empty for an unrecognised string and more than one for a genuinely ambiguous one;
    callers treat both as queue conditions rather than picking a winner.
    """
    normalized = _normalize(text)
    matched = {action for action, pattern in _TYPE_PATTERNS if pattern.search(normalized)}
    survivors = {
        action
        for action in matched
        if not (_UMBRELLA_OF.get(action, frozenset()) & matched)  # dropped if a specific one won
    }
    return tuple(sorted(survivors, key=lambda action: action.value))


# ── number extraction ────────────────────────────────────────────────────────────────────────

#: A rupee amount: `RS.10/-`, `Rs. 18.0000`, `RE 0.50`, `₹90`. The currency marker is required —
#: a bare number in a purpose string is as likely to be a year or a percentage as an amount.
_MONEY = r"(?:\b(?:rs|re|inr)\b\s*\.?|₹)\s*(\d[\d,]*(?:\.\d+)?)"
_MONEY_RE: Final[re.Pattern[str]] = re.compile(_MONEY)

#: `FROM <money> ... TO <money>`, non-greedy so it takes the first "to" after the first amount.
_FROM_TO_RE: Final[re.Pattern[str]] = re.compile(rf"\bfrom\b\s*{_MONEY}.*?\bto\b\s*{_MONEY}")

#: `1:4`, `3 : 8`. Decimal parts are allowed because a few schemes publish fractional ratios.
_RATIO_RE: Final[re.Pattern[str]] = re.compile(r"(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)")

_PERCENT_RE: Final[re.Pattern[str]] = re.compile(r"(\d+(?:\.\d+)?)\s*%")

#: `FROM <old name> TO <new name>`, run against the original-case text so names survive intact.
_NAME_FROM_TO_RE: Final[re.Pattern[str]] = re.compile(
    r"\bfrom\b\s+(.+?)\s+\bto\b\s+(.+?)\s*$", re.IGNORECASE
)

_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def _collapse(text: str) -> str:
    """Original case, single-spaced. What the name extractor reads.

    Non-breaking spaces are folded explicitly: BSE's HTML-derived strings carry them, and while
    `\\s` happens to match U+00A0 in `re`'s str mode, relying on that makes correctness a property
    of the regex flavour rather than of this function.
    """
    return _WHITESPACE_RE.sub(" ", text.replace("\u00a0", " ")).strip()


def _normalize(text: str) -> str:
    """Lower-cased and single-spaced. What every keyword and number pattern reads."""
    return _collapse(text).lower()


def _decimal(raw: str) -> Decimal | None:
    try:
        return tidy(Decimal(raw.replace(",", "")))
    except InvalidOperation:  # pragma: no cover - the regexes only yield valid literals
        return None


def _money_values(text: str) -> list[Decimal]:
    """Every distinct rupee amount in the string, in order of first appearance."""
    seen: list[Decimal] = []
    for match in _MONEY_RE.finditer(text):
        value = _decimal(match.group(1))
        if value is not None and value not in seen:
            seen.append(value)
    return seen


def _ratios(text: str) -> list[tuple[Decimal, Decimal]]:
    """Every distinct `a:b` in the string, in order of first appearance."""
    seen: list[tuple[Decimal, Decimal]] = []
    for match in _RATIO_RE.finditer(text):
        left, right = _decimal(match.group(1)), _decimal(match.group(2))
        if left is None or right is None:
            continue  # pragma: no cover - the regex only yields valid literals
        if (left, right) not in seen:
            seen.append((left, right))
    return seen


def _percents(text: str) -> list[Decimal]:
    seen: list[Decimal] = []
    for match in _PERCENT_RE.finditer(text):
        value = _decimal(match.group(1))
        if value is not None and value not in seen:
            seen.append(value)
    return seen


def _face_values(text: str) -> tuple[Decimal, Decimal] | None:
    match = _FROM_TO_RE.search(text)
    if match is None:
        return None
    old, new = _decimal(match.group(1)), _decimal(match.group(2))
    if old is None or new is None:  # pragma: no cover - the regex only yields valid literals
        return None
    return old, new


# ── terms extraction, per type ───────────────────────────────────────────────────────────────

#: A terms extraction result: the terms, and — when a human is needed — why and what for.
_Extraction = tuple[Terms, ManualQueueReason | None, str]


def _unstated(what: str) -> _Extraction:
    return UnquantifiedTerms(), ManualQueueReason.TERMS_NOT_STATED, f"{what} not stated"


def _conflicting(what: str) -> _Extraction:
    return UnquantifiedTerms(), ManualQueueReason.TERMS_CONFLICTING, what


def _face_value_terms(action: ActionType, text: str) -> _Extraction:
    """Face values for SPLIT and FACE_VALUE_CHANGE, cross-checked against the words.

    A string that says "consolidation" while its face value falls is not a consolidation with a
    typo — it is a string this parser has misread, and the only safe reading of a misread
    corporate action is to hand it to a human.
    """
    values = _face_values(text)
    if values is None:
        return _unstated("face values")
    old, new = values
    if action is ActionType.SPLIT:
        says_consolidation = "consolidation" in text
        says_subdivision = bool(re.search(r"\bsplit\b|\bsub[\s-]?division\b", text))
        if says_consolidation and says_subdivision:
            return _conflicting("text names both a split and a consolidation")
        if says_consolidation and new < old:
            return _conflicting(f"named a consolidation but face value falls {old} -> {new}")
        if says_subdivision and new > old:
            return _conflicting(f"named a split but face value rises {old} -> {new}")
    try:
        return FaceValueTerms(from_value=old, to_value=new), None, ""
    except ValidationError as exc:
        return _conflicting(f"face values {old} -> {new} are not usable: {exc.error_count()} error")


def _bonus_terms(text: str) -> _Extraction:
    ratios = _ratios(text)
    if not ratios:
        return _unstated("bonus ratio")
    if len(ratios) > 1:
        return _conflicting(f"{len(ratios)} different bonus ratios in one string")
    new, held = ratios[0]
    return RatioTerms(new_shares=new, held_shares=held), None, ""


def _rights_terms(text: str) -> _Extraction:
    ratios = _ratios(text)
    if not ratios:
        return _unstated("rights ratio")
    if len(ratios) > 1:
        return _conflicting(f"{len(ratios)} different rights ratios in one string")
    money = _money_values(text)
    if len(money) > 1:
        return _conflicting(f"{len(money)} different rupee amounts in one rights string")
    new, held = ratios[0]
    if not money:
        # A ratio alone cannot price the entitlement, so the factor is still undefined.
        terms = RightsTerms(new_shares=new, held_shares=held)
        return terms, ManualQueueReason.TERMS_NOT_STATED, "rights subscription price not stated"
    if "premium" in text:
        terms = RightsTerms(new_shares=new, held_shares=held, premium_inr=money[0])
    else:
        terms = RightsTerms(new_shares=new, held_shares=held, issue_price_inr=money[0])
    return terms, None, ""


def _dividend_kind(text: str) -> DividendKind:
    if "special" in text:
        return DividendKind.SPECIAL
    if "interim" in text:
        return DividendKind.INTERIM
    if "final" in text:
        return DividendKind.FINAL
    return DividendKind.UNSPECIFIED


def _dividend_terms(text: str) -> _Extraction:
    kind = _dividend_kind(text)
    bare = DividendTerms(dividend_kind=kind)
    money, percents = _money_values(text), _percents(text)
    if len(money) > 1:
        return bare, ManualQueueReason.TERMS_CONFLICTING, f"{len(money)} dividend amounts in one"
    if len(percents) > 1:
        return bare, ManualQueueReason.TERMS_CONFLICTING, f"{len(percents)} percentages in one"
    if money and percents:
        return bare, ManualQueueReason.TERMS_CONFLICTING, "both a rupee amount and a percentage"
    if money:
        return DividendTerms(dividend_kind=kind, amount_inr=money[0]), None, ""
    if percents:
        return DividendTerms(dividend_kind=kind, percent_of_face_value=percents[0]), None, ""
    return bare, ManualQueueReason.TERMS_NOT_STATED, "dividend amount not stated"


def _exchange_ratio_terms(text: str) -> _Extraction:
    ratios = _ratios(text)
    if not ratios:
        return _unstated("exchange ratio")
    if len(ratios) > 1:
        return _conflicting(f"{len(ratios)} different exchange ratios in one string")
    received, held = ratios[0]
    return ExchangeRatioTerms(shares_received=received, shares_held=held), None, ""


def _name_change_terms(original: str) -> _Extraction:
    """Old and new name, from the original-case text so the names survive verbatim.

    Reports missing names as unstated like every other extractor; `_TERMS_REQUIRED` is what
    decides that a name change does not actually block anything, in one place rather than here.

    Splits on the first " to ", so a former name that itself contains " to " would be cut short.
    That is a known and accepted limit: a name change sets no adjustment factor, and the raw text
    is preserved on the action either way.
    """
    match = _NAME_FROM_TO_RE.search(original)
    if match is None:
        return _unstated("company names")
    old, new = match.group(1).strip(" .,"), match.group(2).strip(" .,")
    if not old or not new:  # pragma: no cover - the regex requires a non-empty run either side
        return _unstated("company names")
    return NameChangeTerms(from_name=old, to_name=new), None, ""


def _price_terms(text: str) -> _Extraction:
    """A tender price for BUYBACK/DELISTING. Absent is complete: neither sets a factor."""
    money = _money_values(text)
    if len(money) > 1:
        return PriceTerms(), ManualQueueReason.TERMS_CONFLICTING, f"{len(money)} prices in one"
    return PriceTerms(price_inr=money[0] if money else None), None, ""


def _extract_terms(action: ActionType, normalized: str, original: str) -> _Extraction:
    match action:
        case ActionType.SPLIT | ActionType.FACE_VALUE_CHANGE:
            return _face_value_terms(action, normalized)
        case ActionType.BONUS:
            return _bonus_terms(normalized)
        case ActionType.RIGHTS:
            return _rights_terms(normalized)
        case ActionType.DIVIDEND:
            return _dividend_terms(normalized)
        case (
            ActionType.MERGER
            | ActionType.DEMERGER
            | ActionType.SCHEME_OF_ARRANGEMENT
            | ActionType.DVR_CONVERSION
        ):
            return _exchange_ratio_terms(normalized)
        case ActionType.NAME_CHANGE:
            return _name_change_terms(original)
        case ActionType.BUYBACK | ActionType.DELISTING:
            return _price_terms(normalized)


def parse_purpose(raw: str, *, source: str) -> ParseOutcome:
    """Normalize one exchange purpose string.

    What it does: classifies the action, extracts only the numbers the text actually states, and
    reports whatever a human still has to supply.
    What it assumes: `raw` is one action's purpose text as published; `source` is its register id.
    What it never does: guess a type, invent a ratio, or lose the raw string — every queue entry
    and every returned action carries `raw` verbatim.

    Pure: no clock, no I/O. The caller decides what to do with the outcome; the convenience
    wrapper that pushes it into a queue is `CorporateActionNormalizer`.
    """
    normalized = _normalize(raw)
    candidates = classify(raw)

    if not candidates:
        detail = "empty purpose string" if not normalized else "no known action keyword"
        return ParseOutcome(
            raw_text=raw,
            action=None,
            queue_entry=ManualQueueEntry(
                raw_text=raw,
                source=source,
                reason=ManualQueueReason.UNRECOGNISED_TYPE,
                detail=detail,
            ),
        )

    if len(candidates) > 1:
        named = ", ".join(action.value for action in candidates)
        return ParseOutcome(
            raw_text=raw,
            action=None,
            queue_entry=ManualQueueEntry(
                raw_text=raw,
                source=source,
                reason=ManualQueueReason.AMBIGUOUS_TYPE,
                detail=f"string names {len(candidates)} action types: {named}",
                candidate_types=candidates,
            ),
        )

    action_type = candidates[0]
    terms, reason, detail = _extract_terms(action_type, normalized, _collapse(raw))
    action = ParsedAction(action_type=action_type, terms=terms, raw_text=raw, source=source)

    # Missing detail only blocks when the type's adjustment factor depends on it.
    if reason is ManualQueueReason.TERMS_NOT_STATED and action_type not in _TERMS_REQUIRED:
        reason = None

    if reason is None:
        _LOG.debug("ca.purpose.parsed", source=source, action_type=str(action_type), raw_text=raw)
        return ParseOutcome(raw_text=raw, action=action, queue_entry=None)

    return ParseOutcome(
        raw_text=raw,
        action=action,
        queue_entry=ManualQueueEntry(
            raw_text=raw,
            source=source,
            reason=reason,
            detail=detail,
            action_type=action_type,
        ),
    )


class CorporateActionNormalizer:
    """`parse_purpose` plus the queue, for callers that just want the actions.

    What it does: parses each purpose string, files anything needing a human into its queue, and
    returns the action when one could be built.
    What it assumes: one instance per ingestion run, so `queue` is that run's outstanding work.
    What it never does: return an action whose terms were guessed — `None` and a queue entry is
    always the answer when the type could not be established.
    """

    def __init__(self, queue: ManualEntryQueue | None = None) -> None:
        self._queue = queue if queue is not None else ManualEntryQueue()

    @property
    def queue(self) -> ManualEntryQueue:
        """The manual-entry queue this normalizer files into."""
        return self._queue

    def normalize(self, raw: str, *, source: str) -> ParsedAction | None:
        """Parse one purpose string, queueing whatever a human must resolve."""
        outcome = parse_purpose(raw, source=source)
        if outcome.queue_entry is not None:
            self._queue.add(outcome.queue_entry)
        return outcome.action
