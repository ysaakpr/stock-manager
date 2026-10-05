"""Curated, sourced merger and cash-exit terms — what the corporate-action store cannot say.

Every ``MERGER`` row in ``corporate_actions`` is ``UnquantifiedTerms``: neither exchange's
corporate-action feed carries the exchange ratio or the transferee, so a backtest holding in an
amalgamated company used to sit at its last printed close forever. The terms *are* published —
in the exchange's announcement text (the NSE PR bundle's ``An*.txt``, already in L0) or in the
record-date letter an announcement links to — but as prose, one scheme at a time. They are
transcribed by hand into ``merger_terms.yaml`` beside this module, each number next to the
quoted sentence that states it and the L0 key that holds it, and read back here.

**Why a reviewed file and not rows in ``corporate_actions``.** The table's rows are produced by
parsers from L0 and reconciled across two exchanges; a hand transcription is neither, and putting
it there would make a reconciled-looking row nobody's parser can re-derive (invariant #1). The
table also has no column for a transferee ISIN. A YAML file in the repo is reviewed in the diff,
re-derivable from the L0 objects it names, and changes the run specification when it changes.

**PIT.** Each term carries ``knowable_date`` — the dissemination date of the earliest L0 source
stating it, never the day it was transcribed. Nothing on the decision path reads this module: the
backtest *book* applies a conversion on its record date as mechanical accounting, exactly as it
applies a split (``backtest.book_actions``), and a decision only ever sees the account it leaves.

What this module never does: fetch, infer a ratio, or fill a missing term. A scheme whose terms
no source states is listed under ``unsourced`` with the reason, and stays unapplied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

import yaml

from dataplatform.ingest.models import ISIN_PATTERN

__all__ = [
    "MERGER_TERMS_PATH",
    "CashExitTerm",
    "MergerTerms",
    "MergerTermsError",
    "ShareSwapTerm",
    "TermSource",
    "UnsourcedMerger",
    "load_merger_terms",
]

MERGER_TERMS_PATH: Final[Path] = Path(__file__).with_name("merger_terms.yaml")
_ISIN: Final = re.compile(ISIN_PATTERN)


class MergerTermsError(ValueError):
    """The curated terms file is malformed — a missing source, a bad ISIN, a non-positive ratio."""


@dataclass(frozen=True, slots=True)
class TermSource:
    """Where a term is stated: an L0 object, its location inside it, and the quoted sentence."""

    l0_key: str
    quote: str
    member: str | None = None
    line: int | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ShareSwapTerm:
    """``shares_received`` of ``surviving_isin`` for every ``shares_held`` of ``old_isin``.

    The direction is the scheme's own: old → new. ``record_date`` fixes the entitlement;
    ``knowable_date`` is when the earliest L0 source stating the ratio was disseminated.
    """

    old_isin: str
    old_company: str
    surviving_isin: str
    surviving_company: str
    shares_received: Decimal
    shares_held: Decimal
    record_date: date
    knowable_date: date
    sources: tuple[TermSource, ...]
    note: str | None = None


@dataclass(frozen=True, slots=True)
class CashExitTerm:
    """A delisting: from ``effective_date`` every share of ``old_isin`` is worth ``exit_price``."""

    old_isin: str
    old_company: str
    exit_price: Decimal
    effective_date: date
    knowable_date: date
    sources: tuple[TermSource, ...]
    note: str | None = None


@dataclass(frozen=True, slots=True)
class UnsourcedMerger:
    """A scheme that left a held name with no successor and whose terms no source states."""

    old_isin: str
    old_company: str
    kind: str
    reason: str
    record_date: date | None = None


@dataclass(frozen=True, slots=True)
class MergerTerms:
    """The whole curated file: applied swaps and exits, and the schemes that stay unapplied."""

    share_swaps: tuple[ShareSwapTerm, ...]
    cash_exits: tuple[CashExitTerm, ...]
    unsourced: tuple[UnsourcedMerger, ...]


def load_merger_terms(path: Path = MERGER_TERMS_PATH) -> MergerTerms:
    """Read and validate the curated terms file.

    Raises ``MergerTermsError`` on any row without a source, with a malformed ISIN, a non-positive
    ratio or price, a knowable date that is missing, or an ISIN listed twice — a duplicate would
    convert one holding two ways.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise MergerTermsError(f"{path}: expected a mapping at the top level")
    swaps = tuple(_swap(row) for row in document.get("share_swaps") or ())
    exits = tuple(_exit(row) for row in document.get("cash_exits") or ())
    unsourced = tuple(_unsourced(row) for row in document.get("unsourced") or ())
    seen: set[str] = set()
    for isin in (
        [t.old_isin for t in swaps] + [t.old_isin for t in exits] + [u.old_isin for u in unsourced]
    ):
        if isin in seen:
            raise MergerTermsError(f"{isin} appears more than once in {path.name}")
        seen.add(isin)
    return MergerTerms(share_swaps=swaps, cash_exits=exits, unsourced=unsourced)


