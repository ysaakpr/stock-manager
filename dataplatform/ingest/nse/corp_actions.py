"""NSE equity corporate actions (§4.1 row 5) — `corporates-corporateActions` → `corporate_actions`.

NSE serves corporate actions as a JSON array, one record per action, keyed **natively on ISIN**
(the C.1 sweep on 2026-08-08 confirmed the keys: `isin`, `symbol`, `series`, `exDate`, `recDate`,
`caBroadcastDate`, `bcStartDate`, `bcEndDate`, `ndStartDate`, `ndEndDate`, `subject`, `faceVal`).
That the ISIN is native is the good case for invariant #2 — there is no symbol to accidentally
join on — but the row is still validated through the D2 identity master rather than trusted blind:
the native ISIN must be one the master knows, and where the symbol resolves on the ex-date it must
resolve to that same ISIN. A disagreement is an identity defect, not a row to file anyway.

The action's *type and terms* come from the M2.1 normalizer (`parse_purpose`), which reads the
`subject` string and refuses to guess: a subject it cannot classify, or one whose factor-bearing
terms are absent, lands in the manual-entry queue with its raw text intact rather than becoming a
confidently wrong split. The action's *identity and timing* — ISIN, ex-date, record date, and the
broadcast date that makes it knowable (invariant #7) — come from the fields around the subject.

`caBroadcastDate` is the first date the action was knowable to us: a factor may not be visible to a
decision before its broadcast, even though the ex-date it applies from is later still. It is stored
as both `announcement_date` and `knowable_date`; when a record carries none, the ingest date stands
in, which can only ever make an action *less* knowable, never more.

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
from dataplatform.identity.lineage import LineageResolver
from dataplatform.identity.master import (
    AmbiguousSymbolError,
    Exchange,
    IdentityMaster,
)
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
SOURCE_ID: Final = "nse_corp_actions"

#: The purpose string. The whole action-type/terms surface is derived from this one field.
_SUBJECT_KEY: Final = "subject"

#: Values that mean "no date" across the feed: an empty cell, or NSE's dash placeholder.
_EMPTY_MARKERS: Final = frozenset({"", "-", "--", "n/a", "na", "null", "none"})

#: `DD-Mon-YYYY`, optionally trailed by a time (`caBroadcastDate` carries one). Anchored at the
#: start so the date is read and the time ignored, locale-independently.
_DMY_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})")

#: `YYYY-MM-DD`, the other shape NSE has been seen to use for some date fields.
_YMD_RE: Final[re.Pattern[str]] = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


def parse(
    payload: bytes,
    *,
    filename: str,
    master: IdentityMaster,
    clock: Clock,
    l0_key: str | None = None,
    lineage: LineageResolver | None = None,
) -> CaParseResult:
    """Parse one `corporates-corporateActions` response into normalized corporate actions.

    Assumes `payload` is one whole response — a few kilobytes of JSON, so holding it whole is free
    and a truncation is detectable rather than streamed past. `filename` names the file in errors
    and logs. `master` is the D2 identity master every ISIN is validated against; `clock` supplies
    the knowable-date fallback for a record that carries no broadcast date.

    Raises `ParseError`, naming the file, for a payload that is not this format at all: an HTML
    soft-404, a non-array body, a record missing its ISIN or ex-date. A record that parses but
    cannot be *classified* (an unknown subject) or *resolved* (an ISIN the master rejects) is not
    an error — it is returned in the result's `queued` or `unresolved`, so one bad row never costs
    the rest of the file.
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
            master=master,
            clock=clock,
            l0_key=l0_key,
            lineage=lineage,
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
        "nse_corp_actions.parsed",
        source=SOURCE_ID,
        filename=filename,
        records=len(records),
        actions=len(result.actions),
        queued=len(result.queued),
        unresolved=len(result.unresolved),
        state="VALIDATED",
    )
    return result


def parse_l0(store: L0Store, ref: L0Ref, *, master: IdentityMaster, clock: Clock) -> CaParseResult:
    """Parse the L0 payload a fetch produced, re-verifying its checksum on the way in.

    The pipeline's entry point: `Fetcher.fetch` returns an `L0Ref` and never bytes, so this is how
    a fetched response becomes rows, with `L0Store.get` re-hashing the payload so every row derives
    from bytes that have not changed (invariant #1). The ref's key is threaded onto every row.
    """
    return parse(store.get(ref), filename=ref.filename, master=master, clock=clock, l0_key=ref.key)


# ── one record ───────────────────────────────────────────────────────────────────────────────


