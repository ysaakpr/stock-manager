# Backlog

Improvements agents noticed while building something else. One line each, with the task id that
spotted it. Nothing here is committed work — it is a list of candidates for the owner to promote
into `TASK_GRAPH.yaml` if worth doing.

Agents: add a line here rather than expanding your task's scope (AGENTIC_CONTEXT.md §1).

| Spotted by | Item |
|---|---|
| setup | Object-storage target for L0 + Postgres backups is undecided; `ops/backup.sh` is local-only until one exists. |
| setup | Golden CA suite starts at 7 named cases; §4.3 expects ~13 more ugly cases collected during backfill. |
| M0.1 | `orchestrator/` is excluded from the gate (6 files unformatted, 7 mypy-strict errors). Bringing the build machinery under `make check` is a small, separate task. |
| M0.1 | The `platform/` → `dataplatform/` rename is documented in CLAUDE.md but ~30 `platform/…` path strings in TASK_GRAPH.yaml specs/deliverables still read the old name; a mechanical sweep would remove the indirection. |
