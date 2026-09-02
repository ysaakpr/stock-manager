"""BSE corporate actions (§4.1 row 5) — `DefaultData/w` → `corporate_actions`.

The reconciliation counterpart to the NSE feed. BSE publishes the same universe of corporate
actions as a JSON array, but in its own dialect (the C.1 sweep on 2026-08-08 recorded the keys:
`scrip_code`, `short_name`, `long_name`, `Ex_date`, `exdate`, `Purpose`, `RD_Date`, `BCRD_FROM`,
`BCRD_TO`, `ND_START_DATE`, `ND_END_DATE`, `payment_date`). Two differences from NSE drive this
module's shape:

* **BSE keys on a scrip code, not an ISIN.** So every row must be resolved scrip→ISIN through the
  D2 identity master's BSE listing facts (`build_scrip_index`) — the only legitimate path, because
  invariant #2 forbids joining on any exchange's raw identifier. A scrip the master has never seen,
  or one that maps to two ISINs, lands in the result's `unresolved` rather than under a guess.

* **The ex-date is spelled two ways in one record** (`Ex_date` and `exdate`). Both are read; when
  both are present they must agree, and a disagreement is a broken record (`ParseError`) rather
  than a coin toss — the ex-date is the axis the whole factor chain hangs on.

BSE's feed carries no broadcast/announcement date, so unlike NSE there is no earlier-knowable
timestamp to store: `knowable_date` is the ingest date (from the injected clock), which is the
honest answer — we could not have known of the action before we fetched it, and for a historical
backfill that conservative date can only ever under-state knowability, never over-state it
(invariant #7). The action's type and terms come, exactly as for NSE, from the M2.1 normalizer
reading `Purpose`; an unclassifiable purpose goes to the manual-entry queue with its text intact.

Offline by construction: this module takes bytes, or an `L0Ref` it reads back through `L0Store`.
It never fetches — the crawl engine (`dataplatform.ingest.fetcher`) is the only thing that does.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Any, Final

from dataplatform.clock import Clock
from dataplatform.corpactions.parse_terms import ManualEntryQueue, parse_purpose
from dataplatform.ingest.corp_actions import (
    CaParseResult,
    CorporateAction,
    UnresolvedIdentity,
    month_from_name,
)
from dataplatform.ingest.models import ParseError
from dataplatform.logging import get_logger
from dataplatform.store.l0 import L0Ref, L0Store

__all__ = [
    "SOURCE_ID",
    "parse",
    "parse_l0",
]

_LOG = get_logger(__name__)

#: The register id this parser serves (`source_register.yaml`, `parser.task: M2.2`).
SOURCE_ID: Final = "bse_corp_actions"

#: The purpose string BSE publishes. The action-type/terms surface is derived from this one field.
_PURPOSE_KEY: Final = "Purpose"

#: The two spellings of the ex-date in one record; both are read and must not disagree.
_EX_DATE_KEYS: Final = ("Ex_date", "exdate")

_EMPTY_MARKERS: Final = frozenset({"", "-", "--", "n/a", "na", "null", "none"})

#: The date shapes BSE has been seen to use: an ISO date or datetime (`2024-10-28T00:00:00`), a
#: spelled-out `28 Oct 2024`, a hyphenated `28-Oct-2024`, and a numeric `28/10/2024`.
_ISO_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")
_SPELLED_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(\d{1,2})[ -]([A-Za-z]{3})[ -](\d{4})")
_NUMERIC_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})")


def parse(
    payload: bytes,
    *,
    filename: str,
    scrip_index: Mapping[str, str],
    clock: Clock,
    l0_key: str | None = None,
) -> CaParseResult:
    """Parse one `DefaultData/w` response into normalized corporate actions.

    Assumes `payload` is one whole response. `filename` names the file in errors and logs.
    `scrip_index` is `build_scrip_index(master)` — the scrip→ISIN map every row resolves through;
    `clock` supplies the knowable-date, since BSE publishes no broadcast timestamp.

    Raises `ParseError`, naming the file, for a payload that is not this format: an HTML soft-404, a
    non-array body, a record without a scrip code or ex-date, or a record whose two ex-date fields
    disagree. A record that parses but cannot be *classified* (unknown purpose) or *resolved*
    (scrip the master does not know) is returned in `queued` / `unresolved`, not raised.
    """
    records = _records(_decode(payload, filename=filename), filename=filename)
    queue = ManualEntryQueue()
    actions: list[CorporateAction] = []
    unresolved: list[UnresolvedIdentity] = []

    for index, record in enumerate(records):
        _one(
            record,
            index=index,
            filename=filename,
            scrip_index=scrip_index,
            clock=clock,
            l0_key=l0_key,
            queue=queue,
            actions=actions,
            unresolved=unresolved,
        )

    result = CaParseResult(
        actions=tuple(actions),
        queued=queue.entries,
        unresolved=tuple(unresolved),
    )
    _LOG.info(
        "bse_corp_actions.parsed",
        source=SOURCE_ID,
        filename=filename,
        records=len(records),
        actions=len(result.actions),
        queued=len(result.queued),
        unresolved=len(result.unresolved),
        state="VALIDATED",
    )
    return result


def parse_l0(
    store: L0Store, ref: L0Ref, *, scrip_index: Mapping[str, str], clock: Clock
) -> CaParseResult:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in.

    The pipeline's entry point: `L0Store.get` re-hashes the payload so every row derives from bytes
    that have not changed (invariant #1), and the ref's key is threaded onto every row.
    """
    return parse(
        store.get(ref),
        filename=ref.filename,
        scrip_index=scrip_index,
        clock=clock,
        l0_key=ref.key,
    )


