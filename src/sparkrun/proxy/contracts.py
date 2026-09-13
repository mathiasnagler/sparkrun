"""Shared gateway model records and optional management capabilities.

Plugin authors implement ``GatewaySupervisor.query_models`` using these records.
Console and credential protocols are independent optional capabilities; a model
provider need not implement either. This module imports no API, CLI, or engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ProxyModel:
    """One served model, also exported by ``sparkrun.api.proxy``."""

    model_name: str
    api_base: str = ""
    max_model_len: int | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"model_name": self.model_name, "api_base": self.api_base or "?"}
        if self.max_model_len is not None:
            data["max_model_len"] = self.max_model_len
        return data


class GatewayOperationError(RuntimeError):
    """An expected gateway management failure with a diagnostic safe to display.

    Use for reconciliation, configuration, console state, and credentials.
    Model enumeration uses GatewayQueryError. Unrelated programming exceptions
    should propagate; callers do not treat arbitrary RuntimeError as operational.
    """


class GatewayQueryError(GatewayOperationError):
    """Model enumeration is unavailable; the message must contain no secrets."""


@runtime_checkable
class GatewayConsole(Protocol):
    """Live console state; unavailable properties raise GatewayOperationError."""

    @property
    def ui_url(self) -> str: ...

    @property
    def admin_bind_host(self) -> str: ...

    @property
    def admin_exposed(self) -> bool: ...

    @property
    def admin_auth_required(self) -> bool: ...


@runtime_checkable
class GatewayConsoleCredentials(Protocol):
    """Create/retrieve credentials (may enable auth); GatewayOperationError on refusal."""

    def issue_ui_credential(self) -> str: ...


@runtime_checkable
class GatewayAdminToken(Protocol):
    """Live admin token management, independent of inference authentication.

    No flags reads; ``rotate`` replaces; ``clear`` disables when policy permits.
    None means authentication is open. GatewayOperationError reports a safe diagnostic.
    """

    def admin_token(self, *, rotate: bool = False, clear: bool = False) -> str | None: ...


__all__ = ["ProxyModel", "GatewayOperationError", "GatewayQueryError", "GatewayConsole", "GatewayConsoleCredentials", "GatewayAdminToken"]
