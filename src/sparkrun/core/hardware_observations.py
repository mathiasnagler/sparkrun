"""Reuse operation-local hardware observations without rewriting launch policy."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Mapping, Sequence

from sparkrun.core.hardware import HostHardware
from sparkrun.core.limits import resolve_accelerator_memory, resolve_memory_limit

if TYPE_CHECKING:
    from sparkrun.core.cluster_manager import ClusterDefinition
    from sparkrun.core.scheduler import RankAssignment


def apply_hardware_observations(
    cluster: ClusterDefinition,
    observations: Mapping[str, HostHardware],
    hosts: Sequence[str],
    placement: RankAssignment | None = None,
) -> ClusterDefinition:
    """Return an ephemeral cluster with detected facts and original policy.

    Physical GPU indices and placement are never remapped. Configured capacity
    ceilings and utilization limits stay attached to the same device; a probe
    cannot increase a configured budget. Unmeasured capacity stays unmeasured
    unless an explicit inventory value or platform default supplies it.
    """
    if set(observations) != set(hosts):
        raise ValueError("Hardware observations must cover exactly the launch hosts")
    inventory = dict(cluster.hosts_hardware)
    for host in hosts:
        observed = observations[host]
        if observed.source != "detected":
            raise ValueError("Hardware observation for %s must come from detection" % host)
        slots = [a for a in observed.accelerators for _ in range(a.count)]
        assigned = (
            {placement.local_gpu_for_rank(rank) for rank in placement.ranks_on_host(host)}
            if placement is not None
            else set(range(len(slots)))
        )
        if any(index < 0 or index >= len(slots) for index in assigned):
            raise ValueError("Assigned GPU index is outside detected hardware on %s" % host)
        configured = cluster.hosts_hardware.get(host)
        old_slots = [a for a in configured.accelerators for _ in range(a.count)] if configured is not None else []
        merged = []
        for index, detected in enumerate(slots):
            # Observations supply facts, never memory policy.
            accel = replace(detected, count=1, max_gpu_memory_utilization=None, memory_limit_source=None)
            if configured is not None and index < len(old_slots):
                old = old_slots[index]
                same_device = (old.vendor, old.model) == (detected.vendor, detected.model)
                if not same_device and index in assigned and configured.source != "assumed":
                    raise ValueError(
                        "Detected GPU %d on %s differs from configured inventory; refresh inventory before launching" % (index, host)
                    )
                if same_device or configured.source == "assumed":
                    capacity, source = detected.memory_gb, detected.memory_capacity_source
                    if configured.source != "assumed" and old.memory_gb is not None and (capacity is None or old.memory_gb < capacity):
                        capacity, source = old.memory_gb, old.memory_capacity_source or configured.source
                    accel = replace(
                        accel,
                        memory_gb=capacity,
                        memory_capacity_source=source,
                        max_gpu_memory_utilization=old.max_gpu_memory_utilization,
                        memory_limit_source=old.memory_limit_source,
                    )
            merged.append(accel)
        inventory[host] = replace(
            observed, accelerators=merged, ib_info=deepcopy(observed.ib_info), driver_versions=dict(observed.driver_versions)
        )
    return replace(cluster, hosts_hardware=inventory)


def capacity_verification(capacity: float | None, source: str | None) -> str:
    if capacity is None:
        return "unknown"
    if source and ("platform" in source or "assumed" in source):
        return "estimated"
    return "detected" if source == "detected" else "inventory"


def hardware_evidence(cluster: ClusterDefinition, host: str, placement: RankAssignment | None = None) -> dict:
    """Describe each fact without promoting discovery to connectivity proof."""
    hardware = cluster.hardware_for(host)
    slots = [a for a in hardware.accelerators for _ in range(a.count)]
    indices = (
        sorted({placement.local_gpu_for_rank(rank) for rank in placement.ranks_on_host(host)})
        if placement is not None
        else list(range(len(slots)))
    )
    accelerators = []
    for index in indices:
        accel = slots[index]
        capacity, source = resolve_accelerator_memory(accel, hardware)
        limit, limit_source = resolve_memory_limit(accel, hardware, cluster)
        accelerators.append(
            {
                "index": index,
                "vendor": accel.vendor,
                "model": accel.model,
                "identity_source": hardware.source,
                "capacity_gib": capacity,
                "capacity_source": source,
                "capacity_verification": capacity_verification(capacity, source),
                "max_gpu_memory_utilization": limit,
                "budget_source": limit_source,
            }
        )
    interfaces = hardware.ib_info
    names = [name.strip() for name in (interfaces or {}).get("DETECTED_NET_LIST", "").split(",") if name.strip()]
    return {
        "accelerators": accelerators,
        "drivers": {vendor: {"version": version, "source": hardware.source} for vendor, version in hardware.driver_versions.items()},
        "interfaces": {
            "names": names,
            "status": "discovered" if names else "not discovered" if interfaces is not None else "unknown",
            "peer_connectivity": "not tested",
        },
    }


def format_hardware_evidence(evidence: dict) -> str:
    parts = []
    for accel in evidence["accelerators"]:
        capacity = "unknown" if accel["capacity_gib"] is None else "%.1f GiB" % accel["capacity_gib"]
        parts.append(
            "GPU %d %s/%s %s; capacity %s %s (%s)"
            % (
                accel["index"],
                accel["vendor"],
                accel["model"],
                accel["identity_source"],
                capacity,
                accel["capacity_verification"],
                accel["capacity_source"],
            )
        )
    for vendor, driver in evidence["drivers"].items():
        parts.append("%s driver %s %s" % (vendor, driver["version"], driver["source"]))
    if not evidence["drivers"]:
        parts.append("driver version unknown")
    interfaces = evidence["interfaces"]
    parts.append(
        "RDMA interfaces %s%s; peer connectivity not tested"
        % (
            interfaces["status"],
            " (" + ", ".join(interfaces["names"]) + ")" if interfaces["names"] else "",
        )
    )
    return "; ".join(parts)
