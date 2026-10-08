"""The secret-scanning gate (M15.1, invariant #13, HUMAN_DECISIONS D5).

Every planted credential is assembled at runtime from fragments and a hash, so this file holds no
literal that trips the scanner it tests — or GitHub push protection. Each test drives the real
`ops/secret_scan.py` in a subprocess against the repo's real `.secrets.baseline`, so a weakened
baseline (a dropped detector, a widened allowlist) fails here, not only in review.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCANNER = REPO / "ops" / "secret_scan.py"
BASELINE = REPO / ".secrets.baseline"
SCAN_CMD = "uv run python ops/secret_scan.py"


def _fake(seed: str, length: int) -> str:
    """A deterministic credential-shaped value (hex, letters and digits) — never a literal."""
    out = ""
    while len(out) < length:
        out += hashlib.sha256(f"{seed}:{len(out)}".encode()).hexdigest()
    return out[:length]


def _scan(*args: str | Path, baseline: Path = BASELINE) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), "--baseline", str(baseline), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
    )


KITE_KEY = _fake("kite-key", 16)
KITE_SECRET = _fake("kite-secret", 32)
KITE_TOKEN = _fake("kite-token", 32)
DSN = "postgresql://" + "trader:" + _fake("pg", 20) + "@db.internal:5432/trading"
AWS_KEY = "AKIA" + _fake("aws", 16).upper()
PEM_HEADER = "-----BEGIN " + "RSA " + "PRIVATE KEY-----"
ANTHROPIC = "sk-" + "ant-" + "api03-" + _fake("anthropic", 90)

PLANTED = {
    "kite_api_key_py": ("kite.py", 'KITE_API_KEY = "' + KITE_KEY + '"\n'),
    "kite_api_secret_py": ("kite.py", 'kite_api_secret = "' + KITE_SECRET + '"\n'),
    "kite_api_secret_env": ("kite.env", "KITE_API_SECRET=" + KITE_SECRET + "\n"),
    "kite_access_token_py": ("kite.py", 'access_token = "' + KITE_TOKEN + '"\n'),
    "kite_access_token_env": ("kite.env", "KITE_ACCESS_TOKEN=" + KITE_TOKEN + "\n"),
    "kite_access_token_yaml": ("kite.yaml", "kite:\n  access_token: " + KITE_TOKEN + "\n"),
    "postgres_dsn_py": ("settings.py", 'DATABASE_URL = "' + DSN + '"\n'),
    "postgres_dsn_env": ("db.env", "DATABASE_URL=" + DSN + "\n"),
    "aws_access_key": ("aws.py", 'region = "ap-south-1"\nkey_id = "' + AWS_KEY + '"\n'),
    "private_key": ("id_rsa", PEM_HEADER + "\n" + _fake("pem", 64) + "\n"),
    "anthropic_key_in_prose": ("notes.md", "The key was " + ANTHROPIC + " until rotated.\n"),
}

CLEAN = """\
from pydantic import SecretStr

class Settings:
    kite_api_key: SecretStr
    kite_api_secret: SecretStr
    kite_access_token: SecretStr | None = None  # set in .env, never here

def connect(settings: Settings) -> None:
    api_key = settings.kite_api_key.get_secret_value()
    access_token = os.environ["KITE_ACCESS_TOKEN"]
    sha256 = "{digest}"  # content addresses are not credentials
