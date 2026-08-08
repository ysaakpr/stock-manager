"""Invariant #1 has exactly one implementation, and this is its proof (§4.2, AGENTIC_CONTEXT §6.1).

Everything downstream — every L1 row, every adjusted series, every backtest — is a derivation of
L0, so "the raw bytes never changed" is the assumption the whole platform is standing on. Three
things therefore have to be shown, not asserted in a docstring:

* `put` is write-once: identical bytes are a no-op, different bytes under the same key raise, and
  neither path edits what is already stored,
* no code path in `l0.py` *can* modify or delete a payload — checked by parsing the module, since
  a behavioural test can only show that the paths it happened to call did not,
* `verify_checksums` actually notices damage, including damage introduced behind the module's back.
"""

from __future__ import annotations

import ast
import hashlib
import stat
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from dataplatform.clock import IST, FrozenClock
from dataplatform.store import (
    DEFAULT_CONTENT_TYPE,
    L0ChecksumError,
    L0DefectKind,
    L0ImmutabilityError,
    L0MetadataError,
    L0NotFoundError,
    L0Ref,
    L0Store,
    PathLayoutError,
    l0_meta_path,
    l0_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
L0_MODULE = REPO_ROOT / "dataplatform" / "store" / "l0.py"

SOURCE = "nse_bhavcopy"
TRADING_DATE = date(2026, 8, 7)
BHAVCOPY = "BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip"
PAYLOAD = b"PK\x03\x04 pretend this is a zipped bhavcopy"
OTHER_PAYLOAD = b"PK\x03\x04 a different file under the same name"
FETCHED_AT = datetime(2026, 8, 7, 19, 30, tzinfo=IST)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(FETCHED_AT)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> L0Store:
    return L0Store(clock=clock, data_root=tmp_path)


def corrupt(path: Path, content: bytes) -> None:
    """Overwrite a stored payload from outside the module, as a bit flip or a stray tool would.

    The chmod is part of the point: L0 files are created read-only, so even a deliberate
    corruption has to escalate privileges on the file first.
    """
    path.chmod(0o644)
    path.write_bytes(content)


# ── put: the write-once contract ─────────────────────────────────────────────────────────────


def test_put_stores_the_payload_and_a_sidecar_at_the_layout_path(
    store: L0Store, tmp_path: Path
) -> None:
    ref = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD, content_type="application/zip")

    payload_path = l0_path(SOURCE, TRADING_DATE, BHAVCOPY, data_root=tmp_path)
    assert payload_path.read_bytes() == PAYLOAD
    assert l0_meta_path(payload_path).is_file()
    assert store.path_of(ref) == payload_path
    assert store.exists(SOURCE, TRADING_DATE, BHAVCOPY)


def test_the_ref_records_checksum_size_content_type_and_the_injected_fetch_time(
    store: L0Store,
) -> None:
    ref = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD, content_type="application/zip")

    assert ref.sha256 == hashlib.sha256(PAYLOAD).hexdigest()
    assert ref.size_bytes == len(PAYLOAD)
    assert ref.content_type == "application/zip"
    assert ref.fetched_at == FETCHED_AT
    assert ref.fetched_at.tzinfo is not None
    assert ref.key == f"{SOURCE}/2026-08-07/{BHAVCOPY}"


def test_content_type_defaults_to_octet_stream_when_the_source_declared_none(
    store: L0Store,
) -> None:
    ref = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)
    assert ref.content_type == DEFAULT_CONTENT_TYPE


def test_the_sidecar_round_trips_the_ref(store: L0Store) -> None:
    ref = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD, content_type="application/zip")
    assert store.ref_for(SOURCE, TRADING_DATE, BHAVCOPY) == ref


def test_put_of_identical_content_is_a_no_op_that_preserves_the_first_fetch(
    store: L0Store, clock: FrozenClock, tmp_path: Path
) -> None:
    """Acceptance 1, idempotent half: re-fetching the same file changes nothing at all."""
    first = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD, content_type="application/zip")
    payload_path = l0_path(SOURCE, TRADING_DATE, BHAVCOPY, data_root=tmp_path)
    written_at = payload_path.stat().st_mtime_ns

    clock.advance(timedelta(days=3))
    second = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD, content_type="application/zip")

    assert second == first
    assert second.fetched_at == FETCHED_AT, "fetched_at must record the *first* sighting"
    assert payload_path.stat().st_mtime_ns == written_at, "the payload file was rewritten"
    assert payload_path.read_bytes() == PAYLOAD


