import pytest

from aigw.registry import AuditLog, PromptRegistry, stable_fraction


def registry() -> PromptRegistry:
    r = PromptRegistry()
    r.publish("assistant", "v1", "one")
    r.publish("assistant", "v2", "two")
    return r


def test_publish_is_idempotent_but_immutable():
    r = registry()
    assert r.publish("assistant", "v1", "one").digest == r.versions[("assistant", "v1")].digest
    with pytest.raises(ValueError):
        r.publish("assistant", "v1", "changed")


def test_canary_split_is_close_to_target_and_sticky():
    r = registry()
    r.start_canary("assistant", "v2", 0.1)
    picks = [r.resolve("assistant", f"req-{i}").version for i in range(20_000)]
    share = picks.count("v2") / len(picks)
    assert 0.09 < share < 0.11
    assert all(r.resolve("assistant", f"req-{i}").version == picks[i] for i in range(200))


def test_promote_and_rollback():
    r = registry()
    r.start_canary("assistant", "v2", 0.5)
    r.promote("assistant")
    assert r.resolve("assistant", "x").version == "v2"
    assert r.rollback("assistant") == "v1"
    assert r.resolve("assistant", "x").version == "v1"
    with pytest.raises(ValueError):
        r.rollback("assistant")


def test_pinned_version_overrides_rollout():
    r = registry()
    assert r.resolve("assistant", "x", pinned="v2").version == "v2"


def test_stable_fraction_is_deterministic():
    assert stable_fraction("abc") == stable_fraction("abc")
    assert 0 <= stable_fraction("abc") < 1


def test_audit_chain_detects_edits_and_reordering():
    log = AuditLog()
    for i in range(5):
        log.append({"i": i, "status": "ok"})
    assert log.verify() is None
    log.records[2]["event"]["status"] = "failed"
    assert log.verify() == 2
    log.records[2]["event"]["status"] = "ok"
    log.records[1], log.records[3] = log.records[3], log.records[1]
    assert log.verify() == 1
