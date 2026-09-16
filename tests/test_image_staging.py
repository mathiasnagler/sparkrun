"""Standalone consumers share distribution while requiring every image identity."""

from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from sparkrun.core.image_preparation import (
    ImagePreparationError,
    PreparedImageSet,
    prepare_images,
    resolve_content_images,
    stage_prepared_images,
)
from sparkrun.core.recipe import Recipe
from sparkrun.orchestration.primitives import build_ssh_kwargs

HOSTS = ["node-a", "node-b"]
PINNED = "registry.example/base@sha256:" + "a" * 64
IDS = {"node-a": "sha256:" + "b" * 64, "node-b": "sha256:" + "c" * 64}


def prepared_case(monkeypatch):
    recipe = Recipe.from_dict({"recipe_version": "2", "model": "org/model", "runtime": "vllm", "container": PINNED, "builder": "coldsnap"})

    class Builder:
        def prepare(self, image, recipe, hosts, **kwargs):
            return "local/coldsnap:built"

    class Runtime:
        supports_heterogeneous_images = True

        def resolve_container(self, recipe, **kwargs):
            return recipe.container

    monkeypatch.setattr("sparkrun.core.bootstrap.get_builder", lambda *args: Builder())
    prepared = prepare_images(recipe, Runtime(), HOSTS)
    config = SimpleNamespace(ssh_user="cluster-user", ssh_key="/key", ssh_options=[])
    return recipe, prepared, config


def test_staging_uses_prepared_policy_and_checks_every_host_before_models(monkeypatch):
    recipe, prepared, config = prepared_case(monkeypatch)
    original = deepcopy(recipe.distribution_config)
    policy = deepcopy(prepared.container_distribution)
    seen = []

    def inspect(host, command, **kwargs):
        assert "local/coldsnap:built" in command
        assert kwargs["ssh_kwargs"] == build_ssh_kwargs(config) and kwargs["timeout"] == 30
        seen.append(host)
        return SimpleNamespace(success=True, stdout=IDS[host], stderr="")

    def distribute(actual, image, hosts, cache, actual_config, dry_run, **kwargs):
        assert actual is recipe and actual_config is config
        assert image == "local/coldsnap:built" and hosts == HOSTS
        assert kwargs["container_distribution"] is prepared.container_distribution
        assert not kwargs["skip_model"] and not kwargs["skip_container"]
        kwargs["after_container_sync"]()
        assert set(seen) == set(HOSTS)
        return "comm", {"ib": "address"}, {}, {}

    monkeypatch.setattr("sparkrun.orchestration.primitives.run_command_on_host", inspect)
    monkeypatch.setattr("sparkrun.orchestration.distribution.distribute_from_config", distribute)
    result = stage_prepared_images(prepared, recipe, HOSTS, "/cache", config, require_content_ids=True, stage_models=True)
    assert result.content_images_by_node == tuple(IDS.values()) and result.comm_env == "comm"
    assert recipe.distribution_config == original and prepared.container_distribution == policy


@pytest.mark.parametrize("bad", ["", "sha256:short", "sha256:" + "z" * 64])
def test_missing_or_invalid_second_host_aborts_before_model_transfer(monkeypatch, bad):
    recipe, prepared, config = prepared_case(monkeypatch)

    def inspect(host, *_args, **_kwargs):
        return SimpleNamespace(success=True, stdout=IDS[host] if host == HOSTS[0] else bad, stderr="")

    def distribute(*args, **kwargs):
        kwargs["after_container_sync"]()
        pytest.fail("model transfer must not start without every image identity")

    monkeypatch.setattr("sparkrun.orchestration.primitives.run_command_on_host", inspect)
    monkeypatch.setattr("sparkrun.orchestration.distribution.distribute_from_config", distribute)
    with pytest.raises(ImagePreparationError, match="node-b"):
        stage_prepared_images(prepared, recipe, HOSTS, "/cache", config, require_content_ids=True, stage_models=True)


def test_resolver_preserves_pins_and_host_order_even_when_probes_finish_out_of_order(monkeypatch):
    second = threading.Event()

    def inspect(host, *_args, **_kwargs):
        if host == HOSTS[0]:
            assert second.wait(2)
        else:
            second.set()
        return SimpleNamespace(success=True, stdout=IDS[host], stderr="")

    monkeypatch.setattr("sparkrun.orchestration.primitives.run_command_on_host", inspect)
    assert resolve_content_images([PINNED, "local:tag"], HOSTS) == (PINNED, IDS["node-b"])


def test_content_id_reference_cannot_resolve_to_different_bits(monkeypatch):
    monkeypatch.setattr(
        "sparkrun.orchestration.primitives.run_command_on_host",
        lambda *a, **k: SimpleNamespace(success=True, stdout=IDS["node-b"], stderr=""),
    )
    with pytest.raises(ImagePreparationError, match="different content ID"):
        resolve_content_images([IDS["node-a"]], ["node-a"])


def test_dry_run_returns_declared_references_without_probing(monkeypatch):
    recipe, prepared, config = prepared_case(monkeypatch)
    monkeypatch.setattr("sparkrun.orchestration.primitives.run_command_on_host", lambda *a, **k: pytest.fail("live Docker probe"))

    def distribute(*args, **kwargs):
        assert args[5] is True
        kwargs["after_container_sync"]()
        return None, {}, {}, {}

    monkeypatch.setattr("sparkrun.orchestration.distribution.distribute_from_config", distribute)
    staged = stage_prepared_images(prepared, recipe, HOSTS, "/cache", config, dry_run=True, require_content_ids=True)
    assert staged.content_images_by_node == prepared.images_by_node


def test_image_only_staging_keeps_model_transfer_disabled(monkeypatch):
    recipe, prepared, config = prepared_case(monkeypatch)

    def distribute(*args, **kwargs):
        assert kwargs["skip_model"] is True and kwargs["after_container_sync"] is None
        return None, {}, {}, {}

    monkeypatch.setattr("sparkrun.orchestration.distribution.distribute_from_config", distribute)
    assert stage_prepared_images(prepared, recipe, HOSTS, "/cache", config).content_images_by_node == prepared.images_by_node


def test_invalid_staging_inputs_fail_before_distribution(monkeypatch):
    recipe, prepared, config = prepared_case(monkeypatch)
    monkeypatch.setattr(
        "sparkrun.orchestration.distribution.distribute_from_config", lambda *a, **k: pytest.fail("distribution before validation")
    )
    for hosts in ([], ["node-a"]):
        with pytest.raises(ImagePreparationError, match="one prepared container image per host"):
            stage_prepared_images(prepared, recipe, hosts, "/cache", config)
    with pytest.raises(ImagePreparationError, match="one prepared container image per host"):
        stage_prepared_images(PreparedImageSet(None, None), recipe, HOSTS, "/cache", config)
    with pytest.raises(ImagePreparationError, match="operation-scoped config"):
        stage_prepared_images(prepared, recipe, HOSTS, "/cache", config, ssh_kwargs={"ssh_user": "wrong-user"})
