"""Operation-local hardware facts must not rewrite scheduling policy."""

from copy import deepcopy
from dataclasses import replace

import pytest

from sparkrun.core.cluster_manager import ClusterDefinition
from sparkrun.core.hardware import AcceleratorSpec, HostHardware
from sparkrun.core.hardware_observations import apply_hardware_observations, format_hardware_evidence, hardware_evidence
from sparkrun.core.scheduler import RankAssignment, RankSlot
from sparkrun.models.fit import check_fit
from sparkrun.models.vram import VRAMEstimate


def _detected(model="gb10", memory=None, count=1):
    return HostHardware(
        accelerators=[AcceleratorSpec("nvidia", model, count=count, memory_gb=memory, capabilities=frozenset({"cuda", "rdma:roce-v2"}))],
        source="detected",
        driver_versions={"nvidia": "610.43.02"},
        ib_info={"IB_DETECTED": "1", "DETECTED_NET_LIST": "roce0,roce1"},
    )


def test_observations_preserve_physical_slots_and_configured_budgets():
    hardware = HostHardware(
        accelerators=[
            AcceleratorSpec("nvidia", "h100", memory_gb=70, max_gpu_memory_utilization=0.6),
            AcceleratorSpec("nvidia", "h100", memory_gb=60, max_gpu_memory_utilization=0.7),
            AcceleratorSpec("nvidia", "h100", memory_gb=80, max_gpu_memory_utilization=0.8),
        ]
    )
    other = HostHardware()
    cluster = ClusterDefinition(
        name="mixed",
        hosts=["h", "unused"],
        hosts_hardware={"h": hardware, "unused": other},
        max_gpu_memory_utilization=0.9,
        accelerator_memory_limits={"h100": 0.85},
    )
    placement = RankAssignment((RankSlot("h", 2), RankSlot("h", 0)), ("h",))
    observations = {"h": _detected("h100", 80, 3)}
    original = deepcopy(cluster)
    probe_before = deepcopy(observations)
    merged = apply_hardware_observations(cluster, observations, ["h"], placement)

    assert cluster == original
    assert observations == probe_before
    assert merged is not cluster
    assert merged.hosts_hardware["unused"] is other
    assert merged.max_gpu_memory_utilization == 0.9
    assert merged.accelerator_memory_limits == {"h100": 0.85}
    slots = merged.hardware_for("h").accelerators
    assert [s.memory_gb for s in slots] == [70, 60, 80]
    assert [s.max_gpu_memory_utilization for s in slots] == [0.6, 0.7, 0.8]
    evidence = hardware_evidence(merged, "h", placement)
    assert [s["index"] for s in evidence["accelerators"]] == [0, 2]
    assert [s["capacity_verification"] for s in evidence["accelerators"]] == ["inventory", "detected"]
    assert [s["max_gpu_memory_utilization"] for s in evidence["accelerators"]] == [0.6, 0.8]
    assert placement.by_rank == (RankSlot("h", 2), RankSlot("h", 0))
    merged.hardware_for("h").ib_info["IB_DETECTED"] = "0"
    assert observations["h"].ib_info["IB_DETECTED"] == "1"


@pytest.mark.parametrize("capacity", [None, 80])
def test_configured_ceiling_survives_unmeasured_or_larger_device(capacity):
    old = HostHardware(accelerators=[AcceleratorSpec("nvidia", "h100", memory_gb=60)])
    cluster = ClusterDefinition(name="test", hosts=["h"], hosts_hardware={"h": old})
    merged = apply_hardware_observations(cluster, {"h": _detected("h100", capacity)}, ["h"])
    assert hardware_evidence(merged, "h")["accelerators"][0]["capacity_gib"] == 60
    assert hardware_evidence(merged, "h")["accelerators"][0]["capacity_verification"] == "inventory"


