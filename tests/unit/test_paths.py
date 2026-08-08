"""The lake layout is a contract, not a convention (§4.2).

L0 immutability (invariant #1) only means anything if a re-derivation can find the exact bytes it
was derived from, which requires that every writer and every reader agree on the path down to the
character. These tests pin the three shapes and the validation that keeps a bad name from ever
becoming a directory.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dataplatform.config import get_settings
from dataplatform.store import (
    Layer,
    PathLayoutError,
    l0_dir,
    l0_meta_path,
    l0_path,
    l1_partition_dir,
    l1_partition_path,
    l2_partition_dir,
    l2_partition_path,
    layer_root,
    partition_date_of,
    partition_path,
)

TRADING_DATE = date(2026, 8, 7)
BHAVCOPY = "BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip"


def test_l0_is_partitioned_by_source_year_and_month(tmp_path: Path) -> None:
    path = l0_path("nse_bhavcopy", TRADING_DATE, BHAVCOPY, data_root=tmp_path)

    assert path == tmp_path / "L0" / "nse_bhavcopy" / "2026" / "08" / BHAVCOPY
    assert path.parent == l0_dir("nse_bhavcopy", TRADING_DATE, data_root=tmp_path)


def test_l0_keeps_the_source_filename_exactly_as_fetched(tmp_path: Path) -> None:
    """L0 is what the source served; renaming it loses the evidence that it was."""
    path = l0_path("nse_bhavcopy", TRADING_DATE, BHAVCOPY, data_root=tmp_path)
    assert path.name == BHAVCOPY


def test_l0_metadata_is_a_sidecar_beside_its_payload(tmp_path: Path) -> None:
    payload = l0_path("nse_bhavcopy", TRADING_DATE, BHAVCOPY, data_root=tmp_path)
    meta = l0_meta_path(payload)

    assert meta.parent == payload.parent
    assert meta.name == BHAVCOPY + ".meta.json"


def test_l1_is_a_hive_style_date_partition(tmp_path: Path) -> None:
    path = l1_partition_path("prices_raw", TRADING_DATE, data_root=tmp_path)

    assert path == tmp_path / "L1" / "prices_raw" / "date=2026-08-07" / "part.parquet"
    assert path.parent == l1_partition_dir("prices_raw", TRADING_DATE, data_root=tmp_path)


def test_l2_mirrors_l1_one_layer_over(tmp_path: Path) -> None:
    l1 = l1_partition_path("prices_raw", TRADING_DATE, data_root=tmp_path)
    l2 = l2_partition_path("prices_raw", TRADING_DATE, data_root=tmp_path)

    assert l2 == tmp_path / "L2" / "prices_raw" / "date=2026-08-07" / "part.parquet"
    assert l2.relative_to(tmp_path / "L2") == l1.relative_to(tmp_path / "L1")
    assert l2.parent == l2_partition_dir("prices_raw", TRADING_DATE, data_root=tmp_path)


def test_the_partition_date_reads_back_from_a_directory_or_a_file(tmp_path: Path) -> None:
    directory = l1_partition_dir("prices_raw", TRADING_DATE, data_root=tmp_path)

    assert partition_date_of(directory) == TRADING_DATE
    assert partition_date_of(directory / "part.parquet") == TRADING_DATE


def test_a_path_with_no_date_partition_raises_rather_than_guessing(tmp_path: Path) -> None:
    with pytest.raises(PathLayoutError, match="no date="):
        partition_date_of(tmp_path / "L1" / "prices_raw" / "part.parquet")


def test_the_default_root_comes_from_settings() -> None:
    assert layer_root(Layer.L0) == get_settings().data_root / "L0"


def test_l0_is_not_a_dataset_partition(tmp_path: Path) -> None:
    """L0 is keyed by source and month; asking it for a dataset partition is a bug, not a path."""
    with pytest.raises(PathLayoutError, match="L0 is partitioned by source"):
        partition_path(Layer.L0, "prices_raw", TRADING_DATE, data_root=tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        "NSE_Bhavcopy",  # macOS folds case and Linux does not: one source, two directories
        "nse/bhavcopy",
        "../escape",
        "",
        ".hidden",
        "nse bhavcopy",
    ],
)
def test_an_unusable_source_name_never_becomes_a_directory(source: str, tmp_path: Path) -> None:
    with pytest.raises(PathLayoutError):
        l0_dir(source, TRADING_DATE, data_root=tmp_path)


@pytest.mark.parametrize("dataset", ["Prices_Raw", "prices/raw", "..", ""])
def test_an_unusable_dataset_name_never_becomes_a_directory(dataset: str, tmp_path: Path) -> None:
    with pytest.raises(PathLayoutError):
        l1_partition_dir(dataset, TRADING_DATE, data_root=tmp_path)


@pytest.mark.parametrize("filename", ["sub/dir.csv", "..", ".", "", "../../etc/passwd"])
def test_a_filename_can_never_escape_its_directory(filename: str, tmp_path: Path) -> None:
    with pytest.raises(PathLayoutError):
        l0_path("nse_bhavcopy", TRADING_DATE, filename, data_root=tmp_path)
