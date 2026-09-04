"""NSE's daily close-all snapshot → macro valuation series (M11.1).

`ind_close_all_<DDMMYYYY>.csv` publishes, for every NIFTY index on one session, its closing level
*and* its P/E, P/B and dividend yield. That makes it the platform's daily market-state spine: a
measured 13+ years of aggregate market and sector valuation, from one already-VERIFIED source, on a
dated URL that needs no session cookie.

`dataplatform.ingest.indices.parse_close_snapshot` already reads this file, but only the four
columns §4.1's computed-TRI fallback needs — the P/E and P/B columns are on the floor. This module
reads the valuation columns and lands them as `MacroFact`s, leaving the TRI path untouched.

**The index-identity problem, which is the reason this module is not four lines.** The file names
each index as it was published *that day*, and NSE/IISL renamed 48 of 53 indices in a single event
between 2015-11-06 and 2015-11-10 — `S&P CNX Nifty` → `CNX Nifty` → `Nifty 50`, `CNX Nifty Junior` →
`Nifty Next 50`, and so on. A consumer keyed on today's name silently loses every observation before
that date, which is the survivorship trap D2's `symbol_history` closes for equities, reappearing in
the index dimension. `index_aliases.yaml` is that history, and `canonical_index` is the only way a
name becomes a `series_id` here.

The table is deliberately incomplete: it carries only the renames that name-stem *and* level
evidence both support. An unmapped published name resolves to **itself** — forming a visibly
separate series rather than being silently merged into the wrong index — because a wrong merge
corrupts a history in a way no later test can see, while a split one is obvious the moment anyone
plots it.

Point-in-time: this file is published after the session it reports, so `period_end` is the session
and `release_date` is the same session — the figure is knowable that evening. A snapshot is
immutable once published (the register's `pit_notes`), so these series are never revised, and every
fact is written at `revision_seq` 0.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

from dataplatform.ingest.macro.models import Frequency, MacroFact, MacroRelease, Unit, series_id
from dataplatform.ingest.models import ParseError
from dataplatform.logging import get_logger

__all__ = [
    "CLOSE_SNAPSHOT_SOURCE_ID",
    "INDEX_ALIASES_PATH",
    "MEASURES",
    "IndexAlias",
    "IndexAliasTable",
    "canonical_index",
    "load_index_aliases",
    "parse_index_valuation",
]

_LOG = get_logger(__name__)

#: The register id whose bytes this parser reads (already VERIFIED — §4.1 row 8's TRI input).
CLOSE_SNAPSHOT_SOURCE_ID: Final = "nifty_index_close_snapshot"

#: The checked-in index name history. Ships with the package, like `rss_feeds.yaml`.
INDEX_ALIASES_PATH: Final = Path(__file__).with_name("index_aliases.yaml")

_COL_NAME: Final = "Index Name"
_COL_DATE: Final = "Index Date"
_COL_CLOSE: Final = "Closing Index Value"

#: The four valuation columns, each mapped to its `series_id` measure and unit. `Div Yield` is
#: already in percentage points; P/E and P/B are pure multiples, which is why they are `RATIO` and
#: not `PCT` — a consumer that divided a P/E by 100 would be silently wrong for a decade.
MEASURES: Final[tuple[tuple[str, str, Unit], ...]] = (
    (_COL_CLOSE, "CLOSE", Unit.INDEX),
    ("P/E", "PE", Unit.RATIO),
    ("P/B", "PB", Unit.RATIO),
    ("Div Yield", "DIV_YIELD", Unit.PCT),
)


class IndexAlias(BaseModel):
    """One published index name and the canonical name it belongs to, with why we believe it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    published: str = Field(min_length=1)
    canonical: str = Field(min_length=1)
    evidence: str = Field(min_length=1, description="what supports this mapping; never empty")


