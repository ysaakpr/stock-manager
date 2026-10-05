"""Index-change announcements — the exchange's own record of who entered and left an index (DQ-5).

The constituents CSV (`indices.parse_constituents`) is "as of today" only, and the daily snapshots
accrued from 2026-09-08 joined zero price history: every L1 price date is older than the first
snapshot. A backtest scoped to an index therefore saw either nothing or today's list — the
survivorship bias §8.3.6 forbids. The history exists, though, and it is primary: NSE Indices
announces every constituent change in a press release that names the index, the company, its
symbol, the announcement date and the date the change takes effect. This module reads them.

**Where they are.** `https://niftyindices.com/press-release` serves, in its static HTML, one
`pressItem` per release back to 1998 — `data-date` (the announcement date), the PDF path, and the
title. One request indexes the archive, so no PDF number is ever guessed. Each release is then one
GET of `/Press_Release/ind_prs<DDMMYYYY>[_n].pdf` from the same host, under the register's
`nifty_index_press_releases` policy (browser UA, 2.5 s spacing, robots-permitted paths).

**What it parses.** `parse_press_release_pdf` turns one release into `IndexChangeEvent`s — one per
(index, company, include/exclude) — for the indices whose history this platform reconstructs
(`TRACKED_INDICES`). The release layout from ~2016 on is regular: a numbered heading per index
(`1) Nifty 50`), then `The following company is being excluded:` and a `Sr. No. Company Name
Symbol` table; the effective date is stated once (`effective from September 30, 2026`) or per
part. Older releases lay their tables out detached from their headings and carry no symbol; when a
release cannot be read section by section it is returned *unparsed* with the reason, and the history
builder stops its depth there rather than guessing (`index_history`).

**What it never does.** It never resolves a symbol to an ISIN — that is `index_history`'s job, at
the event date, through the identity windows. It never invents an effective date: a section with no
date it can read is unparsed, not dated to the announcement. And it opens no socket: it takes bytes,
or an `L0Ref` it reads back through `L0Store`; `Fetcher` is the only thing that fetches.
"""

from __future__ import annotations

import html
import io
import re
from collections.abc import Iterable, Mapping
from datetime import date
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from dataplatform.ingest.fetcher import Fetcher
from dataplatform.ingest.models import ParseError
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "LISTING_URL",
    "PRESS_RELEASE_SOURCE_ID",
    "TRACKED_INDICES",
    "ChangeAction",
    "IndexChangeEvent",
    "PressRelease",
    "PressReleaseParse",
    "canonical_index_slug",
    "fetch_listing",
    "fetch_press_release",
    "is_membership_candidate",
    "l0_listing_filename",
    "parse_date_phrase",
    "parse_press_release_l0",
    "parse_press_release_listing",
    "parse_press_release_pdf",
    "press_release_url",
]

_LOG = get_logger(__name__)

#: Register id for the listing page and every release PDF (one host, one policy).
PRESS_RELEASE_SOURCE_ID: Final = "nifty_index_press_releases"

#: The page whose static HTML lists every release with its date and title.
LISTING_URL: Final = "https://niftyindices.com/press-release"

_PDF_BASE: Final = "https://niftyindices.com/Press_Release/"

#: The broad indices whose membership history is reconstructed, with the lake slug each maps to and
#: the member count the index methodology fixes — what a reconstructed date is reconciled against.
TRACKED_INDICES: Final[Mapping[str, int]] = {
    "nifty50": 50,
    "niftynext50": 50,
    "nifty100": 100,
    "nifty200": 200,
    "nifty500": 500,
    "niftymidcap150": 150,
    "niftysmallcap250": 250,
}

#: Every spelling an index heading has carried, normalised (lower case, `&amp;`/punctuation folded,
#: single spaces), to the lake slug. Exact match only: `Nifty50 Equal Weight`, `Nifty 500
#: Healthcare` and `Nifty Midcap 150 Momentum 50` are different indices and must not alias.
_INDEX_ALIASES: Final[Mapping[str, str]] = {
    "nifty 50": "nifty50",
    "nifty50": "nifty50",
    "cnx nifty": "nifty50",
    "s&p cnx nifty": "nifty50",
    "nifty next 50": "niftynext50",
    "nifty next50": "niftynext50",
    "cnx nifty junior": "niftynext50",
    "nifty junior": "niftynext50",
    "nifty 100": "nifty100",
    "nifty100": "nifty100",
    "cnx 100": "nifty100",
    "nifty 200": "nifty200",
    "nifty200": "nifty200",
    "cnx 200": "nifty200",
    "nifty 500": "nifty500",
    "nifty500": "nifty500",
    "cnx 500": "nifty500",
    "s&p cnx 500": "nifty500",
    "nifty midcap 150": "niftymidcap150",
    "nifty midcap150": "niftymidcap150",
    "nifty smallcap 250": "niftysmallcap250",
    "nifty smallcap250": "niftysmallcap250",
}

