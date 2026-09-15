"""RDMA test coverage from discovered inventory and authored cluster intent.

Subnet membership supplies candidates, never proof of reachability. A declared
switch additionally requires every selected host on each observed fabric subnet.
No hardware names, interface prefixes or host ordering imply a topology.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from sparkrun.core.setup_models import OK, WARN, FAIL, SKIP
from sparkrun.orchestration.networking import CX7HostDetection, filter_cx7_interfaces
from sparkrun.orchestration.rdma import RdmaHostFacts, RdmaPair, derive_link_pairs

if TYPE_CHECKING:
    from sparkrun.core.cluster_manager import ClusterDefinition


@dataclass(frozen=True)
class CoverageIssue:
    status: str
    detail: str


@dataclass(frozen=True)
class RdmaCoverage:
    """Inventory coverage, separate from measured transfer verdicts.

    Expected counts are known only for an authored switch and cover observed
    subnets; a subnet absent from every host cannot be recovered by discovery.
    """

    discovered: bool = False
    topology: str | None = None
    fabric_interfaces: tuple[str, ...] = ()
    subnets: dict[str, tuple[str, ...]] = field(default_factory=dict)
    components: tuple[tuple[str, ...], ...] = ()
    uncovered_hosts: tuple[str, ...] = ()
    pair_count: int = 0
    path_count: int = 0
    expected_pair_count: int | None = None
    expected_path_count: int | None = None
    issues: tuple[CoverageIssue, ...] = ()

    @property
    def status(self) -> str:
        if not self.discovered:
            return SKIP
        if any(i.status == FAIL for i in self.issues):
            return FAIL
        return WARN if self.issues else OK


def plan_rdma_tests(
    hosts: list[str],
    detections: dict[str, CX7HostDetection],
    facts: dict[str, RdmaHostFacts],
    *,
    cluster: ClusterDefinition | None = None,
    dry_run: bool = False,
) -> tuple[list[RdmaPair], RdmaCoverage]:
    """Build all candidate pairs and diagnose incomplete/ambiguous coverage.

    Probe failures and coverage errors prevent the runner from starting tests.
    Unused, unaddressed ports do not participate. For a selected subset of a
    ring, only connectivity within that subset can be checked, not ring closure.
    """
    topology = cluster.topology if cluster else None
    patterns = cluster.fabric_interfaces if cluster else []
    full_cluster = cluster is not None and set(hosts) == set(cluster.hosts)
    issues: list[CoverageIssue] = []
    selected = {}
    subnets: dict[str, list[str]] = {}
    addresses: dict[str, tuple[str, str]] = {}

    def issue(status: str, detail: str) -> None:
        if not dry_run:
            issues.append(CoverageIssue(status, detail))

    for host in hosts:
        f = facts.get(host)
        if f is None or f.error or not f.complete:
            issue(FAIL, "%s: RDMA probe incomplete%s" % (host, ": " + f.error if f and f.error else ""))
        det = detections.get(host)
        if det is None or not det.detected:
            issue(FAIL, "%s: no RDMA interface discovery result" % host)
            continue
        ifaces = filter_cx7_interfaces(det.interfaces, patterns)
        selected[host] = replace(det, interfaces=ifaces)
        configured = [i for i in ifaces if i.ip and i.subnet and i.hca]
        if not configured:
            issue(FAIL, "%s: no configured RDMA interfaces%s" % (host, " matching fabric_interfaces" if patterns else ""))
        for iface in ifaces:
            if iface.ip and not (iface.subnet and iface.hca):
                issue(FAIL, "%s:%s: addressed interface has no subnet or RDMA device" % (host, iface.name))
        for iface in configured:
            members = subnets.setdefault(iface.subnet, [])
            if host not in members:
                members.append(host)
            endpoint = (host, iface.name)
            if iface.ip in addresses and addresses[iface.ip] != endpoint:
                issue(FAIL, "Duplicate RDMA address %s on %s:%s and %s:%s" % (iface.ip, *addresses[iface.ip], *endpoint))
            addresses[iface.ip] = endpoint
            dev = f.device(iface.hca) if f else None
            if dev is None or not dev.active:
                issue(FAIL, "%s:%s: configured RDMA device is missing or inactive" % (host, iface.hca))

    try:
        pairs = derive_link_pairs(selected, hosts)
    except ValueError as exc:
        issue(FAIL, str(exc))
        pairs = []

    neighbors: dict[str, set[str]] = {h: set() for h in hosts}
    for pair in pairs:
        neighbors[pair.host_a].add(pair.host_b)
        neighbors[pair.host_b].add(pair.host_a)
    uncovered = tuple(h for h in hosts if not neighbors[h])
    if uncovered:
        issue(FAIL, "No candidate RDMA path for: %s" % ", ".join(uncovered))
    components: list[tuple[str, ...]] = []
    remaining = set(hosts)
    for host in hosts:
        if host not in remaining:
            continue
        reached, pending = set(), [host]
        while pending:
            node = pending.pop()
            if node in reached:
                continue
            reached.add(node)
            pending.extend(neighbors[node] - reached)
        remaining -= reached
        components.append(tuple(h for h in hosts if h in reached))
    if len(components) > 1:
        issue(FAIL, "Selected fabric is disconnected: %s" % "; ".join(", ".join(c) for c in components))
    if not pairs:
        issue(FAIL, "No RDMA paths found; configure the high-speed fabric with: sparkrun setup cx7")

    if topology not in (None, "switch", "direct", "ring"):
        issue(FAIL, "Unsupported cluster topology: %s" % topology)
    if topology == "direct" and len(hosts) != 2:
        issue(FAIL, "Direct topology requires exactly two selected hosts")
    for subnet, members in sorted(subnets.items()):
        if topology == "switch":
            missing = [h for h in hosts if h not in members]
            if missing:
                issue(FAIL, "%s: switch subnet is missing hosts: %s" % (subnet, ", ".join(missing)))
        elif topology in ("direct", "ring") and len(members) > 2:
            issue(FAIL, "%s: %s topology expects point-to-point subnets; found %d hosts" % (subnet, topology, len(members)))
        elif len(members) == 1:
            status = FAIL if topology == "direct" or (topology == "ring" and full_cluster) else WARN
            issue(status, "%s: no selected peer for %s (path untested)" % (subnet, members[0]))
    if topology == "ring" and full_cluster:
        for host in hosts:
            if len(neighbors[host]) != 2:
                issue(FAIL, "%s: ring requires two distinct neighbors; found %d" % (host, len(neighbors[host])))

    expected_pairs = len(hosts) * (len(hosts) - 1) // 2 if topology == "switch" else None
    return pairs, RdmaCoverage(
        discovered=not dry_run,
        topology=topology,
        fabric_interfaces=tuple(patterns),
        subnets={subnet: tuple(members) for subnet, members in sorted(subnets.items())},
        components=tuple(components) if not dry_run else (),
        uncovered_hosts=uncovered if not dry_run else (),
        pair_count=len(pairs),
        path_count=sum(len(p.links) for p in pairs),
        expected_pair_count=expected_pairs,
        expected_path_count=expected_pairs * len(subnets) if expected_pairs is not None and not dry_run else None,
        issues=tuple(issues),
    )
