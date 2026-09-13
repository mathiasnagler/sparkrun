"""Expected provider failures use one public start error boundary."""

from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from sparkrun import api
from sparkrun.api.proxy import _ops
from sparkrun.proxy.contracts import GatewayOperationError
from sparkrun.proxy.engine import ProxyEngine
from sparkrun.proxy.gateway import GatewayUnavailableError


@pytest.mark.parametrize("phase", ["construction", "ownership", "preview", "write", "shutdown", "start"])
@pytest.mark.parametrize("failure", ["operation", "gate", "bug", "interrupt"])
def test_start_error_boundary_preserves_error_kind_and_cause(monkeypatch, phase, failure):
    context = api.default_sctx()
    error = {
        "operation": GatewayOperationError("provider refused operation"),
        "gate": GatewayUnavailableError("provider disabled", gateway="litellm"),
        "bug": RuntimeError("provider programming bug"),
        "interrupt": KeyboardInterrupt(),
    }[failure]
    expected = {
        "operation": api.proxy.ProxyStartFailed,
        "gate": api.proxy.GatewayUnavailable,
        "bug": RuntimeError,
        "interrupt": KeyboardInterrupt,
    }[failure]
    monkeypatch.setattr(_ops, "_discover", lambda **kwargs: [])
    monkeypatch.setattr(ProxyEngine, "is_running", lambda self: phase == "shutdown")
    monkeypatch.setattr(ProxyEngine, "start", Mock(return_value=0))
    if phase in {"preview", "write"}:
        original = ProxyEngine.prepare_config

        def prepare(self, endpoints, aliases, *, write=True):
            if write == (phase == "write"):
                raise error
            return original(self, endpoints, aliases, write=write)

        monkeypatch.setattr(ProxyEngine, "prepare_config", prepare)
    elif phase == "shutdown":
        monkeypatch.setattr(_ops, "_stop_and_wait", Mock(side_effect=error))
    else:
        method = {"construction": "__init__", "ownership": "claim_state_directory", "start": "start"}[phase]
        monkeypatch.setattr(ProxyEngine, method, Mock(side_effect=error))
    with pytest.raises(expected) as caught:
        api.proxy.start(api.proxy.ProxyStartOptions(gateway="litellm", persist=False, restart=True), sctx=context)
    if failure in {"operation", "gate"}:
        assert isinstance(caught.value, api.SparkrunError)
        assert caught.value.__cause__ is error
        assert str(caught.value) == str(error)
        if failure == "gate":
            assert caught.value.gateway == "litellm"
    else:
        assert caught.value is error


def test_malformed_binding_is_a_public_error_and_a_readable_cli_failure(monkeypatch):
    from sparkrun.cli import main
    from sparkrun.plugins.sparkroute.engine import SparkrouteConfigError

    monkeypatch.setenv("SPARKRUN_FEATURE_GATEWAY_SPARKROUTE", "1")
    context = api.default_sctx()
    context.proxy_config.set_bindings([{}])
    context.proxy_config.save()
    monkeypatch.setattr(_ops, "_discover", lambda **kwargs: [])
    with pytest.raises(api.proxy.ProxyStartFailed, match="has no 'recipe'") as caught:
        api.proxy.start(api.proxy.ProxyStartOptions(gateway="sparkroute", dry_run=True), sctx=context)
    assert isinstance(caught.value.__cause__, SparkrouteConfigError)
    result = CliRunner().invoke(main, ["proxy", "start", "--gateway", "sparkroute", "--dry-run"], obj={"sparkrun_ctx": context})
    assert result.exit_code == 1
    assert "Error: bindings[0] has no 'recipe'" in result.stderr
    assert isinstance(result.exception, SystemExit)
