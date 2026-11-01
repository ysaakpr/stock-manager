"""A10 · M17.2 — factual digests of each session's new filings, one model call per filing.

Pre-registration §4 step 2: the Commons build digests the session's new exchange announcements and
results filings, once, for every manager. A digest says what was disclosed, with its numbers and
its period, and nothing else.

**What a filing is.** Two kinds, both read from what the L1 store already holds:

- an NSE announcement (`dataplatform.ingest.announcements`, through
  :meth:`DigestSource.announcement_texts`). It is identified by the exchange's own id, and its text
  is the subject, category and body the store carries. The attached PDF is named by its link and
  never fetched here;
- a results filing (L1 ``pit_fundamentals``, through :meth:`DigestSource.results_filings`). It is
  identified by its ``filing_id``, and its text is the company-level facts the XBRL parser kept.

**Which names.** The daily build (:func:`build_digests`) digests the names it is handed, and the
Commons hands it the *digest scope* (M17.9): the names on screens S1-S5 and the composite shortlist
(`CommonsScreens.digest_scope`), not the whole universe. That keeps the daily build to tens of
model calls rather than hundreds. A name a manager researches outside that scope is digested on
demand by :func:`digest_for_isins`, with the same schema, the same validation and the same cache:
a filing already digested by either path is never digested again. Each filing's text is
bounded (:data:`MAX_FILING_CHARS`, :data:`MAX_RESULT_FACTS`). A truncated input is marked, both in
the prompt and in the digest.

**Which filings are new.** The window is ``(since, session]`` by knowable date. ``since`` is the
session of the last run with no failures (`DigestStore.resume_after`), or, when every earlier run
failed somewhere, the earliest of their window starts. With no earlier run it is the previous NSE
session. It is never more than :data:`DIGEST_LOOKBACK_SESSIONS` sessions back. A capped window is
a gap. Inside the window, a filing whose id is already in the digest store is a cache hit and
costs nothing. So a filing is digested once across every build and every manager, and a failed
filing is retried by the next build, until it falls out of the window.

**Factual only.** The model fills :data:`DIGEST_OUTPUT_SCHEMA` (``additionalProperties: false`` at
every level), which has no field for a recommendation, a rating or a price view.
:class:`DigestBody` refuses an extra key, and :func:`opinion_terms` refuses opinion wording in any
text field (``target price``, ``outperform``, ``undervalued`` and the like). **Grounded**
(``commons-digest/2``, 2026-11-01): every number in a figure must be one the filing states
(:func:`ground_digest`; digit grouping, scale words, rounding and date formats are read
numerically). A refused digest is never stored. The run records it as a failure with the reason,
and the next build retries it.

**Metered.** One call per uncached filing, through :mod:`analyst.llm` (so `StubLLM` drives the
tests), on the model the caller names — the M17 roster's ``models.digest`` (Amendment 5:
`claude-sonnet-5-5` on Bedrock, the same model on the Claude CLI when Bedrock cannot serve it).
There is no default model here: a model change is the roster's, never a constant's. Each digest
records the provider and the model that actually answered, the prompt digest and the token usage.

What it never does: fetch a document, read a wall clock, store a digest that failed validation,
hold a manager's view, or import `analyst.fundmanager`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from analyst.commons.sheets import (
    CommonsRefusedError,
    FilingFact,
    Gap,
    SourceUnavailableError,
    canonical_bytes,
)
from analyst.llm import LLM, LLMError, LLMResponse, Message, Role, ToolSpec, prompt_digest
from analyst.monitor.interlock import GreenGate
from dataplatform.clock import IST, Clock
from dataplatform.logging import get_logger
from dataplatform.query import Dataset, PitContext

__all__ = [
    "DIGEST_LOOKBACK_SESSIONS",
    "DIGEST_MAX_TOKENS",
    "DIGEST_OUTPUT_SCHEMA",
    "DIGEST_SYSTEM_PROMPT",
    "DIGEST_TOOL",
    "DIGEST_VERSION",
    "MAX_FILING_CHARS",
    "MAX_RESULT_FACTS",
    "ON_DEMAND_LOOKBACK_SESSIONS",
    "AnnouncementText",
    "DigestBody",
    "DigestFailure",
    "DigestRefusedError",
    "DigestRun",
    "DigestSource",
    "DigestStore",
    "FilingDigest",
    "FilingInput",
    "FilingKind",
    "OnDemandDigests",
    "build_digests",
    "digest_filing",
    "digest_for_isins",
    "digest_messages",
    "filing_inputs",
    "ground_digest",
    "opinion_terms",
    "parse_digest",
    "ungrounded_figures",
]

_LOG = get_logger(__name__)

#: Versioned identity of the prompt, the schema and the validation. Part of every digest.
DIGEST_VERSION: Final = "commons-digest/2"
#: Output ceiling per digest. A digest is a paragraph and a table, not an essay.
DIGEST_MAX_TOKENS: Final = 2_000
#: The furthest back a run looks for a filing, in NSE sessions before the session.
DIGEST_LOOKBACK_SESSIONS: Final = 5
#: Input bounds per filing: characters of announcement text, and lines of results facts.
MAX_FILING_CHARS: Final = 6_000
MAX_RESULT_FACTS: Final = 120
#: Output bounds: what one digest may say.
_MAX_HEADLINE: Final = 200
_MAX_DISCLOSED: Final = 2_000
_MAX_FIGURES: Final = 30
_MAX_FIELD: Final = 200

_ANNOUNCEMENT_PREFIX: Final = "nse_announcements:"
_RESULTS_PREFIX: Final = "pit_fundamentals:"


class FilingKind(StrEnum):
    """What a digested filing is."""

    ANNOUNCEMENT = "ANNOUNCEMENT"
    RESULTS = "RESULTS"


class DigestRefusedError(ValueError):
    """The model's answer is not a factual digest: wrong shape, an extra field, or an opinion."""


