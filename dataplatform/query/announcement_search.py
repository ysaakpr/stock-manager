"""D4 query: searchable corporate-announcement index (§4.1 row 13, M3 box 3).

The M3 gate promises announcements "searchable by ISIN, date range and keyword within 1 hour of the
EOD poll", and §5.4's T0 tier is the caller: it matches the day's disclosures against the keyword
sets a ratified thesis defines (§5.3 BC2 "exit/divestment of robotics business line", BC3 "auditor
resignation / fraud investigation / promoter pledge >50%") to decide whether to escalate a holding
to T1. So this module answers two shapes of question over `AnnouncementRow`s read from L1:

* **Retrieval** — `AnnouncementIndex.search(isin=, start=, end=, query=)`: the disclosures for a
  security, within a date window, optionally narrowed by a keyword query. The date filter is on the
  announcement's *source* dissemination time (`ts`), the natural PIT — not the poll date — so "what
  was disclosed about this ISIN last week" means what the exchange actually disseminated then.

* **Break-condition matching** — `KeywordQuery` encodes one break condition's phrase/keyword set as
  `all_of` / `any_of` / `none_of`, and `matches`/`search` apply it. The two-sided shape is what
  makes acceptance criterion 3 achievable: `all_of` is the gate that ties a hit to the *subject* of
  the thesis (a divestment announcement about *robotics*, not any divestment), and `none_of` sheds
  the obvious false positive (a robotics division *winning an order*, which shares the word but not
  the event). A query with neither `all_of` nor `any_of` would match every disclosure and is
  rejected at construction — a keyword set matching everything is a mis-specified break condition,
  not a catch-all.

Term matching is deliberately morphological-lite and whole-word-anchored: a term matches at a word
boundary and may run past the end of the word, so `divest` catches "divestment", "divesting" and
"divested" without a stemmer, while anchored at the start so it never fires mid-word (it will
not match "misdivest"-style noise). A multi-word phrase matches across runs of whitespace, so
`"business line"` matches however the source spaced it. Matching is case-insensitive throughout.

The index is a plain in-memory structure built from L1 (`build_from_l1`), which is what keeps the
"within 1 hour of the poll" promise honest: indexing is part of the same EOD job that writes L1, not
a downstream batch, so a disclosure is searchable as soon as its partition is written. Nothing here
fetches or parses — it reads `AnnouncementRow`s the D1 parsers already produced.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dataplatform.clock import IST
from dataplatform.ingest.announcements import AnnouncementRow, iter_l1
from dataplatform.logging import get_logger

__all__ = [
    "AnnouncementIndex",
    "CompiledQuery",
    "KeywordQuery",
    "build_from_l1",
    "normalize",
]

_LOG = get_logger(__name__)

#: Collapses every run of whitespace to a single space, so a term's internal spacing need not
#: match the source's. Applied once when text is normalized and once when a phrase term is compiled.
_WHITESPACE: Final = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lower-case `text` and collapse its whitespace — the one form both sides of a match use.

    A term and the announcement text are compared in this normal form so casing and irregular
    spacing (line breaks in an attachment's extracted text, double spaces) never decide a match.
    """
    return _WHITESPACE.sub(" ", text.lower()).strip()


def _compile_term(term: str) -> re.Pattern[str]:
    """Compile one keyword/phrase term into a whole-word-anchored, morphology-lite matcher.

    Anchored at a word boundary (`\\b`) on the left so a term never fires mid-word, and left open on
    the right so `divest` also matches "divestment"/"divesting" without a stemmer — the exact
    behaviour a break-condition author expects when they write a stem. Internal whitespace becomes
    `\\s+`, so a phrase matches across however the source spaced it. The term is `re.escape`d, so a
    keyword containing regex metacharacters is matched literally, not as a pattern.
    """
    normalized = normalize(term)
    if not normalized:
        raise ValueError("a keyword term cannot be empty or whitespace only")
    # Escape each word separately and rejoin with `\s+`; escaping the whole phrase first would turn
    # the space into a literal `\ ` and then `\s+`-substitution over that leaves a literal backslash
    # in the pattern, which matches nothing. Per-token escaping keeps the phrase flexible on space.
    escaped = r"\s+".join(re.escape(token) for token in normalized.split())
    return re.compile(rf"\b{escaped}", re.IGNORECASE)


