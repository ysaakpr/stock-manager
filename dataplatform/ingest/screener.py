"""Screener per-company ingestion (§4.1 "Fundamentals (restated)", M7.1) — monitoring only.

Screener publishes *restated* fundamentals: figures redrawn under today's accounting treatment,
with no as-of date attached. That makes them useful for a human eyeballing a company (T1/T2
monitoring) and radioactive to anything that reasons about the past. A restated 2019 revenue is
not the number a decision made in 2019 could have seen, so this feed must never reach a backtest
or a decision (decision #7, invariant #8). This module is the ingest half; the physical
quarantine — a separate store root with no path back to the PIT lake — is `store/restated.py`,
and the structural enforcement that a backtest cannot read it is M7.2.

Two things this file is careful about, both named by the acceptance criteria:

* **The crawler provably cannot request a robots-disallowed path.** Screener's robots.txt (recorded
  by C.1 in `source_register.yaml`) disallows listing pagination, search, sorting, per-quarter
  source pages and `/user/*`; AGENTIC_CONTEXT §8 makes respecting them non-negotiable. Rather than
  re-encode those rules here, the crawler resolves the *same* `CrawlPolicy` the rest of D1's
  fetcher uses and refuses any URL it would refuse — before a socket exists. `company_url` builds
  the one permitted shape, and `guard` is the check every candidate URL passes; a symbol that could
  smuggle a query string past them is rejected at construction.
* **The parser never fetches.** It takes bytes (or an `L0Ref` it reads back through `L0Store`) and
  turns one company page into `ScreenerDatum`s. Live fetching is deliberately not here: a
  full-universe pull is a bulk campaign reserved to a human go (B1), so this module builds the
  parser and the URL policy and leaves the fetch loop to the backfill runner.

What it never does: adjust a number, attach a knowable/as-of date it was not given (there is none —
that is the whole point of "restated"), resolve a symbol to an ISIN by name alone (that goes through
the D2 identity master in `store/restated.py`), or hold a value as a float.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from dataplatform.ingest.models import IngestError, ParseError
from dataplatform.ingest.policy import CrawlPolicy, resolve_policy
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "SCREENER_HOST",
    "SCREENER_SOURCE_ID",
    "SCREENER_SOURCE_TAG",
    "ScreenerCrawler",
    "ScreenerDatum",
    "ScreenerError",
    "ScreenerPolicyError",
    "company_symbol_of",
    "parse",
    "parse_html",
    "parse_l0",
]

_LOG = get_logger(__name__)

#: The register id this parser serves (`source_register.yaml`, `parser.task: M7.1`).
SCREENER_SOURCE_ID: Final = "screener_company_fundamentals"

#: The host the register records robots rules for; the crawler refuses any other host.
SCREENER_HOST: Final = "www.screener.in"

#: The tag stamped on every datum this source produces (invariant #8 / M7.1 acceptance #3).
#: A literal, not a parameter: a restated datum that reached the store without this tag would be
#: indistinguishable from PIT data, and the whole quarantine turns on being able to tell them apart.
SCREENER_SOURCE_TAG: Final = "screener_restated"

#: A Screener company slug: letters, digits and the handful of punctuation real tickers carry
#: (`M&M`, `BAJAJ-AUTO`, `3MINDIA`). Deliberately excludes `?`, `/`, `#`, `%` and whitespace, so a
#: slug can never introduce a query string or escape its path segment — that is what makes
#: `company_url` unable to build a robots-disallowed URL in the first place.
_SLUG: Final = re.compile(r"^[A-Za-z0-9&._-]{1,25}$")

#: The one permitted per-company path shape. `/company/{slug}/consolidated/` is the consolidated
#: variant Screener also serves and robots also permits; both are built here and pass the guard.
_COMPANY_PATH: Final = "/company/{slug}/"
_COMPANY_CONSOLIDATED_PATH: Final = "/company/{slug}/consolidated/"

#: Characters stripped off a cell before it is read as a number: the rupee sign, the "Cr."/"%"
#: units Screener appends, thousands separators and surrounding whitespace. A trailing/leading
#: absence marker (`""`) means "no value for this period" and yields no datum, never a zero.
_NUMBER_NOISE: Final = re.compile(r"[₹,%\s]|Cr\.?|Crs?\.?|Rs\.?")


class ScreenerError(IngestError):
    """Base for Screener ingestion failures — a policy refusal or a parse failure."""


class ScreenerPolicyError(ScreenerError):
    """A URL the crawler must not request: a bad slug, the wrong host, or a disallowed path."""


class ScreenerDatum(BaseModel):
    """One restated figure lifted off one company page, before it has an ISIN.

    What it does: carry a single `(statement, metric, period)` cell and its value, tagged as
    restated at construction so the tag cannot be lost between here and the store.
    What it assumes: the parser has already decoded the page and located the cell; a `ScreenerDatum`
    that exists is a number the page really showed.
    What it never does: hold an ISIN (resolution goes through the identity master in
    `store/restated.py`), an adjusted or PIT figure, a knowable date (restated data has none), or a
    value as a float. `value` may be negative — a loss-making quarter is a real restated figure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(
        min_length=1, description="Screener company slug, e.g. RELIANCE, as on page"
    )
    statement: str = Field(
        min_length=1, description="which statement the cell came from, e.g. profit_loss, ratios"
    )
    metric: str = Field(min_length=1, description="the row label, verbatim, e.g. 'Net Profit'")
    period: str = Field(
        min_length=1, description="the column label, verbatim, e.g. 'Mar 2024', 'TTM', 'Current'"
    )
    value: Decimal = Field(
        strict=True,
        allow_inf_nan=False,
        description="the restated figure; may be negative, never NaN/Infinity, never a float",
    )
    source: Literal["screener_restated"] = Field(
        default=SCREENER_SOURCE_TAG,
        description="the source tag (invariant #8); fixed by construction",
    )


