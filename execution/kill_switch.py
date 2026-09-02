"""X1: the kill switch — one flag that halts all order placement, and outlives the process.

EXECUTION_PLAN §6 / invariant-adjacent safety: there must be a single, unambiguous way to stop the
platform from placing orders, trippable by a human *and* automatically by the two things that can
detect the account is no longer safe to trade — the risk rails (A8) and daily reconciliation
(`recon.py`). "One command/endpoint that halts all placement" (M5.12 spec) is exactly this object:
every path that would hand an order to a broker asks it `require_placement_allowed()` first, and a
tripped switch turns that into a loud refusal rather than a trade.

Two properties make it a safety control rather than a convenience flag:

* **It survives a restart.** A switch that forgets it was tripped the moment the process dies is
  worse than none: the operator reboots the daily loop after an incident and it happily resumes
  placing orders. So the state is written to a file on every transition and read back on
  construction — a fresh `KillSwitch` pointed at the same path *is* the same switch. This is the
  half `tests/integration/test_recon_drill.py` checks by constructing a second instance.
* **It fails safe and loud.** A tripped switch does not return `False` for a caller to ignore; it
  raises `TradingHaltedError` from `require_placement_allowed()`, carrying why it tripped and what
  tripped it, so the refusal reaches the operator with its cause attached rather than as a silent
  no-op three frames away (CLAUDE.md: fail loud and specific).

What it never does: decide *whether* to trip. Rails and recon make that call and record their
reason; the switch only carries the state and enforces it. Resetting it is deliberately a separate,
explicit act (`reset`) — an automatic reset would defeat the entire point of a latch.

Time is an injected `Clock` (B10): the trip timestamp is the clock's, so a replay through the
switch is reproducible and nothing here reads a wall clock behind the caller's back.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import structlog

from dataplatform.clock import Clock, SystemClock

__all__ = [
    "KillSwitch",
    "KillSwitchState",
    "TradingHaltedError",
    "TripSource",
]

_LOG = structlog.get_logger(__name__)

#: State-file schema version, written into the file so a future format change is detectable rather
#: than silently misread as the current shape.
_STATE_VERSION = 1


class TripSource(StrEnum):
    """What tripped the switch — the M5.12 spec's "rails or recon", plus the human.

    Recorded on the state so an operator opening an alert (or the state file) sees *what* decided
    the account was unsafe, which is the first question after a halt.
    """

    MANUAL = "MANUAL"
    """A human pulled it — an operator or the daily loop on an explicit instruction."""

    RAILS = "RAILS"
    """The deterministic risk rails (A8) tripped it: a breached cap the rails cannot let stand."""

    RECON = "RECON"
    """Daily reconciliation found the broker and the internal book disagree (`recon.py`)."""


class TradingHaltedError(Exception):
    """Raised by `require_placement_allowed()` when the switch is tripped.

    Carries the trip reason and source so the refusal is self-explaining wherever it surfaces:
    a caller that lets it propagate hands the operator the cause, not just "an order failed".
    """

    def __init__(self, state: KillSwitchState) -> None:
        self.state = state
        detail = "" if state.reason is None else f": {state.reason}"
        source = "unknown" if state.source is None else state.source.value
        when = "" if state.tripped_at is None else f" at {state.tripped_at.isoformat()}"
        super().__init__(
            f"trading is halted — kill switch tripped by {source}{when}{detail}. "
            "Order placement is blocked until the switch is explicitly reset."
        )


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    """The switch's state as it is persisted and read back — the whole latch in one value.

    Frozen because a state is a record of a transition, not a mutable slot; a new transition
    produces a new state and a new file, it does not edit this one. `tripped_at`/`source`/`reason`
    are populated only while `tripped` is true, and cleared on reset so a stale reason cannot be
    read off an armed switch.
    """

    tripped: bool
    reason: str | None = None
    source: TripSource | None = None
    tripped_at: datetime | None = None

    def to_json(self) -> dict[str, object]:
        """Serialize to the on-disk shape. `tripped_at` is an ISO-8601 string (tz-aware)."""
        return {
            "version": _STATE_VERSION,
            "tripped": self.tripped,
            "reason": self.reason,
            "source": None if self.source is None else self.source.value,
            "tripped_at": None if self.tripped_at is None else self.tripped_at.isoformat(),
        }

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> KillSwitchState:
        """Rebuild from the on-disk shape, validating the version and the timestamp's awareness."""
        version = raw.get("version")
        if version != _STATE_VERSION:
            raise ValueError(
                f"kill-switch state file is version {version!r}, this build reads "
                f"{_STATE_VERSION}; refusing to guess at an unknown format"
            )
        source_raw = raw.get("source")
        tripped_at_raw = raw.get("tripped_at")
        tripped_at = None
        if tripped_at_raw is not None:
            if not isinstance(tripped_at_raw, str):
                raise ValueError(f"tripped_at must be an ISO string, got {tripped_at_raw!r}")
            tripped_at = datetime.fromisoformat(tripped_at_raw)
            if tripped_at.tzinfo is None:
                raise ValueError(f"persisted tripped_at is naive: {tripped_at_raw!r}")
        reason = raw.get("reason")
        return cls(
            tripped=bool(raw.get("tripped")),
            reason=None if reason is None else str(reason),
            source=None if source_raw is None else TripSource(str(source_raw)),
            tripped_at=tripped_at,
        )


