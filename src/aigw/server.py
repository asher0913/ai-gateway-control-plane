"""OpenAI-compatible HTTP front end over the same control plane (optional extra ``server``).

Providers are simulated so the service runs anywhere; swapping ``call`` for a
real HTTP client is the only change needed to front live model APIs.
"""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Header, HTTPException
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


def create_app(policy: Policy | None = None) -> FastAPI:
    scenario = reference_scenario()
    scenario.incidents = []  # the live demo starts healthy; inject faults via the admin API
    gateway: Gateway = build_gateway(scenario, policy or POLICIES[-1])
    started = time.monotonic()
    app = FastAPI(title="AI gateway control plane")

    def clock() -> float:
        return time.monotonic() - started

    def call(attempt: Attempt) -> Outcome:
        return sample_outcome(scenario, attempt)  # simulated provider; no sleeping

    @app.post("/v1/chat/completions")
    def chat(body: ChatRequest, x_tenant: str = Header(...)) -> dict:
        text = " ".join(m.content for m in body.messages)
        request = Request(
            request_id=str(uuid.uuid4()),
            tenant=x_tenant,
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

    @app.get("/v1/admin/breakers")
    def breakers() -> dict:
        return {name: breaker.state.value for name, breaker in gateway.breakers.items()}

    @app.get("/v1/admin/budgets")
    def budgets() -> dict:
        return {
            t: {"spent": gateway.ledger.spent.get(t, 0.0), "limit": lim} for t, lim in gateway.ledger.limits.items()
        }

    @app.get("/v1/admin/audit/verify")
    def verify() -> dict:
        broken = gateway.audit.verify()
        return {"records": len(gateway.audit.records), "intact": broken is None, "first_bad_record": broken}

    @app.post("/v1/admin/incidents/{endpoint}")
    def inject(endpoint: str, seconds: float = 60.0) -> dict:
        from .sim import Incident

        now = clock()
        scenario.incidents.append(Incident(endpoint, now, now + seconds, "outage"))
        return {"endpoint": endpoint, "outage_until": now + seconds}

    return app
