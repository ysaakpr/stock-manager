"""Production backups (M15.4): the nightly Postgres dump, the L0 manifest, and the restore drill.

`ops/backup.sh` and `ops/restore.sh` (M0.7) are the operator's by-hand pair; this module is what
the scheduler runs every night, and what the restore drill runs against a dump it wrote. Three
pieces, each a scheduled job or a command:

* **`run_postgres_backup`** — `pg_dump -Fc` of the live database into
  `BACKUP_ROOT/postgres/trading-<IST stamp>.dump`, a JSON sidecar beside it (sha256, size, the
  applied migrations and the key tables' row counts taken just before the dump), then retention:
  the newest dump of each of the last `BACKUP_KEEP_DAILY` days that have one, plus the newest of
  each of the last `BACKUP_KEEP_WEEKLY` ISO weeks. Everything else of ours is pruned.
* **`run_l0_backup`** — an incremental sha256 manifest of the lake (L0 is write-once, so only files
  the manifest has not seen are hashed; a recorded file gone missing raises), then, only when
  `BACKUP_L0_MIRROR` names a target, `rsync --ignore-existing` of L0 onto it. Never `--delete`.
* **`run_restore_drill`** — restores the newest dump into a throwaway database (by default a
  scratch `postgres:16` container on a random loopback port, torn down afterwards), checks the
  migration ledger against this checkout and the key tables' counts against the sidecar, and
  reports. It refuses a target that is the live database.

Credentials: the live DSN is `Settings.database_url`, the drill's optional target is the
`SecretStr` `RESTORE_DRILL_DATABASE_URL`. Neither ever reaches an argv, a log line or a sidecar —
the Postgres client tools read the password from `PGPASSWORD` in their environment, and
`docker run -e PGPASSWORD` passes the *name*, so the value never appears in `ps` either. What is
logged is `host:port/dbname`.

The client tools run in a `postgres:16` container (`BACKUP_PG_CLIENT_IMAGE`) on the host network:
this host has no libpq client, and a client older than the 16.x server refuses to dump. Set the
image to empty to use `pg_dump`/`pg_restore` from `PATH` instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.logging import configure_logging, get_logger
from dataplatform.store.migrate import MigrationError, discover

__all__ = [
    "KEY_TABLES",
    "BackupRecord",
    "DrillReport",
    "DsnParts",
    "Identity",
    "L0BackupResult",
    "LiveTargetError",
    "PgClient",
    "RetentionPlan",
    "identify_database",
    "list_backups",
    "load_sidecar",
    "parse_dsn",
    "refuse_live_target",
    "run_l0_backup",
    "run_postgres_backup",
    "run_restore_drill",
    "same_database",
    "select_retention",
]

#: The tables whose counts a backup records and a drill checks: the ingestion ledger, the job
#: history, the paper book and the append-only journal — what a real-money restart needs back.
#: Every one only grows (the journal and paper tables reject DELETE; sync_state and job_run are
#: upserted and appended), which is what lets the drill demand restored >= recorded.
KEY_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "sync_state",
    "job_run",
    "decision_journal",
    "paper_session",
    "paper_session_resolution",
    "security_master",
)

#: `trading-20261008T053000.dump`: the IST stamp sorts lexicographically, so newest is greatest.
_DUMP_NAME = re.compile(r"^trading-(\d{8}T\d{6})\.dump$")
_STAMP_FORMAT = "%Y%m%dT%H%M%S"

#: Hosts that all mean "this machine" for the cheap first-line check. Not the defence — aliases
#: are endless; `refuse_live_target` compares server identity as well.
_LOOPBACK = frozenset(
    {"", "localhost", "localhost.localdomain", "0.0.0.0", "::", "::1", "::ffff:127.0.0.1"}
)

#: Free space a dump must leave behind, beyond twice the newest dump's size. A full root disk
#: takes Postgres (and the lake writer) down with it; a skipped backup is the cheaper failure.
_DISK_HEADROOM_BYTES = 1 << 30

#: How long an L0 file must sit unmodified before the manifest records it. L0 writes a payload
#: and its `.meta.json` in well under a second; ten minutes is a wide margin over a slow disk.
_SETTLE = timedelta(minutes=10)

#: How old a `*.partial` must be before a backup run deletes it as a crashed run's leftover.
_STALE_PARTIAL = timedelta(days=1)

#: The scratch container's name prefix — `docker ps` shows a drill left running by a crash.
_DRILL_CONTAINER_PREFIX = "trading-restore-drill"

log = get_logger(__name__)


class BackupError(RuntimeError):
    """A backup or a drill step failed; the message is safe to log (no credential in it)."""


class LiveTargetError(BackupError):
    """The drill was pointed at the live database. It only ever restores into a throwaway one."""


# ── DSNs ──────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DsnParts:
    """A Postgres DSN split into what the client tools take, with the password kept secret.

    `redacted` is the only form that may be logged or written to a sidecar.
    """

    host: str
    port: int
    user: str
    dbname: str
    password: SecretStr = field(repr=False)

    @property
    def redacted(self) -> str:
        return f"{self.host or 'local'}:{self.port}/{self.dbname}"

    def env(self) -> dict[str, str]:
        """libpq environment for this target — the password goes here and nowhere else."""
        return {
            "PGHOST": self.host,
            "PGPORT": str(self.port),
            "PGUSER": self.user,
            "PGDATABASE": self.dbname,
            "PGPASSWORD": self.password.get_secret_value(),
        }

    def conninfo(self) -> str:
        """A keyword DSN for psycopg, for an in-process connection. Never logged."""
        return make_conninfo(
            host=self.host,
            port=self.port,
            user=self.user,
            dbname=self.dbname,
            password=self.password.get_secret_value(),
        )


def parse_dsn(dsn: str | SecretStr) -> DsnParts:
    """Split a URL or keyword DSN. Raises `BackupError` without echoing the DSN back."""
    raw = dsn.get_secret_value() if isinstance(dsn, SecretStr) else dsn
    try:
        parts = conninfo_to_dict(raw)
    except psycopg.ProgrammingError:
        # `from None`: psycopg's own message can quote the DSN, password included.
        raise BackupError(
            "unparseable Postgres DSN (value withheld: it may hold a password)"
        ) from None
    return DsnParts(
        host=str(parts.get("host") or "localhost"),
        port=int(parts.get("port") or 5432),
        user=str(parts.get("user") or ""),
        dbname=str(parts.get("dbname") or parts.get("user") or ""),
        password=SecretStr(str(parts.get("password") or "")),
    )


def same_database(a: DsnParts, b: DsnParts) -> bool:
    """Whether two DSNs reach the same database on the same server.

    Loopback spellings are one host, and the port and database name must both match: a scratch
    database on the live server is a different database, and a drill into it is allowed. Errs
    towards "same" — `localhost` and the live server's own name are not resolved against each
    other, but neither can make a live target look different.
    """

    this_host = {socket.gethostname().lower(), socket.getfqdn().lower()}

    def host(value: str) -> str:
        lowered = value.lower().strip("[]")
        local = (
            lowered in _LOOPBACK
            or lowered in this_host
            or lowered.startswith(("/", "127.", "::ffff:127."))
        )
        return "loopback" if local else lowered

    return host(a.host) == host(b.host) and a.port == b.port and a.dbname == b.dbname


def _redact(text: str, *secrets_: str) -> str:
    """`text` with every non-empty secret replaced, for a client tool's stderr in an error."""
    for value in secrets_:
        if value:
            text = text.replace(value, "***")
    return text