class ScreenerCrawler:
    """The per-company URL surface, with the robots check wired in front of every URL.

    What it does: builds the one permitted per-company URL shape and refuses — before any request —
    a URL the host's robots.txt disallows or that points at another host.
    What it assumes: the Source Register's screener row and host record are current (C.1 recorded
    them; a stale record is re-checked before a campaign, AGENTIC_CONTEXT §8).
    What it never does: open a socket, vary by attempt, or offer a way past the robots rules. There
    is no override flag: a disallowed path is not reachable through this object at all.
    """

    def __init__(self, policy: CrawlPolicy) -> None:
        self._policy = policy

    @classmethod
    def from_register(cls, register: SourceRegister | None = None) -> ScreenerCrawler:
        """Wire the crawler from the checked-in Source Register (or a supplied one, for tests)."""
        reg = load_register() if register is None else register
        return cls(resolve_policy(SCREENER_SOURCE_ID, reg))

    @property
    def host(self) -> str:
        """The one host this crawler will talk to."""
        return self._policy.host

    def company_url(self, symbol: str, *, consolidated: bool = False) -> str:
        """The permitted company-page URL for `symbol`, robots-checked before it is returned.

        Raises `ScreenerPolicyError` for a slug that is not a plain company slug, and (via `guard`)
        for the impossible case that even the plain path is disallowed — so a caller that only ever
        uses this method cannot construct a request the policy would refuse.
        """
        slug = self._slug(symbol)
        path = _COMPANY_CONSOLIDATED_PATH if consolidated else _COMPANY_PATH
        url = f"https://{self._policy.host}{path.format(slug=slug)}"
        self.guard(url)
        return url

    def guard(self, url: str) -> None:
        """Raise `ScreenerPolicyError` unless `url` is on the right host and robots-permitted.

        The single enforcement point: `CrawlPolicy.check_url` compares the host and applies the
        host's robots disallow rules to path *and* query together, which is what catches Screener's
        `?page=`, `?q=`, `?sort=` and `?limit=` rules that a path-only matcher would wave through.
        """
        try:
            self._policy.check_url(url)
        except Exception as exc:
            raise ScreenerPolicyError(str(exc)) from exc

    def allows(self, url: str) -> bool:
        """Whether `url` would be permitted, without raising. For tests and pre-flight checks."""
        try:
            self.guard(url)
        except ScreenerPolicyError:
            return False
        return True

    @staticmethod
    def _slug(symbol: str) -> str:
        clean = symbol.strip()
        if not _SLUG.match(clean):
            raise ScreenerPolicyError(
                f"{symbol!r} is not a plain Screener company slug (letters, digits and & . _ - "
                "only); a slug that could carry a query string is refused so the crawler can never "
                "build a robots-disallowed URL"
            )
        return clean


def company_symbol_of(url: str) -> str:
    """The company slug in a Screener company URL — the inverse of `ScreenerCrawler.company_url`.

    Raises `ScreenerError` if the URL is not a `/company/<slug>/…` path, so a caller cannot mistake
    a listing or search URL for a company one.
    """
    parts = [segment for segment in urlsplit(url).path.split("/") if segment]
    if len(parts) < 2 or parts[0] != "company":
        raise ScreenerError(f"{url!r} is not a Screener /company/<slug>/ URL")
    return parts[1]


