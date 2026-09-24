import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aigw.server import create_app  # noqa: E402

BODY = {"messages": [{"role": "user", "content": "hello there"}], "max_tokens": 64}


def test_chat_completion_and_admin_endpoints():
    client = TestClient(create_app())
    response = client.post("/v1/chat/completions", json=BODY, headers={"x-tenant": "search"})
    assert response.status_code == 200
    assert response.json()["model"] == "primary"
    assert client.get("/v1/admin/breakers").json()["primary"] == "closed"
    assert client.get("/v1/admin/audit/verify").json()["intact"] is True


def test_error_mapping():
    client = TestClient(create_app())
    assert client.post("/v1/chat/completions", json=BODY, headers={"x-tenant": "nobody"}).status_code == 401
    assert client.post("/v1/chat/completions", json=BODY).status_code == 422  # missing tenant header


def test_injected_outage_fails_over_and_trips_the_breaker():
    client = TestClient(create_app())
    client.post("/v1/admin/incidents/primary", params={"seconds": 600})
    models = [
        client.post("/v1/chat/completions", json=BODY, headers={"x-tenant": "search"}).json()["model"]
        for _ in range(15)
    ]
    assert "primary" not in models  # served by fallbacks (usually secondary) throughout the outage
    assert client.get("/v1/admin/breakers").json()["primary"] == "open"
