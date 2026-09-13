"""Root CLI management retains recovery after application bootstrap failure."""

import json
from unittest.mock import Mock

import pytest
import yaml
from click.testing import CliRunner

from sparkrun import api
from sparkrun.core import bootstrap
from sparkrun.proxy._supervisor import GatewayState, GatewaySupervisor
from test_proxy_management_initialization import prepare_application


@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("reason", ["bootstrap", "disabled", "removed"])
@pytest.mark.parametrize("command", [["status", "--json"], ["stop"], ["stop", "--dry-run"], ["models", "--json"]])
def test_cli_recovery_uses_the_application_binding(tmp_path, monkeypatch, alternate, reason, command):
    from sparkrun.cli import main

    config, identity = prepare_application(tmp_path, monkeypatch, alternate=alternate, enabled=reason != "disabled")
    # Exercise Click's explicit config binding, not the child's config env var.
    monkeypatch.delenv("SPARKRUN_APPLICATION_CONFIG")
    monkeypatch.setattr(main, "_cli_ext_loaded", False, raising=False)
    name = "sparkroute"
    if reason == "bootstrap":
        monkeypatch.setattr(bootstrap, "_register_plugins", Mock(side_effect=RuntimeError("broken plugin")))
    elif reason == "removed":
        name = "removed-provider"
        state = tmp_path / "cache" / "proxy" / "state.yaml"
        state.write_text(yaml.safe_dump({"gateway": name, "distribution": identity, "pid": 12345, "port": 8000}))
    stop = Mock(return_value=True)
    monkeypatch.setattr(GatewaySupervisor, "stop", stop)
    result = CliRunner().invoke(main, ["proxy", *command], obj={"config_path": config})
    from sparkrun.core.config import resolve_config_path
    from sparkrun.application import get_application_profile

    assert resolve_config_path() == config
    assert get_application_profile().id == identity
    if command[0] == "models":
        assert result.exit_code == 1
        assert "Model list unavailable" in result.stderr and name in result.stderr
        assert not result.stdout
    else:
        assert result.exit_code == 0, result.output
        if command[0] == "status":
            snapshot = json.loads(result.stdout)
            assert snapshot["running"] and snapshot["pid"] == 12345
            assert snapshot["gateway"] == name and snapshot["model_query_error"]
        else:
            stop.assert_called_once_with(dry_run="--dry-run" in command)
    if command[0] != "stop":
        stop.assert_not_called()


def test_cli_reuses_an_existing_context_without_initializing(tmp_path, monkeypatch):
    from sparkrun.cli import main

    prepare_application(tmp_path, monkeypatch, enabled=False)
    context = api.default_sctx()
    monkeypatch.setattr(main, "_cli_ext_loaded", False, raising=False)
    factory = Mock(side_effect=AssertionError("must reuse supplied context"))
    monkeypatch.setattr("sparkrun.application.initialize", factory)
    result = CliRunner().invoke(main, ["proxy", "status", "--json"], obj={"sparkrun_ctx": context})
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["pid"] == 12345
    factory.assert_not_called()


@pytest.mark.parametrize("command", ["status", "stop", "models"])
def test_cli_rejects_invalid_profile_without_accessing_gateway_state(monkeypatch, command):
    from sparkrun.cli import main

    monkeypatch.setenv("SPARKRUN_APPLICATION_PROFILE", "missing_cli_profile:PROFILE")
    monkeypatch.setattr(main, "_cli_ext_loaded", False, raising=False)
    state = Mock(side_effect=AssertionError("accessed another application's state"))
    monkeypatch.setattr(GatewayState, "__init__", state)
    result = CliRunner().invoke(main, ["proxy", command])
    assert result.exit_code == 1
    assert "Error:" in result.stderr and "missing_cli_profile" in result.stderr
    assert isinstance(result.exception, SystemExit)
    state.assert_not_called()


@pytest.mark.parametrize("command", ["status", "stop"])
def test_cli_does_not_recover_from_an_interrupt(monkeypatch, command):
    from sparkrun.cli import main

    monkeypatch.setattr(main, "_cli_ext_loaded", False, raising=False)
    monkeypatch.setattr("sparkrun.application.initialize", Mock(side_effect=KeyboardInterrupt))
    stop = Mock()
    monkeypatch.setattr(GatewaySupervisor, "stop", stop)
    result = CliRunner().invoke(main, ["proxy", command])
    assert result.exit_code == 1 and "Aborted!" in result.stderr
    stop.assert_not_called()