# ── one record ───────────────────────────────────────────────────────────────────────────────


def _one(
    record: Mapping[str, Any],
    *,
    index: int,
    filename: str,
    scrip_index: Mapping[str, str],
    clock: Clock,
    l0_key: str | None,
    queue: ManualEntryQueue,
    actions: list[CorporateAction],
    unresolved: list[UnresolvedIdentity],
) -> None:
    """Turn one feed record into an action, a queue entry, or an unresolved-identity note."""
    purpose = _text(record, _PURPOSE_KEY, index=index, filename=filename, required=True)
    scrip_code = _scrip_code(record, index=index, filename=filename)
    ex_date = _ex_date(record, index=index, filename=filename)
    record_date = _date(record, "RD_Date", index=index, filename=filename)

    outcome = parse_purpose(purpose, source=SOURCE_ID)
    if outcome.queue_entry is not None:
        queue.add(outcome.queue_entry)
    if outcome.action is None:
        return

    isin = scrip_index.get(scrip_code)
    if isin is None:
        unresolved.append(
            UnresolvedIdentity(
                source=SOURCE_ID,
                source_ref=scrip_code,
                ex_date=ex_date,
                raw_text=purpose,
                reason=(
                    f"BSE scrip code {scrip_code!r} is not in the identity master's BSE listings "
                    "(unknown scrip, or a scrip that maps to more than one ISIN)"
                ),
            )
        )
        _LOG.warning(
            "bse_corp_actions.unresolved",
            source=SOURCE_ID,
            scrip_code=scrip_code,
            ex_date=ex_date.isoformat(),
        )
        return

    actions.append(
        CorporateAction.from_parsed(
            outcome.action,
            isin=isin,
            source=SOURCE_ID,
            ex_date=ex_date,
            knowable_date=clock.now().date(),
            record_date=record_date,
            announcement_date=None,
            source_ref=scrip_code,
            l0_key=l0_key,
        )
    )


# ── payload plumbing ───────────────────────────────────────────────────────────────────────────


def _decode(payload: bytes, *, filename: str) -> str:
    """UTF-8 the body, refusing an empty one and an HTML page wearing a 200 (soft-404)."""
    if not payload.strip():
        raise ParseError("empty response body", filename=filename)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(f"body is not UTF-8: {exc}", filename=filename) from exc
    if text.lstrip()[:1] == "<":
        raise ParseError(
            "body is markup, not JSON — an HTML error page answered with a 200 "
            "(ops/gates/source-verification.md §5.1); it must not become corporate-action rows",
            filename=filename,
        )
    return text


