"""D2: the BSE list-of-scrips, and merging it into the identity master.

BSE keys everything on `SC_CODE`, a numeric scrip code, not on a symbol and not on an ISIN. The
legacy bhavcopy (`bhavcopy.py`) is scrip-code-keyed with no ISIN at all, and BSE corporate actions
(`corp_actions.py`, M2.2) are scrip-code-keyed too — so nothing from BSE can be joined into the
ISIN-keyed platform (invariant #2) until the scrip code has been resolved to an ISIN. This file is
that resolver's source: `ListofScripData/w` (`source_register.yaml` → `bse_scrip_master`), a JSON
array of every equity scrip with its `ISIN_NUMBER`.

What this module produces, and how it stays out of NSE's way:

* **`scrip_to_isin`** — the `SC_CODE → ISIN` map the legacy bhavcopy backfill resolves against
  (`bhavcopy.resolve_legacy`). Pure data, no database.

* **A `DerivedMaster`** of BSE `Security`, `Listing` and `SymbolWindow` rows, in the identity
  master's own vocabulary (`dataplatform.identity.master`). The listings carry `exchange = BSE` and
  `security_code = SC_CODE`, so `ingest_scrip_master` writes them through the *same* `IdentityStore`
  the NSE ingest uses (M1.7) and they land as new `exchange_listing` rows keyed on `(isin,
  exchange)` — a BSE listing never overwrites the NSE one, and a security already known from NSE
  keeps its `primary_exchange` (that column is set once, on first sight; see
  `IdentityStore.write_securities`). A BSE-only ISIN gets a fresh `security_master` row with
  `primary_exchange = BSE`. That is the whole of "merges without clobbering NSE listings".

Two deliberate limits, both honest about what the file does and does not say:

* **BSE gives no listing history.** The scrip master is a snapshot: it names today's symbol
  (`scrip_id`) for each scrip, with no rename history and no start date. So each scrip yields one
  open `SymbolWindow` (`valid_from = snapshot_date`, `valid_to = None`). Unlike NSE (M1.7), there
  is no `symbolchange.csv` to walk, so no earlier windows are invented — a resolve for a BSE symbol
  before this snapshot returns "unknown", which is true. Deeper BSE history is a later concern that
  accumulates snapshot over snapshot, exactly as NSE's does.

* **A scrip without a usable ISIN is skipped and counted, never guessed.** `status=Active` rows
  carry an ISIN, but the delisted/suspended pulls (`pit_notes`) include the occasional blank; such a
  row cannot enter an ISIN-keyed master and is reported in `skipped_no_isin` rather than dropped
  silently or hung on a placeholder.

Offline by construction: `parse_scrip_master` takes decoded JSON text and `ingest_scrip_master`
takes a `Connection` the caller owns and never commits — the fetch is D1's, the transaction is the
caller's, exactly as the NSE identity ingest (M1.7) is arranged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from dataplatform.clock import Clock, SystemClock
from dataplatform.identity.master import (
    DetectedBy,
    Exchange,
    HistoryRefusal,
    IdentityConflict,
    IdentityError,
    IdentityStore,
    Listing,
    ListingStatus,
    Security,
    SymbolWindow,
    WriteCounts,
    detect_conflicts,
    plan_history,
)
from dataplatform.logging import get_logger

__all__ = [
    "BSE_SCRIP_MASTER_SOURCE",
    "BseScrip",
    "BseScripDerivedMaster",
    "BseScripIngestReport",
    "BseScripParseError",
    "derive_master",
    "ingest_scrip_master",
    "parse_scrip_master",
    "scrip_to_isin",
]

_log = get_logger(__name__)

#: The register id this source serves; written into `symbol_history.source` and `exchange_listing`,
#: so a BSE window can always be traced to the file that produced it.
BSE_SCRIP_MASTER_SOURCE: Final = "bse_scrip_master"

#: The same ISIN shape `0001_init.sql`'s domain enforces, checked here so a bad row names its scrip
#: instead of surfacing as a constraint violation on a several-thousand-row insert.
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

#: BSE's status strings, mapped to the platform's `ListingStatus`. The scrip master is pulled per
#: status (`Active|Suspended|Delisted`, `source_register.yaml`); every value must map or the row
#: fails loudly rather than defaulting to ACTIVE and quietly resurrecting a dead scrip.
_STATUS: Final[dict[str, ListingStatus]] = {
    "ACTIVE": ListingStatus.ACTIVE,
    "SUSPENDED": ListingStatus.SUSPENDED,
    "DELISTED": ListingStatus.DELISTED,
}


class BseScripParseError(IdentityError):
    """The scrip master JSON is not the shape this parser was written against."""


@dataclass(frozen=True, slots=True)
class BseScrip:
    """One equity scrip as BSE lists it — the `SC_CODE → (symbol, ISIN)` fact, plus listing detail.

    `scrip_code` is the join key BSE itself uses (kept as text — a leading zero is identity, not a
    number). `symbol` is BSE's `scrip_id`, the ticker the bhavcopy prints. `isin` is validated to
    the ISO shape; a scrip whose source ISIN is blank never becomes one of these (it is counted as
    skipped by the parser instead).
    """

    scrip_code: str
    symbol: str
    name: str
    isin: str
    status: ListingStatus
    group: str | None
    face_value_inr: Decimal | None


@dataclass(frozen=True, slots=True)
class BseScripDerivedMaster:
    """Everything one scrip-master snapshot says, in the identity master's vocabulary. Unsaved."""

    securities: tuple[Security, ...]
    listings: tuple[Listing, ...]
    windows: tuple[SymbolWindow, ...]
    skipped_no_isin: int = 0


