"""The secret-scanning gate (M15.1, invariant #13, HUMAN_DECISIONS D5).

Every planted credential is assembled at runtime from fragments and a hash, so this file holds no
literal that trips the scanner it tests — or GitHub push protection. Each test drives the real
`ops/secret_scan.py` in a subprocess against the repo's real `.secrets.baseline` (or a tampered
copy in a temp dir), so a weakened baseline or a fail-open path fails here, not only in review.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import string
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCANNER = REPO / "ops" / "secret_scan.py"
BASELINE = REPO / ".secrets.baseline"
SCAN_CMD = "uv run python ops/secret_scan.py"

FINDING, CLEAN_EXIT, UNTRUSTED = 1, 0, 2


def _fake(seed: str, length: int) -> str:
    """A deterministic credential-shaped value (lowercase hex) — never a literal."""
    out = ""
    while len(out) < length:
        out += hashlib.sha256(f"{seed}:{len(out)}".encode()).hexdigest()
    return out[:length]


def _kite_shaped(seed: str) -> str:
    """32 mixed-case alphanumerics with an upper, a lower and a digit — not hex."""
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits
    for n in range(1000):
        digest = hashlib.sha256(f"{seed}:{n}".encode()).digest()
        value = "".join(alphabet[b % 62] for b in digest)
        upper, lower, digit = (
            any(c in s for c in value) for s in (alphabet[:26], alphabet[26:52], string.digits)
        )
        if upper and lower and digit and not all(c in string.hexdigits for c in value):
            return value
    raise AssertionError("unreachable")


def _scan(
    *args: str | Path, baseline: Path = BASELINE, repo: Path | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(SCANNER), "--baseline", str(baseline)]
    if repo is not None:
        cmd += ["--repo", str(repo)]
    return subprocess.run(
        [*cmd, *map(str, args)], capture_output=True, text=True, timeout=180, check=False
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


def _baseline_copy(tmp_path: Path, edit: Callable[[dict[str, Any]], None]) -> Path:
    config = json.loads(BASELINE.read_text())
    edit(config)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(config))
    return path


KITE_KEY = _fake("kite-key", 16)
KITE_SECRET = _fake("kite-secret", 32)
KITE_TOKEN = _fake("kite-token", 32)
KITE_BARE = _kite_shaped("kite-bare")
BEARER = _fake("bearer", 40)
PG_PW = "Zq" + _fake("pg", 18)
DSN = "postgresql://" + "trader:" + PG_PW + "@db.internal:5432/trading"
AWS_KEY = "AKIA" + _fake("aws", 16).upper()
PEM_HEADER = "-----BEGIN " + "RSA " + "PRIVATE KEY-----"
ANTHROPIC = "sk-" + "ant-" + "api03-" + _fake("anthropic", 90)
ALL_VALUES = (KITE_KEY, KITE_SECRET, KITE_TOKEN, KITE_BARE, BEARER, PG_PW, AWS_KEY, ANTHROPIC)

PLANTED = {
    # Kite: the three credentials, in every syntax they get pasted in.
    "kite_api_key_py": ("kite.py", 'KITE_API_KEY = "' + KITE_KEY + '"\n'),
    "kite_api_secret_py": ("kite.py", 'kite_api_secret = "' + KITE_SECRET + '"\n'),
    "kite_api_secret_env": ("kite.env", "KITE_API_SECRET=" + KITE_SECRET + "\n"),
    "kite_access_token_py": ("kite.py", 'access_token = "' + KITE_TOKEN + '"\n'),
    "kite_access_token_env": ("kite.env", "KITE_ACCESS_TOKEN=" + KITE_TOKEN + "\n"),
    "kite_access_token_yaml": ("kite.yaml", "kite:\n  access_token: " + KITE_TOKEN + "\n"),
    "kiteconnect_api_key_kwarg": ("kite.py", 'kite = KiteConnect(api_key="' + KITE_KEY + '")\n'),
    "generate_session_api_secret": (
        "kite.py",
        'data = kite.generate_session(request_token, api_secret="' + KITE_SECRET + '")\n',
    ),
    "set_access_token_call": ("kite.py", 'kite.set_access_token("' + KITE_TOKEN + '")\n'),
    "kite_token_upper": ("kite.py", 'KITE_TOKEN = "' + KITE_TOKEN + '"\n'),
    "kite_token_lower": ("kite.py", 'kite_token = "' + KITE_TOKEN + '"\n'),
    "bare_token": ("kite.py", 'token = "' + KITE_TOKEN + '"\n'),
    "authorization_token_md": (
        "notes.md",
        "Authorization: token " + KITE_KEY + ":" + KITE_TOKEN + "\n",
    ),
    "authorization_token_py": (
        "client.py",
        'headers = {"Authorization": "token ' + KITE_KEY + ":" + KITE_TOKEN + '"}\n',
    ),
    "authorization_bearer_py": (
        "client.py",
        'headers = {"Authorization": "Bearer ' + BEARER + '"}\n',
    ),
    "authorization_bearer_md": ("api.md", "curl -H 'Authorization: Bearer " + BEARER + "'\n"),
    # Bare Kite-shaped token: no keyword at all.
    "bare_kite_shape_env": ("session.env", KITE_BARE + "\n"),
    "bare_kite_shape_json": ("session.json", '{"value": "' + KITE_BARE + '"}\n'),
    "bare_kite_shape_yaml": ("session.yaml", "- " + KITE_BARE + "\n"),
    "bare_kite_shape_yml": ("session.yml", "value: " + KITE_BARE + "\n"),
    "bare_kite_shape_md": ("today.md", "Pasted for the drill: " + KITE_BARE + "\n"),
    "bare_kite_shape_toml": ("session.toml", 'value = "' + KITE_BARE + '"\n'),
    "bare_kite_shape_py_with_context": ("drill.py", 'SESSION = "' + KITE_BARE + '"  # kite\n'),
    # Postgres: URL form and libpq conninfo form.
    "postgres_dsn_py": ("settings.py", 'DATABASE_URL = "' + DSN + '"\n'),
    "postgres_dsn_env": ("db.env", "DATABASE_URL=" + DSN + "\n"),
    "conninfo_password_py": (
        "db.py",
        'conn = psycopg.connect("host=db user=u password=' + PG_PW + ' dbname=t")\n',
    ),
    "conninfo_password_env": ("db.env", "PG=host=db user=u password=" + PG_PW + " dbname=t\n"),
    # The rest of the brief.
    "aws_access_key": ("aws.py", 'region = "ap-south-1"\nkey_id = "' + AWS_KEY + '"\n'),
    "private_key": ("id_rsa", PEM_HEADER + "\n" + _fake("pem", 64) + "\n"),
    "anthropic_key_in_prose": ("notes.md", "The key was " + ANTHROPIC + " until rotated.\n"),
    # Paths the stock filters used to skip wholesale (B3).
    "swagger_path": ("docs/swagger-setup.md", "KITE_API_SECRET=" + KITE_SECRET + "\n"),
    "lock_file_name": ("package-lock.json", '{"api_secret": "' + KITE_SECRET + '"}\n'),
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
    kite.set_access_token(access_token)
    conn = psycopg.connect(host="db", password=settings.pg_password.get_secret_value())
    dsn = f"host=db user=u password={pw} dbname=t"
    sha256 = "{digest}"  # content addresses are not credentials
""".replace("{digest}", hashlib.sha256(b"x").hexdigest())


