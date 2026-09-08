"""Comparing a stranded worktree lake against the authoritative one (W2 close-out).

This tool answers exactly one question — *may this L0 tree be deleted?* — and the only answer that
must never be wrong is "yes". So each test here pins one way the answer could be a false yes: a
payload the authoritative lake has never seen, one it has under the same key with different bytes,
one whose sidecar was lost so its key is unknowable, and one whose bytes no longer match the
sidecar sitting beside them.

Offline and deterministic: every byte is written into `tmp_path` by the test itself.
"""

from __future__ import annotations

import json
import stat
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dataplatform.clock import FrozenClock
from dataplatform.store.l0 import L0Store
from dataplatform.store.l0_compare import compare_lakes, enumerate_payloads

IST = ZoneInfo("Asia/Kolkata")
SOURCE = "nse_pr_bundle"
DAY = date(2010, 1, 4)


def _store(root: Path) -> L0Store:
    return L0Store(clock=FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=IST)), data_root=root)


def _lake(root: Path, payloads: dict[str, bytes], *, day: date = DAY) -> L0Store:
    """A lake at `root` holding `filename -> bytes`, all under one logical date."""
    store = _store(root)
    for filename, payload in payloads.items():
        store.put(SOURCE, day, filename, payload)
    return store


def _rewrite(path: Path, payload: bytes) -> None:
    """Overwrite a stored file — L0 makes them read-only, so the mode comes off first."""
    path.chmod(stat.S_IWUSR | stat.S_IRUSR)
    path.write_bytes(payload)


# ── enumeration ──────────────────────────────────────────────────────────────────────────────


def test_enumerate_reports_the_real_digest_and_the_recorded_one(tmp_path: Path) -> None:
    _lake(tmp_path, {"PR040110.zip": b"bundle-bytes"})

    (record,) = enumerate_payloads(tmp_path)

    assert record.key == f"{SOURCE}/2010-01-04/PR040110.zip"
    assert record.source == SOURCE
    assert record.logical_date == DAY
    assert record.sha256 == record.recorded_sha256
    assert record.matches_own_sidecar
    assert not record.is_orphan
    assert record.size_bytes == len(b"bundle-bytes")


def test_enumerate_is_empty_for_a_tree_with_no_l0(tmp_path: Path) -> None:
    assert enumerate_payloads(tmp_path) == ()


def test_enumerate_can_be_narrowed_to_one_source(tmp_path: Path) -> None:
    store = _lake(tmp_path, {"PR040110.zip": b"a"})
    store.put("nifty_tri_history", DAY, "tri.json", b"b")

    assert [r.source for r in enumerate_payloads(tmp_path, source=SOURCE)] == [SOURCE]
    assert len(enumerate_payloads(tmp_path)) == 2


# ── the answer that must never be a false yes ────────────────────────────────────────────────


def test_a_fully_duplicated_tree_is_safe_to_delete(tmp_path: Path) -> None:
    payloads = {"PR040110.zip": b"one", "PR050110.zip": b"two"}
    _lake(tmp_path / "stranded", payloads)
    _lake(tmp_path / "authoritative", payloads | {"PR060110.zip": b"three"})

    comparison = compare_lakes(tmp_path / "stranded", tmp_path / "authoritative")

    assert comparison.safe_to_delete
    assert comparison.checked == 2
    assert len(comparison.matched) == 2
    # The authoritative lake holding *more* is the normal case, not a finding: the comparison is
    # one-way on purpose.
    assert comparison.absent == ()


def test_a_payload_only_the_stranded_tree_holds_is_reported(tmp_path: Path) -> None:
    _lake(tmp_path / "stranded", {"PR040110.zip": b"one", "PR050110.zip": b"only-here"})
    _lake(tmp_path / "authoritative", {"PR040110.zip": b"one"})

    comparison = compare_lakes(tmp_path / "stranded", tmp_path / "authoritative")

    assert not comparison.safe_to_delete
    assert [record.filename for record in comparison.absent] == ["PR050110.zip"]


