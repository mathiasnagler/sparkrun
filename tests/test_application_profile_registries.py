"""An application owns its default catalog; Spark defaults cannot leak into Alternate application."""

from dataclasses import replace

import pytest
import yaml

from sparkrun.core.application_profile import ApplicationProfile, SPARKRUN, select_application_profile
from sparkrun.core.registry import (
    RegistryEntry,
    RegistryManager,
    application_profile_registries,
    application_profile_bootstrap_urls,
)

ALTERNATE_REGISTRY = {
    "name": "alternate-recipes",
    "url": "https://example.test/alternate-recipes.git",
    "subpath": "recipes",
    "benchmark_subpath": "benchmarking",
    "trusted": True,
}


def select(**kwargs):
    return select_application_profile(
        ApplicationProfile(
            id="alternate-test",
            display_name="Alternate test",
            command="alternate-test",
            package="alternate-test",
            **kwargs,
        )
    )


@pytest.mark.parametrize("catalog", [(), (ALTERNATE_REGISTRY,)])
def test_profile_catalog_replaces_spark_defaults_on_first_run_restore_and_reset(tmp_path, monkeypatch, catalog):
    select(registries=catalog)
    monkeypatch.setattr("sparkrun.core.registry.BOOTSTRAP_REGISTRY_URLS", ["https://example.test/spark-only.git"])

    def discover(*args):
        pytest.fail("Alternate application must not discover Sparkrun bootstrap sources")

    monkeypatch.setattr(RegistryManager, "_discover_manifest_entries", discover)
    expected = [r["name"] for r in catalog]
    for _ in range(2):  # new manager instances must resolve the same policy
        manager = RegistryManager(tmp_path / "config", tmp_path / "cache")
        assert [e.name for e in manager.list_registries()] == expected
        assert manager.restore_missing_defaults() == []
        assert [e.name for e in manager.reset_to_defaults()] == expected
        assert [e.name for e in manager.list_registries()] == expected
    assert application_profile_bootstrap_urls() == ()
    assert yaml.safe_load((tmp_path / "config/registries.yaml").read_text())["registries"] == []


def test_sparkrun_catalog_can_also_be_replaced_explicitly():
    select_application_profile(replace(SPARKRUN, registries=(), bootstrap_registry_urls=()))
    assert application_profile_registries() == []
    assert application_profile_bootstrap_urls() == ()


def test_sparkrun_keeps_its_current_catalog(monkeypatch):
    monkeypatch.setattr("sparkrun.core.registry.BOOTSTRAP_REGISTRY_URLS", ["https://example.test/core.git"])
    names = {entry.name for entry in application_profile_registries()}
    assert {"official", "eugr", "community", "atlas"} <= names
    assert application_profile_bootstrap_urls() == ["https://example.test/core.git"]


def test_only_profile_bootstrap_sources_are_discovered(tmp_path, monkeypatch):
    url = "https://example.test/alternate-recipes.git"
    select(registries=(ALTERNATE_REGISTRY,), bootstrap_registry_urls=(url,))
    seen = []

    def discover(manager, requested):
        seen.append(requested)
        return [RegistryEntry(**ALTERNATE_REGISTRY), RegistryEntry(name="extra-alternate", url=url, subpath="extra")]

    monkeypatch.setattr(RegistryManager, "_discover_manifest_entries", discover)
    manager = RegistryManager(tmp_path / "config", tmp_path / "cache")
    entries = {entry.name: entry for entry in manager.list_registries()}
    assert seen == [url]
    assert set(entries) == {"alternate-recipes", "extra-alternate"}
    assert entries["alternate-recipes"].trusted
    assert not entries["extra-alternate"].trusted  # the manifest cannot grant itself trust


def test_empty_catalog_retains_explicit_user_registries(tmp_path):
    select(registries=())
    manager = RegistryManager(tmp_path / "config", tmp_path / "cache")
    manager.add_registry(RegistryEntry(name="my-recipes", url="https://example.test/user-recipes.git", subpath="recipes"))
    assert [e.name for e in RegistryManager(tmp_path / "config", tmp_path / "cache").list_registries()] == ["my-recipes"]
    assert manager.restore_missing_defaults() == []


def test_disabling_removing_and_customizing_profile_registry_remain_user_choices(tmp_path):
    select(registries=(ALTERNATE_REGISTRY,))
    manager = RegistryManager(tmp_path / "config", tmp_path / "cache")
    manager.disable_registry("alternate-recipes")
    manager.untrust_registry("alternate-recipes")
    entry = RegistryManager(tmp_path / "config", tmp_path / "cache").get_registry("alternate-recipes")
    assert not entry.enabled and not entry.trusted
    manager.remove_registry("alternate-recipes")
    assert manager.list_registries() == []
    assert manager.restore_missing_defaults() == []
    assert [e.name for e in manager.reset_to_defaults()] == ["alternate-recipes"]
    assert manager.get_registry("alternate-recipes").trusted


@pytest.mark.parametrize("value", ["official", {"name": "r", "url": "https://example.test/r"}, ("r",)])
def test_registry_catalog_requires_sequence_of_mappings(value):
    with pytest.raises(TypeError, match="registries"):
        select(registries=value)


def test_registry_catalog_rejects_ambiguous_duplicate_names():
    with pytest.raises(ValueError, match="Duplicate.*registry"):
        select(registries=(ALTERNATE_REGISTRY, ALTERNATE_REGISTRY))


@pytest.mark.parametrize("value,error", [("https://example.test/repo", TypeError), (("",), ValueError), ((None,), TypeError)])
def test_bootstrap_urls_require_sequence_of_nonempty_strings(value, error):
    with pytest.raises(error, match="bootstrap_registry_urls"):
        select(bootstrap_registry_urls=value)


def test_cli_identifies_application_owned_registry(tmp_path, monkeypatch):
    select(registries=(ALTERNATE_REGISTRY,))
    from click.testing import CliRunner
    from sparkrun.cli import _registry
    from types import SimpleNamespace

    manager = RegistryManager(tmp_path / "config", tmp_path / "cache")
    monkeypatch.setattr(_registry, "_get_config_and_registry", lambda *a: (SimpleNamespace(), manager))
    shown = CliRunner().invoke(_registry.registry_show, ["alternate-recipes"])
    assert shown.exit_code == 0, shown.output
    assert "application profile 'alternate-test'" in shown.output
    listed = CliRunner().invoke(_registry.registry_list)
    assert listed.exit_code == 0, listed.output
    assert "profile:alternat" in listed.output  # Source column is truncated to 16 characters.
    assert "plugin:distribution" not in listed.output
