"""Image defaults must agree across per-host planning and image preparation."""

from unittest.mock import Mock

import pytest

from sparkrun.core.cluster_manager import ClusterDefinition
from sparkrun.core.hardware import AcceleratorSpec, HostHardware
from sparkrun.core.image_preparation import prepare_images
from sparkrun.core.images import ImagePlanError, resolve_runtime_image_plan
from sparkrun.core.recipe import Recipe, RecipeError
from sparkrun.runtimes.base import RuntimePlugin
from sparkrun.runtimes.vllm_distributed import VllmDistributedRuntime
from sparkrun.runtimes.vllm_ray import VllmRayRuntime


def _cluster():
    return ClusterDefinition(
        name="mixed",
        hosts=["spark", "generic"],
        hosts_hardware={
            "spark": HostHardware(accelerators=[AcceleratorSpec(vendor="nvidia", model="gb10")]),
            "generic": HostHardware(accelerators=[AcceleratorSpec(vendor="nvidia", model="h200")]),
        },
    )


def _recipe(**fields):
    return Recipe.from_dict({"recipe_version": "2", "model": "org/model", "runtime": "vllm-distributed", **fields})


def test_platform_defaults_are_used_per_host_and_preserve_declarations():
    recipe = _recipe()
    cluster = _cluster()
    runtime = VllmDistributedRuntime()
    from sparkrun.orchestration.job_metadata import generate_intent_id

    before = generate_intent_id(recipe)
    planned = resolve_runtime_image_plan(recipe, runtime, cluster.hosts, cluster=cluster)
    prepared = prepare_images(recipe, runtime, cluster.hosts, cluster=cluster, run_builder=False)
    assert planned.images_by_node == (
        "ghcr.io/spark-arena/dgx-vllm-eugr-nightly-tf5:latest",
        "vllm/vllm-openai:latest",
    )
    assert prepared.image_plan == planned
    assert planned.declared == ()
    # Distribution derives the per-node transfer entries, but not recipe image declarations.
    assert recipe.container == "" and recipe.containers == []
    assert generate_intent_id(recipe) == before


def test_explicit_recipe_image_bypasses_default_hook(monkeypatch):
    runtime = VllmDistributedRuntime()
    default = Mock(side_effect=AssertionError("explicit image must win"))
    monkeypatch.setattr(runtime, "default_image_for", default)
    recipe = _recipe(container="org/explicit:tag")
    cluster = _cluster()
    planned = resolve_runtime_image_plan(recipe, runtime, cluster.hosts, cluster=cluster)
    assert planned.images_by_node == ("org/explicit:tag",) * 2
    default.assert_not_called()


def test_explicit_host_images_need_no_runtime_default(monkeypatch):
    runtime = VllmDistributedRuntime()
    monkeypatch.setattr(runtime, "default_image_for", Mock(side_effect=AssertionError("unused default")))
    recipe = _recipe(containers=[{"host": "generic", "image": "org/tuned:tag"}])
    assert resolve_runtime_image_plan(recipe, runtime, ["generic"], cluster=_cluster()).images_by_node == ("org/tuned:tag",)


def test_per_host_image_overrides_only_its_host():
    recipe = _recipe(containers=[{"host": "spark", "image": "org/tuned:tag"}])
    cluster = _cluster()
    planned = resolve_runtime_image_plan(recipe, VllmDistributedRuntime(), cluster.hosts, cluster=cluster)
    assert planned.images_by_node == ("org/tuned:tag", "vllm/vllm-openai:latest")
    assert planned.declared == (("spark", "org/tuned:tag"),)


def test_homogeneous_runtime_rejects_differing_platform_defaults():
    cluster = _cluster()
    with pytest.raises(ImagePlanError, match="requires one image"):
        resolve_runtime_image_plan(_recipe(), VllmRayRuntime(), cluster.hosts, cluster=cluster)
    planned = resolve_runtime_image_plan(_recipe(container="org/shared:tag"), VllmRayRuntime(), cluster.hosts, cluster=cluster)
    assert planned.images_by_node == ("org/shared:tag",) * 2


def test_builder_rejects_differing_sources_before_preparation(monkeypatch):
    builder = Mock(transforms_image=True)
    monkeypatch.setattr("sparkrun.core.bootstrap.get_builder", lambda *args: builder)
    cluster = _cluster()
    with pytest.raises(RecipeError, match="builder requires one source image"):
        prepare_images(_recipe(builder="test"), VllmDistributedRuntime(), cluster.hosts, cluster=cluster)
    builder.prepare.assert_not_called()


def test_unknown_hardware_uses_runtime_prefix():
    runtime = VllmDistributedRuntime()
    unknown = HostHardware()
    assert runtime.resolve_container(_recipe(), host_hardware=unknown) == runtime.default_image_for()


def test_absent_default_requires_an_explicit_image():
    runtime = RuntimePlugin()
    assert runtime.resolve_container(_recipe()) == ""
    with pytest.raises(ImagePlanError, match="No container image"):
        resolve_runtime_image_plan(_recipe(), runtime, ["host"])


def test_default_hook_errors_are_visible(monkeypatch):
    platform = Mock()
    platform.default_image.side_effect = RuntimeError("broken platform")
    monkeypatch.setattr("sparkrun.platforms.resolve_platform", lambda hardware: platform)
    with pytest.raises(RuntimeError, match="broken platform"):
        VllmDistributedRuntime().resolve_container(_recipe(), host_hardware=HostHardware())
