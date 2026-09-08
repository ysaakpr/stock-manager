"""`Bc<date>.csv` — corporate actions whose knowable date is the file's own publication date (W2).

This is the member the whole wave exists for. Every other corporate-action surface this platform
holds answers "what is true now": `dataplatform.ingest.bse.corp_actions` stamps
`knowable_date=clock.now().date()`, which is why all 47,887 rows in `corporate_actions` share a
single knowable date and invariant #7 ("no future data in a decision") is satisfied *vacuously* —
a PIT-honest backtest sees zero corporate actions on every decision date in history, and one that
joins on `ex_date` instead is silently using look-ahead.

`Bc020113.csv` inside `PR020113.zip` is NSE's corporate-action book **as broadcast on 2013-01-02**.
The date is a property of the file, so `knowable_date` is a measurement rather than an assumption,
and a decision taken on 2013-01-01 provably cannot see it.

**`knowable_date` is `PrBundle.publication_date` and nothing else.** This module accepts no
`Clock`, imports no clock, and calls no wall-clock function; `tests/unit/test_pr_bundle_bc.py`
asserts all three and fails if any is reintroduced.

**These rows are symbol-keyed and carry no ISIN.** The published columns are
`SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE` —
stable across every era measured, and with no identity in them but a trading symbol. ISIN is the
only join key (invariant #2), so `BcRow` deliberately has **no `isin` field**: there is nothing to
put in it that would not be invented. Resolving these rows needs a symbol→ISIN mapping that is
correct *as at the broadcast date*, which is D2 lineage work and is not done here — see the
reconciliation plan in the W2 PR body.

Purpose strings are kept byte-for-byte as published. Classification into the M2.1 taxonomy is
deliberately **not** done here: the promotion of these rows against the existing
`corporate_actions` table is a separate reviewed task, and a parser that quietly normalised on the
way in would make that reconciliation impossible to audit.

Offline by construction: takes bytes, or a `PrBundle` opened from L0. It never fetches.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from datetime import date
from io import StringIO
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse.pr_bundle.bundle import PR_BUNDLE_SOURCE_ID, MemberKind, PrBundle
from dataplatform.logging import get_logger

__all__ = [
    "BC_COLUMNS",
    "BcRow",
    "parse_bc",
    "parse_bc_bundle",
]

_LOG = get_logger(__name__)

#: The published header, in order. Identical on every date measured from 2010-01-04 to 2026-09-04;
#: only the *values* changed shape (see `_parse_date`). A header that is not this is a format
#: change to stop on, not to guess through.
BC_COLUMNS: Final[tuple[str, ...]] = (
    "SERIES",
    "SYMBOL",
    "SECURITY",
    "RECORD_DT",
    "BC_STRT_DT",
    "BC_END_DT",
    "EX_DT",
    "ND_STRT_DT",
    "ND_END_DT",
    "PURPOSE",
)

#: Values that mean "no date". The early eras pad an absent date with a single space, the 2025+
#: era leaves the field empty; both appear mid-row, so neither can be treated as a short row.
_EMPTY_MARKERS: Final[frozenset[str]] = frozenset({"", "-", "--", "n/a", "na", "null", "none"})

#: `DD/MM/YYYY`, every era up to and including 2025-10-01.
_SLASH_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")

#: `YYYY-MM-DD`, the shape from the ~2025-10 cutover onward. Both are accepted on every date
#: rather than dispatched on an era: the boundary is bracketed to a month, and sniffing the value
#: in front of us is both narrower and impossible to get wrong by a day.
_DASH_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


class BcRow(BaseModel):
    """One corporate action exactly as NSE broadcast it on `knowable_date`.

    What it does: carry the published row, its dates typed, and the broadcast date that makes it
    knowable. What it assumes: nothing about the action's *type* — `purpose` is raw text and
    classification belongs to the reconciliation task. What it never does: carry an ISIN. The
    source has none, ISIN is the only join key (invariant #2), and a field here would only ever
    hold a guess.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    knowable_date: date = Field(
        description=(
            "the bundle's own publication date — the first date this action was knowable to the "
            "market, and the reason this source is worth fetching (invariant #7)"
        )
    )
    series: str = Field(description="the trading series the action is filed under, e.g. EQ, BE, N1")
    symbol: str = Field(min_length=1, description="NSE trading symbol; NOT a join key")
    security_name: str = Field(description="the issuer name as published, truncated by NSE")
    purpose: str = Field(
        min_length=1, description="the purpose string byte-for-byte as published; never normalized"
    )
    record_date: date | None = Field(default=None, description="RECORD_DT")
    book_closure_start: date | None = Field(default=None, description="BC_STRT_DT")
    book_closure_end: date | None = Field(default=None, description="BC_END_DT")
    ex_date: date | None = Field(
        default=None,
        description="EX_DT; absent on some debt actions (e.g. a GOI loan REDEMPTION)",
    )
    no_delivery_start: date | None = Field(default=None, description="ND_STRT_DT")
    no_delivery_end: date | None = Field(default=None, description="ND_END_DT")
    source: str = Field(default=PR_BUNDLE_SOURCE_ID, description="Source Register id")
    l0_key: str | None = Field(
        default=None, description="the L0 payload this row derives from (invariant #1)"
    )

    @property
    def announced_before_ex_date(self) -> bool | None:
        """Whether the broadcast preceded the ex-date, the shape a usable action has.

        `None` when the row carries no ex-date. `False` is not a defect: NSE re-broadcasts the
        book on later dates too, so an action first seen after its own ex-date simply means this
        bundle is not the earliest one carrying it — which the reconciliation task resolves by
        keeping the *minimum* knowable date across bundles.
        """
        if self.ex_date is None:
            return None
        return self.knowable_date <= self.ex_date


def parse_bc(
    payload: bytes,
    *,
    filename: str,
    knowable_date: date,
    l0_key: str | None = None,
) -> tuple[BcRow, ...]:
    """Parse one `Bc` member into rows stamped with the bundle's publication date.

    `knowable_date` is required and has exactly one legal source: `PrBundle.publication_date`,
    which is derived from the payload. It is a parameter rather than a clock read so that this
    function stays pure and the caller cannot substitute "now" without writing that word at the
    call site — `parse_bc_bundle` is the entry point that wires it correctly.

    Raises `ParseError`, naming the file and the physical line, for a payload that is not this
    format: an HTML soft-404, a header that is not `BC_COLUMNS`, a row with the wrong column
    count, a row with no symbol or purpose, or a date field that is neither shape. A blank line is
    skipped — the source pads with them — and nothing else is skipped silently.
    """
    text = _decode(payload, filename=filename)
    reader = csv.reader(StringIO(text))
    rows = list(reader)
    if not rows:
        raise ParseError("file is empty", filename=filename)

    header = tuple(cell.strip().upper() for cell in rows[0])
    if header != BC_COLUMNS:
        raise ParseError(
            f"unexpected header {header!r}; expected {BC_COLUMNS!r}",
            filename=filename,
            line=1,
        )

    parsed: list[BcRow] = []
    for offset, raw in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in raw):
            continue
        parsed.append(
            _row(raw, filename=filename, line=offset, knowable_date=knowable_date, l0_key=l0_key)
        )

    _LOG.info(
        "pr_bundle_bc.parsed",
        source=PR_BUNDLE_SOURCE_ID,
        filename=filename,
        knowable_date=knowable_date.isoformat(),
        rows=len(parsed),
        state="VALIDATED",
    )
    return tuple(parsed)