def _one(
    record: Mapping[str, Any],
    *,
    index: int,
    filename: str,
    master: IdentityMaster,
    clock: Clock,
    l0_key: str | None,
    lineage: LineageResolver | None,
    queue: ManualEntryQueue,
    actions: list[CorporateAction],
    unresolved: list[UnresolvedIdentity],
) -> None:
    """Turn one feed record into an action, a queue entry, or an unresolved-identity note."""
    subject = _text(record, _SUBJECT_KEY, index=index, filename=filename, required=True)
    symbol = _text(record, "symbol", index=index, filename=filename, required=True)
    native_isin = _text(record, "isin", index=index, filename=filename, required=True)
    ex_date = _date(record, "exDate", index=index, filename=filename, required=True)
    assert ex_date is not None  # required=True guarantees it; narrows the type for mypy

    record_date = _date(record, "recDate", index=index, filename=filename)
    broadcast = _date(record, "caBroadcastDate", index=index, filename=filename)
    knowable = broadcast if broadcast is not None else clock.now().date()

    # Type and terms first: a subject we cannot classify never reaches identity resolution, because
    # there would be nothing to store even if the ISIN were perfect.
    outcome = parse_purpose(subject, source=SOURCE_ID)
    if outcome.queue_entry is not None:
        queue.add(outcome.queue_entry)
    if outcome.action is None:
        return

    resolution = _resolve_isin(
        native_isin,
        symbol=symbol,
        ex_date=ex_date,
        master=master,
        lineage=lineage,
    )
    if resolution is None:
        unresolved.append(
            UnresolvedIdentity(
                source=SOURCE_ID,
                source_ref=f"{symbol}:{native_isin}",
                ex_date=ex_date,
                raw_text=subject,
                reason=(
                    f"native ISIN {native_isin!r} is unknown to the identity master, or symbol "
                    f"{symbol!r} resolves to a different security on {ex_date.isoformat()}"
                ),
            )
        )
        _LOG.warning(
            "nse_corp_actions.unresolved",
            source=SOURCE_ID,
            symbol=symbol,
            native_isin=native_isin,
            ex_date=ex_date.isoformat(),
        )
        return

    isin, filed_against = resolution
    actions.append(
        CorporateAction.from_parsed(
            outcome.action,
            isin=isin,
            filed_against_isin=filed_against,
            source=SOURCE_ID,
            ex_date=ex_date,
            knowable_date=knowable,
            record_date=record_date,
            announcement_date=broadcast,
            source_ref=f"{symbol}:{record.get('series', '')}".rstrip(":"),
            l0_key=l0_key,
        )
    )


def _resolve_isin(
    native_isin: str,
    *,
    symbol: str,
    ex_date: date,
    master: IdentityMaster,
    lineage: LineageResolver | None = None,
) -> tuple[str, str | None] | None:
    """Validate the feed's native ISIN through D2; return `(isin, filed_against)` or `None`.

    Native ISIN is the join key here, but "native" is not "trusted": it must be a security the
    master knows, and where the symbol resolves on the ex-date it must resolve to the *same* ISIN.
    A symbol that resolves elsewhere is an identity conflict — the master queues it — and this row
    is held back rather than filed against a contested identity.

    An ISIN the master does not know gets one second chance, through `lineage`. A face-value split
    retires the ISIN it is filed against, so the row naming a retired ISIN is the *normal* shape
    of a split, not a broken row: 290 of 445 reissues carry their action this way. When the
    lineage resolves it to a survivor the master does know, the action is filed against the
    survivor and `filed_against` carries the retired ISIN for provenance. Without a lineage — or
    when the survivor is itself unknown — the row is still held back.
    """
    isin, filed_against = native_isin, None
    if native_isin not in master.securities:
        if lineage is None:
            return None
        survivor = lineage.survivor_of(native_isin)
        if survivor == native_isin or survivor not in master.securities:
            return None
        isin, filed_against = survivor, native_isin
    try:
        resolved = master.try_resolve(symbol, ex_date, exchange=Exchange.NSE)
    except AmbiguousSymbolError:
        # The master has already queued the ambiguity; we simply do not file this row against it.
        return None
    if resolved is not None and resolved != isin:
        return None
    return isin, filed_against


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

    NSE nests the array under a `data` key in some responses and returns a bare array in others;
    both are accepted, and anything else is a format change to stop on rather than guess through.
    """
    try:
        document = json.loads(text, parse_float=str, parse_int=str)
    except json.JSONDecodeError as exc:
        raise ParseError(f"body is not valid JSON: {exc}", filename=filename) from exc

    if isinstance(document, Mapping) and "data" in document:
        document = document["data"]
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


def _date(
    record: Mapping[str, Any], key: str, *, index: int, filename: str, required: bool = False
) -> date | None:
    """Parse a feed date field, tolerating the empty markers and both date shapes NSE uses."""
    raw = record.get(key)
    text = "" if raw is None else str(raw).strip()
    if text.lower() in _EMPTY_MARKERS:
        if required:
            raise ParseError(
                f"record {index}: {key!r} is empty but an ex-date is mandatory", filename=filename
            )
        return None
    parsed = _parse_date(text)
    if parsed is None:
        raise ParseError(
            f"record {index}: {key!r} is {raw!r}, not a DD-Mon-YYYY or YYYY-MM-DD date",
            filename=filename,
        )
    return parsed


def _parse_date(text: str) -> date | None:
    """`DD-Mon-YYYY[ time]` or `YYYY-MM-DD` → `date`, locale-independently, else `None`."""
    dmy = _DMY_RE.match(text)
    if dmy is not None:
        month = month_from_name(dmy.group(2))
        if month is None:
            return None
        try:
            return date(int(dmy.group(3)), month, int(dmy.group(1)))
        except ValueError:
            return None
    ymd = _YMD_RE.match(text)
    if ymd is not None:
        try:
            return date(int(ymd.group(1)), int(ymd.group(2)), int(ymd.group(3)))
        except ValueError:
            return None
    return None
