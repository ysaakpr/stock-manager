"""OPS: ops/l0-s3-sync.sh against a stub `aws` — no AWS call, no network.

The stub logs every argv it is given, serves a listing from a directory standing in for the
bucket, and reads the SSE-C key from the `fileb:///dev/fd/N` pipe the script hands it (recording
only its length). What is proved: an existing key is never re-uploaded, a bucket without
versioning is refused, a loose env or key file is refused before it is sourced or read, the key
never reaches argv, the bucket never reaches the output, and `verify` reports even when a sampled
download fails.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "l0-s3-sync.sh"
BUCKET = "stub-bucket-123456789012"
KEY = bytes(range(7, 39))  # 32 bytes, no 0x00, so a raw leak would be visible in the log

STUB = """#!{python}
import base64, hashlib, json, os, shutil, sys

args = sys.argv[1:]
root = os.environ["STUB_REMOTE"]


def opt(name):
    return args[args.index(name) + 1] if name in args else None


key_len = None
for flag in ("--sse-c-key", "--sse-customer-key"):
    ref = opt(flag)
    if ref:
        with open(ref.removeprefix("fileb://"), "rb") as pipe:
            key = pipe.read()
        key_len = len(key)
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({{"argv": args, "key_len": key_len}}) + "\\n")

if args[:2] == ["s3api", "get-bucket-versioning"]:
    print(os.environ.get("STUB_VERSIONING", "Enabled"))
elif args[:2] == ["s3api", "get-object-lock-configuration"]:
    print("An error occurred (ObjectLockConfigurationNotFoundError)", file=sys.stderr)
    sys.exit(254)
elif args[:2] == ["s3api", "list-objects-v2"]:
    prefix = opt("--prefix") or ""
    rows = []
    for dirpath, _, names in os.walk(root):
        for name in names:
            full = os.path.join(dirpath, name)
            k = os.path.relpath(full, root)
            if k.startswith(prefix):
                rows.append((k, os.path.getsize(full)))
    rows.sort()
    if opt("--max-items"):
        rows = rows[: int(opt("--max-items"))]
    if not rows:
        print("None")
    for k, size in rows:
        print(f"{{k}}\\t{{size}}" if "Size" in opt("--query") else k)
elif args[:2] == ["s3api", "head-object"]:
    print(base64.b64encode(hashlib.md5(key).digest()).decode())
elif args[:2] == ["s3", "cp"]:
    src, dst = args[2], args[3]
    if src.startswith("s3://"):
        if os.environ.get("STUB_FAIL_DOWNLOAD"):
            print("download failed (stub)", file=sys.stderr)
            sys.exit(1)
        shutil.copyfile(os.path.join(root, src[5:].split("/", 1)[1]), dst)
    else:
        target = os.path.join(root, dst[5:].split("/", 1)[1])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(src, target)
else:
    sys.exit(f"stub aws: unexpected {{args}}")
"""


@dataclass(frozen=True)
class Call:
    """One stub invocation: its argv, and the length of the key it read from the pipe, if any."""

    argv: list[str]
    key_len: int | None


@dataclass
class Rig:
    tmp: Path
    l0: Path
    remote: Path
    env_file: Path
    key_file: Path
    log: Path
    env: dict[str, str]

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env={**self.env, **extra},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def calls(self) -> list[Call]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [Call(row["argv"], row["key_len"]) for row in rows]

    def uploads(self) -> list[str]:
        return [
            c.argv[2]
            for c in self.calls()
            if c.argv[:2] == ["s3", "cp"] and not c.argv[2].startswith("s3://")
        ]


def _settled(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    old = time.time() - 3600
    os.utime(path, (old, old))


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    home = tmp_path / "home"
    home.mkdir()
    l0 = tmp_path / "data" / "L0"
    _settled(l0 / "src" / "2024" / "a.csv", b"alpha\n")
    _settled(l0 / "src" / "2024" / "b c.csv", b"bravo with a space in the name\n")
    _settled(l0 / "src" / "2024" / "b c.csv.meta.json", b"{}\n")
    remote = tmp_path / "bucket"
    remote.mkdir()

    keydir = tmp_path / "keys"
    keydir.mkdir()
    key_file = keydir / "sm-raw.key.b64"
    key_file.write_text(base64.b64encode(KEY).decode() + "\n")
    key_file.chmod(0o400)
    (keydir / "sm-raw.key.fingerprint").write_text(hashlib.sha256(KEY).hexdigest()[:16] + "\n")
    env_file = tmp_path / "l0-s3.env"
    env_file.write_text(f"L0_S3_BUCKET={BUCKET}\nL0_S3_PREFIX=sm-raw\n")
    env_file.chmod(0o600)

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "aws"
    stub.write_text(STUB.format(python=sys.executable))
    stub.chmod(0o755)
    log = tmp_path / "aws.log"
    env = {
        "HOME": str(home),
        "USER": subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip(),
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "L0_S3_ENV": str(env_file),
        "SSE_C_KEY_B64": str(key_file),
        "DATA_ROOT": str(l0.parent),
        "STUB_REMOTE": str(remote),
        "STUB_LOG": str(log),
    }
    return Rig(tmp_path, l0, remote, env_file, key_file, log, env)


def _assert_quiet(rig: Rig, proc: subprocess.CompletedProcess[str]) -> None:
    """Neither the bucket nor the key, in any encoding, reaches the output or the stub's argv."""
    argv_text = rig.log.read_text() if rig.log.exists() else ""
    assert BUCKET not in proc.stdout + proc.stderr
    for text in (proc.stdout, proc.stderr, argv_text):
        assert base64.b64encode(KEY).decode() not in text
        assert KEY.hex() not in text
        assert KEY.decode("latin-1") not in text