def _records(text: str, *, filename: str) -> list[Mapping[str, Any]]:
    """The JSON array of action records, kept out of `float` on the way in.

    BSE returns a bare array here and a `{Table: [...]}` envelope on some sibling endpoints; both
    are accepted, anything else is a format change to stop on.
    """
    try:
        document = json.loads(text, parse_float=str, parse_int=str)
    except json.JSONDecodeError as exc:
        raise ParseError(f"body is not valid JSON: {exc}", filename=filename) from exc

    if isinstance(document, Mapping):
        for key in ("Table", "data"):
            if key in document:
                document = document[key]
                break
    if not isinstance(document, list):
        raise ParseError(
            f"expected a JSON array of action records, got {type(document).__name__}",
            filename=filename,
        )
    for index, record in enumerate(document):
        if not isinstance(record, Mapping):
            raise ParseError(
                f"record {index} is {type(record).__name__}, not an object", filename=filename
            )
    return list(document)


def _scrip_code(record: Mapping[str, Any], *, index: int, filename: str) -> str:
    """The scrip code as a clean string, however the feed typed it (BSE has used both str and int).

    A trailing `.0` is stripped: a scrip code is an integer identifier, and a JSON number that
    arrived as `500325.0` must key the scrip index identically to the string `"500325"`.
    """
    value = record.get("scrip_code")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ParseError(
            f"record {index}: no non-empty 'scrip_code' field; present: "
            f"{', '.join(sorted(map(str, record)))}",
            filename=filename,
        )
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _ex_date(record: Mapping[str, Any], *, index: int, filename: str) -> date:
    """The ex-date, read from both spellings and required to agree when both are present."""
    seen: dict[str, date] = {}
    for key in _EX_DATE_KEYS:
        parsed = _date(record, key, index=index, filename=filename)
        if parsed is not None:
            seen[key] = parsed
    if not seen:
        raise ParseError(
            f"record {index}: no ex-date in any of {', '.join(_EX_DATE_KEYS)}; a corporate action "
            "must have one",
            filename=filename,
        )
    distinct = set(seen.values())
    if len(distinct) > 1:
        pairs = ", ".join(f"{key}={value.isoformat()}" for key, value in seen.items())
        raise ParseError(
            f"record {index}: ex-date fields disagree ({pairs}); the ex-date is the factor "
            "chain's axis and cannot be guessed",
            filename=filename,
        )
    return next(iter(distinct))


def _text(
    record: Mapping[str, Any], key: str, *, index: int, filename: str, required: bool = False
) -> str:
    """A string field. Raises for a missing *required* field; returns '' for an absent optional."""
    value = record.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ParseError(
                f"record {index}: no non-empty {key!r} field; present: "
                f"{', '.join(sorted(map(str, record)))}",
                filename=filename,
            )
        return ""
    return str(value).strip()


def _date(record: Mapping[str, Any], key: str, *, index: int, filename: str) -> date | None:
    """Parse a BSE date field, tolerating its several shapes and the empty markers."""
    raw = record.get(key)
    text = "" if raw is None else str(raw).strip()
    if text.lower() in _EMPTY_MARKERS:
        return None
    parsed = _parse_date(text)
    if parsed is None:
        raise ParseError(
            f"record {index}: {key!r} is {raw!r}, not a recognised BSE date", filename=filename
        )
    return parsed


def _parse_date(text: str) -> date | None:
    """One BSE date string → `date`, across its ISO / spelled / numeric shapes, else `None`."""
    iso = _ISO_RE.match(text)
    if iso is not None:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    spelled = _SPELLED_RE.match(text)
    if spelled is not None:
        month = month_from_name(spelled.group(2))
        if month is None:
            return None
        try:
            return date(int(spelled.group(3)), month, int(spelled.group(1)))
        except ValueError:
            return None
    numeric = _NUMERIC_RE.match(text)
    if numeric is not None:
        try:
            return date(int(numeric.group(3)), int(numeric.group(2)), int(numeric.group(1)))
        except ValueError:
            return None
    return None