# ── the client tools ──────────────────────────────────────────────────────────────────────────

#: Runs one argv with an environment, streaming stdin/stdout, and returns (exit code, stderr).
Runner = Callable[..., subprocess.CompletedProcess[bytes]]


@dataclass(frozen=True, slots=True)
class PgClient:
    """How to invoke `pg_dump`/`pg_restore`: in a client container, or from `PATH`.

    What it never does: put a credential in the argv. The libpq variables are passed to docker by
    name (`-e PGPASSWORD`), so the value travels in the environment only.
    """

    image: str = "postgres:16"

    def argv(self, tool: str, *args: str) -> list[str]:
        if not self.image:
            return [tool, *args]
        passthrough: list[str] = []
        for name in ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD"):
            passthrough += ["-e", name]
        return [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "host",
            *passthrough,
            self.image,
            tool,
            *args,
        ]


def _run(
    argv: Sequence[str],
    target: DsnParts,
    *,
    stdin: Any = None,
    stdout: Any = None,
    runner: Runner = subprocess.run,
) -> None:
    """Run a client tool against `target`; raise `BackupError` with redacted stderr on failure."""
    env = {**os.environ, **target.env()}
    result = runner(
        list(argv), stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, env=env, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()[-2000:]
        detail = _redact(detail, target.password.get_secret_value())
        raise BackupError(f"{argv[-1] if argv else '?'} exited {result.returncode}: {detail}")


# ── retention ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """Which stamped backups to keep and which to delete, newest first in each."""

    keep: tuple[datetime, ...]
    prune: tuple[datetime, ...]


def select_retention(
    stamps: Iterable[datetime], *, keep_daily: int, keep_weekly: int
) -> RetentionPlan:
    """Grandfather-father retention over backup instants.

    Keeps the newest backup of each of the `keep_daily` most recent calendar days that have one,
    and the newest of each of the `keep_weekly` most recent ISO weeks that have one; prunes the
    rest. Counted over days *with a backup*, not over the calendar, so a fortnight of failed runs
    does not age every good backup out at once. The newest backup is always kept.
    """
    if keep_daily < 1 or keep_weekly < 0:
        raise ValueError("retention needs keep_daily >= 1 and keep_weekly >= 0")
    ordered = sorted(set(stamps), reverse=True)
    keep: set[datetime] = set()
    days: set[date] = set()
    weeks: set[tuple[int, int]] = set()
    for stamp in ordered:
        day = stamp.date()
        if day not in days and len(days) < keep_daily:
            days.add(day)
            keep.add(stamp)
        week = stamp.isocalendar()[:2]
        if week not in weeks and len(weeks) < keep_weekly:
            weeks.add((week[0], week[1]))
            keep.add(stamp)
    return RetentionPlan(
        keep=tuple(stamp for stamp in ordered if stamp in keep),
        prune=tuple(stamp for stamp in ordered if stamp not in keep),
    )


# ── the Postgres dump ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BackupRecord:
    """One dump and what its sidecar says about it."""

    path: Path
    stamp: datetime
    bytes: int
    sha256: str
    source: str
    server_version: str
    migrations: tuple[str, ...]
    counts: Mapping[str, int]
    seconds: float

    def sidecar(self) -> dict[str, Any]:
        document = asdict(self)
        document["path"] = self.path.name
        document["stamp"] = self.stamp.isoformat()
        document["migrations"] = list(self.migrations)
        document["counts"] = dict(self.counts)
        return document


def postgres_dir(settings: Settings) -> Path:
    return settings.backup_root / "postgres"


def load_sidecar(dump: Path) -> dict[str, Any]:
    """The dump's sidecar, validated. Raises `BackupError` for a missing or unusable one.

    A sidecar is written (temp + rename) *before* its dump is renamed into place, so a dump with no
    valid sidecar is not one this module finished — it is never restored from, and never counted
    or pruned by retention.
    """
    path = dump.with_suffix(".json")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BackupError(f"{dump.name} has no sidecar {path.name}") from None
    except (OSError, ValueError) as error:
        raise BackupError(f"{path.name} is not a readable sidecar: {error}") from None
    if not isinstance(document, dict) or not isinstance(document.get("sha256"), str):
        raise BackupError(f"{path.name} has no sha256: not a sidecar this module wrote")
    return document


def list_backups(directory: Path, tzinfo: Any) -> dict[datetime, Path]:
    """Every finished dump under `directory` — named like ours *and* with a valid sidecar.

    A dump without one is logged and left alone: not restorable, not retention's to count or prune.
    """
    found: dict[datetime, Path] = {}
    if not directory.is_dir():
        return found
    for path in directory.iterdir():
        match = _DUMP_NAME.match(path.name)
        if not match:
            continue
        try:
            load_sidecar(path)
        except BackupError as error:
            log.warning("backup.sidecar_invalid", path=str(path), error=str(error))
            continue
        stamp = datetime.strptime(match.group(1), _STAMP_FORMAT).replace(tzinfo=tzinfo)
        found[stamp] = path
    return found


def _clean_stale_partials(directory: Path, *, now: datetime) -> int:
    """Delete `trading-*.partial` files older than `_STALE_PARTIAL`; return the bytes freed.

    A partial is what a run killed mid-dump leaves behind. The job's lock means none is being
    written by another run, but a younger one is left alone in case a by-hand run is in flight.
    """
    freed = 0
    cutoff = (now - _STALE_PARTIAL).timestamp()
    for path in directory.glob("trading-*.partial"):
        stat = path.stat()
        if stat.st_mtime < cutoff:
            path.unlink()
            freed += stat.st_size
            log.info("backup.partial_removed", path=str(path), bytes=stat.st_size)
    return freed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _counts(conn: psycopg.Connection[Any], tables: Sequence[str]) -> dict[str, int]:
    """Exact counts of the tables that exist; a table this database lacks is left out."""
    counts: dict[str, int] = {}
    for table in tables:
        exists = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
        if exists is None or exists[0] is None:
            continue
        row = conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone()
        counts[table] = int(row[0]) if row else 0
    return counts


def _applied(conn: psycopg.Connection[Any]) -> tuple[str, ...]:
    ledger = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()
    if ledger is None or ledger[0] is None:
        return ()
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return tuple(str(row[0]) for row in rows)


def _source_state(live: DsnParts) -> tuple[dict[str, int], tuple[str, ...], str]:
    """The key tables' counts, the migration ledger and the server version, read just before the
    dump. Its own function so an offline test can stand in for the database."""
    with psycopg.connect(live.conninfo(), autocommit=True) as conn:
        counts = _counts(conn, KEY_TABLES)
        migrations = _applied(conn)
        version_row = conn.execute("SHOW server_version").fetchone()
    return counts, migrations, str(version_row[0]) if version_row else ""


def run_postgres_backup(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    client: PgClient | None = None,
    runner: Runner = subprocess.run,
) -> BackupRecord:
    """Dump the live database, write its sidecar, then apply retention. Returns the new record.

    What it does: refuses when the disk would be left with less than twice the newest dump plus
    1 GiB free; records the key tables' counts and the migration ledger; streams `pg_dump -Fc
    --no-owner --no-privileges` into `<name>.partial`, then renames it into place only once it is
    complete and hashed; prunes per `select_retention`.
    What it assumes: the injected clock is the run's (B10); the database is reachable.
    What it never does: overwrite a dump, prune anything not named like one of ours, or put the
    DSN anywhere but the client's environment. Any failure raises, so the scheduler records the
    run FAILED and `failure_alerts` pages it like any other job.
    """
    settings = get_settings() if settings is None else settings
    clock = SystemClock(settings.tzinfo) if clock is None else clock
    client = PgClient(settings.backup_pg_client_image) if client is None else client
    live = parse_dsn(settings.database_url)
    directory = postgres_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)

    now = clock.now()
    # Stale partials go first, so the space they held counts as free in the guard below.
    freed = _clean_stale_partials(directory, now=now)
    existing = list_backups(directory, settings.tzinfo)
    newest_bytes = existing[max(existing)].stat().st_size if existing else 0
    free = shutil.disk_usage(directory).free
    needed = 2 * newest_bytes + _DISK_HEADROOM_BYTES
    if free < needed:
        raise BackupError(
            f"refusing to dump: {free} bytes free under {directory} ({freed} freed from stale "
            f"partials), need {needed} (twice the newest dump plus 1 GiB headroom)"
        )

    stamp = now.astimezone(settings.tzinfo).replace(microsecond=0)
    final = directory / f"trading-{stamp.strftime(_STAMP_FORMAT)}.dump"
    if final.exists():
        raise BackupError(f"{final.name} already exists; a backup is never overwritten")
    partial = final.with_name(final.name + ".partial")
    log.info("backup.start", target=live.redacted, path=str(final))

    counts, migrations, server_version = _source_state(live)

    started = time.monotonic()
    try:
        with partial.open("wb") as handle:
            _run(
                client.argv("pg_dump", "-Fc", "--no-owner", "--no-privileges"),
                live,
                stdout=handle,
                runner=runner,
            )
            handle.flush()
            os.fsync(handle.fileno())
        size = partial.stat().st_size
        if size == 0:
            raise BackupError("pg_dump produced an empty archive")
        digest = _sha256(partial)
        record = BackupRecord(
            path=final,
            stamp=stamp,
            bytes=size,
            sha256=digest,
            source=live.redacted,
            server_version=server_version,
            migrations=migrations,
            counts=counts,
            seconds=round(time.monotonic() - started, 1),
        )
        # The sidecar lands first, atomically, and the dump is renamed into place last: a crash
        # anywhere leaves either a partial (cleaned later) or a sidecar with no dump (ignored),
        # never a finished-looking dump whose sidecar is missing or torn.
        _write_atomically(
            final.with_suffix(".json"), json.dumps(record.sidecar(), indent=2, sort_keys=True)
        )
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.rename(final)
    log.info(
        "backup.done",
        target=live.redacted,
        path=str(final),
        bytes=size,
        seconds=record.seconds,
        counts=counts,
    )
    _apply_retention(settings, directory)
    return record


