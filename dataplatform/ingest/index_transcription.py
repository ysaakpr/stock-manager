"""Curated transcriptions of index-change releases that have no text layer (DQ-5, pre-2021 depth).

`index_changes.parse_press_release_pdf` reads a release through its text layer. One release on the
path back from today has none: `ind_prs23082021.pdf`, the September 2021 semi-annual review, draws
every word as an image mask, and the platform has no OCR. PR #37's history therefore stopped at
2021-10-22. That release was read by an agent from its rendered pages and typed into
`data/index_release_transcriptions.yaml`, each row as printed, with the L0 key and sha256 of the
object it was read from, the pages, the date and who transcribed it. This module loads that file,
validates it, and turns an entry into the same `PressReleaseParse` the PDF parser would have
produced — so the history builder treats a transcribed release exactly like a parsed one.

**Read versus derived.** The September 2021 PDF does not print all of its NIFTY 500 section (rows
20+ of the exclusions and the whole inclusion table are absent). Those rows are *derived* from the
release's own NIFTY 100, Midcap 150 and Smallcap 250 tables (NIFTY 500 is their disjoint union) and
kept apart from the transcribed rows; `TranscribedRow.derived` marks them, and the history report
lists every derived event. A test re-derives them from the component sections.

What it never does: modify L0, run OCR, or apply a transcription to bytes it was not read from —
`transcription_parse` refuses an L0 object whose sha256 differs from the one the entry pins.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

import yaml

from dataplatform.ingest.index_changes import (
    TRACKED_INDICES,
    ChangeAction,
    IndexChangeEvent,
    PressReleaseParse,
)
from dataplatform.ingest.models import ParseError
from dataplatform.store.l0 import L0Ref

__all__ = [
    "TRANSCRIPTIONS_PATH",
    "ReleaseTranscription",
    "TranscribedRow",
    "TranscribedSection",
    "TranscriptionError",
    "TranscriptionFlag",
    "load_release_transcriptions",
    "transcription_parse",
]

#: The curated file, beside this package's other reviewed reference data.
TRANSCRIPTIONS_PATH: Final[Path] = Path(__file__).with_name("data") / (
    "index_release_transcriptions.yaml"
)

_SYMBOL: Final = re.compile(r"^[A-Z0-9][A-Z0-9&\-_.]*$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


class TranscriptionError(ValueError):
    """The transcriptions file is malformed — a row out of sequence, a bad symbol, no provenance."""


@dataclass(frozen=True, slots=True)
class TranscribedRow:
    """One table row: Sr. No. (None for a derived row), company name and symbol as printed."""

    sr: int | None
    company: str
    symbol: str
    derived: bool = False


@dataclass(frozen=True, slots=True)
class TranscribedSection:
    """One index's section: its exclusions and inclusions, read and (when flagged) derived."""

    index_slug: str
    heading: str
    pages: tuple[int, ...]
    exclude: tuple[TranscribedRow, ...]
    include: tuple[TranscribedRow, ...]
    derivation: str | None = None

    @property
    def balanced(self) -> bool:
        """A fixed-size index replaces like for like: as many in as out."""
        return len(self.exclude) == len(self.include)