class KeywordQuery(BaseModel):
    """One break condition's keyword set: which terms must, may, and must not appear (§5.3/§5.4).

    What it does: encode the phrase/keyword logic of a single break condition as three term lists —
    `all_of` (every term must appear), `any_of` (at least one must appear), `none_of` (none may
    appear). It is the *declaration*; `compile()` turns it into the matcher (`CompiledQuery`) that
    decides whether an announcement satisfies it.
    What it assumes: the terms are stems/phrases a human ratified as part of a thesis; they are
    matched whole-word-anchored and case-insensitively (see `_compile_term`).
    What it never does: match everything. A query with neither `all_of` nor `any_of` is rejected at
    construction — that is a mis-specified break condition, and silently matching every disclosure
    would flood T0 with false escalations rather than none.

    It is a plain, serializable declaration (frozen pydantic model), so a ratified break condition's
    keyword set lives on the thesis object (§5.3) and travels with it; the compiled regex form is
    derived, never stored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    all_of: tuple[str, ...] = Field(default=(), description="every term must appear for a match")
    any_of: tuple[str, ...] = Field(default=(), description="at least one term must appear")
    none_of: tuple[str, ...] = Field(
        default=(), description="no term may appear — sheds obvious false positives"
    )

    @model_validator(mode="after")
    def _must_constrain(self) -> KeywordQuery:
        """A break condition that names no required term matches everything — reject it.

        Validated here rather than at match time so a mis-specified thesis fails when it is written,
        not silently on the day it should have flagged a break.
        """
        if not self.all_of and not self.any_of:
            raise ValueError(
                "a KeywordQuery must set at least one of all_of/any_of; a query with only none_of "
                "(or nothing) matches every announcement, which is a mis-specified break condition"
            )
        # Compile every term once here so an empty/whitespace term is a construction-time error too.
        self.compile()
        return self

    def compile(self) -> CompiledQuery:
        """Precompile the terms into a `CompiledQuery` matcher — do once, then reuse per row."""
        return CompiledQuery(
            all_of=tuple(_compile_term(term) for term in self.all_of),
            any_of=tuple(_compile_term(term) for term in self.any_of),
            none_of=tuple(_compile_term(term) for term in self.none_of),
        )

    def matches(self, text: str) -> bool:
        """Whether `text` satisfies this query — a one-off convenience that compiles each call.

        For matching many rows, `compile()` once and reuse the `CompiledQuery`; `AnnouncementIndex`
        does exactly that.
        """
        return self.compile().matches(text)

    def matches_row(self, row: AnnouncementRow) -> bool:
        """Whether an announcement's searchable text (subject + category + body) satisfies this."""
        return self.compile().matches(row.searchable_text)


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """A `KeywordQuery` with its terms compiled to patterns — the reusable matcher.

    Kept separate from `KeywordQuery` so the declaration stays a plain serializable model while the
    regex form (which is not serializable and is expensive to rebuild) is computed once and applied
    across a whole announcement set.
    """

    all_of: tuple[re.Pattern[str], ...]
    any_of: tuple[re.Pattern[str], ...]
    none_of: tuple[re.Pattern[str], ...]

    def matches(self, text: str) -> bool:
        """Whether `text` satisfies the query: all of `all_of`, one of `any_of`, none of `none_of`.

        Evaluated in the order a false result is cheapest and clearest: an excluded term present, or
        a required term absent, or no optional term present. The text is normalized here, so a raw
        announcement text and an already-normalized one get the same answer.
        """
        haystack = normalize(text)
        if any(pattern.search(haystack) for pattern in self.none_of):
            return False
        if not all(pattern.search(haystack) for pattern in self.all_of):
            return False
        return not (self.any_of and not any(p.search(haystack) for p in self.any_of))

    def matches_row(self, row: AnnouncementRow) -> bool:
        """Whether an announcement's searchable text satisfies this compiled query."""
        return self.matches(row.searchable_text)