# ── the output schema ────────────────────────────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DigestFigure(_Strict):
    """One number the filing states, as text exactly as stated, with its unit and period."""

    label: str = Field(min_length=1, max_length=_MAX_FIELD)
    value: str = Field(min_length=1, max_length=_MAX_FIELD)
    unit: str | None = Field(default=None, max_length=_MAX_FIELD)
    period: str | None = Field(default=None, max_length=_MAX_FIELD)


class DigestBody(_Strict):
    """What the model returns: what was disclosed, the period, and the figures. Nothing else.

    No field holds a recommendation, a rating or a price view, and an extra key is refused
    (``extra="forbid"``, matching the schema's ``additionalProperties: false``).
    """

    headline: str = Field(min_length=1, max_length=_MAX_HEADLINE)
    disclosed: str = Field(min_length=1, max_length=_MAX_DISCLOSED)
    period: str | None = Field(default=None, max_length=_MAX_FIELD)
    figures: tuple[DigestFigure, ...] = Field(default=(), max_length=_MAX_FIGURES)


def _string(max_length: int, description: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": max_length}
    return node if description is None else {**node, "description": description}


def _nullable_string(max_length: int, description: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": ["string", "null"], "maxLength": max_length}
    return node if description is None else {**node, "description": description}


_HEADLINE_RULE: Final = (
    "One line, at most 200 characters. For a results filing: the basis (standalone or "
    "consolidated) and the period, then revenue from operations and profit after tax (profit "
    "for the period) of the latest period, with their values and units, whenever the filing "
    "states them. For an announcement: the company and what it disclosed."
)
_PERIOD_RULE: Final = (
    "The date or period the filing covers or concerns, as the text states it: a results "
    "period, or an announcement's meeting, record, effective or event date. Null only when "
    "the text states none."
)
_FIGURES_RULE: Final = (
    "Every number the filing states. For an announcement that includes every date, percentage, "
    "quantity and amount in the text, even when the headline or 'disclosed' already says it. "
    "Never a number you computed: no change, growth rate, margin, ratio, sum or conversion of "
    "your own. A figure whose number is not in the filing text is refused."
)
_VALUE_RULE: Final = (
    "The number, date or percentage exactly as the text writes it; never a value you computed."
)


#: The JSON Schema the model fills, written out rather than generated so it is frozen with
#: :data:`DIGEST_VERSION`. ``additionalProperties: false`` at every level: no opinion field exists.
DIGEST_OUTPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "disclosed", "period", "figures"],
    "properties": {
        "headline": _string(_MAX_HEADLINE, _HEADLINE_RULE),
        "disclosed": _string(_MAX_DISCLOSED),
        "period": _nullable_string(_MAX_FIELD, _PERIOD_RULE),
        "figures": {
            "type": "array",
            "maxItems": _MAX_FIGURES,
            "description": _FIGURES_RULE,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "value", "unit", "period"],
                "properties": {
                    "label": _string(_MAX_FIELD),
                    "value": _string(_MAX_FIELD, _VALUE_RULE),
                    "unit": _nullable_string(_MAX_FIELD),
                    "period": _nullable_string(_MAX_FIELD),
                },
            },
        },
    },
}

DIGEST_TOOL: Final = ToolSpec(
    name="filing_digest",
    description=(
        "Record the factual digest of one exchange filing: a one-line headline, what was "
        "disclosed, the period it covers, and each number it states."
    ),
    input_schema=DIGEST_OUTPUT_SCHEMA,
)

DIGEST_SYSTEM_PROMPT: Final = (
    "You transcribe Indian listed-company filings into a factual digest. You are given one "
    "filing as JSON: its kind, its date, and the text the exchange published or the figures "
    "the company filed. Record what was disclosed, the period it covers, and every number it "
    "states, with its unit, exactly as stated. Use only the text you are given. If the text "
    "only names an attached document, say that the detail is in the attachment and do not "
    "guess its content. Never give a recommendation, a rating, a price target, a valuation "
    "view, an expectation or any opinion on the company or its shares. If the text is "
    "truncated, say so in 'disclosed'. "
    "'figures' holds numbers the filing states, never one you computed: no change, growth "
    "rate, margin, ratio, sum or conversion of your own. Copy each value as the text writes "
    "it; a figure whose number is not in the text is refused. "
    "For a results filing, the headline names the basis (standalone or consolidated) and the "
    "period, then revenue from operations and profit after tax (profit for the period) of the "
    "latest period, with their values and units, whenever the filing states them. "
    "For an announcement, every date, percentage, quantity and amount the text states (a "
    "meeting, record or effective date, a stake, a capacity, a price, an order value) is also "
    "a figure, even when the headline or 'disclosed' already says it; and 'period' is the date "
    "or period the announcement concerns whenever the text states one. The dissemination time "
    "is not a figure."
)

