"""Prompt versions with canary rollout, and a tamper-evident audit log."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


def stable_fraction(key: str) -> float:
    """Map a string to [0, 1) deterministically, independent of process or host."""
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


@dataclass(frozen=True)
class PromptVersion:
    name: str
    version: str
    template: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.template.encode()).hexdigest()[:12]


@dataclass
class _Rollout:
    stable: str
    canary: str | None = None
    canary_fraction: float = 0.0
    history: list[str] = field(default_factory=list)


class PromptRegistry:
    """Immutable prompt versions, a stable pointer, and an optional weighted canary.

    A request id is hashed to decide canary membership, so the same caller
    sees the same version on every retry and on every gateway replica.
    """

    def __init__(self) -> None:
        self.versions: dict[tuple[str, str], PromptVersion] = {}
        self.rollouts: dict[str, _Rollout] = {}

    def publish(self, name: str, version: str, template: str) -> PromptVersion:
        key = (name, version)
        if key in self.versions:
            if self.versions[key].template != template:
                raise ValueError(f"{name}@{version} already exists with different content")
            return self.versions[key]
        prompt = PromptVersion(name, version, template)
        self.versions[key] = prompt
        self.rollouts.setdefault(name, _Rollout(stable=version))
        return prompt

    def start_canary(self, name: str, version: str, fraction: float) -> None:
        if (name, version) not in self.versions:
            raise KeyError(f"{name}@{version}")
        if not 0 < fraction < 1:
            raise ValueError("canary fraction must be in (0, 1)")
        rollout = self.rollouts[name]
        rollout.canary, rollout.canary_fraction = version, fraction

    def promote(self, name: str) -> None:
        rollout = self.rollouts[name]
        if rollout.canary is None:
            raise ValueError("no canary to promote")
        rollout.history.append(rollout.stable)
        rollout.stable, rollout.canary, rollout.canary_fraction = rollout.canary, None, 0.0

    def abort_canary(self, name: str) -> None:
        rollout = self.rollouts[name]
        rollout.canary, rollout.canary_fraction = None, 0.0

    def rollback(self, name: str) -> str:
        rollout = self.rollouts[name]
        if not rollout.history:
            raise ValueError("nothing to roll back to")
        rollout.canary, rollout.canary_fraction = None, 0.0
        rollout.stable = rollout.history.pop()
        return rollout.stable

    def resolve(self, name: str, request_id: str, pinned: str | None = None) -> PromptVersion:
        if pinned is not None:
            return self.versions[(name, pinned)]
        rollout = self.rollouts[name]
        if rollout.canary and stable_fraction(f"{name}:{request_id}") < rollout.canary_fraction:
            return self.versions[(name, rollout.canary)]
        return self.versions[(name, rollout.stable)]


class AuditLog:
    """Append-only log where each record commits to the previous record's hash.

    Editing, deleting or reordering any record breaks every later hash, which
    :meth:`verify` detects. (Truncating the tail is detectable only if the
    latest head hash is stored elsewhere.)
    """

    GENESIS = "0" * 64

    def __init__(self) -> None:
        self.records: list[dict] = []

    @property
    def head(self) -> str:
        return self.records[-1]["hash"] if self.records else self.GENESIS

    def append(self, event: dict) -> dict:
        body = json.dumps(event, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256((self.head + body).encode()).hexdigest()
        record = {"seq": len(self.records), "event": event, "prev": self.head, "hash": digest}
        self.records.append(record)
        return record

    def verify(self) -> int | None:
        """Index of the first corrupted record, or ``None`` if the chain is intact."""
        previous = self.GENESIS
        for index, record in enumerate(self.records):
            body = json.dumps(record["event"], sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256((previous + body).encode()).hexdigest()
            if record["prev"] != previous or record["hash"] != expected or record["seq"] != index:
                return index
            previous = record["hash"]
        return None
