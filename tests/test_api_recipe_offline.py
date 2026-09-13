"""Offline inventory policy must reach manifest discovery, including first use."""

from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

from sparkrun import api
from sparkrun.application import ApplicationProfile, initialize
from sparkrun.core.registry import RegistryManager, application_profile_registries


@pytest.fixture(params=[False, True], ids=["sparkrun", "alternate"])
def offline_context(request, tmp_path, monkeypatch):
    url = "https://example.invalid/bootstrap.git"
    profile = None
    if request.param:
        profile = ApplicationProfile(
            id="offline-test",
            display_name="Offline test",
            command="offline-test",
            package="offline-test",
            registries=({"name": "alternate", "url": url, "subpath": "recipes"},),
            bootstrap_registry_urls=(url,),
        )
    monkeypatch.setattr("sparkrun.core.registry.BOOTSTRAP_REGISTRY_URLS", [url])
    config = tmp_path / "isolated-config.yaml"
    config.write_text(yaml.safe_dump({"cache_dir": str(tmp_path / "cache")}))
    context = initialize(profile, config_path=config)
    calls = []

    def run(command, *args, **kwargs):
        # Intercept the actual process boundary; manifest discovery does not
        # use _clone_or_pull. Simulate successful manifest discovery offline.
        calls.append(command)
        if command[:2] == ["git", "clone"]:
            manifest = Path(command[-1]) / ".sparkrun" / "registry.yaml"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(yaml.safe_dump({"registries": [{"name": "discovered", "recipes": "recipes"}]}))
        else:
            assert "sparse-checkout" in command
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("sparkrun.core.registry.subprocess.run", run)
    return context, calls, url


@pytest.mark.parametrize("inventory", ["absent", "cached", "malformed"])
def test_offline_search_and_catalog_use_local_inventory(offline_context, inventory):
    context, calls, _ = offline_context
    manager = context.registry_manager
    entry = replace(next(entry for entry in application_profile_registries() if entry.visible), declared_by="")
    if inventory == "cached":
        manager._save_registries([entry])
    elif inventory == "malformed":
        manager._registries_path.write_text("[invalid: yaml")
    recipe = manager.cache_root / entry.name / entry.subpath / "sample.yaml"
    recipe.parent.mkdir(parents=True)
    recipe.write_text("sparkrun_version: '2'\nmodel: test/model\nruntime: vllm\ncontainer: test/image\n")

    for query in (None, "@" + entry.name, "@" + entry.name + "/sample"):
        result = api.search_recipes(query, ensure_initialized=False, include_local=False, sctx=context)
        assert len(result) == 1
        assert result[0].registry == entry.name
    page = api.catalog_recipes(registry=entry.name, sctx=context)
    assert len(page["recipes"]) == 1
    assert page["recipes"][0]["reference"].startswith("catalog:")
    assert entry.name in [item["name"] for item in api.list_registries(sctx=context)]
    assert calls == []
    assert not manager._manifest_discovery_attempted


@pytest.mark.parametrize("action", ["initialize", "refresh"])
def test_offline_read_does_not_consume_later_discovery(offline_context, action):
    context, calls, url = offline_context
    manager = context.registry_manager
    assert api.search_recipes(ensure_initialized=False, include_local=False, sctx=context) == []
    assert calls == []
    assert not manager._registries_path.exists()
    if action == "initialize":
        api.search_recipes(include_local=False, sctx=context)
    else:
        api.refresh_registries(sctx=context)
    assert [cmd[-2] for cmd in calls if cmd[:2] == ["git", "clone"]] == [url]
    assert manager.get_registry("discovered", allow_discovery=False).url == url
    # Filter and search share the same manager; later searches reuse inventory.
    api.search_recipes("@discovered", include_local=False, sctx=context)
    assert len([cmd for cmd in calls if cmd[:2] == ["git", "clone"]]) == 1


def test_offline_filters_keep_unknown_disabled_and_conflict_errors(offline_context):
    context, calls, _ = offline_context
    manager = context.registry_manager
    entry = replace(next(entry for entry in application_profile_registries() if entry.visible), declared_by="")
    entry.enabled = False
    manager._save_registries([entry])
    for query, registry, reason in (("@missing", None, "unknown"), ("@" + entry.name, None, "disabled"), ("@a", "b", "conflict")):
        with pytest.raises(api.InvalidRegistryFilter) as error:
            api.search_recipes(query, registry=registry, ensure_initialized=False, sctx=context)
        assert error.value.reason == reason
    assert calls == []


def test_filter_and_search_reuse_context_manager(tmp_path, monkeypatch):
    config = tmp_path / "isolated-config.yaml"
    config.write_text("{}")
    context = initialize(config_path=config)
    manager = context.registry_manager
    manager._save_registries([])

    def extra_manager():
        pytest.fail("API must reuse the context registry manager")

    monkeypatch.setattr(context.config, "get_registry_manager", extra_manager)
    assert api.resolve_recipe_filter("plain", sctx=context) == (None, "plain")
    assert api.search_recipes(include_local=False, sctx=context) == []
    with pytest.raises(api.InvalidRegistryFilter):
        api.resolve_recipe_filter("@missing", sctx=context)


def test_manifest_network_guard_catches_unmocked_bootstrap(tmp_path):
    manager = RegistryManager(tmp_path / "config", tmp_path / "cache")
    with pytest.raises(pytest.fail.Exception, match="Unmocked Git network operation"):
        manager._discover_manifest_entries("https://example.invalid/bootstrap.git")