def test_put_of_different_content_for_an_existing_key_raises(store: L0Store) -> None:
    """Acceptance 1, conflicting half: same key, different bytes is a stop-the-world event."""
    store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD, content_type="application/zip")

    with pytest.raises(L0ImmutabilityError) as caught:
        store.put(SOURCE, TRADING_DATE, BHAVCOPY, OTHER_PAYLOAD, content_type="application/zip")

    assert BHAVCOPY in str(caught.value)


def test_a_rejected_put_leaves_the_stored_payload_and_sidecar_untouched(
    store: L0Store, tmp_path: Path
) -> None:
    original = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD, content_type="application/zip")
    payload_path = l0_path(SOURCE, TRADING_DATE, BHAVCOPY, data_root=tmp_path)
    sidecar_before = l0_meta_path(payload_path).read_text(encoding="utf-8")

    with pytest.raises(L0ImmutabilityError):
        store.put(SOURCE, TRADING_DATE, BHAVCOPY, OTHER_PAYLOAD)

    assert payload_path.read_bytes() == PAYLOAD
    assert l0_meta_path(payload_path).read_text(encoding="utf-8") == sidecar_before
    assert store.get(original) == PAYLOAD


def test_a_payload_that_disagrees_with_its_sidecar_is_never_repaired_by_a_put(
    store: L0Store, tmp_path: Path
) -> None:
    """Even re-putting the *correct* bytes will not overwrite a damaged file — repair is §3.10."""
    store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)
    payload_path = l0_path(SOURCE, TRADING_DATE, BHAVCOPY, data_root=tmp_path)
    corrupt(payload_path, b"damaged")

    with pytest.raises(L0ImmutabilityError):
        store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)

    assert payload_path.read_bytes() == b"damaged"


def test_stored_files_are_created_without_write_permission(store: L0Store, tmp_path: Path) -> None:
    ref = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)

    for path in (store.path_of(ref), store.meta_path_of(ref)):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH), f"{path} is writable"


def test_the_same_filename_under_a_different_month_or_source_is_a_different_key(
    store: L0Store,
) -> None:
    first = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)
    other_month = store.put(SOURCE, date(2026, 7, 6), BHAVCOPY, OTHER_PAYLOAD)
    other_source = store.put("nse_secbhav", TRADING_DATE, BHAVCOPY, OTHER_PAYLOAD)

    assert store.get(first) == PAYLOAD
    assert store.get(other_month) == OTHER_PAYLOAD
    assert store.get(other_source) == OTHER_PAYLOAD


def test_two_dates_in_one_month_sharing_a_filename_collide_loudly(store: L0Store) -> None:
    """The layout partitions by month, so this is one key — and answering with the wrong date
    would be worse than refusing. Real sources date their filenames; this is the guard for one
    that does not."""
    store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)

    with pytest.raises(L0ImmutabilityError, match="collide"):
        store.put(SOURCE, date(2026, 8, 6), BHAVCOPY, PAYLOAD)

    assert store.ref_for(SOURCE, TRADING_DATE, BHAVCOPY).logical_date == TRADING_DATE


def test_a_filename_that_tries_to_escape_its_directory_is_rejected(store: L0Store) -> None:
    with pytest.raises(PathLayoutError):
        store.put(SOURCE, TRADING_DATE, "../../etc/passwd", PAYLOAD)
    with pytest.raises(PathLayoutError):
        store.put("../escape", TRADING_DATE, BHAVCOPY, PAYLOAD)


def test_a_naive_fetch_time_is_rejected_at_the_ref(store: L0Store) -> None:
    """B10: a naive instant would make a replayed fetch time depend on the host's locale."""
    with pytest.raises(ValueError, match="tz-aware"):
        L0Ref(
            source=SOURCE,
            logical_date=TRADING_DATE,
            filename=BHAVCOPY,
            sha256="0" * 64,
            size_bytes=1,
            fetched_at=datetime(2026, 8, 7, 19, 30),  # naive on purpose
            content_type=DEFAULT_CONTENT_TYPE,
        )


# ── read ─────────────────────────────────────────────────────────────────────────────────────


def test_get_returns_the_exact_bytes_and_open_streams_them(store: L0Store) -> None:
    ref = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)

    assert store.get(ref) == PAYLOAD
    with store.open(ref) as handle:
        assert handle.read() == PAYLOAD


def test_get_refuses_to_hand_back_bytes_that_no_longer_match_their_checksum(
    store: L0Store, tmp_path: Path
) -> None:
    """A silent bit flip here would propagate into every table derived from this file."""
    ref = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)
    corrupt(l0_path(SOURCE, TRADING_DATE, BHAVCOPY, data_root=tmp_path), OTHER_PAYLOAD)

    with pytest.raises(L0ChecksumError):
        store.get(ref)


