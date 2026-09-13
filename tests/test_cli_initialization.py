"""CLI context and extension loading share the application initializer."""

from unittest.mock import Mock

import click
import pytest
from click.testing import CliRunner

from sparkrun.application import initialize
from sparkrun.cli._common import _get_context
from sparkrun.cli.ext import ExtensibleCommand, PluggableGroup
from sparkrun.core import bootstrap, cli_registry


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "custom" / "config.yaml"
    path.parent.mkdir()
    path.write_text("review:\n  marker: custom\n")
    return path


@pytest.mark.parametrize("verbosity", [-1, 0, True, 2, 4])
def test_cli_context_uses_public_initializer_and_keeps_progress(config_path, monkeypatch, verbosity):
    factory = Mock(wraps=initialize)
    monkeypatch.setattr("sparkrun.application.initialize", factory)
    registered = []
    monkeypatch.setattr(bootstrap, "_register_plugins", lambda v, *, config: registered.append(config.config_path))
    ctx = click.Context(click.Command("review"), obj={"config_path": config_path, "verbose": verbosity})
    context = _get_context(ctx)
    assert context.config.config_path == config_path
    assert context.config.get("review.marker") == "custom"
    assert context.verbose == (verbosity > 0)
    assert context.progress.verbosity == max(0, min(int(verbosity), 3))
    assert registered == [config_path]
    assert _get_context(ctx) is context
    factory.assert_called_once_with(config_path=config_path)


def test_command_discovery_binds_config_before_plugins_and_verbosity_afterward(config_path, monkeypatch):
    monkeypatch.setattr(cli_registry, "_CLI_EXTENSIONS", [])
    observed = []
    contexts = []

    def command():
        context = _get_context(click.get_current_context())
        contexts.append(context)
        click.echo(context.config.get("review.marker"))

    def register(v, *, config):
        observed.append(config.config_path)
        cli_registry.register_cli_command(click.Command("contributed", callback=command))

    monkeypatch.setattr(bootstrap, "_register_plugins", register)

    @click.group(cls=PluggableGroup)
    @click.option("-v", count=True)
    @click.pass_context
    def root(ctx, v):
        ctx.ensure_object(dict)["verbose"] = v

    result = CliRunner().invoke(root, ["-vv", "contributed"], obj={"config_path": config_path})
    assert result.exit_code == 0, result.output
    assert result.stdout == "custom\n"
    assert observed == [config_path]
    assert contexts[0].config.config_path == config_path
    assert contexts[0].progress.verbosity == 2


def test_standalone_option_discovery_uses_the_same_config_binding(config_path, monkeypatch):
    monkeypatch.setattr(cli_registry, "_CLI_OPTIONS", {})
    observed = []

    def register(v, *, config):
        observed.append(config.config_path)
        cli_registry.register_cli_options(
            cli_registry.CliOptionSpec(
                owner="review", target="review", loader=lambda: [click.Option(["--sample"], is_flag=True)], decode=lambda raw: raw
            )
        )

    def command(sample):
        assert sample is True
        assert _get_context(click.get_current_context()).config.config_path == config_path

    monkeypatch.setattr(bootstrap, "_register_plugins", register)
    root = ExtensibleCommand("review", extension_target="review", callback=command)
    result = CliRunner().invoke(root, ["--sample"], obj={"config_path": config_path})
    assert result.exit_code == 0, result.output
    assert observed == [config_path]


def test_cli_context_rejects_a_different_configuration_binding(config_path, tmp_path):
    first = _get_context(click.Context(click.Command("first"), obj={"config_path": config_path}))
    with pytest.raises(click.UsageError, match="another configuration path"):
        _get_context(click.Context(click.Command("second"), obj={"config_path": tmp_path / "other.yaml"}))
    assert first.config.config_path == config_path


@pytest.mark.parametrize("extensions_loaded", [False, True])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize(
    "args",
    [
        ["proxy", "alias", "add", "friendly", "model"],
        ["proxy", "alias", "remove", "friendly"],
        ["proxy", "sync"],
        ["proxy", "stop"],
        ["proxy", "status"],
    ],
)
def test_every_cli_invocation_rejects_conflicting_config(config_path, tmp_path, monkeypatch, extensions_loaded, cached, args):
    from sparkrun.cli import main

    context = initialize(config_path=config_path)
    other = tmp_path / "other" / "config.yaml"
    obj = {"config_path": other}
    if cached:
        obj["sparkrun_ctx"] = context
    monkeypatch.setattr(main, "_cli_ext_loaded", extensions_loaded, raising=False)
    result = CliRunner().invoke(main, args, obj=obj)
    assert result.exit_code == 2, result.output
    assert "another configuration path" in result.stderr
    assert not (config_path.parent / "proxy.yaml").exists()
    assert not other.parent.exists()


@pytest.mark.parametrize("extensions_loaded", [False, True])
def test_public_launcher_rejects_rebinding(config_path, tmp_path, monkeypatch, extensions_loaded):
    from sparkrun.application import run_cli
    from sparkrun.cli import main

    initialize(config_path=config_path)
    monkeypatch.setattr(main, "_cli_ext_loaded", extensions_loaded, raising=False)
    with pytest.raises(click.UsageError, match="another configuration path"):
        run_cli(args=["proxy", "alias", "add", "friendly", "model"], obj={"config_path": tmp_path / "other.yaml"}, standalone_mode=False)
    assert not (config_path.parent / "proxy.yaml").exists()


@pytest.mark.parametrize("initialized", [False, True])
def test_reused_command_tree_honors_explicit_first_and_same_bindings(config_path, monkeypatch, initialized):
    from sparkrun.cli import main
    from sparkrun.proxy.supervisor import GatewaySupervisor
    import yaml

    if initialized:
        initialize(config_path=config_path)
    monkeypatch.setattr(main, "_cli_ext_loaded", True, raising=False)
    monkeypatch.setattr(GatewaySupervisor, "is_running", lambda self: False)
    result = CliRunner().invoke(main, ["proxy", "alias", "add", "friendly", "model"], obj={"config_path": config_path})
    assert result.exit_code == 0, result.output
    assert yaml.safe_load((config_path.parent / "proxy.yaml").read_text())["aliases"] == {"friendly": "model"}


def test_explicit_binding_survives_plugin_failure_and_allows_process_recovery(config_path, tmp_path, monkeypatch):
    from sparkrun.cli import main
    from sparkrun.proxy.supervisor import GatewaySupervisor

    monkeypatch.setattr(main, "_cli_ext_loaded", False, raising=False)
    monkeypatch.setattr(bootstrap, "_register_plugins", Mock(side_effect=RuntimeError("plugin failed")))
    monkeypatch.setattr(GatewaySupervisor, "is_running", lambda self: False)
    result = CliRunner().invoke(main, ["proxy", "status"], obj={"config_path": config_path})
    assert result.exit_code == 0, result.output
    assert bootstrap._initialization_error is not None
    again = CliRunner().invoke(main, ["proxy", "status"], obj={"config_path": tmp_path / "other.yaml"})
    assert again.exit_code == 2
    assert "another configuration path" in again.stderr
