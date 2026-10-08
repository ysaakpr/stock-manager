"""A5 · Disclosure text for the evidence bundle: what a holding's filings *say*, bounded and PIT.

The M6.8 live drill found that a T1 review handed only a disclosure's headline ("Resignation of
Auditor") decides on the subject line alone, and that the same model separated a real resignation
from a mandatory Section 139(2) rotation only when the body was in the bundle (`ops/gates/
M6-live-drill.md`, finding F1). This module is the production half of that fix: given the
announcement index the daily job already builds from L1, it selects one holding's recent
disclosures and attaches each one's text, so the bundle builder can render the text and not only
the headline.

It holds three properties:

* **Point-in-time at the instant, not the day.** Only a disclosure whose exchange dissemination
  `ts` is at or before the decision's `as_of` timestamp may be selected (invariant #7). The window
  is a query over the index, so a later row is *excluded* here — it is the window's edge, not a
  leak — while the bundle builder still refuses any row a caller passes in from after the trading
  date.

* **Bounded text.** Each disclosure's text is truncated to `DisclosurePolicy.max_chars_per_item`,
  at most `max_items` disclosures are selected (newest first), and their text together never
  exceeds `max_chars_per_bundle`. A disclosure the bundle cap leaves no room for keeps its headline
  and is marked `text_unavailable: bundle_text_cap`. The bundle's own token budget still applies on
  top of this; these caps make sure that one prolix filing cannot crowd out the rest.

* **Missing text is said, never hidden.** When a disclosure has no usable text, it is marked
  `text_unavailable` with a reason, never silently reduced to its headline. The reasons are: the
  feed carried no text (`no_body`); the feed's text only restates the headline, as NSE's
  `attchmntText` usually does (`headline_echo`); or the bundle cap was spent (`bundle_text_cap`).
  An attached document (the PDF `attachment_ref` names) is **never fetched here**: this is the
  decision path and makes no network call. Its text is reported `not_captured` until a capture job
  stores it.

Nothing here reads a clock, a database, `Settings`, or the network. It reads `AnnouncementRow`s
from an in-memory `AnnouncementIndex` and returns frozen values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final

from dataplatform.clock import IST
from dataplatform.ingest.announcements import AnnouncementRow
from dataplatform.query import AnnouncementIndex, normalize

__all__ = [
    "DEFAULT_DISCLOSURE_POLICY",
    "DisclosurePolicy",
    "DisclosureText",
    "DocumentTextStatus",
    "TextUnavailableReason",
    "disclosure_text",
    "select_disclosures",
]

#: NSE's `attchmntText` almost always opens "<Company> has informed the Exchange about <subject>".
#: Stripping that lead-in leaves what the text adds beyond the headline, if anything.
_INFORMED_LEAD: Final = re.compile(
    r"^.*?\bha(?:s|ve) informed the exchange\b\s*(?:about|regarding|that|of|on)?\s*",
    re.IGNORECASE,
)

#: A word, for comparing what the text says with what the headline says.
_WORD: Final = re.compile(r"[^\W_]+")

#: Words that add nothing to a headline when NSE wraps it ("...regarding Change in Auditors of the
#: company."). A text whose only words beyond the headline's are these is still an echo.
_FILLER: Final = frozenset(
    {"a", "an", "and", "bank", "company", "for", "in", "its", "limited", "ltd", "of", "on", "the"}
)

#: How a truncated text ends, so the reader (and the model) knows it is not the whole document.
_TRUNCATION_MARK: Final = " […truncated]"


class TextUnavailableReason(StrEnum):
    """Why a disclosure's text is not in the bundle. A closed set, so the gate can count them."""

    NO_BODY = "no_body"
    """The exchange feed carried no text for this disclosure, only its headline."""

    HEADLINE_ECHO = "headline_echo"
    """The feed's text only restates the headline (NSE's usual "has informed the Exchange")."""

    BUNDLE_TEXT_CAP = "bundle_text_cap"
    """The per-bundle text cap was already spent on newer disclosures."""


class DocumentTextStatus(StrEnum):
    """Whether the text of a disclosure's attached document (its PDF) is in the bundle."""

    NOT_CAPTURED = "not_captured"
    """The disclosure links a document whose text no capture job has stored. Never fetched here."""

    NONE = "none"
    """The disclosure links no document."""


@dataclass(frozen=True, slots=True)
class DisclosurePolicy:
    """The bounds on disclosure text in one bundle, so its model cost stays bounded.

    `max_chars_per_item` caps one disclosure's text. `max_chars_per_bundle` caps the text of every
    disclosure together. `max_items` caps how many disclosures are selected, newest first.
    `lookback_days` is how many calendar days before the as-of date the window opens.
    """

    max_chars_per_item: int = 2_000
    max_chars_per_bundle: int = 8_000
    max_items: int = 8
    lookback_days: int = 30

    def __post_init__(self) -> None:
        for name in ("max_chars_per_item", "max_chars_per_bundle", "max_items", "lookback_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be a whole number, got {value!r}")
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.max_chars_per_item <= len(_TRUNCATION_MARK):
            raise ValueError(
                f"max_chars_per_item ({self.max_chars_per_item}) must exceed the "
                f"{len(_TRUNCATION_MARK)}-character truncation mark, or a cut text has no room"
            )
        if self.max_chars_per_item > self.max_chars_per_bundle:
            raise ValueError(
                f"max_chars_per_item ({self.max_chars_per_item}) exceeds max_chars_per_bundle "
                f"({self.max_chars_per_bundle}); one disclosure could never fit its own bundle"
            )


