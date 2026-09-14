from __future__ import annotations


import pytest

from sparkrun.core.image_preparation import (
    prepare_images,
)
from sparkrun.core.recipe import Recipe


HOSTS = ["node-a", "node-b"]
PINNED = "registry.example/vllm@sha256:" + "a" * 64


class _Runtime:
    runtime_name = "stub"
    supports_heterogeneous_images = True

    def resolve_container(self, recipe, *, host_hardware=None):
        return recipe.container


def _recipe(**extra):
    return Recipe.from_dict(
        {
            "recipe_version": "2",
            "model": "org/model",
            "model_revision": "model-revision",
            "runtime": "vllm-distributed",
            "container": PINNED,
            **extra,
        }
    )


def test_prepare_images_runs_builder_and_returns_authoritative_plan(monkeypatch):
    recipe = _recipe(builder="snapshot")
    calls = []

    class _Builder:
        def prepare(self, image, recipe, hosts, **kwargs):
            calls.append((image, hosts, kwargs["transfer_mode"]))
            return "myorg/snapshot-vllm:built"

    monkeypatch.setattr("sparkrun.core.bootstrap.get_builder", lambda *_args, **_kwargs: _Builder())
    prepared = prepare_images(
        recipe,
        _Runtime(),
        HOSTS,
        transfer_mode="delegated",
    )

    assert prepared.source_image == PINNED
    assert prepared.head_image == "myorg/snapshot-vllm:built"
    assert prepared.images_by_node == ("myorg/snapshot-vllm:built",) * 2
    assert calls == [(PINNED, HOSTS, "delegated")]


def test_prepare_images_strategy_override_skips_builder_and_drives_distribution(monkeypatch):
    recipe = _recipe(builder="snapshot")
    capsules = ("registry/capsule0@sha256:" + "b" * 64, "registry/capsule1@sha256:" + "c" * 64)
    monkeypatch.setattr(
        "sparkrun.core.bootstrap.get_builder",
        lambda *_args, **_kwargs: pytest.fail("restore resolved builder"),
    )

    prepared = prepare_images(
        recipe,
        _Runtime(),
        HOSTS,
        run_builder=False,
        images_by_node=capsules,
    )

    assert prepared.builder is None
    assert prepared.images_by_node == capsules
    assert prepared.container_distribution.enabled is True
    assert [entry.name for entry in prepared.container_distribution.entries] == list(capsules)
    assert [entry.name for entry in recipe.distribution_config.containers.entries] != list(capsules)
