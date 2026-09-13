"""Recovery-only process supervision refuses mutations before discovery."""

from unittest.mock import Mock

import pytest
import yaml
from click.testing import CliRunner

from sparkrun import api
from sparkrun.api.proxy import _ops
from sparkrun.core import bootstrap
from sparkrun.proxy.supervisor import GatewaySupervisor
from test_proxy_management_initialization import prepare_application


OPERATIONS = ["sync", "add", "remove", "register_loaded_model", "unregister_loaded_model"]
COMMANDS = [["sync"], ["models", "--refresh"], ["alias", "add", "friendly", "new-target"], ["alias", "remove", "friendly"]]


def prepare_recovery(tmp_path, monkeypatch, *, alternate=False, reason="disabled"):
    config, identity = prepare_application(tmp_path, monkeypatch, alternate=alternate, enabled=reason != "disabled")
    (config.parent / "proxy.yaml").write_text("aliases:\n  friendly: old-target\n")
    gateway = "sparkroute"
    if reason == "removed":
        gateway = "removed-provider"
        state = tmp_path / "cache" / "proxy" / "state.yaml"
        state.write_text(yaml.safe_dump({"gateway": gateway, "distribution": identity, "pid": 12345, "port": 8000}))
    elif reason == "bootstrap":
        monkeypatch.setattr(bootstrap, "_register_plugins", Mock(side_effect=RuntimeError("broken plugin")))
    discovery = Mock(side_effect=AssertionError("discovery ran without an implementation"))
    monkeypatch.setattr(_ops, "_discover", discovery)
    return config, gateway, discovery


def update(operation, *, context=None, require_running=True):
    if operation == "sync":
        return api.proxy.sync(require_running=require_running, sctx=context)
    if operation == "add":
        return api.proxy.add_alias("friendly", "new-target", sctx=context)
    if operation == "remove":
        return api.proxy.remove_alias("friendly", sctx=context)
    return getattr(api.proxy, operation)("fixture", sctx=context)


@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("reason", ["disabled", "removed"])
@pytest.mark.parametrize("operation", OPERATIONS)
def test_unavailable_provider_refuses_api_mutation(tmp_path, monkeypatch, alternate, reason, operation):
    config, gateway, discovery = prepare_recovery(tmp_path, monkeypatch, alternate=alternate, reason=reason)
    with pytest.raises(api.proxy.GatewayUnavailable, match="Restore its plugin") as caught:
        update(operation)
    assert caught.value.gateway == gateway
    assert isinstance(caught.value, api.SparkrunError)
    discovery.assert_not_called()
    saved = yaml.safe_load((config.parent / "proxy.yaml").read_text())
    expected = {"friendly": "new-target"} if operation == "add" else {} if operation == "remove" else {"friendly": "old-target"}
    assert saved.get("aliases", {}) == expected
    assert api.proxy.status().running
    stop = Mock(return_value=True)
    monkeypatch.setattr(GatewaySupervisor, "stop", stop)
    assert api.proxy.stop(dry_run=True).stopped
    stop.assert_called_once_with(dry_run=True)


@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("reason", ["disabled", "removed"])
@pytest.mark.parametrize("command", COMMANDS)
def test_unavailable_provider_cli_explains_mutation_refusal(tmp_path, monkeypatch, alternate, reason, command):
    from sparkrun.cli import main

    config, gateway, discovery = prepare_recovery(tmp_path, monkeypatch, alternate=alternate, reason=reason)
    monkeypatch.setattr(main, "_cli_ext_loaded", False, raising=False)
    monkeypatch.delenv("SPARKRUN_APPLICATION_CONFIG")
    result = CliRunner().invoke(main, ["proxy", *command], obj={"config_path": config})
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert "Error:" in result.stderr and "Restore its plugin" in result.stderr and gateway in result.stderr
    if command[0] == "alias":
        assert "Alias " + ("added" if command[1] == "add" else "removed") in result.stdout
    discovery.assert_not_called()


@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("frontend", ["sync", "register_loaded_model", "unregister_loaded_model", "cli_sync", "cli_refresh"])
def test_failed_bootstrap_refuses_mutation_before_discovery(tmp_path, monkeypatch, alternate, frontend):
    from sparkrun.cli import main

    config, _, discovery = prepare_recovery(tmp_path, monkeypatch, alternate=alternate, reason="bootstrap")
    if frontend.startswith("cli"):
        monkeypatch.setattr(main, "_cli_ext_loaded", False, raising=False)
        command = ["sync"] if frontend == "cli_sync" else ["models", "--refresh"]
        result = CliRunner().invoke(main, ["proxy", *command], obj={"config_path": config})
        assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
        assert "Error:" in result.stderr and "Restore its plugin" in result.stderr
    else:
        with pytest.raises(api.proxy.GatewayUnavailable, match="sparkroute"):
            update(frontend)
    discovery.assert_not_called()


@pytest.mark.parametrize("operation", OPERATIONS)
def test_stopped_recovery_mutations_keep_noop_behavior(tmp_path, monkeypatch, operation):
    prepare_recovery(tmp_path, monkeypatch)
    monkeypatch.setattr(GatewaySupervisor, "is_running", lambda self: False)
    result = update(operation)
    assert not result.proxy_running
    if operation in {"add", "remove"}:
        assert result.saved


def test_stopped_sync_without_require_running_still_needs_provider(tmp_path, monkeypatch):
    prepare_recovery(tmp_path, monkeypatch)
    monkeypatch.setattr(GatewaySupervisor, "is_running", lambda self: False)
    with pytest.raises(api.proxy.GatewayUnavailable):
        update("sync", require_running=False)


@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("operation", ["add", "remove"])
def test_alias_cli_bootstrap_failure_does_not_claim_a_saved_edit(tmp_path, monkeypatch, alternate, operation):
    from sparkrun.cli import main

    config, _, discovery = prepare_recovery(tmp_path, monkeypatch, alternate=alternate, reason="bootstrap")
    before = (config.parent / "proxy.yaml").read_bytes()
    monkeypatch.setattr(main, "_cli_ext_loaded", False, raising=False)
    args = ["proxy", "alias", operation, "friendly"] + (["new-target"] if operation == "add" else [])
    result = CliRunner().invoke(main, args, obj={"config_path": config})
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert "Error: Application initialization failed" in result.stderr
    assert not result.stdout
    assert (config.parent / "proxy.yaml").read_bytes() == before
    discovery.assert_not_called()
