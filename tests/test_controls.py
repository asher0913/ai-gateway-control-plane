import pytest

from aigw.controls import (
    BreakerState,
    BudgetExceeded,
    BudgetLedger,
    CircuitBreaker,
    RateLimiter,
    TenantLimits,
    TokenBucket,
)


def test_token_bucket_burst_and_refill():
    bucket = TokenBucket(capacity=10, rate=2, now=0)
    assert bucket.try_take(10, 0)
    assert not bucket.try_take(1, 0)
    assert bucket.try_take(4, 2.0)  # refilled 4 tokens in 2 s
    assert not bucket.try_take(1, 2.0)
    bucket.adjust(100, 2.0)
    assert bucket.tokens == 10  # refunds never exceed capacity


def test_rate_limiter_checks_concurrency_then_rates():
    limiter = RateLimiter({"a": TenantLimits(requests_per_minute=60, tokens_per_minute=6000, max_concurrency=2)})
    assert limiter.admit("a", 100, 0) is None
    assert limiter.admit("a", 100, 0) is None
    assert limiter.admit("a", 100, 0) == "concurrency"
    limiter.settle("a", 100, 50, 0)
    assert limiter.in_flight["a"] == 1
    assert limiter.admit("ghost", 1, 0) == "unknown_tenant"


def test_token_rejection_returns_the_request_slot():
    limiter = RateLimiter({"a": TenantLimits(requests_per_minute=60, tokens_per_minute=600, max_concurrency=10)})
    before = limiter.requests["a"].tokens
    assert limiter.admit("a", 10_000, 0) == "token_rate"
    assert limiter.requests["a"].tokens == before
    assert limiter.in_flight["a"] == 0


def test_settlement_refunds_overestimates():
    limiter = RateLimiter({"a": TenantLimits(60, 1200, 5)})  # 200-token burst
    assert limiter.admit("a", 200, 0) is None
    assert limiter.admit("a", 1, 0) == "token_rate"
    limiter.settle("a", estimated_tokens=200, actual_tokens=20, now=0)
    assert limiter.admit("a", 150, 0) is None


def test_budget_reservations_prevent_overspend():
    ledger = BudgetLedger({"a": 1.0})
    ledger.reserve("a", 0.6)
    with pytest.raises(BudgetExceeded):
        ledger.reserve("a", 0.5)  # would exceed while the first call is in flight
    assert ledger.commit("a", reserved=0.6, actual=0.2) == pytest.approx(0.2)
    ledger.reserve("a", 0.5)
    ledger.release("a", 0.5)
    assert ledger.available("a") == pytest.approx(0.8)
    ledger.reserve("a", 0.1)
    assert ledger.commit("a", reserved=0.1, actual=5.0) == pytest.approx(0.1)  # never charges beyond the hold


def breaker(**kw) -> CircuitBreaker:
    return CircuitBreaker(**{"window": 10, "min_calls": 4, "failure_rate": 0.5, "cooldown": 10, "probes": 2, **kw})


def test_breaker_needs_minimum_calls_before_opening():
    b = breaker()
    for t in range(3):
        b.record(False, t)
    assert b.state is BreakerState.CLOSED
    b.record(False, 3)
    assert b.state is BreakerState.OPEN
    assert not b.allow(5)


def test_breaker_half_open_admits_limited_probes_then_closes():
    b = breaker()
    for t in range(4):
        b.record(False, t)
    assert b.allow(13.5)  # cooldown elapsed -> half-open, probe 1
    assert b.state is BreakerState.HALF_OPEN
    assert b.allow(13.6)  # probe 2
    assert not b.allow(13.7)  # no more probes until results arrive
    b.record(True, 14)
    b.record(True, 15)
    assert b.state is BreakerState.CLOSED
    assert [s for _, s in b.transitions] == ["open", "half_open", "closed"]


def test_failed_probe_reopens_and_restarts_cooldown():
    b = breaker()
    for t in range(4):
        b.record(False, t)
    assert b.allow(20)
    b.record(False, 21)
    assert b.state is BreakerState.OPEN
    assert not b.allow(25)
    assert b.allow(31.5)


def test_late_results_while_open_are_ignored():
    b = breaker()
    for t in range(4):
        b.record(False, t)
    b.record(True, 5)
    assert b.state is BreakerState.OPEN