class AnnouncementIndex:
    """An in-memory, searchable view over announcement rows — by ISIN, date range and keyword.

    What it does: hold a set of `AnnouncementRow`s indexed by ISIN, and answer retrieval and
    break-condition queries over them. Retrieval filters on the announcement's source `ts` (the
    natural PIT), so a date window means when the exchange disseminated, not when we polled.
    What it assumes: the rows were produced by the D1 parsers (so `ts` is aware and `isin` is a real
    identity), and that the set is small enough to hold in memory — one EOD poll, or a bounded
    backfill window, which is what the T0 monitor and the M3 gate query.
    What it never does: fetch, parse, or mutate a row. It is a read view; rebuild to see new rows.
    """

    def __init__(self, rows: Iterable[AnnouncementRow]) -> None:
        """Index `rows` by ISIN. Order within an ISIN is by `ts`, then the exchange's own id."""
        self._by_isin: dict[str, list[AnnouncementRow]] = {}
        for row in rows:
            self._by_isin.setdefault(row.isin, []).append(row)
        for isin_rows in self._by_isin.values():
            isin_rows.sort(key=lambda row: (row.ts, row.source, row.source_ref or ""))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(isins={len(self._by_isin)}, rows={self.size})"

    @property
    def size(self) -> int:
        """Total rows indexed."""
        return sum(len(rows) for rows in self._by_isin.values())

    @property
    def isins(self) -> frozenset[str]:
        """Every ISIN with at least one indexed announcement."""
        return frozenset(self._by_isin)

    def search(
        self,
        *,
        isin: str | None = None,
        start: date | None = None,
        end: date | None = None,
        query: KeywordQuery | None = None,
    ) -> tuple[AnnouncementRow, ...]:
        """Announcements matching every constraint given, in `(ts, source, source_ref)` order.

        `isin` restricts to one security; omit it to search all. `start`/`end` are an inclusive date
        window on the announcement's source dissemination date (`ts.date()`), the natural PIT — a
        disclosure disseminated outside the window is excluded even if it was polled inside it.
        `query` narrows to disclosures whose subject/category/body satisfy a break condition's
        keyword set. All constraints are ANDed; passing none returns every indexed row, sorted.
        """
        if start is not None and end is not None and start > end:
            raise ValueError(f"empty date range: start {start.isoformat()} > end {end.isoformat()}")

        candidates: Iterable[AnnouncementRow]
        candidates = self._by_isin.get(isin, ()) if isin is not None else self._all_rows()
        # Compile the keyword query once, not once per row. The date window is applied on the
        # exchange-zone (IST) calendar date of the source instant, so it means the same window
        # whether a row was read fresh from the parser (IST-localized) or back from L1 (stored UTC).
        compiled = None if query is None else query.compile()
        hits = [
            row
            for row in candidates
            if (start is None or _ist_date(row) >= start)
            and (end is None or _ist_date(row) <= end)
            and (compiled is None or compiled.matches_row(row))
        ]
        hits.sort(key=lambda row: (row.ts, row.source, row.source_ref or ""))
        return tuple(hits)

    def matches(
        self, query: KeywordQuery, *, isin: str | None = None
    ) -> tuple[AnnouncementRow, ...]:
        """The announcements a break condition's keyword set fires on — the T0 shortlist (§5.4).

        A thin, intention-revealing wrapper over `search`: given a ratified break condition's
        `KeywordQuery`, return the disclosures that satisfy it (optionally for one holding's ISIN),
        which is exactly what T0 hands to T1 as evidence.
        """
        return self.search(isin=isin, query=query)

    def _all_rows(self) -> list[AnnouncementRow]:
        """Every indexed row, unsorted — `search` sorts the filtered result."""
        return [row for rows in self._by_isin.values() for row in rows]


def _ist_date(row: AnnouncementRow) -> date:
    """The exchange-zone (Asia/Kolkata) calendar date of a row's source dissemination instant.

    The date window means an IST trading date, so the instant is converted to IST before its date is
    taken — a row stored in UTC in L1 and the same row fresh from the parser (IST) then answer the
    window identically, because both are the same instant.
    """
    return row.ts.astimezone(IST).date()


def build_from_l1(
    *, start: date | None = None, end: date | None = None, data_root: Path | None = None
) -> AnnouncementIndex:
    """Build the searchable index from the L1 announcement partitions in a poll-date range.

    The path that keeps the M3 gate's "within 1 hour of the poll" honest: the same EOD job that
    writes an announcement partition builds (or rebuilds) this index straight from it, so a
    disclosure is queryable as soon as it is normalized — no separate downstream batch. Partitions
    are chosen by their poll date; the search layer then filters to the caller's true window on each
    row's source `ts`.
    """
    index = AnnouncementIndex(iter_l1(start=start, end=end, data_root=data_root))
    _LOG.info(
        "announcements.index_built",
        start=None if start is None else start.isoformat(),
        end=None if end is None else end.isoformat(),
        isins=len(index.isins),
        rows=index.size,
    )
    return index
