"""Catalog source identity and per-operation inventory boundaries."""

from unittest.mock import patch

import pytest
import yaml

from sparkrun import api
from sparkrun.application import ApplicationProfile, initialize
from sparkrun.core.launcher import resolve_recipe_trust
from sparkrun.core.registry import RegistryEntry


@pytest.fixture(params=["sparkrun", "alternate"])
def context(request, tmp_path):
    profile = None
    if request.param == "alternate":
        profile = ApplicationProfile(
            id="catalog-test", display_name="Catalog", command="catalog-test", package="catalog-test", registries=()
        )
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"cache_dir": str(tmp_path / "cache")}))
    return initialize(profile, config_path=config)


def recipe_data(**extra):
    return {"sparkrun_version": "2", "model": "test/model", "runtime": "sglang", "container": "test/image", **extra}


def shared_cache(context, *, overlapping=False):
    manager = context.registry_manager
    url = "https://example.invalid/shared.git"
    entries = [
        RegistryEntry(name="first", url=url, subpath="first"),
        RegistryEntry(name="second", url=url, subpath="first" if overlapping else "second", trusted=overlapping),
    ]
    manager._save_registries(entries, pending_bootstrap_urls=[])
    for entry in entries:
        directory = manager._clone_dir_for_url(url) / entry.subpath
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "sample.yaml").write_text(
            yaml.safe_dump(recipe_data(pre_exec=["echo needs-trust"], executor_config={"privileged": True}))
        )
        manager._link_registry_to_shared(entry)
    return entries


@pytest.mark.parametrize("kind", ["reference", "qualified", "canonical", "alias"])
def test_shared_cache_preserves_untrusted_source(context, kind):
    entries = shared_cache(context)
    row = api.catalog_recipes(registry="first", sctx=context)["recipes"][0]
    reference = {
        "reference": row["reference"],
        "qualified": "@first/sample",
        "canonical": row["source_path"],
        "alias": str(context.registry_manager._cache_dir("first") / "first/sample.yaml"),
    }[kind]
    details = api.get_recipe_details(reference, sctx=context)
    recipe, _ = api.resolve_catalog_recipe(reference, sctx=context)
    assert details["registry"] == recipe.source_registry == "first"
    assert recipe.source_registry_url == entries[0].url
    assert details["trusted"] is False
    assert resolve_recipe_trust(recipe, False, sctx=context) is False
    assert sum(issue["code"] == "recipe_trust_required" for issue in details["issues"]) == 2
    assert api.get_recipe_details(details["reference"], sctx=context)["trusted"] is False


def test_overlapping_sources_require_identity_for_canonical_path(context):
    shared_cache(context, overlapping=True)
    first = api.get_recipe_details("@first/sample", sctx=context)
    second = api.get_recipe_details("@second/sample", sctx=context)
    assert first["trusted"] is False and second["trusted"] is True
    assert first["reference"] != second["reference"]
    with pytest.raises(api.RecipeNotFound):
        api.get_recipe_details(first["source_path"], sctx=context)
    alias = context.registry_manager._cache_dir("first") / "first/sample.yaml"
    assert api.get_recipe_details(str(alias), sctx=context)["trusted"] is False


@pytest.mark.parametrize("action", ["disable", "remove"])
def test_cache_cannot_become_local_after_registry_unavailable(context, action):
    shared_cache(context)
    row = api.catalog_recipes(registry="first", sctx=context)["recipes"][0]
    alias = context.registry_manager._cache_dir("first") / "first/sample.yaml"
    api.configure_registry(action, "first", sctx=context)
    for selected in (row["reference"], row["source_path"], str(alias), "@first/sample"):
        with pytest.raises(api.RecipeNotFound):
            api.get_recipe_details(selected, sctx=context)


