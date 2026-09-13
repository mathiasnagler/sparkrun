"""Supported process-lifecycle base for gateway plugin implementations.

The implementation stays private; this facade preserves the exact base class
used by bundled and legacy gateways. Records and errors live in contracts.
"""

from sparkrun.proxy._supervisor import GatewaySupervisor

__all__ = ["GatewaySupervisor"]
