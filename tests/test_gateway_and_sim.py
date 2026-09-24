import pytest

from aigw.controls import BudgetLedger, RateLimiter, TenantLimits
from aigw.gateway import Attempt, Endpoint, Final, Gateway, LatencyEstimator, Outcome, Policy, Request
from aigw.registry import PromptRegistry
from aigw.sim import POLICIES, compare, reference_scenario, run, traffic

CHAT = frozenset({"chat"})
ENDPOINTS = [Endpoint("cheap", CHAT, 1, 1, 1.0), Endpoint("pricey", CHAT, 10, 10, 1.0)]


def gateway(policy: Policy, budget: float = 10.0) -> Gateway:
    prompts = PromptRegistry()
    prompts.publish("p", "v1", "hello")
    limiter = RateLimiter({"t": TenantLimits(6000, 10_000_000, 100)})
    return Gateway(ENDPOINTS, limiter, BudgetLedger({"t": budget}), prompts, policy)


def req(i: int = 0) -> Request:
    return Request(f"r{i}", "t", "chat", "p", input_tokens=1000, max_output_tokens=1000)


def test_cheapest_capable_endpoint_is_preferred():
    step = gateway(Policy("x")).on_arrival(req(), 0)
    assert isinstance(step, Attempt) and step.endpoint.name == "cheap"


def test_fallback_moves_to_a_different_endpoint():
    gw = gateway(Policy("x", max_attempts=2, fallback=True))
    first = gw.on_arrival(req(), 0)
    second = gw.on_attempt_done(first, Outcome(False, 1.0), 1.0)
    assert isinstance(second, Attempt) and second.endpoint.name == "pricey"
    final = gw.on_attempt_done(second, Outcome(True, 1.0, 500), 2.4)
    assert final.status == "ok" and final.attempts == 2


def test_without_fallback_retries_stay_on_the_same_endpoint():
    gw = gateway(Policy("x", max_attempts=2))
    first = gw.on_arrival(req(), 0)
    assert gw.on_attempt_done(first, Outcome(False, 1.0), 1.0).endpoint.name == "cheap"


def test_open_breaker_skips_endpoint_up_front():
    gw = gateway(Policy("x", max_attempts=2, fallback=True, breaker=True))
    for t in range(20):
        gw.breakers["cheap"].record(False, t)
    step = gw.on_arrival(req(), 21)
    assert step.endpoint.name == "pricey"


def test_budget_reserves_worst_case_and_charges_actual():
    gw = gateway(Policy("x"), budget=0.05)
    step = gw.on_arrival(req(), 0)
    assert step.reserved_usd == pytest.approx(ENDPOINTS[1].cost(1000, 1000))  # pricey worst case: 0.02
    final = gw.on_attempt_done(step, Outcome(True, 1.0, 100), 1.0)
    assert final.cost_usd == pytest.approx(ENDPOINTS[0].cost(1000, 100))
    assert gw.ledger.reserved["t"] == pytest.approx(0.0)


def test_budget_exhaustion_rejects():
    gw = gateway(Policy("x"), budget=0.03)
    assert isinstance(gw.on_arrival(req(0), 0), Attempt)
    rejected = gw.on_arrival(req(1), 0)
    assert isinstance(rejected, Final) and rejected.status == "rejected:budget"


def test_latency_estimate_decays_back_to_prior():
    est = LatencyEstimator([ENDPOINTS[0]], alpha=1.0, half_life_s=10)
    est.observe("cheap", 9.0, now=0)
    assert est.estimate("cheap", 0) == pytest.approx(9.0)
    assert est.estimate("cheap", 10) == pytest.approx(5.0)
    assert est.estimate("cheap", 1000) == pytest.approx(1.0, abs=1e-6)


def test_sync_handle_matches_event_api():
    gw = gateway(Policy("x", max_attempts=2, fallback=True))
    outcomes = iter([Outcome(False, 0.5), Outcome(True, 0.5, 10)])
    final = gw.handle(req(), lambda attempt: next(outcomes), clock=lambda: 0.0)
    assert final.status == "ok" and final.endpoint == "pricey"


@pytest.fixture(scope="module")
def report():
    return compare()


def test_simulation_is_deterministic():
    scenario = reference_scenario()
    requests = traffic(scenario)[:2000]
    assert run(scenario, POLICIES[-1], requests) == run(scenario, POLICIES[-1], requests)


def test_resilience_features_rank_as_expected(report):
    by_name = {p["policy"]: p for p in report["policies"]}
    single, retries, fallback, breaker, aware = (by_name[p.name] for p in POLICIES)
    assert single["success_rate_during"]["outage"] == 0.0
    assert retries["success_rate"] > single["success_rate"]
    assert fallback["success_rate"] > 0.99 and aware["success_rate"] > 0.99
    assert breaker["failed_attempts_by_endpoint"]["primary"] < fallback["failed_attempts_by_endpoint"]["primary"] / 3
    assert aware["latency_p99_s"] < breaker["latency_p99_s"] / 2
    assert retries["latency_p99_s"] > single["latency_p99_s"]  # retries amplify the tail


def test_invariants_hold_under_every_policy(report):
    for p in report["policies"]:
        assert p["budget_invariant_held"]
        assert p["audit_chain_intact"] and p["audit_records"] == p["offered"]
        # the noisy tenant is contained without starving the others
        assert p["tenants"]["batch"]["rate_limited"] > 0.4 * p["tenants"]["batch"]["offered"]
        assert p["tenants"]["search"]["rate_limited"] < 0.01 * p["tenants"]["search"]["offered"]
        versions = p["prompt_versions"]
        assert 0.07 < versions["v2"] / (versions["v1"] + versions["v2"]) < 0.13
