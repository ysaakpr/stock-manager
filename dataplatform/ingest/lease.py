"""D1: one request budget per host, enforced rather than agreed (2026-09-06 audit, finding N7).

`RateLimiter` spaces requests 2-3 s apart per host and its own docstring states the assumption it
rests on: *"it assumes it is the only gate in front of the transport — spacing enforced anywhere
else is spacing that a second caller can skip."* That held when one process fetched. It stopped
holding when the development model became two machines, both able to run drivers against
`nsearchives.nseindia.com`, with the guard written down as a **procedure**:

> One request budget per host: before starting a fetch campaign on the server, make sure no driver
> runs on the laptop against the same host, and the other way round.

Nothing enforced it. Two concurrent drivers halve the effective spacing on the one host that
carries prices, delivery, corporate actions and fundamentals — which is the politeness policy being
circumvented through a side channel rather than changed, and the penalty for getting it wrong is a
403 spike on everything at once (§4.1, AGENTIC_CONTEXT §8). This module is the lock the procedure
was standing in for.

**What it is.** A lease file per host under `<data_root>/.host-lease/`, taken with `O_EXCL` before
the first request and released on exit. A second driver on the same machine cannot take it and
exits naming the holder — its pid, its command and when it started — rather than quietly doubling
the request rate.

**What it is not.** A distributed lock. The laptop and the server share only git and ssh, and a
lock that needs a network round trip before every campaign would fail closed on a laptop with no
server configured, which is worse than what it prevents. The cross-machine half is enforced where
the two machines already talk: `ops/remote.sh` refuses to start a driver on the server while one
runs, and (with this module) a local driver refuses while the server has one. That is two honest
checks at the two places a human actually starts a driver, not a fiction of a global mutex.

**Staleness.** A driver that is killed leaves its file behind, and a lease nobody can break is an
outage. So a lease is breakable on two independent grounds — the holder's pid is gone, or the lease
is older than its TTL — and breaking one is logged at WARNING with what was broken. The TTL is
generous because these are hour-scale campaigns; the pid check is what makes the common case fast.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from dataplatform.clock import Clock
from dataplatform.config import get_settings
from dataplatform.logging import get_logger

__all__ = [
    "DEFAULT_TTL",
    "LEASE_DIRNAME",
    "HostBusyError",
    "HostLeaseError",
    "LeaseHolder",
    "host_lease",
    "lease_dir",
    "lease_path",
    "read_lease",
]

_LOG = get_logger(__name__)

#: Where leases live. Under the lake root rather than `/tmp` so a lease survives nothing that the
#: data does not, and so a machine with two checkouts pointing at one lake shares one budget.
LEASE_DIRNAME: Final = ".host-lease"

#: How long a lease may go unrefreshed before another driver may break it. Campaigns here run for
#: hours (the integrated-filing sweep ran ~5), so this is deliberately longer than any of them; the
#: pid check is what releases a lease promptly in the ordinary crash case.
DEFAULT_TTL: Final = timedelta(hours=12)


class HostLeaseError(RuntimeError):
    """Base for lease failures, so a driver can catch the layer without catching the world."""


class HostBusyError(HostLeaseError):
    """Another driver holds this host's request budget. Carries the holder, not just a message."""

    def __init__(self, holder: LeaseHolder) -> None:
        self.holder = holder
        super().__init__(
            f"{holder.host}: another driver holds this host's request budget — "
            f"pid {holder.pid} on {holder.machine}, started {holder.started_at.isoformat()}, "
            f"running {holder.command!r}. Two drivers against one host halve the spacing the "
            f"crawl policy promises, so this one is not starting. Wait for it, or — if it is "
            f"gone — the lease frees itself when its pid dies or after "
            f"{int(DEFAULT_TTL.total_seconds() // 3600)}h."
        )