#: Opinion wording a factual digest never needs. Matched case-insensitively on word boundaries in
#: every text field. A credit-rating *action* is a fact and stays allowed ("rating" alone is not
#: here), and so is a fair-value line in a balance sheet ("fair value" is not here either). The
#: terms are the ones that state a view on the shares.
_OPINION_TERMS: Final[tuple[str, ...]] = (
    "recommend",
    "recommends",
    "recommended",
    "recommendation",
    "target price",
    "price target",
    "outperform",
    "underperform",
    "overweight",
    "underweight",
    "undervalued",
    "overvalued",
    "bullish",
    "bearish",
    "strong buy",
    "buy rating",
    "sell rating",
    "hold rating",
    "should buy",
    "should sell",
    "worth buying",
    "we expect",
    "we believe",
    "investors should",
)
_OPINION: Final = re.compile(
    r"\b(" + "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in _OPINION_TERMS) + r")\b",
    re.IGNORECASE,
)


def opinion_terms(body: DigestBody) -> tuple[str, ...]:
    """The opinion terms found in any text field of ``body``, lowercased and sorted."""
    texts = [body.headline, body.disclosed, body.period or ""]
    for figure in body.figures:
        texts += [figure.label, figure.value, figure.unit or "", figure.period or ""]
    found = {" ".join(m.group(1).lower().split()) for t in texts for m in _OPINION.finditer(t)}
    return tuple(sorted(found))


def parse_digest(arguments: Mapping[str, Any]) -> DigestBody:
    """Validate the model's structured output into a :class:`DigestBody`, or refuse it.

    Raises :class:`DigestRefusedError` for a missing or extra field (an opinion field such as
    ``recommendation`` is an extra field) or for opinion wording in any text.
    """
    try:
        body = DigestBody.model_validate(dict(arguments))
    except ValidationError as exc:
        extra = sorted(
            str(err["loc"][-1]) for err in exc.errors() if err["type"] == "extra_forbidden"
        )
        if extra:
            raise DigestRefusedError(
                f"the digest carries fields outside the schema: {extra}"
            ) from exc
        raise DigestRefusedError(f"the digest does not match the schema: {exc}") from exc
    terms = opinion_terms(body)
    if terms:
        raise DigestRefusedError(f"the digest states an opinion: {list(terms)}")
    return body


# ── grounding: every figure's number is in the filing ───────────────────────────────────────────
#
# A figure is a number the filing states (the schema and the prompt say so), so each number in a
# figure's ``value`` must be found in what the model was given. "Found" is numeric, not textual:
# Indian and western digit grouping (1,23,456 / 123,456), trailing zeros (5030.6 / 5030.60), a
# percent sign, a scale word on either side (5,030.6 million is 5030600000; 120 crore is
# 1,20,00,00,000; "Rupees Ten Crore" is 10 crore), a figure rounded to the precision it is written
# at (503.06 crore for 5030645123), a sign or a bracketed loss, and a date in any common format
# (02nd of November 2026 / 2 Nov 2026 / 2026-11-02 / 02/11/2026). A computed value — a change, a
# margin, a ratio, a sum — is not in the text and is refused: the schema never asks for one.

#: Scale words: the number before one also stands for itself times the scale.
_SCALES: Final[Mapping[str, Decimal]] = {
    **dict.fromkeys(("thousand", "thousands"), Decimal(10) ** 3),
    **dict.fromkeys(("lakh", "lakhs", "lac", "lacs"), Decimal(10) ** 5),
    **dict.fromkeys(("million", "millions", "mn", "mln"), Decimal(10) ** 6),
    **dict.fromkeys(("crore", "crores", "cr", "crs"), Decimal(10) ** 7),
    **dict.fromkeys(("billion", "billions", "bn"), Decimal(10) ** 9),
}
_SCALE_AFTER: Final = re.compile(
    r"\)?\s*(?:-\s*)?(" + "|".join(sorted(_SCALES, key=len, reverse=True)) + r")\b\.?",
    re.IGNORECASE,
)
#: A run of digits and commas with an optional decimal part. Commas are checked afterwards.
_NUMBER: Final = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")
_PERCENT: Final = re.compile(r"%|\bper\s*cent\b|\bpercent(?:age)?\b", re.IGNORECASE)

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
_MONTH: Final = r"(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?(?![a-z])"
_DAY: Final = r"(\d{1,2})(?:st|nd|rd|th)?"
#: Dates, most specific first. Each match is (year | None, month, day) once read; a numeric
#: day-month-year is read both ways round, so a figure never fails on the convention alone.
_ISO_DATE: Final = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_NUMERIC_DATE: Final = re.compile(r"(?<![\d.])(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?![\d])")
_DAY_MONTH: Final = re.compile(
    r"(?<![\d.])" + _DAY + r"(?:\s+of)?[\s,./-]*" + _MONTH + r"(?:[\s,./'-]*(\d{4})(?!\d))?",
    re.IGNORECASE,
)
_MONTH_DAY: Final = re.compile(
    r"(?<![a-z])" + _MONTH + r"[\s,./-]*" + _DAY + r"(?![\d:])(?:,?\s*(\d{4})(?!\d))?",
    re.IGNORECASE,
)

