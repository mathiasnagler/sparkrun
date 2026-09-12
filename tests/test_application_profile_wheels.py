"""Opt-in offline wheel composition proof: SPARKRUN_TEST_WHEELS=1 pytest ... ."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SPARKRUN_TEST_WHEELS") != "1", reason="set SPARKRUN_TEST_WHEELS=1 for offline wheel installation tests"
)
ROOT = Path(__file__).resolve().parents[1]
CORE_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


@pytest.fixture(scope="module")
def wheels(tmp_path_factory):
    uv = shutil.which("uv")
    if not uv:
        pytest.fail("uv is required for wheel composition tests")
    root = tmp_path_factory.mktemp("application-profile-wheels")
    output = root / "wheels"

    def run(args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=120, cwd=root)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    for source in (
        ROOT,
        ROOT / "tests/fixtures/application_profiles/plugin",
        ROOT / "tests/fixtures/application_profiles/application",
    ):
        # Building through an sdist excludes stale local build/ output.
        run([uv, "build", "--offline", "--out-dir", str(output), str(source)])
    environment = root / "environment"
    run([uv, "venv", "--python", sys.executable, str(environment)])
    python = environment / "bin/python"
    core_wheel = next(output.glob("sparkrun-*.whl"))
    run([uv, "pip", "install", "--offline", "--python", str(python), str(core_wheel)])
    core = next(environment.glob("lib/python*/site-packages/sparkrun"))

    def digest():
        return {
            str(p.relative_to(core)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in core.rglob("*")
            if p.is_file() and p.suffix != ".pyc"
        }

    before = digest()
    plugin_wheel = next(output.glob("profile_test_plugin-*.whl"))
    run([uv, "pip", "install", "--offline", "--python", str(python), str(plugin_wheel)])
    run(
        [
            str(python),
            "-I",
            "-c",
            "import importlib.util; import profile_test_plugin; assert importlib.util.find_spec('profile_test_app') is None; "
            "from sparkrun.core.features import register_feature; "
            "[register_feature(flag) for flag in profile_test_plugin.FEATURE_DEFINITIONS]; "
            "profile_test_plugin.register(None); from sparkrun.core.features import get_feature; "
            "assert get_feature('test.installed_plugin') is not None",
        ]
    )
    app_wheel = next(output.glob("profile_test_app-*.whl"))
    run([uv, "pip", "install", "--offline", "--python", str(python), str(app_wheel)])
    assert digest() == before, "Downstream installation changed the upstream wheel's files"
    return root, environment, python


def invoke(wheels, tmp_path, *args, env_extra=None):
    _, environment, _ = wheels
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SPARKRUN_", "PROFILE_TEST_APP_")) and k not in {"PYTHONPATH", "STATEFUL_ROOT", "VIRTUAL_ENV"}
    }
    env.update(HOME=str(tmp_path), SPARKRUN_NO_TELEMETRY="1", SPARKRUN_NO_EXTERNAL_PLUGINS="1")
    env.update(env_extra or {})
    result = subprocess.run(
        [str(environment / "bin" / args[0]), *args[1:]], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    return result


def test_wheel_launchers_help_version_completion_and_isolation(wheels, tmp_path):
    for command in ("sparkrun", "profile-test-app"):
        result = invoke(wheels, tmp_path, command, "--help")
        assert result.returncode == 0, result.stderr
        assert "Usage: " + command in result.stdout
        result = invoke(wheels, tmp_path, command, "--version")
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith(command + ", version ")
        result = invoke(wheels, tmp_path, command, "setup", "version")
        assert result.returncode == 0, result.stderr
        assert "Application: " + command + " " in result.stdout
        assert "Channel:     stable" in result.stdout
        assert f"Core:        sparkrun {CORE_VERSION}" in result.stdout
        assert "Loaded plugins:" in result.stdout
        if command == "profile-test-app":
            assert "Profile:     profile-test-app" in result.stdout
            assert "profile-test-plugin: 0.1.0 (profile-test-plugin; installed; required)" in result.stdout
        completion = "_" + command.upper().replace("-", "_") + "_COMPLETE"
        result = invoke(wheels, tmp_path, command, env_extra={completion: "bash_source"})
        assert result.returncode == 0, result.stderr
        assert command in result.stdout
    code = """
