#!/bin/sh
# M0.3: the app container's entrypoint. Applies pending platform/store migrations before the
# process starts serving, so the container never comes up talking to a schema `/health` cannot
# read (the M0 gate audit's box 1 failure). `dataplatform.store.migrate` is already idempotent —
# a second run applies nothing — so this is safe on every restart, not just a cold one.
#
# `set -eu`: a migration failure (bad SQL, an edited applied file, an unreachable DB) must exit
# this script non-zero and never reach `exec "$@"` — the app must not start serving on a
# half-migrated schema.
set -eu

echo "entrypoint: applying migrations" >&2
python -m dataplatform.store.migrate

exec "$@"
