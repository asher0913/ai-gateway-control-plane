import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from aigw.server import create_app, parse_api_keys  # noqa: E402

BODY = {"messages": [{"role": "user", "content": "hello there"}], "max_tokens": 64}
KEYS = {"sk-search-test": "search", "sk-batch-test": "batch"}
ADMIN = "admin-token-for-tests"
SEARCH = {"Authorization": "Bearer sk-search-test"}
BATCH = {"Authorization": "Bearer sk-batch-test"}
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN}"}


def _client(**kwargs) -> TestClient:
    return TestClient(create_app(api_keys=kwargs.pop("api_keys", KEYS), admin_token=kwargs.pop("admin", ADMIN)))


def test_chat_completion_and_admin_endpoints():
    client = _client()
    response = client.post("/v1/chat/completions", json=BODY, headers=SEARCH)
    assert response.status_code == 200
    assert response.json()["model"] == "primary"
    assert client.get("/v1/admin/breakers", headers=ADMIN_HEADERS).json()["primary"] == "closed"
    assert client.get("/v1/admin/audit/verify", headers=ADMIN_HEADERS).json()["intact"] is True


def test_requests_need_a_known_api_key():
    client = _client()
    assert client.post("/v1/chat/completions", json=BODY).status_code == 401
    assert client.post("/v1/chat/completions", json=BODY, headers={"x-tenant": "search"}).status_code == 401
    assert (
        client.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer sk-guess"}).status_code == 401
    )


def test_the_tenant_comes_from_the_key_not_the_header():
    client = _client()
    spoof = {**BATCH, "x-tenant": "search"}  # the batch tenant claims to be search
    assert client.post("/v1/chat/completions", json=BODY, headers=spoof).status_code == 200
    budgets = client.get("/v1/admin/budgets", headers=ADMIN_HEADERS).json()
    assert budgets["batch"]["spent"] > 0 and budgets["search"]["spent"] == 0


def test_admin_endpoints_reject_tenants_and_anonymous_callers():
    client = _client()
    for headers in ({}, SEARCH, {"Authorization": "Bearer wrong"}):
        assert client.post("/v1/admin/incidents/primary", params={"seconds": 600}, headers=headers).status_code == 401
        assert client.get("/v1/admin/budgets", headers=headers).status_code == 401
    assert client.get("/v1/admin/breakers", headers=ADMIN_HEADERS).json()["primary"] == "closed"  # nothing injected


def test_admin_api_is_disabled_without_an_admin_token():
    client = _client(admin="")
    assert client.get("/v1/admin/breakers", headers={"Authorization": "Bearer "}).status_code == 403


def test_app_refuses_to_start_without_api_keys(monkeypatch):
    monkeypatch.delenv("AIGW_API_KEYS", raising=False)
    with pytest.raises(RuntimeError):
        create_app()
    assert parse_api_keys("a=search, b=batch") == {"a": "search", "b": "batch"}
    with pytest.raises(ValueError):
        parse_api_keys("missing-tenant")


def test_injected_outage_fails_over_and_trips_the_breaker():
    client = _client()
    client.post("/v1/admin/incidents/primary", params={"seconds": 600}, headers=ADMIN_HEADERS)
    models = [client.post("/v1/chat/completions", json=BODY, headers=SEARCH).json()["model"] for _ in range(15)]
    assert "primary" not in models  # served by fallbacks (usually secondary) throughout the outage
    assert client.get("/v1/admin/breakers", headers=ADMIN_HEADERS).json()["primary"] == "open"