def test_local_symlink_and_legacy_reference_cannot_erase_registry_identity(context):
    import json
    import sparkrun.api._catalog as catalog

    shared_cache(context)
    source = context.registry_manager._cache_dir("first") / "first/sample.yaml"
    root = context.config.config_path.parent / "recipes"
    root.mkdir()
    (root / "alias.yaml").symlink_to(source)
    row = api.catalog_recipes(local_only=True, sctx=context)["recipes"][0]
    assert row["registry"] == "first"
    assert api.get_recipe_details(row["reference"], sctx=context)["trusted"] is False
    old = catalog._root(context) / "references" / ("0" * 32 + ".json")
    old.write_text(json.dumps({"path": str(source.resolve()), "registry": None, "imported": False}))
    with pytest.raises(api.RecipeNotFound):
        api.get_recipe_details("catalog:" + "0" * 32, sctx=context)


@pytest.mark.parametrize("count", [1, 30])
def test_inventory_loaded_once_per_operation_and_refreshed_between_calls(context, count):
    manager = context.registry_manager
    entry = RegistryEntry(name="cached", url="https://example.invalid/cached.git", subpath="recipes")
    manager._save_registries([entry], pending_bootstrap_urls=[])
    root = manager._cache_dir(entry.name) / entry.subpath
    root.mkdir(parents=True)
    for index in range(count):
        (root / ("item-%d.yaml" % index)).write_text(yaml.safe_dump(recipe_data()))
    with patch.object(manager, "_load_registries", wraps=manager._load_registries) as loads:
        page = api.catalog_recipes(registry="cached", limit=1, sctx=context)
    assert page["total"] == count and len(page["recipes"]) == 1
    assert loads.call_count == 1
    reference = page["recipes"][0]["reference"]
    for operation in (api.get_recipe_details, api.resolve_catalog_recipe, api.retain_catalog_recipe):
        with patch.object(manager, "_load_registries", wraps=manager._load_registries) as loads:
            operation(reference, sctx=context)
        assert loads.call_count == 1
    assert api.get_recipe_details(reference, sctx=context)["trusted"] is False
    api.configure_registry("trust", "cached", acknowledge_trust=True, sctx=context)
    assert api.get_recipe_details(reference, sctx=context)["trusted"] is True
    entry.url = "https://example.invalid/replaced.git"
    manager._save_registries([entry], pending_bootstrap_urls=[])
    with pytest.raises(api.RecipeNotFound):
        api.get_recipe_details(reference, sctx=context)


def test_catalog_and_regular_loader_share_benchmark_attribution(context):
    from sparkrun.api._resolve import resolve_recipe
    from sparkrun.benchmarking.base import BenchmarkExecution

    shared_cache(context)
    normal = resolve_recipe("@first/sample", sctx=context)
    reference = api.catalog_recipes(registry="first", sctx=context)["recipes"][0]["reference"]
    selected, _ = api.resolve_catalog_recipe(reference, sctx=context)
    assert selected.source_registry_url == normal.source_registry_url
    execution = BenchmarkExecution(recipe=selected, recipe_name=selected.qualified_name, overrides={})
    assert execution.generate_metadata(resolve_image=False)["recipe"]["registry_git"] == normal.source_registry_url


def test_regular_api_does_not_ignore_ambiguous_cache_ownership(context):
    from sparkrun.api._resolve import resolve_recipe

    entries = shared_cache(context, overlapping=True)
    path = context.registry_manager._clone_dir_for_url(entries[0].url) / "first/sample.yaml"
    with pytest.raises(api.RecipeNotFound):
        resolve_recipe(str(path), sctx=context)


def test_disabling_registry_revokes_automatic_trust_of_resolved_recipe(context):
    shared_cache(context)
    api.configure_registry("trust", "first", acknowledge_trust=True, sctx=context)
    recipe, _ = api.resolve_catalog_recipe("@first/sample", sctx=context)
    assert resolve_recipe_trust(recipe, False, sctx=context)
    api.configure_registry("disable", "first", sctx=context)
    assert not resolve_recipe_trust(recipe, False, sctx=context)
