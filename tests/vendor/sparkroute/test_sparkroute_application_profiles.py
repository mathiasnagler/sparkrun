# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/sparkroute/LICENSE_EXCEPTION.

"""Profile-aware behavior, with no gateway downloads or real workload launches."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from sparkrun.plugins.sparkroute import _application_profile, release, jobs


@pytest.fixture
def profile(tmp_path, monkeypatch):
    api = pytest.importorskip("sparkrun.core.application_profile")
    api._reset_application_profile_for_tests()
    monkeypatch.setenv("HOME", str(tmp_path))
    code = (
        "from sparkrun.core.application_profile import ApplicationProfile\n"
        "PROFILE = ApplicationProfile(id='jetson-test', display_name='Jetson test', "
        "command='jetson-test', package='jetson-test', profile_ref='sparkroute_test_profile:PROFILE', "
        "feature_defaults={'gateway.sparkroute': True}, registries=())\n"
    )
    (tmp_path / "sparkroute_test_profile.py").write_text(code)
    scope = {}
    exec(code, scope)
    api.select_application_profile(scope["PROFILE"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(tmp_path), os.environ.get("PYTHONPATH", "")]))
    return scope["PROFILE"]


def test_cache_paths_are_product_owned(profile, tmp_path, monkeypatch):
    monkeypatch.setenv("SPARKRUN_CACHE_DIR", str(tmp_path / "foreign"))
    assert release.binary_path("test").parent == tmp_path / ".cache/jetson-test/gateways/sparkroute/test"
    monkeypatch.setenv("JETSON_TEST_CACHE_DIR", str(tmp_path / "custom"))
    assert release.binary_path("test").is_relative_to(tmp_path / "custom")
    assert release.binary_path("test", cache_dir=tmp_path / "explicit").is_relative_to(tmp_path / "explicit")


def test_binary_override_is_shared_across_profiles(profile, tmp_path, monkeypatch):
    monkeypatch.setenv("JETSON_TEST_SPARKROUTE_BINARY", str(tmp_path / "unused-profile-binary"))
    binary = tmp_path / "sparkroute"
    binary.write_bytes(b"shared development binary")
    monkeypatch.setenv("SPARKROUTE_BINARY", str(binary))
    with mock.patch.object(release.urllib.request, "urlopen") as download:
        assert release.ensure_binary(cache_dir=tmp_path) == binary
    download.assert_not_called()


def test_callback_uses_active_console_in_current_environment(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "environment"))
    windows = sys.platform == "win32"
    console = Path(sys.prefix) / ("Scripts" if windows else "bin") / ("jetson-test.exe" if windows else "jetson-test")
    with mock.patch.object(release.shutil, "which", return_value="/foreign/bin/jetson-test") as search:
        with pytest.raises(release.GatewayReleaseError, match="jetson-test"):
            release.resolve_sparkrun_executable()
        console.parent.mkdir(parents=True)
        console.write_text("fixture")
        console.chmod(0o755)
        assert release.resolve_sparkrun_executable() == str(console)
        search.assert_not_called()


def test_gateway_foreground_and_background_children_keep_profile(profile, tmp_path):
    from sparkrun.plugins.sparkroute.engine import SparkrouteEngine
    from sparkrun.core.config import SparkrunConfig

    config = SparkrunConfig(tmp_path / "custom-config.yaml")
    engine = SparkrouteEngine(state_dir=tmp_path / "state", sctx=SimpleNamespace(config=config))
    with (
        mock.patch("sparkrun.plugins.sparkroute.engine.require_gateway_enabled"),
        mock.patch("sparkrun.plugins.sparkroute.engine.ensure_binary", return_value=tmp_path / "binary"),
        mock.patch.object(engine, "is_running", return_value=False),
        mock.patch.object(engine, "_prepare_live_tokens"),
        mock.patch.object(engine, "build_command", return_value=["fixture"]),
        mock.patch.object(engine, "_save_state"),
        mock.patch.object(engine, "_clear_state"),
        mock.patch.object(engine, "stop_autodiscover"),
        mock.patch.object(engine, "_await_admin_ready"),
        mock.patch.object(engine, "reconcile", return_value=(0, 0)),
        mock.patch.object(engine, "_launch_background", return_value=123) as background,
        mock.patch("sparkrun.plugins.sparkroute.engine.subprocess.Popen") as foreground,
    ):
        foreground.return_value.wait.return_value = 0
        assert engine.start() == 0
        assert engine.start(foreground=True) == 0
    for environment in (background.call_args.args[1], foreground.call_args.kwargs["env"]):
        assert environment["SPARKRUN_APPLICATION_PROFILE"] == profile.profile_ref
        assert environment["SPARKRUN_APPLICATION_CONFIG"] == str(config.config_path)


def test_worker_environment_and_fresh_api_initialization(profile, tmp_path):
    from sparkrun.core.config import SparkrunConfig
    from sparkrun.plugins.sparkroute.protocol import Binding, Request

    config = SparkrunConfig(tmp_path / "custom.yaml")
    config.set("features", {"gateway.sparkroute": False})
    config.save()
    request = Request("test", "ensure_ready", binding=Binding("catalog:missing", "fingerprint", ("lab",)), wait=False)
    with mock.patch.object(jobs.subprocess, "Popen", return_value=SimpleNamespace(pid=123)) as spawn:
        jobs.start_operation(request, sctx=SimpleNamespace(config=config))
    environment = spawn.call_args.kwargs["env"]
    code = (
        "from sparkrun.application import initialize; import json, sys; "
        "from sparkrun.core.features import feature_gate_enabled; "
        "c=initialize(); print(json.dumps([c.application_profile.id,str(c.config.config_path),feature_gate_enabled('gateway.sparkroute',c.variables)])); "
        "assert 'click' not in sys.modules"
    )
    result = subprocess.run([sys.executable, "-c", code], env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [profile.id, str(config.config_path), False]


def test_legacy_host_preserves_environment(monkeypatch):
    monkeypatch.setattr(_application_profile, "host_api", lambda: None)
    monkeypatch.setenv("PRESERVE_TEST_SETTING", "value")
    assert _application_profile.child_environment()["PRESERVE_TEST_SETTING"] == "value"


def test_missing_host_api_cannot_silently_drop_child_identity(monkeypatch):
    missing = ModuleNotFoundError("legacy host", name="sparkrun.core.application_profile")
    with mock.patch.object(_application_profile.importlib, "import_module", side_effect=missing):
        monkeypatch.delenv("SPARKRUN_APPLICATION_PROFILE", raising=False)
        assert _application_profile.host_api() is None
        monkeypatch.setenv("SPARKRUN_APPLICATION_PROFILE", "example:PROFILE")
        with pytest.raises(RuntimeError, match="requires a host"):
            _application_profile.host_api()
