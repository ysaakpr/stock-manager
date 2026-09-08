"""The NSE daily report bundle (`PR<DDMMYY>.zip`) — opening it and dating it (W2).

One zip per trading session on the archive host, carrying ~14-25 member reports. The bundle is
registered as `nse_pr_bundle`; W2 parses four of its members and *registers* the rest, so a
later task adding one starts from a name this module already knows rather than from a guess.

A registry entry is load-bearing, and one wrong word in it cost three years of data: `FFIX` was
documented here as "Fixed income" until 2026-09-08, and nobody opened the member to check. It is
the free-float **index membership** file (`ffix.py`). Read a member before you name it.

**Why this source exists in the platform at all.** `Bc<date>.csv` is a corporate-action list whose
*file date is the broadcast date*. Every other corporate-action surface we hold is a query against
"what is true now", which is why all 47,887 rows in `corporate_actions` share one `knowable_date`
and invariant #7 is satisfied vacuously. A dated bundle is the only thing that fixes that, and the
dating therefore has to be exact — see `publication_date`.

**Publication date is derived from the payload, never from a clock.** Nothing in this package
imports a clock, calls `datetime.now()`, or accepts one, and `tests/unit/test_pr_bundle_bc.py`
fails if that changes. The date comes from the digits the members carry in their own names, which
survive re-download, re-zipping and mirroring; the archive filename is used only as a cross-check.

**Format eras** (39 requests on 2026-09-08, plus 12 of bisection on the same day;
`ops/studies/evidence/nse-pr-bundle.md`):

| era | span | member names | `Bc` dates | `Ix` | `mcap` |
|---|---|---|---|---|---|
| `ix_era`     | 2010-01-04 → ~2010-10 | `Bc040110` upper, DDMMYY | `DD/MM/YYYY` | yes | no |
| `classic`    | ~2010-10 → 2024-01-31 | same                     | `DD/MM/YYYY` | no  | no |
| `mcap_upper` | 2024-02-01 → 2025-10-10 | `Bc010724`+`MCAP01072024` [*] | `DD/MM/YYYY` | no | yes |
| `lowercase`  | 2025-10-13 → open     | `bc04092026` lower, DDMMYYYY | `YYYY-MM-DD` | no | yes |

[*] DDMMYY **and** DDMMYYYY member names inside one zip, which is why name width is read per
member and never per bundle.

Three of the four boundaries are dates; `ix_era`→`classic` is still the bracket
(2010-10-04, 2010-10-18], because the `Ix` member's disappearance changes no parser behaviour —
`ix.py` documents it as a 2010-only validation asset either way. Nothing here dispatches on an
era: the readers sniff casing, name width and date shape from the bytes in front of them, so a
boundary is documentation accuracy and never correctness.

Offline by construction: this module takes bytes, or an `L0Ref` it reads back through `L0Store`.
It never fetches.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from io import BytesIO
from typing import Final

from dataplatform.ingest.models import ParseError
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "ARCHIVE_START",
    "LOWERCASE_ERA_START",
    "MCAP_ERA_START",
    "PR_BUNDLE_SOURCE_ID",
    "URL_TEMPLATE",
    "BundleMember",
    "MemberKind",
    "PrBundle",
    "url_for",
]

_LOG = get_logger(__name__)

#: The register id this package serves (`source_register.yaml`, `parser.task: W2`).
PR_BUNDLE_SOURCE_ID: Final = "nse_pr_bundle"

#: The archive URL, `{DDMMYY}` being the session date. Verified 2026-09-08 across 2010-2026.
URL_TEMPLATE: Final = (
    "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{DDMMYY}.zip"
)

#: The first session the bundle archive serves, pinned exactly rather than bracketed:
#: `PR311209.zip` and `PR010110.zip` are both 404 and `PR040110.zip` is 200 (2026-09-08). Probes
#: at 2005, 2007, 2008 and four points across 2009 are all 404, so nothing older is published.
ARCHIVE_START: Final = date(2010, 1, 4)

#: The first bundle carrying an `mcap` member — the `classic` → `mcap_upper` boundary, pinned by
#: bisection on 2026-09-08 (7 requests) inside the bracket Phase 1 left it in. `PR310124.zip`
#: carries `Bc310124.csv` alone; `PR010224.zip` adds `MCAP01022024.csv`, and every bundle measured
#: after it carries one.
MCAP_ERA_START: Final = date(2024, 2, 1)

#: The first bundle with lowercase member names — the `mcap_upper` → `lowercase` boundary, pinned
#: by bisection on 2026-09-08 (5 requests). One event, not three: `PR101025.zip` has
#: `Bc101025.csv` + `MCAP10102025.csv` with `Bc` dates `13/10/2025`, and the next session's
#: `PR131025.zip` has `bc13102025.csv` + `mcap13102025.csv` with `2025-10-17` — casing, name width
#: and date shape all change together.
LOWERCASE_ERA_START: Final = date(2025, 10, 13)


class MemberKind(StrEnum):
    """Every member name the bundle has been seen to carry, parsed or merely registered.

    Registering the unparsed members is the point: a later task that wants board meetings starts
    from `MemberKind.BM` and the evidence log's column notes, and `PrBundle.unknown_members`
    reports a name that is in *no* era we measured — a format change worth an alert rather than a
    file quietly ignored.

    The value is the lowercased filename stem, because casing is exactly what changed across the
    2025 cutover (`Bc020125.csv` → `bc02012026.csv`) and is therefore the one thing a member's
    identity must not depend on.
    """

    # ── parsed by W2 ──
    BC = "bc"
    """Corporate actions, broadcast-dated by the file itself. The reason this source exists."""
    IX = "ix"
    """Index membership with issue-cap, market cap and weightage. 2010 only; see `ix.py`."""
    FFIX = "ffix"
    """Dated index constituent membership with free-float weightage. 2010-01-04..2013-04-30.

    **This entry said "Fixed income" until 2026-09-08 and that was wrong.** `ffix` is
    *free-float index*, and the payload is the constituent list of every index NSE published
    that session, with each member's investible factor, close, free-float market cap and index
    weightage. Nothing in it is a bond. The mislabel is why 827 bundles of dated index
    membership sat unread while `ops/BACKLOG.md:126` recorded that no such history exists to
    fetch. See `ffix.py`.
    """
    MCAP = "mcap"
    """Daily issue size, market cap and last-trade-date, per symbol. ~2024-07 onward."""

    # ── registered, not parsed by W2 ──
    AN = "an"
    """Corporate announcements (text)."""
    BM = "bm"
    """Board meetings (text)."""
    BH = "bh"
    """Price-band hits."""
    PD = "pd"
    """52-week high/low, price detail."""
    PR = "pr"
    """The headline daily price report the bundle is named for."""
    GL = "gl"
    """Gainers and losers."""
    HL = "hl"
    """New highs and lows."""
    TT = "tt"
    """Top traded."""
    RTT = "rtt"
    """Retail top traded."""
    RPD = "rpd"
    """Retail price detail."""
    NPD = "npd"
    """Non-promoter detail."""
    ETF = "etf"
    """Exchange-traded funds."""
    SME = "sme"
    """SME platform."""
    CORPBOND = "corpbond"
    """Corporate bonds."""
    FO = "fo"
    """Futures and options."""
    OP = "op"
    """Options."""
    CD = "cd"
    """Currency derivatives."""
    CF = "cf"
    """Currency futures."""
    CO = "co"
    """Currency options."""
    PE = "pe_"
    """Index P/E. Seen only at 2025-01-02; the trailing underscore is part of the stem."""

    @property
    def parsed_by_w2(self) -> bool:
        """Whether this package turns the member into rows, as opposed to merely naming it."""
        return self in _PARSED


_PARSED: Final[frozenset[MemberKind]] = frozenset(
    {MemberKind.BC, MemberKind.FFIX, MemberKind.IX, MemberKind.MCAP}
)

#: Members carrying no date and no rows — documentation shipped inside every bundle. Named so
#: they are not mistaken for an unrecognised report.
_DOC_MEMBERS: Final[frozenset[str]] = frozenset(
    {"readme.txt", "readmenew.txt", "help.txt", "rdm_help.txt", "rdm.doc", "rdm.docx", "nuver.txt"}
)

#: `<stem><6 or 8 digits>.<ext>`. The stem is letters and underscores (`PE_020125.csv`); the digit
#: run is DDMMYY or DDMMYYYY and both appear *inside one zip* — `Bc010724.csv` sits beside
#: `MCAP01072024.csv` in the 2024 era, which is why width is read per member and never per bundle.
_MEMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<stem>[A-Za-z_]+?)(?P<digits>\d{6}|\d{8})\.(?P<ext>[A-Za-z]+)$"
)

#: `PR<DDMMYY>.zip`, the archive's own filename. Used only to cross-check the members' agreement.
_ARCHIVE_RE: Final[re.Pattern[str]] = re.compile(r"^PR(?P<digits>\d{6})\.zip$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class BundleMember:
    """One member of one bundle: what it is, what it is called, and when it says it is from.

    `kind` is `None` for a name no era we measured produced — kept rather than dropped so
    `PrBundle.unknown_members` can report it.
    """

    name: str
    kind: MemberKind | None
    member_date: date | None
    size_bytes: int

    @property
    def is_documentation(self) -> bool:
        """Whether this is one of the readme/help files every bundle ships."""
        return self.name.lower() in _DOC_MEMBERS


def url_for(day: date) -> str:
    """The archive URL for one session's bundle.

    Assumes `day` is a trading date. Makes no claim that a bundle exists for it: sessions before
    `ARCHIVE_START` are simply not published, and the archive answers 404 for them — which is
    evidence about the source, not an error (`fetcher` raises `FetchHTTPError` and the caller
    records it).
    """
    return URL_TEMPLATE.format(DDMMYY=day.strftime("%d%m%y"))


class PrBundle:
    """An opened PR bundle: its members, and the publication date they agree on.

    What it does: indexes the zip's members by kind, derives the bundle's publication date from
    the dates the members carry in their own names, and hands one member's bytes to a reader.
    What it assumes: the payload is the whole zip. Bundles are a few hundred kilobytes, so holding
    one whole is free and a truncation raises rather than being streamed past.
    What it never does: read a clock, fetch, or guess a date for a bundle whose members disagree.
    """

    def __init__(self, payload: bytes, *, filename: str) -> None:
        self._filename = filename
        try:
            self._zip = zipfile.ZipFile(BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise ParseError(
                f"not a zip archive: {exc}; an HTML error page answered with a 200 looks exactly "
                "like this and must not become rows",
                filename=filename,
            ) from exc
        self._members: tuple[BundleMember, ...] = tuple(
            _classify(info.filename, info.file_size) for info in self._zip.infolist()
        )
        self._publication_date = _publication_date(self._members, filename=filename)
        _LOG.info(
            "pr_bundle.opened",
            source=PR_BUNDLE_SOURCE_ID,
            filename=filename,
            publication_date=self._publication_date.isoformat(),
            members=len(self._members),
            unknown=len(self.unknown_members),
            state="VALIDATED",
        )

    # ── identity ─────────────────────────────────────────────────────────────────────────────

    @property
    def publication_date(self) -> date:
        """The session this bundle publishes — **derived from the file, never from a clock**.

        This is the value that becomes `knowable_date` on every `Bc` row, and it is the whole
        reason the source is worth fetching: an action in `Bc020113.csv` was knowable to the
        market on 2013-01-02 and on no earlier date, so a backtest deciding on 2013-01-01 must
        not see it. `dataplatform.ingest.bse.corp_actions` stamps `clock.now().date()` instead,
        which is what makes today's `corporate_actions` table PIT-honest only vacuously.

        Derived from the digit runs in the *member* names — inside the payload, so they survive
        re-download and mirroring — and cross-checked against the archive filename. Members that
        disagree raise rather than voting, because a bundle whose own members cannot agree what
        day they are is not a bundle to date an action from.
        """
        return self._publication_date

    @property
    def filename(self) -> str:
        """The archive filename this bundle was stored under, for errors and logs."""
        return self._filename

    # ── members ──────────────────────────────────────────────────────────────────────────────

    @property
    def members(self) -> tuple[BundleMember, ...]:
        """Every member, in the zip's own order."""
        return self._members

    @property
    def unknown_members(self) -> tuple[BundleMember, ...]:
        """Members whose name matches no `MemberKind` and is not documentation.

        A non-empty tuple is a format change: NSE added a report. It is surfaced rather than
        raised, because an unrecognised *extra* member does not stop the members we do read.
        """
        return tuple(m for m in self._members if m.kind is None and not m.is_documentation)

    def member(self, kind: MemberKind) -> BundleMember | None:
        """The bundle's member of one kind, or `None` when this era does not publish it.

        `None` is the normal answer for `IX` after 2010 and for `MCAP` before ~2024 — absence is
        a measured property of the era, not a failure.
        """
        return next((m for m in self._members if m.kind is kind), None)

    def has(self, kind: MemberKind) -> bool:
        """Whether this bundle carries a member of that kind."""
        return self.member(kind) is not None

    def read(self, kind: MemberKind) -> bytes:
        """The bytes of one member.

        Raises `ParseError` naming the bundle and the members it does carry when the kind is
        absent — a caller asking for `IX` in 2013 has made a planning mistake, and the member
        list is what tells them so.
        """
        member = self.member(kind)
        if member is None:
            raise ParseError(
                f"bundle carries no {kind.value!r} member; it has "
                f"{', '.join(sorted(m.name for m in self._members))}",
                filename=self._filename,
            )
        return self._zip.read(member.name)

    def close(self) -> None:
        """Release the underlying zip."""
        self._zip.close()

    def __enter__(self) -> PrBundle:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── construction ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def from_l0(cls, store: L0Store, ref: L0Ref) -> PrBundle:
        """Open the L0 payload a fetch produced, re-verifying its checksum on the way in.

        The pipeline's entry point: `Fetcher.fetch` returns an `L0Ref` and never bytes, so this is
        how a fetched bundle becomes rows, with `L0Store.get` re-hashing the payload so every row
        derives from bytes that have not changed (invariant #1).
        """
        return cls(store.get(ref), filename=ref.filename)


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _classify(name: str, size_bytes: int) -> BundleMember:
    """Read one member's name into a kind and the date it carries, both case-insensitively."""
    match = _MEMBER_RE.match(name)
    if match is None:
        return BundleMember(name=name, kind=None, member_date=None, size_bytes=size_bytes)
    stem = match.group("stem").lower()
    kind = next((k for k in MemberKind if k.value == stem), None)
    return BundleMember(
        name=name,
        kind=kind,
        member_date=_member_date(match.group("digits")),
        size_bytes=size_bytes,
    )