@dataclass(frozen=True, slots=True)
class BseScripIngestReport:
    """What one scrip-master ingest saw and changed. Every count is zero on an idempotent re-run."""

    snapshot_date: date
    scrips_seen: int
    skipped_no_isin: int
    counts: WriteCounts
    conflicts: tuple[IdentityConflict, ...] = ()
    refusals: tuple[HistoryRefusal, ...] = ()

    @property
    def is_clean(self) -> bool:
        """No ambiguous identity and no source/store disagreement — the trust condition."""
        return not self.conflicts and not self.refusals

    @property
    def changed_nothing(self) -> bool:
        """True when the store already held exactly this — what idempotence looks like."""
        return self.counts.total == 0


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def parse_scrip_master(text: str) -> tuple[tuple[BseScrip, ...], int]:
    """Parse `ListofScripData/w` JSON into scrips, returning `(scrips, skipped_no_isin)`.

    What it does: reads the JSON array, pulls `SCRIP_CD`, `scrip_id`, `Scrip_Name`/`Issuer_Name`,
    `ISIN_NUMBER`, `Status`, `GROUP` and `FACE_VALUE` from each record, validates the ISIN shape and
    maps the status. Records whose ISIN is blank are counted (`skipped_no_isin`) and excluded — they
    cannot enter an ISIN-keyed master — rather than dropped silently.
    What it assumes: the text is the whole decoded response (a fetch is D1's; a truncated download
    is a D1 problem and reaches here as invalid JSON, which raises).
    What it never does: invent an ISIN, or accept an unknown status string.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BseScripParseError(f"scrip master is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise BseScripParseError(
            f"scrip master must be a JSON array of scrips, got {type(payload).__name__}"
        )

    scrips: list[BseScrip] = []
    skipped = 0
    for index, record in enumerate(payload):
        if not isinstance(record, dict):
            raise BseScripParseError(f"scrip {index} is not a JSON object: {record!r}")
        scrip_code = _text(record, "SCRIP_CD")
        if not scrip_code:
            raise BseScripParseError(f"scrip {index} has no SCRIP_CD: {record!r}")

        isin_raw = _text(record, "ISIN_NUMBER").upper()
        if not isin_raw:
            skipped += 1
            continue
        if not _ISIN.match(isin_raw):
            raise BseScripParseError(f"scrip {scrip_code} ISIN {isin_raw!r} is not an ISIN")

        status_raw = _text(record, "Status").upper()
        status = _STATUS.get(status_raw)
        if status is None:
            raise BseScripParseError(
                f"scrip {scrip_code} has status {status_raw!r}; expected one of {sorted(_STATUS)}"
            )

        symbol = _text(record, "scrip_id") or _text(record, "Scrip_Name")
        if not symbol:
            raise BseScripParseError(
                f"scrip {scrip_code} has neither scrip_id nor Scrip_Name to use as a symbol"
            )
        name = _text(record, "Issuer_Name") or _text(record, "Scrip_Name") or symbol

        scrips.append(
            BseScrip(
                scrip_code=scrip_code,
                symbol=symbol.upper(),
                name=name,
                isin=isin_raw,
                status=status,
                group=_text(record, "GROUP") or None,
                face_value_inr=_decimal(record, "FACE_VALUE", scrip_code=scrip_code),
            )
        )

    _log.info("bse.scrip_master.parsed", scrips=len(scrips), skipped_no_isin=skipped)
    return tuple(scrips), skipped


def scrip_to_isin(scrips: Iterable[BseScrip]) -> dict[str, str]:
    """The `SC_CODE → ISIN` map the legacy bhavcopy backfill resolves against.

    A scrip code appearing twice with two ISINs is an identity contradiction the map cannot
    arbitrate, so it raises rather than letting one silently win — the same stance the identity
    master takes on an ambiguous symbol.
    """
    mapping: dict[str, str] = {}
    for scrip in scrips:
        existing = mapping.get(scrip.scrip_code)
        if existing is not None and existing != scrip.isin:
            raise BseScripParseError(
                f"scrip code {scrip.scrip_code} maps to two ISINs ({existing}, {scrip.isin}); "
                "the scrip master contradicts itself and a human must resolve which is right"
            )
        mapping[scrip.scrip_code] = scrip.isin
    return mapping


# ── derivation ───────────────────────────────────────────────────────────────────────────────


def derive_master(
    scrips: Iterable[BseScrip],
    *,
    snapshot_date: date,
) -> BseScripDerivedMaster:
    """Turn one scrip-master snapshot into BSE securities, listings and symbol windows.

    What it does: emits one `Security` (`primary_exchange = BSE`), one `Listing` (`exchange = BSE`,
    `security_code = SC_CODE`) and one open `SymbolWindow` per scrip. The window is open because the
    snapshot names today's symbol and BSE publishes no rename history — no earlier window invented.
    What it never does: date a window from anything but the snapshot, or set a `valid_to` the file
    does not support.

    Note that `primary_exchange = BSE` is only *asserted* for a BSE-only ISIN: `write_securities`
    sets `primary_exchange` once, on the row's first sight, so a security already written by the NSE
    ingest keeps `NSE` and merely gains a BSE `exchange_listing` row.
    """
    scrip_list = tuple(scrips)
    securities = tuple(
        Security(
            isin=scrip.isin,
            name=scrip.name,
            primary_exchange=Exchange.BSE,
            status=scrip.status,
            first_seen_date=snapshot_date,
            last_seen_date=snapshot_date,
            face_value_inr=scrip.face_value_inr,
        )
        for scrip in scrip_list
    )
    listings = tuple(
        Listing(
            isin=scrip.isin,
            exchange=Exchange.BSE,
            status=scrip.status,
            security_code=scrip.scrip_code,
            series=scrip.group,
            face_value_inr=scrip.face_value_inr,
        )
        for scrip in scrip_list
    )
    windows = tuple(
        SymbolWindow(
            exchange=Exchange.BSE,
            symbol=scrip.symbol,
            valid_from=snapshot_date,
            valid_to=None,
            isin=scrip.isin,
            series=scrip.group,
            source=BSE_SCRIP_MASTER_SOURCE,
        )
        for scrip in scrip_list
    )
    _log.info(
        "bse.scrip_master.derived",
        snapshot_date=snapshot_date.isoformat(),
        securities=len(securities),
        listings=len(listings),
        windows=len(windows),
    )
    return BseScripDerivedMaster(securities=securities, listings=listings, windows=windows)


# ── ingest ───────────────────────────────────────────────────────────────────────────────────


def ingest_scrip_master(
    conn: object,
    *,
    scrip_master_json: str,
    snapshot_date: date | None = None,
    clock: Clock | None = None,
) -> BseScripIngestReport:
    """Merge one BSE scrip-master snapshot into the identity master. Does not commit.

    What it does: parses the JSON, derives BSE securities/listings/windows, detects every ambiguity
    against what is already stored as well as within the snapshot, and writes through the same
    `IdentityStore` the NSE ingest uses — so BSE listings land as new `(isin, exchange)` rows
    without touching NSE's, and a dual-listed ISIN keeps its NSE-set `primary_exchange`.
    What it assumes: one writer at a time and a caller that owns the transaction (commit is the
    caller's), exactly as `dataplatform.store.db.connection` intends.
    What it never does: raise on an ambiguous symbol — the conflict is queued and the report says
    `is_clean = False`, mirroring the NSE ingest, so one bad symbol does not cost the other scrips
    their update.

    `conn` is typed `object` to avoid importing the psycopg connection into an offline-testable
    module; `IdentityStore` gives it its real contract.
    """
    from dataplatform.store.db import Connection  # local import: keeps the module import-light

    assert isinstance(conn, Connection)
    clock = SystemClock() if clock is None else clock
    snapshot_date = clock.today() if snapshot_date is None else snapshot_date

    scrips, skipped = parse_scrip_master(scrip_master_json)
    derived = derive_master(scrips, snapshot_date=snapshot_date)

    store = IdentityStore(conn, clock=clock)
    stored = store.load_windows()
    plan = plan_history(stored, derived.windows)
    conflicts = detect_conflicts(
        plan.applied_to(stored), source=BSE_SCRIP_MASTER_SOURCE, detected_by=DetectedBy.INGEST
    )

    securities = store.write_securities(derived.securities)
    listings = store.write_listings(derived.listings)
    inserted, closed = store.apply_history(plan)
    queued = sum(1 for conflict in conflicts if store.record(conflict))

    counts = WriteCounts(
        securities=securities,
        listings=listings,
        windows_inserted=inserted,
        windows_closed=closed,
        conflicts_queued=queued,
    )
    report = BseScripIngestReport(
        snapshot_date=snapshot_date,
        scrips_seen=len(scrips),
        skipped_no_isin=skipped,
        counts=counts,
        conflicts=conflicts,
        refusals=plan.refusals,
    )
    _log.info(
        "bse.scrip_master.ingested",
        source=BSE_SCRIP_MASTER_SOURCE,
        snapshot_date=snapshot_date.isoformat(),
        scrips=len(scrips),
        skipped_no_isin=skipped,
        securities=securities,
        listings=listings,
        windows_inserted=inserted,
        windows_closed=closed,
        conflicts=len(conflicts),
        refusals=len(plan.refusals),
    )
    for refusal in plan.refusals:
        _log.warning(
            "bse.scrip_master.history.refused",
            isin=refusal.stored.isin,
            symbol=refusal.stored.symbol,
            reason=refusal.reason,
        )
    return report


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _text(record: dict[str, object], key: str) -> str:
    """A record field as a stripped string. Missing or JSON null becomes the empty string."""
    value = record.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _decimal(record: dict[str, object], key: str, *, scrip_code: str) -> Decimal | None:
    """A money field as `Decimal` (never float — CLAUDE.md), or `None` when blank."""
    raw = _text(record, key)
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise BseScripParseError(
            f"scrip {scrip_code} {key} is {raw!r}, which is not a number"
        ) from exc
