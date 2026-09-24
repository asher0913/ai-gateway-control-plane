"""Deterministic fault-injection simulation of the gateway on a virtual clock.

Providers are modelled as latency distributions with a baseline error rate,
plus scheduled incidents: a hard *outage* (requests fail after a connect
timeout) and a *brownout* (latency inflates and some requests fail). The same
seeded traffic is replayed against each resilience policy.
"""

from __future__ import annotations

import heapq
import math
import random
import statistics
from dataclasses import dataclass, field

from .controls import BudgetLedger, CircuitBreaker, RateLimiter, TenantLimits
from .gateway import Attempt, Endpoint, Final, Gateway, Outcome, Policy, Request
from .registry import PromptRegistry


@dataclass(frozen=True)
class Incident:
    endpoint: str
    start: float
    end: float
    kind: str  # "outage" | "brownout"
    latency_multiplier: float = 1.0
    error_rate: float = 1.0


@dataclass(frozen=True)
class ProviderModel:
    median_latency_s: float
    sigma: float = 0.35  # lognormal spread
    per_output_token_s: float = 0.004
    error_rate: float = 0.005
    timeout_s: float = 10.0
    connect_fail_s: float = 1.0  # how long a hard outage takes to surface as an error


@dataclass
class Scenario:
    endpoints: list[Endpoint]
    providers: dict[str, ProviderModel]
    incidents: list[Incident]
    tenants: dict[str, TenantLimits]
    tenant_rates: dict[str, float]  # requests per second
    budgets_usd: dict[str, float]
    duration_s: float = 3600.0
    seed: int = 0
    breaker: dict = field(default_factory=lambda: {"window": 20, "min_calls": 10, "failure_rate": 0.5, "cooldown": 30})


def reference_scenario(seed: int = 0) -> Scenario:
    chat = frozenset({"chat"})
    endpoints = [
        # Price tiers in USD per million tokens, similar to small / large hosted models.
        Endpoint("primary", chat | {"json"}, 0.15, 0.60, 2.0),
        Endpoint("secondary", chat | {"json"}, 2.50, 10.00, 2.6),
        Endpoint("backup", chat, 3.00, 15.00, 3.2),
    ]
    providers = {
        "primary": ProviderModel(median_latency_s=0.9),
        "secondary": ProviderModel(median_latency_s=1.3),
        "backup": ProviderModel(median_latency_s=1.8, error_rate=0.01),
    }
    incidents = [
        Incident("primary", 900, 1500, "outage"),
        Incident("primary", 2400, 3000, "brownout", latency_multiplier=3.0, error_rate=0.05),
    ]
    tenants = {
        "search": TenantLimits(requests_per_minute=240, tokens_per_minute=400_000, max_concurrency=40),
        "support": TenantLimits(requests_per_minute=120, tokens_per_minute=200_000, max_concurrency=20),
        "batch": TenantLimits(requests_per_minute=60, tokens_per_minute=150_000, max_concurrency=8),
    }
    # "batch" offers twice its allowed request rate: the limiter must contain it without hurting others.
    rates = {"search": 2.5, "support": 1.2, "batch": 2.0}
    # "batch" also has a small monthly budget slice that runs out part-way through the hour.
    budgets = {"search": 100.0, "support": 50.0, "batch": 1.0}
    return Scenario(endpoints, providers, incidents, tenants, rates, budgets, seed=seed)


def traffic(scenario: Scenario) -> list[Request]:
    rng = random.Random(scenario.seed)
    requests = []
    for tenant, rate in sorted(scenario.tenant_rates.items()):
        t = 0.0
        index = 0
        while True:
            t += rng.expovariate(rate)
            if t >= scenario.duration_s:
                break
            capability = "json" if tenant == "search" and rng.random() < 0.3 else "chat"
            requests.append(
                Request(
                    request_id=f"{tenant}-{index}",
                    tenant=tenant,
                    capability=capability,
                    prompt="assistant",
                    input_tokens=rng.randint(200, 3000),
                    max_output_tokens=rng.choice((256, 512, 1024)),
                    arrival=t,
                )
            )
            index += 1
    requests.sort(key=lambda r: (r.arrival, r.request_id))
    return requests


