"""A5 · Break-condition matcher: does a linked news item break a thesis, how fast (§5.3/§5.4).

Linkage (`linkage.py`) answers *which* held ISIN a news item is about. This module answers the
next question: does that item satisfy one of the holding's ratified break conditions, and — the
part §5.3 BC3 turns on — with what urgency. A break condition carries a keyword set
(`KeywordQuery`, the same declaration the exchange-announcement matcher uses) and a `type`. The
type decides the escalation speed:

* An **integrity** break (§5.3 BC3 — "auditor resignation / fraud investigation / promoter
  pledge") is the one class that escalates *immediately*: `evaluation: "T0 → immediate T1"` in
  the thesis schema, and `Urgency.IMMEDIATE` here. §5.6 keys the `IMMEDIATE` exit off exactly
  this type, so an integrity keyword hit cannot wait for the next scheduled review — the moment
  T0 sees it, T1 is due.
* A **fundamental** or **structural** break escalates on the normal T1 cadence
  (`Urgency.ROUTINE`): still an escalation, but worked through rather than acted on the same day.

`default_integrity_watch()` is the universal BC3 net: every core holding carries an integrity
break condition (§5.3 makes it one of the three standard conditions), so the integrity keyword
set is defined once here rather than copied onto every thesis. A thesis may still add its own BC3
keywords; this is the floor.

The matcher does no linking and no fetching — it takes `NewsLink`s and the watches for the ISINs
those links point at, and returns `NewsMatch`es. It is deliberately mechanical (T0 is ~₹0, no
LLM here): a keyword satisfied is an escalation raised, and the model reads the item only at T1.
Precision is inherited from two places — linkage decided the item is really about this holding,
and the `KeywordQuery`'s `all_of`/`none_of` shape sheds the near-miss — so a match here is a hit
worth a strong model's time, the whole point of keeping T1 cheap to reach but not cheap to run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from analyst.monitor.linkage import NewsLink
from analyst.monitor.t0 import T0Check, T0Flag
from analyst.thesis import BreakConditionType
from dataplatform.logging import get_logger
from dataplatform.query.announcement_search import KeywordQuery

__all__ = [
    "INTEGRITY_QUERY",
    "BreakConditionWatch",
    "NewsMatch",
    "Urgency",
    "default_integrity_watch",
    "match_link",
    "match_links",
]

_LOG = get_logger(__name__)

#: The universal integrity keyword set (§5.3 BC3). Curated for precision: `any_of` names the three
#: integrity events by their common phrasings, and `none_of` sheds the obvious false positive — a
#: company *appointing* an auditor, or an emissions/hiring "pledge", shares a word not the event.
#: A hit on this set is what §5.6 lets exit immediately, so it is worth getting the shape right.
INTEGRITY_QUERY: Final[KeywordQuery] = KeywordQuery(
    any_of=(
        "auditor resign",
        "auditor has resigned",
        "resignation of auditor",
        "resigns as auditor",
        "fraud",
        "forensic audit",
        "serious fraud investigation",
        "sfio",
        "promoter pledge",
        "pledged shares",
        "share pledge",
        "invocation of pledge",
    ),
    none_of=(
        "auditor appoint",
        "reappoint",
        "carbon pledge",
        "net zero pledge",
        "no fraud",
    ),
)


class Urgency(StrEnum):
    """How fast a matched break condition must reach T1 (§5.4/§5.6)."""

    IMMEDIATE = "IMMEDIATE"
    """An integrity break (§5.3 BC3): escalate the same session, T0 → immediate T1. Unlocks the
    §5.6 IMMEDIATE exit — it cannot wait for a scheduled review."""

    ROUTINE = "ROUTINE"
    """A fundamental or structural break: escalate on the normal T1 cadence, worked through."""


@dataclass(frozen=True, slots=True)
class BreakConditionWatch:
    """One holding's break condition as a keyword set the news feed is matched on (§5.3/§5.4).

    The T0-tier equivalent of a thesis `BreakCondition`, carrying just what the mechanical matcher
    needs: the condition's stable id (so an escalation records a verdict per condition — the
    journal's `BreakConditionEvaluation.id`), its `type` (which decides the escalation urgency),
    and its keyword query. Kept separate from the thesis model so the matcher needs no `Thesis`
    graph, and so the universal integrity watch (`default_integrity_watch`) can exist without one.
    """

    break_condition_id: str
    condition_type: BreakConditionType
    query: KeywordQuery

    @property
    def urgency(self) -> Urgency:
        """`IMMEDIATE` for an integrity break (§5.3 BC3), `ROUTINE` otherwise (§5.6)."""
        return (
            Urgency.IMMEDIATE
            if self.condition_type is BreakConditionType.INTEGRITY
            else Urgency.ROUTINE
        )


def default_integrity_watch(break_condition_id: str = "BC3") -> BreakConditionWatch:
    """The universal integrity break condition (§5.3 BC3), as a T0 watch.

    Every core holding carries one; defining it once here keeps the integrity keyword set in one
    place rather than copied onto every thesis. `break_condition_id` defaults to the schema's `BC3`
    but is overridable so a thesis that numbers its conditions differently stays consistent.
    """
    return BreakConditionWatch(
        break_condition_id=break_condition_id,
        condition_type=BreakConditionType.INTEGRITY,
        query=INTEGRITY_QUERY,
    )


@dataclass(frozen=True, slots=True)
class NewsMatch:
    """A linked news item that satisfies a holding's break condition — an escalation, formed.

    Carries the link (which item, which ISIN, why it linked), the break condition it fired, its type
    and the resulting urgency, so a T1 review reconstructs exactly what T0 saw and how fast it had
    to move — without re-running either the link or the keyword match.
    """

    link: NewsLink
    break_condition_id: str
    condition_type: BreakConditionType
    urgency: Urgency

    @property
    def escalates_immediately(self) -> bool:
        """Whether this match must reach T1 the same session (§5.3 BC3 / §5.6)."""
        return self.urgency is Urgency.IMMEDIATE

    def to_t0_flag(self, case_id: str) -> T0Flag:
        """This match as a T0 announcement flag, ready for the escalation queue (§5.4).

        Uses `T0Check.ANNOUNCEMENT` (a news keyword hit is the same class of trigger as an exchange
        announcement one) and records the break condition and urgency in `detail` — strings only,
        per the journal's payload rule — so the `ESCALATE` entry names the condition and states
        whether the hit demanded an immediate review.
        """
        subject = self.link.news_row.title or self.link.news_row.url
        return T0Flag(
            check=T0Check.ANNOUNCEMENT,
            isin=self.link.isin,
            case_id=case_id,
            summary=(
                f"break condition {self.break_condition_id} ({self.condition_type.value}) "
                f"keyword hit on news [{self.urgency.value}]: {subject}"
            ),
            detail={
                "break_condition_id": self.break_condition_id,
                "condition_type": self.condition_type.value,
                "urgency": self.urgency.value,
                "matched_form": self.link.matched_form,
                "match_kind": self.link.kind.value,
                "source": self.link.news_row.source,
                "url": self.link.news_row.url,
                "subject": subject,
            },
            break_condition_id=self.break_condition_id,
        )


def _searchable(link: NewsLink) -> str:
    """The text a news link's break conditions are matched on: its headline and tagged entities.

    A break-condition keyword ("auditor resign") lives in the headline of an RSS item; a GDELT event
    has no headline but tags actors. Joining both means the keyword set is applied to whatever the
    source stated, mirroring the linkage haystack so a link and its match read the same words.
    """
    row = link.news_row
    parts: list[str] = []
    if row.title is not None:
        parts.append(row.title)
    parts.extend(row.entities)
    return " ".join(parts)


def match_link(link: NewsLink, watches: Iterable[BreakConditionWatch]) -> tuple[NewsMatch, ...]:
    """Every break condition of one linked holding that the news item satisfies.

    Applies each watch's keyword query to the item's text; a satisfied query is one escalation, with
    urgency the condition's type dictates (integrity → immediate). Matches come in watch order.
    Silent when no watch fires — a link with no break-condition hit is a mention, not a break, and
    T0's job is to escalate breaks, not coverage.
    """
    text = _searchable(link)
    matches: list[NewsMatch] = []
    for watch in watches:
        if watch.query.matches(text):
            matches.append(
                NewsMatch(
                    link=link,
                    break_condition_id=watch.break_condition_id,
                    condition_type=watch.condition_type,
                    urgency=watch.urgency,
                )
            )
    return tuple(matches)


def match_links(
    links: Iterable[NewsLink],
    watches_by_isin: Mapping[str, Iterable[BreakConditionWatch]],
) -> tuple[NewsMatch, ...]:
    """Match a stream of linked news items against each holding's break conditions.

    `watches_by_isin` maps a held ISIN to the break conditions T0 watches for it (its thesis's
    T0-tier conditions plus the universal integrity watch). A link whose ISIN has no watches yields
    nothing — a held name with no ratified break condition is not a T0 escalation surface. They
    come back in input order, integrity ones flagged `IMMEDIATE`; the caller enqueues them for T1.
    """
    matches: list[NewsMatch] = []
    for link in links:
        watches = watches_by_isin.get(link.isin)
        if not watches:
            continue
        matches.extend(match_link(link, watches))
    if matches:
        immediate = sum(1 for match in matches if match.escalates_immediately)
        _LOG.info("matcher.escalations", total=len(matches), immediate=immediate)
    return tuple(matches)