class IndexAliasTable(BaseModel):
    """The index name history, keyed for lookup by published name (case- and space-insensitive)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    aliases: tuple[IndexAlias, ...]

    def resolve(self, published: str) -> str:
        """The canonical name for `published` — itself when the table does not know the name.

        Resolving to itself is the deliberate choice: an unknown historical name becomes its own
        series, visible and separate, rather than being guessed into an existing one.
        """
        return self._by_key.get(_alias_key(published), published.strip())

    @property
    def _by_key(self) -> dict[str, str]:
        return {_alias_key(a.published): a.canonical.strip() for a in self.aliases}


def _alias_key(name: str) -> str:
    """Lookup key: lower-cased, ampersand- and space-insensitive, so spacing drift cannot miss."""
    return name.strip().lower().replace("&", "").replace(" ", "")


def load_index_aliases(path: Path | None = None) -> IndexAliasTable:
    """Load and validate the checked-in index name history.

    Raises `ValueError` (through pydantic) on a malformed table, and on a duplicate published name
    mapping to two different canonicals — an ambiguous rename must be resolved by a human, not by
    whichever row happens to be read last.
    """
    source = INDEX_ALIASES_PATH if path is None else path
    raw = yaml.safe_load(source.read_text())
    table = IndexAliasTable(version=raw["version"], aliases=tuple(raw.get("aliases") or ()))
    seen: dict[str, str] = {}
    for alias in table.aliases:
        key = _alias_key(alias.published)
        prior = seen.get(key)
        if prior is not None and prior != alias.canonical.strip():
            raise ValueError(
                f"{source}: {alias.published!r} maps to both {prior!r} and {alias.canonical!r}; "
                "an ambiguous rename is a research question, not a last-writer-wins"
            )
        seen[key] = alias.canonical.strip()
    return table


def canonical_index(published: str, *, table: IndexAliasTable | None = None) -> str:
    """The canonical index name for a name as published on some historical session."""
    return (table or load_index_aliases()).resolve(published)


def parse_index_valuation(
    payload: bytes,
    *,
    filename: str,
    table: IndexAliasTable | None = None,
    l0_key: str | None = None,
) -> MacroRelease:
    """Parse one `ind_close_all_<DDMMYYYY>.csv` into a release of index valuation facts.

    What it does: read every index row, resolve its published name to canonical through the alias
    table, and emit up to four `MacroFact`s per index — closing level, P/E, P/B, dividend yield —
    all dated to the session the file reports and released the same day.
    What it assumes: every row of one file shares one `Index Date`; the file is one session's
    snapshot, and a file mixing sessions is malformed rather than interesting.
    What it never does: turn a blank or `-` value into `0` (an index that states no P/E is not an
    index on zero earnings — the fact is simply absent), read a name through a rename rule instead
    of the evidence table, or accept a body that is markup wearing a 200.

    Raises `ParseError`, naming the file and line, for an empty or HTML body, a header missing a
    required column, a malformed date or number, or a file whose rows disagree on the session.
    """
    aliases = load_index_aliases() if table is None else table
    text = _decode(payload, filename=filename)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ParseError("no header row", filename=filename)
    fields = {name.strip(): name for name in reader.fieldnames}
    for required in (_COL_NAME, _COL_DATE, _COL_CLOSE):
        if required not in fields:
            raise ParseError(
                f"header is missing {required!r}; present: {', '.join(sorted(fields))}",
                filename=filename,
            )

    facts: list[MacroFact] = []
    session: date | None = None
    indices = 0
    for line_no, record in enumerate(reader, start=2):
        published = (record.get(fields[_COL_NAME]) or "").strip()
        if not published:
            continue  # a trailing blank line is not a row
        row_date = _session_date(
            (record.get(fields[_COL_DATE]) or "").strip(), line=line_no, filename=filename
        )
        if session is None:
            session = row_date
        elif row_date != session:
            raise ParseError(
                f"row is dated {row_date} but the file's first row is {session}; one close-all "
                "file is one session, and a mixed file would scatter facts across partitions",
                filename=filename,
                line=line_no,
            )
        indices += 1
        subject = aliases.resolve(published)
        for column, measure, unit in MEASURES:
            if column not in fields:
                continue  # the four columns are stable across every era measured, but do not assume
            value = _optional_decimal(
                (record.get(fields[column]) or "").strip(),
                column=column,
                line=line_no,
                filename=filename,
            )
            if value is None:
                continue  # not stated is not zero
            facts.append(
                MacroFact(
                    series_id=series_id("IN", "NSE", subject, measure),
                    period_end=session,
                    release_date=session,
                    frequency=Frequency.DAILY,
                    unit=unit,
                    value=value,
                    source=CLOSE_SNAPSHOT_SOURCE_ID,
                    l0_key=l0_key,
                )
            )

    if session is None or not facts:
        raise ParseError("no index rows in close-all snapshot", filename=filename)

    _LOG.info(
        "macro.index_valuation_parsed",
        source=CLOSE_SNAPSHOT_SOURCE_ID,
        filename=filename,
        session=session.isoformat(),
        indices=indices,
        facts=len(facts),
        state="VALIDATED",
    )
    return MacroRelease(
        release_date=session,
        source=CLOSE_SNAPSHOT_SOURCE_ID,
        facts=tuple(facts),
        l0_key=l0_key,
    )


def _decode(payload: bytes, *, filename: str) -> str:
    """UTF-8 the body, refusing an empty one and the HTML soft-404 that wears a 200."""
    if not payload.strip():
        raise ParseError("empty response body", filename=filename)
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError(f"body is not UTF-8: {exc}", filename=filename) from exc
    if text.lstrip()[:1] == "<":
        raise ParseError(
            "body is markup, not CSV — a bad archive path answers with an HTML error page, and it "
            "must not become a valuation series",
            filename=filename,
        )
    return text


def _session_date(raw: str, *, line: int, filename: str) -> date:
    """`DD-MM-YYYY` as published. Any other shape is a format change, not a date to guess at."""
    parts = raw.split("-")
    if len(parts) != 3:
        raise ParseError(f"{_COL_DATE} {raw!r} is not DD-MM-YYYY", filename=filename, line=line)
    try:
        day, month, year = (int(part) for part in parts)
        return date(year, month, day)
    except ValueError as exc:
        raise ParseError(
            f"{_COL_DATE} {raw!r} is not a real date: {exc}", filename=filename, line=line
        ) from exc


def _optional_decimal(raw: str, *, column: str, line: int, filename: str) -> Decimal | None:
    """An exact `Decimal`, or `None` for a value the file does not state.

    The archive writes an unstated value as blank, `-`, `0.00` for a yield-less index, or `NA`. Only
    the first two are read as "absent": a literal `0.00` is a published zero and is kept, because
    deciding it means "unknown" would be this parser inventing a judgment the file did not make.
    The old era also writes leading-dot decimals (`.71`), which `Decimal` reads exactly.
    """
    if raw in {"", "-", "NA", "N.A.", "--"}:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise ParseError(
            f"{column} {raw!r} is not a decimal", filename=filename, line=line
        ) from exc
