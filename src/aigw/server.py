"""OpenAI-compatible HTTP front end over the same control plane (optional extra ``server``).

Providers are simulated so the service runs anywhere; swapping ``call`` for a
real HTTP client is the only change needed to front live model APIs.

Who is calling is decided by the server, not the client:

- ``/v1/chat/completions`` needs ``Authorization: Bearer <tenant API key>``. The tenant, and so
  the rate limits and budget charged, comes from the key. An ``x-tenant`` header is ignored.
- ``/v1/admin/*`` (fault injection, breaker, budget and audit state) needs the separate admin
  token. With no admin token configured, the admin API is disabled.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .gateway import Attempt, Final, Gateway, Outcome, Policy, Request
from .sim import POLICIES, build_gateway, reference_scenario, sample_outcome


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = Field(default="chat", description="capability to route on: chat or json")
    messages: list[Message]
    max_tokens: int = Field(default=512, ge=1, le=4096)
    prompt: str = "assistant"


STATUS_CODES = {
    "rejected:request_rate": 429,
    "rejected:token_rate": 429,
    "rejected:concurrency": 429,
    "rejected:budget": 402,
    "rejected:unknown_tenant": 401,
    "rejected:no_capable_endpoint": 400,
    "rejected:no_healthy_endpoint": 503,
    "failed": 502,
}


API_KEYS_ENV = "AIGW_API_KEYS"  # "key1=search,key2=support"
ADMIN_TOKEN_ENV = "AIGW_ADMIN_TOKEN"


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def parse_api_keys(spec: str) -> dict[str, str]:
    """``"key1=search,key2=support"`` -> {key: tenant}."""
    keys = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        key, sep, tenant = item.partition("=")
        if not sep or not key or not tenant:
            raise ValueError(f"expected key=tenant, got {item!r}")
        keys[key] = tenant
    return keys


def demo_credentials(tenants) -> tuple[dict[str, str], str]:
    """Random per-tenant API keys and an admin token, for ``aigw serve`` without configuration."""
    return {f"sk-{tenant}-{secrets.token_hex(12)}": tenant for tenant in tenants}, secrets.token_hex(16)


def _bearer(authorization: str | None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def create_app(
    policy: Policy | None = None, api_keys: dict[str, str] | None = None, admin_token: str | None = None
) -> FastAPI:
    """``api_keys`` maps API key -> tenant; ``admin_token`` enables the admin API.

    Both default to the ``AIGW_API_KEYS`` and ``AIGW_ADMIN_TOKEN`` environment variables.
    Keys are kept only as SHA-256 digests.
    """
    if api_keys is None:
        api_keys = parse_api_keys(os.environ.get(API_KEYS_ENV, ""))
    if not api_keys:
        raise RuntimeError(f"no API keys configured: pass api_keys or set {API_KEYS_ENV}=key=tenant,...")
    admin_token = admin_token if admin_token is not None else os.environ.get(ADMIN_TOKEN_ENV)
    tenant_of_key = {_digest(key): tenant for key, tenant in api_keys.items()}
    admin_digest = _digest(admin_token) if admin_token else None
    scenario = reference_scenario()
    scenario.incidents = []  # the live demo starts healthy; inject faults via the admin API
    gateway: Gateway = build_gateway(scenario, policy or POLICIES[-1])
    started = time.monotonic()
    app = FastAPI(title="AI gateway control plane")

    def clock() -> float:
        return time.monotonic() - started

    def call(attempt: Attempt) -> Outcome:
        return sample_outcome(scenario, attempt)  # simulated provider; no sleeping

    def tenant(authorization: str | None = Header(default=None)) -> str:
        key = _bearer(authorization)
        found = tenant_of_key.get(_digest(key)) if key else None
        if found is None:
            raise HTTPException(401, "missing or unknown API key", headers={"WWW-Authenticate": "Bearer"})
        return found

    def admin(authorization: str | None = Header(default=None)) -> None:
        if admin_digest is None:
            raise HTTPException(403, "the admin API is disabled: no admin token configured")
        token = _bearer(authorization)
        if token is None or not hmac.compare_digest(_digest(token), admin_digest):
            raise HTTPException(401, "admin token required", headers={"WWW-Authenticate": "Bearer"})

    @app.post("/v1/chat/completions")
    def chat(body: ChatRequest, caller: str = Depends(tenant)) -> dict:  # noqa: B008
        text = " ".join(m.content for m in body.messages)
        request = Request(
            request_id=str(uuid.uuid4()),
            tenant=caller,
            capability=body.model,
            prompt=body.prompt,
            input_tokens=max(1, len(text) // 4),
            max_output_tokens=body.max_tokens,
            arrival=clock(),
        )
        final: Final = gateway.handle(request, call, clock)
        if final.status != "ok":
            raise HTTPException(STATUS_CODES.get(final.status, 500), detail=final.status)
        return {
            "id": request.request_id,
            "object": "chat.completion",
            "model": final.endpoint,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "(simulated)"}}],
            "usage": {"prompt_tokens": request.input_tokens},
            "gateway": {"attempts": final.attempts, "cost_usd": final.cost_usd, "prompt_version": final.prompt_version},
        }

    guarded = [Depends(admin)]

    @app.get("/v1/admin/breakers", dependencies=guarded)
    def breakers() -> dict:
        return {name: breaker.state.value for name, breaker in gateway.breakers.items()}

    @app.get("/v1/admin/budgets", dependencies=guarded)
    def budgets() -> dict:
        return {
            t: {"spent": gateway.ledger.spent.get(t, 0.0), "limit": lim} for t, lim in gateway.ledger.limits.items()
        }

    @app.get("/v1/admin/audit/verify", dependencies=guarded)
    def verify() -> dict:
        broken = gateway.audit.verify()
        return {"records": len(gateway.audit.records), "intact": broken is None, "first_bad_record": broken}

    @app.post("/v1/admin/incidents/{endpoint}", dependencies=guarded)
    def inject(endpoint: str, seconds: float = 60.0) -> dict:
        from .sim import Incident

        now = clock()
        scenario.incidents.append(Incident(endpoint, now, now + seconds, "outage"))
        return {"endpoint": endpoint, "outage_until": now + seconds}

    return app