def test_reading_a_key_that_was_never_stored_fails_loudly(store: L0Store) -> None:
    ref = L0Ref(
        source=SOURCE,
        logical_date=TRADING_DATE,
        filename=BHAVCOPY,
        sha256="0" * 64,
        size_bytes=0,
        fetched_at=FETCHED_AT,
        content_type=DEFAULT_CONTENT_TYPE,
    )
    with pytest.raises(L0NotFoundError):
        store.get(ref)
    with pytest.raises(L0NotFoundError):
        store.open(ref)
    with pytest.raises(L0NotFoundError):
        store.ref_for(SOURCE, TRADING_DATE, BHAVCOPY)
    assert not store.exists(SOURCE, TRADING_DATE, BHAVCOPY)


def test_an_unreadable_sidecar_is_an_error_not_a_default(store: L0Store, tmp_path: Path) -> None:
    store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)
    corrupt(l0_meta_path(l0_path(SOURCE, TRADING_DATE, BHAVCOPY, data_root=tmp_path)), b"{nope")

    with pytest.raises(L0MetadataError):
        store.ref_for(SOURCE, TRADING_DATE, BHAVCOPY)


# ── iteration ────────────────────────────────────────────────────────────────────────────────


def stock_a_quarter(store: L0Store) -> None:
    """One file per source per day across a month boundary, so range pruning has work to do."""
    for day in (date(2026, 7, 30), date(2026, 8, 6), TRADING_DATE, date(2026, 9, 1)):
        stamp = day.strftime("%d%m%Y")
        store.put(SOURCE, day, f"bhav_{stamp}.csv.zip", f"bhav {stamp}".encode())
        store.put("nse_secbhav", day, f"sec_bhavdata_full_{stamp}.csv", f"sec {stamp}".encode())


def test_iter_refs_filters_by_source_and_inclusive_date_range(store: L0Store) -> None:
    stock_a_quarter(store)

    dates = [ref.logical_date for ref in store.iter_refs(SOURCE)]
    assert dates == [date(2026, 7, 30), date(2026, 8, 6), TRADING_DATE, date(2026, 9, 1)]
    assert {ref.source for ref in store.iter_refs(SOURCE)} == {SOURCE}

    windowed = list(store.iter_refs(SOURCE, start=date(2026, 8, 6), end=TRADING_DATE))
    assert [ref.logical_date for ref in windowed] == [date(2026, 8, 6), TRADING_DATE]

    assert [ref.logical_date for ref in store.iter_refs(SOURCE, end=date(2026, 7, 31))] == [
        date(2026, 7, 30)
    ]
    assert [ref.logical_date for ref in store.iter_refs(SOURCE, start=date(2026, 9, 1))] == [
        date(2026, 9, 1)
    ]


def test_iter_refs_without_a_source_covers_every_source_in_a_stable_order(store: L0Store) -> None:
    stock_a_quarter(store)

    keys = [ref.key for ref in store.iter_refs()]
    assert keys == sorted(keys), "iteration order must be deterministic for replay"
    assert len(keys) == 8
    assert keys == [ref.key for ref in store.iter_refs()]


def test_iter_refs_on_an_empty_lake_yields_nothing(store: L0Store) -> None:
    assert list(store.iter_refs()) == []


def test_an_inverted_date_range_is_a_programming_error(store: L0Store) -> None:
    with pytest.raises(ValueError, match="empty date range"):
        list(store.iter_refs(SOURCE, start=TRADING_DATE, end=date(2026, 7, 1)))


# ── verify_checksums ─────────────────────────────────────────────────────────────────────────


def test_verify_checksums_is_clean_on_an_intact_store(store: L0Store) -> None:
    stock_a_quarter(store)

    report = store.verify_checksums()

    assert report.ok
    assert report.checked == 8
    assert report.defects == ()


def test_verify_checksums_detects_a_corrupted_file(store: L0Store, tmp_path: Path) -> None:
    """Acceptance 3, and the reason a decade-old L0 tree is trustworthy at all."""
    stock_a_quarter(store)
    damaged = l0_path(SOURCE, TRADING_DATE, "bhav_07082026.csv.zip", data_root=tmp_path)
    corrupt(damaged, b"bhav 07082026 with one byte changed")

    report = store.verify_checksums()

    assert not report.ok
    assert [(defect.kind, defect.path) for defect in report.defects] == [
        (L0DefectKind.CHECKSUM_MISMATCH, damaged)
    ]
    assert report.checked == 8