_UNITS: Final[Mapping[str, int]] = {
    word: n
    for n, word in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
        ]
    )
}
_TENS: Final[Mapping[str, int]] = {
    word: 10 * n
    for n, word in enumerate(
        ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"], start=2
    )
}
_WORD_SCALES: Final[Mapping[str, Decimal]] = {"hundred": Decimal(100), **_SCALES}
_NUMBER_WORDS: Final = re.compile(
    r"\b(?:(?:"
    + "|".join(sorted([*_UNITS, *_TENS, *_WORD_SCALES], key=len, reverse=True))
    + r")\b[\s-]*(?:and\s+)?)+",
    re.IGNORECASE,
)

type _Date = tuple[int | None, int, int]


def _grouped(digits: str) -> Decimal | None:
    """The value of a comma-grouped run (western 1,234,567 or Indian 12,34,567), else ``None``.

    The Indian head may run to three digits: filings write "Rs. 466,73,61,738.00".
    """
    whole, _, fraction = digits.partition(".")
    parts = whole.split(",")
    if len(parts) == 1:
        return Decimal(digits)
    head, middle, last = parts[0], parts[1:-1], parts[-1]
    western = 1 <= len(head) <= 3 and all(len(p) == 3 for p in parts[1:])
    indian = 1 <= len(head) <= 3 and all(len(p) == 2 for p in middle) and len(last) == 3
    if not (western or indian):
        return None
    return Decimal(whole.replace(",", "") + ("." + fraction if fraction else ""))


def _numbers(text: str, *, split_groups: bool) -> list[tuple[Decimal, Decimal | None]]:
    """Every number in ``text`` as (absolute value, the scale word written after it).

    A comma run that is not a valid grouping ("28,2026") is read as its parts. With
    ``split_groups`` every comma run is read whole and as its parts (the text side, where reading
    too many numbers can only make a figure easier to find, never harder).
    """
    out: list[tuple[Decimal, Decimal | None]] = []
    for match in _NUMBER.finditer(text):
        token = match.group(0)
        scale_match = _SCALE_AFTER.match(text, match.end())
        scale = _SCALES[scale_match.group(1).lower()] if scale_match else None
        whole = _grouped(token)
        if whole is None and split_groups:
            whole = Decimal(token.replace(",", ""))
        values = [] if whole is None else [whole]
        if whole is None or (split_groups and "," in token):
            pieces = token.split(",")
            values += [Decimal(p) for p in pieces[:-1] if p] + [Decimal(pieces[-1])]
        out += [(abs(v), scale) for v in values]
    return out


def _word_numbers(text: str) -> list[Decimal]:
    """Numbers written in words ("Rupees Ten Crore Only", "one lakh twenty five thousand")."""
    out: list[Decimal] = []
    for match in _NUMBER_WORDS.finditer(text):
        words = [w.lower() for w in re.findall(r"[a-z]+", match.group(0), re.IGNORECASE)]
        total, current, largest, last_scale = Decimal(0), Decimal(0), Decimal(0), None
        seen = False
        for word in words:
            if word in _UNITS:
                current += _UNITS[word]
                seen = True
            elif word in _TENS:
                current += _TENS[word]
                seen = True
            elif word in _WORD_SCALES and seen:
                scale = _WORD_SCALES[word]
                if scale == 100:
                    current = (current or Decimal(1)) * scale
                elif scale > largest:
                    total, current, largest = (total + current) * scale, Decimal(0), scale
                else:
                    total, current = total + current * scale, Decimal(0)
                last_scale = scale
        if seen:
            value = total + current
            out.append(value)
            if last_scale is not None and last_scale != 100 and current == 0:
                out.append(value / last_scale)
    return out


def _dates(text: str) -> tuple[list[list[_Date]], str]:
    """Every date written in ``text`` — one list of readings per written date — and ``text``
    with those dates blanked out, so their digits are not read again as plain numbers."""
    found: list[list[_Date]] = []
    spans: list[tuple[int, int]] = []

    def year(raw: str | None) -> int | None:
        if raw is None:
            return None
        return int(raw) + 2000 if len(raw) == 2 else int(raw)

    def add(match: re.Match[str], readings: list[_Date]) -> None:
        if any(match.start() < end and start < match.end() for start, end in spans):
            return
        valid = [(y, m, d) for y, m, d in readings if 1 <= m <= 12 and 1 <= d <= 31]
        if valid:
            found.append(valid)
            spans.append((match.start(), match.end()))

    for match in _ISO_DATE.finditer(text):
        add(match, [(int(match.group(1)), int(match.group(2)), int(match.group(3)))])
    for match in _NUMERIC_DATE.finditer(text):
        a, b, y = int(match.group(1)), int(match.group(2)), year(match.group(3))
        add(match, [(y, b, a), (y, a, b)])
    for match in _DAY_MONTH.finditer(text):
        month = _MONTHS[match.group(2).lower()]
        add(match, [(year(match.group(3)), month, int(match.group(1)))])
    for match in _MONTH_DAY.finditer(text):
        month = _MONTHS[match.group(1).lower()]
        add(match, [(year(match.group(3)), month, int(match.group(2)))])
    blanked = list(text)
    for start, end in spans:
        blanked[start:end] = " " * (end - start)
    return found, "".join(blanked)


def _same_date(a: _Date, b: _Date) -> bool:
    return a[1:] == b[1:] and (a[0] is None or b[0] is None or a[0] == b[0])


def _quantum(value: Decimal) -> Decimal:
    exponent = value.as_tuple().exponent
    return Decimal(1).scaleb(exponent if isinstance(exponent, int) and exponent < 0 else 0)


