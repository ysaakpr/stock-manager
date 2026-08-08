-- 0002_status_surface — the two tables the §4.4 status API reads that 0001 did not create (M0.5).
--
-- 0001 created everything the plan's §4.2 sketch names, which covers four of the six endpoints:
-- /status/sync, /status/sources and /status/gaps read sync_state, /status/quality reads
-- quality_flag. The remaining two endpoints have nowhere to read from, and an endpoint with
-- nowhere to read from is an endpoint that returns invented data — the one thing the D5 task
-- forbids. So:
--
--   * scheduler_heartbeat — /health reports the age of the newest heartbeat and turns 503 when it
--     exceeds SCHEDULER_HEARTBEAT_STALE_AFTER_SECONDS. The heartbeat is written by the scheduler
--     (M0.6), which is being built alongside this; the reader is here because /health cannot exist
--     without it, and the writer's own acceptance ("a heartbeat visible via /health") is expressed
--     against this table.
--   * archive_bundle — /archives?date= returns the manifest of the daily bundle. The publisher is
--     D6/M1.12; this is the row it writes when a bundle is published, so that "which archives
--     exist" is answerable from the database rather than by trusting a directory listing.
--
-- Both tables are deliberately narrow, with a jsonb column for the owning module's own detail, so
-- that M0.6 and M1.12 can record more without a migration of their own.

-- ── D5 · scheduler liveness (§4.4 /health) ──────────────────────────────────────────────────

CREATE TABLE scheduler_heartbeat (
    scheduler_id text        PRIMARY KEY,
    beat_at      timestamptz NOT NULL,
    detail       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    updated_at   timestamptz NOT NULL
);
COMMENT ON TABLE scheduler_heartbeat IS
    'D5 · One row per scheduler instance, upserted every tick. /health reports the age of the '
    'newest beat and fails its probe once that age passes the configured threshold, which is how '
    'a scheduler that died silently becomes visible instead of simply never running a job again.';
COMMENT ON COLUMN scheduler_heartbeat.scheduler_id IS
    'Stable identity of the beating process — not its PID, which changes on every restart and '
    'would leave a dead scheduler''s last beat behind as a permanent extra row. One row per '
    'logical scheduler, upserted, so the table stays as small as the deployment.';
COMMENT ON COLUMN scheduler_heartbeat.beat_at IS
    'The instant the scheduler last completed a tick, taken from its injected Clock (B10) and not '
    'from the database''s now(): a replayed or frozen-clock run has to write the time it claims '
    'to be running at, or /health would report the wall clock back to a test that froze it.';
COMMENT ON COLUMN scheduler_heartbeat.detail IS
    'Scheduler-owned free-form context for the beat — registered job names, the last job run, the '
    'tick interval. Free-form so M0.6 and later job work can record more without a migration; '
    '/health does not read it, so nothing here is a contract.';

-- ── D6 · published daily archives (§4.5, /archives?date=) ───────────────────────────────────

CREATE TABLE archive_bundle (
    logical_date    date        PRIMARY KEY,
    schema_version  text        NOT NULL,
    bundle_path     text        NOT NULL,
    manifest_sha256 text        NOT NULL,
    file_count      integer     NOT NULL CHECK (file_count >= 0),
    total_bytes     bigint      NOT NULL CHECK (total_bytes >= 0),
    manifest        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    published_at    timestamptz NOT NULL
);
COMMENT ON TABLE archive_bundle IS
    'D6 · One row per published daily archive bundle (§4.5): the manifest, its checksum, and where '
    'the bundle lives. Written by the archive publisher (M1.12) and read by GET /archives?date=. '
    'A row here is the claim that a bundle was published; the manifest inside it is the claim '
    'about what the bundle contains, and both are checksummed so a claim can be verified.';
COMMENT ON COLUMN archive_bundle.bundle_path IS
    'Bundle location relative to the archive root, never an absolute host path: the same row has '
    'to mean the same bundle read from the app container, from a restored backup, and from a '
    'developer''s checkout.';
COMMENT ON COLUMN archive_bundle.manifest IS
    'The manifest as published — per-file sha256, byte size, row count and L0 lineage. Stored '
    'rather than only referenced so that /archives can answer without reading the lake, and so a '
    'lost bundle is still provably described. dataplatform.status.models.ArchiveFile is the shape '
    'of its `files` entries and is the contract M1.12 writes to.';
COMMENT ON COLUMN archive_bundle.manifest_sha256 IS
    'SHA-256 of the manifest file as written into the bundle, so the stored copy above can be '
    'checked against the one on disk instead of being assumed to match it.';