def test_a_corruption_that_preserves_length_is_still_detected(
    store: L0Store, tmp_path: Path
) -> None:
    """Size alone would pass this; only the hash catches a single flipped byte."""
    ref = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)
    flipped = bytes([PAYLOAD[0] ^ 0x01]) + PAYLOAD[1:]
    corrupt(store.path_of(ref), flipped)

    report = store.verify_checksums()

    assert len(flipped) == ref.size_bytes
    assert [defect.kind for defect in report.defects] == [L0DefectKind.CHECKSUM_MISMATCH]


def test_verify_checksums_reports_a_sidecar_whose_payload_vanished(
    store: L0Store, tmp_path: Path
) -> None:
    ref = store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)
    payload_path = store.path_of(ref)
    payload_path.unlink()  # simulates a botched restore; nothing in l0.py can do this

    report = store.verify_checksums()

    assert [defect.kind for defect in report.defects] == [L0DefectKind.MISSING_PAYLOAD]
    assert report.defects[0].path == payload_path


def test_verify_checksums_reports_a_payload_nobody_claims(store: L0Store, tmp_path: Path) -> None:
    """An interrupted write, or a file dropped in by hand — either way it is not derivable-from."""
    store.put(SOURCE, TRADING_DATE, BHAVCOPY, PAYLOAD)
    orphan = l0_path(SOURCE, TRADING_DATE, "half_written.csv", data_root=tmp_path)
    orphan.write_bytes(b"truncat")

    report = store.verify_checksums()

    assert [(defect.kind, defect.path) for defect in report.defects] == [
        (L0DefectKind.ORPHAN_PAYLOAD, orphan)
    ]
    assert report.checked == 1


def test_verify_checksums_reports_an_unparseable_sidecar_and_keeps_going(
    store: L0Store, tmp_path: Path
) -> None:
    stock_a_quarter(store)
    payload = l0_path(SOURCE, TRADING_DATE, "bhav_07082026.csv.zip", data_root=tmp_path)
    corrupt(l0_meta_path(payload), b'{"source": "nse_bhavcopy"')

    report = store.verify_checksums()

    # One defect, not two: the payload is still claimed, so it is not also reported as an orphan.
    assert [defect.kind for defect in report.defects] == [L0DefectKind.UNREADABLE_METADATA]
    assert report.checked == 7, "the other seven records were still verified"


def test_verify_checksums_can_be_scoped_to_one_source_and_range(
    store: L0Store, tmp_path: Path
) -> None:
    stock_a_quarter(store)
    corrupt(
        l0_path(SOURCE, date(2026, 9, 1), "bhav_01092026.csv.zip", data_root=tmp_path), b"changed"
    )

    inside = store.verify_checksums(SOURCE, start=date(2026, 9, 1))
    outside = store.verify_checksums(SOURCE, start=date(2026, 7, 1), end=date(2026, 8, 31))

    assert [defect.kind for defect in inside.defects] == [L0DefectKind.CHECKSUM_MISMATCH]
    assert inside.checked == 1
    assert outside.ok
    assert outside.checked == 3


# ── the module cannot mutate, by construction ────────────────────────────────────────────────
#
# Acceptance 2 is a property of every code path in l0.py, including the ones no test happens to
# call, so it is checked against the parsed module rather than against behaviour. The rules: no
# destructive filesystem primitive is called at all, every path opened for reading is opened
# read-only, and every creating `os.open` is exclusive. Together those leave no way to reach the
# bytes of a file that already exists.

#: Calls that could remove, replace, truncate or rewrite a stored file. Deliberately matched by
#: bare attribute name (`unlink`, `replace`, ...) with no regard for the receiver: l0.py must not
#: even *look* like it mutates, and the cost of the false positives — `str.replace`, mainly — is
#: one clear test failure telling a future maintainer to find another way.
DESTRUCTIVE_CALLS = frozenset(
    {
        "chmod",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "fchmod",
        "ftruncate",
        "lchmod",
        "move",
        "remove",
        "removedirs",
        "rename",
        "renames",
        "replace",
        "rmdir",
        "rmtree",
        "touch",
        "truncate",
        "unlink",
        "write_bytes",
        "write_text",
    }
)

#: Modes that only ever read. Anything with `w`, `a`, `+` or `x` in it is not on this list.
READ_MODES = frozenset({"r", "rb", "rt", "br"})


def module_tree(source: str) -> ast.Module:
    return ast.parse(source)


