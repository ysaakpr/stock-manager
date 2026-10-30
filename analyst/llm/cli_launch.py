"""Launching the `claude` CLI: a bounded retry for an executable that is briefly not there.

The CLI is an npm global install, and npm replaces it in place: `npm install -g` *retires* the old
package directory and the `bin/claude` symlink (renames both aside) before it links the new ones,
so for a second or two there is no `claude` to exec. Claude Code's own auto-updater runs that
install from the interactive sessions on this host, every 30 minutes per session. On 2026-10-29
one landed at 22:24:52 IST, one second before the M17 job's LLM stages: every commons digest and
all four managers' round-0 calls failed with ENOENT inside half a second, and the session was lost.

So a launch that failed because the executable was missing (ENOENT, which is also what a symlink
to a not-yet-linked target gives) or was still being written (ETXTBSY) is retried a bounded number
of times after a growing delay. Nothing else is. A CLI that ran and exited non-zero, a timeout and
unparsable output all may have reached the model, and whether to ask again is the caller's call.

A retry cannot send a prompt twice. `subprocess.run` writes stdin only after `Popen` has returned,
and `Popen` raises an exec failure from its constructor, before a byte of input is written: a
launch that failed never reached the model.
"""

from __future__ import annotations

import errno
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from dataplatform.logging import get_logger

__all__ = ["CLI_LAUNCH_RETRY_DELAYS_SECONDS", "is_cli_launch_gap", "run_claude_cli"]

log = get_logger(__name__)

#: The waits before each relaunch: three retries, 35 s in all. An npm relink leaves a gap of a
#: second or two, so the first retry alone clears it; the rest cover a slow registry or disk.
#: Bounded because a CLI that is really gone has to fail loud, not stall the daily loop.
CLI_LAUNCH_RETRY_DELAYS_SECONDS: Final[tuple[float, ...]] = (5.0, 10.0, 20.0)

#: The errnos that mean "the executable is mid-replacement", not "the executable is broken".
_GAP_ERRNOS: Final[frozenset[int]] = frozenset({errno.ENOENT, errno.ETXTBSY})


def is_cli_launch_gap(error: OSError) -> bool:
    """Whether `error` is the exec failure an in-place reinstall produces (ENOENT or ETXTBSY)."""
    return error.errno in _GAP_ERRNOS


def run_claude_cli(
    argv: Sequence[str],
    *,
    stdin: str,
    timeout_seconds: float,
    env: Mapping[str, str],
    cwd: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retry_delays: Sequence[float] = CLI_LAUNCH_RETRY_DELAYS_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run the CLI once to completion, relaunching only when the launch itself never happened.

    What it does: `subprocess.run` with stdin, captured text output and no `check`. When that
    raises an `OSError` that `is_cli_launch_gap` accepts, it logs `llm.cli_missing_retry` (attempt,
    delay) and launches again after the next of `retry_delays`.
    What it assumes: `argv` was built by the caller and is never shell-interpreted; `env` is the
    allowlisted environment (`claude_cli_env`), whose `PATH` is where `argv[0]` is looked up.
    What it never does: retry a process that started (a non-zero exit is returned as is, and a
    `subprocess.TimeoutExpired` propagates at once), retry any other `OSError`, or swallow the
    last launch failure: once the delays are spent, the final `OSError` is raised unchanged for
    the caller to turn into its own error.
    """
    delays = tuple(retry_delays)
    attempt = 1
    while True:
        try:
            return subprocess.run(
                list(argv),
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                cwd=cwd,
                env=dict(env),
            )
        except OSError as error:
            if not is_cli_launch_gap(error) or attempt > len(delays):
                raise
            delay = delays[attempt - 1]
            log.warning(
                "llm.cli_missing_retry",
                executable=argv[0],
                errno=errno.errorcode.get(error.errno or 0, str(error.errno)),
                attempt=attempt,
                attempts=len(delays) + 1,
                delay_seconds=delay,
            )
            sleep(delay)
            attempt += 1