# ── row parsing ──────────────────────────────────────────────────────────────────────────────────


def _swap(row: dict[str, Any]) -> ShareSwapTerm:
    old = _isin(row, "old_isin")
    surviving = _isin(row, "surviving_isin")
    if old == surviving:
        raise MergerTermsError(f"{old}: a share swap's surviving ISIN must differ from the old")
    return ShareSwapTerm(
        old_isin=old,
        old_company=_text(row, "old_company"),
        surviving_isin=surviving,
        surviving_company=_text(row, "surviving_company"),
        shares_received=_positive(row, "shares_received"),
        shares_held=_positive(row, "shares_held"),
        record_date=_date(row, "record_date"),
        knowable_date=_date(row, "knowable_date"),
        sources=_sources(row),
        note=row.get("note"),
    )


def _exit(row: dict[str, Any]) -> CashExitTerm:
    return CashExitTerm(
        old_isin=_isin(row, "old_isin"),
        old_company=_text(row, "old_company"),
        exit_price=_positive(row, "exit_price_inr"),
        effective_date=_date(row, "effective_date"),
        knowable_date=_date(row, "knowable_date"),
        sources=_sources(row),
        note=row.get("note"),
    )


def _unsourced(row: dict[str, Any]) -> UnsourcedMerger:
    record = row.get("record_date")
    return UnsourcedMerger(
        old_isin=_isin(row, "old_isin"),
        old_company=_text(row, "old_company"),
        kind=_text(row, "kind"),
        reason=_text(row, "reason"),
        record_date=None if record is None else _date(row, "record_date"),
    )


def _sources(row: dict[str, Any]) -> tuple[TermSource, ...]:
    raw = row.get("sources")
    if not raw:
        raise MergerTermsError(f"{row.get('old_isin')}: a sourced term needs at least one source")
    out: list[TermSource] = []
    for entry in raw:
        key, quote = entry.get("l0_key"), entry.get("quote")
        if not key or not quote:
            raise MergerTermsError(f"{row.get('old_isin')}: every source needs l0_key and quote")
        line = entry.get("line")
        out.append(
            TermSource(
                l0_key=str(key),
                quote=" ".join(str(quote).split()),
                member=entry.get("member"),
                line=None if line is None else int(line),
                url=entry.get("url"),
            )
        )
    return tuple(out)


def _isin(row: dict[str, Any], key: str) -> str:
    value = str(row.get(key) or "")
    if not _ISIN.fullmatch(value):
        raise MergerTermsError(f"{key} {value!r} is not an ISIN")
    return value


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not value:
        raise MergerTermsError(f"{row.get('old_isin')}: {key} is required")
    return str(value)


def _date(row: dict[str, Any], key: str) -> date:
    value = row.get(key)
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise MergerTermsError(f"{row.get('old_isin')}: {key} {value!r}: {exc}") from exc
    raise MergerTermsError(f"{row.get('old_isin')}: {key} is required")


def _positive(row: dict[str, Any], key: str) -> Decimal:
    value = row.get(key)
    if isinstance(value, float):
        raise MergerTermsError(f"{row.get('old_isin')}: {key} must be a string or int, not float")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MergerTermsError(f"{row.get('old_isin')}: {key} {value!r} is not a number") from exc
    if not number.is_finite() or number <= 0:
        raise MergerTermsError(f"{row.get('old_isin')}: {key} must be positive, got {value!r}")
    return number
