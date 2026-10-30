"""Launching the `claude` CLI: an executable that is briefly missing is relaunched, nothing else is.

On 2026-10-29 an npm reinstall of the CLI (Claude Code's own auto-updater) retired `bin/claude` one
second before the M17 job's LLM stages, and every call failed ENOENT inside half a second. These
tests pin the repair: a launch that failed ENOENT or ETXTBSY is retried after a bounded, growing
delay and logged; a process that ran (non-zero exit, timeout) and any other `OSError` are not; and
when every attempt fails the original `OSError` surfaces for the caller to turn into its error.

Offline: `subprocess.run` is faked, except in the one test that execs a local `/bin/sh` script
through a real dangling symlink, which is the state npm leaves `bin/claude` in mid-install.
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from analyst.llm import CLI_LAUNCH_RETRY_DELAYS_SECONDS, is_cli_launch_gap, run_claude_cli

ARGV = ("claude", "--print")
ENV = {"PATH": "/nonexistent"}


def _enoent() -> OSError:
    return FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), "claude")


def _etxtbsy() -> OSError:
    return OSError(errno.ETXTBSY, os.strerror(errno.ETXTBSY), "claude")


class SequencedRun:
    """Stands in for `subprocess.run`: raises each queued launch error in turn, then completes."""

    def __init__(self, errors: Sequence[BaseException], *, returncode: int = 0) -> None:
        self.errors = list(errors)
        self.returncode = returncode
        self.calls = 0
        self.stdins: list[str | None] = []

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        self.stdins.append(kwargs.get("input"))
        return subprocess.CompletedProcess(list(argv), self.returncode, "out", "err")


def _install(monkeypatch: pytest.MonkeyPatch, fake: SequencedRun) -> SequencedRun:
    monkeypatch.setattr("analyst.llm.cli_launch.subprocess.run", fake)
    return fake


def _run(sleeps: list[float]) -> subprocess.CompletedProcess[str]:
    return run_claude_cli(ARGV, stdin="prompt", timeout_seconds=1.0, env=ENV, sleep=sleeps.append)


def test_the_backoff_is_three_retries_and_bounded() -> None:
    """Long enough to outlast an npm relink, short enough that a CLI truly gone fails loud."""
    assert CLI_LAUNCH_RETRY_DELAYS_SECONDS == (5.0, 10.0, 20.0)
    assert sum(CLI_LAUNCH_RETRY_DELAYS_SECONDS) <= 60


def test_a_missing_executable_is_relaunched_until_it_is_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails if the retry is removed: the first ENOENT would propagate."""
    fake = _install(monkeypatch, SequencedRun([_enoent(), _enoent()]))
    sleeps: list[float] = []
    with capture_logs() as logs:
        finished = _run(sleeps)

    assert finished.returncode == 0 and finished.stdout == "out"
    assert fake.calls == 3
    # The prompt reached exactly one process: the launches that failed never read stdin.
    assert fake.stdins == ["prompt"]
    assert sleeps == [5.0, 10.0]
    retries = [e for e in logs if e["event"] == "llm.cli_missing_retry"]
    assert [(e["attempt"], e["delay_seconds"]) for e in retries] == [(1, 5.0), (2, 10.0)]
    assert all(e["log_level"] == "warning" and e["errno"] == "ENOENT" for e in retries)
    assert all(e["executable"] == "claude" and e["attempts"] == 4 for e in retries)


def test_an_executable_still_being_written_is_relaunched(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, SequencedRun([_etxtbsy()]))
    sleeps: list[float] = []
    assert _run(sleeps).returncode == 0
    assert fake.calls == 2 and sleeps == [5.0]


def test_an_executable_that_never_comes_back_raises_after_the_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, SequencedRun([_enoent() for _ in range(10)]))
    sleeps: list[float] = []
    with capture_logs() as logs, pytest.raises(FileNotFoundError) as caught:
        _run(sleeps)

    assert caught.value.errno == errno.ENOENT
    assert fake.calls == 4, "one launch plus three retries, and no more"
    assert sleeps == [5.0, 10.0, 20.0]
    assert [e["attempt"] for e in logs if e["event"] == "llm.cli_missing_retry"] == [1, 2, 3]


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EACCES, os.strerror(errno.EACCES), "claude"),
        OSError(errno.ENOEXEC, os.strerror(errno.ENOEXEC), "claude"),
        OSError(errno.ENOMEM, os.strerror(errno.ENOMEM)),
    ],
    ids=["EACCES", "ENOEXEC", "ENOMEM"],
)
def test_any_other_launch_error_is_raised_at_once(
    monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    fake = _install(monkeypatch, SequencedRun([error]))
    sleeps: list[float] = []
    with pytest.raises(OSError) as caught:
        _run(sleeps)
    assert caught.value is error
    assert fake.calls == 1 and sleeps == []


def test_a_process_that_ran_and_failed_is_returned_not_relaunched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero exit means the CLI started and may have reached the model: never repeat it."""
    fake = _install(monkeypatch, SequencedRun([], returncode=1))
    sleeps: list[float] = []
    assert _run(sleeps).returncode == 1
    assert fake.calls == 1 and sleeps == []


def test_a_timeout_is_not_relaunched(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, SequencedRun([subprocess.TimeoutExpired("claude", 1.0)]))
    sleeps: list[float] = []
    with pytest.raises(subprocess.TimeoutExpired):
        _run(sleeps)
    assert fake.calls == 1 and sleeps == []


def test_the_classifier_accepts_only_the_reinstall_errnos() -> None:
    """Inverting or widening the classification fails here, not in production."""
    assert is_cli_launch_gap(_enoent()) and is_cli_launch_gap(_etxtbsy())
    assert not is_cli_launch_gap(PermissionError(errno.EACCES, "denied"))
    assert not is_cli_launch_gap(OSError(errno.EIO, "io"))
    assert not is_cli_launch_gap(OSError("no errno"))


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX /bin/sh")
def test_a_real_dangling_bin_symlink_is_relaunched_once_npm_relinks_it(tmp_path: Path) -> None:
    """The real OS state, not a hand-made exception: `bin/claude` points at a package npm has
    retired, so exec through `PATH` fails ENOENT; the "relink" lands during the first backoff."""
    bin_dir, package = tmp_path / "bin", tmp_path / "lib" / "claude-code"
    bin_dir.mkdir()
    package.mkdir(parents=True)
    target = package / "claude.exe"
    (bin_dir / "claude").symlink_to(target)
    sleeps: list[float] = []

    def relink(delay: float) -> None:
        # Builtins only: the PATH below holds nothing but the bin dir under test.
        target.write_text('#!/bin/sh\nread -r line\nprintf "%s" "$line"\n', encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IXUSR)
        sleeps.append(delay)

    with capture_logs() as logs:
        finished = run_claude_cli(
            ["claude"],
            stdin="ping\n",
            timeout_seconds=10.0,
            env={"PATH": str(bin_dir)},
            sleep=relink,
        )

    assert finished.returncode == 0 and finished.stdout == "ping"
    assert sleeps == [5.0]
    assert [e["errno"] for e in logs if e["event"] == "llm.cli_missing_retry"] == ["ENOENT"]
