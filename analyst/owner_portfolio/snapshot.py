"""M19.3 · Private, write-once snapshots of the owner's Kite account, and `OwnerPortfolio`.

`refresh` reads every allowlisted endpoint once and writes the raw envelopes, byte for byte, to
``<data_root>/private/kite/<YYYY-MM-DD>/<HHMMSS>/<endpoint>.json`` plus a ``manifest.json`` that
records each file's sha256, the HTTP status and the clock time (IST). A snapshot is written once and
never edited — the L0 discipline — but it lives **outside** ``data/L0``: it is the owner's private
account, not market data, so it is in no lake rebuild and not in the S3 L0 mirror. ``data/`` is
gitignored, so nothing here can be committed.

Every review tool works from a snapshot (`load`), never from a live call: a review is reproducible
and the agent cannot hammer the API. A refresh closer than `MIN_REFRESH_INTERVAL` to the last one
is refused.

Endpoint failures: an expired session aborts the refresh (nothing is written) with
`KiteSessionError`. Any other API error on a core endpoint (profile, margins, holdings, positions,
orders, trades) aborts too. The MF and GTT endpoints are optional — an account without the MF
segment answers them with an error — so their error envelope is kept as it came, and `load`
reports the endpoint in `OwnerPortfolio.unavailable` instead of an empty list.

What it never does: write to Postgres, the journal or the status API; log a symbol, a quantity or
a value (only counts and snapshot ids); edit or delete a snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import structlog

from dataplatform.clock import IST, Clock
from execution.kite_read import (
    Endpoint,
    KiteFunds,
    KiteGtt,
    KiteHolding,
    KiteMalformedResponseError,
    KiteMfHolding,
    KiteMfOrder,
    KiteMfSip,
    KiteOrder,
    KitePosition,
    KiteProfile,
    KiteRawResponse,
    KiteReader,
    KiteRequestError,
    KiteSessionError,
    KiteTrade,
    parse_envelope,
    parse_funds,
    parse_gtt_triggers,
    parse_holdings,
    parse_mf_holdings,
    parse_mf_orders,
    parse_mf_sips,
    parse_orders,
    parse_positions,
    parse_profile,
    parse_trades,
)

__all__ = [
    "CORE_ENDPOINTS",
    "MIN_REFRESH_INTERVAL",
    "OPTIONAL_ENDPOINTS",
    "SNAPSHOT_FORMAT",
    "EndpointUnavailable",
    "OwnerPortfolio",
    "RefreshTooSoonError",
    "SnapshotError",
    "SnapshotInfo",
    "SnapshotIntegrityError",
    "SnapshotNotFoundError",
    "SnapshotStore",
    "default_snapshot_root",
]

_LOG = structlog.get_logger(__name__)

SNAPSHOT_FORMAT: Final[str] = "kite-snapshot/1"
MIN_REFRESH_INTERVAL: Final[timedelta] = timedelta(minutes=5)
CORE_ENDPOINTS: Final[tuple[Endpoint, ...]] = (
    Endpoint.PROFILE,
    Endpoint.MARGINS,
    Endpoint.HOLDINGS,
    Endpoint.POSITIONS,
    Endpoint.ORDERS,
    Endpoint.TRADES,
)
OPTIONAL_ENDPOINTS: Final[tuple[Endpoint, ...]] = (
    Endpoint.MF_HOLDINGS,
    Endpoint.MF_SIPS,
    Endpoint.MF_ORDERS,
    Endpoint.GTT_TRIGGERS,
)
_MANIFEST: Final[str] = "manifest.json"
_SNAPSHOT_ID: Final = re.compile(r"(\d{4}-\d{2}-\d{2})/(\d{6})")
_DIR_MODE: Final[int] = 0o700
_FILE_MODE: Final[int] = 0o400


def default_snapshot_root(data_root: Path) -> Path:
    """``<data_root>/private/kite`` — gitignored with the rest of ``data/``, outside ``data/L0``."""
    return data_root / "private" / "kite"


# ── errors ───────────────────────────────────────────────────────────────────────────────────


class SnapshotError(Exception):
    """Base for snapshot-store failures."""


class SnapshotNotFoundError(SnapshotError):
    """No snapshot with that id (or none at all)."""


class SnapshotIntegrityError(SnapshotError):
    """A snapshot file does not match its manifest (hash, size) or the manifest is unusable."""


class RefreshTooSoonError(SnapshotError):
    """The last snapshot is newer than the minimum refresh interval; use it instead."""

    def __init__(self, latest: SnapshotInfo, wait: timedelta) -> None:
        super().__init__(
            f"refresh refused: snapshot {latest.snapshot_id} is younger than the "
            f"{int(MIN_REFRESH_INTERVAL.total_seconds())} s minimum interval; use it, or retry in "
            f"{int(wait.total_seconds()) + 1} s"
        )
        self.latest = latest
        self.wait = wait


# ── values ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    """A snapshot's id (``YYYY-MM-DD/HHMMSS``), when it was taken (IST) and where it lives."""

    snapshot_id: str
    taken_at: datetime
    path: Path