def parse_bc_bundle(bundle: PrBundle, *, l0_key: str | None = None) -> tuple[BcRow, ...]:
    """Parse the `Bc` member of an opened bundle, dated by that bundle.

    The only entry point a pipeline should use: it takes the knowable date from the bundle the
    bytes came out of, so the row's PIT stamp and the payload can never be from different days.
    """
    return parse_bc(
        bundle.read(MemberKind.BC),
        filename=_member_name(bundle),
        knowable_date=bundle.publication_date,
        l0_key=l0_key,
    )


# ── internals ────────────────────────────────────────────────────────────────────────────────


def _member_name(bundle: PrBundle) -> str:
    """The `Bc` member's own filename, so an error names the CSV and not the zip."""
    member = bundle.member(MemberKind.BC)
    return bundle.filename if member is None else member.name


def _decode(payload: bytes, *, filename: str) -> str:
    """Latin-1 the body, refusing an empty one and markup wearing a 200.

    Latin-1 rather than UTF-8: issuer names in the older eras carry stray high bytes that are not
    valid UTF-8, and losing a whole session's actions to one accented company name would be the
    wrong trade. Latin-1 cannot fail, so the soft-404 check below is what catches a bad payload.
    """
    if not payload.strip():
        raise ParseError("empty response body", filename=filename)
    text = payload.decode("latin-1")
    if text.lstrip()[:1] == "<":
        raise ParseError(
            "body is markup, not CSV — an HTML error page answered with a 200; it must not "
            "become corporate-action rows",
            filename=filename,
        )
    return text