""".format(digest=hashlib.sha256(b"x").hexdigest())


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_planted_credential_fails_the_scan(case: str, tmp_path: Path) -> None:
    name, content = PLANTED[case]
    target = tmp_path / name
    target.write_text(content)

    result = _scan(target)

    assert result.returncode == 1, f"{case} passed the scan:\n{result.stdout}{result.stderr}"
    assert name in result.stderr
    # The report locates the finding; it must never become the leak itself.
    for value in (KITE_KEY, KITE_SECRET, KITE_TOKEN, DSN, AWS_KEY, ANTHROPIC):
        assert value not in result.stdout + result.stderr


def test_clean_file_passes(tmp_path: Path) -> None:
    target = tmp_path / "clean.py"
    target.write_text(CLEAN)

    result = _scan(target)

    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_commit_message_is_scanned(tmp_path: Path) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("[M8.3] Wire Kite\n\nused KITE_ACCESS_TOKEN=" + KITE_TOKEN + " to test\n")

    assert _scan("--message", message).returncode == 1

    message.write_text("[M8.3] Wire Kite\n\nThe token is read from the environment.\n")
    assert _scan("--message", message).returncode == 0


def test_inline_pragma_cannot_silence_a_finding(tmp_path: Path) -> None:
    """`pragma: allowlist secret` is an allowlist entry nobody reviews; the wrapper ignores it."""
    target = tmp_path / "kite.py"
    target.write_text('api_secret = "' + KITE_SECRET + '"  # pragma: allowlist ' + "secret\n")
    baseline = tmp_path / "baseline.json"
    config = json.loads(BASELINE.read_text())
    config["filters_used"].append({"path": "detect_secrets.filters.allowlist.is_line_allowlisted"})
    baseline.write_text(json.dumps(config))

    assert _scan(target, baseline=baseline).returncode == 1


def test_baseline_without_a_required_detector_is_a_config_error(tmp_path: Path) -> None:
    """A baseline that drops a detector must not quietly turn into a weaker, passing scan."""
    target = tmp_path / "clean.py"
    target.write_text(CLEAN)
    config = json.loads(BASELINE.read_text())
    config["plugins_used"] = [p for p in config["plugins_used"] if p["name"] != "KeywordDetector"]
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(config))

    result = _scan(target, baseline=baseline)

    assert result.returncode == 2
    assert "KeywordDetector" in result.stderr


def test_baseline_has_no_entropy_or_verification_and_only_reviewed_entries() -> None:
    """The allowlist stays narrow: path-keyed entries, no blanket directory, offline settings."""
    config = json.loads(BASELINE.read_text())
    plugins = {p["name"] for p in config["plugins_used"]}
    filters = {f["path"] for f in config["filters_used"]}

    assert "HexHighEntropyString" not in plugins
    assert "Base64HighEntropyString" not in plugins
    assert not any("verification" in f or "allowlist" in f for f in filters)
    assert not any(f.endswith("should_exclude_file") for f in filters)
    for path, entries in config["results"].items():
        assert (REPO / path).is_file(), f"baseline entry for a missing file: {path}"
        assert entries, path


def test_accept_is_pinned_to_path_and_value(tmp_path: Path) -> None:
    """--accept allowlists that value in that file — not the same value elsewhere, not a new one."""
    _git(tmp_path, "init", "-q")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(BASELINE.read_text())
    reviewed = tmp_path / "docs.md"
    reviewed.write_text("example: " + DSN + "\n")
    elsewhere = tmp_path / "app.py"
    elsewhere.write_text('DSN = "' + DSN + '"\n')

    cmd = [sys.executable, str(SCANNER), "--baseline", str(baseline), "--repo", str(tmp_path)]
    accept = subprocess.run(
        [*cmd, "--accept", str(reviewed)], capture_output=True, text=True, check=False
    )
    assert accept.returncode == 0, accept.stderr
    before = json.loads(BASELINE.read_text())["results"]
    after = json.loads(baseline.read_text())["results"]
    assert set(after) == set(before) | {"docs.md"}, "accept must only append"

    def scan(path: Path) -> int:
        return subprocess.run([*cmd, str(path)], capture_output=True, check=False).returncode

    assert scan(reviewed) == 0
    assert scan(elsewhere) == 1
    reviewed.write_text("example: " + DSN + "\nKITE_ACCESS_TOKEN=" + KITE_TOKEN + "\n")
    assert scan(reviewed) == 1


def test_staged_mode_scans_the_index_not_the_working_tree(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    target = tmp_path / "kite.env"

    target.write_text("KITE_API_SECRET=" + KITE_SECRET + "\n")
    _git(tmp_path, "add", "kite.env")
    assert _scan("--repo", tmp_path, "--staged").returncode == 1

    # Staged clean, dirty only on disk: the commit would be clean, so the hook passes.
    target.write_text("KITE_API_SECRET=\n")
    _git(tmp_path, "add", "kite.env")
    target.write_text("KITE_API_SECRET=" + KITE_SECRET + "\n")
    assert _scan("--repo", tmp_path, "--staged").returncode == 0


def test_commit_range_catches_a_secret_deleted_by_a_later_commit(tmp_path: Path) -> None:
    """A push publishes every commit, so a secret removed before the tip is still a leak."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("project\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "kite.py").write_text('api_secret = "' + KITE_SECRET + '"\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "oops")
    (tmp_path / "kite.py").write_text("api_secret = os.environ['KITE_API_SECRET']\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "fix")

    assert _scan("--repo", tmp_path).returncode == 0, "the final tree is clean"
    assert _scan("--repo", tmp_path, "--commits", "HEAD~1..HEAD").returncode == 0
    result = _scan("--repo", tmp_path, "--commits", "HEAD~2..HEAD")
    assert result.returncode == 1
    assert "kite.py" in result.stderr


def test_commit_range_scans_commit_messages(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("project\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "debug: KITE_ACCESS_TOKEN=" + KITE_TOKEN)

    result = _scan("--repo", tmp_path, "--commits", "HEAD~1..HEAD")
    assert result.returncode == 1
    assert "COMMIT_MSG" in result.stderr


def test_make_check_runs_the_secret_scan_first() -> None:
    """Removing (or demoting) the scan from the gate must fail this test."""
    make = shutil.which("make")
    assert make, "make is required: `make check` is the gate"
    # Run as a top-level make even when this test runs inside `make check`: a sub-make inherits
    # MAKEFLAGS/MAKELEVEL and prints "Entering directory" lines.
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MAKELEVEL", "MFLAGS"}}
    dry_run = subprocess.run(
        [make, "--no-print-directory", "-n", "check"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    commands = [line.strip() for line in dry_run.stdout.splitlines() if line.strip()]
    assert commands[0] == SCAN_CMD, commands


def test_hooks_and_ci_invoke_the_scan() -> None:
    for hook, flag in (("pre-commit", "--staged"), ("commit-msg", "--message")):
        path = REPO / "ops" / "hooks" / hook
        assert path.stat().st_mode & stat.S_IXUSR, f"{hook} is not executable"
        assert f"ops/secret_scan.py {flag}" in path.read_text()
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "fetch-depth: 0" in ci
    assert 'ops/secret_scan.py --commits "$range"' in ci