def parse(payload: bytes, *, filename: str, symbol: str | None = None) -> tuple[ScreenerDatum, ...]:
    """Parse one Screener company page into restated datums, in page order.

    `symbol` overrides the slug read from the page's canonical link; supply it when the fetch
    context knows which company was requested (the crawler does). When omitted, the canonical link
    is authoritative and its absence is a `ParseError` — a page with no identity produces no datum.

    Raises `ParseError`, naming the file, for a non-UTF-8 body, a page carrying no company identity,
    or a page with no recognisable financial table (a soft 404 or a redirect shell, which must not
    be mistaken for a company with no data).
    """
    return parse_html(_text_of(payload, filename=filename), filename=filename, symbol=symbol)


def parse_l0(store: L0Store, ref: L0Ref, *, symbol: str | None = None) -> tuple[ScreenerDatum, ...]:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in.

    `L0Store.get` re-hashes the payload, so "every value derives from bytes that have not changed"
    holds at the point of derivation, not only at fetch. Defaults the company slug to the L0 key's
    filename stem when the page carries no canonical link, so an archived page still resolves.
    """
    fallback = symbol
    if fallback is None:
        stem = ref.filename.rsplit(".", 1)[0]
        fallback = stem or None
    return parse(store.get(ref), filename=ref.filename, symbol=fallback)


def parse_html(text: str, *, filename: str, symbol: str | None = None) -> tuple[ScreenerDatum, ...]:
    """Parse the decoded HTML body. Split from `parse` so a caller can hand over text it has."""
    reader = _ScreenerHTMLParser()
    reader.feed(text)
    reader.close()

    resolved_symbol = symbol or reader.canonical_symbol
    if not resolved_symbol:
        raise ParseError(
            "no company identity: neither a caller-supplied symbol nor a canonical "
            "/company/<slug>/ link was found — this is not a parseable company page",
            filename=filename,
        )

    datums: list[ScreenerDatum] = []
    for statement, metric, period, raw in reader.cells:
        value = _to_decimal(raw)
        if value is None:
            continue
        datums.append(
            ScreenerDatum(
                symbol=resolved_symbol,
                statement=statement,
                metric=metric,
                period=period,
                value=value,
            )
        )

    if not datums:
        raise ParseError(
            "no restated figures found on the page; a Screener company page always carries at "
            "least one data table, so an empty parse means a soft 404 or a changed layout",
            filename=filename,
        )

    _LOG.info(
        "screener.parsed",
        source=SCREENER_SOURCE_ID,
        filename=filename,
        symbol=resolved_symbol,
        datums=len(datums),
        state="NORMALIZED",
    )
    return tuple(datums)


class _ScreenerHTMLParser(HTMLParser):
    """Pull the company slug and every `<table class="data-table">` / `#top-ratios` cell off a page.

    A small state machine rather than a DOM: the standard-library parser hands us a stream of start,
    data and end events, and we track just enough of the nesting (section → heading → table →
    thead/tbody → row → cell) to attach each value to its statement, metric and period. Nothing
    here trusts column *order* beyond "first cell is the label"; a table is read by its own header
    row, so a reordered set of period columns still lines each value up with its own period.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical_symbol: str | None = None
        #: (statement, metric, period, raw_value) tuples, in page order.
        self.cells: list[tuple[str, str, str, str]] = []

        self._statement: str | None = None
        self._capture_heading = False
        self._heading_parts: list[str] = []

        self._in_table = False
        self._section_is_ratios = False
        self._in_thead = False
        self._in_tbody = False
        self._periods: list[str] = []
        self._row_cells: list[str] = []

        self._capture_cell = False
        self._cell_parts: list[str] = []

        self._in_ratios = False
        self._ratio_name: str | None = None
        self._ratio_value_parts: list[str] | None = None
        self._ratio_target: str = ""

    # ── start tags ───────────────────────────────────────────────────────────────────────────
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name: (value or "") for name, value in attrs}
        if tag == "link" and _rel_is_canonical(attr):
            self.canonical_symbol = _slug_of_href(attr.get("href", ""))
            return
        if tag == "section":
            self._statement = _slugify(attr.get("id", "")) or None
            return
        if tag in {"h1", "h2", "h3"}:
            self._capture_heading = True
            self._heading_parts = []
            return
        if tag == "ul" and attr.get("id") == "top-ratios":
            self._in_ratios = True
            return
        if self._in_ratios and tag == "li":
            self._ratio_name = None
            self._ratio_value_parts = None
            return
        if self._in_ratios and tag == "span":
            cls = attr.get("class", "")
            if "name" in cls.split():
                self._capture_cell = True
                self._cell_parts = []
                self._ratio_target = "name"
            elif "value" in cls.split() or "number" in cls.split():
                self._capture_cell = True
                self._cell_parts = []
                self._ratio_target = "value"
            return
        if tag == "table" and "data-table" in attr.get("class", "").split():
            self._in_table = True
            self._periods = []
            return
        if not self._in_table:
            return
        if tag == "thead":
            self._in_thead = True
        elif tag == "tbody":
            self._in_tbody = True
        elif tag == "tr":
            self._row_cells = []
        elif tag in {"th", "td"}:
            self._capture_cell = True
            self._cell_parts = []

    # ── text ─────────────────────────────────────────────────────────────────────────────────
    def handle_data(self, data: str) -> None:
        if self._capture_heading:
            self._heading_parts.append(data)
        elif self._capture_cell:
            self._cell_parts.append(data)

    # ── end tags ─────────────────────────────────────────────────────────────────────────────
    def handle_endtag(self, tag: str) -> None:
        if tag in {"h1", "h2", "h3"} and self._capture_heading:
            self._capture_heading = False
            heading = _slugify("".join(self._heading_parts))
            # A heading names its statement only when a section did not already; the section id is
            # the more stable label when both exist.
            if heading and not self._statement:
                self._statement = heading
            return
        if self._in_ratios:
            self._end_ratio_tag(tag)
        if self._in_table:
            self._end_table_tag(tag)

    def _end_ratio_tag(self, tag: str) -> None:
        if tag == "span" and self._capture_cell:
            text = "".join(self._cell_parts).strip()
            self._capture_cell = False
            if self._ratio_target == "name":
                self._ratio_name = text
            else:
                self._ratio_value_parts = [text]
        elif tag == "li":
            if self._ratio_name and self._ratio_value_parts:
                self.cells.append(
                    ("ratios", self._ratio_name, "current", self._ratio_value_parts[0])
                )
            self._ratio_name = None
            self._ratio_value_parts = None
        elif tag == "ul":
            self._in_ratios = False

    def _end_table_tag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._capture_cell:
            self._row_cells.append("".join(self._cell_parts).strip())
            self._capture_cell = False
        elif tag == "tr":
            self._end_row()
        elif tag == "thead":
            self._in_thead = False
        elif tag == "tbody":
            self._in_tbody = False
        elif tag == "table":
            self._in_table = False
            self._in_thead = False
            self._in_tbody = False

    def _end_row(self) -> None:
        cells = self._row_cells
        self._row_cells = []
        if not cells:
            return
        # A header row (in <thead>, or a headerless table's first row) defines the periods: every
        # cell after the first (the empty label corner) is a period column.
        if (self._in_thead or not self._periods) and (self._in_thead or _looks_like_header(cells)):
            self._periods = list(cells[1:])
            return
        metric = cells[0]
        statement = self._statement or "unknown"
        if not metric:
            return
        for index, raw in enumerate(cells[1:]):
            period = self._periods[index] if index < len(self._periods) else f"col{index + 1}"
            if raw:
                self.cells.append((statement, metric, period, raw))