def sample_outcome(scenario: Scenario, attempt: Attempt) -> Outcome:
    """Deterministic per (request, attempt): identical draws under every policy."""
    name = attempt.endpoint.name
    model = scenario.providers[name]
    rng = random.Random(f"{scenario.seed}:{attempt.request.request_id}:{attempt.number}:{name}")
    active = [i for i in scenario.incidents if i.endpoint == name and i.start <= attempt.start < i.end]
    output_tokens = rng.randint(attempt.request.max_output_tokens // 4, attempt.request.max_output_tokens)
    latency = model.median_latency_s * math.exp(rng.gauss(0, model.sigma)) + output_tokens * model.per_output_token_s
    error_rate = model.error_rate
    for incident in active:
        if incident.kind == "outage":
            return Outcome(False, model.connect_fail_s * (0.8 + 0.4 * rng.random()), error="unavailable")
        latency *= incident.latency_multiplier
        error_rate = max(error_rate, incident.error_rate)
    if latency > model.timeout_s:
        return Outcome(False, model.timeout_s, error="timeout")
    if rng.random() < error_rate:
        return Outcome(False, latency * rng.random(), error="server_error")
    return Outcome(True, latency, output_tokens)


POLICIES = [
    Policy("single endpoint, no retries"),
    Policy("same-endpoint retries (x3)", max_attempts=3),
    Policy("fallback to other endpoints", max_attempts=3, fallback=True),
    Policy("fallback + circuit breaker", max_attempts=3, fallback=True, breaker=True),
    Policy("fallback + breaker + latency-aware", max_attempts=3, fallback=True, breaker=True, latency_aware=True),
]


def build_gateway(scenario: Scenario, policy: Policy) -> Gateway:
    prompts = PromptRegistry()
    prompts.publish("assistant", "v1", "You are a helpful assistant.")
    prompts.publish("assistant", "v2", "You are a helpful assistant. Cite sources when you can.")
    prompts.start_canary("assistant", "v2", 0.1)
    return Gateway(
        scenario.endpoints,
        RateLimiter(scenario.tenants),
        BudgetLedger(dict(scenario.budgets_usd)),
        prompts,
        policy,
        breaker_factory=lambda: CircuitBreaker(**scenario.breaker),
    )


def run(scenario: Scenario, policy: Policy, requests: list[Request] | None = None) -> dict:
    """Event loop: arrivals and attempt completions processed in time order."""
    requests = requests if requests is not None else traffic(scenario)
    gateway = build_gateway(scenario, policy)
    events: list[tuple[float, int, str, object]] = []
    sequence = 0
    for request in requests:
        heapq.heappush(events, (request.arrival, sequence, "arrival", request))
        sequence += 1
    finals: list[Final] = []

    def schedule(step):
        nonlocal sequence
        if isinstance(step, Final):
            finals.append(step)
            return
        outcome = sample_outcome(scenario, step)
        heapq.heappush(events, (step.start + outcome.latency_s, sequence, "done", (step, outcome)))
        sequence += 1

    while events:
        now, _, kind, payload = heapq.heappop(events)
        if kind == "arrival":
            schedule(gateway.on_arrival(payload, now))
        else:
            attempt, outcome = payload
            schedule(gateway.on_attempt_done(attempt, outcome, now))

    return summarize(scenario, policy, gateway, finals)


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q / 100 * (len(ordered) - 1)))]


def summarize(scenario: Scenario, policy: Policy, gateway: Gateway, finals: list[Final]) -> dict:
    admitted = [f for f in finals if not f.status.startswith("rejected")]
    ok = [f for f in admitted if f.status == "ok"]
    latencies = [f.finished - f.request.arrival for f in ok]
    by_tenant = {}
    for tenant in scenario.tenants:
        mine = [f for f in finals if f.request.tenant == tenant]
        mine_admitted = [f for f in mine if not f.status.startswith("rejected")]
        by_tenant[tenant] = {
            "offered": len(mine),
            "rate_limited": sum(
                f.status in ("rejected:request_rate", "rejected:token_rate", "rejected:concurrency") for f in mine
            ),
            "budget_rejected": sum(f.status == "rejected:budget" for f in mine),
            "success_rate_admitted": sum(f.status == "ok" for f in mine_admitted) / len(mine_admitted)
            if mine_admitted
            else 0.0,
            "spent_usd": round(gateway.ledger.spent.get(tenant, 0.0), 4),
        }
    incident_windows = [(i.start, i.end, i.kind) for i in scenario.incidents]

    def success_in(window):
        start, end, _ = window
        inside = [f for f in admitted if start <= f.request.arrival < end]
        return sum(f.status == "ok" for f in inside) / len(inside) if inside else 0.0

    timeline, p95_timeline = [], []
    for minute in range(int(scenario.duration_s // 60)):
        inside = [f for f in admitted if minute * 60 <= f.request.arrival < (minute + 1) * 60]
        timeline.append(sum(f.status == "ok" for f in inside) / len(inside) if inside else 1.0)
        done = [f.finished - f.request.arrival for f in inside if f.status == "ok"]
        p95_timeline.append(_pct(done, 95) if done else None)  # no successes: no latency to report

    return {
        "policy": policy.name,
        "offered": len(finals),
        "admitted": len(admitted),
        "success_rate": len(ok) / len(admitted) if admitted else 0.0,
        "success_rate_during": {kind: round(success_in(w), 4) for w in incident_windows for kind in [w[2]]},
        "no_healthy_endpoint": sum(f.status == "rejected:no_healthy_endpoint" for f in finals),
        "latency_p50_s": _pct(latencies, 50),
        "latency_p95_s": _pct(latencies, 95),
        "latency_p99_s": _pct(latencies, 99),
        "mean_attempts": statistics.fmean(f.attempts for f in admitted) if admitted else 0.0,
        "attempts_by_endpoint": dict(gateway.attempts_by_endpoint),
        "failed_attempts_by_endpoint": dict(gateway.failures_by_endpoint),
        "cost_usd": round(sum(f.cost_usd for f in ok), 4),
        "cost_per_success_usd": sum(f.cost_usd for f in ok) / len(ok) if ok else 0.0,
        "tenants": by_tenant,
        "budget_invariant_held": all(
            gateway.ledger.spent.get(t, 0.0) <= gateway.ledger.limits[t] + 1e-9 for t in gateway.ledger.limits
        ),
        "breaker_transitions": {name: b.transitions for name, b in gateway.breakers.items() if b.transitions},
        "audit_records": len(gateway.audit.records),
        "audit_chain_intact": gateway.audit.verify() is None,
        "prompt_versions": {
            v: sum(f.prompt_version == v for f in ok)
            for v in sorted({f.prompt_version for f in ok if f.prompt_version})
        },
        "success_timeline_per_minute": [round(x, 4) for x in timeline],
        "p95_latency_timeline_per_minute": [None if x is None else round(x, 3) for x in p95_timeline],
    }


def compare(seed: int = 0) -> dict:
    scenario = reference_scenario(seed)
    requests = traffic(scenario)
    return {
        "scenario": {
            "duration_s": scenario.duration_s,
            "requests": len(requests),
            "incidents": [i.__dict__ for i in scenario.incidents],
            "tenant_rates": scenario.tenant_rates,
        },
        "policies": [run(scenario, policy, requests) for policy in POLICIES],
    }
