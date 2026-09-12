"""Recorded liveness with explicit executor/destination coverage.

A missing observation is unknown, never proof of absence. Coverage is kept per
executor so a successful Docker query cannot mask a failed local-process query.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sparkrun.orchestration.executor import ExecutorTarget


@dataclass(frozen=True)
class ExecutorCoverage:
    target: ExecutorTarget
    status_scope: str
    requested_hosts: frozenset[str]
    hosts: frozenset[str]

    def __post_init__(self):
        object.__setattr__(self, "requested_hosts", frozenset(self.requested_hosts))
        object.__setattr__(self, "hosts", frozenset(self.hosts))
        if not self.hosts <= self.requested_hosts:
            raise ValueError("Covered hosts must have been requested")

    def matches(self, target: ExecutorTarget) -> bool:
        return self.target.executor == target.executor and self.target.destination_key == target.destination_key

    def matches_job(self, job) -> bool:
        metadata = job.metadata
        executor = metadata.get("executor")
        if not executor:
            return False  # legacy records without a selector have unknown coverage
        key = metadata.get("executor_destination_key")
        if key is None:
            return False  # no durable destination evidence, including legacy custom local paths
        return executor == self.target.executor and key == self.target.destination_key

    def covers_job(self, job) -> bool:
        hosts = frozenset(job.hosts)
        return bool(hosts) and hosts <= self.hosts and self.matches_job(job)

    def to_dict(self):
        return {
            "target": self.target.to_dict(),
            "status_scope": self.status_scope,
            "requested_hosts": sorted(self.requested_hosts),
            "hosts": sorted(self.hosts),
        }

    @classmethod
    def from_dict(cls, data):
        from sparkrun.orchestration.executor import ExecutorTarget

        def strings(key):
            values = data[key]
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError("Invalid observation hosts")
            return frozenset(values)

        target = ExecutorTarget(**data["target"])
        scope = data["status_scope"]
        hosts, requested = strings("hosts"), strings("requested_hosts")
        if not isinstance(scope, str) or not hosts <= requested:
            raise ValueError("Invalid observation coverage")
        return cls(target, scope, requested, hosts)


@dataclass(frozen=True)
class RunningSnapshot:
    cluster_ids: frozenset[str]
    coverage: tuple[ExecutorCoverage, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "cluster_ids", frozenset(self.cluster_ids))
        object.__setattr__(self, "coverage", tuple(self.coverage))

    def for_target(self, target: ExecutorTarget) -> bool:
        """Even an incomplete observation must name the requested destination."""
        return any(c.matches(target) for c in self.coverage)

    def covers(self, target: ExecutorTarget, hosts) -> bool:
        return bool(hosts) and any(c.matches(target) and set(hosts) <= c.hosts for c in self.coverage)

    def matches_job(self, job) -> bool:
        return any(c.matches_job(job) for c in self.coverage)

    def confirms_absent(self, job) -> bool:
        return job.cluster_id not in self.cluster_ids and any(c.covers_job(job) for c in self.coverage)

    def to_dict(self):
        return {"version": 1, "cluster_ids": sorted(self.cluster_ids), "coverage": [c.to_dict() for c in self.coverage]}

    @classmethod
    def from_dict(cls, data):
        if data.get("version") != 1 or not isinstance(data.get("cluster_ids"), list):
            raise ValueError("Unknown running snapshot format")
        if any(not isinstance(cid, str) for cid in data["cluster_ids"]):
            raise ValueError("Invalid running snapshot IDs")
        return cls(frozenset(data["cluster_ids"]), tuple(ExecutorCoverage.from_dict(c) for c in data["coverage"]))