def _looks_like_header(cells: list[str]) -> bool:
    """A headerless table's first row is its header iff its label corner is empty."""
    return bool(cells) and cells[0] == ""


def _rel_is_canonical(attr: dict[str, str]) -> bool:
    return attr.get("rel", "").lower() == "canonical"


def _slug_of_href(href: str) -> str | None:
    try:
        return company_symbol_of(href)
    except ScreenerError:
        return None


def _slugify(text: str) -> str:
    """Lower-case a heading/section id into a statement key: `Profit & Loss` → profit_loss."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.strip().lower())
    return cleaned.strip("_")


def _to_decimal(raw: str) -> Decimal | None:
    """Turn a Screener cell into a `Decimal`, or `None` when the cell holds no number.

    Strips the rupee sign, the `Cr.`/`%` units and thousands separators, reads a parenthesised
    value as negative (an accounting convention Screener uses for losses), and returns `None` for
    an empty cell or a non-numeric label — a missing period is absence, never a zero.
    """
    text = raw.strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    stripped = _NUMBER_NOISE.sub("", text)
    if stripped in {"", "-", "—"}:
        return None
    if negative and not stripped.startswith("-"):
        stripped = f"-{stripped}"
    try:
        value = Decimal(stripped)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return value


def _text_of(payload: bytes, *, filename: str) -> str:
    """Decode the page as UTF-8. A non-UTF-8 body is a `ParseError`, not a mojibake best-effort."""
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(f"page is not valid UTF-8: {exc}", filename=filename) from exc