def _row(
    raw: Sequence[str],
    *,
    filename: str,
    line: int,
    knowable_date: date,
    l0_key: str | None,
) -> BcRow:
    """One published line → one `BcRow`, or a `ParseError` naming the line."""
    if len(raw) != len(BC_COLUMNS):
        raise ParseError(
            f"expected {len(BC_COLUMNS)} columns, got {len(raw)}: {raw!r}",
            filename=filename,
            line=line,
        )
    series, symbol, security, record, bc_start, bc_end, ex, nd_start, nd_end, purpose = (
        cell.strip() for cell in raw
    )
    if not symbol:
        raise ParseError("row has no SYMBOL", filename=filename, line=line)
    if not purpose:
        raise ParseError(f"row for {symbol!r} has no PURPOSE", filename=filename, line=line)

    return BcRow(
        knowable_date=knowable_date,
        series=series,
        symbol=symbol,
        security_name=security,
        purpose=purpose,
        record_date=_parse_date(record, field="RECORD_DT", filename=filename, line=line),
        book_closure_start=_parse_date(bc_start, field="BC_STRT_DT", filename=filename, line=line),
        book_closure_end=_parse_date(bc_end, field="BC_END_DT", filename=filename, line=line),
        ex_date=_parse_date(ex, field="EX_DT", filename=filename, line=line),
        no_delivery_start=_parse_date(nd_start, field="ND_STRT_DT", filename=filename, line=line),
        no_delivery_end=_parse_date(nd_end, field="ND_END_DT", filename=filename, line=line),
        l0_key=l0_key,
    )


def _parse_date(text: str, *, field: str, filename: str, line: int) -> date | None:
    """`DD/MM/YYYY` or `YYYY-MM-DD` → a date; `None` for the era's empty markers.

    Both shapes are accepted on every date rather than selected by era: the cutover between them
    is bracketed to (2025-10-01, 2025-11-03] and not pinned, and a value that is unambiguously one
    shape or the other needs no boundary to read. A value that is neither raises — a date we
    cannot read must never become a silent `None`, because a missing ex-date and a misparsed one
    are worlds apart in an adjustment chain.
    """
    value = text.strip()
    if value.lower() in _EMPTY_MARKERS:
        return None
    slash = _SLASH_RE.match(value)
    if slash is not None:
        return _build(
            int(slash.group(3)),
            int(slash.group(2)),
            int(slash.group(1)),
            value=value,
            field=field,
            filename=filename,
            line=line,
        )
    dash = _DASH_RE.match(value)
    if dash is not None:
        return _build(
            int(dash.group(1)),
            int(dash.group(2)),
            int(dash.group(3)),
            value=value,
            field=field,
            filename=filename,
            line=line,
        )
    raise ParseError(
        f"{field} is {value!r}, which is neither DD/MM/YYYY nor YYYY-MM-DD",
        filename=filename,
        line=line,
    )


def _build(
    year: int, month: int, day: int, *, value: str, field: str, filename: str, line: int
) -> date:
    """Assemble a date, turning an impossible one into a located `ParseError`."""
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ParseError(f"{field} is {value!r}: {exc}", filename=filename, line=line) from exc