@dataclass(frozen=True, slots=True)
class _Grounds:
    """What a filing states, read once: its quantities and its dates."""

    quantities: tuple[Decimal, ...]
    dates: tuple[_Date, ...]

    @classmethod
    def of(cls, text: str) -> _Grounds:
        dates, _ = _dates(text)
        quantities: set[Decimal] = set()
        for value, scale in _numbers(text, split_groups=True):
            quantities.add(value)
            if scale is not None:
                quantities.add(value * scale)
        for value in _word_numbers(text):
            quantities.add(value)
        return cls(tuple(sorted(quantities)), tuple(d for readings in dates for d in readings))

    def has(self, value: Decimal, quantum: Decimal) -> bool:
        """``value`` is stated, exactly or rounded to the precision it is written at."""
        half = quantum / 2
        return any(abs(q - value) <= half for q in self.quantities)

    def has_date(self, readings: Sequence[_Date]) -> bool:
        return any(_same_date(r, d) for r in readings for d in self.dates)


def _figure_grounded(figure: DigestFigure, grounds: _Grounds) -> bool:
    """Every number in the figure's value is one the filing states (module comment above)."""
    dates, rest = _dates(figure.value)
    if not all(grounds.has_date(readings) for readings in dates):
        return False
    unit = figure.unit or ""
    percent = bool(_PERCENT.search(figure.value) or _PERCENT.search(unit))
    unit_scales = [_SCALES[m.group(1).lower()] for m in _SCALE_AFTER.finditer(" " + unit)]
    for value, scale in _numbers(rest, split_groups=False):
        quantum = _quantum(value)
        candidates = [(value, quantum)]
        candidates += [(value * s, quantum * s) for s in (scale, *unit_scales) if s is not None]
        if percent:
            candidates.append((value / 100, quantum / 100))
        if not any(grounds.has(v, q) for v, q in candidates):
            return False
    return True


def ungrounded_figures(body: DigestBody, item: FilingInput) -> tuple[DigestFigure, ...]:
    """The figures of ``body`` with a number that ``item`` does not state, in order.

    What it reads: the filing's text, its id and its knowable date — everything the model was
    given. What it assumes: a figure is a stated number (the schema's rule), never a computed one.
    What it never does: drop or rewrite a figure; the caller refuses the digest.
    """
    grounds = _Grounds.of(f"{item.text}\n{item.filing_id}\n{item.knowable_date.isoformat()}")
    return tuple(f for f in body.figures if not _figure_grounded(f, grounds))


def ground_digest(body: DigestBody, item: FilingInput) -> DigestBody:
    """Refuse ``body`` when any figure's number is not in the filing; else return it unchanged.

    Raises :class:`DigestRefusedError` naming each ungrounded figure, so the refusal takes the
    path every refused digest takes: never stored, named as the run's failure, retried by the
    next build (and, on demand, reported to the manager as an unfulfilled digest).
    """
    missing = ungrounded_figures(body, item)
    if missing:
        named = "; ".join(f"{f.label!r} = {f.value!r}" for f in missing)
        raise DigestRefusedError(
            f"the digest states {len(missing)} figure(s) whose number is not in the filing "
            f"text: {named}"[:450]
        )
    return body


# ── inputs ───────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AnnouncementText:
    """One NSE announcement with the text the L1 store holds. ``ref`` is the exchange's id."""

    isin: str
    ref: str
    ts: datetime
    knowable_date: date
    category: str | None
    subject: str
    body: str | None
    attachment_ref: str | None


@runtime_checkable
class DigestSource(Protocol):
    """Where the digests read the lake. Every answer is a dated :class:`Dataset`."""

    def sessions(self, through: date, count: int) -> Dataset[date]:
        """The last ``count`` NSE sessions on or before ``through``, ascending."""

    def announcement_texts(self, start: date, through: date) -> Dataset[AnnouncementText]:
        """Every NSE announcement knowable in ``[start, through]``, with its text."""

    def results_filings(self, start: date, through: date) -> Dataset[FilingFact]:
        """Every company-level PIT fundamentals fact filed in ``[start, through]``."""


@dataclass(frozen=True, slots=True)
class FilingInput:
    """One filing as the model sees it: identified, dated, and bounded."""

    filing_id: str
    kind: FilingKind
    isin: str
    knowable_date: date
    text: str
    truncated: bool

    def document(self) -> dict[str, Any]:
        return {
            "filing_id": self.filing_id,
            "kind": self.kind.value,
            "isin": self.isin,
            "knowable_date": self.knowable_date.isoformat(),
            "truncated": self.truncated,
            "text": self.text,
        }

    @property
    def input_digest(self) -> str:
        return hashlib.sha256(canonical_bytes(self.document())).hexdigest()


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


def _announcement_input(row: AnnouncementText) -> FilingInput:
    lines = [f"Subject: {row.subject}"]
    if row.category:
        lines.append(f"Category: {row.category}")
    lines.append(f"Disseminated: {row.ts.astimezone(IST).isoformat()}")
    if row.body:
        lines.append(f"Text: {row.body}")
    if row.attachment_ref:
        lines.append(f"Attachment (not included): {row.attachment_ref}")
    text, truncated = _bounded("\n".join(lines), MAX_FILING_CHARS)
    return FilingInput(
        filing_id=_ANNOUNCEMENT_PREFIX + row.ref,
        kind=FilingKind.ANNOUNCEMENT,
        isin=row.isin,
        knowable_date=row.knowable_date,
        text=text,
        truncated=truncated,
    )


