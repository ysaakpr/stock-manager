"""The console's rendering primitives: template fill, escaping, masking, IST and durations.

The fill is a mirror of `analyst.cases.web._fill`, not an import of it: `dataplatform` may never
import `analyst` (System 1 does not depend on System 2), and moving that function down would edit a
ratification module for the sake of a status page. Both are a dozen lines; if a third caller
appears, the shared copy belongs here and `analyst` imports it.

Every dynamic value reaches a page through `esc` (structured values) or `free_text` (anything a
process or an operator wrote — an exception string, an alert title), and every instant through
`ist`, which is the one place a timestamp becomes text and always ends in ``IST``.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Final

from dataplatform.clock import IST
from dataplatform.redaction import mask_and_truncate

__all__ = [
    "MISSING",
    "age",
    "esc",
    "fill",
    "free_text",
    "human_bytes",
    "ist",
    "seconds",
]

_TEMPLATES: Final = Path(__file__).parent / "templates"

#: What an absent value renders as — never a zero, never an empty cell that reads as "fine".
MISSING: Final = "—"


@cache
def _template(name: str) -> str:
    """One template file from `templates/`, read once per process (they ship with the code)."""
    return (_TEMPLATES / name).read_text(encoding="utf-8")


def fill(name: str, fields: Mapping[str, str]) -> str:
    """Fill `{{token}}` placeholders in a template with already-prepared strings.

    What it assumes: every value is already escaped by its builder (`esc`/`free_text`).
    What it never does: escape, evaluate or loop — the pages are fixed and few, and a template
    engine would be a new dependency for no new capability.
    """
    text = _template(name)
    for key, value in fields.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def esc(value: object) -> str:
    """HTML-escape a structured value (a name, a state, a cron) for interpolation."""
    return html.escape(str(value))


def free_text(value: str | None, *, limit: int = 2000) -> str:
    """Free text as it may be shown: masked, bounded, then escaped (invariant #13).

    Applied even to fields the query layer already masked: the console is the last point before a
    credential would reach a browser, and masking twice costs nothing.
    """
    if value is None:
        return MISSING
    return html.escape(mask_and_truncate(value, limit))


def ist(moment: datetime | None) -> str:
    """An instant in Asia/Kolkata, labelled: ``2026-10-12 18:30:00 IST``."""
    if moment is None:
        return MISSING
    return moment.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def seconds(value: float | None) -> str:
    """A duration in seconds, at the precision an operator reads it."""
    if value is None:
        return MISSING
    if value < 0:
        return "-" + seconds(-value)
    if value < 120:
        return f"{value:.1f} s"
    return _span(value)


def _span(value: float) -> str:
    total = int(value)
    days, rest = divmod(total, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes = rest // 60
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"


def age(value: float | None) -> str:
    """How long ago, from an age in seconds; a negative age (a future stamp) says so."""
    if value is None:
        return MISSING
    if value < 0:
        return f"{seconds(-value)} in the future"
    return f"{seconds(value)} ago"


def human_bytes(count: int) -> str:
    """A byte count in binary units, one decimal."""
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")  # pragma: no cover
