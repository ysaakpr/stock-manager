"""The weekly L0 → S3 mirror job (OPS): `ops/l0-s3-sync.sh sync`, then a sampled `verify`.

The script owns everything that touches the bucket and the SSE-C key: it reads the untracked env
file for the bucket, decodes the key into a pipe for the AWS CLI, refuses an unversioned bucket and
never overwrites a key. This module only runs it on the scheduler's cadence and turns its exit
codes into a job outcome, so a broken mirror reaches `job_run` and `/status/jobs` instead of a
terminal nobody is watching (the mirror sat unsynced from 8 Oct to 6 Nov 2026 because it was
run by hand).

Python never sees the key: it is not read here, not passed in argv (the argv is the script path,
a mode and a sample size) and not in the environment this hands the script, which is an allowlist.
The bucket name carries the AWS account id, so `s3://<bucket>` in anything the AWS CLI prints is
masked before it is logged or raised into `job_run.error`.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

from dataplatform.config import REPO_ROOT, Settings
from dataplatform.logging import get_logger

__all__ = [
    "DEFAULT_SAMPLE",
    "SCRIPT",
    "L0S3SyncError",
    "L0S3SyncResult",
    "redact",
    "run_l0_s3_sync",
]

log = get_logger(__name__)

SCRIPT: Final = REPO_ROOT / "ops" / "l0-s3-sync.sh"

#: Objects re-downloaded and compared by sha256 each week — the runbook's by-hand default.
DEFAULT_SAMPLE: Final = 300

#: The script's exit code for a verify that found only keys S3 lacks: nothing extra, no size or
#: sha256 disagreement, no failed download. Normally L0 files that settled after the sync listed.
VERIFY_ONLY_MISSING: Final = 5

#: Per-step wall budgets. A week of new L0 is a few thousand `aws s3 cp` calls; the 29-day gap of
#: Oct-Nov 2026 (9,470 objects) is the size the sync budget was set against.
SYNC_TIMEOUT: Final = timedelta(hours=3)
VERIFY_TIMEOUT: Final = timedelta(hours=1)

#: What the script is handed from the scheduler's environment. Its config (bucket, key path) comes
#: from the env file it sources itself; these only let it find `aws`, `$HOME` and the instance
#: role, and let a test or an operator point it elsewhere. No `DATABASE_URL`, no API token.
_PASSED_ENV: Final = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TMPDIR")
_PASSED_PREFIXES: Final = ("AWS_", "L0_S3_", "SSE_C_")
_PASSED_EXTRA: Final = ("L0_MANIFEST",)

_S3_URL = re.compile(r"s3://[^/\s'\"]+")
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")

Runner = Callable[..., subprocess.CompletedProcess[str]]


class L0S3SyncError(RuntimeError):
    """The mirror is not known good: a step exited non-zero, or verify disagreed."""


@dataclass(frozen=True, slots=True)
class StepOutcome:
    mode: str
    returncode: int
    summary: str


@dataclass(frozen=True, slots=True)
class L0S3SyncResult:
    """What one job run did: each script step in order, and whether a re-sync was needed."""

    steps: tuple[StepOutcome, ...]

    @property
    def resynced(self) -> bool:
        return sum(step.mode == "sync" for step in self.steps) > 1


def redact(text: str) -> str:
    """`text` with any `s3://<bucket>` and any 12-digit AWS account id masked."""
    return _ACCOUNT_ID.sub("<account>", _S3_URL.sub("s3://<bucket>", text))


def _script_env(environ: Mapping[str, str], data_root: Path) -> dict[str, str]:
    env = {
        name: value
        for name, value in environ.items()
        if name in _PASSED_ENV or name in _PASSED_EXTRA or name.startswith(_PASSED_PREFIXES)
    }
    env["DATA_ROOT"] = str(data_root)
    return env


def _summary(output: str, lines: int = 6) -> str:
    """The last few non-empty lines, redacted — the script ends every mode with its totals."""
    tail = [line for line in output.splitlines() if line.strip()][-lines:]
    return redact("\n".join(tail))


def _step(
    mode: str,
    args: tuple[str, ...],
    *,
    script: Path,
    env: dict[str, str],
    timeout: timedelta,
    runner: Runner,
) -> StepOutcome:
    log.info("l0_s3_sync.step_start", mode=mode)
    try:
        result = runner(
            ["bash", str(script), mode, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout.total_seconds(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise L0S3SyncError(
            f"l0-s3-sync {mode} did not finish within {timeout}; the script's lock is released "
            "with the process, so the next run starts clean"
        ) from None
    outcome = StepOutcome(
        mode=mode,
        returncode=result.returncode,
        summary=_summary(f"{result.stdout or ''}\n{result.stderr or ''}"),
    )
    log.info(
        "l0_s3_sync.step_done", mode=mode, returncode=outcome.returncode, summary=outcome.summary
    )
    return outcome


def run_l0_s3_sync(
    settings: Settings,
    *,
    sample: int = DEFAULT_SAMPLE,
    script: Path = SCRIPT,
    environ: Mapping[str, str] | None = None,
    runner: Runner | None = None,
) -> L0S3SyncResult:
    """Upload the L0 files the mirror lacks, then verify the mirror on a random sample.

    What it does: `sync`; then `verify <sample>`. When verify reports only keys S3 lacks (exit 5 —
    files that settled after the sync listed), one more `sync` and `verify`. Any other non-zero
    exit, or a second verify that is not clean, raises `L0S3SyncError` with the step's redacted
    totals, so the run is FAILED and `failure_alerts` pages it.
    What it assumes: `settings.data_root` is the real lake; the script's own config (env file, key
    and fingerprint) is in place on this host, and the host has the instance role.
    What it never does: read the SSE-C key, put it or the bucket in argv, a log line or an error,
    or re-run a verify that found extra objects, a size or sha256 disagreement or a failed
    download — that is an incident for the owner, and a second random sample could miss it.
    """
    env = _script_env(os.environ if environ is None else environ, settings.data_root)
    run_script: Runner = subprocess.run if runner is None else runner
    steps: list[StepOutcome] = []

    def run(mode: str, *args: str) -> StepOutcome:
        timeout = SYNC_TIMEOUT if mode == "sync" else VERIFY_TIMEOUT
        outcome = _step(mode, args, script=script, env=env, timeout=timeout, runner=run_script)
        steps.append(outcome)
        return outcome

    def fail(outcome: StepOutcome) -> L0S3SyncError:
        return L0S3SyncError(
            f"l0-s3-sync {outcome.mode} exited {outcome.returncode}:\n{outcome.summary}"
        )

    for attempt in (1, 2):
        synced = run("sync")
        if synced.returncode != 0:
            raise fail(synced)
        verified = run("verify", str(sample))
        if verified.returncode == 0:
            log.info("l0_s3_sync.verified", attempts=attempt)
            return L0S3SyncResult(tuple(steps))
        if verified.returncode != VERIFY_ONLY_MISSING or attempt == 2:
            raise fail(verified)
        log.warning("l0_s3_sync.resync", reason="verify found only keys S3 lacks")
    raise AssertionError("unreachable")  # pragma: no cover
