"""Offline gateway consumer of the public 0.4 plugin contract."""

import os
import secrets

from sparkrun.proxy.supervisor import GatewaySupervisor
from sparkrun.proxy.contracts import GatewayQueryError, ProxyModel


class ProfileTestGateway(GatewaySupervisor):
    gateway_name = "profile-test"
    supports_autodiscover = False
    ui_url = "http://127.0.0.1:9999/admin"
    admin_bind_host = "127.0.0.1"
    admin_exposed = False

    def query_models(self):
        if os.environ.get("PROFILE_TEST_GATEWAY_FAIL"):
            raise GatewayQueryError("fixture control plane unavailable")
        return (ProxyModel("fixture-model", "http://fixture/v1", 8192),)

    @property
    def admin_auth_required(self):
        return self.admin_token() is not None

    def admin_token(self, *, rotate=False, clear=False):
        path = self.state_dir / "fixture-admin-token"
        if clear:
            path.unlink(missing_ok=True)
        elif rotate:
            path.write_text(secrets.token_urlsafe(32))
        return path.read_text() if path.exists() else None

    def issue_ui_credential(self):
        return self.admin_token() or self.admin_token(rotate=True)
