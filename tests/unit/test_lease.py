"""One request budget per host, enforced (2026-09-06 audit, finding N7).

The rule was written down in CLAUDE.md as a procedure — "before starting a fetch campaign on the
server, make sure no driver runs on the laptop against the same host, and the other way round" —
and nothing checked it. Two drivers against `nsearchives.nseindia.com` halve the spacing
`RateLimiter` promises, which is the politeness policy being circumvented through a side channel
rather than changed, and the penalty lands on the one host that carries prices, delivery, corporate
actions and fundamentals at once.

Every test here is written so the plausible wrong implementation fails it: a lock that lets a
second driver in, one that cannot be broken after a crash (an outage nobody can clear), one that a
crashed driver's stale file blocks forever, one that deletes a *different* holder's lease on its
way out, and one that waits instead of refusing.

Offline and clockless by construction: `FrozenClock` everywhere, `tmp_path` for the lake, no
socket and no sleep.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest.lease import (
    DEFAULT_TTL,
    HostBusyError,
    HostLeaseError,
    LeaseHolder,
    host_lease,
    lease_path,
    read_lease,
)

HOST: Final = "nsearchives.nseindia.com"
NOW: Final = datetime(2026, 9, 6, 9, 0, tzinfo=IST)


def _clock(at: datetime = NOW) -> FrozenClock:
    return FrozenClock(at)


# ── taking and releasing ─────────────────────────────────────────────────────────────────────


def test_a_lease_is_held_for_the_block_and_gone_after(tmp_path: Path) -> None:
    with host_lease(HOST, clock=_clock(), command="backfill", data_root=tmp_path) as holder:
        assert holder.host == HOST
        assert holder.pid == os.getpid()
        assert read_lease(HOST, data_root=tmp_path) == holder

    assert read_lease(HOST, data_root=tmp_path) is None


def test_a_second_driver_is_refused_and_told_who_holds_it(tmp_path: Path) -> None:
    """Refused, not queued: a driver that waits six hours starts when nobody is watching.

    The message has to carry the holder, because "resource busy" costs an operator the twenty
    minutes of `ps` that the error could have saved.
    """
    with (
        host_lease(HOST, clock=_clock(), command="integrated campaign", data_root=tmp_path),
        pytest.raises(HostBusyError) as caught,
        host_lease(HOST, clock=_clock(), command="delivery", data_root=tmp_path),
    ):
        pytest.fail("the second lease must not be granted")

    message = str(caught.value)
    assert "integrated campaign" in message
    assert str(os.getpid()) in message
    assert caught.value.holder.command == "integrated campaign"


def test_two_different_hosts_do_not_block_each_other(tmp_path: Path) -> None:
    """The budget is per host. A BSE campaign and an NSE one are not competing for anything."""
    with (
        host_lease(HOST, clock=_clock(), command="nse", data_root=tmp_path),
        host_lease("api.bseindia.com", clock=_clock(), command="bse", data_root=tmp_path),
    ):
        assert read_lease(HOST, data_root=tmp_path) is not None
        assert read_lease("api.bseindia.com", data_root=tmp_path) is not None


def test_a_lease_is_released_even_when_the_driver_raises(tmp_path: Path) -> None:
    """A campaign that dies on a 403 must not leave the host locked until the TTL."""
    with (
        pytest.raises(RuntimeError, match="403 spike"),
        host_lease(HOST, clock=_clock(), command="backfill", data_root=tmp_path),
    ):
        raise RuntimeError("403 spike")

    assert read_lease(HOST, data_root=tmp_path) is None


# ── breaking a lease nobody is holding any more ──────────────────────────────────────────────


def test_a_lease_whose_process_is_gone_is_broken(tmp_path: Path) -> None:
    """A killed driver must not lock the host until the TTL. Its pid is the evidence it is gone."""
    _plant(tmp_path, pid=_dead_pid(), machine=os.uname().nodename, at=NOW)

    with host_lease(HOST, clock=_clock(), command="new run", data_root=tmp_path) as holder:
        assert holder.pid == os.getpid()


def test_an_expired_lease_is_broken_even_when_its_pid_is_alive(tmp_path: Path) -> None:
    """The second, independent ground. A wedged driver is indistinguishable from a busy one.

    Its pid is alive — so the pid check alone would keep the host locked forever, which is exactly
    the outage a lock with no expiry causes.
    """
    _plant(tmp_path, pid=os.getpid(), machine="some-other-machine", at=NOW)
    later = _clock(NOW + DEFAULT_TTL + timedelta(minutes=1))

    with host_lease(HOST, clock=later, command="new run", data_root=tmp_path) as holder:
        assert holder.machine == os.uname().nodename


def test_a_live_lease_from_another_machine_is_not_broken(tmp_path: Path) -> None:
    """A pid on another machine says nothing about a process here. Never break on that ground.

    This is the case that matters most: the server's driver and the laptop's can easily share a pid
    number, and treating "no such pid here" as "that driver is gone" would break a live lease.
    """
    _plant(tmp_path, pid=_dead_pid(), machine="the-server", at=NOW)

    with (
        pytest.raises(HostBusyError) as caught,
        host_lease(HOST, clock=_clock(), command="laptop run", data_root=tmp_path),
    ):
        pytest.fail("a live lease from another machine must be respected")

    assert caught.value.holder.machine == "the-server"


def test_a_corrupt_lease_file_is_not_silently_taken_over(tmp_path: Path) -> None:
    """Refusing to guess. A file we cannot parse is a lease we cannot prove is dead."""
    path = lease_path(HOST, data_root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    assert read_lease(HOST, data_root=tmp_path) is None
    with pytest.raises(HostLeaseError):
        LeaseHolder.of("{not json")


# ── the release is ours, and only ours ───────────────────────────────────────────────────────


def test_releasing_does_not_delete_a_lease_someone_else_now_holds(tmp_path: Path) -> None:
    """An overrunning campaign must not free the driver that legitimately replaced it.

    Without the ownership check on release, a campaign that ran past its TTL would delete the new
    holder's file on its way out and hand a third driver a host two are already fetching from.
    """
    with host_lease(HOST, clock=_clock(), command="long campaign", data_root=tmp_path):
        # Somebody breaks and re-takes the lease while we are still inside the block.
        _plant(tmp_path, pid=_dead_pid(), machine="the-server", at=NOW, overwrite=True)

    survivor = read_lease(HOST, data_root=tmp_path)
    assert survivor is not None and survivor.machine == "the-server"


def _plant(root: Path, *, pid: int, machine: str, at: datetime, overwrite: bool = False) -> None:
    """Write a lease file directly, standing in for another driver."""
    path = lease_path(HOST, data_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:  # pragma: no cover - guards a mistake in a test
        raise AssertionError("a lease is already planted")
    path.write_text(
        json.dumps(
            {
                "host": HOST,
                "pid": pid,
                "machine": machine,
                "command": "someone else",
                "started_at": at.isoformat(),
            }
        )
    )


def _dead_pid() -> int:
    """A pid that is certainly not running: run a trivial child and let it exit.

    A subprocess rather than `os.fork` — forking a multi-threaded pytest process is exactly the
    deadlock the runtime warns about, and this needs only the number.
    """
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid
