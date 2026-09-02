"""M1.12 — the D6 archive publisher and the `/archives` download surface.

Drives the publisher end to end for one real backfilled session (2026-08-07): the checked-in UDiFF
bhavcopy (M1.5) and delivery file (M1.6) are written to a tmp L0, the L1 `prices_raw` partition is
rebuilt from them (M1.8), and a bundle is published from that partition into a tmp archive root and
recorded in a scratch database. Every acceptance criterion of the task is a test here:

  1. a bundle is produced for a real backfilled date with a checksum-complete manifest
     (`test_manifest_is_checksum_complete`, `test_bundle_row_is_recorded`)
  2. `/archives?date=` returns the manifest and the files download and verify
     (`test_archives_endpoint_serves_manifest`, `test_files_download_and_verify`)
  3. manifest lineage maps every output file to the L0 refs it derived from
     (`test_lineage_maps_every_file_to_its_l0_refs`)

Runs against a scratch database dropped afterwards (needs the docker postgres — `make up`); the lake
is entirely under `tmp_path`. Never touches the network (B8).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Final

import psycopg
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from dataplatform.archives.manifest import MANIFEST_FILENAME
from dataplatform.archives.publisher import (
    ArchivePublishError,
    PublishReport,
    publish_bundle,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.master import Exchange, IdentityMaster, SymbolWindow
from dataplatform.ingest.nse import bhavcopy
from dataplatform.status.api import app, clock_source, settings_source
from dataplatform.status.models import ArchivesOut
from dataplatform.store.db import connect, connection, with_dbname
from dataplatform.store.l0 import L0Store
from dataplatform.store.l1 import PRICES_RAW_DATASET, rebuild_prices_raw_from_l0
from dataplatform.store.migrate import migrate
from dataplatform.store.paths import l1_partition_path

pytestmark = pytest.mark.integration

REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
SESSION: Final = date(2026, 8, 7)
NOW: Final = datetime(2026, 8, 10, 18, 30, tzinfo=IST)

_BHAVCOPY_FILE: Final = "BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip"
_DELIVERY_FILE: Final = "sec_bhavdata_full_07082026.csv"
_BHAVCOPY_PATH: Final = REPO_ROOT / "tests" / "fixtures" / "nse_bhavcopy" / "udiff" / _BHAVCOPY_FILE
_DELIVERY_PATH: Final = REPO_ROOT / "tests" / "fixtures" / "nse_delivery" / _DELIVERY_FILE

_BHAVCOPY_SOURCE: Final = "nse_bhavcopy"
_DELIVERY_SOURCE: Final = "nse_sec_bhavdata_full"

#: Keyed by pid so parallel build agents against one Postgres do not drop each other's database.
SCRATCH_DB: Final = f"trading_m1_12_archives_{os.getpid()}"


# ── database ─────────────────────────────────────────────────────────────────────────────────


def _settings_for(dbname: str, *, data_root: Path) -> Settings:
    """Settings for the configured server, a chosen database, and a chosen lake root."""
    return Settings(database_url=with_dbname(Settings().database_url, dbname), data_root=data_root)


@pytest.fixture(scope="session")
def _scratch_db() -> Iterator[str]:
    """Create the scratch database for the session and drop it afterwards."""
    admin = _settings_for("postgres", data_root=REPO_ROOT / "data")
    try:
        conn = connect(admin, autocommit=True)
    except psycopg.OperationalError as error:  # pragma: no cover - environment, not logic
        pytest.skip(f"postgres is not reachable — run `make up` first: {error}")
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
    finally:
        conn.close()

    yield SCRATCH_DB

    conn = connect(admin, autocommit=True)
    try:
        conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    finally:
        conn.close()


@pytest.fixture
def settings(_scratch_db: str, tmp_path: Path) -> Settings:
    """Scratch-database settings whose lake and archive root are this test's tmp dir."""
    built = _settings_for(_scratch_db, data_root=tmp_path)
    migrate(built, clock=FrozenClock(NOW))
    return built


@pytest.fixture
def clean_bundle_table(settings: Settings) -> Iterator[None]:
    """Empty `archive_bundle` before and after a test, so committed rows do not leak across."""
    with connection(settings, autocommit=True) as conn:
        conn.execute("DELETE FROM archive_bundle")
    yield
    with connection(settings, autocommit=True) as conn:
        conn.execute("DELETE FROM archive_bundle")


# ── lake ─────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def l0(tmp_path: Path) -> L0Store:
    """An L0 store rooted in the test's lake, with the session's two raw fixtures written in."""
    store = L0Store(clock=FrozenClock(SESSION), data_root=tmp_path)
    store.put(_BHAVCOPY_SOURCE, SESSION, _BHAVCOPY_FILE, _BHAVCOPY_PATH.read_bytes())
    store.put(_DELIVERY_SOURCE, SESSION, _DELIVERY_FILE, _DELIVERY_PATH.read_bytes())
    return store


@pytest.fixture
def master() -> IdentityMaster:
    """A master resolving every symbol in the session to the ISIN its price row carries (#2)."""
    price_rows = bhavcopy.parse(
        _BHAVCOPY_PATH.read_bytes(), filename=_BHAVCOPY_FILE, trade_date=SESSION
    )
    windows = {
        (row.symbol, row.isin): SymbolWindow(
            exchange=Exchange.NSE,
            symbol=row.symbol,
            valid_from=date(2000, 1, 1),
            valid_to=None,
            isin=row.isin,
        )
        for row in price_rows
    }
    return IdentityMaster(tuple(windows.values()))


@pytest.fixture
def backfilled(l0: L0Store, master: IdentityMaster, tmp_path: Path) -> Path:
    """A backfilled session: the L1 `prices_raw` partition rebuilt from L0. Returns the lake."""
    bhav_ref = l0.ref_for(_BHAVCOPY_SOURCE, SESSION, _BHAVCOPY_FILE)
    deliv_ref = l0.ref_for(_DELIVERY_SOURCE, SESSION, _DELIVERY_FILE)
    rebuild_prices_raw_from_l0(
        l0, bhav_ref, delivery_ref=deliv_ref, master=master, data_root=tmp_path
    )
    return tmp_path


@pytest.fixture
def published(
    settings: Settings, clean_bundle_table: None, l0: L0Store, backfilled: Path, tmp_path: Path
) -> PublishReport:
    """Publish the session's bundle into the tmp archive root and commit its row."""
    with connection(settings, autocommit=True) as conn:
        return publish_bundle(
            conn, l0, SESSION, clock=FrozenClock(NOW), archive_root=tmp_path, data_root=tmp_path
        )


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    """The real status app pointed at the scratch database and this test's clock."""

    @contextmanager
    def _build() -> Iterator[TestClient]:
        app.dependency_overrides[settings_source] = lambda: settings
        app.dependency_overrides[clock_source] = lambda: FrozenClock(NOW)
        try:
            with TestClient(app) as live:
                yield live
        finally:
            app.dependency_overrides.clear()

    with _build() as live:
        yield live


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _l1_row_count(data_root: Path) -> int:
    metadata = pq.read_metadata(l1_partition_path(PRICES_RAW_DATASET, SESSION, data_root=data_root))
    return int(metadata.num_rows)


# ── acceptance 1: a bundle with a checksum-complete manifest ──────────────────────────────────


def test_manifest_is_checksum_complete(published: PublishReport, backfilled: Path) -> None:
    """Every file the manifest names exists, and its sha256, byte size and row count are right."""
    manifest = json.loads((published.bundle_dir / MANIFEST_FILENAME).read_bytes())
    files = manifest["files"]
    names = {entry["name"] for entry in files}
    assert names == {"prices_raw.parquet", "prices_raw.csv"}

    expected_rows = _l1_row_count(backfilled)
    for entry in files:
        on_disk = (published.bundle_dir / entry["path"]).read_bytes()
        assert _sha256(on_disk) == entry["sha256"], entry["name"]
        assert len(on_disk) == entry["bytes"], entry["name"]
        assert entry["rows"] == expected_rows, entry["name"]

    # The report reconciles with the manifest it wrote.
    assert published.file_count == len(files)
    assert published.total_bytes == sum(entry["bytes"] for entry in files)


def test_manifest_sha256_matches_the_file_on_disk(published: PublishReport) -> None:
    """`manifest_sha256` is the hash of the actual `manifest.json`, not an assumed value."""
    manifest_bytes = (published.bundle_dir / MANIFEST_FILENAME).read_bytes()
    assert published.manifest_sha256 == _sha256(manifest_bytes)


def test_bundle_row_is_recorded(published: PublishReport, settings: Settings) -> None:
    """The publisher wrote one `archive_bundle` row, matching the report and the manifest."""
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT schema_version, bundle_path, manifest_sha256, file_count, total_bytes, "
            "manifest FROM archive_bundle WHERE logical_date = %s",
            (SESSION,),
        ).fetchone()
    assert row is not None
    schema_version, bundle_path, manifest_sha, file_count, total_bytes, manifest = row
    assert schema_version == "1"
    assert bundle_path == published.bundle_path == f"archives/{SESSION.isoformat()}"
    assert manifest_sha == published.manifest_sha256
    assert file_count == published.file_count
    assert total_bytes == published.total_bytes
    assert {entry["name"] for entry in manifest["files"]} == set(published.filenames)


def test_republish_is_byte_identical(
    settings: Settings, clean_bundle_table: None, l0: L0Store, backfilled: Path, tmp_path: Path
) -> None:
    """Bundles are derived, not L0: re-publishing the same date reproduces identical file bytes."""
    with connection(settings, autocommit=True) as conn:
        first = publish_bundle(
            conn, l0, SESSION, clock=FrozenClock(NOW), archive_root=tmp_path, data_root=tmp_path
        )
        before = {p.name: p.read_bytes() for p in sorted(first.bundle_dir.iterdir())}
        second = publish_bundle(
            conn, l0, SESSION, clock=FrozenClock(NOW), archive_root=tmp_path, data_root=tmp_path
        )
    after = {p.name: p.read_bytes() for p in sorted(second.bundle_dir.iterdir())}
    assert after == before
    assert second.manifest_sha256 == first.manifest_sha256


# ── acceptance 2: /archives serves the manifest and the files download and verify ─────────────


def test_archives_endpoint_serves_manifest(published: PublishReport, api: TestClient) -> None:
    """`GET /archives?date=` returns the manifest the publisher recorded."""
    body = ArchivesOut.model_validate(
        api.get("/archives", params={"date": SESSION.isoformat()}).json()
    )
    assert body.bundle is not None
    assert body.bundle.date == SESSION
    assert body.bundle.bundle_path == f"archives/{SESSION.isoformat()}"
    assert body.bundle.manifest_sha256 == published.manifest_sha256
    assert body.bundle.file_count == published.file_count
    assert {file.name for file in body.bundle.files} == set(published.filenames)


def test_files_download_and_verify(published: PublishReport, api: TestClient) -> None:
    """Every manifested file downloads over the API and its bytes hash to the recorded sha256."""
    body = ArchivesOut.model_validate(
        api.get("/archives", params={"date": SESSION.isoformat()}).json()
    )
    assert body.bundle is not None
    assert body.bundle.files  # a real bundle has files to download

    for file in body.bundle.files:
        response = api.get(
            "/archives/download", params={"date": SESSION.isoformat(), "name": file.name}
        )
        assert response.status_code == 200, file.name
        assert _sha256(response.content) == file.sha256, file.name
        assert len(response.content) == file.bytes, file.name


def test_download_rejects_a_file_not_in_the_manifest(
    published: PublishReport, api: TestClient
) -> None:
    """A name the manifest does not list is a 404 — which is also what forecloses traversal."""
    assert (
        api.get(
            "/archives/download",
            params={"date": SESSION.isoformat(), "name": "../../etc/passwd"},
        ).status_code
        == 404
    )
    assert (
        api.get(
            "/archives/download", params={"date": SESSION.isoformat(), "name": "nope.parquet"}
        ).status_code
        == 404
    )