def called_name(node: ast.Call) -> str | None:
    """The bare name being called: `foo(...)`, `x.foo(...)` and `a.b.foo(...)` all give `foo`."""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _receiver(func: ast.Attribute) -> str | None:
    """The name a method is called on: `os.open` gives `os`, `path.open` gives `path`."""
    return func.value.id if isinstance(func.value, ast.Name) else None


def destructive_calls(tree: ast.Module) -> list[tuple[str, int]]:
    return [
        (name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (name := called_name(node)) is not None
        and name in DESTRUCTIVE_CALLS
    ]


def write_mode_opens(tree: ast.Module) -> list[tuple[str, int]]:
    """`open(...)`/`Path.open(...)` calls whose mode is not read-only.

    `os.open` is excluded here and checked by `exclusive_creation_flags` instead: it takes flags,
    not a mode string, and it is the one call in the module that creates a file.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or called_name(node) != "open":
            continue
        method = isinstance(node.func, ast.Attribute)
        if method and isinstance(node.func, ast.Attribute) and _receiver(node.func) == "os":
            continue
        # `path.open(mode)` puts the mode first; the builtin `open(path, mode)` puts it second.
        position = 0 if method else 1
        mode: ast.expr | None = node.args[position] if len(node.args) > position else None
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode = keyword.value
        if mode is None:  # the default, "r"
            continue
        if not (isinstance(mode, ast.Constant) and mode.value in READ_MODES):
            found.append((ast.unparse(mode), node.lineno))
    return found


def creating_opens_without_exclusive(tree: ast.Module) -> list[int]:
    """Line numbers of `os.open` calls that create a file without `O_EXCL` (or that truncate it)."""
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or called_name(node) != "open":
            continue
        if not isinstance(node.func, ast.Attribute) or _receiver(node.func) != "os":
            continue
        if len(node.args) < 2:
            continue
        flags = {child.attr for child in ast.walk(node.args[1]) if isinstance(child, ast.Attribute)}
        writes = bool(flags & {"O_CREAT", "O_WRONLY", "O_RDWR", "O_APPEND", "O_TRUNC"})
        if writes and not {"O_CREAT", "O_EXCL"} <= flags:
            offenders.append(node.lineno)
        if flags & {"O_TRUNC", "O_APPEND"}:
            offenders.append(node.lineno)
    return offenders


def test_l0_calls_no_destructive_filesystem_primitive() -> None:
    """Acceptance 2: there is no code path that could remove or rewrite a stored payload."""
    found = destructive_calls(module_tree(L0_MODULE.read_text(encoding="utf-8")))
    assert not found, (
        "l0.py must not call anything that can modify or delete a stored file: "
        + ", ".join(f"{name}() at line {line}" for name, line in found)
    )


def test_l0_only_ever_opens_a_path_for_reading() -> None:
    found = write_mode_opens(module_tree(L0_MODULE.read_text(encoding="utf-8")))
    assert not found, "l0.py opened a path in a writable mode: " + ", ".join(
        f"{mode} at line {line}" for mode, line in found
    )


def test_every_file_l0_creates_is_created_exclusively() -> None:
    """`O_EXCL` is what makes write-once a kernel guarantee rather than a check-then-write race."""
    tree = module_tree(L0_MODULE.read_text(encoding="utf-8"))
    assert not creating_opens_without_exclusive(tree)


def test_the_scanners_are_not_vacuous() -> None:
    """A guard that has stopped matching anything would pass silently — so prove it still bites."""
    clean = L0_MODULE.read_text(encoding="utf-8")
    assert "os.open" in clean, "l0.py no longer creates files the way these guards assume"

    for offending in (
        "def wipe(path):\n    path.unlink()\n",
        "def wipe(path):\n    import shutil\n    shutil.rmtree(path)\n",
        "def wipe(path):\n    path.write_bytes(b'')\n",
    ):
        assert destructive_calls(module_tree(clean + "\n" + offending)), offending

    appended = "def sneak(path):\n    return path.open('wb')\n"
    assert write_mode_opens(module_tree(clean + "\n" + appended))

    truncating = "def sneak(path):\n    return os.open(path, os.O_WRONLY | os.O_CREAT)\n"
    assert creating_opens_without_exclusive(module_tree(clean + "\n" + truncating))


def test_the_public_surface_offers_no_way_to_delete_or_update() -> None:
    """The API itself must not tempt a caller: there is no `delete`, no `update`, no `overwrite`."""
    forbidden = ("delete", "remove", "update", "overwrite", "replace", "purge", "clean")
    offered = [name for name in dir(L0Store) if not name.startswith("_")]

    assert not [name for name in offered if any(word in name for word in forbidden)], offered
    assert "put" in offered and "get" in offered