def _results_input(filing_id: str, facts: Sequence[FilingFact]) -> FilingInput:
    ordered = sorted(
        facts,
        key=lambda f: (f.nature.value, f.period_end, f.period_start or date.min, f.concept),
    )
    lines = [
        f"{f.nature.value.lower()} | {f.period_start.isoformat() if f.period_start else '-'}"
        f" to {f.period_end.isoformat()} | {f.concept} | {f.value}"
        for f in ordered
    ]
    truncated = len(lines) > MAX_RESULT_FACTS
    header = (
        f"Results filing {facts[0].filing_id}, filed {facts[0].filing_date.isoformat()}.\n"
        "nature | period | concept | value (INR unless the concept is per share or a count)"
    )
    text, cut = _bounded("\n".join([header, *lines[:MAX_RESULT_FACTS]]), MAX_FILING_CHARS)
    return FilingInput(
        filing_id=filing_id,
        kind=FilingKind.RESULTS,
        isin=facts[0].isin,
        knowable_date=facts[0].filing_date,
        text=text,
        truncated=truncated or cut,
    )


def filing_inputs(
    announcements: Iterable[AnnouncementText],
    results: Iterable[FilingFact],
    *,
    isins: frozenset[str],
    after: date,
    through: date,
) -> tuple[FilingInput, ...]:
    """The bounded inputs of every filing on ``isins`` knowable in ``(after, through]``.

    One input per filing id: an announcement seen twice is one, and a results filing's facts are
    grouped. Sorted by knowable date, then filing id.
    """
    inputs: dict[str, FilingInput] = {}
    for row in announcements:
        if row.isin in isins and after < row.knowable_date <= through:
            item = _announcement_input(row)
            inputs.setdefault(item.filing_id, item)
    grouped: dict[str, list[FilingFact]] = defaultdict(list)
    for fact in results:
        if fact.isin in isins and fact.segment is None and after < fact.filing_date <= through:
            grouped[_RESULTS_PREFIX + fact.filing_id].append(fact)
    for filing_id, facts in grouped.items():
        if len({(f.isin, f.filing_date) for f in facts}) != 1:
            raise ValueError(f"results filing {filing_id} spans more than one ISIN or date")
        inputs[filing_id] = _results_input(filing_id, facts)
    return tuple(sorted(inputs.values(), key=lambda i: (i.knowable_date, i.filing_id)))


# ── outputs ──────────────────────────────────────────────────────────────────────────────────────


class FilingDigest(_Strict):
    """One stored digest: the filing it covers, what the model said, and what that cost."""

    filing_id: str = Field(min_length=1)
    kind: FilingKind
    isin: str
    knowable_date: date
    trading_date: date
    digest_version: str
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_truncated: bool
    prompt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    body: DigestBody
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    digested_at: datetime

    @field_validator("body")
    @classmethod
    def _factual(cls, body: DigestBody) -> DigestBody:
        # The same refusal on the stored record, so a digest built around parse_digest is refused.
        terms = opinion_terms(body)
        if terms:
            raise ValueError(f"the digest states an opinion: {list(terms)}")
        return body


class DigestFailure(_Strict):
    """A filing the run could not digest, and why. It is retried by the next run."""

    filing_id: str
    reason: str


class DigestRun(_Strict):
    """One run over a session: the filings it covered and what happened to each."""

    trading_date: date
    since: date
    filing_ids: tuple[str, ...]
    digested: tuple[str, ...]
    cached: tuple[str, ...]
    failures: tuple[DigestFailure, ...]
    gaps: tuple[Gap, ...]
    run_digest: str

    @staticmethod
    def digest_of(
        *,
        trading_date: date,
        since: date,
        filing_ids: Sequence[str],
        failures: Sequence[DigestFailure],
        gaps: Sequence[Gap],
    ) -> str:
        """Over what the run covered and what failed, not over which ids were cache hits."""
        document = {
            "trading_date": trading_date.isoformat(),
            "since": since.isoformat(),
            "digest_version": DIGEST_VERSION,
            "filing_ids": list(filing_ids),
            "failures": [f.model_dump(mode="json") for f in failures],
            "gaps": [g.model_dump(mode="json") for g in gaps],
        }
        return hashlib.sha256(canonical_bytes(document)).hexdigest()

    @property
    def clean(self) -> bool:
        return not self.failures


class DigestStore(Protocol):
    """Append-only storage for digests (keyed by filing id) and for the runs that made them."""

    def get(self, filing_id: str) -> FilingDigest | None:
        """The stored digest of ``filing_id``, or ``None``."""

    def record(self, digest: FilingDigest, *, recorded_at: datetime) -> bool:
        """Store ``digest``. Returns ``False`` when a digest of that filing is already stored."""

    def record_run(self, run: DigestRun, *, recorded_at: datetime) -> bool:
        """Store ``run``. Returns ``False`` when the same run is already stored."""

    def resume_after(self, before: date) -> date | None:
        """Where the next window opens for a run on ``before``, or ``None`` with no earlier run.

        The latest session before ``before`` with a clean run (no failures), because a clean run
        covered every window before it. With no clean run, the earliest ``since`` of any earlier
        run, so a filing that failed is still in the window.
        """


# ── one filing ───────────────────────────────────────────────────────────────────────────────────


def digest_messages(item: FilingInput) -> list[Message]:
    """The one user turn a digest call sends: the bounded filing as canonical JSON."""
    return [Message(Role.USER, json.dumps(item.document(), sort_keys=True, ensure_ascii=False))]