import json
from sparkrun.application import initialize
c = initialize()
print(json.dumps({'config': str(c.config.config_path.parent), 'cache': str(c.config.cache_dir)}))
"""
    result = invoke(
        wheels,
        tmp_path,
        "python",
        "-c",
        code,
        env_extra={
            "SPARKRUN_APPLICATION_PROFILE": "profile_test_app.profile:PROFILE_TEST_APP",
            "SPARKRUN_CACHE_DIR": str(tmp_path / "forbidden"),
        },
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["config"] == str(tmp_path / ".config/profile-test-app")
    assert data["cache"] == str(tmp_path / ".cache/profile-test-app")
    assert not (tmp_path / "forbidden").exists()
    result = invoke(wheels, tmp_path, "profile-test-app", "setup", "version", "--json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["version"] == "1.0.0"
    assert data["distribution"]["id"] == "profile-test-app"
    assert data["core"]["version"] == CORE_VERSION
    plugin = next(p for p in data["plugins"] if p["name"] == "profile-test-plugin")
    assert plugin["package"] == "profile-test-plugin" and plugin["version"] == "0.1.0"
    assert plugin["module"] == "profile_test_plugin" and plugin["required"] and plugin["loaded"]


def test_same_wheel_plugin_explicitly_selected_in_sparkrun(wheels, tmp_path):
    config = tmp_path / ".config/sparkrun/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("integrations:\n  profile-test-plugin: true\n")
    result = invoke(wheels, tmp_path, "sparkrun", "setup", "plugins", "list", "--json")
    assert result.returncode == 0, result.stderr
    plugin = next(p for p in json.loads(result.stdout) if p["name"] == "profile-test-plugin")
    assert plugin["selected"] and plugin["loaded"] and plugin["failure"] is None
    config.write_text("integrations:\n  profile-test-plugin: false\n")
    code = "from sparkrun.application import initialize; import sys; initialize(); assert 'profile_test_plugin' not in sys.modules"
    result = invoke(wheels, tmp_path, "python", "-c", code)
    assert result.returncode == 0, result.stderr


def test_child_api_profile_is_reconstructed_from_installed_package(wheels, tmp_path):
    code = "from sparkrun.application import initialize; import sys; c=initialize(); print(c.application_profile.id); assert 'sparkrun.cli' not in sys.modules"
    result = invoke(
        wheels, tmp_path, "python", "-c", code, env_extra={"SPARKRUN_APPLICATION_PROFILE": "profile_test_app.profile:PROFILE_TEST_APP"}
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "profile-test-app"
    result = invoke(wheels, tmp_path, "python", "-m", "sparkrun", "--version")
    assert result.returncode == 0 and result.stdout.startswith("sparkrun, version"), result.stderr


def test_sparkroute_bridge_uses_installed_alternate_console_and_config(wheels, tmp_path):
    config = tmp_path / "custom-config.yaml"
    config.write_text("features:\n  gateway.sparkroute: true\n")
    code = """
