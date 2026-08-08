"""D5: the status API — and the process the `app` container runs (M0.3).

Skeleton on purpose. M0.5 owns the §4.4 surface (`/status/sync`, `/status/sources`,
`/status/gaps`, `/status/quality`, `/archives`) and the DB-backed scheduler heartbeat in
`/health`; this module exists so the compose stack has a real ASGI app to serve before that
lands. It touches no database and fabricates no state — process liveness is a fact about this
process, which is the only thing it reports.
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="trading-platform status API", version="0.1.0")


class Liveness(BaseModel):
    """Response contract for `/health`. M0.5 adds the scheduler heartbeat age beside it."""

    status: str


@app.get("/health")
def health() -> Liveness:
    """Report that this process is serving requests.

    Assumes nothing about the database, the scheduler or the lake, and never queries them —
    a green `/health` from this skeleton means the container is up and nothing more.
    """
    return Liveness(status="ok")