def test_download_for_an_unpublished_date_is_404(published: PublishReport, api: TestClient) -> None:
    """No bundle for the date means no download, even for a plausible file name."""
    assert (
        api.get(
            "/archives/download",
            params={"date": date(2026, 8, 6).isoformat(), "name": "prices_raw.parquet"},
        ).status_code
        == 404
    )


# ── acceptance 3: manifest lineage maps every file to the L0 refs it derived from ─────────────


def test_lineage_maps_every_file_to_its_l0_refs(published: PublishReport, l0: L0Store) -> None:
    """Each output file's lineage names exactly the date's L0 payloads, with their checksums."""
    manifest = json.loads((published.bundle_dir / MANIFEST_FILENAME).read_bytes())

    expected = {
        (ref.source, ref.filename, ref.sha256) for ref in l0.iter_refs(start=SESSION, end=SESSION)
    }
    assert expected  # the fixtures put both the bhavcopy and delivery payloads in L0

    assert {entry["name"] for entry in manifest["files"]} == set(published.filenames)
    for entry in manifest["files"]:
        refs = entry["lineage"]
        assert refs, entry["name"]  # no output file may have empty lineage
        got = {(ref["source"], ref["filename"], ref["sha256"]) for ref in refs}
        assert got == expected, entry["name"]


def test_db_manifest_preserves_lineage(published: PublishReport, settings: Settings) -> None:
    """The stored `archive_bundle.manifest` keeps lineage too, beside the ArchiveFileOut `files`."""
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT manifest FROM archive_bundle WHERE logical_date = %s", (SESSION,)
        ).fetchone()
    assert row is not None
    manifest = row[0]
    assert set(manifest["lineage"].keys()) == set(published.filenames)
    for name in published.filenames:
        assert manifest["lineage"][name], name


def test_publish_without_l0_lineage_fails_loud(
    settings: Settings, clean_bundle_table: None, master: IdentityMaster, tmp_path: Path
) -> None:
    """A date whose L1 exists but whose L0 is absent cannot claim lineage — it must refuse."""
    # Build only the L1 partition, with an L0 that has nothing for the date.
    empty_l0 = L0Store(clock=FrozenClock(SESSION), data_root=tmp_path)
    loaded_l0 = L0Store(clock=FrozenClock(SESSION), data_root=tmp_path / "raw")
    loaded_l0.put(_BHAVCOPY_SOURCE, SESSION, _BHAVCOPY_FILE, _BHAVCOPY_PATH.read_bytes())
    loaded_l0.put(_DELIVERY_SOURCE, SESSION, _DELIVERY_FILE, _DELIVERY_PATH.read_bytes())
    bhav_ref = loaded_l0.ref_for(_BHAVCOPY_SOURCE, SESSION, _BHAVCOPY_FILE)
    deliv_ref = loaded_l0.ref_for(_DELIVERY_SOURCE, SESSION, _DELIVERY_FILE)
    rebuild_prices_raw_from_l0(
        loaded_l0, bhav_ref, delivery_ref=deliv_ref, master=master, data_root=tmp_path
    )

    with connection(settings, autocommit=True) as conn, pytest.raises(ArchivePublishError):
        publish_bundle(
            conn,
            empty_l0,
            SESSION,
            clock=FrozenClock(NOW),
            archive_root=tmp_path,
            data_root=tmp_path,
        )


def test_missing_l1_partition_fails_loud(
    settings: Settings, clean_bundle_table: None, l0: L0Store, tmp_path: Path
) -> None:
    """A date with L0 but no L1 partition is not backfilled — publishing must refuse, not empty."""
    with connection(settings, autocommit=True) as conn, pytest.raises(ArchivePublishError):
        publish_bundle(
            conn, l0, SESSION, clock=FrozenClock(NOW), archive_root=tmp_path, data_root=tmp_path
        )
    # Nothing was recorded for a date that could not be bundled.
    with connection(settings) as conn:
        count = conn.execute(
            "SELECT count(*) FROM archive_bundle WHERE logical_date = %s", (SESSION,)
        ).fetchone()
        assert count is not None
        assert count[0] == 0
