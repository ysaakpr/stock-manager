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
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from dataplatform.ingest.macro.models import Frequency, MacroFact, MacroRelease, Unit, series_id
from dataplatform.ingest.models import ParseError
from dataplatform.logging import get_logger

__all__ = [
    "CLOSE_SNAPSHOT_SOURCE_ID",
    "INDEX_ALIASES_PATH",
    "MEASURES",
    "IndexAlias",
    "IndexAliasTable",
    "IndexSeries",
    "SwitchSide",
    "canonical_index",
    "load_index_aliases",
    "parse_index_valuation",
    "published_index_names",
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


#: An archive filename, the only form a switch may be cited in: it names the session it reports.
_SNAPSHOT_FILE: Final = re.compile(r"^ind_close_all_(\d{2})(\d{2})(\d{4})\.csv$")


class SwitchSide(BaseModel):
    """One side of a rename: the name as published, and the dated archive file that publishes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    file: str = Field(pattern=_SNAPSHOT_FILE.pattern)

    @property
    def session(self) -> date:
        """The session the cited file reports, read from its `DDMMYYYY` filename."""
        match = _SNAPSHOT_FILE.match(self.file)
        assert match is not None  # the field pattern already refused anything else
        day, month, year = (int(group) for group in match.groups())
        return date(year, month, day)


class IndexAlias(BaseModel):
    """One published name, the canonical index it belongs to, and the dated switch that shows it.

    The evidence is structural, not prose alone: `before` and `after` are the two archive files
    straddling the switch — the predecessor name in the first and not the second, the successor in
    the second and not the first — and `published` is one of the two names. A row without them does
    not load, so a mapping cannot be added on the strength of a sentence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    published: str = Field(min_length=1)
    canonical: str = Field(min_length=1)
    renamed_on: date = Field(description="first session published under the successor name")
    before: SwitchSide
    after: SwitchSide
    sessions_between: int = Field(
        default=0, ge=0, description="sessions between the two files in which neither name appears"
    )
    evidence: str = Field(
        min_length=1, description="the measured levels and why no other name fits"
    )

    @model_validator(mode="after")
    def _switch_is_dated_and_cited(self) -> IndexAlias:
        if self.before.session >= self.after.session:
            raise ValueError(
                f"{self.published!r}: before ({self.before.file}) must precede after "
                f"({self.after.file}) — a switch is cited by two files straddling it"
            )
        if self.renamed_on != self.after.session:
            raise ValueError(
                f"{self.published!r}: renamed_on {self.renamed_on} is not the session of the "
                f"first file under the new name ({self.after.file})"
            )
        if self.published not in (self.before.name, self.after.name):
            raise ValueError(
                f"{self.published!r} is neither side of the switch it cites "
                f"({self.before.name!r} -> {self.after.name!r})"
            )
        return self


class IndexSeries(BaseModel):
    """A published name that is its own index — new, or retired with no successor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    first_seen: date
    last_seen: date | None = Field(default=None, description="set only for a retired index")
    evidence: str = Field(min_length=1, description="why no rename explains it; never empty")

    @model_validator(mode="after")
    def _dated(self) -> IndexSeries:
        if self.last_seen is not None and self.last_seen < self.first_seen:
            raise ValueError(f"{self.name!r}: last_seen {self.last_seen} precedes first_seen")
        return self


class IndexAliasTable(BaseModel):
    """The index name history, keyed for lookup by published name (case- and space-insensitive)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    aliases: tuple[IndexAlias, ...]
    series: tuple[IndexSeries, ...] = ()

    def resolve(self, published: str) -> str:
        """The canonical name for `published` — itself when the table does not know the name.

        Resolving to itself is the deliberate choice: an unknown historical name becomes its own
        series, visible and separate, rather than being guessed into an existing one.
        """
        return self._by_key.get(_alias_key(published), published.strip())

    def knows(self, published: str) -> bool:
        """Whether the table has any evidence about `published` — as an alias, a canonical name, a
        name on either side of a cited switch, or a recorded series of its own.

        A name it does not know still resolves (to itself), so this is not a validity check. It is
        the question the backfill's coverage report asks: which names did the archive publish that
        the name history has never seen, the input to widening it.
        """
        return _alias_key(published) in self._known_keys

    @property
    def _by_key(self) -> dict[str, str]:
        return {_alias_key(a.published): a.canonical.strip() for a in self.aliases}

    @property
    def _known_keys(self) -> set[str]:
        names = [n for a in self.aliases for n in (a.published, a.canonical, a.before.name)]
        names += [a.after.name for a in self.aliases] + [s.name for s in self.series]
        return {_alias_key(name) for name in names}


def _alias_key(name: str) -> str:
    """Lookup key: lower-cased, ampersand- and space-insensitive, so spacing drift cannot miss."""
    return name.strip().lower().replace("&", "").replace(" ", "")


def load_index_aliases(path: Path | None = None) -> IndexAliasTable:
    """Load and validate the checked-in index name history.

    Raises `ValueError` (through pydantic) on a malformed table or a row missing its dated switch;
    on a duplicate published name mapping to two different canonicals (an ambiguous rename must be
    resolved by a human, not by whichever row happens to be read last); on a switch whose two names
    do not resolve to the row's canonical, or a canonical that is itself remapped (a chain must be
    written out to its end); and on a name recorded both as its own series and as an alias.
    """
    source = INDEX_ALIASES_PATH if path is None else path
    raw = yaml.safe_load(source.read_text())
    table = IndexAliasTable(
        version=raw["version"],
        aliases=tuple(raw.get("aliases") or ()),
        series=tuple(raw.get("series") or ()),
    )
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
    for alias in table.aliases:
        canonical = _alias_key(alias.canonical)
        if _alias_key(table.resolve(alias.canonical)) != canonical:
            raise ValueError(
                f"{source}: {alias.published!r} maps to {alias.canonical!r}, which is itself "
                f"remapped to {table.resolve(alias.canonical)!r}; write the chain out to its end"
            )
        for name in (alias.before.name, alias.after.name):
            if _alias_key(table.resolve(name)) != canonical:
                raise ValueError(
                    f"{source}: {alias.published!r} cites {name!r}, which resolves to "
                    f"{table.resolve(name)!r}, not {alias.canonical!r}; both sides of a switch "
                    "must land on one series"
                )
    aliased = {_alias_key(n) for a in table.aliases for n in (a.published, a.canonical)}
    for series in table.series:
        if _alias_key(series.name) in aliased:
            raise ValueError(
                f"{source}: {series.name!r} is recorded as its own series and as an alias; it "
                "is one or the other"
            )
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
    source: str = CLOSE_SNAPSHOT_SOURCE_ID,
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

    `source` is the register id the bytes were fetched under: the niftyindices row by default, or
    the NSE archive host's `nse_index_close_snapshot` (byte-identical payload, its own host row).

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
    named = _filename_session(filename)
    month_first = False
    indices = 0
    for line_no, record in enumerate(reader, start=2):
        published = (record.get(fields[_COL_NAME]) or "").strip()
        if not published:
            continue  # a trailing blank line is not a row
        row_date, swapped = _session_date(
            (record.get(fields[_COL_DATE]) or "").strip(),
            named=named,
            line=line_no,
            filename=filename,
        )
        month_first = month_first or swapped
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
                    source=source,
                    l0_key=l0_key,
                )
            )

    facts, withheld = _withhold_ambiguous(facts)
    if withheld:
        _LOG.warning(
            "macro.index_valuation_withheld",
            source=source,
            filename=filename,
            subjects=", ".join(withheld),
            reason="one index name published on several rows with different values",
        )
    if session is None or not facts:
        raise ParseError("no index rows in close-all snapshot", filename=filename)

    if month_first:
        _LOG.warning(
            "macro.index_valuation_month_first",
            source=source,
            filename=filename,
            session=session.isoformat(),
            reason="Index Date written MM-DD-YYYY; read month-first because only that reading "
            "names the session in the filename",
        )
    _LOG.info(
        "macro.index_valuation_parsed",
        source=source,
        filename=filename,
        session=session.isoformat(),
        indices=indices,
        facts=len(facts),
        state="VALIDATED",
    )
    return MacroRelease(
        release_date=session,
        source=source,
        facts=tuple(facts),
        l0_key=l0_key,
        withheld=withheld,
    )


