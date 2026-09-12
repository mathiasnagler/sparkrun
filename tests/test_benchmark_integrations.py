"""Generic benchmark extension contracts, independent of Arena's framework."""

import json
import os
import subprocess
import sys

import click
from click.testing import CliRunner
import pytest

from sparkrun.cli.ext import ExtensibleCommand
from sparkrun.core.benchmark_integrations import BenchmarkIntegration, register_benchmark_integration
from sparkrun.core.cli_registry import CliOptionSpec, register_cli_options, registered_cli_options
from sparkrun.core.installed_plugins import PluginConflictError, registration_transaction


def test_failed_plugin_registration_rolls_back_options_and_hooks():
    from scitrera_app_framework import Variables
    from sparkrun.core.benchmark_integrations import registered_benchmark_integrations

    with pytest.raises(RuntimeError):
        with registration_transaction(Variables()):
            register_cli_options(CliOptionSpec("failing", "test.target", lambda: [], lambda v: v))
            register_benchmark_integration(BenchmarkIntegration("failing"))
            raise RuntimeError("plugin failed")
    assert not registered_cli_options("test.target")
    assert "failing" not in registered_benchmark_integrations()


def test_duplicate_integration_provider_is_rejected(monkeypatch):
    monkeypatch.setattr("sparkrun.core.benchmark_integrations._INTEGRATIONS", {})
    first = BenchmarkIntegration("example")
    register_benchmark_integration(first)
    register_benchmark_integration(first)
    with pytest.raises(PluginConflictError):
        register_benchmark_integration(BenchmarkIntegration("example", on_complete=lambda ctx: None))


@pytest.mark.parametrize("declarations", [["--profile", "plugin_profile"], ["--new-name", "profile"], ["--help", "plugin_help"]])
def test_cli_option_collision_blocks_before_callback(monkeypatch, declarations):
    monkeypatch.setattr("sparkrun.core.cli_registry._CLI_OPTIONS", {})
    register_cli_options(CliOptionSpec("conflict", "test.target", lambda: [click.Option(declarations)], lambda v: v))
    called = []

    @click.command(cls=ExtensibleCommand, extension_target="test.target")
    @click.option("--profile")
    def command(**kwargs):
        called.append(kwargs)

    result = CliRunner().invoke(command, ["--help"])
    assert result.exit_code == 2
    assert "conflicting option" in result.output
    assert not called


@pytest.mark.parametrize("enabled", [False, True])
def test_gating_help_completion_and_console_free_bootstrap(tmp_path, enabled):
    config = tmp_path / ".config/sparkrun"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("features:\n  integration.arena: " + str(enabled).lower())
    (config / "registries.yaml").write_text("registries: []\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SPARKRUN_", "ALTERNATE_", "XDG_", "STATEFUL_"))}
    env.update(HOME=str(tmp_path), SPARKRUN_NO_TELEMETRY="1", SPARKRUN_NO_EXTERNAL_PLUGINS="1", SPARKRUN_NO_INSTALLED_PLUGINS="1")
    code = """
import json, sys
from sparkrun.core.bootstrap import init_sparkrun
import sparkrun.core.registry as registry
registry.BOOTSTRAP_REGISTRY_URLS = []
registry.RegistryManager._clone_or_pull = lambda *args: False
init_sparkrun()
assert 'click' not in sys.modules
loaded = 'sparkrun.plugins.sparkarena' in sys.modules
from click.testing import CliRunner
from sparkrun.cli import main
runner = CliRunner()
help_result = runner.invoke(main, ['--help'])
assert help_result.exit_code == 0, help_result.output
option_help = runner.invoke(main, ['benchmark', 'perf', '--help'])
assert option_help.exit_code == 0, option_help.output
completion = runner.invoke(main, [], prog_name='sparkrun', env={
    '_SPARKRUN_COMPLETE': 'bash_complete', 'COMP_WORDS': 'sparkrun benchmark perf --ar', 'COMP_CWORD': '3',
})
assert completion.exit_code == 0, completion.output
arena = runner.invoke(main, ['arena', '--help'])
flag = runner.invoke(main, ['benchmark', 'perf', 'r', '--arena', '--help'])
print(json.dumps(dict(loaded=loaded, root=help_result.output, options=option_help.output,
                     completion=completion.output, arena_exit=arena.exit_code, flag_exit=flag.exit_code)))
"""
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=40)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["loaded"] is enabled
    assert ("  arena " in result["root"]) is enabled
    assert ("--arena" in result["options"]) is enabled
    assert ("plain,--arena" in result["completion"]) is enabled
    assert result["arena_exit"] == (0 if enabled else 2)
    assert result["flag_exit"] == (0 if enabled else 2)


def test_context_lifecycle_uses_detached_state_snapshots(tmp_path, monkeypatch):
    from sparkrun.api import BenchmarkOptions
    from sparkrun.api._benchmark import _NullProgressEmitter
    from sparkrun.api._context import default_sctx
    from sparkrun.benchmarking.base import BenchmarkExecution
    from sparkrun.benchmarking.run_state import BenchmarkRunState
    from sparkrun.core.benchmark_integrations import BenchmarkIntegrationSession

    monkeypatch.setattr("sparkrun.core.benchmark_integrations._INTEGRATIONS", {})
    phases = []

    def prepare(options, context):
        assert context.result is context.state is None
        phases.append("prepare")
        return options

    def bound(context):
        assert context.result.benchmark_id == "bench_context"
        assert context.result.recipe_yaml is None
        assert context.result.framework == ""
        assert not hasattr(context.result, "launch_result")
        assert not hasattr(context.state, "save")
        with pytest.raises(TypeError):
            context.state.extras["new"] = True
        context.state.extras["legacy"]["nested"] = "changed copy"
        context.data["bound"] = True
        phases.append("bind")

    def complete(context):
        assert context.state.extras["legacy"]["nested"] == "original"
        assert context.data["bound"]
        phases.append("complete")

    register_benchmark_integration(BenchmarkIntegration("example", prepare=prepare, on_bind=bound, on_complete=complete))
    options = BenchmarkOptions(recipe="r", integrations={"example": {}})
    session = BenchmarkIntegrationSession(options, sctx=default_sctx(), emitter=_NullProgressEmitter())
    state = BenchmarkRunState(
        benchmark_id="bench_context",
        cluster_id="job",
        recipe_qualified_name="r",
        framework="fw",
        profile=None,
        base_args={},
        schedule=[],
        extras={"legacy": {"nested": "original"}},
    )
    session.prepare()
    session.bind(BenchmarkExecution(benchmark_id="bench_context", success=True), state, resumed=True)
    session.complete()
    assert phases == ["prepare", "bind", "complete"]
    assert state.extras["legacy"] == {"nested": "original"}


@pytest.mark.parametrize("answer", [True, False, None])
def test_integration_confirmation_uses_typed_decision_callback(answer):
    from sparkrun.api import BenchmarkDecision
    from sparkrun.api._benchmark import _CallbackProgressEmitter

    requests, events = [], []

    def decide(request):
        requests.append(request)
        return answer

    emitter = _CallbackProgressEmitter(events.append, decide if answer is not None else None)
    assert emitter.confirm("Publish with warnings?", default=False) is bool(answer)
    assert not events  # decisions are separate from progress output
    assert requests == ([BenchmarkDecision("integration_confirmation", "Publish with warnings?", False)] if answer is not None else [])
