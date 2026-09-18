"""Durable GPU reservations, independent of instantaneous device activity."""

from __future__ import annotations

import base64
import binascii
import json
import math
from dataclasses import asdict, dataclass

EXCLUSIVE_GPU_THRESHOLD = 0.8
ALLOCATION_LABEL = "sparkrun.gpu_allocations"


@dataclass(frozen=True)
class GpuAllocation:
    """One global rank's reservation of a physical local GPU."""

    rank: int
    gpu_index: int
    util_fraction: float = 1.0
    memory_gb: float | None = None

    def __post_init__(self):
        if type(self.rank) is not int or self.rank < 0 or type(self.gpu_index) is not int or self.gpu_index < 0:
            raise ValueError("GPU allocation rank and index must be non-negative integers")
        validate_fraction(self.util_fraction)
        validate_memory(self.memory_gb)
        if not self.exclusive and self.memory_gb is None:
            raise ValueError("Shared GPU allocations require a memory reservation")

    @property
    def exclusive(self) -> bool:
        return self.util_fraction >= EXCLUSIVE_GPU_THRESHOLD


def validate_fraction(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value <= 1:
        raise ValueError("GPU allocation util_fraction must be finite and in (0, 1]")


def validate_memory(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0):
        raise ValueError("GPU allocation memory_gb must be finite and non-negative")


def encode_allocations(allocations: tuple[GpuAllocation, ...]) -> str:
    """Base64 avoids commas in docker ps's label-list format."""
    payload = {"schema": 1, "allocations": [asdict(a) for a in allocations]}
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def decode_allocations(value: str | None) -> tuple[GpuAllocation, ...]:
    """Absent/invalid records never establish permission to share a GPU."""
    if not value:
        return ()
    try:
        payload = json.loads(base64.b64decode(value, validate=True))
        if payload["schema"] != 1 or not isinstance(payload["allocations"], list):
            return ()
        allocations = tuple(GpuAllocation(**item) for item in payload["allocations"])
        if len({a.rank for a in allocations}) != len(allocations):
            return ()
        return allocations
    except (ValueError, TypeError, KeyError, binascii.Error):
        return ()


def allocations_for_host(placement, host: str) -> tuple[GpuAllocation, ...]:
    if placement is None:
        return ()
    return tuple(
        GpuAllocation(rank, slot.local_gpu, slot.util_fraction, slot.memory_gb)
        for rank, slot in enumerate(placement.by_rank)
        if slot.host == host
    )
