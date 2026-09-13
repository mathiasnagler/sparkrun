"""Actual pinned provider errors follow the host contract without vendor edits."""

import io
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest
import yaml
from click.testing import CliRunner

from sparkrun import api
from sparkrun.api.proxy import _ops
from sparkrun.plugins.sparkroute.admin import AdminError, RevisionConflict
from sparkrun.plugins.sparkroute.engine import SparkrouteConfigError, SparkrouteEngine
from sparkrun.proxy.contracts import GatewayOperationError
from sparkrun.proxy.gateway import gateway_class
from test_proxy_management_initialization import prepare_application


@pytest.fixture
def provider(tmp_path, monkeypatch):
    prepare_application(tmp_path, monkeypatch)
    context = api.default_sctx()
    monkeypatch.setattr(_ops, "_discover", lambda **kwargs: [])
    return context


@pytest.mark.parametrize("operation", ["add", "remove"])
@pytest.mark.parametrize("frontend", ["api", "cli"])
def test_real_binding_error_preserves_alias_persistence(provider, operation, frontend):
    from sparkrun.cli import main

    provider.proxy_config.set_bindings([{}])
    provider.proxy_config.add_alias("friendly", "old-target")
    provider.proxy_config.save()
    if frontend == "api":
        with pytest.raises(api.proxy.ProxyUpdateFailed, match="has no 'recipe'") as caught:
            if operation == "add":
                api.proxy.add_alias("friendly", "new-target", sctx=provider)
            else:
                api.proxy.remove_alias("friendly", sctx=provider)
        assert isinstance(caught.value.__cause__, SparkrouteConfigError)
    else:
        args = ["proxy", "alias", operation, "friendly"] + (["new-target"] if operation == "add" else [])
        result = CliRunner().invoke(main, args, obj={"sparkrun_ctx": provider})
        assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
        assert "Error: bindings[0] has no 'recipe'" in result.stderr
        assert "Alias " + ("added" if operation == "add" else "removed") in result.stdout
    saved = yaml.safe_load(provider.proxy_config.config_path.read_text())
    assert saved.get("aliases", {}) == ({"friendly": "new-target"} if operation == "add" else {})


@pytest.mark.parametrize("failure", ["transport", "auth"])
@pytest.mark.parametrize("frontend", ["api_sync", "api_register", "api_unregister", "cli_sync", "cli_refresh"])
def test_real_admin_client_failure_is_public(provider, monkeypatch, failure, frontend):
    from sparkrun.cli import main

    error = (
        URLError("refused with private request context")
        if failure == "transport"
        else HTTPError("http://fixture/v1", 401, "Unauthorized", {}, io.BytesIO(b'{"error":{"message":"authentication required"}}'))
    )
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=error))
    if frontend.startswith("api"):
        with pytest.raises(api.proxy.ProxyUpdateFailed) as caught:
            if frontend == "api_sync":
                api.proxy.sync(endpoints=[], require_running=True, sctx=provider)
            elif frontend == "api_register":
                api.proxy.register_loaded_model("fixture", sctx=provider)
            else:
                api.proxy.unregister_loaded_model("fixture", sctx=provider)
        adapted = caught.value.__cause__
        assert isinstance(adapted, AdminError) and isinstance(adapted, GatewayOperationError)
        assert isinstance(adapted.__cause__, AdminError)
        assert adapted.status == (401 if failure == "auth" else 0)
        assert adapted.retryable == (failure == "transport")
        assert str(caught.value) == str(adapted.__cause__)
        assert "private request context" not in str(caught.value)
    else:
        args = ["proxy", "sync"] if frontend == "cli_sync" else ["proxy", "models", "--refresh"]
        result = CliRunner().invoke(main, args, obj={"sparkrun_ctx": provider})
        assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
        assert "Error:" in result.stderr
    # A failed query remains distinguishable from a legitimately empty model list.
    status = api.proxy.status(sctx=provider)
    assert status.running and status.model_query_error
    with pytest.raises(api.proxy.ProxyQueryFailed):
        status.require_models()


def test_exhausted_revision_conflicts_are_operational(provider, monkeypatch):
    error = RevisionConflict("revision moved", status=409, code="revision_conflict")
    client = Mock()
    client.active_revision.return_value = "1"
    client.get_set.return_value = {"document": {}}
    client.replace.side_effect = error
    monkeypatch.setattr(SparkrouteEngine, "admin_client", lambda self: client)
    monkeypatch.setattr("sparkrun.plugins.sparkroute.engine.time.sleep", lambda delay: None)
    with pytest.raises(api.proxy.ProxyUpdateFailed, match="kept changing") as caught:
        api.proxy.sync(endpoints=[], sctx=provider)
    adapted = caught.value.__cause__
    assert isinstance(adapted, GatewayOperationError) and isinstance(adapted, AdminError)
    assert adapted.code == "revision_conflict" and adapted.retryable
    assert adapted.__cause__.__cause__ is error
    assert client.replace.call_count > 1


def test_adapter_preserves_started_process_when_reconcile_fails(provider, monkeypatch, caplog):
    # Upstream intentionally leaves the process running after a failed initial
    # reconcile. Adapted errors must still match its AdminError catch.
    engine_class = gateway_class("sparkroute")
    engine = engine_class(proxy_config=provider.proxy_config, sctx=provider)
    monkeypatch.setattr("sparkrun.plugins.sparkroute.engine.ensure_binary", lambda: Path("fixture"))
    monkeypatch.setattr(engine, "is_running", lambda: False)
    monkeypatch.setattr(engine, "_prepare_live_tokens", lambda: None)
    monkeypatch.setattr(engine, "build_command", lambda binary: [str(binary)])
    monkeypatch.setattr(engine, "_launch_background", lambda *args: 12345)
    monkeypatch.setattr(engine, "_await_admin_ready", lambda: None)
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=URLError("refused")))
    assert engine.start() == 0
    assert engine.current_pid() == 12345
    assert "Gateway started but its configuration could not be reconciled" in caplog.text


def test_compatibility_is_local_to_registered_pinned_provider(provider):
    from sparkrun.proxy._gateway_adapters import adapt_gateway_class

    class Derived(SparkrouteEngine):
        pass

    adapted = gateway_class("sparkroute")
    assert adapted is gateway_class("sparkroute")
    assert adapted is not SparkrouteEngine and issubclass(adapted, SparkrouteEngine)
    assert adapt_gateway_class(Derived) is Derived
    assert adapt_gateway_class(adapted) is adapted
