"""A10 · M17.9 — the frozen keyword tables that classify an announcement's subject line.

Two tables live in ``event_keywords.yaml`` next to this module:

- **integrity events** (pre-registration §8 Amendment 1 (b)): auditor, CFO or independent-director
  resignation, a SEBI order, a rating downgrade to sub-investment-grade or "issuer not
  cooperating", and a default. Each one excludes its ISIN from the universe for 60 sessions
  (`analyst.commons.exclusions`);
- **event watch** (study §2, screen S5): buyback, bonus, split, order win, index change and a
  results board-meeting intimation. These are listed as facts and never ranked.

The YAML's header states how a pattern is matched: on NSE's subject line, which L1 carries as
``subject`` (NSE's ``desc``) and ``body`` (NSE's ``attchmntText``). ``category`` is always null in
this lake, so it is never read. The attached PDF is never read either.

**Frozen.** :data:`EVENT_KEYWORDS_DIGEST` is the sha256 of the YAML's bytes. It is part of the
screens rule hash (`analyst.commons.screens.SCREENS_RULE_HASH`), so an edited keyword is a new
rule. :func:`load_event_keywords` refuses a file with an unknown category, an unknown key or a
pattern that does not compile.

What it never does: read a wall clock, fetch, or guess a category from a symbol.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

import yaml

__all__ = [
    "EVENT_KEYWORDS_DIGEST",
    "EVENT_KEYWORDS_PATH",
    "EVENT_WATCH_CATEGORIES",
    "INTEGRITY_CATEGORIES",
    "Classification",
    "KeywordRule",
    "KeywordTable",
    "classify",
    "load_event_keywords",
    "meeting_date",
    "subject_line",
]

EVENT_KEYWORDS_PATH: Final = Path(__file__).with_name("event_keywords.yaml")
_VERSION: Final = "commons-event-keywords/1"

#: The categories each table must define, exactly. A table that adds or drops one is refused.
INTEGRITY_CATEGORIES: Final[tuple[str, ...]] = (
    "auditor_resignation",
    "cfo_resignation",
    "independent_director_resignation",
    "sebi_order",
    "rating_downgrade_sub_ig",
    "default",
)
EVENT_WATCH_CATEGORIES: Final[tuple[str, ...]] = (
    "buyback",
    "bonus",
    "split",
    "order_win",
    "index_change",
    "results_board_meeting",
)
_RULE_KEYS: Final = frozenset({"subject", "text", "unless"})
_FLAGS: Final = re.IGNORECASE


@dataclass(frozen=True, slots=True)
class KeywordRule:
    """One rule: an optional subject pattern, text patterns that must all match, and a veto."""

    subject: re.Pattern[str] | None
    text: tuple[re.Pattern[str], ...]
    unless: re.Pattern[str] | None

    def matches(self, subject: str, text: str) -> bool:
        if self.subject is not None and not self.subject.search(subject):
            return False
        if not all(p.search(text) for p in self.text):
            return False
        return self.unless is None or not self.unless.search(text)


@dataclass(frozen=True, slots=True)
class KeywordTable:
    """Both frozen tables, and the digest of the bytes they were read from."""

    version: str
    integrity: Mapping[str, tuple[KeywordRule, ...]]
    event_watch: Mapping[str, tuple[KeywordRule, ...]]
    digest: str


@dataclass(frozen=True, slots=True)
class Classification:
    """The categories one announcement matched in each table, sorted."""

    integrity: tuple[str, ...]
    event_watch: tuple[str, ...]


def _compile(pattern: object, where: str) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{where}: a pattern must be a non-empty string")
    try:
        return re.compile(pattern, _FLAGS)
    except re.error as exc:
        raise ValueError(f"{where}: {pattern!r} does not compile: {exc}") from exc


def _rules(raw: object, where: str) -> tuple[KeywordRule, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{where}: a category is a non-empty list of rules")
    out: list[KeywordRule] = []
    for k, item in enumerate(raw):
        here = f"{where}[{k}]"
        if not isinstance(item, dict) or not item:
            raise ValueError(f"{here}: a rule is a mapping")
        if unknown := set(item) - _RULE_KEYS:
            raise ValueError(f"{here}: unknown keys {sorted(unknown)}")
        texts = item.get("text", [])
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, list):
            raise ValueError(f"{here}: text is a pattern or a list of patterns")
        if "subject" not in item and not texts:
            raise ValueError(f"{here}: a rule needs a subject or a text pattern")
        out.append(
            KeywordRule(
                subject=_compile(item["subject"], here) if "subject" in item else None,
                text=tuple(_compile(t, here) for t in texts),
                unless=_compile(item["unless"], here) if "unless" in item else None,
            )
        )
    return tuple(out)


def _table(raw: object, expected: Sequence[str], name: str) -> dict[str, tuple[KeywordRule, ...]]:
    if not isinstance(raw, dict):
        raise ValueError(f"{name}: not a mapping of categories")
    if set(raw) != set(expected):
        raise ValueError(f"{name}: categories {sorted(raw)} are not exactly {sorted(expected)}")
    return {category: _rules(raw[category], f"{name}.{category}") for category in expected}


def load_event_keywords(path: Path = EVENT_KEYWORDS_PATH) -> KeywordTable:
    """Read and validate the frozen keyword tables at ``path``.

    What it does: parses the YAML, checks the version and that each table defines exactly its
    categories, compiles every pattern, and digests the file's bytes.
    What it never does: accept an unknown key or category, or a pattern that does not compile.
    """
    payload = path.read_bytes()
    document: Any = yaml.safe_load(payload)
    if not isinstance(document, dict) or set(document) != {"version", "integrity", "event_watch"}:
        raise ValueError(f"{path.name}: expected exactly version, integrity and event_watch")
    if document["version"] != _VERSION:
        raise ValueError(f"{path.name}: version {document['version']!r} is not {_VERSION!r}")
    return KeywordTable(
        version=_VERSION,
        integrity=_table(document["integrity"], INTEGRITY_CATEGORIES, "integrity"),
        event_watch=_table(document["event_watch"], EVENT_WATCH_CATEGORIES, "event_watch"),
        digest=hashlib.sha256(payload).hexdigest(),
    )


def subject_line(subject: str, body: str | None) -> str:
    """What a ``text`` pattern is matched against: ``"<subject> | <body>"``."""
    return f"{subject} | {body}" if body else subject


def classify(subject: str, body: str | None, table: KeywordTable) -> Classification:
    """The integrity and event-watch categories one announcement's subject line matches."""
    text = subject_line(subject, body)

    def matched(rules: Mapping[str, tuple[KeywordRule, ...]]) -> tuple[str, ...]:
        return tuple(
            sorted(c for c, rs in rules.items() if any(r.matches(subject, text) for r in rs))
        )

    return Classification(
        integrity=matched(table.integrity), event_watch=matched(table.event_watch)
    )