# ── planted credentials ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_planted_credential_fails_the_scan(case: str, tmp_path: Path) -> None:
    name, content = PLANTED[case]
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)

    result = _scan(target)

    assert result.returncode == FINDING, f"{case}: {result.returncode}\n{result.stderr}"
    assert Path(name).name in result.stderr
    # The report locates the finding; it must never become the leak itself.
    for value in ALL_VALUES:
        assert value not in result.stdout + result.stderr


def test_clean_file_passes(tmp_path: Path) -> None:
    target = tmp_path / "clean.py"
    target.write_text(CLEAN)

    result = _scan(target)

    assert result.returncode == CLEAN_EXIT, result.stderr
    assert "clean" in result.stdout


@pytest.mark.parametrize("name", ["digests.md", "digests.json", "digests.yaml", "kite.py"])
def test_hex_digests_are_not_kite_shaped(name: str, tmp_path: Path) -> None:
    """sha256/sha1/md5 digests, this repo's content addresses, never look like a bare token."""
    lines = [
        f"kite access token sha256: {hashlib.sha256(b'a').hexdigest()}",
        f"token sha1 {hashlib.sha1(b'b').hexdigest()}",
        f"access md5 {hashlib.md5(b'c').hexdigest()}",
        f"KITE {hashlib.md5(b'd').hexdigest().upper()}",
    ]
    target = tmp_path / name
    target.write_text("\n".join(f"# {line}" for line in lines) + "\n")

    result = _scan(target)

    assert result.returncode == CLEAN_EXIT, result.stderr