@dataclass(frozen=True, slots=True)
class EndpointUnavailable:
    """An optional endpoint Kite answered with an error, kept as evidence rather than emptied."""

    endpoint: str
    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class OwnerPortfolio:
    """One snapshot, parsed: the owner's account as Kite reported it, ISIN-keyed.

    ``holdings`` keeps every row Kite sent (a name sold out today stays, with ``held_quantity``
    0); `held` is the rows the account owns now. ``positions``, ``orders`` and ``trades`` are
    today's, as Kite only serves today's. ``unavailable`` names optional endpoints that errored.
    """

    snapshot_id: str
    taken_at: datetime
    profile: KiteProfile
    funds: KiteFunds
    holdings: tuple[KiteHolding, ...]
    positions: tuple[KitePosition, ...]
    orders: tuple[KiteOrder, ...]
    trades: tuple[KiteTrade, ...]
    mf_holdings: tuple[KiteMfHolding, ...] = ()
    mf_sips: tuple[KiteMfSip, ...] = ()
    mf_orders: tuple[KiteMfOrder, ...] = ()
    gtt_triggers: tuple[KiteGtt, ...] = ()
    unavailable: tuple[EndpointUnavailable, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.taken_at.tzinfo is None:
            raise ValueError("OwnerPortfolio.taken_at must be tz-aware (IST)")
        seen: set[str] = set()
        for row in self.holdings:
            if row.isin in seen:
                raise KiteMalformedResponseError(
                    "holdings: one ISIN appears in two rows; a review cannot key it"
                )
            seen.add(row.isin)

    @property
    def held(self) -> tuple[KiteHolding, ...]:
        """Holdings the account owns now (settled + T+1 > 0), in ISIN order."""
        return tuple(h for h in sorted(self.holdings, key=lambda h: h.isin) if h.held_quantity > 0)

    def holding(self, isin: str) -> KiteHolding | None:
        return next((h for h in self.holdings if h.isin == isin), None)


# ── the store ────────────────────────────────────────────────────────────────────────────────


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _write_once(path: Path, body: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    path.chmod(_FILE_MODE)


class SnapshotStore:
    """The private snapshot directory: list, refresh (write-once), load (verify, then parse).

    What it assumes: one owner, one process refreshing at a time — the min interval and the
    exclusive directory create make a second concurrent refresh fail rather than interleave.
    What it never does: overwrite or edit a file it wrote; read the network itself (the caller
    hands it a `KiteReader`); log what the account holds.
    """

    def __init__(
        self,
        root: Path,
        *,
        clock: Clock,
        min_interval: timedelta = MIN_REFRESH_INTERVAL,
    ) -> None:
        self._root = root
        self._clock = clock
        self._min_interval = min_interval

    @property
    def root(self) -> Path:
        return self._root

    def _dir(self, snapshot_id: str) -> Path:
        match = _SNAPSHOT_ID.fullmatch(snapshot_id)
        if match is None:
            raise SnapshotNotFoundError(f"{snapshot_id!r} is not a snapshot id (YYYY-MM-DD/HHMMSS)")
        return self._root / match.group(1) / match.group(2)

    def snapshots(self) -> tuple[SnapshotInfo, ...]:
        """Every complete snapshot (one with a manifest), oldest first."""
        if not self._root.is_dir():
            return ()
        found: list[SnapshotInfo] = []
        for day in sorted(p for p in self._root.iterdir() if p.is_dir()):
            for slot in sorted(p for p in day.iterdir() if p.is_dir()):
                snapshot_id = f"{day.name}/{slot.name}"
                if _SNAPSHOT_ID.fullmatch(snapshot_id) is None:
                    continue
                manifest = slot / _MANIFEST
                if not manifest.is_file():
                    continue  # an interrupted write: no manifest, not a snapshot
                found.append(self._info(snapshot_id, manifest))
        return tuple(found)

    def _info(self, snapshot_id: str, manifest_path: Path) -> SnapshotInfo:
        manifest = self._manifest(manifest_path)
        try:
            taken_at = datetime.fromisoformat(str(manifest["taken_at"]))
        except (KeyError, ValueError):
            raise SnapshotIntegrityError(f"{snapshot_id}: manifest has no valid taken_at") from None
        return SnapshotInfo(snapshot_id=snapshot_id, taken_at=taken_at, path=manifest_path.parent)

    @staticmethod
    def _manifest(path: Path) -> dict[str, Any]:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise SnapshotIntegrityError(f"{path} is not a readable manifest") from None
        if not isinstance(manifest, dict) or manifest.get("format") != SNAPSHOT_FORMAT:
            raise SnapshotIntegrityError(f"{path} is not a {SNAPSHOT_FORMAT} manifest")
        return manifest

    def latest(self) -> SnapshotInfo | None:
        snapshots = self.snapshots()
        return snapshots[-1] if snapshots else None

    def refresh(self, reader: KiteReader) -> SnapshotInfo:
        """Read every endpoint once and write a new snapshot. See the module docstring."""
        now = self._clock.now().astimezone(IST)
        latest = self.latest()
        if latest is not None and now - latest.taken_at < self._min_interval:
            raise RefreshTooSoonError(latest, self._min_interval - (now - latest.taken_at))
        responses: dict[Endpoint, KiteRawResponse] = {}
        for endpoint in (*CORE_ENDPOINTS, *OPTIONAL_ENDPOINTS):
            raw = reader.raw(endpoint)
            try:
                _PARSERS[endpoint](parse_envelope(raw))
            except KiteSessionError:
                _LOG.warning("owner_portfolio.refresh.session_expired", endpoint=endpoint.stem)
                raise
            except KiteRequestError as err:
                if endpoint in CORE_ENDPOINTS:
                    raise
                _LOG.warning(
                    "owner_portfolio.refresh.optional_unavailable",
                    endpoint=endpoint.stem,
                    error_type=err.error_type,
                )
            responses[endpoint] = raw
        return self.write(responses, taken_at=now)

    def write(
        self, responses: Mapping[Endpoint, KiteRawResponse], *, taken_at: datetime
    ) -> SnapshotInfo:
        """Write ``responses`` as a new snapshot taken at ``taken_at``. Write-once."""
        missing = [e.stem for e in CORE_ENDPOINTS if e not in responses]
        if missing:
            raise SnapshotError(f"a snapshot needs every core endpoint; missing {missing}")
        local = taken_at.astimezone(IST)
        snapshot_id = f"{local:%Y-%m-%d}/{local:%H%M%S}"
        target = self._dir(snapshot_id)
        self._root.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        target.parent.mkdir(mode=_DIR_MODE, exist_ok=True)
        try:
            target.mkdir(mode=_DIR_MODE)
        except FileExistsError:
            raise SnapshotError(
                f"snapshot {snapshot_id} already exists; snapshots are write-once"
            ) from None
        files: dict[str, dict[str, Any]] = {}
        for endpoint, raw in sorted(responses.items(), key=lambda kv: kv[0].stem):
            name = f"{endpoint.stem}.json"
            _write_once(target / name, raw.body)
            files[name] = {
                "endpoint": endpoint.value,
                "status_code": raw.status_code,
                "sha256": _sha256(raw.body),
                "bytes": len(raw.body),
            }
        manifest = {
            "format": SNAPSHOT_FORMAT,
            "snapshot_id": snapshot_id,
            "taken_at": local.isoformat(),
            "files": files,
        }
        # The manifest goes last: a snapshot without one was interrupted and is never listed.
        _write_once(
            target / _MANIFEST, json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        )
        _LOG.info("owner_portfolio.snapshot.written", snapshot_id=snapshot_id, files=len(files))
        return SnapshotInfo(snapshot_id=snapshot_id, taken_at=local, path=target)

    def load(self, snapshot_id: str | None = None) -> OwnerPortfolio:
        """Verify and parse a snapshot (the latest when ``snapshot_id`` is None)."""
        if snapshot_id is None:
            latest = self.latest()
            if latest is None:
                raise SnapshotNotFoundError(
                    "no Kite snapshot yet: refresh first (needs a logged-in session)"
                )
            snapshot_id = latest.snapshot_id
        target = self._dir(snapshot_id)
        manifest_path = target / _MANIFEST
        if not manifest_path.is_file():
            raise SnapshotNotFoundError(f"no snapshot {snapshot_id}")
        info = self._info(snapshot_id, manifest_path)
        manifest = self._manifest(manifest_path)
        entries = manifest.get("files")
        if not isinstance(entries, dict):
            raise SnapshotIntegrityError(f"{snapshot_id}: manifest lists no files")
        parsed: dict[Endpoint, Any] = {}
        unavailable: list[EndpointUnavailable] = []
        for endpoint in (*CORE_ENDPOINTS, *OPTIONAL_ENDPOINTS):
            name = f"{endpoint.stem}.json"
            entry = entries.get(name)
            if entry is None:
                if endpoint in CORE_ENDPOINTS:
                    raise SnapshotIntegrityError(f"{snapshot_id}: core file {name} missing")
                unavailable.append(
                    EndpointUnavailable(endpoint.stem, "NotCaptured", "not in snapshot")
                )
                continue
            body = (target / name).read_bytes()
            if len(body) != entry.get("bytes") or _sha256(body) != entry.get("sha256"):
                raise SnapshotIntegrityError(f"{snapshot_id}: {name} does not match its manifest")
            raw = KiteRawResponse(endpoint.value, int(entry.get("status_code", 0)), body)
            try:
                parsed[endpoint] = _PARSERS[endpoint](parse_envelope(raw))
            except KiteRequestError as err:
                if endpoint in CORE_ENDPOINTS:
                    raise
                unavailable.append(EndpointUnavailable(endpoint.stem, err.error_type, err.message))
        portfolio = OwnerPortfolio(
            snapshot_id=snapshot_id,
            taken_at=info.taken_at,
            profile=parsed[Endpoint.PROFILE],
            funds=parsed[Endpoint.MARGINS],
            holdings=tuple(sorted(parsed[Endpoint.HOLDINGS], key=lambda h: h.isin)),
            positions=parsed[Endpoint.POSITIONS],
            orders=parsed[Endpoint.ORDERS],
            trades=parsed[Endpoint.TRADES],
            mf_holdings=parsed.get(Endpoint.MF_HOLDINGS, ()),
            mf_sips=parsed.get(Endpoint.MF_SIPS, ()),
            mf_orders=parsed.get(Endpoint.MF_ORDERS, ()),
            gtt_triggers=parsed.get(Endpoint.GTT_TRIGGERS, ()),
            unavailable=tuple(unavailable),
        )
        _LOG.info(
            "owner_portfolio.snapshot.loaded",
            snapshot_id=snapshot_id,
            holdings=len(portfolio.holdings),
            unavailable=len(portfolio.unavailable),
        )
        return portfolio


_PARSERS: Final[Mapping[Endpoint, Any]] = {
    Endpoint.PROFILE: parse_profile,
    Endpoint.MARGINS: parse_funds,
    Endpoint.HOLDINGS: parse_holdings,
    Endpoint.POSITIONS: parse_positions,
    Endpoint.ORDERS: parse_orders,
    Endpoint.TRADES: parse_trades,
    Endpoint.MF_HOLDINGS: parse_mf_holdings,
    Endpoint.MF_SIPS: parse_mf_sips,
    Endpoint.MF_ORDERS: parse_mf_orders,
    Endpoint.GTT_TRIGGERS: parse_gtt_triggers,
}