@dataclass(frozen=True, slots=True)
class LeaseHolder:
    """Who holds one host's request budget, in enough detail to go and look at them."""

    host: str
    pid: int
    machine: str
    command: str
    started_at: datetime

    def as_json(self) -> str:
        """The file's contents. Sorted keys so a lease file diffs cleanly."""
        return json.dumps(
            {
                "host": self.host,
                "pid": self.pid,
                "machine": self.machine,
                "command": self.command,
                "started_at": self.started_at.isoformat(),
            },
            sort_keys=True,
        )

    @classmethod
    def of(cls, payload: str) -> LeaseHolder:
        """Parse a lease file. Raises `HostLeaseError` on anything that is not one."""
        try:
            data = json.loads(payload)
            return cls(
                host=str(data["host"]),
                pid=int(data["pid"]),
                machine=str(data["machine"]),
                command=str(data["command"]),
                started_at=datetime.fromisoformat(str(data["started_at"])),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise HostLeaseError(f"lease file is not a lease: {exc}") from exc


def lease_dir(data_root: Path | None = None) -> Path:
    """The directory holding every host's lease for one lake."""
    root = get_settings().data_root if data_root is None else data_root
    return root / LEASE_DIRNAME


def lease_path(host: str, *, data_root: Path | None = None) -> Path:
    """Where `host`'s lease lives. The host is used verbatim — it is a DNS name, not user input."""
    return lease_dir(data_root) / f"{host}.json"


def read_lease(host: str, *, data_root: Path | None = None) -> LeaseHolder | None:
    """Who holds `host` right now, or None. Read-only — never breaks a lease, never takes one."""
    path = lease_path(host, data_root=data_root)
    try:
        return LeaseHolder.of(path.read_text())
    except FileNotFoundError:
        return None
    except HostLeaseError:
        # A corrupt lease is a held lease as far as reading goes: refusing to guess is the point.
        return None


@contextmanager
def host_lease(
    host: str,
    *,
    clock: Clock,
    command: str,
    data_root: Path | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> Iterator[LeaseHolder]:
    """Hold `host`'s request budget for the duration of the block, or refuse to start.

    What it does: creates `<data_root>/.host-lease/<host>.json` with `O_EXCL` and removes it on the
    way out, so exactly one driver per lake is fetching from one host at a time.
    What it assumes: the caller is about to make requests. Take it once around a campaign, not
    around each request — a lease per request would serialise nothing and cost a syscall each time.
    What it never does: wait. A driver that queues behind another for six hours is a driver nobody
    is watching when it finally starts; refusing with the holder named is the useful answer.

    Raises `HostBusyError` when a live lease exists. Breaks a lease whose pid is gone or whose age
    exceeds `ttl`, logging what it broke.
    """
    path = lease_path(host, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = LeaseHolder(
        host=host,
        pid=os.getpid(),
        machine=socket.gethostname(),
        command=command,
        started_at=clock.now(),
    )
    _take(path, holder, clock=clock, ttl=ttl)
    _LOG.info(
        "crawl.lease_taken",
        host=host,
        pid=holder.pid,
        machine=holder.machine,
        command=command,
        path=str(path),
    )
    try:
        yield holder
    finally:
        _release(path, holder)


def _take(path: Path, holder: LeaseHolder, *, clock: Clock, ttl: timedelta) -> None:
    """Create the lease file, breaking a dead or expired one first. Raises if a live one exists."""
    try:
        _create(path, holder)
        return
    except FileExistsError:
        pass

    existing = read_lease(holder.host, data_root=path.parent.parent)
    if existing is not None and not _is_breakable(existing, clock=clock, ttl=ttl):
        raise HostBusyError(existing)

    reason = "unreadable" if existing is None else _break_reason(existing, clock=clock, ttl=ttl)
    _LOG.warning(
        "crawl.lease_broken",
        host=holder.host,
        reason=reason,
        previous_pid=None if existing is None else existing.pid,
        previous_machine=None if existing is None else existing.machine,
        previous_started_at=None if existing is None else existing.started_at.isoformat(),
    )
    path.unlink(missing_ok=True)
    try:
        _create(path, holder)
    except FileExistsError as exc:  # another driver won the same race
        current = read_lease(holder.host, data_root=path.parent.parent)
        raise (HostBusyError(current) if current else HostLeaseError(str(exc))) from exc


def _create(path: Path, holder: LeaseHolder) -> None:
    """Write the lease with `O_EXCL`, so two processes racing produce one winner and one error."""
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(holder.as_json())
        handle.flush()
        os.fsync(handle.fileno())


def _release(path: Path, holder: LeaseHolder) -> None:
    """Remove our own lease, and only ours.

    Checked rather than assumed: if a long campaign overran its TTL and another driver legitimately
    broke and re-took the lease, this one must not delete the new holder's file on its way out.
    """
    current = read_lease(holder.host, data_root=path.parent.parent)
    if current is not None and (current.pid, current.machine) != (holder.pid, holder.machine):
        _LOG.warning(
            "crawl.lease_not_ours",
            host=holder.host,
            held_by_pid=current.pid,
            held_by_machine=current.machine,
        )
        return
    path.unlink(missing_ok=True)
    _LOG.info("crawl.lease_released", host=holder.host, pid=holder.pid)


def _is_breakable(holder: LeaseHolder, *, clock: Clock, ttl: timedelta) -> bool:
    """Whether this lease may be taken over — its process is gone, or it is older than the TTL."""
    return _break_reason(holder, clock=clock, ttl=ttl) is not None


def _break_reason(holder: LeaseHolder, *, clock: Clock, ttl: timedelta) -> str | None:
    """Why this lease may be broken, or None when it may not."""
    if clock.now() - holder.started_at > ttl:
        return "expired"
    if holder.machine == socket.gethostname() and not _pid_alive(holder.pid):
        return "holder is gone"
    return None


def _pid_alive(pid: int) -> bool:
    """Whether a pid on *this* machine is running. Meaningless for another machine's pid.

    `signal 0` asks the kernel about the process without touching it. `PermissionError` means the
    process exists and belongs to someone else — alive, and not ours to break on that ground.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