def test_commit_message_is_scanned(tmp_path: Path) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("[M8.3] Wire Kite\n\nused KITE_ACCESS_TOKEN=" + KITE_TOKEN + " to test\n")

    assert _scan("--message", message).returncode == FINDING

    message.write_text("[M8.3] Wire Kite\n\nThe token is read from the environment.\n")
    assert _scan("--message", message).returncode == CLEAN_EXIT


# ── files detect-secrets would skip in silence (B2) ─────────────────────────────────────────────

LATIN1_SECRET = b'# caf\xe9 \xa9 notes\napi_secret = "' + KITE_SECRET.encode() + b'"\n'


def test_non_utf8_text_is_decoded_and_scanned(tmp_path: Path) -> None:
    target = tmp_path / "kite.py"
    target.write_bytes(LATIN1_SECRET)

    assert _scan(target).returncode == FINDING


def test_utf16_text_is_decoded_and_scanned(tmp_path: Path) -> None:
    target = tmp_path / "kite.env"
    target.write_bytes(("KITE_API_SECRET=" + KITE_SECRET + "\n").encode("utf-16"))

    assert _scan(target).returncode == FINDING


def test_unreadable_file_is_an_error_not_a_pass(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    target = tmp_path / "kite.py"
    target.write_text('api_secret = "' + KITE_SECRET + '"\n')
    target.chmod(0)
    try:
        result = _scan(target)
        assert result.returncode == UNTRUSTED
        assert "cannot read" in result.stderr

        _git(tmp_path, "init", "-q")
        tree = _scan(repo=tmp_path)
        assert tree.returncode == UNTRUSTED
        assert "kite.py" in tree.stderr
    finally:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_binary_is_named_as_not_scanned(tmp_path: Path) -> None:
    """A true binary has no text to scan: it is listed every run, never silently passed."""
    target = tmp_path / "bundle.zip"
    target.write_bytes(b"PK\x03\x04\x00\x00" + b"\x00" * 16)

    result = _scan(target)

    assert result.returncode == CLEAN_EXIT
    assert "NOT SCANNED, binary (1)" in result.stdout
    assert "bundle.zip" in result.stdout


def test_tree_and_commit_modes_scan_non_utf8_files(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("project\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "kite.py").write_bytes(LATIN1_SECRET)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "latin-1")

    assert _scan(repo=tmp_path).returncode == FINDING
    assert _scan("--commits", "HEAD~1..HEAD", repo=tmp_path).returncode == FINDING


# ── a baseline cannot weaken or break the scan (B1, B3, N1, N2, N5) ─────────────────────────────


def _drop_plugin(name: str) -> Callable[[dict[str, Any]], None]:
    def edit(config: dict[str, Any]) -> None:
        config["plugins_used"] = [p for p in config["plugins_used"] if p["name"] != name]

    return edit


def _repath_plugin(name: str, path: str) -> Callable[[dict[str, Any]], None]:
    def edit(config: dict[str, Any]) -> None:
        for plugin in config["plugins_used"]:
            if plugin["name"] == name:
                plugin["path"] = path

    return edit


SECRET_FILE = (
    "kite.env",
    "KITE_ACCESS_TOKEN=" + KITE_TOKEN + "\nKITE_API_SECRET=" + KITE_SECRET + "\n",
)


@pytest.mark.parametrize("dropped", ["KeywordDetector", "AnthropicKeyDetector", "IPPublicDetector"])
def test_dropping_any_detector_is_a_config_error(dropped: str, tmp_path: Path) -> None:
    target = tmp_path / SECRET_FILE[0]
    target.write_text(SECRET_FILE[1])

    result = _scan(target, baseline=_baseline_copy(tmp_path, _drop_plugin(dropped)))

    assert result.returncode == UNTRUSTED
    assert dropped in result.stderr


def test_missing_plugin_file_is_a_config_error(tmp_path: Path) -> None:
    """detect-secrets logs 'Unable to load plugins!' and reports clean; the wrapper must not."""
    target = tmp_path / SECRET_FILE[0]
    target.write_text(SECRET_FILE[1])
    edit = _repath_plugin("TokenAssignmentDetector", f"file://{tmp_path / 'gone.py'}")

    result = _scan(target, baseline=_baseline_copy(tmp_path, edit))

    assert result.returncode == UNTRUSTED, result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_plugin_import_error_is_a_config_error(tmp_path: Path) -> None:
    target = tmp_path / SECRET_FILE[0]
    target.write_text(SECRET_FILE[1])
    broken = tmp_path / "broken_plugins.py"
    broken.write_text("raise ImportError('broken on purpose')\n")
    edit = _repath_plugin("TokenAssignmentDetector", f"file://{broken}")

    result = _scan(target, baseline=_baseline_copy(tmp_path, edit))

    assert result.returncode == UNTRUSTED, result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_plugin_path_without_the_class_is_a_config_error(tmp_path: Path) -> None:
    target = tmp_path / SECRET_FILE[0]
    target.write_text(SECRET_FILE[1])
    other = tmp_path / "other_plugins.py"
    other.write_text("X = 1\n")
    edit = _repath_plugin("KiteShapedTokenDetector", f"file://{other}")

    assert _scan(target, baseline=_baseline_copy(tmp_path, edit)).returncode == UNTRUSTED


def _add_filter(entry: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def edit(config: dict[str, Any]) -> None:
        config["filters_used"].append(entry)

    return edit


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "detect_secrets.filters.regex.should_exclude_file", "pattern": ["tests/.*"]},
        {"path": "detect_secrets.filters.regex.should_exclude_line", "pattern": ["token"]},
        {"path": "detect_secrets.filters.regex.should_exclude_secret", "pattern": [".*"]},
        {"path": "detect_secrets.filters.regex.should_exclude_file", "pattern": ["("]},
        {"path": "file:///tmp/x.py::f"},
    ],
    ids=["exclude_file", "exclude_line", "exclude_secret", "bad_exclude_regex", "custom_filter"],
)
def test_exclusion_filters_are_rejected(entry: dict[str, Any], tmp_path: Path) -> None:
    target = tmp_path / "clean.py"
    target.write_text(CLEAN)

    result = _scan(target, baseline=_baseline_copy(tmp_path, _add_filter(entry)))

    assert result.returncode == UNTRUSTED
    assert "filter not allowed" in result.stderr
    assert "Traceback" not in result.stderr


def test_keyword_exclude_is_rejected(tmp_path: Path) -> None:
    def edit(config: dict[str, Any]) -> None:
        for plugin in config["plugins_used"]:
            if plugin["name"] == "KeywordDetector":
                plugin["keyword_exclude"] = "secret"

    target = tmp_path / "clean.py"
    target.write_text(CLEAN)
    result = _scan(target, baseline=_baseline_copy(tmp_path, edit))

    assert result.returncode == UNTRUSTED
    assert "keyword_exclude" in result.stderr


@pytest.mark.parametrize(
    "filter_path",
    [
        "detect_secrets.filters.allowlist.is_line_allowlisted",
        "detect_secrets.filters.heuristic.is_indirect_reference",
        "detect_secrets.filters.heuristic.is_swagger_file",
        "detect_secrets.filters.heuristic.is_lock_file",
        "detect_secrets.filters.common.is_ignored_due_to_verification_policies",
    ],
)
def test_forbidden_filters_are_stripped(filter_path: str, tmp_path: Path) -> None:
    """Listing a skip-filter in the baseline does not bring it back."""
    (tmp_path / "docs").mkdir()
    targets = {
        "kite.py": 'kite = KiteConnect(api_key="'
        + KITE_KEY
        + '")  # pragma: allowlist '
        + "secret\n",
        "docs/swagger-setup.md": "KITE_API_SECRET=" + KITE_SECRET + "\n",
        "package-lock.json": '{"api_secret": "' + KITE_SECRET + '"}\n',
    }
    baseline = _baseline_copy(tmp_path, _add_filter({"path": filter_path, "min_level": 2}))
    for name, content in targets.items():
        (tmp_path / name).write_text(content)
        assert _scan(tmp_path / name, baseline=baseline).returncode == FINDING, name


@pytest.mark.parametrize(
    "content",
    ["", "{not json", "[]", '{"plugins_used": [], "results": {}}', '{"results": {}}'],
    ids=["empty", "corrupt", "not_object", "no_plugins", "no_plugins_key"],
)
def test_corrupt_baseline_is_a_clear_error(content: str, tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(content)
    target = tmp_path / "clean.py"
    target.write_text(CLEAN)

    result = _scan(target, baseline=baseline)

    assert result.returncode == UNTRUSTED
    assert result.stderr.startswith("secret-scan: ")
    assert "Traceback" not in result.stderr


def test_unknown_plugin_is_a_clear_error(tmp_path: Path) -> None:
    def edit(config: dict[str, Any]) -> None:
        config["plugins_used"].append({"name": "NoSuchDetector"})

    target = tmp_path / "clean.py"
    target.write_text(CLEAN)
    result = _scan(target, baseline=_baseline_copy(tmp_path, edit))

    assert result.returncode == UNTRUSTED
    assert "Traceback" not in result.stderr


def test_baseline_entry_holding_a_raw_value_is_rejected(tmp_path: Path) -> None:
    def edit(config: dict[str, Any]) -> None:
        entry = {"type": "Secret Keyword", "filename": "x.py", "is_verified": False}
        config["results"]["x.py"] = [{**entry, "hashed_secret": KITE_SECRET[:20], "line_number": 1}]

    target = tmp_path / "clean.py"
    target.write_text(CLEAN)
    result = _scan(target, baseline=_baseline_copy(tmp_path, edit))

    assert result.returncode == UNTRUSTED
    assert "not a sha1" in result.stderr


def test_baseline_holds_only_hashes_and_reviewed_entries() -> None:
    """The allowlist stays narrow: path-keyed sha1 entries, no filters beyond value heuristics."""
    config = json.loads(BASELINE.read_text())
    plugins = {p["name"] for p in config["plugins_used"]}
    filters = {f["path"] for f in config["filters_used"]}

    assert "HexHighEntropyString" not in plugins
    assert "Base64HighEntropyString" not in plugins
    assert all(f.startswith("detect_secrets.filters.heuristic.is_") for f in filters)
    assert not filters & {
        "detect_secrets.filters.heuristic.is_indirect_reference",
        "detect_secrets.filters.heuristic.is_lock_file",
        "detect_secrets.filters.heuristic.is_swagger_file",
    }
    for path, entries in config["results"].items():
        assert (REPO / path).is_file(), f"baseline entry for a missing file: {path}"
        for entry in entries:
            assert set(entry) == {"type", "filename", "hashed_secret", "is_verified", "line_number"}
            assert len(entry["hashed_secret"]) == 40
            assert all(c in "0123456789abcdef" for c in entry["hashed_secret"])


# ── --accept, --staged, --commits ───────────────────────────────────────────────────────────────


def test_accept_is_pinned_to_path_and_value(tmp_path: Path) -> None:
    """--accept allowlists that value in that file — not the same value elsewhere, not a new one."""
    _git(tmp_path, "init", "-q")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(BASELINE.read_text())
    reviewed = tmp_path / "docs.md"
    reviewed.write_text("example: " + DSN + "\n")
    elsewhere = tmp_path / "app.py"
    elsewhere.write_text('DSN = "' + DSN + '"\n')

    accept = _scan("--accept", reviewed, baseline=baseline, repo=tmp_path)
    assert accept.returncode == CLEAN_EXIT, accept.stderr
    before = json.loads(BASELINE.read_text())["results"]
    after = json.loads(baseline.read_text())["results"]
    assert set(after) == set(before) | {"docs.md"}, "accept must only append"

    assert _scan(reviewed, baseline=baseline, repo=tmp_path).returncode == CLEAN_EXIT
    assert _scan(elsewhere, baseline=baseline, repo=tmp_path).returncode == FINDING
    reviewed.write_text("example: " + DSN + "\nKITE_ACCESS_TOKEN=" + KITE_TOKEN + "\n")
    assert _scan(reviewed, baseline=baseline, repo=tmp_path).returncode == FINDING


def test_staged_mode_scans_the_index_not_the_working_tree(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    target = tmp_path / "kite.env"

    target.write_text("KITE_API_SECRET=" + KITE_SECRET + "\n")
    _git(tmp_path, "add", "kite.env")
    assert _scan("--staged", repo=tmp_path).returncode == FINDING

    # Staged clean, dirty only on disk: the commit would be clean, so the hook passes.
    target.write_text("KITE_API_SECRET=\n")
    _git(tmp_path, "add", "kite.env")
    target.write_text("KITE_API_SECRET=" + KITE_SECRET + "\n")
    assert _scan("--staged", repo=tmp_path).returncode == CLEAN_EXIT


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

    assert _scan(repo=tmp_path).returncode == CLEAN_EXIT, "the final tree is clean"
    assert _scan("--commits", "HEAD~1..HEAD", repo=tmp_path).returncode == CLEAN_EXIT
    result = _scan("--commits", "HEAD~2..HEAD", repo=tmp_path)
    assert result.returncode == FINDING
    assert "kite.py" in result.stderr


def test_commit_range_scans_commit_messages(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("project\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "debug: KITE_ACCESS_TOKEN=" + KITE_TOKEN)

    result = _scan("--commits", "HEAD~1..HEAD", repo=tmp_path)
    assert result.returncode == FINDING
    assert "COMMIT_MSG" in result.stderr


@pytest.mark.parametrize("rev", ["HEAD", "HEAD^!", "main"])
def test_commits_requires_a_range(rev: str, tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("project\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")

    result = _scan("--commits", rev, repo=tmp_path)

    assert result.returncode == UNTRUSTED
    assert "A..B" in result.stderr


# ── wiring: make check, hooks, CI ───────────────────────────────────────────────────────────────


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
    workflow = (REPO / ".github" / "workflows" / "secret-scan.yml").read_text()
    assert "fetch-depth: 0" in workflow
    assert 'ops/secret_scan.py --commits "$range"' in workflow
    # Never cancelled by a newer push: the scan of what main already published must finish.
    assert "cancel-in-progress" not in workflow
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "secret-scan:" not in ci, "the scan job lives in its own, uncancellable workflow"