def _withhold_ambiguous(facts: list[MacroFact]) -> tuple[list[MacroFact], tuple[str, ...]]:
    """Drop every fact of a subject the file publishes twice with different values.

    The archive does this: the 2013-02-08 file lists `CNX Alpha Index` on two rows, the second
    carrying what the day before was `CNX High Beta`'s level. Which row is the real index cannot be
    told from the file, so neither is kept — choosing one would put a different index's history
    under a name with no evidence it belongs there. A repeated row with identical values is kept
    once. Returns the kept facts (file order) and the withheld `series_id` subjects, sorted.
    """
    by_id: dict[str, list[MacroFact]] = {}
    for fact in facts:
        by_id.setdefault(fact.series_id, []).append(fact)
    ambiguous = {
        sid.rsplit(".", 1)[0]
        for sid, group in by_id.items()
        if len({fact.value for fact in group}) > 1
    }
    kept: list[MacroFact] = []
    seen: set[str] = set()
    for fact in facts:
        if fact.series_id.rsplit(".", 1)[0] in ambiguous or fact.series_id in seen:
            continue
        seen.add(fact.series_id)
        kept.append(fact)
    return kept, tuple(sorted(ambiguous))


def published_index_names(payload: bytes, *, filename: str) -> tuple[str, ...]:
    """Every index name one close-all file publishes, exactly as published, in file order.

    The names `parse_index_valuation` resolves away: a `series_id` is upper-cased and
    separator-safe, so the published spelling cannot be read back from the facts. A coverage report
    that lists names the alias table has never seen needs the spelling the archive used.
    Raises `ParseError` for the same bodies `parse_index_valuation` refuses.
    """
    reader = csv.DictReader(io.StringIO(_decode(payload, filename=filename)))
    if reader.fieldnames is None:
        raise ParseError("no header row", filename=filename)
    fields = {name.strip(): name for name in reader.fieldnames}
    if _COL_NAME not in fields:
        raise ParseError(f"header is missing {_COL_NAME!r}", filename=filename)
    names = ((record.get(fields[_COL_NAME]) or "").strip() for record in reader)
    return tuple(name for name in names if name)


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


