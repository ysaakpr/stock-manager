"""The app container's entrypoint must import and serve (M0.3 acceptance criterion 1).

A container whose CMD cannot import its ASGI app exits instead of running, so this is the
offline half of that criterion: it fails here, in a fast unit test, rather than as a crash
loop in docker. M0.5 replaces the skeleton and adds the integration coverage for the real
§4.4 endpoints.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from dataplatform.status.api import app


def test_health_reports_process_liveness() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
