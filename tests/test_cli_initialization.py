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
    with pytest.raises(RuntimeError, match="another configuration path"):
        _get_context(click.Context(click.Command("second"), obj={"config_path": tmp_path / "other.yaml"}))
    assert first.config.config_path == config_path
