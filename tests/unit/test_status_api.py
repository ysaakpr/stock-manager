"""The app container's entrypoint imports and exposes the whole §4.4 surface — offline.

A container whose CMD cannot import its ASGI app exits instead of running, so this is the fast
offline half of M0.3's first acceptance criterion: it fails here, in a unit test, rather than as a
crash loop in docker. It also pins the surface itself, because the daily loop, the M0/M1 gate
scripts and the future UI address these six paths by name — a route quietly renamed or dropped is
a broken consumer, and that is worth catching without a database.

Behaviour needs rows, a clock and Postgres, and lives in `tests/integration/test_status_api.py`.
Nothing here connects to anything: importing the app must stay free of I/O, or every unit run in
the suite starts depending on `make up`.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.routing import APIRoute

from dataplatform.status.api import app

#: The §4.4 surface, verbatim from EXECUTION_PLAN.md.
SURFACE = frozenset(
    {
        "/health",
        "/status/sync",
        "/status/sources",
        "/status/gaps",
        "/status/quality",
        "/archives",
    }
)


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    """The generated schema — which is also the proof every response model resolves."""
    return app.openapi()


def test_the_entrypoint_exposes_every_section_4_4_endpoint() -> None:
    served = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert served >= SURFACE, f"missing from the §4.4 surface: {sorted(SURFACE - served)}"


def test_every_endpoint_is_a_get_and_declares_a_response_model() -> None:
    """A status endpoint that writes, or that returns an undeclared shape, is not a contract."""
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path in SURFACE:
            assert route.methods == {"GET"}, f"{route.path} serves {route.methods}"
            assert route.response_model is not None, f"{route.path} declares no response model"


def test_the_openapi_schema_builds_and_documents_the_surface(openapi: dict[str, Any]) -> None:
    assert set(openapi["paths"]) >= SURFACE


def test_health_documents_its_503(openapi: dict[str, Any]) -> None:
    """The staleness contract is part of the API, not an implementation detail of the probe."""
    assert "503" in openapi["paths"]["/health"]["get"]["responses"]