#: The bounds a caller who names none inherits. At the builder's 4 characters per token, 8,000
#: characters is at most 2,000 tokens of disclosure text against the 12,000-token default budget,
#: so the flag, thesis and price context always keep most of the room.
DEFAULT_DISCLOSURE_POLICY: Final = DisclosurePolicy()


@dataclass(frozen=True, slots=True)
class DisclosureText:
    """One disclosure as the bundle carries it: the row, its bounded text, or why there is none.

    Exactly one of `text` and `unavailable` is set. `original_chars` is the length of the feed's
    text before truncation (0 when it had none), so the journal records how much was cut.
    """

    announcement: AnnouncementRow
    text: str | None
    unavailable: TextUnavailableReason | None
    truncated: bool
    original_chars: int
    document: DocumentTextStatus

    def __post_init__(self) -> None:
        if (self.text is None) == (self.unavailable is None):
            raise ValueError(
                "a DisclosureText carries either its text or a text_unavailable reason, not "
                f"both and not neither (text={self.text!r}, unavailable={self.unavailable!r})"
            )


def _is_headline_echo(subject: str, body: str) -> bool:
    """Whether `body` says nothing beyond `subject` once the exchange's lead-in is stripped."""
    remainder = set(_WORD.findall(_INFORMED_LEAD.sub("", normalize(body), count=1)))
    headline = set(_WORD.findall(normalize(subject)))
    return remainder - headline <= _FILLER


def disclosure_text(
    announcement: AnnouncementRow, *, max_chars: int = DEFAULT_DISCLOSURE_POLICY.max_chars_per_item
) -> DisclosureText:
    """The bounded text of one disclosure, or the reason it has none.

    What it does: takes the feed's `body`, marks it `no_body` when empty and `headline_echo` when it
    only restates the subject, and otherwise truncates it to `max_chars` (marking the cut).
    What it assumes: `announcement` came from the D1 parsers, so `body` is the exchange's own text.
    What it never does: fetch the attached document. It only reports that document as
    `not_captured`.
    """
    if max_chars <= len(_TRUNCATION_MARK):
        # Below this the slice index goes negative and returns nearly the whole text.
        raise ValueError(
            f"max_chars ({max_chars}) must exceed the {len(_TRUNCATION_MARK)}-character "
            "truncation mark"
        )
    document = (
        DocumentTextStatus.NOT_CAPTURED if announcement.attachment_ref else DocumentTextStatus.NONE
    )
    body = (announcement.body or "").strip()
    if not body:
        return DisclosureText(announcement, None, TextUnavailableReason.NO_BODY, False, 0, document)
    if _is_headline_echo(announcement.subject, body):
        return DisclosureText(
            announcement, None, TextUnavailableReason.HEADLINE_ECHO, False, len(body), document
        )
    if len(body) <= max_chars:
        return DisclosureText(announcement, body, None, False, len(body), document)
    cut = body[: max_chars - len(_TRUNCATION_MARK)].rstrip() + _TRUNCATION_MARK
    return DisclosureText(announcement, cut, None, True, len(body), document)


def select_disclosures(
    index: AnnouncementIndex,
    *,
    isin: str,
    as_of: datetime,
    policy: DisclosurePolicy = DEFAULT_DISCLOSURE_POLICY,
) -> tuple[DisclosureText, ...]:
    """A holding's recent disclosures known at `as_of`, newest first, with bounded text.

    What it does: searches `index` for `isin` over `policy.lookback_days` up to `as_of`'s IST date,
    keeps only rows whose dissemination `ts` is at or before `as_of`, takes the newest
    `policy.max_items`, and attaches each one's text under the per-item and per-bundle caps.
    What it assumes: `as_of` is the decision's tz-aware as-of instant, and `index` was built from
    L1 by the daily job.
    What it never does: return a disclosure disseminated after `as_of` (invariant #7), or exceed
    `policy.max_chars_per_bundle` characters of text across what it returns.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be tz-aware; a decision's as-of instant is a point in time")
    end: date = as_of.astimezone(IST).date()
    start = end - timedelta(days=policy.lookback_days)
    known = [row for row in index.search(isin=isin, start=start, end=end) if row.ts <= as_of]
    known.sort(key=lambda row: (row.ts, row.source, row.source_ref or ""), reverse=True)

    selected: list[DisclosureText] = []
    spent = 0
    for row in known[: policy.max_items]:
        item = disclosure_text(row, max_chars=policy.max_chars_per_item)
        text = item.text
        if text is not None:
            if spent + len(text) > policy.max_chars_per_bundle:
                item = DisclosureText(
                    row,
                    None,
                    TextUnavailableReason.BUNDLE_TEXT_CAP,
                    False,
                    item.original_chars,
                    item.document,
                )
            else:
                spent += len(text)
        selected.append(item)
    return tuple(selected)
