"""Usable-memory cap resolution for scheduling and fit.

Resolves a per-accelerator ``max_gpu_memory_utilization`` — the fraction of
nominal accelerator capacity (inventory, then qualified platform default) treated as usable for **scheduling /
fit** decisions (``usable = memory_gb × cap``).  This is the *memory* axis and
is distinct from the scheduler's compute ``util_fraction``; it does not affect
the serving ``--gpu-memory-utilization`` flag.

Resolution precedence (highest first):

1. ``AcceleratorSpec.max_gpu_memory_utilization`` — per-host+accelerator
2. ``cluster.accelerator_memory_limits[accel.model]`` — per-accelerator-type
3. ``cluster.max_gpu_memory_utilization`` — cluster-wide default
4. ``platform.default_max_gpu_memory_utilization(accel)`` — platform default
   (e.g. DGX Spark GB10 → 0.90)
5. :data:`~sparkrun.core.hardware.DEFAULT_MAX_GPU_MEMORY_UTILIZATION` (``1.0``)

Only values in ``(0.0, 1.0]`` are accepted at each level; anything else is
ignored and resolution falls through to the next tier.
"""

from __future__ import annotations

import dataclasses
import logging
import math
from typing import TYPE_CHECKING

from sparkrun.core.hardware import (
    DEFAULT_MAX_GPU_MEMORY_UTILIZATION,
    AcceleratorSpec,
    HostHardware,
    resolve_hardware,
)

if TYPE_CHECKING:
    from sparkrun.core.cluster_manager import ClusterDefinition

logger = logging.getLogger(__name__)


def _valid_fraction(value: float | None) -> float | None:
    """Return *value* if it is a usable fraction in ``(0.0, 1.0]``, else ``None``."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 < f <= 1.0:
        return f
    logger.warning("Ignoring out-of-range max_gpu_memory_utilization=%r (must be 0.0 < x <= 1.0)", value)
    return None


def resolve_max_gpu_memory_utilization(
    accel: AcceleratorSpec,
    host_hw: HostHardware,
    cluster: "ClusterDefinition | None",
) -> float:
    """Resolve the usable-memory cap for *accel* on *host_hw*.

    See the module docstring for the precedence chain.  Always returns a
    concrete fraction in ``(0.0, 1.0]`` (``1.0`` when nothing applies).
    """
    return resolve_memory_limit(accel, host_hw, cluster)[0]


def resolve_memory_limit(accel: AcceleratorSpec, host_hw: HostHardware, cluster: "ClusterDefinition | None") -> tuple[float, str]:
    """The scheduling fraction and its provenance, resolved by the same chain."""
    # 1. Per-host+accelerator (explicit on the spec).
    explicit = _valid_fraction(accel.max_gpu_memory_utilization)
    if explicit is not None:
        return explicit, accel.memory_limit_source or "accelerator override"

    if cluster is not None:
        # 2. Per-accelerator-type map.
        per_type = _valid_fraction(cluster.accelerator_memory_limits.get(accel.model))
        if per_type is not None:
            return per_type, "cluster accelerator-type override"
        # 3. Cluster-wide default.
        cluster_wide = _valid_fraction(cluster.max_gpu_memory_utilization)
        if cluster_wide is not None:
            return cluster_wide, "cluster override"

    # 4. Platform default.
    platform_default = _resolve_platform_default(accel, host_hw)
    if platform_default is not None:
        return platform_default, "platform default"

    # 5. Hard fallback.
    return DEFAULT_MAX_GPU_MEMORY_UTILIZATION, "default capacity"


def _resolve_platform_default(accel: AcceleratorSpec, host_hw: HostHardware) -> float | None:
    """Platform-tier default for *accel*, or ``None`` when no platform claims the host."""
    # Lazy import to avoid a core -> platforms import cycle (platforms imports
    # core.hardware at module load).
    from sparkrun.platforms import resolve_accelerator_platform

    platform = resolve_accelerator_platform(accel, host_hw)
    if platform is None:
        return None
    value = platform.default_max_gpu_memory_utilization(accel)
    if value is None:
        return None
    fraction = _valid_fraction(value)
    if isinstance(value, bool) or fraction is None:
        raise ValueError("Platform %r supplied an invalid scheduling memory fraction" % platform.platform_name)
    return fraction


def resolve_accelerator_memory(accel: AcceleratorSpec, host_hw: HostHardware) -> tuple[float | None, str]:
    """Resolve capacity and its provenance without modifying inventory."""
    capacity = accel.memory_gb
    source = accel.memory_capacity_source or host_hw.source
    if capacity is None:
        from sparkrun.platforms import resolve_accelerator_platform

        platform = resolve_accelerator_platform(accel, host_hw)
        capacity = platform.default_accelerator_memory_gb(accel) if platform is not None else None
        source = "platform default" if host_hw.source != "assumed" else "assumed platform default"
    if capacity is None:
        return None, "unknown"
    if isinstance(capacity, bool) or not isinstance(capacity, (int, float)) or not math.isfinite(capacity) or capacity <= 0:
        raise ValueError("invalid accelerator memory capacity from %s" % source)
    return float(capacity), source


def resolve_accelerator_memory_gb(accel: AcceleratorSpec, host_hw: HostHardware) -> float | None:
    """Inventory capacity, then a qualified platform default; otherwise unknown."""
    return resolve_accelerator_memory(accel, host_hw)[0]


def usable_memory_gb(
    accel: AcceleratorSpec,
    host_hw: HostHardware,
    cluster: "ClusterDefinition | None",
) -> float | None:
    """Usable memory (GB) for *accel* = ``memory_gb × resolved cap``.

    Returns ``None`` when neither inventory nor the matched platform supplies
    capacity (callers report "memory not verified").
    """
    capacity = resolve_accelerator_memory_gb(accel, host_hw)
    return capacity * resolve_max_gpu_memory_utilization(accel, host_hw, cluster) if capacity is not None else None


def resolved_hardware_for_scheduling(
    cluster: "ClusterDefinition | None",
    hosts: list[str],
) -> dict[str, HostHardware]:
    """Materialize per-host hardware with the usable-memory cap folded in.

    For every host in *hosts*, resolves the full cap chain (cluster + platform
    + per-accel) once and bakes the result into each
    :class:`AcceleratorSpec.max_gpu_memory_utilization` so the schedulers — which
    only see ``host_hardware`` and must stay free of ``platforms`` /
    ``cluster_manager`` imports — can apply a single resolved fraction.

    Missing nominal capacity is filled from qualified platform defaults. The
    returned copies are ephemeral scheduling inputs and are never persisted.
    """
    resolved: dict[str, HostHardware] = {}
    for host in hosts:
        hw = cluster.hardware_for(host) if cluster is not None else resolve_hardware()
        new_accels = []
        for accel in hw.accelerators:
            cap, source = resolve_memory_limit(accel, hw, cluster)
            capacity, capacity_source = resolve_accelerator_memory(accel, hw)
            new_accels.append(
                dataclasses.replace(
                    accel,
                    memory_gb=capacity,
                    memory_capacity_source=capacity_source,
                    max_gpu_memory_utilization=cap,
                    memory_limit_source=source,
                )
            )
        resolved[host] = dataclasses.replace(hw, accelerators=new_accels)

    return resolved
