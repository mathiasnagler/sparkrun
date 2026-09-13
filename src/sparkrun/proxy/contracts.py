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


class GatewayQueryError(RuntimeError):
    """Model enumeration is unavailable; the message must contain no secrets."""


@runtime_checkable
class GatewayConsole(Protocol):
    """An admin console and its live listener/authentication state."""

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
    """Optional creation/retrieval of console credentials (may enable auth)."""

    def issue_ui_credential(self) -> str: ...


@runtime_checkable
class GatewayAdminToken(Protocol):
    """Live admin token management, independent of inference authentication.

    No flags reads; ``rotate`` replaces; ``clear`` disables when policy permits.
    None means authentication is open. RuntimeError reports a safe diagnostic.
    """

    def admin_token(self, *, rotate: bool = False, clear: bool = False) -> str | None: ...


__all__ = ["ProxyModel", "GatewayQueryError", "GatewayConsole", "GatewayConsoleCredentials", "GatewayAdminToken"]