import json, subprocess, sys
from sparkrun.application import initialize
from sparkrun.plugins.sparkroute.release import resolve_sparkrun_executable
from sparkrun.plugins.sparkroute._application_profile import child_environment
c = initialize()
assert 'click' not in sys.modules
console = resolve_sparkrun_executable()
assert console.endswith('/profile-test-app')
request = {'schema_version': 4, 'request_id': 'wheel-test', 'operation': 'catalog_registries'}
child = subprocess.run([console, 'gateway-bridge'], input=json.dumps(request), env=child_environment(c.config.config_path), capture_output=True, text=True, timeout=30)
assert child.returncode == 0, child.stderr
response = json.loads(child.stdout)
assert response['ok'], response
assert response['result']['registries'] == [], response
print(json.dumps({'distribution': c.application_profile.id, 'config': str(c.config.config_path), 'response': response}))
"""
    result = invoke(
        wheels,
        tmp_path,
        "python",
        "-c",
        code,
        env_extra={
            "SPARKRUN_APPLICATION_PROFILE": "profile_test_app.profile:PROFILE_TEST_APP",
            "SPARKRUN_APPLICATION_CONFIG": str(config),
        },
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["distribution"] == "profile-test-app"
    assert data["config"] == str(config)


def test_sparkroute_inventory_recognizes_verified_profile_support(wheels, tmp_path):
    result = invoke(
        wheels,
        tmp_path,
        "profile-test-app",
        "setup",
        "plugins",
        "list",
        "--json",
        env_extra={
            "PROFILE_TEST_APP_FEATURE_GATEWAY_SPARKROUTE": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    rows = data if isinstance(data, list) else data["plugins"]
    row = next(item for item in rows if item["name"] == "sparkroute")
    assert row["loaded"] and row["enabled"] and row["failure"] is None


def test_alternate_tune_default_can_be_overridden_without_affecting_sparkrun(wheels, tmp_path):
    disabled = invoke(wheels, tmp_path, "profile-test-app", "tune", "vllm", "--help")
    assert disabled.returncode == 1, disabled.stderr
    assert "profile-test-app setup features enable cli.tune" in disabled.stderr
    for command in ("sparkrun", "profile-test-app"):
        enabled = invoke(wheels, tmp_path, command, "tune", "vllm", "--help", env_extra={"PROFILE_TEST_APP_FEATURE_CLI_TUNE": "1"})
        assert enabled.returncode == 0, enabled.stderr
        assert "--tp" in enabled.stdout


def test_arena_plugin_profile_defaults_and_override(wheels, tmp_path):
    for command, enabled in (("sparkrun", True), ("profile-test-app", False)):
        result = invoke(wheels, tmp_path, command, "setup", "plugins", "list", "--json")
        assert result.returncode == 0, result.stderr
        plugins = {plugin["name"]: plugin for plugin in json.loads(result.stdout)}
        assert "arena" not in plugins
        plugin = plugins["sparkarena"]
        assert plugin["module"] == "sparkrun.plugins.sparkarena"
        assert plugin["loaded"] is enabled
        assert plugin["version"] == (CORE_VERSION if enabled else None)
        result = invoke(wheels, tmp_path, command, "--help")
        assert result.returncode == 0, result.stderr
        assert ("  arena " in result.stdout) is enabled
        result = invoke(wheels, tmp_path, command, "benchmark", "perf", "--help")
        assert result.returncode == 0, result.stderr
        assert ("--arena" in result.stdout) is enabled
        result = invoke(
            wheels,
            tmp_path,
            command,
            env_extra={
                "_" + command.upper().replace("-", "_") + "_COMPLETE": "bash_complete",
                "COMP_WORDS": command + " benchmark perf --ar",
                "COMP_CWORD": "3",
            },
        )
        assert result.returncode == 0, result.stderr
        assert ("plain,--arena" in result.stdout) is enabled
    result = invoke(
        wheels, tmp_path, "profile-test-app", "benchmark", "perf", "--help", env_extra={"PROFILE_TEST_APP_FEATURE_INTEGRATION_ARENA": "1"}
    )
    assert result.returncode == 0, result.stderr
    assert "--arena" in result.stdout
    assert "profile-test-app arena login" in result.stdout


def test_alternate_registry_commands_never_restore_spark_defaults(wheels, tmp_path):
    # A populated Sparkrun catalog beside the fresh Profile test application home must not leak.
    spark_config = tmp_path / ".config/sparkrun"
    spark_config.mkdir(parents=True)
    (spark_config / "registries.yaml").write_text(
        "config_version: 1\nregistries:\n- name: atlas\n  url: https://github.com/Atlas-Inf/sparkrun-recipes.git\n"
        "  subpath: recipes\n  trusted: true\n"
    )
    original = (spark_config / "registries.yaml").read_bytes()
    for args in (
        ("registry", "list", "--json"),
        ("registry", "revert-to-defaults", "--no-update"),
        ("registry", "update"),
        ("registry", "list", "--json"),
    ):
        result = invoke(wheels, tmp_path, "profile-test-app", *args)
        assert result.returncode == 0, result.stdout + result.stderr
        if args[1] == "list":
            assert json.loads(result.stdout) == []
        elif args[1] == "update":
            assert "No enabled registries to update" in result.stdout
        else:
            assert "0 entries" in result.stdout
    result = invoke(wheels, tmp_path, "profile-test-app", "registry", "list-benchmark-profiles", "--all")
    assert result.returncode == 0, result.stderr
    assert "No benchmark profiles found" in result.stdout
    assert (spark_config / "registries.yaml").read_bytes() == original


@pytest.mark.parametrize("command", ["sparkrun", "profile-test-app"])
@pytest.mark.parametrize("channel", ["stable", "beta", "alpha"])
def test_installed_k8s_plugin_defaults_and_explicit_opt_in(wheels, tmp_path, command, channel):
    config = tmp_path / ".config" / command / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"features": {"channel": channel}}))
    enabled = command == "sparkrun" and channel == "alpha"
    result = invoke(wheels, tmp_path, command, "setup", "plugins", "list", "--json")
    assert result.returncode == 0, result.stderr
    row = next(row for row in json.loads(result.stdout) if row["name"] == "k8s")
    assert row["loaded"] is enabled and row["enabled"] is enabled
    assert row["version"] == (CORE_VERSION if enabled else None)
    result = invoke(wheels, tmp_path, command, "setup", "features", "list", "--json")
    assert result.returncode == 0, result.stderr
    flags = {row["name"] for row in json.loads(result.stdout)}
    children = {"executor.k8s", "cli.setup.k8s", "api.run.k8s"}
    assert children & flags == (children if enabled else set())
    result = invoke(wheels, tmp_path, command, "setup", "k8s", "kubectl", "--list")
    assert result.returncode == (0 if enabled else 2), result.stdout + result.stderr
    result = invoke(
        wheels,
        tmp_path,
        command,
        "setup",
        "k8s",
        "kubectl",
        "--list",
        env_extra={command.upper().replace("-", "_") + "_FEATURE_INTEGRATION_K8S": "1"},
    )
    assert result.returncode == 0 and "No cached kubectl binaries" in result.stdout, result.stdout + result.stderr


def test_wheel_alternate_wizard_preview_and_visibility(wheels, tmp_path):
    result = invoke(
        wheels, tmp_path, "profile-test-app", "setup", "wizard", "--hosts", "192.0.2.1", "--cluster", "preview", "--dry-run", "--yes"
    )
    assert result.returncode == 0, result.stderr
    assert "no changes made" in result.stdout
    assert not list(tmp_path.rglob("preview.yaml"))
    result = invoke(wheels, tmp_path, "profile-test-app", "setup", "--help", env_extra={"PROFILE_TEST_APP_FEATURE_CLI_SETUP_WIZARD": "0"})
    assert result.returncode == 0, result.stderr
    assert "wizard" not in result.stdout
    result = invoke(wheels, tmp_path, "profile-test-app", "setup", env_extra={"PROFILE_TEST_APP_FEATURE_CLI_SETUP_WIZARD": "0"})
    assert result.returncode == 0, result.stderr
    assert "Setup and configuration commands" in result.stdout


def test_installed_plugin_observes_application_and_controller_identity(wheels, tmp_path):
    config = tmp_path / "site.yaml"
    config.write_text("integrations:\n  profile-test-plugin: true\n")
    other_config = tmp_path / "other.yaml"
    other_config.write_text(config.read_text())
    code = """
import json, sys
from sparkrun.application import initialize, get_controller_identity
context = initialize(config_path=sys.argv[1])
assert context.variables.get("test.plugin.application_identity") == context.application_identity
assert get_controller_identity() == context.controller_identity
print(json.dumps(context.controller_identity.to_dict()))
"""
    identities = []
    for profile in ("sparkrun.core.application_profile:SPARKRUN", "profile_test_app.profile:PROFILE_TEST_APP"):
        profile_identities = []
        for config_path in (config, other_config):
            result = invoke(
                wheels, tmp_path, "python", "-I", "-c", code, str(config_path), env_extra={"SPARKRUN_APPLICATION_PROFILE": profile}
            )
            assert result.returncode == 0, result.stderr
            profile_identities.append(json.loads(result.stdout))
        assert profile_identities[0] == profile_identities[1]
        identities.append(profile_identities[0])
    assert {item["application"]["id"] for item in identities} == {"sparkrun", "profile-test-app"}
    assert len({item["controller_id"] for item in identities}) == 2
    assert len(list(tmp_path.glob(".controllers/*.id"))) == 2