_MONTHS: Final[Mapping[str, int]] = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}

_MONTH_RE: Final = (
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
#: "September 30, 2026" / "Sept. 30,2026" / "Apr. 25, 2003" / "30th September 2026" / "30-Sep-2026".
#: A two-digit day the PDF's text layer split with a space before its comma ("January 2 8, 2019",
#: ind_prs21012019.pdf) is read as one number — only before a comma, so "May 3 2019" stays day 3.
_DATE_MDY: Final = re.compile(
    _MONTH_RE + r"\.?\s*(?P<day>\d\s\d(?=\s*,)|\d{1,2})(?:st|nd|rd|th)?\s*,?\s*"
    r"(?P<year>(?:19|20)\d{2})",
    re.I,
)
_DATE_DMY: Final = re.compile(
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?[\s\-]+" + _MONTH_RE + r"\.?[\s\-,]+(?P<year>(?:19|20)\d{2})",
    re.I,
)
#: The phrases that introduce an effective date. "close of <date>" is the session *before* the
#: change and is deliberately not one of them.
_EFFECTIVE_INTRO: Final = re.compile(
    r"(?:effective\s+(?:from|on|date)?\s*:?|w\s*\.?\s*e\s*\.?\s*f\s*\.?|with\s+effect\s+from)\s*",
    re.I,
)

_LISTING_ITEM: Final = re.compile(
    r'<div class="pressItem" data-date="(?P<date>[^"]*)"[^>]*>\s*<p>[^<]*</p>\s*'
    r"<a href='(?P<href>[^']*)'[^>]*>(?P<title>[^<]*)</a>",
)
_PDF_NAME: Final = re.compile(r"^ind_prs\d{8}(?:_\d+)?\.pdf$", re.I)

#: Titles that are never about equity index membership: debt, IPO/SME lists, launches, notices.
_NOT_MEMBERSHIP: Final = re.compile(
    r"fixed income|\bsdl\b|\bipo\b|\bsme\b|bond|g-?sec|gilt|t-?bill|money market|\baif\b|launch"
    r"|maturity|dissemination|data provider|shareholding|\besg\b|etf listed|name of the index"
    r"|treasury|crisil|debt|corporate bond|screening partner|\breit\b|invit",
    re.I,
)
#: A title naming a tracked index — always a candidate, whatever else it says.
_NAMES_TRACKED: Final = re.compile(
    r"nifty\s*50(?![\s-]*(?:equal|value|arbitrage|shariah|dividend|usd|pr|tr)\b)(?!\d)"
    r"|nifty\s*next\s*50|junior|nifty\s*100(?!\s*(?:esg|equal|low|quality|alpha|enhanced))\b"
    r"|cnx\s*100\b|nifty\s*200(?!\s*(?:momentum|quality|alpha|value))\b|cnx\s*200\b"
    r"|nifty\s*500(?!\s*(?:healthcare|multicap|value|momentum|quality|equal|low))\b"
    r"|cnx\s*500\b|midcap\s*150(?!\s*(?:momentum|quality))\b|smallcap\s*250(?!\s*(?:momentum|quality))\b"
    r"|s&p\s*cnx\s*nifty\b|cnx\s*nifty\b",
    re.I,
)
#: A title naming one specific index (that is not a tracked one): about that index only.
_NAMES_OTHER_INDEX: Final = re.compile(
    r"(?:nifty|cnx|s&p\s*cnx)\s+(?!indices\b|equity\b|index\b)[a-z0-9&]+", re.I
)
#: A generic change title: "Replacements in indices", "Change in Index", "Index Changes",
#: "Exclusion of X from Nifty indices", "Revision in criteria and replacements in indices".
_GENERIC_CHANGE: Final = re.compile(
    r"(?:change|replacement|inclusion|exclusion|reconstitution)s?\s+(?:in|of|from)\s+(?:the\s+)?"
    r"(?:indices|index)\b|index\s+(?:changes?|reconstitution)|^\s*(?:exclusion|inclusion)\s+of\s"
    r"|replacements?\s+and\s+revision|replacements?\s+in\s+indices"
    # A demerger is handled by a "corporate (action) adjustment": the resulting company enters
    # every index its parent is in on the ex-date and leaves after it lists — two real changes.
    r"|corporate\s+(?:action\s+)?adjustment",
    re.I,
)

#: "1) Nifty 50", "(2) CNX 100 Index", and — under a lettered sub-part — "a) Nifty 50".
#: Every membership release says what it does to an index; a title with none of these words is a
#: methodology or criteria notice ("Revision in stock selection methodology of the Nifty Next 50").
_CHANGE_WORD: Final = re.compile(
    r"chang|replac|inclu|exclu|reconstitut|adjustment|deferment|revocation", re.I
)

#: Titles that touch every index whatever else they name: a release that names one index's
#: methodology "and replacements in indices", and a rebalancing deferment (March 2020), which moves
#: the effective date of changes already announced.
_ALWAYS_MEMBERSHIP: Final = re.compile(
    r"replacements?\s+in\s+indices|deferment\s+of\s+index\s+rebalancing", re.I
)

_HEADING: Final = re.compile(
    r"^\s*\(?(?P<num>\d{1,2}|[a-z]|[ivx]{1,4})\s*\)\s*(?P<name>[A-Za-z&][^\n]{1,80}?)\s*$"
)
_ACTION: Final = re.compile(
    r"(?:following\s+)?(?:compan(?:y|ies)|stocks?|securit(?:y|ies)|scrips?)\s+"
    r"(?:is|are|shall\s+be|will\s+be)\s+(?:being\s+)?(?P<verb>excluded|included)",
    re.I,
)
_TABLE_HEADER: Final = re.compile(r"company\s+name", re.I)
_ROW: Final = re.compile(r"^\s*(?P<sr>\d{1,3})\s+(?P<body>\S.*?)\s*$")
_SYMBOL: Final = re.compile(r"^[A-Z0-9][A-Z0-9&\-_.]*$")
_SECTION_STOP: Final = re.compile(
    r"^\s*(?:note|notes|about\s+|disclaimer|for\s+more\s+information|place\s*:|"
    r"[A-H]\.\s+\S|the\s+above\s+)",
    re.I,
)


class ChangeAction(StrEnum):
    """Which way a change moves a company: into the index from its effective date, or out of it."""

    INCLUDE = "include"
    EXCLUDE = "exclude"
    #: A later release withdrawing an announced change before it took effect ("Exclusion revoked").
    REVOKE_INCLUDE = "revoke_include"
    REVOKE_EXCLUDE = "revoke_exclude"

    @property
    def is_revocation(self) -> bool:
        return self in (ChangeAction.REVOKE_INCLUDE, ChangeAction.REVOKE_EXCLUDE)

    @property
    def revoked(self) -> ChangeAction:
        """The action a revocation withdraws."""
        if self is ChangeAction.REVOKE_INCLUDE:
            return ChangeAction.INCLUDE
        if self is ChangeAction.REVOKE_EXCLUDE:
            return ChangeAction.EXCLUDE
        raise ValueError(f"{self} is not a revocation")


class PressRelease(BaseModel):
    """One listing entry: a release, the date it was announced, and its title as published.

    What it assumes: `announced` is the listing's `data-date` — the exchange's own dating of the
    release, and therefore the date its contents became knowable.
    What it never does: carry an effective date as fact. `title_effective` is the `w.e.f.` the
    title happens to state, used only to bound the coverage of releases not yet fetched; the
    effective date of a change is read from the body.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    announced: date
    filename: str = Field(pattern=r"^ind_prs\d{8}(?:_\d+)?\.pdf$")
    title: str = Field(min_length=1)
    title_effective: date | None = None

    @property
    def url(self) -> str:
        return press_release_url(self.filename)


class IndexChangeEvent(BaseModel):
    """One company entering or leaving one tracked index on one effective date — as announced.

    What it does: carry what the release says — the index (lake slug), the action, the company's
    name and NSE symbol as printed, the effective date, and the announcement (knowable) date.
    What it assumes: the symbol is the one the company traded under on the effective date; the
    builder resolves it to an ISIN *at that date* (invariant #2), never by today's symbol table.
    What it never does: carry an ISIN — the release has none, and one guessed here would bypass
    the identity resolution that makes the join legitimate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index_slug: str
    action: ChangeAction
    company_name: str = Field(min_length=1)
    symbol: str | None = Field(default=None, description="NSE symbol as printed, when printed")
    effective: date
    announced: date
    release: str = Field(description="the release filename — the provenance of this event")
    l0_key: str | None = None


class PressReleaseParse(BaseModel):
    """Everything one release yielded: its events, and the sections it could not read.

    `unparsed` names each tracked-index section the parser saw but could not turn into events
    (no effective date, a table it could not segment, a row with no company). A release with a
    non-empty `unparsed` is evidence that the reconstruction is incomplete at its date — the
    builder stops that index's depth there rather than reconstructing across a hole.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    release: str
    announced: date
    events: tuple[IndexChangeEvent, ...]
    unparsed: tuple[str, ...] = ()
    tracked_sections: tuple[str, ...] = ()


# ── the listing ────────────────────────────────────────────────────────────────────────────────


def press_release_url(filename: str) -> str:
    """The release PDF's URL. Refuses anything not shaped like a release filename."""
    if not _PDF_NAME.fullmatch(filename):
        raise ParseError("not a press-release filename", filename=filename)
    return _PDF_BASE + filename


def l0_listing_filename(as_of: date) -> str:
    """The L0 name of one capture of the listing page — dated, because the URL is not."""
    return f"press_release_listing_{as_of:%Y%m%d}.html"


def parse_press_release_listing(payload: bytes, *, filename: str) -> tuple[PressRelease, ...]:
    """Parse the listing page into its releases, newest first, deduplicated by filename.

    Raises `ParseError` when the page holds no `pressItem` at all — the site's shell answering
    instead of the listing is a gate, not an empty archive.
    """
    text = payload.decode("utf-8", errors="replace")
    releases: dict[str, PressRelease] = {}
    for match in _LISTING_ITEM.finditer(text):
        href = match.group("href")
        name = href.rsplit("/", 1)[-1]
        if not _PDF_NAME.fullmatch(name):
            continue  # a release published as something other than ind_prs*.pdf — not ours
        announced = parse_date_phrase(match.group("date"))
        if announced is None:
            raise ParseError(f"unreadable data-date {match.group('date')!r}", filename=filename)
        title = " ".join(html.unescape(match.group("title")).split())
        releases.setdefault(
            name.lower(),
            PressRelease(
                announced=announced,
                filename=name,
                title=title,
                title_effective=_title_effective(title),
            ),
        )
    if not releases:
        raise ParseError("no pressItem entries — not the press-release listing", filename=filename)
    ordered = tuple(
        sorted(releases.values(), key=lambda r: (r.announced, r.filename), reverse=True)
    )
    _LOG.info(
        "index_changes.listing_parsed",
        source=PRESS_RELEASE_SOURCE_ID,
        filename=filename,
        releases=len(ordered),
        oldest=ordered[-1].announced.isoformat(),
        newest=ordered[0].announced.isoformat(),
        state="VALIDATED",
    )
    return ordered


def is_membership_candidate(title: str) -> bool:
    """Could this release change the membership of a tracked index? — decided from the title.

    Kept: a title naming a tracked index, or a generic change title ("Replacements in indices",
    "Exclusion of X from Nifty indices"). Dropped: debt/IPO/SME/launch notices, and titles naming
    only some other index (`Change in NIFTY PSU Bank Index`). The filter errs toward fetching: a
    generic title that turns out to touch no tracked index costs one request, while a dropped one
    that did would be a hole in the history.
    """
    if _NOT_MEMBERSHIP.search(title) or not _CHANGE_WORD.search(title):
        return False
    if _NAMES_TRACKED.search(title):
        return True
    if _ALWAYS_MEMBERSHIP.search(title):
        return True
    if _GENERIC_CHANGE.search(title):
        named = [m.group(0) for m in _NAMES_OTHER_INDEX.finditer(title)]
        return not named or all(re.search(r"nifty\s+indices", n, re.I) for n in named)
    return False


def _title_effective(title: str) -> date | None:
    match = _EFFECTIVE_INTRO.search(title)
    return None if match is None else parse_date_phrase(title[match.end() :])


def parse_date_phrase(text: str) -> date | None:
    """The first calendar date written at the start of `text`, in any of the releases' styles.

    Accepts "September 30, 2026", "Sept. 30,2026", "Apr. 25, 2003", "30th September 2026",
    "30-Sep-2026" and the listing's "Aug 10, 2026". Returns `None` rather than guessing when the
    text does not open with a date (leading whitespace and a colon are tolerated).
    """
    head = text.lstrip(" :\t\n")[:40]
    for pattern in (_DATE_MDY, _DATE_DMY):
        match = pattern.match(head)
        if match is None:
            continue
        try:
            return date(
                int(match.group("year")),
                _MONTHS[match.group("month").lower().rstrip(".")],
                int(match.group("day").replace(" ", "")),
            )
        except (KeyError, ValueError):
            return None
    return None


# ── fetching (through the crawl engine only) ───────────────────────────────────────────────────


def fetch_listing(fetcher: Fetcher, *, as_of: date) -> L0Ref:
    """Capture the listing page into L0 under `as_of` — one request indexes the whole archive."""
    return fetcher.fetch(
        PRESS_RELEASE_SOURCE_ID, LISTING_URL, as_of, filename=l0_listing_filename(as_of)
    )


def fetch_press_release(fetcher: Fetcher, release: PressRelease) -> L0Ref:
    """Fetch one release PDF into L0, filed under its announcement date.

    The announcement date is the release's own logical date, so a re-fetch of the same release is
    the same L0 key (an idempotent no-op for identical bytes, a loud `L0ImmutabilityError` if the
    exchange ever re-issued different bytes under the same name).
    """
    return fetcher.fetch(
        PRESS_RELEASE_SOURCE_ID, release.url, release.announced, filename=release.filename
    )


# ── the release body ───────────────────────────────────────────────────────────────────────────


def canonical_index_slug(heading: str) -> str | None:
    """The tracked lake slug a section heading names, or `None` for every other index."""
    folded = html.unescape(heading).lower()
    folded = re.sub(r"\(.*?\)", " ", folded)
    folded = re.sub(r"\bindex\b|\bindices\b", " ", folded)
    folded = re.sub(r"[^a-z0-9& ]+", " ", folded)
    folded = " ".join(folded.split())
    return _INDEX_ALIASES.get(folded)


def parse_press_release_l0(store: L0Store, ref: L0Ref, release: PressRelease) -> PressReleaseParse:
    """Parse a release PDF already in L0, re-verifying its checksum on the way in."""
    return parse_press_release_pdf(
        store.get(ref),
        filename=ref.filename,
        announced=release.announced,
        l0_key=ref.key,
    )


def parse_press_release_pdf(
    payload: bytes, *, filename: str, announced: date, l0_key: str | None = None
) -> PressReleaseParse:
    """Read one release into its tracked-index change events.

    Assumes `payload` is the PDF the exchange published and `announced` its listing date. Walks
    the extracted text line by line: a numbered heading opens a section (kept only when it names a
    tracked index), an "is being excluded/included" line sets the action (and, when it carries a
    `w.e.f.`, that action's date), a "Company Name" line opens a table, and each `<n> <name>
    <SYMBOL>` row becomes an event. The effective date is the nearest one stated before the
    section — "effective from …" anywhere in the text updates it — so a release whose parts take
    effect on different dates dates each part correctly.

    Raises `ParseError` for bytes that are not a readable PDF. A tracked section it cannot read is
    returned in `unparsed`, never silently skipped and never half-read into events.
    """
    lines = _pdf_lines(payload, filename=filename)
    events: list[IndexChangeEvent] = []
    unparsed: list[str] = []
    tracked: list[str] = []

    current_date: date | None = None
    section: str | None = None  # tracked slug of the open section, or None for an untracked one
    section_label = ""
    section_date: date | None = None
    action: ChangeAction | None = None
    action_date: date | None = None
    in_table = False
    pending: list[str] = []  # the row being accumulated (a long name wraps onto the next line)
    section_events: list[IndexChangeEvent] = []
    section_problem: str | None = None
    section_undated: list[tuple[ChangeAction, str, str | None]] = []
    # Sections whose rows came before any effective date; dated at the end if the release states
    # exactly one (ind_prs01082018.pdf puts "These changes shall become effective from …" last).
    deferred: list[tuple[str, str, list[tuple[ChangeAction, str, str | None]]]] = []
    saw_action = False
    action_rows = 0  # rows read under the open action — an action with none is a detached table

    def flush_row() -> None:
        nonlocal section_problem
        if not pending:
            return
        body = " ".join(pending).strip()
        pending.clear()
        if section is None or action is None:
            return
        effective = action_date or section_date
        name, symbol = _split_row(body)
        nonlocal action_rows
        action_rows += 1
        if not name:
            section_problem = section_problem or f"row without a company name: {body!r}"
            return
        if effective is None:
            section_undated.append((action, name, symbol))
            return
        section_events.append(
            IndexChangeEvent(
                index_slug=section,
                action=action,
                company_name=name,
                symbol=symbol,
                effective=effective,
                announced=announced,
                release=filename,
                l0_key=l0_key,
            )
        )

    def close_action() -> None:
        nonlocal section_problem, action_rows
        flush_row()
        if section is not None and action is not None and action_rows == 0:
            # Pre-2015 releases print every statement first and every table after, detached; a
            # statement with no rows of its own is that layout, and its rows cannot be attributed.
            section_problem = section_problem or (
                f"the '{action.value}' statement has no table of its own (detached layout)"
            )
        action_rows = 0

    def close_section() -> None:
        nonlocal section, section_events, section_problem, saw_action, section_undated
        close_action()
        if section is not None:
            if (
                section_problem is None
                and saw_action
                and not section_events
                and not section_undated
            ):
                section_problem = "an include/exclude statement with no table rows"
            if section_problem is not None:
                unparsed.append(f"{section_label}: {section_problem}")
            else:
                events.extend(section_events)
                if section_undated:
                    deferred.append((section_label, section, section_undated))
        section = None
        section_events = []
        section_problem = None
        section_undated = []
        saw_action = False

    for raw in lines:
        line = " ".join(raw.split())
        if not line:
            continue
        intro = _EFFECTIVE_INTRO.search(line)
        stated = None if intro is None else parse_date_phrase(line[intro.end() :])

        heading = _HEADING.match(line)
        if heading is not None and not _ACTION.search(line) and not _ROW_LOOKS_LIKE_DATA(line):
            close_section()
            action = None
            name = heading.group("name")
            slug = canonical_index_slug(name)
            section = slug
            section_label = name.strip()
            section_date = stated or current_date
            action = None
            action_date = None
            in_table = False
            if slug is not None:
                tracked.append(slug)
            continue

        verb = _ACTION.search(line)
        if verb is not None:
            close_action()
            action = (
                ChangeAction.EXCLUDE
                if verb.group("verb").lower() == "excluded"
                else ChangeAction.INCLUDE
            )
            action_date = stated
            in_table = False
            saw_action = section is not None
            continue

        if stated is not None:
            # "These changes shall become effective from …" re-dates everything after it.
            flush_row()
            current_date = stated
            if section is not None and not in_table:
                section_date = stated
            continue

        if _TABLE_HEADER.search(line) and len(line) < 60:
            flush_row()
            in_table = True
            continue
        if line.lower() in {"sr.", "sr. no.", "no.", "sr no"}:
            continue

        if in_table:
            if _SECTION_STOP.match(line) or line.lower().startswith("page "):
                flush_row()
                in_table = False
                continue
            row = _ROW.match(line)
            if re.fullmatch(r"\d{1,3}", line):
                flush_row()
                pending.append("")  # the serial alone; the name wraps onto the next lines
            elif row is not None:
                flush_row()
                pending.append(row.group("body"))
            elif pending and _split_row(" ".join(pending))[1] is None and len(line) < 60:
                pending.append(line)  # a long company name wrapped before its symbol
            else:
                # A complete row followed by prose: the table is over (an unlabelled note).
                flush_row()
                in_table = False
            continue

    close_section()
    if deferred:
        release_dates = _stated_effective_dates(lines)
        for label, slug, rows in deferred:
            if len(release_dates) != 1:
                unparsed.append(f"{label}: no effective date stated for the section")
                continue
            (only,) = release_dates
            events.extend(
                IndexChangeEvent(
                    index_slug=slug,
                    action=row_action,
                    company_name=name,
                    symbol=symbol,
                    effective=only,
                    announced=announced,
                    release=filename,
                    l0_key=l0_key,
                )
                for row_action, name, symbol in rows
            )
    events.extend(_index_list_events(lines, filename=filename, announced=announced, l0_key=l0_key))
    remark_events, remark_problems = _remark_table_events(
        lines, filename=filename, announced=announced, l0_key=l0_key
    )
    events.extend(remark_events)
    unparsed.extend(remark_problems)

    result = PressReleaseParse(
        release=filename,
        announced=announced,
        events=tuple(events),
        unparsed=tuple(unparsed),
        tracked_sections=tuple(dict.fromkeys(tracked)),
    )
    _LOG.info(
        "index_changes.release_parsed",
        source=PRESS_RELEASE_SOURCE_ID,
        filename=filename,
        announced=announced.isoformat(),
        events=len(result.events),
        unparsed=len(result.unparsed),
        tracked_sections=list(result.tracked_sections),
        state="VALIDATED" if not result.unparsed else "PARTIAL",
    )
    for problem in result.unparsed:
        _LOG.error(
            "index_changes.section_unparsed",
            source=PRESS_RELEASE_SOURCE_ID,
            filename=filename,
            announced=announced.isoformat(),
            problem=problem,
            state="QUARANTINED",
        )
    return result


def _stated_effective_dates(lines: list[str]) -> set[date]:
    """Every distinct date the release introduces as an effective date, anywhere in its text."""
    text = " ".join(" ".join(line.split()) for line in lines)
    found = (parse_date_phrase(text[m.end() :]) for m in _EFFECTIVE_INTRO.finditer(text))
    return {d for d in found if d is not None}


#: The one-column table a spin-off exclusion lists its indices in ("Sr. No. Index Name").
_INDEX_LIST_HEADER: Final = re.compile(r"^\s*(?:sr\.?\s*)?(?:no\.?\s*)?index\s+name\s*$", re.I)
_INDEX_LIST_ROW: Final = re.compile(r"^\s*\d{1,3}\s+(?P<index>\S.*?)\s*$")
#: "…decided to exclude ITC Hotels Ltd. (ITCHOTELS) from various indices…", "…exclude JIOFIN from…".
_EXCLUDE_NAMED: Final = re.compile(
    r"decided\s+to\s+exclude\s+(?:(?P<name>[^()]{2,120}?)\s*\(\s*(?P<sym>[A-Z0-9&\-]{2,20})\s*\)"
    r"|(?P<bare>[A-Z][A-Z0-9&\-]{1,19}))\s+from",
)


def _index_list_events(
    lines: list[str], *, filename: str, announced: date, l0_key: str | None
) -> list[IndexChangeEvent]:
    """A spin-off exclusion: the company named in prose, its indices in an "Index Name" table.

    After a demerger the resulting company sits in its parent's indices until it has listed and
    traded freely; its exit is announced as "decided to exclude <Name> (<SYMBOL>) from various
    indices as listed hereunder effective from <date>" over a one-column table of index names.
    Only that exact shape is read — a release that names no symbol yields nothing here.
    """
    text = " ".join(" ".join(line.split()) for line in lines)
    match = _EXCLUDE_NAMED.search(text)
    if match is None:
        return []
    intro = _EFFECTIVE_INTRO.search(text, match.end())
    effective = None if intro is None else parse_date_phrase(text[intro.end() :])
    if effective is None:
        return []
    symbol = match.group("sym") or match.group("bare")
    name = (match.group("name") or symbol).strip()
    slugs: list[str] = []
    in_list = False
    for raw in lines:
        line = " ".join(raw.split())
        if _INDEX_LIST_HEADER.match(line):
            in_list = True
            continue
        if not in_list:
            continue
        row = _INDEX_LIST_ROW.match(line)
        if row is None:
            if line and not line.lower().startswith(("sr", "no")):
                in_list = False
            continue
        slug = canonical_index_slug(row.group("index"))
        if slug is not None and slug not in slugs:
            slugs.append(slug)
    return [
        IndexChangeEvent(
            index_slug=slug,
            action=ChangeAction.EXCLUDE,
            company_name=name,
            symbol=symbol,
            effective=effective,
            announced=announced,
            release=filename,
            l0_key=l0_key,
        )
        for slug in slugs
    ]


_REMARK_HEADER: Final = re.compile(r"index\s+name\s+security\s+name\s+symbol\s+remarks", re.I)
_REMARK_ROW: Final = re.compile(
    r"^(?P<lead>.*?)\s*(?P<sym>[A-Z][A-Z0-9&\-]{1,19})\s+"
    r"(?P<remark>exclusion\s+revoked|inclusion\s+revoked|exclusion|inclusion)\s*$",
    re.I,
)
_REMARK_ACTIONS: Final[Mapping[str, ChangeAction]] = {
    "exclusion revoked": ChangeAction.REVOKE_EXCLUDE,
    "inclusion revoked": ChangeAction.REVOKE_INCLUDE,
    "exclusion": ChangeAction.EXCLUDE,
    "inclusion": ChangeAction.INCLUDE,
}


def _remark_table_events(
    lines: list[str], *, filename: str, announced: date, l0_key: str | None
) -> tuple[list[IndexChangeEvent], list[str]]:
    """A revocation table: "Sr. No. | Index Name | Security Name | Symbol | Remarks".

    Used when a review is partly withdrawn (2024-09-25: Vodafone Idea's exclusion revoked, and
    the knock-on changes). Each row is `[<n> <index>] <security> <SYMBOL> <remark>`; a row without
    a leading number belongs to the index above it, and an index name may wrap onto its own line.
    The effective date is the first one stated after the table starts.
    """
    events: list[IndexChangeEvent] = []
    problems: list[str] = []
    start = next((i for i, line in enumerate(lines) if _REMARK_HEADER.search(line)), None)
    if start is None:
        return events, problems
    # The date the table's changes take effect: the last one stated before it (2024-03-19 states
    # it in the opening paragraph), else the first one after it (2024-09-25 states it below).
    before = " ".join(" ".join(line.split()) for line in lines[:start])
    after = " ".join(" ".join(line.split()) for line in lines[start:])
    stated_before = [
        parse_date_phrase(before[m.end() :]) for m in _EFFECTIVE_INTRO.finditer(before)
    ]
    stated_after = [parse_date_phrase(after[m.end() :]) for m in _EFFECTIVE_INTRO.finditer(after)]
    effective = next((d for d in reversed(stated_before) if d is not None), None) or next(
        (d for d in stated_after if d is not None), None
    )
    current: str | None = None  # tracked slug of the index the rows belong to
    index_words: list[str] = []  # the index name, which may wrap over lines
    security_words: list[str] = []  # a security name wrapped before its symbol
    rows: list[tuple[str, ChangeAction, str, str]] = []
    for raw in lines[start + 1 :]:
        line = " ".join(raw.split())
        if not line or _REMARK_HEADER.search(line) or line.lower() in {"sr.", "no."}:
            continue
        if line.startswith(("*", "#")) or _SECTION_STOP.match(line):
            break
        lead_num = re.match(r"^(\d{1,2})\s+(.*)$", line)
        body = line
        if lead_num is not None:
            body = lead_num.group(2)
            index_words, security_words, current = [], [], None
        row = _REMARK_ROW.match(body)
        if row is None:
            if lead_num is not None or (current is None and index_words):
                index_words.append(body)
                current = canonical_index_slug(" ".join(index_words))
            elif current is not None:
                security_words.append(body)
            continue
        lead = row.group("lead").split()
        if current is None and (lead_num is not None or index_words):
            # "<index> <security> …" on one line: the longest prefix that names an index.
            for k in range(min(6, len(lead)), 0, -1):
                named = canonical_index_slug(" ".join([*index_words, *lead[:k]]))
                if named is not None:
                    current, lead = named, lead[k:]
                    break
        security = " ".join([*security_words, *lead])
        security_words = []
        if current is None:
            continue
        action = _REMARK_ACTIONS[" ".join(row.group("remark").lower().split())]
        rows.append((current, action, security.rstrip("*#"), row.group("sym")))
    if rows and effective is None:
        problems.append("revocation table: no effective date stated")
        return events, problems
    assert effective is not None or not rows
    for slug, action, security, symbol in rows:
        assert effective is not None
        events.append(
            IndexChangeEvent(
                index_slug=slug,
                action=action,
                company_name=security or symbol,
                symbol=symbol,
                effective=effective,
                announced=announced,
                release=filename,
                l0_key=l0_key,
            )
        )
    return events, problems


def _ROW_LOOKS_LIKE_DATA(line: str) -> bool:  # noqa: N802 — reads as the predicate it is
    """A table row like `1) ABC Ltd. ABC` is not a heading; a heading has no trailing SYMBOL."""
    tokens = line.split()
    return (
        len(tokens) >= 3
        and bool(_SYMBOL.match(tokens[-1]))
        and tokens[-2].lower() in {"ltd.", "ltd", "limited"}
    )


def _split_row(body: str) -> tuple[str, str | None]:
    """Split a table row's text into (company name, symbol) — the symbol is the trailing token.

    A symbol is upper case with no lower-case letter (`ABFRL`, `M&M`, `BAJAJ-AUTO`, `3MINDIA`); a
    name ends in `Ltd.`/`Limited`/a word with lower case. A row with no such trailing token (the
    pre-2015 tables) yields `(name, None)`, and the builder quarantines it rather than guess.
    """
    tokens = body.split()
    if len(tokens) >= 2 and _SYMBOL.match(tokens[-1]) and not tokens[-1].isdigit():
        name = " ".join(tokens[:-1])
        if re.search(r"[a-z]", name) or name.endswith((".", "LTD", "LIMITED", "REIT", "TRUST")):
            return name, tokens[-1]
    return " ".join(tokens), None


def _pdf_lines(payload: bytes, *, filename: str) -> list[str]:
    if not payload.startswith(b"%PDF"):
        raise ParseError("not a PDF (no %PDF header) — a soft-404 or a gate", filename=filename)
    try:
        reader = PdfReader(io.BytesIO(payload))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PdfReadError, ValueError, KeyError) as exc:
        raise ParseError(f"unreadable PDF: {exc}", filename=filename) from exc
    if len("".join(text.split())) < _MIN_TEXT_CHARS:
        # ind_prs23082021.pdf (the 2021-09 semi-annual review, 29 pages) draws every glyph as an
        # image: there is no text layer to read. Without OCR it is a hole, and it must say so.
        raise ParseError(
            "no extractable text — an image-only PDF; reading it needs OCR, which this platform "
            "does not have",
            filename=filename,
        )
    return _join_split_dates(text.splitlines())


#: Fewer non-blank characters than this and the PDF has no text layer worth the name.
_MIN_TEXT_CHARS: Final = 200


def _join_split_dates(lines: list[str]) -> list[str]:
    """Rejoin an effective date that the PDF wrapped across lines ("effective from October" /
    "16, 2024 (close of …)") so the date reads as one phrase; every other line is untouched."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        intros = list(_EFFECTIVE_INTRO.finditer(line))
        if intros and i + 1 < len(lines):
            tail = line[intros[-1].end() :]
            if parse_date_phrase(tail) is None and len(tail.strip()) < 20:
                line = f"{line.rstrip()} {lines[i + 1].strip()}"
                i += 1
        out.append(line)
        i += 1
    return out


def candidate_releases(releases: Iterable[PressRelease]) -> tuple[PressRelease, ...]:
    """The releases that could change a tracked index's membership, newest first."""
    return tuple(r for r in releases if is_membership_candidate(r.title))
