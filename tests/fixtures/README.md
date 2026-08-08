# Frozen test fixtures

Checked in on purpose (AGENTIC_CONTEXT.md B8). The suite is offline and deterministic: no test in
this repo may touch the network.

Layout: `tests/fixtures/<source>/<era>/` — one sample file per source per **format era**, so a
parser change has to keep passing every era the source has ever emitted. Record where each file
came from (URL, date fetched) next to it or in the source register, never a bare blob.

Also here: `cases/` for ratified-fixture case configs (B9).