def _filename_session(filename: str) -> date | None:
    """The session an archive filename names (`ind_close_all_<DDMMYYYY>.csv`), else `None`.

    The filename is the one date the archive writes in a fixed order: it is the URL the session
    was requested by, built from the trading calendar, never from the file's contents.
    """
    match = _SNAPSHOT_FILE.match(Path(filename).name)
    if match is None:
        return None
    day, month, year = (int(group) for group in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _session_date(raw: str, *, named: date | None, line: int, filename: str) -> tuple[date, bool]:
    """`DD-MM-YYYY` as published, or `DD/MM/YYYY`; month-first only when the filename settles it.

    Returns the date and whether it was read month-first.

    Measured over the M11.2 backfill: files from (at least) 2014-06-26 to 2015-04 write the date
    with slashes; every other session uses hyphens. The field order is day-month-year in both — a
    26/06/2014 settles it, and the backfill also checks every file's date against the session it was
    requested for. Any other shape is a format change, not a date to guess at.

    The one exception (M14.2): the 2023-04-06, -10 and -11 files write `04-06-2023`, `04-10-2023`
    and `04-11-2023` — month-first, between day-first neighbours. The date is read month-first only
    when all three hold: the filename names a session (`named`), the day-first reading is not that
    session, and the month-first reading is exactly it. Then the two readings differ and only one of
    them agrees with the date the file was requested under, so nothing is guessed. In every other
    case the day-first reading stands, and a file it dates to another session is still refused by
    the caller — a field order is never swapped to make a mismatch go away.
    """
    separator = "/" if "/" in raw else "-"
    parts = raw.split(separator)
    if len(parts) != 3:
        raise ParseError(f"{_COL_DATE} {raw!r} is not DD-MM-YYYY", filename=filename, line=line)
    try:
        first, second, year = (int(part) for part in parts)
    except ValueError as exc:
        raise ParseError(
            f"{_COL_DATE} {raw!r} is not a real date: {exc}", filename=filename, line=line
        ) from exc
    day_first = _real_date(year, second, first)
    if day_first is not None and (named is None or day_first == named):
        return day_first, False
    if named is not None and _real_date(year, first, second) == named:
        return named, True
    if day_first is None:
        raise ParseError(f"{_COL_DATE} {raw!r} is not a real date", filename=filename, line=line)
    return day_first, False


def _real_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


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