def test_sync_uploads_only_keys_s3_lacks_and_never_overwrites(rig: Rig) -> None:
    existing = rig.remote / "sm-raw" / "src" / "2024" / "a.csv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"different bytes and size")  # must survive: names alone decide
    (rig.l0 / "src" / "2024" / "fresh.csv").write_bytes(b"still being written")

    proc = rig.run("sync")

    assert proc.returncode == 0, proc.stderr
    assert sorted(rig.uploads()) == [
        str(rig.l0 / "src" / "2024" / "b c.csv"),
        str(rig.l0 / "src" / "2024" / "b c.csv.meta.json"),
    ]
    assert existing.read_bytes() == b"different bytes and size"
    assert not (rig.remote / "sm-raw" / "src" / "2024" / "fresh.csv").exists()
    assert "uploaded 2, failed 0, deferred 1" in proc.stdout
    assert all(c.key_len == 32 for c in rig.calls() if c.argv[:2] == ["s3", "cp"])
    _assert_quiet(rig, proc)

    again = rig.run("sync")
    assert again.returncode == 0, again.stderr
    assert "to upload: 0" in again.stdout
    assert len(rig.uploads()) == 2  # nothing re-sent on the second run


@pytest.mark.parametrize("status", ["None", "Suspended"])
@pytest.mark.parametrize("mode", ["sync", "probe"])
def test_writes_refused_without_versioning(rig: Rig, mode: str, status: str) -> None:
    proc = rig.run(mode, STUB_VERSIONING=status)

    assert proc.returncode == 2
    assert "refusing to write" in proc.stderr
    assert not rig.uploads()
    assert not [c for c in rig.calls() if c.argv[1] in ("list-objects-v2", "head-object")]
    _assert_quiet(rig, proc)


@pytest.mark.parametrize("perm", [0o644, 0o640, 0o620, 0o604])
def test_loose_env_file_is_refused_before_sourcing(rig: Rig, perm: int) -> None:
    marker = rig.tmp / "sourced"
    rig.env_file.write_text(f"touch {marker}\nL0_S3_BUCKET={BUCKET}\n")
    rig.env_file.chmod(perm)

    proc = rig.run("sync")

    assert proc.returncode == 2
    assert "env file" in proc.stderr and "group/other" in proc.stderr
    assert not marker.exists()
    assert not rig.calls()


@pytest.mark.parametrize("perm", [0o644, 0o440, 0o604, 0o700])
def test_loose_key_file_is_refused_before_reading(rig: Rig, perm: int) -> None:
    rig.key_file.chmod(perm)

    proc = rig.run("sync")

    assert proc.returncode == 2
    assert "SSE-C key" in proc.stderr
    assert not rig.calls()


def test_missing_fingerprint_is_refused(rig: Rig) -> None:
    (rig.key_file.parent / "sm-raw.key.fingerprint").unlink()

    proc = rig.run("probe")

    assert proc.returncode == 2
    assert "fingerprint" in proc.stderr
    assert not rig.calls()


def test_probe_heads_an_object_from_the_listing_and_checks_its_key_md5(rig: Rig) -> None:
    assert rig.run("sync").returncode == 0

    proc = rig.run("probe")

    assert proc.returncode == 0, proc.stderr
    heads = [c for c in rig.calls() if c.argv[:2] == ["s3api", "head-object"]]
    assert len(heads) == 1 and heads[0].key_len == 32
    assert "sm-raw/src/2024/a.csv" in heads[0].argv
    _assert_quiet(rig, proc)


def test_probe_on_an_empty_mirror_does_not_treat_none_as_a_key(rig: Rig) -> None:
    proc = rig.run("probe")

    assert proc.returncode == 1
    assert "nothing under sm-raw/ to probe" in proc.stderr
    assert not [c for c in rig.calls() if c.argv[1] == "head-object"]


def test_verify_passes_and_reports_the_true_sample_size(rig: Rig) -> None:
    assert rig.run("sync").returncode == 0

    proc = rig.run("verify", "300")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "sampled: 3 " in proc.stdout
    assert "l0-s3-sync: VERIFIED" in proc.stdout
    _assert_quiet(rig, proc)


def test_verify_counts_an_empty_listing_as_zero_objects(rig: Rig) -> None:
    proc = rig.run("verify", "0")

    assert proc.returncode == 1
    assert "remote objects: 0" in proc.stdout
    assert "missing or size-mismatched: 3  extra or size-mismatched: 0" in proc.stdout


def test_verify_prints_its_summary_when_a_sampled_download_fails(rig: Rig) -> None:
    assert rig.run("sync").returncode == 0

    proc = rig.run("verify", "2", STUB_FAIL_DOWNLOAD="1")

    assert proc.returncode == 1
    assert "download failures: 2" in proc.stdout
    assert "sampled: 2 " in proc.stdout
    assert "l0-s3-sync: NOT VERIFIED" in proc.stdout
    _assert_quiet(rig, proc)


def test_verify_checks_the_sample_against_the_l0_manifest(rig: Rig) -> None:
    assert rig.run("sync").returncode == 0
    manifest = rig.tmp / "MANIFEST.sha256"
    manifest.write_text(
        "".join(
            f"{'0' * 64 if p.name == 'a.csv' else hashlib.sha256(p.read_bytes()).hexdigest()}"
            f"  L0/{p.relative_to(rig.l0)}\n"
            for p in sorted(rig.l0.rglob("*"))
            if p.is_file()
        )
    )

    proc = rig.run("verify", "3", L0_MANIFEST=str(manifest))

    assert proc.returncode == 1
    assert "3 of them in" in proc.stdout
    assert "sha256 mismatch with manifest: src/2024/a.csv" in proc.stdout
    assert "local 0, manifest 1" in proc.stdout