def test_assumed_capacity_is_not_promoted_to_measured_and_explicit_limit_survives():
    old = HostHardware(accelerators=[AcceleratorSpec("nvidia", "gb10", memory_gb=121, max_gpu_memory_utilization=0.75)], source="assumed")
    cluster = ClusterDefinition(name="test", hosts=["h"], hosts_hardware={"h": old})
    merged = apply_hardware_observations(cluster, {"h": _detected()}, ["h"])
    assert merged.hardware_for("h").accelerators[0].memory_gb is None
    accel = hardware_evidence(merged, "h")["accelerators"][0]
    assert accel["capacity_verification"] == "estimated"
    assert accel["max_gpu_memory_utilization"] == 0.75


@pytest.mark.parametrize("kind", ["missing-host", "extra-host", "unverified", "missing-gpu", "changed-gpu"])
def test_invalid_observations_fail_without_reassigning_or_mutating_inventory(kind):
    cluster = ClusterDefinition(name="test", hosts=["h"], hosts_hardware={"h": _detected("h100", 80, 2)})
    placement = RankAssignment((RankSlot("h", 1),), ("h",))
    observations = {"h": _detected("h100", 80, 2)}
    if kind == "missing-host":
        observations = {}
    elif kind == "extra-host":
        observations["other"] = _detected()
    elif kind == "unverified":
        observations["h"] = replace(observations["h"], source="assumed")
    elif kind == "missing-gpu":
        observations["h"] = _detected("h100", 80, 1)
    else:
        observations["h"] = _detected("h200", 141, 2)
    before = deepcopy(cluster)
    with pytest.raises(ValueError):
        apply_hardware_observations(cluster, observations, ["h"], placement)
    assert cluster == before
    assert placement.by_rank[0].local_gpu == 1


@pytest.mark.parametrize(("memory", "verification", "status"), [(None, "estimated", "unknown"), (100, "detected", "fits")])
def test_detection_does_not_imply_measured_capacity_or_peer_connectivity(memory, verification, status):
    cluster = ClusterDefinition(name="test", hosts=["h"], max_gpu_memory_utilization=0.8)
    cluster = apply_hardware_observations(cluster, {"h": _detected(memory=memory)}, ["h"])
    evidence = hardware_evidence(cluster, "h")
    accel = evidence["accelerators"][0]
    assert accel["identity_source"] == "detected"
    assert accel["capacity_verification"] == verification
    assert accel["capacity_gib"] == (121 if memory is None else memory)
    assert evidence["drivers"]["nvidia"] == {"version": "610.43.02", "source": "detected"}
    assert evidence["interfaces"] == {"names": ["roce0", "roce1"], "status": "discovered", "peer_connectivity": "not tested"}
    message = format_hardware_evidence(evidence)
    assert verification in message
    assert "610.43.02 detected" in message
    assert "peer connectivity not tested" in message

    estimate = VRAMEstimate(
        model_weights_gb=50, kv_cache_per_token_bytes=None, kv_cache_total_gb=10, total_per_gpu_gb=60, max_model_len=None, tensor_parallel=1
    )
    fit = check_fit(estimate, cluster)
    assert fit.ok
    assert fit.per_host["h"].status == status
    assert fit.to_dict()["per_host"]["h"]["memory_capacity_verification"] == verification
    assert not check_fit(replace(estimate, total_per_gpu_gb=200), cluster).ok


def test_explicit_device_limit_survives_correction_of_assumed_gpu_model():
    old = HostHardware(
        accelerators=[
            AcceleratorSpec(
                "nvidia", "gb10", memory_gb=121, max_gpu_memory_utilization=0.75, memory_limit_source="runtime gpu_memory_utilization"
            )
        ],
        source="assumed",
    )
    cluster = ClusterDefinition(name="test", hosts=["h"], hosts_hardware={"h": old})
    observed = {"h": _detected("h100", 80)}
    merged = apply_hardware_observations(cluster, observed, ["h"])
    assert apply_hardware_observations(merged, observed, ["h"]) == merged
    accel = hardware_evidence(merged, "h")["accelerators"][0]
    assert accel["model"] == "h100"
    assert accel["capacity_gib"] == 80
    assert accel["max_gpu_memory_utilization"] == 0.75
    assert accel["budget_source"] == "runtime gpu_memory_utilization"