def _write_atomically(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _apply_retention(settings: Settings, directory: Path) -> RetentionPlan:
    backups = list_backups(directory, settings.tzinfo)
    plan = select_retention(
        backups, keep_daily=settings.backup_keep_daily, keep_weekly=settings.backup_keep_weekly
    )
    for stamp in plan.prune:
        path = backups[stamp]
        path.unlink()
        path.with_suffix(".json").unlink(missing_ok=True)
        log.info("backup.pruned", path=str(path))
    log.info("backup.retention", kept=len(plan.keep), pruned=len(plan.prune))
    return plan


# ── L0 ────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class L0BackupResult:
    """What one L0 pass did."""

    manifest: Path
    files: int
    hashed: int
    mirrored_to: str | None
    #: New files too young to record this run (`_SETTLE`); the next run picks them up.
    deferred: int = 0


def _read_manifest(path: Path) -> dict[str, str]:
    """`sha256sum`-format lines → {relative path: digest}."""
    entries: dict[str, str] = {}
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if digest and name:
            entries[name] = digest
    return entries


def _lake_files(data_root: Path) -> Iterator[str]:
    lake = data_root / "L0"
    for path in sorted(lake.rglob("*")):
        if path.is_file():
            yield path.relative_to(data_root).as_posix()


def _sidecar_sha256(payload: Path) -> str | None:
    """The sha256 L0's own `<payload>.meta.json` records, or `None` when there is no sidecar."""
    meta = payload.with_name(payload.name + ".meta.json")
    if payload.name.endswith(".meta.json") or not meta.exists():
        return None
    try:
        recorded = json.loads(meta.read_text(encoding="utf-8")).get("sha256")
    except (OSError, ValueError, AttributeError) as error:
        raise BackupError(f"unreadable L0 sidecar {meta}: {error}") from None
    return recorded if isinstance(recorded, str) else None


def _device(path: Path) -> int:
    try:
        return path.stat().st_dev
    except FileNotFoundError:
        raise BackupError(f"L0 mirror target {path} does not exist (not mounted?)") from None


def _is_remote(target: str) -> bool:
    """rsync's remote forms: `host:path`, `user@host:path`, `rsync://…`."""
    head = target.split("/", 1)[0]
    return target.startswith("rsync://") or ":" in head


def run_l0_backup(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    runner: Runner = subprocess.run,
) -> L0BackupResult:
    """Extend the L0 manifest with every new file, then mirror L0 if a target is configured.

    What it does: keeps `BACKUP_ROOT/l0/MANIFEST.sha256`, checkable with `sha256sum -c` from
    `DATA_ROOT`. L0 is write-once (invariant #1), so a file already in the manifest is not
    re-hashed — the weekly `l0_verify` sweep owns re-checking bytes — and only new files cost a
    read. A recorded file that is no longer on disk raises: that is an incident, not drift. Then,
    when `BACKUP_L0_MIRROR` is set, `rsync -a --ignore-existing DATA_ROOT/L0/ <mirror>/L0/`.
    What it assumes: `rsync` is on PATH when a mirror is configured.
    What it never does: record a file modified in the last `_SETTLE` (10 minutes) — it may still be
    being written; the next run takes it — or a payload whose bytes disagree with the sha256 its
    own L0 `.meta.json` recorded (that raises). Never deletes or rewrites anything in L0 or on the
    mirror (`--ignore-existing`, never `--delete`); never copies to a local mirror path on the
    lake's own filesystem (an unmounted mount point would silently fill the root disk); never
    invents a mirror target — that stays an owner decision, and the job says so in its log.
    """
    settings = get_settings() if settings is None else settings
    clock = SystemClock(settings.tzinfo) if clock is None else clock
    data_root = settings.data_root
    directory = settings.backup_root / "l0"
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "MANIFEST.sha256"
    if not (data_root / "L0").is_dir():
        raise BackupError(f"no lake at {data_root / 'L0'}: is DATA_ROOT the real lake?")
    recorded = _read_manifest(manifest)
    present = set(_lake_files(data_root))

    missing = sorted(set(recorded) - present)
    if missing:
        raise BackupError(
            f"{len(missing)} L0 file(s) in the manifest are gone from {data_root} (first: "
            f"{missing[0]}); L0 is immutable and repairing it is the owner's "
            "(AGENTIC_CONTEXT §3.10)"
        )

    settled_before = (clock.now() - _SETTLE).timestamp()
    unseen = sorted(present - set(recorded))
    new = [name for name in unseen if (data_root / name).stat().st_mtime < settled_before]
    deferred = len(unseen) - len(new)
    if new:
        partial = manifest.with_name(manifest.name + ".partial")
        if manifest.exists():
            shutil.copyfile(manifest, partial)
        try:
            with partial.open("a", encoding="utf-8") as handle:
                for name in new:
                    digest = _sha256(data_root / name)
                    expected = _sidecar_sha256(data_root / name)
                    if expected is not None and expected != digest:
                        raise BackupError(
                            f"{name} hashes to {digest[:12]}… but its L0 sidecar records "
                            f"{expected[:12]}…: not recorded; L0 integrity is the owner's call "
                            "(AGENTIC_CONTEXT §3.10)"
                        )
                    handle.write(f"{digest}  {name}\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        partial.replace(manifest)
    log.info(
        "backup.l0_manifest",
        files=len(present),
        hashed=len(new),
        deferred=deferred,
        manifest=str(manifest),
    )

    mirror = settings.backup_l0_mirror
    if not mirror:
        log.warning(
            "backup.l0_mirror_unconfigured",
            reason="BACKUP_L0_MIRROR is unset: L0 has a manifest but no second copy",
        )
        return L0BackupResult(manifest, len(present), len(new), None, deferred)

    if not _is_remote(mirror) and _device(Path(mirror)) == _device(data_root / "L0"):
        raise BackupError(
            f"L0 mirror target {mirror} is on the lake's own filesystem — an unmounted mount "
            "point? Refusing to copy the lake onto the disk it is meant to survive."
        )

    argv = ["rsync", "-a", "--ignore-existing", f"{data_root / 'L0'}/", f"{mirror.rstrip('/')}/L0/"]
    result = runner(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()[-2000:]
        raise BackupError(f"rsync of L0 to the mirror exited {result.returncode}: {detail}")
    log.info("backup.l0_mirrored", files=len(present))
    return L0BackupResult(manifest, len(present), len(new), mirror, deferred)


# ── the restore drill ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DrillReport:
    """What a drill found. `ok` is the verdict; `problems` says why it is not."""

    dump: Path
    dump_bytes: int
    target: str
    restore_seconds: float
    total_seconds: float
    migrations_on_disk: tuple[str, ...]
    migrations_restored: tuple[str, ...]
    recorded: Mapping[str, int]
    restored: Mapping[str, int]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    def render(self) -> str:
        lines = [
            f"dump      {self.dump.name} ({self.dump_bytes} bytes)",
            f"target    {self.target}",
            f"restore   {self.restore_seconds:.1f}s (drill total {self.total_seconds:.1f}s)",
            f"migrations restored {len(self.migrations_restored)}, on disk "
            f"{len(self.migrations_on_disk)}"
            + (
                ""
                if self.migrations_restored == self.migrations_on_disk
                else f" — pending vs this checkout: "
                f"{sorted(set(self.migrations_on_disk) - set(self.migrations_restored))}"
            ),
            "table                       recorded   restored",
        ]
        for table in sorted(set(self.recorded) | set(self.restored)):
            lines.append(
                f"  {table:<26}{self.recorded.get(table, '-')!s:>9}"
                f"{self.restored.get(table, '-')!s:>11}"
            )
        lines += [f"PROBLEM   {problem}" for problem in self.problems]
        lines.append("ok        restore drill passed" if self.ok else "FAILED    restore drill")
        return "\n".join(lines)


def check_restored(
    *,
    recorded: Mapping[str, int],
    restored: Mapping[str, int],
    recorded_migrations: Sequence[str],
    restored_migrations: Sequence[str],
) -> tuple[str, ...]:
    """The drill's verdict, as a list of problems (empty is a pass).

    Counts are taken just *before* the dump's snapshot, and every key table only grows, so a
    restored count below the recorded one means rows were lost; above it is writes that landed
    in between. A key table recorded but absent after restore, an empty restore of a table that
    had rows, and a migration ledger that differs from the one recorded are all failures.
    """
    problems: list[str] = []
    if tuple(restored_migrations) != tuple(recorded_migrations):
        problems.append(
            f"migration ledger differs: recorded {list(recorded_migrations)}, "
            f"restored {list(restored_migrations)}"
        )
    for table, count in sorted(recorded.items()):
        if table not in restored:
            problems.append(f"{table}: recorded {count} rows, missing after restore")
        elif restored[table] < count:
            problems.append(f"{table}: recorded {count} rows, restored only {restored[table]}")
    return tuple(problems)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_ready(target: DsnParts, *, timeout_s: float = 90.0) -> None:
    """Until a TCP connection works. The image's init server listens on a socket only, so TCP
    succeeding is the signal that initdb finished and the real server is up."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with psycopg.connect(target.conninfo(), connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise BackupError(f"scratch Postgres at {target.redacted} never came up") from None
            time.sleep(1)


@contextmanager
def ephemeral_postgres(image: str, *, keep: bool = False) -> Iterator[DsnParts]:
    """A throwaway Postgres container on a random loopback port, removed on exit.

    The password is random per drill and reaches docker as `-e POSTGRES_PASSWORD` by name. It is
    published on 127.0.0.1 only, and nothing about the live stack is touched.
    """
    password = secrets.token_urlsafe(24)
    port = _free_port()
    name = f"{_DRILL_CONTAINER_PREFIX}-{secrets.token_hex(4)}"
    env = {**os.environ, "POSTGRES_PASSWORD": password}
    argv = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "POSTGRES_PASSWORD",
        "-e",
        "POSTGRES_DB=drill",
        "-p",
        f"127.0.0.1:{port}:5432",
        image,
    ]
    started = subprocess.run(argv, env=env, capture_output=True, check=False)
    if started.returncode != 0:
        detail = _redact(started.stderr.decode("utf-8", "replace"), password)
        # `docker run -d` can fail after creating the container (a port clash, a start error);
        # remove it by name either way — `rm -f` of a container that never existed succeeds.
        try:
            _remove_container(name)
        finally:
            raise BackupError(f"could not start the scratch container: {detail.strip()}")
    log.info("drill.container_started", container=name, port=port)
    try:
        target = DsnParts(
            host="127.0.0.1",
            port=port,
            user="postgres",
            dbname="drill",
            password=SecretStr(password),
        )
        _wait_ready(target)
        yield target
    finally:
        if keep:
            log.info("drill.container_kept", container=name, port=port)
        else:
            _remove_container(name)


def _remove_container(name: str) -> None:
    """`docker rm -f` the scratch container, failing loud: a drill container left running holds
    a port and a restored copy of the live data, and must not be reported as removed."""
    removed = subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    if removed.returncode != 0:
        detail = removed.stderr.decode("utf-8", "replace").strip()
        log.error("drill.container_remove_failed", container=name, error=detail)
        raise BackupError(
            f"could not remove scratch container {name} (exit {removed.returncode}): {detail}; "
            f"remove it by hand: docker rm -f {name}"
        )
    log.info("drill.container_removed", container=name)


def run_restore_drill(
    settings: Settings | None = None,
    *,
    dump: Path | None = None,
    target: DsnParts | None = None,
    client: PgClient | None = None,
    keep: bool = False,
    runner: Runner = subprocess.run,
    identify: Identifier | None = None,
) -> DrillReport:
    """Restore a dump into a throwaway database and check it. Never the live database.

    What it does: picks `dump` or the newest one under `BACKUP_ROOT/postgres`, verifies its sha256
    against the sidecar, then restores it with `pg_restore --exit-on-error` into `target` — or,
    when none is given, into `RESTORE_DRILL_DATABASE_URL`, or else into a scratch container made
    for this drill — and compares the migration ledger and key-table counts with the sidecar.
    What it assumes: the target database exists and is empty.
    What it never does: restore into the live database — `LiveTargetError` before any byte is
    written when the target *is* the live database by `refuse_live_target`'s identity check, or
    cannot be told apart from it.
    """
    settings = get_settings() if settings is None else settings
    client = PgClient(settings.backup_pg_client_image) if client is None else client
    started = time.monotonic()

    if dump is None:
        backups = list_backups(postgres_dir(settings), settings.tzinfo)
        if not backups:
            raise BackupError(f"no dumps under {postgres_dir(settings)}; run a backup first")
        dump = backups[max(backups)]
    sidecar = load_sidecar(dump)
    if _sha256(dump) != sidecar["sha256"]:
        raise BackupError(f"{dump.name} does not match its recorded sha256: corrupt, not restoring")

    if target is None and settings.restore_drill_database_url is not None:
        target = parse_dsn(settings.restore_drill_database_url)
    live = parse_dsn(settings.database_url)
    identify = identify_database if identify is None else identify
    if target is not None:
        refuse_live_target(target, live, identify=identify)
        return _drill_into(dump, sidecar, target, client, runner, started)

    image = client.image or "postgres:16"
    with ephemeral_postgres(image, keep=keep) as scratch:
        refuse_live_target(scratch, live, identify=identify)
        return _drill_into(dump, sidecar, scratch, client, runner, started)


#: (cluster system identifier, database name): what makes two connections the same database,
#: however their DSNs spell the host.
Identity = tuple[str, str]
Identifier = Callable[[DsnParts], Identity]


def identify_database(parts: DsnParts) -> Identity:
    """Ask the server which cluster and database a DSN actually reaches.

    `pg_control_system().system_identifier` is fixed at initdb and differs between clusters, so it
    tells the live server from a scratch container whatever the host is called; with
    `current_database()` it tells two databases on one cluster apart. Needs a superuser or
    `pg_monitor`; a role without it cannot be identified, and is refused.
    """
    with psycopg.connect(parts.conninfo(), connect_timeout=10, autocommit=True) as conn:
        row = conn.execute(
            "SELECT system_identifier::text, current_database() FROM pg_control_system()"
        ).fetchone()
    if row is None:
        raise BackupError(f"{parts.redacted} returned no system identifier")
    return str(row[0]), str(row[1])


def refuse_live_target(
    target: DsnParts, live: DsnParts, *, identify: Identifier = identify_database
) -> None:
    """Raise `LiveTargetError` unless `target` is provably not the live database.

    First the cheap check — the DSNs spell the same host, port and database. Then the one that
    cannot be fooled by an alias (`127.0.0.2`, `0.0.0.0`, the host's own name, a mapped IPv6
    address): connect to both and compare cluster identifier and database name. Either side
    that cannot be reached or identified is a refusal too — "could not tell" is never "different".
    """
    refusal = (
        f"refusing to restore into {target.redacted}: that is the live database. "
        "The drill only restores into a throwaway one."
    )
    if same_database(target, live):
        raise LiveTargetError(refusal)
    try:
        theirs, ours = identify(target), identify(live)
    except Exception as error:
        detail = _redact(
            str(error),
            target.password.get_secret_value(),
            live.password.get_secret_value(),
        )
        raise LiveTargetError(
            f"refusing to restore into {target.redacted}: could not prove it is not the live "
            f"database ({type(error).__name__}: {detail.strip()})"
        ) from None
    if theirs == ours:
        raise LiveTargetError(refusal)
    log.info("drill.target_identified", target=target.redacted, distinct_from_live=True)


def _drill_into(
    dump: Path,
    sidecar: Mapping[str, Any],
    target: DsnParts,
    client: PgClient,
    runner: Runner,
    started: float,
) -> DrillReport:
    log.info("drill.restore_start", dump=dump.name, target=target.redacted)
    restore_started = time.monotonic()
    with dump.open("rb") as handle:
        _run(
            client.argv(
                "pg_restore",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "-d",
                target.dbname,
            ),
            target,
            stdin=handle,
            runner=runner,
        )
    restore_seconds = time.monotonic() - restore_started

    with psycopg.connect(target.conninfo(), autocommit=True) as conn:
        restored = _counts(conn, KEY_TABLES)
        restored_migrations = _applied(conn)
    try:
        on_disk = tuple(migration.version for migration in discover())
    except MigrationError as error:
        raise BackupError(f"cannot read this checkout's migrations: {error}") from error

    problems = list(
        check_restored(
            recorded=sidecar.get("counts", {}),
            restored=restored,
            recorded_migrations=sidecar.get("migrations", []),
            restored_migrations=restored_migrations,
        )
    )
    report = DrillReport(
        dump=dump,
        dump_bytes=int(sidecar.get("bytes", dump.stat().st_size)),
        target=target.redacted,
        restore_seconds=restore_seconds,
        total_seconds=time.monotonic() - started,
        migrations_on_disk=on_disk,
        migrations_restored=restored_migrations,
        recorded=dict(sidecar.get("counts", {})),
        restored=restored,
        problems=tuple(problems),
    )
    log.info(
        "drill.done",
        ok=report.ok,
        dump=dump.name,
        target=target.redacted,
        restore_s=round(restore_seconds, 1),
        problems=list(report.problems),
    )
    return report


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m dataplatform.store.backup {postgres,l0,drill}` — the same code the jobs run."""
    parser = argparse.ArgumentParser(prog="python -m dataplatform.store.backup")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("postgres", help="dump the live database now, then apply retention")
    commands.add_parser("l0", help="extend the L0 manifest; mirror L0 if BACKUP_L0_MIRROR is set")
    drill = commands.add_parser("drill", help="restore the newest dump into a throwaway database")
    drill.add_argument("--dump", type=Path, help="this dump instead of the newest")
    drill.add_argument("--keep", action="store_true", help="leave the scratch container running")
    args = parser.parse_args(argv)
    configure_logging()

    try:
        if args.command == "postgres":
            record = run_postgres_backup()
            print(f"dumped {record.path} ({record.bytes} bytes, {record.seconds}s)")
            return 0
        if args.command == "l0":
            result = run_l0_backup()
            mirror = result.mirrored_to or "none configured (BACKUP_L0_MIRROR unset)"
            print(
                f"L0 manifest {result.manifest}: {result.files} files, {result.hashed} new, "
                f"{result.deferred} deferred (modified < 10 min ago)"
            )
            print(f"L0 mirror   {mirror}")
            return 0
        report = run_restore_drill(dump=args.dump, keep=args.keep)
        print(report.render())
        return 0 if report.ok else 1
    except BackupError as error:
        log.error("backup.failed", command=args.command, error=str(error))
        print(f"backup: {error}", file=sys.stderr)
        return 2 if isinstance(error, LiveTargetError) else 1


if __name__ == "__main__":
    sys.exit(main())