@dataclass(frozen=True, slots=True)
class TranscriptionFlag:
    """Something in the source that did not read cleanly, kept with the transcription."""

    index_slug: str
    kind: str
    pages: tuple[int, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class ReleaseTranscription:
    """A release read by eye: its provenance, effective date, sections and flags."""

    release: str
    l0_key: str
    sha256: str
    page_count: int
    announced: date
    title: str
    transcribed_by: str
    transcribed_on: date
    method: str
    effective: date
    effective_page: int
    effective_quote: str
    unchanged: tuple[str, ...]
    sections: tuple[TranscribedSection, ...]
    crosscheck_sections: tuple[TranscribedSection, ...]
    flags: tuple[TranscriptionFlag, ...]

    def section(self, index_slug: str) -> TranscribedSection | None:
        """The tracked or cross-check section for `index_slug`, if the release has one."""
        for s in (*self.sections, *self.crosscheck_sections):
            if s.index_slug == index_slug:
                return s
        return None


def load_release_transcriptions(
    path: Path = TRANSCRIPTIONS_PATH,
) -> Mapping[str, ReleaseTranscription]:
    """Load and validate the curated file, keyed by release filename.

    Raises `TranscriptionError` for anything that would make a transcription untrustworthy: missing
    provenance, a Sr. No. sequence that skips or repeats, a symbol that is not one, a derived row
    without a stated derivation, an untracked index in `sections`, or a page outside the PDF.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise TranscriptionError(f"{path.name}: expected `version: 1`")
    out: dict[str, ReleaseTranscription] = {}
    for entry in raw.get("transcriptions") or []:
        t = _transcription(entry)
        if t.release in out:
            raise TranscriptionError(f"{t.release}: transcribed twice")
        out[t.release] = t
    return out


def transcription_parse(transcription: ReleaseTranscription, ref: L0Ref) -> PressReleaseParse:
    """The `PressReleaseParse` a transcription stands for, bound to the L0 object it was read from.

    Raises `ParseError` when `ref` is not that object (another filename, or bytes whose sha256 is
    not the pinned one): a transcription is evidence about one payload only.
    """
    if ref.filename != transcription.release or ref.sha256 != transcription.sha256:
        raise ParseError(
            f"transcription pins sha256 {transcription.sha256[:12]}…, L0 holds "
            f"{ref.sha256[:12]}… — the release was re-issued; re-transcribe it",
            filename=ref.filename,
        )
    events: list[IndexChangeEvent] = []
    for section in transcription.sections:
        for action, rows in (
            (ChangeAction.EXCLUDE, section.exclude),
            (ChangeAction.INCLUDE, section.include),
        ):
            for row in rows:
                events.append(
                    IndexChangeEvent(
                        index_slug=section.index_slug,
                        action=action,
                        company_name=row.company,
                        symbol=row.symbol,
                        effective=transcription.effective,
                        announced=transcription.announced,
                        release=transcription.release,
                        l0_key=transcription.l0_key,
                    )
                )
    return PressReleaseParse(
        release=transcription.release,
        announced=transcription.announced,
        events=tuple(events),
        unparsed=(),
        tracked_sections=tuple(
            dict.fromkeys(
                [*(s.index_slug for s in transcription.sections), *transcription.unchanged]
            )
        ),
    )


# ── validation ─────────────────────────────────────────────────────────────────────────────────


def _transcription(entry: Any) -> ReleaseTranscription:
    if not isinstance(entry, dict):
        raise TranscriptionError("a transcription must be a mapping")
    release = _str(entry, "release")
    where = release
    sha = _str(entry, "sha256", where)
    if not _SHA256.fullmatch(sha):
        raise TranscriptionError(f"{where}: sha256 must be 64 lowercase hex")
    l0_key = _str(entry, "l0_key", where)
    if not l0_key.endswith("/" + release):
        raise TranscriptionError(f"{where}: l0_key {l0_key!r} does not name the release")
    page_count = int(entry.get("page_count") or 0)
    if page_count <= 0:
        raise TranscriptionError(f"{where}: page_count is required")
    effective = entry.get("effective") or {}
    eff_page = int(effective.get("page") or 0)
    _check_pages((eff_page,), page_count, where)
    unchanged: list[str] = []
    for u in entry.get("unchanged") or []:
        slug = _tracked(_str(u, "index", where), where)
        _check_pages((int(u.get("page") or 0),), page_count, where)
        _str(u, "quote", where)
        unchanged.append(slug)
    sections = tuple(_section(s, page_count, where, tracked=True) for s in entry["sections"])
    crosscheck = tuple(
        _section(s, page_count, where, tracked=False)
        for s in entry.get("crosscheck_sections") or []
    )
    named = [s.index_slug for s in sections] + unchanged
    if len(set(named)) != len(named):
        raise TranscriptionError(f"{where}: an index appears in more than one section")
    flags = tuple(
        TranscriptionFlag(
            index_slug=_str(f, "index", where),
            kind=_str(f, "kind", where),
            pages=_check_pages(tuple(f.get("pages") or ()), page_count, where),
            detail=" ".join(_str(f, "detail", where).split()),
        )
        for f in entry.get("flags") or []
    )
    for s in sections:
        derived = any(r.derived for r in (*s.exclude, *s.include))
        if derived and not any(f.index_slug == s.index_slug for f in flags):
            raise TranscriptionError(f"{where}: {s.index_slug} has derived rows but no flag")
    return ReleaseTranscription(
        release=release,
        l0_key=l0_key,
        sha256=sha,
        page_count=page_count,
        announced=_date(entry, "announced", where),
        title=_str(entry, "title", where),
        transcribed_by=_str(entry, "transcribed_by", where),
        transcribed_on=_date(entry, "transcribed_on", where),
        method=" ".join(_str(entry, "method", where).split()),
        effective=_date(effective, "date", where),
        effective_page=eff_page,
        effective_quote=" ".join(_str(effective, "quote", where).split()),
        unchanged=tuple(unchanged),
        sections=sections,
        crosscheck_sections=crosscheck,
        flags=flags,
    )


def _section(raw: Any, page_count: int, where: str, *, tracked: bool) -> TranscribedSection:
    slug = _str(raw, "index", where)
    if tracked:
        _tracked(slug, where)
    elif slug in TRACKED_INDICES:
        raise TranscriptionError(f"{where}: {slug} is tracked; it belongs in `sections`")
    where = f"{where} {slug}"
    derivation = raw.get("derivation")
    exclude = _rows(raw.get("exclude") or [], where) + _derived(raw, "exclude_derived", where)
    include = _rows(raw.get("include") or [], where) + _derived(raw, "include_derived", where)
    if any(r.derived for r in (*exclude, *include)) and not derivation:
        raise TranscriptionError(f"{where}: derived rows need a `derivation`")
    symbols = [r.symbol for r in (*exclude, *include)]
    if len(set(symbols)) != len(symbols):
        raise TranscriptionError(f"{where}: a symbol is both included and excluded, or repeated")
    return TranscribedSection(
        index_slug=slug,
        heading=_str(raw, "heading", where),
        pages=_check_pages(tuple(raw.get("pages") or ()), page_count, where),
        exclude=exclude,
        include=include,
        derivation=None if derivation is None else " ".join(str(derivation).split()),
    )


def _rows(raw: list[Any], where: str) -> tuple[TranscribedRow, ...]:
    rows: list[TranscribedRow] = []
    for expected, item in enumerate(raw, start=1):
        if not isinstance(item, list) or len(item) != 3:
            raise TranscriptionError(f"{where}: row {item!r} is not [sr, company, symbol]")
        sr, company, symbol = item
        if sr != expected:
            raise TranscriptionError(f"{where}: Sr. No. {sr} where {expected} was expected")
        rows.append(TranscribedRow(int(sr), _company(company, where), _symbol(symbol, where)))
    return tuple(rows)


def _derived(raw: Mapping[str, Any], key: str, where: str) -> tuple[TranscribedRow, ...]:
    rows: list[TranscribedRow] = []
    for item in raw.get(key) or []:
        if not isinstance(item, list) or len(item) != 2:
            raise TranscriptionError(f"{where}: derived row {item!r} is not [company, symbol]")
        company, symbol = item
        rows.append(
            TranscribedRow(None, _company(company, where), _symbol(symbol, where), derived=True)
        )
    return tuple(rows)


def _company(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranscriptionError(f"{where}: empty company name")
    return value.strip()


def _symbol(value: Any, where: str) -> str:
    symbol = str(value)
    if not _SYMBOL.fullmatch(symbol):
        raise TranscriptionError(f"{where}: {symbol!r} is not an NSE symbol")
    return symbol


def _tracked(slug: str, where: str) -> str:
    if slug not in TRACKED_INDICES:
        raise TranscriptionError(f"{where}: {slug!r} is not a tracked index")
    return slug


def _check_pages(pages: tuple[Any, ...], page_count: int, where: str) -> tuple[int, ...]:
    out = tuple(int(p) for p in pages)
    if not out or any(p < 1 or p > page_count for p in out):
        raise TranscriptionError(f"{where}: pages {list(pages)} outside 1..{page_count}")
    return out


def _str(raw: Any, key: str, where: str = "") -> str:
    value = raw.get(key) if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise TranscriptionError(f"{where or 'transcription'}: `{key}` is required")
    return value


def _date(raw: Any, key: str, where: str) -> date:
    value = raw.get(key) if isinstance(raw, dict) else None
    if isinstance(value, date):
        return value
    raise TranscriptionError(f"{where}: `{key}` must be a date")
