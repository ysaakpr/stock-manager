"""Secret scan — the enforceable half of invariant #13 (AGENTIC_CONTEXT §6, HUMAN_DECISIONS D5).

The repo is public, so a credential in a pushed commit is compromised the moment it lands and can
only be rotated, never un-published. This wraps detect-secrets (pinned in uv.lock) so the same scan,
with the same audited allowlist, runs at four points:

    uv run python ops/secret_scan.py                  # working tree — `make check`
    uv run python ops/secret_scan.py --staged         # staged blobs — pre-commit hook
    uv run python ops/secret_scan.py --message FILE   # commit message — commit-msg hook
    uv run python ops/secret_scan.py --commits A..B   # every commit in a range — CI on a PR
    uv run python ops/secret_scan.py PATH...          # explicit files

Exit 0 clean, 1 on any finding not in the baseline, 2 on a usage or configuration error.

What it assumes: `.secrets.baseline` at the repo root holds the plugin set and every accepted false
positive, keyed by path + detector + hash of the matched value (ops/runbooks/secret-scan.md).

What it never does: touch the network (live verification is stripped from the settings whatever
the baseline says), honour an inline `pragma: allowlist secret` comment (an allowlist entry nobody
reviews), rewrite the baseline, or print a matched value — a finding is reported as path, line
and detector only, so the scan's own output can never become the leak.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from detect_secrets.core import baseline as ds_baseline
from detect_secrets.core.secrets_collection import SecretsCollection
from detect_secrets.settings import get_filters, get_plugins, get_settings

REPO = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO / ".secrets.baseline"

# Stripped from the settings on every run, whatever the baseline file says, so editing the
# baseline cannot quietly re-enable either. Verification places a live call to the credential's
# provider and hides anything that "fails" it — a planted test value, or a real key already
# revoked — which is backwards for a leak scanner and is a network call at scan time besides.
FORBIDDEN_FILTERS = (
    "detect_secrets.filters.common.is_ignored_due_to_verification_policies",
    "detect_secrets.filters.allowlist.is_line_allowlisted",
)

# The detectors the gate exists for (M15.1): Kite-style key/secret/token assignments, a DSN with an
# embedded password, an AWS key, a private key. A baseline that drops one is a configuration error,
# not a quieter scan — with no plugins at all detect-secrets logs an error and reports "clean".
REQUIRED_PLUGINS = frozenset(
    {
        "KeywordDetector",
        "TokenAssignmentDetector",
        "BasicAuthDetector",
        "AWSKeyDetector",
        "PrivateKeyDetector",
    }
)


# An accepted false positive: (repo-relative path, detector, sha1 of the matched value) — the
# baseline's own key. Line numbers are deliberately not part of it, so an edit above an accepted
# line does not resurrect it, and moving it to another file does.
Accepted = frozenset[tuple[str, str, str]]


class ScanConfigError(Exception):
    """The baseline would make the scan weaker than the gate promises."""


@dataclass(frozen=True)
class Finding:
    """One unaccepted match. Carries no part of the matched value."""

    where: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.where}:{self.line}: {self.kind}"


def _git(*args: str, cwd: Path) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def _load_baseline(path: Path) -> Accepted:
    """Configure detect-secrets from the baseline and return its accepted findings."""
    config = ds_baseline.load_from_file(str(path))
    # The repo-local plugins (ops/secret_scan_plugins.py) are named relative to the repo root;
    # anchor them there so the scan does not depend on the caller's working directory.
    for plugin in config.get("plugins_used", []):
        if str(plugin.get("path", "")).startswith("file://"):
            plugin["path"] = f"file://{REPO / plugin['path'].removeprefix('file://')}"
    loaded = ds_baseline.load(config, str(path))
    settings = get_settings()
    settings.disable_filters(*(f for f in FORBIDDEN_FILTERS if f in settings.filters))
    # Only the derived caches — detect_secrets' own cache_bust() also drops the settings just
    # loaded, which leaves the scan with no plugins and a silent pass.
    get_plugins.cache_clear()
    get_filters.cache_clear()
    missing = REQUIRED_PLUGINS - set(get_settings().plugins)
    if missing:
        raise ScanConfigError(f"baseline lacks required detector(s): {', '.join(sorted(missing))}")
    return frozenset((name, secret.secret_hash, secret.type) for name, secret in loaded)


def _scan(files: list[str], root: Path, accepted: Accepted, label: str) -> list[Finding]:
    """Scan `files` (relative to `root`) and return what the baseline does not accept.

    Compared on the collection's root-relative key, not `PotentialSecret.filename`: detect-secrets
    stores the joined absolute path there, so its own set subtraction never matches a baseline.
    """
    if not files:
        return []
    found = SecretsCollection(root=str(root))
    # Parallel across at most 4 processes: the whole tree is ~70 s single-threaded.
    found.scan_files(*files, num_processors=min(4, os.cpu_count() or 1))
    prefix = f"{label}:" if label else ""
    return [
        Finding(f"{prefix}{name}", secret.line_number, secret.type)
        for name, secret in found
        if (name, secret.secret_hash, secret.type) not in accepted
    ]


def _scan_blobs(blobs: dict[str, bytes], accepted: Accepted, label: str = "") -> list[Finding]:
    """Scan content that is not on disk as-is (a staged blob, a past commit) at its repo path.

    The content is materialised under its own repo-relative path so a baseline entry for that
    path still applies; the scratch directory is removed before returning.
    """
    with tempfile.TemporaryDirectory(prefix="secret-scan-") as tmp:
        root = Path(tmp)
        for name, content in blobs.items():
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        return _scan(sorted(blobs), root, accepted, label)


def _repo_relative(path: Path, repo: Path) -> str:
    """`path` relative to `repo` (the baseline's key for it), or "" if it lies outside."""
    resolved, root = path.resolve(), repo.resolve()
    return resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else ""


def _split_z(raw: bytes) -> list[str]:
    return [p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p]


def scan_tree(repo: Path, accepted: Accepted, baseline: Path) -> list[Finding]:
    """Tracked files plus untracked-but-not-ignored ones: everything a `git add -A` could stage."""
    names = _split_z(_git("ls-files", "-z", "--cached", "--others", "--exclude-standard", cwd=repo))
    skip = _repo_relative(baseline, repo)
    files = [n for n in names if n != skip and (repo / n).is_file() and not (repo / n).is_symlink()]
    return _scan(files, repo, accepted, "")


def scan_staged(repo: Path, accepted: Accepted, baseline: Path) -> list[Finding]:
    """The index, not the working tree: what the commit will actually contain."""
    skip = _repo_relative(baseline, repo)
    names = _split_z(_git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRT", cwd=repo))
    blobs = {n: _git("show", f":{n}", cwd=repo) for n in names if n != skip}
    return _scan_blobs(blobs, accepted)


def scan_message(path: Path, accepted: Accepted) -> list[Finding]:
    """A commit message — invariant #13 names it, and it can never be rewritten once pushed."""
    return _scan_blobs({"COMMIT_EDITMSG": path.read_bytes()}, accepted)


def scan_commits(repo: Path, rev_range: str, accepted: Accepted, baseline: Path) -> list[Finding]:
    """Every commit in the range, each at its own content — not just the range's final tree.

    A secret added in one commit and deleted in the next never reaches the final tree, but a push
    publishes both commits, so each commit's added/changed blobs and its message are scanned.
    Merge commits are diffed with --cc: only what the merge itself introduced.
    """
    skip = _repo_relative(baseline, repo)
    shas = _git("rev-list", "--reverse", rev_range, cwd=repo).decode().split()
    findings: list[Finding] = []
    for sha in shas:
        short = sha[:12]
        message = _git("log", "-1", "--format=%B", sha, cwd=repo)
        findings += _scan_blobs({"COMMIT_MSG": message}, accepted, short)
        diff_tree = ["diff-tree", "--no-commit-id", "-r", "--root", "--cc", "-z", "--name-only"]
        names = _split_z(_git(*diff_tree, "--diff-filter=ACMRT", sha, cwd=repo))
        blobs = {n: _git("show", f"{sha}:{n}", cwd=repo) for n in names if n != skip}
        findings += _scan_blobs(blobs, accepted, short)
    return findings


def _name_for(path: Path, repo: Path) -> str:
    """Repo-relative when inside the repo — the baseline's key — absolute otherwise."""
    return _repo_relative(path, repo) or str(path.resolve())


def accept(repo: Path, paths: list[Path], baseline: Path) -> list[Finding]:
    """Add every current finding in `paths` to the baseline as a reviewed false positive.

    Appends only: entries for every other path are kept exactly as they are (the stock
    `detect-secrets scan --baseline FILE` drops them). Each entry is pinned to its path, detector
    and value hash, so it accepts that one value in that one file and nothing else. The caller
    must have looked at every line first — this is the reviewable diff, not the review.
    """
    config = json.loads(baseline.read_text())
    _load_baseline(baseline)
    found = SecretsCollection(root=str(repo))
    found.scan_files(*(_name_for(p, repo) for p in paths), num_processors=1)
    results: dict[str, list[dict[str, object]]] = config["results"]
    added: list[Finding] = []
    for name, secret in found:
        entries = results.setdefault(name, [])
        if any(
            (e["hashed_secret"], e["type"]) == (secret.secret_hash, secret.type) for e in entries
        ):
            continue
        entries.append(
            {
                "type": secret.type,
                "filename": name,
                "hashed_secret": secret.secret_hash,
                "is_verified": False,
                "line_number": secret.line_number,
            }
        )
        added.append(Finding(name, secret.line_number or 0, secret.type))
    config["results"] = {
        name: sorted(entries, key=lambda e: (str(e["type"]), str(e["hashed_secret"])))
        for name, entries in sorted(results.items())
    }
    baseline.write_text(json.dumps(config, indent=2) + "\n")
    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="scan the index (pre-commit)")
    mode.add_argument("--message", type=Path, metavar="FILE", help="scan a commit message file")
    mode.add_argument("--commits", metavar="RANGE", help="scan every commit in a rev range (CI)")
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
    if not args.baseline.is_file():
        print(f"secret-scan: baseline not found: {args.baseline}", file=sys.stderr)
        return 2
    missing = [p for p in args.paths if not p.is_file()]
    if missing:
        print(f"secret-scan: not a file: {missing[0]}", file=sys.stderr)
        return 2
    if args.accept:
        try:
            added = accept(args.repo, args.paths, args.baseline)
        except ScanConfigError as exc:
            print(f"secret-scan: {exc}", file=sys.stderr)
            return 2
        for finding in added:
            print(f"accepted  {finding}")
        print(f"secret-scan: {len(added)} entr(y/ies) added to {args.baseline} — review the diff")
        return 0
    try:
        accepted = _load_baseline(args.baseline)
    except ScanConfigError as exc:
        print(f"secret-scan: {exc}", file=sys.stderr)
        return 2
    try:
        if args.staged:
            findings = scan_staged(args.repo, accepted, args.baseline)
        elif args.message:
            findings = scan_message(args.message, accepted)
        elif args.commits:
            findings = scan_commits(args.repo, args.commits, accepted, args.baseline)
        elif args.paths:
            names = [_name_for(p, args.repo) for p in args.paths]
            findings = _scan(names, args.repo, accepted, "")
        else:
            findings = scan_tree(args.repo, accepted, args.baseline)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip() if exc.stderr else ""
        print(f"secret-scan: git failed: {stderr}", file=sys.stderr)
        return 2

    if not findings:
        print("secret-scan: clean")
        return 0
    print(f"secret-scan: {len(findings)} finding(s) — the value is never printed:", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(
        "If any is real: rotate it at the provider FIRST (ops/runbooks/secret-leak.md). "
        "False positive: ops/runbooks/secret-scan.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