def _member_date(digits: str) -> date | None:
    """`DDMMYY` or `DDMMYYYY` → a date; `None` when the digits are not a real one.

    Both widths appear inside a single zip, so width is decided per member. A two-digit year is
    read as `20YY`: the archive begins in 2010 and this platform will not outlive the century that
    assumption holds for. `None` rather than a raise, because a digit run that is not a date means
    "this member is not date-stamped", which `_publication_date` handles by ignoring it.
    """
    try:
        day, month = int(digits[0:2]), int(digits[2:4])
        year = 2000 + int(digits[4:6]) if len(digits) == 6 else int(digits[4:8])
        return date(year, month, day)
    except ValueError:
        return None


def _publication_date(members: tuple[BundleMember, ...], *, filename: str) -> date:
    """The one date every dated member agrees on, cross-checked against the archive filename.

    Raises `ParseError` when the members disagree, and when none of them carries a date at all —
    an undated bundle cannot say when its corporate actions became knowable, and stamping one
    with today's date is precisely the defect this source exists to remove.
    """
    dated = {m.member_date for m in members if m.member_date is not None}
    if not dated:
        raise ParseError(
            "no member carries a date in its name, so the bundle cannot be dated from its own "
            f"contents; members: {', '.join(sorted(m.name for m in members))}",
            filename=filename,
        )
    if len(dated) > 1:
        detail = ", ".join(
            f"{m.name}={m.member_date.isoformat()}" for m in members if m.member_date is not None
        )
        raise ParseError(
            f"members disagree about the bundle's date ({detail}); a bundle whose own members "
            "cannot agree what session they are must not date a corporate action",
            filename=filename,
        )
    published = dated.pop()

    archive = _ARCHIVE_RE.match(filename)
    if archive is not None:
        from_name = _member_date(archive.group("digits"))
        if from_name is not None and from_name != published:
            raise ParseError(
                f"archive filename says {from_name.isoformat()} but its members say "
                f"{published.isoformat()}; the payload and its name are not the same session",
                filename=filename,
            )
    return published


def iter_members(bundle: PrBundle) -> Iterator[tuple[MemberKind | None, BundleMember]]:
    """Every member paired with its kind — the availability sweep a campaign reports from."""
    for member in bundle.members:
        yield member.kind, member


def member_manifest(bundle: PrBundle) -> Mapping[str, int]:
    """Member name → uncompressed size, for the per-date availability table."""
    return {m.name: m.size_bytes for m in bundle.members}
