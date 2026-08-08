"""L0/L1/L2 on-disk layout (§4.2) — the single source of truth for where a byte lives.

    L0  data/L0/{source}/{yyyy}/{mm}/{filename}          exact fetched files, immutable
    L1  data/L1/{dataset}/date={yyyy-mm-dd}/part.parquet  normalized, ISIN-keyed, raw prices
    L2  data/L2/{dataset}/date={yyyy-mm-dd}/part.parquet  derived; recomputable from L0 + L1

Every module that reads or writes the lake builds its paths here. Two modules that each format
their own directory string are two layouts, and L0's immutability guarantee (invariant #1) is only
worth anything if a re-derivation can find the exact bytes it was derived from.

The `date=` partition directory is Hive-style on purpose: DuckDB and pyarrow both read it as a
partition column, so the query layer (D4) filters by date without opening every file.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from pathlib import Path

from dataplatform.config import get_settings

__all__ = [
    "DEFAULT_PART_FILENAME",
    "Layer",
    "PathLayoutError",
    "l0_dir",
    "l0_meta_path",
    "l0_path",
    "l1_partition_dir",
    "l1_partition_path",
    "l2_partition_dir",
    "l2_partition_path",
    "layer_root",
    "partition_date_of",
    "partition_dir",
    "partition_path",
]

#: Identifiers that name a directory in the lake. Lower-case only: macOS is case-insensitive and
#: Linux is not, so `NSE/` and `nse/` are one directory on the dev box and two on the server —
#: which would split a source's history in half the first time it happened.
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: The partition directory name L1/L2 use, e.g. `date=2026-08-07`.
_PARTITION = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")

#: The single file written per (dataset, date) partition. One file keeps a partition rewrite
#: atomic-ish and byte-comparable (M1.5's determinism check).
DEFAULT_PART_FILENAME = "part.parquet"


class PathLayoutError(ValueError):
    """A name that cannot be part of a lake path — empty, uppercase, or a path separator."""


class Layer(StrEnum):
    """The three storage layers of §4.2, and the directory each one occupies."""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


def layer_root(layer: Layer, *, data_root: Path | None = None) -> Path:
    """The root directory of one layer, e.g. `<data_root>/L0`."""
    root = get_settings().data_root if data_root is None else data_root
    return root / layer.value


def l0_dir(source: str, logical_date: date, *, data_root: Path | None = None) -> Path:
    """Directory holding everything fetched from `source` for `logical_date`.

    Partitioned by year and month so a decade of daily files stays browsable and a single
    directory never holds 3,650 entries.
    """
    return (
        layer_root(Layer.L0, data_root=data_root)
        / _identifier(source, "source")
        / f"{logical_date.year:04d}"
        / f"{logical_date.month:02d}"
    )


def l0_path(
    source: str, logical_date: date, filename: str, *, data_root: Path | None = None
) -> Path:
    """Path of one raw payload in L0.

    `filename` keeps the source's own name (`BhavCopy_NSE_CM_..._F_0000.csv.zip`) exactly as
    fetched, case included: L0 is what the source served, and renaming it loses the evidence that
    it was. Only path separators and traversal are rejected.
    """
    return l0_dir(source, logical_date, data_root=data_root) / _filename(filename)


def l0_meta_path(payload: Path) -> Path:
    """Sidecar path for a payload's fetch metadata (`<payload>.meta.json`).

    A sidecar rather than a parallel tree so the bytes and the record of where they came from can
    never be separated by a move or a partial copy.
    """
    return payload.with_name(payload.name + ".meta.json")


def partition_dir(
    layer: Layer, dataset: str, trading_date: date, *, data_root: Path | None = None
) -> Path:
    """Partition directory for a dataset/date in L1 or L2, e.g. `L1/prices_raw/date=2026-08-07`."""
    if layer is Layer.L0:
        raise PathLayoutError("L0 is partitioned by source and month, not by dataset — use l0_dir")
    return (
        layer_root(layer, data_root=data_root)
        / _identifier(dataset, "dataset")
        / f"date={trading_date.isoformat()}"
    )


def partition_path(
    layer: Layer,
    dataset: str,
    trading_date: date,
    *,
    filename: str = DEFAULT_PART_FILENAME,
    data_root: Path | None = None,
) -> Path:
    """The parquet file inside a dataset/date partition."""
    return partition_dir(layer, dataset, trading_date, data_root=data_root) / _filename(filename)


def l1_partition_dir(dataset: str, trading_date: date, *, data_root: Path | None = None) -> Path:
    """L1 partition directory for a dataset/date."""
    return partition_dir(Layer.L1, dataset, trading_date, data_root=data_root)


def l1_partition_path(
    dataset: str,
    trading_date: date,
    *,
    filename: str = DEFAULT_PART_FILENAME,
    data_root: Path | None = None,
) -> Path:
    """L1 parquet file for a dataset/date."""
    return partition_path(Layer.L1, dataset, trading_date, filename=filename, data_root=data_root)


def l2_partition_dir(dataset: str, trading_date: date, *, data_root: Path | None = None) -> Path:
    """L2 partition directory — the same shape as L1, one layer over (§4.2 "L2 mirror")."""
    return partition_dir(Layer.L2, dataset, trading_date, data_root=data_root)


def l2_partition_path(
    dataset: str,
    trading_date: date,
    *,
    filename: str = DEFAULT_PART_FILENAME,
    data_root: Path | None = None,
) -> Path:
    """L2 parquet file for a dataset/date."""
    return partition_path(Layer.L2, dataset, trading_date, filename=filename, data_root=data_root)


def partition_date_of(path: Path) -> date:
    """Read the trading date back out of an L1/L2 path — the inverse of `partition_dir`.

    Scans the path's parts so it works on either the partition directory or a file inside it.
    Raises rather than guessing: a path with no `date=` component is not a partition, and a
    caller that treats it as one would silently mis-date a whole file.
    """
    for part in reversed(path.parts):
        match = _PARTITION.match(part)
        if match:
            return date.fromisoformat(match.group(1))
    raise PathLayoutError(f"no date=YYYY-MM-DD partition component in {str(path)!r}")


def _identifier(value: str, kind: str) -> str:
    """Validate a source or dataset name before it becomes a directory."""
    if not _IDENTIFIER.match(value):
        raise PathLayoutError(
            f"{kind} {value!r} is not a valid lake identifier: lower-case letters, digits, "
            "'.', '_' and '-' only, starting with a letter or digit"
        )
    return value


def _filename(value: str) -> str:
    """Validate that a filename is exactly one path component and cannot escape its directory."""
    if not value or value in {".", ".."} or value != Path(value).name or "/" in value:
        raise PathLayoutError(f"{value!r} is not a single filename component")
    return value
