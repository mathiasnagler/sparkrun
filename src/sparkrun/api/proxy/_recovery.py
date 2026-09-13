"""Internal distinction between recorded process recovery and provider operations."""

from sparkrun.proxy.supervisor import GatewaySupervisor
from ._errors import GatewayUnavailable


class ProcessRecoverySupervisor(GatewaySupervisor):
    """The provider is unavailable; only inherited process recovery is usable."""


def require_implementation(engine: GatewaySupervisor) -> None:
    """Refuse provider-dependent work before discovery or live configuration I/O."""
    if isinstance(engine, ProcessRecoverySupervisor):
        raise GatewayUnavailable(
            "Gateway %r has no available implementation. Restore its plugin and application configuration before updating models or aliases; "
            "process status and stop remain available." % engine.gateway_name,
            gateway=engine.gateway_name,
        )