def digest_filing(
    item: FilingInput, *, llm: LLM, trading_date: date, clock: Clock, model: str
) -> FilingDigest:
    """One model call that turns ``item`` into a validated :class:`FilingDigest`.

    What it does: asks ``model`` through ``llm`` to fill :data:`DIGEST_TOOL`, validates the answer
    with :func:`parse_digest` and :func:`ground_digest` (every figure's number is in ``item``),
    and records the usage the response reports.
    What it never does: retry, drop a figure, or return a digest that failed validation. A
    response with no structured output, a truncated one, an invalid one or one with an
    ungrounded figure raises :class:`DigestRefusedError`.
    """
    messages = digest_messages(item)
    response: LLMResponse = llm.complete(
        messages,
        model=model,
        tools=(DIGEST_TOOL,),
        system=DIGEST_SYSTEM_PROMPT,
        max_tokens=DIGEST_MAX_TOKENS,
    )
    if response.truncated:
        raise DigestRefusedError("the model hit its output ceiling; the digest is incomplete")
    calls = [c for c in response.tool_calls if c.name == DIGEST_TOOL.name]
    if len(calls) != 1:
        raise DigestRefusedError(
            f"expected one {DIGEST_TOOL.name} output, got {len(calls)} (text: "
            f"{response.text[:80]!r})"
        )
    body = ground_digest(parse_digest(calls[0].arguments), item)
    return FilingDigest(
        filing_id=item.filing_id,
        kind=item.kind,
        isin=item.isin,
        knowable_date=item.knowable_date,
        trading_date=trading_date,
        digest_version=DIGEST_VERSION,
        input_digest=item.input_digest,
        input_truncated=item.truncated,
        prompt_digest=prompt_digest(
            messages, model=model, tools=(DIGEST_TOOL,), system=DIGEST_SYSTEM_PROMPT
        ),
        provider=response.provider,
        model=response.model,
        body=body,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_write_tokens=response.usage.cache_write_tokens,
        cache_read_tokens=response.usage.cache_read_tokens,
        digested_at=clock.now(),
    )


# ── one run ──────────────────────────────────────────────────────────────────────────────────────


def build_digests(
    session: date,
    *,
    isins: frozenset[str],
    source: DigestSource,
    llm: LLM,
    store: DigestStore,
    gate: GreenGate,
    clock: Clock,
    model: str,
) -> DigestRun:
    """Digest every new filing on ``isins`` (the session's digest scope) and record the run.

    What it does: refuses a red day or a future session (:class:`CommonsRefusedError`), finds the
    window (module docstring), reads both filing kinds through ``source`` and the PIT guard,
    serves each filing from ``store`` when it is already digested, and otherwise makes one
    model call, validates the answer and stores it. The run is recorded too.
    What it assumes: ``session`` is an NSE session, after its EOD. The caller commits.
    What it never does: digest a filing twice, store a refused digest, or let one filing's
    failure stop the others. Each failure is named in the run.
    """
    if session > clock.today():
        raise CommonsRefusedError(f"{session.isoformat()} is after today ({clock.today()})")
    verdict = gate(session)
    if not verdict:
        raise CommonsRefusedError(f"data is red for {session.isoformat()}: {verdict.reason}")
    pit = PitContext(as_of=session)
    gaps: list[Gap] = []
    try:
        calendar = sorted(set(pit.admit(source.sessions(session, DIGEST_LOOKBACK_SESSIONS + 1))))
    except SourceUnavailableError as exc:
        raise CommonsRefusedError(f"no session calendar: {exc}") from exc
    if not calendar or calendar[-1] != session:
        raise CommonsRefusedError(f"{session.isoformat()} is not an NSE session in the lake")
    floor = calendar[0]
    previous = calendar[-2] if len(calendar) > 1 else session
    resume = store.resume_after(session)
    since = previous if resume is None else resume
    if since < floor:
        gaps.append(
            Gap(
                source="digests",
                reason=f"the window would open after {since}, more than "
                f"{DIGEST_LOOKBACK_SESSIONS} sessions back; filings before {floor} are not "
                "digested",
            )
        )
        since = floor

    announcements = _read(
        lambda: source.announcement_texts(since, session), pit, gaps, "announcements"
    )
    results = _read(lambda: source.results_filings(since, session), pit, gaps, "results")
    inputs = filing_inputs(announcements, results, isins=isins, after=since, through=session)

    digested: list[str] = []
    cached: list[str] = []
    failures: list[DigestFailure] = []
    for item in inputs:
        if store.get(item.filing_id) is not None:
            cached.append(item.filing_id)
            continue
        try:
            digest = digest_filing(item, llm=llm, trading_date=session, clock=clock, model=model)
        except (DigestRefusedError, LLMError) as exc:
            _LOG.warning(
                "commons.digest.failed",
                trading_date=session.isoformat(),
                filing_id=item.filing_id,
                error=type(exc).__name__,
                reason=str(exc)[:300],
            )
            failures.append(DigestFailure(filing_id=item.filing_id, reason=str(exc)[:500]))
            continue
        store.record(digest, recorded_at=clock.now())
        digested.append(item.filing_id)
        _LOG.info(
            "commons.digest.recorded",
            trading_date=session.isoformat(),
            filing_id=item.filing_id,
            kind=item.kind.value,
            input_tokens=digest.input_tokens,
            output_tokens=digest.output_tokens,
        )

    ordered_gaps = tuple(sorted(gaps, key=lambda g: (g.source, g.reason)))
    filing_ids = tuple(item.filing_id for item in inputs)
    run = DigestRun(
        trading_date=session,
        since=since,
        filing_ids=filing_ids,
        digested=tuple(digested),
        cached=tuple(cached),
        failures=tuple(failures),
        gaps=ordered_gaps,
        run_digest=DigestRun.digest_of(
            trading_date=session,
            since=since,
            filing_ids=filing_ids,
            failures=failures,
            gaps=ordered_gaps,
        ),
    )
    store.record_run(run, recorded_at=clock.now())
    _LOG.info(
        "commons.digest.run",
        trading_date=session.isoformat(),
        since=since.isoformat(),
        filings=len(filing_ids),
        digested=len(digested),
        cached=len(cached),
        failures=len(failures),
        gaps=len(ordered_gaps),
    )
    return run


