"""Generated gateway configuration belongs to the accepted start transition."""

from dataclasses import asdict
import json
from unittest.mock import Mock

import pytest

from sparkrun import api
from sparkrun.api.proxy import _ops
from sparkrun.proxy.contracts import GatewayOperationError
from sparkrun.proxy.discovery import DiscoveredEndpoint


@pytest.fixture(params=["litellm", "sparkroute"])
def gateway(request, tmp_path, monkeypatch):
    from sparkrun.plugins.sparkroute.engine import SparkrouteEngine
    from sparkrun.proxy.engine import ProxyEngine

    name = request.param
    monkeypatch.setenv("SPARKRUN_FEATURE_GATEWAY_SPARKROUTE", "1")
    context = api.default_sctx()
    base = ProxyEngine if name == "litellm" else SparkrouteEngine
    root = tmp_path / "gateway"
    events = []

    class LocalEngine(base):
        def __init__(self, **kwargs):
            super().__init__(state_dir=root, **kwargs)

        def prepare_config(self, endpoints, aliases, *, write=True):
            events.append("write" if write else "preview")
            return super().prepare_config(endpoints, aliases, write=write)

        def start(self, **kwargs):
            events.append("start")
            assert kwargs["config_path"] == (self.config_path if name == "litellm" else None)
            return 0

    kwargs = {"proxy_config": context.proxy_config, "sctx": context} if name == "sparkroute" else {}
    engine = LocalEngine(**kwargs)
    engine.claim_state_directory()
    engine.prepare_config(
        [
            DiscoveredEndpoint(
                cluster_id="previous",
                model="previous-model",
                served_model_name=None,
                runtime="vllm",
                host="localhost",
                port=8000,
                healthy=True,
            )
        ],
        {},
    )
    engine._save_state(pid=12345)
    events.clear()
    monkeypatch.setattr(_ops, "_engine_class", lambda gateway: LocalEngine)
    monkeypatch.setattr(_ops, "_discover", lambda **kwargs: [])
    monkeypatch.setattr(LocalEngine, "is_running", lambda self: True)
    return name, context, engine, events


def generated_files(context, engine):
    roots = (engine.state_dir, context.proxy_config.config_path.parent)
    return {path: path.read_bytes() for root in roots for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("restart", [False, True])
def test_refused_start_and_shutdown_timeout_preserve_generated_state(gateway, monkeypatch, restart):
    name, context, engine, events = gateway
    before = generated_files(context, engine)
    wait = Mock(return_value=False)
    monkeypatch.setattr(_ops, "_stop_and_wait", wait)
    error = api.proxy.ProxyStartFailed if restart else api.proxy.ProxyAlreadyRunning
    with pytest.raises(error):
        api.proxy.start(api.proxy.ProxyStartOptions(gateway=name, restart=restart, persist=False), sctx=context)
    assert generated_files(context, engine) == before
    assert events == ["preview"]
    assert wait.call_count == int(restart)


@pytest.mark.parametrize("foreground", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_successful_start_writes_after_shutdown_and_normalizes_path(gateway, monkeypatch, foreground, restart):
    name, context, engine, events = gateway
    monkeypatch.setattr(type(engine), "is_running", lambda self: restart)

    def stop_and_wait(current):
        events.append("stop")
        return True

    monkeypatch.setattr(_ops, "_stop_and_wait", stop_and_wait)
    result = api.proxy.start(api.proxy.ProxyStartOptions(gateway=name, restart=restart, persist=False, foreground=foreground), sctx=context)
    assert events == ["preview", *(["stop"] if restart else []), "write", "start"]
    assert result.started and result.restarted == restart
    assert result.foreground_rc == (0 if foreground else None)
    expected_path = str(engine.config_path) if name == "litellm" else None
    assert result.config_path == expected_path
    assert json.loads(json.dumps(asdict(result)))["config_path"] == expected_path


def test_dry_run_preserves_generated_state_and_has_no_config_path(gateway):
    name, context, engine, events = gateway
    before = generated_files(context, engine)
    result = api.proxy.start(api.proxy.ProxyStartOptions(gateway=name, dry_run=True), sctx=context)
    assert not result.started and result.dry_run
    assert result.config_path is None
    assert json.loads(json.dumps(asdict(result)))["config_path"] is None
    assert generated_files(context, engine) == before
    assert events == ["preview"]


def test_invalid_preview_does_not_stop_a_working_gateway(gateway, monkeypatch):
    name, context, engine, _ = gateway
    before = generated_files(context, engine)
    monkeypatch.setattr(type(engine), "prepare_config", Mock(side_effect=GatewayOperationError("invalid configuration")))
    wait = Mock()
    monkeypatch.setattr(_ops, "_stop_and_wait", wait)
    with pytest.raises(api.proxy.ProxyStartFailed, match="invalid configuration") as caught:
        api.proxy.start(api.proxy.ProxyStartOptions(gateway=name, restart=True, persist=False), sctx=context)
    assert isinstance(caught.value.__cause__, GatewayOperationError)
    wait.assert_not_called()
    assert generated_files(context, engine) == before
