"""M14.6 — a persisted daily momentum-v2 signal panel, so a daily-ranking sweep reads the lake once.

D13 ranks its universe on the first session of each month; the M14.6 per-stock variants rank it on
*every* session. The candidate set of one session (``backtest.run._L1MomentumV2Data._compute``)
costs ~1.5 s once its look-back closes are cached and ~8 s cold, so a daily arm recomputing it pays
about an hour per decade — and every arm, window and floor of a sweep would pay it again for the
same answer, because a session's candidate set depends on the lake, the universe screen and the
price basis, never on the arm or the replay window.

So the set is computed once per (universe, session) by the very same ``_compute``, written here as
plain text, and served back to every run while :func:`signal_panel_in_force` holds the panel. A run
reading the panel and a run computing live see the same records (``str(Decimal)`` round-trips
exactly; the record order is irrelevant because every ranking sorts on ``(-momentum, isin)``), so
a run's specification — and its digest — does not record which path served it. The M14.6 driver
checks that claim on D13 rather than assuming it (``ops/gates/M14.6-per-stock-entry-report.md``).

Layout: ``<root>/manifest.json`` names each universe key's sub-directory, the session span and the
lake it was cut from; ``<root>/<subdir>/<YYYY-MM>.json`` maps each session to its records, ISIN
sorted, as ``[isin, momentum_0_12, momentum_12_1, price, volatility]`` strings.

What it never does: write under the lake (a panel is a derived research artefact and lives in the
run directory), answer for a session or universe it was not built for (``None`` — the caller then
computes live), or carry a record knowable after its session (every record's ``knowable_date`` is
the session itself, checked on write and re-stamped on read).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from backtest.policies.momentum_v2 import MomentumV2Record

__all__ = [
    "PANEL_VERSION",
    "SignalPanel",
    "SignalPanelError",
    "current_signal_panel",
    "panel_key",
    "signal_panel_in_force",
    "write_month",
    "write_panel_manifest",
]

PANEL_VERSION = 1
_MANIFEST = "manifest.json"


class SignalPanelError(Exception):
    """A panel that cannot be trusted: wrong version, a record dated past its session, a gap."""


def panel_key(universe: str, *, adjusted: bool, l1_isins_only: bool, data_root: str) -> str:
    """The identity of one candidate-set series: universe screen, price basis, lake root.

    ``universe`` is the ``repr`` of the run's ``UniverseParameters`` (``None`` for no screen), the
    same string the run specification records, so a panel cut for the ₹1 cr floor never answers
    for a ₹10 cr run.
    """
    return f"{universe}|adjusted={adjusted}|l1_isins_only={l1_isins_only}|root={data_root}"


def _encode(record: MomentumV2Record) -> list[str]:
    return [
        record.isin,
        str(record.momentum_0_12),
        str(record.momentum_12_1),
        str(record.price),
        str(record.volatility),
    ]


def write_month(
    root: Path, subdir: str, month: str, sessions: Mapping[date, Sequence[MomentumV2Record]]
) -> Path:
    """Write one month of one series; refuses a record whose knowable date is not its session."""
    body: dict[str, list[list[str]]] = {}
    for session in sorted(sessions):
        records = sessions[session]
        for record in records:
            if record.knowable_date != session:
                raise SignalPanelError(
                    f"{record.isin} on {session.isoformat()} is knowable "
                    f"{record.knowable_date.isoformat()}; a panel row is its session's close"
                )
        body[session.isoformat()] = [_encode(r) for r in sorted(records, key=lambda r: r.isin)]
    target = root / subdir / f"{month}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(body, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def write_panel_manifest(root: Path, manifest: Mapping[str, Any]) -> None:
    """Write ``manifest.json`` (``version``, ``first``, ``last``, ``keys`` and provenance)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / _MANIFEST).write_text(
        json.dumps({"version": PANEL_VERSION, **manifest}, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class SignalPanel:
    """A built panel, read lazily a month file at a time.

    ``lookup`` answers only for a key the panel holds and a session inside its span; a session in
    the span that the month file does not carry is a broken panel and raises rather than letting
    the caller silently compute a different answer for part of a run.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        manifest = json.loads((root / _MANIFEST).read_text(encoding="utf-8"))
        if manifest.get("version") != PANEL_VERSION:
            raise SignalPanelError(f"{root}: panel version {manifest.get('version')!r} unknown")
        self._manifest: dict[str, Any] = manifest
        self._first = date.fromisoformat(manifest["first"])
        self._last = date.fromisoformat(manifest["last"])
        self._keys: dict[str, str] = dict(manifest["keys"])
        self._months: dict[tuple[str, str], dict[str, list[list[str]]]] = {}

    @property
    def root(self) -> Path:
        return self._root

    @property
    def manifest(self) -> Mapping[str, Any]:
        return self._manifest

    def identity(self) -> dict[str, Any]:
        """What a campaign manifest records about the panel it ran under."""
        return {
            "path": str(self._root.resolve()),
            "first": self._first.isoformat(),
            "last": self._last.isoformat(),
            "lake_last_session": self._manifest.get("lake_last_session"),
            "commit": self._manifest.get("commit"),
            "keys": sorted(self._keys),
        }

    def holds(self, key: str) -> bool:
        return key in self._keys

    def lookup(self, key: str, as_of: date) -> tuple[MomentumV2Record, ...] | None:
        """The candidate set of ``as_of`` for ``key``; ``None`` outside what the panel covers."""
        subdir = self._keys.get(key)
        if subdir is None or not self._first <= as_of <= self._last:
            return None
        month = as_of.strftime("%Y-%m")
        cache_key = (subdir, month)
        body = self._months.get(cache_key)
        if body is None:
            path = self._root / subdir / f"{month}.json"
            if not path.is_file():
                raise SignalPanelError(f"panel {self._root} has no {subdir}/{month}.json")
            body = json.loads(path.read_text(encoding="utf-8"))
            # Keep the panel's memory to a few months: a walk moves forward.
            if len(self._months) >= 4:
                self._months.pop(next(iter(self._months)))
            self._months[cache_key] = body
        rows = body.get(as_of.isoformat())
        if rows is None:
            return None  # not a session of this lake; the live path answers (and finds nothing)
        return tuple(
            MomentumV2Record(
                isin=isin,
                momentum_0_12=Decimal(m012),
                momentum_12_1=Decimal(m121),
                price=Decimal(price),
                volatility=Decimal(vol),
                knowable_date=as_of,
            )
            for isin, m012, m121, price, vol in rows
        )


_CURRENT: ContextVar[SignalPanel | None] = ContextVar("signal_panel", default=None)


@contextmanager
def signal_panel_in_force(panel: SignalPanel | None) -> Iterator[None]:
    """Serve momentum-v2 candidate sets from ``panel`` inside the block (``None``: compute live).

    The same shape as ``backtest.book_actions.corporate_actions_in_force``: the sweep builds its
    data sources deep inside the runner, and a context variable reaches them without threading a
    parameter through every report function. A spawned worker must enter it itself.
    """
    token = _CURRENT.set(panel)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_signal_panel() -> SignalPanel | None:
    """The panel :func:`signal_panel_in_force` put in force, or ``None``."""
    return _CURRENT.get()