class KillSwitch:
    """A latching halt on order placement whose state lives in a file, not in this process.

    Construct it with the path its state lives at and an injected `Clock`. If the file already
    exists it is read, so a switch tripped by yesterday's incident is still tripped when today's
    daily loop constructs a fresh one — that is the "survives a restart" property, and it is why
    the state is a file rather than an attribute.

    The contract every placement path follows: call `require_placement_allowed()` before handing an
    order to a broker. A tripped switch raises `TradingHaltedError`; an armed one returns and the
    order proceeds. Rails and recon call `trip(...)` when they detect an unsafe account; a human
    calls `reset(...)` to arm it again after the cause is understood.

    What it never does: reset itself, or let a placement through while tripped. There is no override
    parameter — the whole value of a kill switch is that it cannot be argued with in code.
    """

    __slots__ = ("_clock", "_path", "_state")

    def __init__(self, path: Path | str, *, clock: Clock | None = None) -> None:
        self._path = Path(path)
        self._clock = SystemClock() if clock is None else clock
        self._state = self._read()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(path={self._path!r}, tripped={self._state.tripped})"

    @property
    def state(self) -> KillSwitchState:
        """The current latch state, as last persisted."""
        return self._state

    @property
    def is_tripped(self) -> bool:
        """True while placement is halted. The cheap read; `require_placement_allowed` enforces."""
        return self._state.tripped

    def require_placement_allowed(self) -> None:
        """Raise `TradingHaltedError` if the switch is tripped; return silently if it is armed.

        This is the one call every path that would place an order makes first. It raises rather
        than returning a bool so a caller cannot forget to check the result — an ignored `False`
        is exactly the silent failure a kill switch exists to prevent.
        """
        if self._state.tripped:
            raise TradingHaltedError(self._state)

    def trip(self, *, reason: str, source: TripSource) -> KillSwitchState:
        """Halt placement, recording why and what tripped it. Idempotent while already tripped.

        Persists before returning, so the halt survives the process even if it dies on the next
        line. Re-tripping an already-tripped switch keeps the *first* cause: the original reason is
        the one an incident review wants, not whatever tripped it again afterwards.
        """
        if not reason.strip():
            raise ValueError("a trip needs a non-blank reason — an operator must see the cause")
        if self._state.tripped:
            _LOG.info(
                "kill_switch.trip.noop",
                already_source=None if self._state.source is None else self._state.source.value,
                new_source=source.value,
            )
            return self._state
        state = KillSwitchState(
            tripped=True, reason=reason, source=source, tripped_at=self._clock.now()
        )
        self._write(state)
        _LOG.warning(
            "kill_switch.tripped",
            source=source.value,
            reason=reason,
            tripped_at=None if state.tripped_at is None else state.tripped_at.isoformat(),
        )
        return state

    def reset(self, *, note: str) -> KillSwitchState:
        """Arm the switch again, clearing the trip. A deliberate human act, never automatic.

        `note` is required and logged: re-arming a safety latch after a halt is a decision that
        should leave a trace of who decided the cause was resolved. A no-op on an already-armed
        switch, so re-running a recovery step is safe.
        """
        if not note.strip():
            raise ValueError("a reset needs a non-blank note — re-arming is a recorded decision")
        if not self._state.tripped:
            return self._state
        state = KillSwitchState(tripped=False)
        self._write(state)
        _LOG.warning("kill_switch.reset", note=note)
        return state

    # ── persistence ────────────────────────────────────────────────────────────────────────────

    def _read(self) -> KillSwitchState:
        """Load the state file, or return an armed switch if it does not exist yet.

        A missing file means a never-tripped switch — the safe default for a first run. A file that
        exists but cannot be parsed is *not* treated as armed: an unreadable kill-switch state is
        an operational fault that must be fixed, not silently interpreted as "safe to trade".
        """
        if not self._path.exists():
            return KillSwitchState(tripped=False)
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"kill-switch state file {self._path} is not a JSON object")
        return KillSwitchState.from_json(raw)

    def _write(self, state: KillSwitchState) -> None:
        """Persist `state` atomically, then keep it in memory.

        Written to a temp file in the same directory and `os.replace`d into place so a crash
        mid-write can never leave a half-written state file that reads back as neither tripped nor
        armed — the rename is atomic, so a reader sees the old state or the new, never a torn one.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_json(), indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".killswitch-", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(self._path)
        except BaseException:
            # Do not leave the temp file behind if the replace never happened; suppressing the
            # cleanup's own OSError so it can never mask the original error (CLAUDE.md: fail loud).
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
        self._state = state
