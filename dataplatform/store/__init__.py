"""D4: canonical store - L0/L1/L2 layout and the Postgres masters.

The lake layout (§4.2) and the immutable raw store built on it are this package's public surface:
other packages build lake paths through these names and never format a directory string of their
own, and they reach L0 through `L0Store` rather than opening files under `data/L0` themselves.

The Postgres half is here too: `connect`/`connection` are how every module opens a database
connection. The schema lives in `migrations/NNNN_name.sql` and is applied by
`dataplatform.store.migrate`, which `make migrate` runs as `python -m`; that module is
deliberately *not* re-exported here, because importing it from the package would leave it in
`sys.modules` before `-m` executes it and runpy would then run a second copy.
"""

from dataplatform.store.db import Connection, connect, connection, with_dbname
from dataplatform.store.l0 import (
    DEFAULT_CONTENT_TYPE,
    L0ChecksumError,
    L0Defect,
    L0DefectKind,
    L0Error,
    L0ImmutabilityError,
    L0MetadataError,
    L0NotFoundError,
    L0Ref,
    L0Store,
    L0VerificationReport,
)
from dataplatform.store.paths import (
    DEFAULT_PART_FILENAME,
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
    partition_dir,
    partition_path,
)

__all__ = [
    "DEFAULT_CONTENT_TYPE",
    "DEFAULT_PART_FILENAME",
    "Connection",
    "L0ChecksumError",
    "L0Defect",
    "L0DefectKind",
    "L0Error",
    "L0ImmutabilityError",
    "L0MetadataError",
    "L0NotFoundError",
    "L0Ref",
    "L0Store",
    "L0VerificationReport",
    "Layer",
    "PathLayoutError",
    "connect",
    "connection",
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
    "with_dbname",
]