def test_the_same_key_with_different_bytes_is_a_mismatch_not_a_match(tmp_path: Path) -> None:
    _lake(tmp_path / "stranded", {"PR040110.zip": b"stranded-bytes"})
    _lake(tmp_path / "authoritative", {"PR040110.zip": b"different-bytes"})

    comparison = compare_lakes(tmp_path / "stranded", tmp_path / "authoritative")

    assert not comparison.safe_to_delete
    assert comparison.matched == ()
    ((record, recorded),) = comparison.mismatched
    assert record.filename == "PR040110.zip"
    assert recorded != record.sha256


def test_a_payload_with_no_sidecar_is_an_orphan_and_blocks_the_yes(tmp_path: Path) -> None:
    _lake(tmp_path / "stranded", {"PR040110.zip": b"one"})
    _lake(tmp_path / "authoritative", {"PR040110.zip": b"one"})
    sidecar = tmp_path / "stranded" / "L0" / SOURCE / "2010" / "01" / "PR040110.zip.meta.json"
    sidecar.unlink()

    comparison = compare_lakes(tmp_path / "stranded", tmp_path / "authoritative")

    assert not comparison.safe_to_delete
    assert [record.filename for record in comparison.orphans] == ["PR040110.zip"]
    # No sidecar means no logical date, so the key cannot be matched even though identical bytes
    # do sit in the authoritative lake. "Cannot tell" must not read as "yes".
    assert [record.key for record in comparison.absent] == [f"{SOURCE}/?/PR040110.zip"]


def test_an_unreadable_sidecar_is_treated_as_an_absent_one(tmp_path: Path) -> None:
    _lake(tmp_path / "stranded", {"PR040110.zip": b"one"})
    _lake(tmp_path / "authoritative", {"PR040110.zip": b"one"})
    sidecar = tmp_path / "stranded" / "L0" / SOURCE / "2010" / "01" / "PR040110.zip.meta.json"
    _rewrite(sidecar, b"{ truncated")

    comparison = compare_lakes(tmp_path / "stranded", tmp_path / "authoritative")

    assert not comparison.safe_to_delete
    assert len(comparison.orphans) == 1


def test_damage_in_the_stranded_copy_is_reported_separately(tmp_path: Path) -> None:
    _lake(tmp_path / "stranded", {"PR040110.zip": b"one"})
    _lake(tmp_path / "authoritative", {"PR040110.zip": b"one"})
    _rewrite(tmp_path / "stranded" / "L0" / SOURCE / "2010" / "01" / "PR040110.zip", b"rotted")

    comparison = compare_lakes(tmp_path / "stranded", tmp_path / "authoritative")

    assert not comparison.safe_to_delete
    assert [record.filename for record in comparison.self_inconsistent] == ["PR040110.zip"]
    # The rotted bytes do not hash to what the authoritative lake records either, so the same
    # payload is both self-inconsistent and a mismatch — two facts, reported as two.
    assert len(comparison.mismatched) == 1


def test_a_sidecar_with_a_bad_logical_date_does_not_raise(tmp_path: Path) -> None:
    _lake(tmp_path / "stranded", {"PR040110.zip": b"one"})
    _lake(tmp_path / "authoritative", {"PR040110.zip": b"one"})
    sidecar = tmp_path / "stranded" / "L0" / SOURCE / "2010" / "01" / "PR040110.zip.meta.json"
    raw = json.loads(sidecar.read_text(encoding="utf-8"))
    raw["logical_date"] = "not-a-date"
    _rewrite(sidecar, json.dumps(raw).encode("utf-8"))

    comparison = compare_lakes(tmp_path / "stranded", tmp_path / "authoritative")

    assert not comparison.safe_to_delete
    assert [record.key for record in comparison.absent] == [f"{SOURCE}/?/PR040110.zip"]


def test_summary_names_both_roots_and_every_count(tmp_path: Path) -> None:
    _lake(tmp_path / "stranded", {"PR040110.zip": b"one"})
    _lake(tmp_path / "authoritative", {"PR040110.zip": b"one"})

    summary = compare_lakes(tmp_path / "stranded", tmp_path / "authoritative").summary()

    assert "1 payload(s)" in summary
    assert str(tmp_path / "stranded") in summary
    assert str(tmp_path / "authoritative") in summary
