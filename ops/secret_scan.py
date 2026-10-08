"""Secret scan — the enforceable half of invariant #13 (AGENTIC_CONTEXT §6, HUMAN_DECISIONS D5).

The repo is public, so a credential in a pushed commit is compromised the moment it lands and can
only be rotated, never un-published. This wraps detect-secrets (pinned in uv.lock) so the same scan,
with the same audited allowlist, runs at four points:

    uv run python ops/secret_scan.py                  # working tree — `make check`
    uv run python ops/secret_scan.py --staged         # staged blobs — pre-commit hook
    uv run python ops/secret_scan.py --message FILE   # commit message — commit-msg hook
    uv run python ops/secret_scan.py --commits A..B   # every commit in a range — CI
    uv run python ops/secret_scan.py PATH...          # explicit files

Exit 0 clean, 1 on any finding not in the baseline, 2 when the scan cannot be trusted: a file it
cannot read, or a baseline that is corrupt or would weaken the scan.

What it assumes: `.secrets.baseline` at the repo root holds the detector set and every accepted
false positive, keyed by path + detector + hash of the matched value (ops/runbooks/secret-scan.md).

What it never does: fail open. Every file is read here, not by detect-secrets (which skips
unreadable and non-UTF-8 files without a word): unreadable is exit 2, non-UTF-8 text is decoded and
scanned, and a true binary is named in the output as unscanned. It never touches the network,
never honours an inline `pragma: allowlist secret`, never rewrites the baseline outside `--accept`,
and never prints a matched value — so its own output can never become the leak.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from detect_secrets.core import scan as ds_scan
from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
from detect_secrets.settings import (
    configure_settings_from_baseline,
    get_filters,
    get_plugins,
    get_settings,
)
from detect_secrets.util.importlib import import_file_as_module

REPO = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO / ".secrets.baseline"

# Stripped from the settings on every run, whatever the baseline says. Verification calls the
# credential's provider (a network call) and hides anything that "fails" it — a planted fake or a
# revoked key. The rest silently skip whole files or lines: an inline pragma nobody reviews; any
# path containing "swagger"; any file named like a lock file; `x = f("…")` call-shaped lines
# (which is exactly `KiteConnect(api_key="…")`); and an extension list that would skip a text file
# named .pdf — binaries are classified here instead.
FORBIDDEN_FILTERS = frozenset(
    {
        "detect_secrets.filters.common.is_ignored_due_to_verification_policies",
        "detect_secrets.filters.allowlist.is_line_allowlisted",
        "detect_secrets.filters.heuristic.is_swagger_file",
        "detect_secrets.filters.heuristic.is_lock_file",
        "detect_secrets.filters.heuristic.is_indirect_reference",
        "detect_secrets.filters.heuristic.is_non_text_file",
    }
)
# Value-shape heuristics that drop a placeholder, never a file or a path. A baseline naming any
# other filter (a regex exclude, a wordlist, a custom file:// filter) is rejected: the reviewed
# per-entry allowlist is the only way to accept something.
PERMITTED_FILTERS = frozenset(
    {
        "detect_secrets.filters.common.is_invalid_file",
        "detect_secrets.filters.heuristic.is_likely_id_string",
        "detect_secrets.filters.heuristic.is_not_alphanumeric_string",
        "detect_secrets.filters.heuristic.is_potential_uuid",
        "detect_secrets.filters.heuristic.is_prefixed_with_dollar_sign",
        "detect_secrets.filters.heuristic.is_sequential_string",
        "detect_secrets.filters.heuristic.is_templated_secret",
    }
)

# Every detector the gate runs — all of detect-secrets' own except the two entropy ones (see the
# gate note), plus the repo-local ones. The baseline must list each and each must load: dropping
# any, required for Kite or not, is a configuration error (exit 2), never a quieter scan. A newer
# detect-secrets may add detectors; those are welcome but not required until added here.
REQUIRED_PLUGINS = frozenset(
    {
        "AWSKeyDetector",
        "AnthropicKeyDetector",
        "ArtifactoryDetector",
        "AuthorizationHeaderDetector",
        "AzureStorageKeyDetector",
        "BasicAuthDetector",
        "CloudantDetector",
        "ConninfoPasswordDetector",
        "DiscordBotTokenDetector",
        "GitHubTokenDetector",
        "GitLabTokenDetector",
        "IPPublicDetector",
        "IbmCloudIamDetector",
        "IbmCosHmacDetector",
        "JwtTokenDetector",
        "KeywordDetector",
        "KiteShapedTokenDetector",
        "MailchimpDetector",
        "NpmDetector",
        "OpenAIDetector",
        "PrivateKeyDetector",
        "PypiTokenDetector",
        "SendGridDetector",
        "SlackDetector",
        "SoftlayerDetector",
        "SquareOAuthDetector",
        "StripeDetector",
        "TelegramBotTokenDetector",
        "TokenAssignmentDetector",
        "TwilioKeyDetector",
    }
)

# detect-secrets' own detectors, by class name, as shipped in the pinned version.
_STOCK_PLUGINS = frozenset(cls.__name__ for cls in get_mapping_from_secret_type_to_class().values())

_ENTRY_KEYS = frozenset({"type", "filename", "hashed_secret", "is_verified", "line_number"})
_SHA1 = re.compile(r"[0-9a-f]{40}")
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")

# An accepted false positive: (path, detector, sha1 of the matched value) — the baseline's own key.
# Line numbers are deliberately not part of it, so an edit above an accepted line does not
# resurrect it, and moving it to another file does.
Accepted = frozenset[tuple[str, str, str]]
Settings = dict[str, Any]


class ScanConfigError(Exception):
    """The scan cannot be trusted: bad baseline, unloadable detector, unreadable input."""


@dataclass(frozen=True)
class Finding:
    """One unaccepted match. Carries no part of the matched value."""

    where: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.where}:{self.line}: {self.kind}"


@dataclass
class Report:
    findings: list[Finding]
    binaries: list[str]


# ── baseline ────────────────────────────────────────────────────────────────────────────────────


def _read_baseline(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScanConfigError(f"baseline not found: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScanConfigError(f"baseline unreadable or not JSON: {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ScanConfigError(f"baseline is not a JSON object: {path}")
    return config


def _validate(config: dict[str, Any]) -> Settings:
    """Check a baseline and return the detect-secrets settings to run with (filters sanitised)."""
    plugins = config.get("plugins_used")
    filters = config.get("filters_used", [])
    results = config.get("results")
    if not isinstance(plugins, list) or not all(isinstance(p, dict) for p in plugins):
        raise ScanConfigError("baseline: plugins_used must be a list of objects")
    if not isinstance(filters, list) or not all(isinstance(f, dict) for f in filters):
        raise ScanConfigError("baseline: filters_used must be a list of objects")
    if not isinstance(results, dict):
        raise ScanConfigError("baseline: results must be an object")

    names = [p.get("name") for p in plugins]
    if not all(isinstance(n, str) for n in names) or len(set(names)) != len(names):
        raise ScanConfigError("baseline: every plugin needs a unique name")
    missing = REQUIRED_PLUGINS - {str(n) for n in names}
    if missing:
        raise ScanConfigError(f"baseline lacks required detector(s): {', '.join(sorted(missing))}")
    for plugin in plugins:
        if plugin.get("keyword_exclude"):
            raise ScanConfigError(f"baseline: {plugin['name']} has a keyword_exclude")

    kept = []
    for filt in filters:
        path = filt.get("path")
        if path in FORBIDDEN_FILTERS:
            continue
        if path not in PERMITTED_FILTERS:
            raise ScanConfigError(
                f"baseline: filter not allowed: {path} — accept a false positive with --accept"
            )
        kept.append(filt)

    for filename, entries in results.items():
        if not isinstance(entries, list):
            raise ScanConfigError(f"baseline: results[{filename!r}] must be a list")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - _ENTRY_KEYS:
                raise ScanConfigError(f"baseline: unexpected fields in an entry for {filename}")
            if entry.get("filename") != filename or not isinstance(entry.get("type"), str):
                raise ScanConfigError(f"baseline: malformed entry for {filename}")
            # Only a sha1 may sit here — never the value itself.
            if not _SHA1.fullmatch(str(entry.get("hashed_secret", ""))):
                raise ScanConfigError(f"baseline: hashed_secret for {filename} is not a sha1")
    return {"plugins_used": plugins, "filters_used": kept}


def _anchor_plugin_paths(settings: Settings) -> None:
    """Repo-local plugins are named relative to the repo root; anchor them there."""
    for plugin in settings["plugins_used"]:
        path = plugin.get("path")
        if isinstance(path, str) and path.startswith("file://"):
            rel = path.removeprefix("file://")
            plugin["path"] = f"file://{rel if Path(rel).is_absolute() else REPO / rel}"


def _configure(settings: Settings) -> None:
    """Load `settings` into detect-secrets and prove every listed detector actually loaded.

    detect-secrets reports a file "clean" when a plugin fails to import ('Unable to load
    plugins!') or none is configured, so the loaded classes are checked, not the names asked for.
    Runs in the parent and in every worker process.
    """
    get_settings().clear()
    get_mapping_from_secret_type_to_class.cache_clear()
    try:
        configure_settings_from_baseline(settings)
    except Exception as exc:
        raise ScanConfigError(f"baseline settings rejected by detect-secrets: {exc}") from exc
    current = get_settings()
    current.disable_filters(*(f for f in FORBIDDEN_FILTERS if f in current.filters))
    get_plugins.cache_clear()
    get_filters.cache_clear()
    get_mapping_from_secret_type_to_class.cache_clear()
    try:
        loaded = {type(plugin).__name__ for plugin in get_plugins()}
    except BaseException as exc:  # the loader raises TypeError, FileNotFoundError, whatever imports
        raise ScanConfigError(f"a detector failed to load: {type(exc).__name__}: {exc}") from exc
    wanted = {p["name"] for p in settings["plugins_used"]}
    _check_plugin_sources(settings)
    if loaded != wanted:
        raise ScanConfigError(
            f"detector(s) listed but not loaded: {', '.join(sorted(wanted - loaded)) or '-'}"
        )
    leftover = set(current.filters) & FORBIDDEN_FILTERS
    if leftover:
        raise ScanConfigError(f"forbidden filter still active: {', '.join(sorted(leftover))}")


def _check_plugin_sources(settings: Settings) -> None:
    """Each `file://` detector must be defined in the file its own entry names.

    detect-secrets resolves a class by name across every listed file, so an entry repointed at the
    wrong file would still load — from somewhere nobody chose. Stock detectors must be stock.
    """
    modules: dict[str, Any] = {}
    for plugin in settings["plugins_used"]:
        name, path = plugin["name"], plugin.get("path")
        if path is None:
            if name not in _STOCK_PLUGINS:
                raise ScanConfigError(f"unknown detector without a path: {name}")
            continue
        filename = str(path).removeprefix("file://")
        try:
            if filename not in modules:
                modules[filename] = import_file_as_module(filename)
        except BaseException as exc:
            raise ScanConfigError(f"detector file failed to load: {filename}: {exc}") from exc
        if not isinstance(getattr(modules[filename], name, None), type):
            raise ScanConfigError(f"{name} is not defined in {filename}")


def load_baseline(path: Path) -> tuple[Settings, Accepted]:
    """Validate the baseline, configure detect-secrets from it, return (settings, accepted)."""
    config = _read_baseline(path)
    settings = _validate(config)
    _anchor_plugin_paths(settings)
    _configure(settings)
    accepted = frozenset(
        (name, entry["hashed_secret"], entry["type"])
        for name, entries in config["results"].items()
        for entry in entries
    )
    return settings, accepted


# ── reading and scanning ────────────────────────────────────────────────────────────────────────


def decode(data: bytes) -> str | None:
    """Text to scan, or None for a true binary.

    UTF-16 by BOM; otherwise a NUL anywhere means binary (git's own heuristic, over the whole
    blob); otherwise UTF-8, falling back to latin-1, which maps every byte — so one stray byte
    never makes a file unscannable.
    """
    if data.startswith(_UTF16_BOMS):
        return data.decode("utf-16", errors="replace")
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _worker_init(settings: Settings) -> None:
    _configure(settings)


def _scan_files(paths: list[str]) -> list[tuple[str, int, str, str]]:
    """Scan absolute paths with the configured detectors: (path, line, type, value hash)."""
    out = []
    for path in paths:
        for secret in ds_scan.scan_file(path):
            out.append((path, secret.line_number, secret.type, secret.secret_hash))
    return out


def _materialise(
    blobs: Iterable[tuple[str, bytes]], root: Path
) -> tuple[dict[str, str], list[str]]:
    """Write each decodable blob as UTF-8 under `root`; return (scratch path → name, binaries).

    The scratch copy keeps the name's extension (detectors and filters look at it) and its repo
    path (a baseline entry still applies).
    """
    by_scratch: dict[str, str] = {}
    binaries: list[str] = []
    for name, data in blobs:
        text = decode(data)
        if text is None:
            binaries.append(name)
            continue
        dest = root / name.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        by_scratch[str(dest)] = name
    return by_scratch, binaries


def scan_blobs(
    blobs: Iterable[tuple[str, bytes]], settings: Settings, accepted: Accepted, label: str = ""
) -> Report:
    """Decode each (name, content), scan it, and return what the baseline does not accept."""
    with tempfile.TemporaryDirectory(prefix="secret-scan-") as tmp:
        by_scratch, binaries = _materialise(blobs, Path(tmp))
        paths = sorted(by_scratch)
        jobs = min(4, os.cpu_count() or 1)
        if len(paths) < 32 or jobs == 1:
            raw = _scan_files(paths)
        else:
            with ProcessPoolExecutor(jobs, initializer=_worker_init, initargs=(settings,)) as pool:
                parts = pool.map(_scan_files, [paths[i::jobs] for i in range(jobs)])
                raw = [hit for part in parts for hit in part]
    prefix = f"{label}:" if label else ""
    findings = sorted(
        {
            Finding(f"{prefix}{by_scratch[path]}", line, kind)
            for path, line, kind, digest in raw
            if (by_scratch[path], digest, kind) not in accepted
        },
        key=lambda f: (f.where, f.line, f.kind),
    )
    return Report(findings, [f"{prefix}{b}" for b in binaries])


def _git(*args: str, cwd: Path) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def _split_z(raw: bytes) -> list[str]:
    return [p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p]


def _repo_relative(path: Path, repo: Path) -> str:
    """`path` relative to `repo` (the baseline's key for it), or "" if it lies outside."""
    resolved, root = path.resolve(), repo.resolve()
    return resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else ""


def _name_for(path: Path, repo: Path) -> str:
    """Repo-relative when inside the repo — the baseline's key — absolute otherwise."""
    return _repo_relative(path, repo) or str(path.resolve())


def _read_files(files: list[tuple[str, Path]]) -> list[tuple[str, bytes]]:
    """Read every file here; any that cannot be read makes the whole scan untrustworthy."""
    out, unreadable = [], []
    for name, path in files:
        try:
            out.append((name, path.read_bytes()))
        except OSError as exc:
            unreadable.append(f"{name} ({exc.strerror or type(exc).__name__})")
    if unreadable:
        raise ScanConfigError("cannot read: " + "; ".join(unreadable))
    return out


def scan_tree(repo: Path, settings: Settings, accepted: Accepted, baseline: Path) -> Report:
    """Tracked files plus untracked-but-not-ignored ones: everything a `git add -A` could stage.

    A symlink is scanned as its target string — what git stores for it. A tracked file deleted
    from disk has nothing to scan; a nested repository (listed as a directory) is its own repo.
    """
    names = _split_z(_git("ls-files", "-z", "--cached", "--others", "--exclude-standard", cwd=repo))
    skip = _repo_relative(baseline, repo)
    blobs: list[tuple[str, bytes]] = []
    files: list[tuple[str, Path]] = []
    for name in names:
        path = repo / name
        if name == skip:
            continue
        if path.is_symlink():
            blobs.append((name, str(path.readlink()).encode()))
        elif path.is_dir() or not os.path.lexists(path):
            continue
        else:
            files.append((name, path))
    return scan_blobs(blobs + _read_files(files), settings, accepted)


def scan_paths(repo: Path, paths: list[Path], settings: Settings, accepted: Accepted) -> Report:
    blobs = _read_files([(_name_for(p, repo), p) for p in paths])
    return scan_blobs(blobs, settings, accepted)


def scan_staged(repo: Path, settings: Settings, accepted: Accepted, baseline: Path) -> Report:
    """The index, not the working tree: what the commit will actually contain."""
    skip = _repo_relative(baseline, repo)
    names = _split_z(_git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRT", cwd=repo))
    blobs = [(n, _git("show", f":{n}", cwd=repo)) for n in names if n != skip]
    return scan_blobs(blobs, settings, accepted)


def scan_message(path: Path, settings: Settings, accepted: Accepted) -> Report:
    """A commit message — invariant #13 names it, and it can never be rewritten once pushed."""
    return scan_blobs(_read_files([("COMMIT_EDITMSG", path)]), settings, accepted)


def scan_commits(
    repo: Path, rev_range: str, settings: Settings, accepted: Accepted, baseline: Path
) -> Report:
    """Every commit in `A..B`, each at its own content — not just the range's final tree.

    A secret added in one commit and deleted in the next never reaches the final tree, but a push
    publishes both, so each commit's added/changed blobs and its message are scanned. Merge commits
    are diffed with --cc: only what the merge itself introduced.
    """
    if ".." not in rev_range:
        raise ScanConfigError(f"--commits takes a range A..B, got {rev_range!r}")
    skip = _repo_relative(baseline, repo)
    shas = _git("rev-list", "--reverse", rev_range, cwd=repo).decode().split()
    report = Report([], [])
    diff_tree = ["diff-tree", "--no-commit-id", "-r", "--root", "--cc", "-z", "--name-only"]
    for sha in shas:
        blobs = [("COMMIT_MSG", _git("log", "-1", "--format=%B", sha, cwd=repo))]
        names = _split_z(_git(*diff_tree, "--diff-filter=ACMRT", sha, cwd=repo))
        blobs += [(n, _git("show", f"{sha}:{n}", cwd=repo)) for n in names if n != skip]
        part = scan_blobs(blobs, settings, accepted, sha[:12])
        report.findings += part.findings
        report.binaries += part.binaries
    return report


def accept(repo: Path, paths: list[Path], baseline: Path) -> list[Finding]:
    """Add every current finding in `paths` to the baseline as a reviewed false positive.

    Appends only: entries for every other path are kept exactly as they are (the stock
    `detect-secrets scan --baseline FILE` drops them). Each entry is pinned to its path, detector
    and value hash, so it accepts that one value in that one file and nothing else. The caller
    must have looked at every line first — this is the reviewable diff, not the review.
    """
    config = _read_baseline(baseline)
    load_baseline(baseline)
    blobs = _read_files([(_name_for(p, repo), p) for p in paths])
    with tempfile.TemporaryDirectory(prefix="secret-accept-") as tmp:
        by_scratch, _ = _materialise(blobs, Path(tmp))
        hits = [
            (by_scratch[path], line, kind, digest)
            for path, line, kind, digest in _scan_files(sorted(by_scratch))
        ]
    results: dict[str, list[dict[str, Any]]] = config["results"]
    added: list[Finding] = []
    for name, line, kind, digest in hits:
        entries = results.setdefault(name, [])
        if any((e["hashed_secret"], e["type"]) == (digest, kind) for e in entries):
            continue
        entries.append(
            {
                "type": kind,
                "filename": name,
                "hashed_secret": digest,
                "is_verified": False,
                "line_number": line,
            }
        )
        added.append(Finding(name, line, kind))
    config["results"] = {
        name: sorted(entries, key=lambda e: (str(e["type"]), str(e["hashed_secret"])))
        for name, entries in sorted(results.items())
    }
    baseline.write_text(json.dumps(config, indent=2) + "\n")
    return added


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────────


def _run(args: argparse.Namespace) -> int:
    missing = [p for p in args.paths if not p.is_file()]
    if missing:
        raise ScanConfigError(f"not a file: {missing[0]}")
    if args.accept:
        added = accept(args.repo, args.paths, args.baseline)
        for finding in added:
            print(f"accepted  {finding}")
        print(f"secret-scan: {len(added)} entr(y/ies) added to {args.baseline} — review the diff")
        return 0

    settings, accepted = load_baseline(args.baseline)
    if args.staged:
        report = scan_staged(args.repo, settings, accepted, args.baseline)
    elif args.message:
        report = scan_message(args.message, settings, accepted)
    elif args.commits:
        report = scan_commits(args.repo, args.commits, settings, accepted, args.baseline)
    elif args.paths:
        report = scan_paths(args.repo, args.paths, settings, accepted)
    else:
        report = scan_tree(args.repo, settings, accepted, args.baseline)

    if report.binaries:
        # A documented gap, named on every run rather than passed in silence: a binary (NUL bytes —
        # zip, xlsx, most pdf) has no text lines to scan. ops/runbooks/secret-scan.md.
        print(f"secret-scan: NOT SCANNED, binary ({len(report.binaries)}):")
        for name in report.binaries:
            print(f"  {name}")
    if not report.findings:
        print("secret-scan: clean")
        return 0
    print(
        f"secret-scan: {len(report.findings)} finding(s) — the value is never printed:",
        file=sys.stderr,
    )
    for finding in report.findings:
        print(f"  {finding}", file=sys.stderr)
    print(
        "If any is real: rotate it at the provider FIRST (ops/runbooks/secret-leak.md). "
        "False positive: ops/runbooks/secret-scan.md.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="scan the index (pre-commit)")
    mode.add_argument("--message", type=Path, metavar="FILE", help="scan a commit message file")
    mode.add_argument("--commits", metavar="A..B", help="scan every commit in a range (CI)")
    mode.add_argument(
        "--accept",
        action="store_true",
        help="add the findings in PATHS to the baseline as reviewed false positives",
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--repo", type=Path, default=REPO, help="git work tree to scan (default: this checkout)"
    )
    parser.add_argument("paths", nargs="*", type=Path, help="explicit files to scan")
    args = parser.parse_args(argv)
    if args.paths and (args.staged or args.message or args.commits):
        parser.error("explicit paths cannot be combined with --staged/--message/--commits")
    if args.accept and not args.paths:
        parser.error("--accept needs the file(s) whose findings were reviewed")

    try:
        return _run(args)
    except ScanConfigError as exc:
        print(f"secret-scan: {exc}", file=sys.stderr)
        return 2
    except BrokenProcessPool as exc:
        print(f"secret-scan: a scan worker failed to start or died: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip() if exc.stderr else ""
        print(f"secret-scan: git failed: {stderr}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
