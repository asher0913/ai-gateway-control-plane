"""Admission controls: token buckets, concurrency limits, budgets and circuit breakers.

Every component takes the current time as an argument instead of reading a
clock. The same code therefore runs under a real clock in the HTTP service and
under a virtual clock in the deterministic simulator.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class TokenBucket:
    """Classic token bucket: ``capacity`` burst, refilled at ``rate`` per second."""

    def __init__(self, capacity: float, rate: float, now: float = 0.0) -> None:
        if capacity <= 0 or rate <= 0:
            raise ValueError("capacity and rate must be positive")
        self.capacity = capacity
        self.rate = rate
        self.tokens = capacity
        self.updated = now

    def _refill(self, now: float) -> None:
        if now > self.updated:
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now

    def try_take(self, amount: float, now: float) -> bool:
        self._refill(now)
        if amount > self.tokens:
            return False
        self.tokens -= amount
        return True

    def adjust(self, delta: float, now: float) -> None:
        """Settle an estimate: positive ``delta`` refunds, negative charges (may go into debt)."""
        self._refill(now)
        self.tokens = min(self.capacity, self.tokens + delta)


@dataclass
class TenantLimits:
    requests_per_minute: float
    tokens_per_minute: float
    max_concurrency: int


class RateLimiter:
    """Per-tenant request rate, token rate and in-flight limits.

    Token usage is unknown until the response arrives, so admission charges an
    *estimate* and :meth:`settle` corrects it with the actual count. Refunds
    make over-estimation harmless; under-estimation shows up as debt that
    delays the tenant's next requests.
    """

    def __init__(self, limits: dict[str, TenantLimits], now: float = 0.0) -> None:
        self.limits = limits
        self.requests = {
            t: TokenBucket(lim.requests_per_minute / 6, lim.requests_per_minute / 60, now) for t, lim in limits.items()
        }
        self.tokens = {
            t: TokenBucket(lim.tokens_per_minute / 6, lim.tokens_per_minute / 60, now) for t, lim in limits.items()
        }
        self.in_flight = {t: 0 for t in limits}

    def admit(self, tenant: str, estimated_tokens: int, now: float) -> str | None:
        """Return ``None`` if admitted, else the name of the limit that rejected it."""
        if tenant not in self.limits:
            return "unknown_tenant"
        if self.in_flight[tenant] >= self.limits[tenant].max_concurrency:
            return "concurrency"
        if not self.requests[tenant].try_take(1, now):
            return "request_rate"
        if not self.tokens[tenant].try_take(estimated_tokens, now):
            self.requests[tenant].adjust(1, now)  # give the request slot back
            return "token_rate"
        self.in_flight[tenant] += 1
        return None

    def settle(self, tenant: str, estimated_tokens: int, actual_tokens: int, now: float) -> None:
        self.in_flight[tenant] -= 1
        self.tokens[tenant].adjust(estimated_tokens - actual_tokens, now)


class BudgetExceeded(Exception):
    pass


@dataclass
class BudgetLedger:
    """Spend limits with reservations, so concurrent requests cannot overspend.

    ``reserve`` holds the worst-case cost before a call; ``commit`` converts
    the hold into actual spend (never more than was reserved); ``release``
    drops it on failure. Invariant: ``spent + reserved <= limit`` at all times.
    """

    limits: dict[str, float]
    spent: dict[str, float] = field(default_factory=dict)
    reserved: dict[str, float] = field(default_factory=dict)

    def available(self, tenant: str) -> float:
        return self.limits[tenant] - self.spent.get(tenant, 0.0) - self.reserved.get(tenant, 0.0)

    def reserve(self, tenant: str, amount: float) -> None:
        if tenant not in self.limits:
            raise KeyError(tenant)
        if amount > self.available(tenant) + 1e-12:
            raise BudgetExceeded(f"{tenant}: needs {amount:.6f}, has {self.available(tenant):.6f}")
        self.reserved[tenant] = self.reserved.get(tenant, 0.0) + amount

    def commit(self, tenant: str, reserved: float, actual: float) -> float:
        charged = min(actual, reserved)
        self.reserved[tenant] -= reserved
        self.spent[tenant] = self.spent.get(tenant, 0.0) + charged
        return charged

    def release(self, tenant: str, reserved: float) -> None:
        self.reserved[tenant] -= reserved


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Failure-rate breaker over a sliding window of recent calls.

    CLOSED → OPEN when at least ``min_calls`` of the last ``window`` calls are
    recorded and the failure rate reaches ``failure_rate``. OPEN rejects calls
    for ``cooldown`` seconds, then HALF_OPEN lets ``probes`` trial calls through:
    all succeed → CLOSED with a fresh window, any fails → OPEN again.
    """

    def __init__(
        self,
        window: int = 20,
        min_calls: int = 10,
        failure_rate: float = 0.5,
        cooldown: float = 30.0,
        probes: int = 3,
    ) -> None:
        self.window = window
        self.min_calls = min_calls
        self.failure_rate = failure_rate
        self.cooldown = cooldown
        self.probes = probes
        self.state = BreakerState.CLOSED
        self.outcomes: deque[bool] = deque(maxlen=window)
        self.opened_at = 0.0
        self.probes_started = 0
        self.probe_successes = 0
        self.transitions: list[tuple[float, str]] = []

    def _move(self, state: BreakerState, now: float) -> None:
        self.state = state
        self.transitions.append((now, state.value))
        if state is BreakerState.OPEN:
            self.opened_at = now
        if state is BreakerState.HALF_OPEN:
            self.probes_started = self.probe_successes = 0
        if state is BreakerState.CLOSED:
            self.outcomes.clear()

    def allow(self, now: float) -> bool:
        if self.state is BreakerState.OPEN and now - self.opened_at >= self.cooldown:
            self._move(BreakerState.HALF_OPEN, now)
        if self.state is BreakerState.OPEN:
            return False
        if self.state is BreakerState.HALF_OPEN:
            if self.probes_started >= self.probes:
                return False
            self.probes_started += 1
        return True

    def record(self, success: bool, now: float) -> None:
        if self.state is BreakerState.HALF_OPEN:
            if not success:
                self._move(BreakerState.OPEN, now)
                return
            self.probe_successes += 1
            if self.probe_successes >= self.probes:
                self._move(BreakerState.CLOSED, now)
            return
        if self.state is BreakerState.OPEN:
            return  # late result from a call admitted before the breaker opened
        self.outcomes.append(success)
        failures = self.outcomes.count(False)
        if len(self.outcomes) >= self.min_calls and failures / len(self.outcomes) >= self.failure_rate:
            self._move(BreakerState.OPEN, now)
