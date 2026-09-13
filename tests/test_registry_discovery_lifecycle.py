"""Explicit refresh retries discovery; local edits and previews stay offline."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from sparkrun import api
from sparkrun.application import ApplicationProfile, initialize


@pytest.fixture(params=["sparkrun", "alternate"])
def discovery(request, tmp_path, monkeypatch):
    import sparkrun.core.registry as registry

    urls = ["https://example.invalid/first.git", "https://example.invalid/second.git"]
    monkeypatch.setattr(registry, "BOOTSTRAP_REGISTRY_URLS", urls)
    profile = None
    if request.param == "alternate":
        profile = ApplicationProfile(
            id="discovery-test",
            display_name="Discovery test",
            command="discovery-test",
            package="discovery-test",
            registries=(),
            bootstrap_registry_urls=tuple(urls),
        )
    config = tmp_path / "isolated-config.yaml"
    config.write_text(yaml.safe_dump({"cache_dir": str(tmp_path / "cache")}))
    context = initialize(profile, config_path=config)
    available = set()
    calls = []

    def run(command, *args, **kwargs):
        if command[:2] == ["git", "clone"]:
            url = command[-2]
            calls.append(url)
            if url not in available:
                return CompletedProcess(command, 1, stdout="", stderr="simulated unavailable source")
            name = "first" if url == urls[0] else "second"
            path = Path(command[-1]) / ".sparkrun" / "registry.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump({"registries": [{"name": name, "recipes": "recipes"}]}))
        else:
            assert "sparse-checkout" in command
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(registry.subprocess, "run", run)
    monkeypatch.setattr(registry.RegistryManager, "_clone_or_pull", lambda self, entry: True)
    return context, urls, available, calls


def test_failed_search_can_be_retried_by_explicit_refresh(discovery):
    context, urls, available, calls = discovery
    assert api.search_recipes(include_local=False, sctx=context) == []
    assert calls == urls
    api.search_recipes(include_local=False, sctx=context)
    assert calls == urls  # automatic reads do not retry a failed bootstrap
    available.update(urls)
    result = api.refresh_registries(sctx=context)
    assert result["updated"]["first"] and result["updated"]["second"]
    assert result["failed"] == []
    assert calls == urls + urls


def test_explicit_failure_is_not_an_intentionally_empty_catalog(discovery):
    context, urls, _, calls = discovery
    for _ in range(2):
        if context.application_profile.id == "sparkrun":
            assert api.refresh_registries(sctx=context)["updated"]  # usable fallback inventory
        else:
            with pytest.raises(api.SparkrunError, match="discovery") as error:
                api.refresh_registries(sctx=context)
            assert error.value.__cause__ is not None
    assert calls == urls + urls


def test_partial_discovery_retry_survives_new_manager_and_local_edits(discovery):
    context, urls, available, calls = discovery
    available.add(urls[0])
    assert api.refresh_registries(sctx=context)["updated"]["first"]
    assert calls == urls
    api.configure_registry("disable", "first", sctx=context)
    assert calls == urls
    # A new context reuses the same binding but has a fresh manager.
    context = initialize()
    available.add(urls[1])
    result = api.refresh_registries(sctx=context)
    assert result["updated"]["second"]
    assert "first" not in result["updated"]
    assert calls == urls + [urls[1]]
    api.refresh_registries(sctx=context)
    assert calls == urls + [urls[1]]  # completed bootstrap sources stay completed


@pytest.mark.parametrize("inventory", [None, "[broken: yaml", "invalid", "[invalid]", "registries: [invalid]"])
def test_local_configuration_preserves_pending_discovery(discovery, inventory):
    context, urls, available, calls = discovery
    if inventory is not None:
        context.registry_manager._registries_path.write_text(inventory)
    api.configure_registry("add", "user", url="https://example.invalid/user.git", subpath="recipes", sctx=context)
    api.configure_registry("disable", "user", sctx=context)
    api.configure_registry("enable", "user", sctx=context)
    api.configure_registry("trust", "user", acknowledge_trust=True, sctx=context)
    api.configure_registry("untrust", "user", sctx=context)
    api.configure_registry("remove", "user", sctx=context)
    assert calls == []
    available.update(urls)
    result = api.refresh_registries(sctx=initialize())
    assert result["updated"]["first"] and result["updated"]["second"]
    assert "user" not in result["updated"]
    assert calls == urls


def test_empty_or_disabled_inventory_does_not_require_bootstrap(discovery):
    context, _, _, calls = discovery
    # Existing configuration without pending discovery is intentional inventory.
    context.registry_manager._registries_path.write_text("registries: []\n")
    assert api.refresh_registries(sctx=context) == {"updated": {}, "failed": []}
    assert calls == []
    api.configure_registry("add", "disabled", url="https://example.invalid/disabled.git", sctx=context)
    api.configure_registry("disable", "disabled", sctx=context)
    assert api.refresh_registries(sctx=context) == {"updated": {}, "failed": []}
    assert calls == []


@pytest.mark.parametrize("kind", ["reference", "absolute", "qualified"])
def test_catalog_preview_resolves_once_and_uses_shared_offline_trust(discovery, monkeypatch, kind):
    from unittest.mock import Mock
    import sparkrun.api._catalog as catalog_module
    from sparkrun.core.config import SparkrunConfig

    context, _, _, calls = discovery
    api.configure_registry("add", "cached", url="https://example.invalid/cached.git", subpath="recipes", sctx=context)
    api.configure_registry("trust", "cached", acknowledge_trust=True, sctx=context)
    path = context.registry_manager.cache_root / "cached" / "recipes" / "sample.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("sparkrun_version: '2'\nmodel: test/model\nruntime: vllm\ncontainer: test/image\n")
    page = api.catalog_recipes(sctx=context)
    selected = {"reference": page["recipes"][0]["reference"], "absolute": str(path), "qualified": "@cached/sample"}[kind]
    selection = Mock(wraps=catalog_module._selection)
    monkeypatch.setattr(catalog_module, "_selection", selection)

    def extra_manager(*a, **kw):
        pytest.fail("Catalog preview must use the supplied context's registry manager")

    monkeypatch.setattr(SparkrunConfig, "get_registry_manager", extra_manager)
    details = api.get_recipe_details(selected, sctx=context)
    assert details["trusted"] is True
    assert selection.call_count == 1
    assert calls == []
    selection.reset_mock()
    recipe, _ = api.resolve_catalog_recipe(details["reference"], sctx=context)
    assert recipe.source_registry == "cached"
    assert selection.call_count == 1
    api.retain_catalog_recipe(details["reference"], sctx=context)
    assert calls == []


def test_no_bootstrap_sources_is_valid_empty_catalog(tmp_path):
    profile = ApplicationProfile(id="empty-test", display_name="Empty", command="empty-test", package="empty-test", registries=())
    config = tmp_path / "isolated-config.yaml"
    config.write_text("{}")
    assert api.refresh_registries(sctx=initialize(profile, config_path=config)) == {"updated": {}, "failed": []}


@pytest.mark.parametrize("action", ["override", "remove"])
def test_late_discovery_preserves_user_choices(discovery, action):
    context, urls, available, calls = discovery
    api.configure_registry("add", "first", url="https://example.invalid/custom.git", subpath="custom", sctx=context)
    api.configure_registry("disable", "first", sctx=context)
    if action == "remove":
        api.configure_registry("remove", "first", sctx=context)
    assert calls == []
    available.update(urls)
    context = initialize()
    result = api.refresh_registries(sctx=context)
    assert result["updated"]["second"]
    assert "first" not in result["updated"]
    entries = {entry.name: entry for entry in context.registry_manager.list_registries(allow_discovery=False)}
    if action == "remove":
        assert "first" not in entries
    else:
        assert entries["first"].url == "https://example.invalid/custom.git"
        assert entries["first"].subpath == "custom"
        assert entries["first"].trusted is False
        assert entries["first"].enabled is False


def test_pending_marker_cannot_add_bootstrap_sources(discovery):
    context, _, _, calls = discovery
    context.registry_manager._registries_path.write_text(
        yaml.safe_dump(
            {
                "registries": [],
                "pending_bootstrap_urls": ["https://example.invalid/unconfigured.git"],
            }
        )
    )
    assert api.refresh_registries(sctx=context) == {"updated": {}, "failed": []}
    assert calls == []


def test_import_preview_resolution_and_retention_do_not_discover(discovery):
    context, _, _, calls = discovery
    details = api.import_recipe("sparkrun_version: '2'\nmodel: test/model\nruntime: vllm\ncontainer: test/image\n", sctx=context)
    assert details["trusted"] is False
    recipe, _ = api.resolve_catalog_recipe(details["reference"], sctx=context)
    assert recipe.is_url_sourced
    api.retain_catalog_recipe(details["reference"], sctx=context)
    assert calls == []


@pytest.mark.parametrize("name", [None, "first"])
def test_cli_update_discovers_once_and_surfaces_failure(discovery, monkeypatch, name):
    from click.testing import CliRunner
    from sparkrun.cli import _registry as cli_registry

    context, urls, available, calls = discovery
    monkeypatch.setattr(cli_registry, "_get_config_and_registry", lambda _: (context.config, context.registry_manager))
    args = [name] if name else []
    runner = CliRunner()
    result = runner.invoke(cli_registry.registry_update, args)
    assert calls == urls
    if context.application_profile.id != "sparkrun" or name:
        assert result.exit_code == 1
        assert "Error:" in result.output
    else:
        assert result.exit_code == 0, result.output
    available.update(urls)
    result = runner.invoke(cli_registry.registry_update, args)
    assert result.exit_code == 0, result.output
    assert "Updating first... done" in result.output
    assert ("Updating second... done" in result.output) == (name is None)
    assert calls == urls + urls


def test_cli_update_keeps_named_disabled_check_offline(discovery, monkeypatch):
    from click.testing import CliRunner
    from sparkrun.cli import _registry as cli_registry

    context, _, _, calls = discovery
    api.configure_registry("add", "disabled", url="https://example.invalid/disabled.git", sctx=context)
    api.configure_registry("disable", "disabled", sctx=context)
    monkeypatch.setattr(cli_registry, "_get_config_and_registry", lambda _: (context.config, context.registry_manager))
    result = CliRunner().invoke(cli_registry.registry_update, ["disabled"])
    assert result.exit_code == 1
    assert "disabled; enable it before updating" in result.output
    assert calls == []
