"""`ops/scheduler.sh` puts the nvm-installed `claude` CLI on PATH for the M17 managers.

A systemd user unit's PATH reaches neither `~/.local/bin` (uv) nor `~/.nvm/.../bin` (claude); the
first live dry session decided with the CLI only from an interactive shell. These tests run the real
script with a fake HOME and a fake `uv` that prints the PATH it was handed instead of starting the
scheduler.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "scheduler.sh"


def _fake_uv(tmp_path: Path) -> Path:
    uv = tmp_path / "uv"
    uv.write_text('#!/usr/bin/env bash\necho "PATH=$PATH"\n', encoding="utf-8")
    uv.chmod(0o755)
    return uv


def _run(tmp_path: Path, home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "UV_BIN": str(_fake_uv(tmp_path)),
        "SCHEDULER_REPO": str(tmp_path),
        "DATA_ROOT": str(tmp_path / "data"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=True, timeout=30
    )


def test_newest_nvm_claude_is_put_on_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for version in ("v20.1.0", "v24.19.0"):
        bin_dir = home / ".nvm" / "versions" / "node" / version / "bin"
        bin_dir.mkdir(parents=True)
        claude = bin_dir / "claude"
        claude.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        claude.chmod(0o755)
    result = _run(tmp_path, home)
    path_line = next(line for line in result.stdout.splitlines() if line.startswith("PATH="))
    first = path_line.removeprefix("PATH=").split(os.pathsep)[0]
    assert first.endswith("v24.19.0/bin")


def test_missing_claude_warns_but_still_starts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = _run(tmp_path, home)
    assert "no claude CLI found" in result.stderr
    assert any(line.startswith("PATH=/usr/bin:/bin") for line in result.stdout.splitlines())
