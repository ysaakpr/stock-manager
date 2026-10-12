"""M18: the operator console — server-rendered pages over the status API's own queries.

One web view of the running platform for the operator (§8.1's FastAPI + HTMX), mounted on the
status app at `/console` rather than run as a new service (decision #13). It reads Postgres and the
lake only through `dataplatform.status.queries`, the functions the JSON endpoints use, so a page
can never disagree with `/status/*`; it is read-only, loopback-only like the API (M15.6), renders
every instant in IST, never renders a credential, and loads nothing from outside this process.

`router` is the whole public surface; `api.py` includes it.
"""

from dataplatform.status.console.routes import READ_METHODS, router

__all__ = ["READ_METHODS", "router"]
