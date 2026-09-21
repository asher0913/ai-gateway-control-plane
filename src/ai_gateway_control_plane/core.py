from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import time


@dataclass(frozen=True)
class Endpoint:
    name: str
    capabilities: frozenset[str]
    cost_per_1k: float
    latency_ms: float


@dataclass(frozen=True)
class Request:
    request_id: str
    api_key: str
    capability: str
    estimated_tokens: int
    prompt_name: str
    prompt_version: str | None = None


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = refill_per_second
        self.updated = time.monotonic()

    def consume(self, amount: int) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_rate)
        self.updated = now
        if amount > self.tokens:
            return False
        self.tokens -= amount
        return True


class CircuitBreaker:
    def __init__(self, threshold: int = 2, recovery_seconds: float = 5.0) -> None:
        self.threshold = threshold
        self.recovery_seconds = recovery_seconds
        self.failures: dict[str, int] = {}
        self.opened_at: dict[str, float] = {}

    def available(self, endpoint: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if endpoint not in self.opened_at:
            return True
        if now - self.opened_at[endpoint] >= self.recovery_seconds:
            self.failures[endpoint] = 0
            self.opened_at.pop(endpoint, None)
            return True
        return False

    def record(self, endpoint: str, success: bool, now: float | None = None) -> None:
        if success:
            self.failures[endpoint] = 0
            self.opened_at.pop(endpoint, None)
            return
        self.failures[endpoint] = self.failures.get(endpoint, 0) + 1
        if self.failures[endpoint] >= self.threshold:
            self.opened_at[endpoint] = time.monotonic() if now is None else now


class PromptRegistry:
    def __init__(self) -> None:
        self.versions: dict[str, dict[str, str]] = {}
        self.active: dict[str, str] = {}

    def publish(self, name: str, version: str, template: str) -> None:
        bucket = self.versions.setdefault(name, {})
        if version in bucket:
            raise ValueError("prompt version already exists")
        bucket[version] = template
        self.active.setdefault(name, version)

    def activate(self, name: str, version: str) -> None:
        if version not in self.versions.get(name, {}):
            raise KeyError(version)
        self.active[name] = version

    def resolve(self, name: str, version: str | None = None) -> tuple[str, str]:
        selected = version or self.active[name]
        return selected, self.versions[name][selected]


class Gateway:
    def __init__(self, endpoints: list[Endpoint], budgets: dict[str, float], limit: int = 4000) -> None:
        self.endpoints = endpoints
        self.budgets = dict(budgets)
        self.buckets = {key: TokenBucket(limit, limit / 60) for key in budgets}
        self.breaker = CircuitBreaker()
        self.prompts = PromptRegistry()
        self.audit: list[dict[str, object]] = []

    def route(self, capability: str) -> Endpoint:
        candidates = [e for e in self.endpoints if capability in e.capabilities and self.breaker.available(e.name)]
        if not candidates:
            raise RuntimeError("no healthy endpoint")
        return min(candidates, key=lambda e: (e.cost_per_1k * 0.65 + e.latency_ms / 1000 * 0.35, e.name))

    def handle(self, request: Request, actual_tokens: int | None = None, success: bool = True) -> dict[str, object]:
        if request.api_key not in self.budgets:
            raise PermissionError("unknown api key")
        if not self.buckets[request.api_key].consume(request.estimated_tokens):
            return self._record(request, "rate_limited")
        endpoint = self.route(request.capability)
        estimated_cost = request.estimated_tokens / 1000 * endpoint.cost_per_1k
        if estimated_cost > self.budgets[request.api_key]:
            return self._record(request, "budget_exceeded", endpoint.name)
        version, _ = self.prompts.resolve(request.prompt_name, request.prompt_version)
        used = actual_tokens if actual_tokens is not None else request.estimated_tokens
        cost = used / 1000 * endpoint.cost_per_1k
        self.budgets[request.api_key] -= cost
        self.breaker.record(endpoint.name, success)
        return self._record(request, "ok" if success else "upstream_error", endpoint.name, version, cost)

    def _record(self, request: Request, status: str, endpoint: str | None = None, prompt_version: str | None = None, cost: float = 0.0) -> dict[str, object]:
        record = {"request_id": request.request_id, "status": status, "endpoint": endpoint, "prompt_version": prompt_version, "cost": round(cost, 6)}
        self.audit.append(record)
        return record


def fixture() -> Gateway:
    gateway = Gateway([
        Endpoint("fast", frozenset({"chat"}), 0.8, 120),
        Endpoint("reasoner", frozenset({"chat", "reasoning"}), 1.6, 350),
        Endpoint("backup", frozenset({"chat", "reasoning"}), 2.0, 500),
    ], {"demo": 2.0})
    gateway.prompts.publish("assistant", "v1", "Answer with evidence.")
    gateway.prompts.publish("assistant", "v2", "Answer with evidence and uncertainty.")
    gateway.prompts.activate("assistant", "v2")
    return gateway


def demo() -> dict[str, object]:
    gateway = fixture()
    records = [gateway.handle(Request(f"r{i}", "demo", "chat", 100 + i * 10, "assistant"), actual_tokens=80 + i * 10) for i in range(5)]
    return {"requests": len(records), "statuses": [record["status"] for record in records], "remaining_budget": round(gateway.budgets["demo"], 4), "endpoints": sorted({record["endpoint"] for record in records if record["endpoint"]})}


if __name__ == "__main__":
    print(json.dumps(demo(), indent=2, sort_keys=True))
