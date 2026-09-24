"""The gateway control loop: admission, routing, retries with fallback, settlement.

The gateway is written as two event handlers so that one implementation
serves both the deterministic simulator and the synchronous HTTP service:

``on_arrival(request, now)``            admit or reject, then choose the first endpoint
``on_attempt_done(attempt, outcome, now)``  record health, finish, or fall back
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .controls import BudgetExceeded, BudgetLedger, CircuitBreaker, RateLimiter
from .registry import AuditLog, PromptRegistry


@dataclass(frozen=True)
class Endpoint:
    name: str
    capabilities: frozenset[str]
    usd_per_mtok_input: float  # USD per million input tokens
    usd_per_mtok_output: float
    typical_latency_s: float  # prior for the latency estimate before any traffic

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.usd_per_mtok_input + output_tokens * self.usd_per_mtok_output) / 1e6


@dataclass(frozen=True)
class Request:
    request_id: str
    tenant: str
    capability: str
    prompt: str
    input_tokens: int
    max_output_tokens: int
    arrival: float = 0.0


@dataclass(frozen=True)
class Outcome:
    success: bool
    latency_s: float
    output_tokens: int = 0
    error: str | None = None


@dataclass
class Attempt:
    request: Request
    endpoint: Endpoint
    start: float
    number: int
    tried: tuple[str, ...]
    reserved_usd: float
    prompt_version: str


@dataclass
class Final:
    request: Request
    status: str  # ok | rejected:<reason> | failed
    finished: float
    endpoint: str | None = None
    attempts: int = 0
    cost_usd: float = 0.0
    prompt_version: str | None = None


@dataclass(frozen=True)
class Policy:
    """Resilience features, switchable so their effect can be measured one at a time."""

    name: str
    max_attempts: int = 1
    fallback: bool = False  # retry on a *different* endpoint
    breaker: bool = False
    latency_aware: bool = False
    backoff_s: float = 0.2
    cost_weight: float = 1.0
    latency_weight: float = 0.005  # USD one second of expected latency is worth


class LatencyEstimator:
    """EWMA of observed latency per endpoint, decaying back to a prior when idle.

    Failures count with their elapsed time, so a timing-out endpoint looks slow.
    Without decay, an endpoint that the router stops using would keep its last
    (bad) estimate forever and never win traffic back after it recovers; with a
    half-life, stale evidence fades and the router re-probes it.
    """

    def __init__(self, endpoints: Sequence[Endpoint], alpha: float = 0.2, half_life_s: float = 60.0) -> None:
        self.alpha = alpha
        self.half_life_s = half_life_s
        self.prior = {e.name: e.typical_latency_s for e in endpoints}
        self._value = dict(self.prior)
        self._updated = {e.name: 0.0 for e in endpoints}

    def estimate(self, endpoint: str, now: float) -> float:
        age = max(0.0, now - self._updated[endpoint])
        weight = 0.5 ** (age / self.half_life_s)
        return self.prior[endpoint] + (self._value[endpoint] - self.prior[endpoint]) * weight

    def observe(self, endpoint: str, latency: float, now: float) -> None:
        current = self.estimate(endpoint, now)
        self._value[endpoint] = current + self.alpha * (latency - current)
        self._updated[endpoint] = now


class Gateway:
    def __init__(
        self,
        endpoints: Sequence[Endpoint],
        limiter: RateLimiter,
        ledger: BudgetLedger,
        prompts: PromptRegistry,
        policy: Policy,
        breaker_factory: Callable[[], CircuitBreaker] = CircuitBreaker,
        audit: AuditLog | None = None,
    ) -> None:
        self.endpoints = list(endpoints)
        self.limiter = limiter
        self.ledger = ledger
        self.prompts = prompts
        self.policy = policy
        self.breakers = {e.name: breaker_factory() for e in self.endpoints}
        self.latency = LatencyEstimator(self.endpoints)
        self.audit = audit if audit is not None else AuditLog()
        self.attempts_by_endpoint = {e.name: 0 for e in self.endpoints}
        self.failures_by_endpoint = {e.name: 0 for e in self.endpoints}

    # ----------------------------------------------------------------- routing
    def rank(self, request: Request, now: float = 0.0) -> list[Endpoint]:
        """Capable endpoints, best first. Cost is estimated at the request's max output."""
        capable = [e for e in self.endpoints if request.capability in e.capabilities]

        def score(e: Endpoint) -> float:
            cost = e.cost(request.input_tokens, request.max_output_tokens)
            if not self.policy.latency_aware:
                return self.policy.cost_weight * cost
            return self.policy.cost_weight * cost + self.policy.latency_weight * self.latency.estimate(e.name, now)

        return sorted(capable, key=lambda e: (score(e), e.name))

    def _choose(self, request: Request, tried: tuple[str, ...], now: float) -> Endpoint | None:
        ranked = self.rank(request, now)
        if not ranked:
            return None
        if not self.policy.fallback:
            ranked = ranked[:1]  # always the preferred endpoint
        elif tried:
            ranked = [e for e in ranked if e.name not in tried] or ranked
        for endpoint in ranked:
            if not self.policy.breaker or self.breakers[endpoint.name].allow(now):
                return endpoint
        return None

    # ------------------------------------------------------------------ events
    def on_arrival(self, request: Request, now: float) -> Attempt | Final:
        estimate = request.input_tokens + request.max_output_tokens
        rejection = self.limiter.admit(request.tenant, estimate, now)
        if rejection:
            return self._finish(Final(request, f"rejected:{rejection}", now))
        ranked = self.rank(request, now)
        if not ranked:
            self.limiter.settle(request.tenant, estimate, 0, now)
            return self._finish(Final(request, "rejected:no_capable_endpoint", now))
        # Reserve the worst case first: a fallback may land on the most expensive endpoint.
        worst_case = max(e.cost(request.input_tokens, request.max_output_tokens) for e in ranked)
        try:
            self.ledger.reserve(request.tenant, worst_case)
        except BudgetExceeded:
            self.limiter.settle(request.tenant, estimate, 0, now)
            return self._finish(Final(request, "rejected:budget", now))
        endpoint = self._choose(request, (), now)
        if endpoint is None:
            self.ledger.release(request.tenant, worst_case)
            self.limiter.settle(request.tenant, estimate, 0, now)
            return self._finish(Final(request, "rejected:no_healthy_endpoint", now))
        version = self.prompts.resolve(request.prompt, request.request_id).version
        return self._attempt(request, endpoint, now, 1, (), worst_case, version)

    def on_attempt_done(self, attempt: Attempt, outcome: Outcome, now: float) -> Attempt | Final:
        name = attempt.endpoint.name
        self.latency.observe(name, outcome.latency_s, now)
        if self.policy.breaker:
            self.breakers[name].record(outcome.success, now)
        request = attempt.request
        estimate = request.input_tokens + request.max_output_tokens
        if outcome.success:
            actual = attempt.endpoint.cost(request.input_tokens, outcome.output_tokens)
            charged = self.ledger.commit(request.tenant, attempt.reserved_usd, actual)
            self.limiter.settle(request.tenant, estimate, request.input_tokens + outcome.output_tokens, now)
            return self._finish(Final(request, "ok", now, name, attempt.number, charged, attempt.prompt_version))
        self.failures_by_endpoint[name] += 1
        tried = (*attempt.tried, name)
        if attempt.number < self.policy.max_attempts:
            retry_at = now + self.policy.backoff_s * 2 ** (attempt.number - 1)
            endpoint = self._choose(request, tried, retry_at)
            if endpoint is not None:
                return self._attempt(
                    request, endpoint, retry_at, attempt.number + 1, tried, attempt.reserved_usd, attempt.prompt_version
                )
        self.ledger.release(request.tenant, attempt.reserved_usd)
        self.limiter.settle(request.tenant, estimate, request.input_tokens, now)
        return self._finish(Final(request, "failed", now, name, attempt.number, 0.0, attempt.prompt_version))

    def _attempt(self, request, endpoint, start, number, tried, reserved, version) -> Attempt:
        self.attempts_by_endpoint[endpoint.name] += 1
        return Attempt(request, endpoint, start, number, tried, reserved, version)

    def _finish(self, final: Final) -> Final:
        self.audit.append(
            {
                "request_id": final.request.request_id,
                "tenant": final.request.tenant,
                "status": final.status,
                "endpoint": final.endpoint,
                "attempts": final.attempts,
                "cost_usd": round(final.cost_usd, 8),
                "prompt_version": final.prompt_version,
                "t": round(final.finished, 6),
            }
        )
        return final

    # ------------------------------------------------------------- sync driver
    def handle(self, request: Request, call: Callable[[Attempt], Outcome], clock: Callable[[], float]) -> Final:
        """Run one request to completion against a blocking backend (used by the HTTP service)."""
        step = self.on_arrival(request, clock())
        while isinstance(step, Attempt):
            outcome = call(step)
            step = self.on_attempt_done(step, outcome, clock())
        return step