#: How far back an on-demand request looks: the dossier's announcement window (M17.9).
ON_DEMAND_LOOKBACK_SESSIONS: Final = 20


class OnDemandDigests(_Strict):
    """What :func:`digest_for_isins` returns: the names' digests in the window, and any failure.

    It is not a :class:`DigestRun` and is never recorded as one, so an on-demand request never
    moves the daily build's window.
    """

    trading_date: date
    isins: tuple[str, ...]
    since: date
    digests: tuple[FilingDigest, ...]
    digested: tuple[str, ...]
    cached: tuple[str, ...]
    failures: tuple[DigestFailure, ...]
    gaps: tuple[Gap, ...]


def digest_for_isins(
    isins: Iterable[str],
    session: date,
    *,
    source: DigestSource,
    llm: LLM,
    store: DigestStore,
    gate: GreenGate,
    clock: Clock,
    model: str,
    lookback_sessions: int = ON_DEMAND_LOOKBACK_SESSIONS,
) -> OnDemandDigests:
    """The digests of every filing on ``isins`` knowable in the last ``lookback_sessions``.

    What it does: the same refusals as :func:`build_digests` (a red day, a future session, no
    calendar), the same reads through ``source`` and the PIT guard, and the same cache: a filing
    already in ``store`` is served from it, any other costs one validated model call and is
    stored. M17.4 calls it for the names a manager asked to research.
    What it never does: record a :class:`DigestRun` (the daily window is the daily build's), store
    a refused digest, or let one filing's failure stop the others.
    """
    if lookback_sessions < 1:
        raise ValueError("lookback_sessions must be >= 1")
    if session > clock.today():
        raise CommonsRefusedError(f"{session.isoformat()} is after today ({clock.today()})")
    verdict = gate(session)
    if not verdict:
        raise CommonsRefusedError(f"data is red for {session.isoformat()}: {verdict.reason}")
    wanted = frozenset(isins)
    pit = PitContext(as_of=session)
    gaps: list[Gap] = []
    try:
        calendar = sorted(set(pit.admit(source.sessions(session, lookback_sessions + 1))))
    except SourceUnavailableError as exc:
        raise CommonsRefusedError(f"no session calendar: {exc}") from exc
    if not calendar or calendar[-1] != session:
        raise CommonsRefusedError(f"{session.isoformat()} is not an NSE session in the lake")
    since = calendar[0] if len(calendar) > 1 else session
    announcements = _read(
        lambda: source.announcement_texts(since, session), pit, gaps, "announcements"
    )
    results = _read(lambda: source.results_filings(since, session), pit, gaps, "results")
    inputs = filing_inputs(announcements, results, isins=wanted, after=since, through=session)

    digests: list[FilingDigest] = []
    digested: list[str] = []
    cached: list[str] = []
    failures: list[DigestFailure] = []
    for item in inputs:
        stored = store.get(item.filing_id)
        if stored is not None:
            digests.append(stored)
            cached.append(item.filing_id)
            continue
        try:
            digest = digest_filing(item, llm=llm, trading_date=session, clock=clock, model=model)
        except (DigestRefusedError, LLMError) as exc:
            _LOG.warning(
                "commons.digest.on_demand_failed",
                trading_date=session.isoformat(),
                filing_id=item.filing_id,
                error=type(exc).__name__,
                reason=str(exc)[:300],
            )
            failures.append(DigestFailure(filing_id=item.filing_id, reason=str(exc)[:500]))
            continue
        store.record(digest, recorded_at=clock.now())
        digests.append(digest)
        digested.append(item.filing_id)
    _LOG.info(
        "commons.digest.on_demand",
        trading_date=session.isoformat(),
        isins=len(wanted),
        filings=len(inputs),
        digested=len(digested),
        cached=len(cached),
        failures=len(failures),
    )
    return OnDemandDigests(
        trading_date=session,
        isins=tuple(sorted(wanted)),
        since=since,
        digests=tuple(digests),
        digested=tuple(digested),
        cached=tuple(cached),
        failures=tuple(failures),
        gaps=tuple(sorted(gaps, key=lambda g: (g.source, g.reason))),
    )


def _read[R](
    read: Callable[[], Dataset[R]], pit: PitContext, gaps: list[Gap], label: str
) -> tuple[R, ...]:
    try:
        return pit.admit(read())
    except SourceUnavailableError as exc:
        gaps.append(Gap(source=f"{label}:{exc.source}", reason=exc.reason))
        return ()