_MONTHS: Final = {
    m: i
    for i, names in enumerate(
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
    for m in names
}
_MONTH: Final = r"(?P<mon>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")"
_DATE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # 10-Oct-2026, 10 Oct 2026, 10th October, 2026
    re.compile(
        r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)?[\s\-/]+" + _MONTH + r"\.?,?[\s\-/]+(?P<y>\d{4})\b",
        _FLAGS,
    ),
    # October 10, 2026
    re.compile(r"\b" + _MONTH + r"\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})\b", _FLAGS),
    # 10/10/2026, 10-10-2026, 10.10.2026 (day first, as Indian filings write it)
    re.compile(r"\b(?P<d>\d{1,2})[/\-.](?P<m>\d{1,2})[/\-.](?P<y>\d{4})\b"),
)
_HELD_ON: Final = re.compile(r"\b(held|scheduled|convened|meeting)\b[^|]{0,40}?\bon\b", _FLAGS)


def meeting_date(text: str) -> date | None:
    """The board-meeting date a results intimation names, or ``None`` when it names none.

    The first date written after "held on" / "scheduled on" / "meeting ... on". A text with no such
    phrase, or with a date that does not exist, gives ``None``: an undated intimation is listed
    without a date, never with a guessed one.
    """
    anchor = _HELD_ON.search(text)
    if anchor is None:
        return None
    tail = text[anchor.end() : anchor.end() + 80]
    found: list[tuple[int, date]] = []
    for pattern in _DATE_PATTERNS:
        match = pattern.search(tail)
        if match is None:
            continue
        groups = match.groupdict()
        month = _MONTHS[groups["mon"].lower()] if groups.get("mon") else int(groups["m"])
        try:
            found.append((match.start(), date(int(groups["y"]), month, int(groups["d"]))))
        except ValueError:
            continue
    return min(found)[1] if found else None


#: The sha256 of the frozen keyword file, struck at import.
EVENT_KEYWORDS_DIGEST: Final = load_event_keywords().digest
