"""Scoped status observations for tests that replace the executor query boundary."""

from sparkrun.core.cluster_status import ClusterStatus
from sparkrun.core.status_observation import ExecutorCoverage
from sparkrun.orchestration.executor import ExecutorTarget


def host_coverage(hosts, *, requested_hosts=None, executor="docker"):
    return (ExecutorCoverage(ExecutorTarget(executor), "host", frozenset(requested_hosts or hosts), frozenset(hosts)),)


def observed_status(**kwargs):
    hosts = {entry.host for entry in kwargs.get("hosts", ())}
    errors = kwargs.get("errors", {})
    return ClusterStatus(**kwargs, coverage=host_coverage(hosts - errors.keys(), requested_hosts=hosts | errors.keys()))


def host_snapshot(cluster_ids, hosts):
    from sparkrun.core.status_observation import RunningSnapshot

    return RunningSnapshot(frozenset(cluster_ids), host_coverage(hosts))
