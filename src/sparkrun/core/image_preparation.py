"""Shared builder preparation and container-image planning.

The result carries a launch-local distribution policy for the shared transfer
layer. Image preparation does not transfer assets or open a second transport.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from sparkrun.core.images import (
    ImagePlan,
    ImagePlanError,
    derive_container_entries,
    resolve_image_plan,
    resolve_runtime_image_plan,
    validate_runtime_image_plan,
)

if TYPE_CHECKING:
    from scitrera_app_framework import Variables

    from sparkrun.builders.base import BuilderPlugin
    from sparkrun.core.cluster_manager import ClusterDefinition
    from sparkrun.core.config import SparkrunConfig
    from sparkrun.core.recipe import Recipe, DistributionResourceConfig, DistributionContainerEntry
    from sparkrun.runtimes.base import RuntimePlugin


logger = logging.getLogger(__name__)


class ImagePreparationError(RuntimeError):
    """The image phase could not produce launchable, resident images."""


@dataclass(frozen=True)
class PreparedImageSet:
    """Builder output and per-node launch image plan."""

    source_image: str | None
    image_plan: ImagePlan | None
    builder: BuilderPlugin | None = None
    container_distribution: DistributionResourceConfig[DistributionContainerEntry] | None = None
    """Launch-local transfer policy; never installed on the reusable recipe.

    No image plan means the executor does not use container assets.
    """

    @property
    def default_image(self) -> str | None:
        return self.image_plan.default_image if self.image_plan is not None else None

    @property
    def images_by_node(self) -> tuple[str, ...]:
        return self.image_plan.images_by_node if self.image_plan is not None else ()

    @property
    def head_image(self) -> str | None:
        return self.image_plan.head_image() if self.image_plan is not None else None


def builder_transforms_image(recipe: Recipe, v: Variables | None = None) -> bool:
    """Best-effort image-transform check used by the pre-side-effect guard."""
    if not getattr(recipe, "builder", ""):
        return False
    try:
        from sparkrun.core.bootstrap import get_builder

        builder = get_builder(recipe.builder, v)
    except Exception:
        logger.debug("Could not resolve builder '%s' for image-transform check", recipe.builder, exc_info=True)
        return False
    return bool(getattr(builder, "transforms_image", True))


def validate_image_configuration(
    recipe: Recipe,
    runtime: RuntimePlugin,
    *,
    v: Variables | None = None,
    run_builder: bool = True,
    transform_check: Callable[[Recipe, Variables | None], bool] = builder_transforms_image,
) -> None:
    """Fail before side effects on incompatible per-machine image settings."""
    if not getattr(recipe, "containers", None):
        return

    from sparkrun.core.recipe import RecipeError

    if not runtime.supports_heterogeneous_images:
        raise RecipeError(
            "Recipe declares per-machine container images (`containers:`), which the '%s' runtime "
            "does not support: its ranks must all run the same build (Ray requires one build across "
            "head and workers; MPI ranks must share an ABI). Remove the `containers:` block, or use "
            "a runtime that supports it (sglang, vllm-distributed, llama-cpp)." % runtime.runtime_name
        )
    if run_builder and transform_check(recipe, v):
        raise RecipeError(
            "Recipe declares per-machine container images (`containers:`) together with builder '%s', "
            "which builds the image it is given. A builder produces one image, so the two cannot be "
            "combined — build the per-machine images out of band and reference them by tag, or drop "
            "the `containers:` block." % recipe.builder
        )


def prepare_images(
    recipe: Recipe,
    runtime: RuntimePlugin,
    host_list: list[str],
    *,
    config: SparkrunConfig | None = None,
    v: Variables | None = None,
    cluster: ClusterDefinition | None = None,
    dry_run: bool = False,
    transfer_mode: str = "local",
    ssh_kwargs: dict | None = None,
    run_builder: bool = True,
    needs_image: bool = True,
    images_by_node: Sequence[str] | None = None,
    validate: bool = True,
    transform_check: Callable[[Recipe, Variables | None], bool] = builder_transforms_image,
    builder_context: Mapping[str, Any] | None = None,
) -> PreparedImageSet:
    """Run the optional builder and resolve the authoritative per-node plan."""
    if not host_list:
        raise ImagePreparationError("image preparation requires at least one target host")
    if validate and needs_image and images_by_node is None:
        validate_image_configuration(
            recipe,
            runtime,
            v=v,
            run_builder=run_builder,
            transform_check=transform_check,
        )

    from copy import deepcopy
    from sparkrun.core.recipe import RecipeError

    try:
        image_plan = (
            resolve_runtime_image_plan(recipe, runtime, host_list, cluster=cluster, images_by_node=images_by_node) if needs_image else None
        )
    except ImagePlanError as error:
        raise RecipeError(str(error)) from error
    if not needs_image and images_by_node is not None:
        raise RecipeError("prepared container images require an executor that uses images")
    source = image_plan.head_image() if image_plan is not None else None
    builder: BuilderPlugin | None = None
    if getattr(recipe, "builder", "") and run_builder:
        from sparkrun.core.bootstrap import get_builder

        builder = get_builder(recipe.builder, v)
        if builder is not None:
            transforms = bool(getattr(builder, "transforms_image", True))
            if transforms and image_plan is None:
                raise RecipeError("An image-transforming builder requires an executor that uses images")
            if transforms and images_by_node is not None:
                raise RecipeError("Prepared images cannot be combined with an image-transforming builder; disable the builder")
            if transforms and image_plan is not None and image_plan.heterogeneous:
                raise RecipeError("A builder requires one source image; set `container:` or build per-host images separately")
            built_image = builder.prepare(
                source or "",
                recipe,
                host_list,
                config=config,
                dry_run=dry_run,
                transfer_mode=transfer_mode,
                ssh_kwargs=ssh_kwargs,
                builder_context=builder_context,
            )
            if image_plan is not None and built_image != source:
                try:
                    image_plan = validate_runtime_image_plan(
                        resolve_image_plan(recipe, built_image, host_list, cluster_hosts=list(cluster.hosts) if cluster else None),
                        runtime,
                        host_list,
                    )
                except ImagePlanError as error:
                    raise RecipeError(str(error)) from error

    container_distribution: DistributionResourceConfig[DistributionContainerEntry] | None = None
    containers = recipe.distribution_config.containers
    if image_plan is not None and (images_by_node is not None or (containers is not None and not containers.explicit)):
        container_distribution = deepcopy(containers)
        if images_by_node is not None:
            container_distribution.enabled = True
        container_distribution.entries = [*derive_container_entries(image_plan, host_list)]
    if image_plan is not None and image_plan.heterogeneous:
        logger.info("Per-machine container images: %d distinct image(s) across %d host(s)", len(image_plan.distinct), len(host_list))
    return PreparedImageSet(source_image=source, image_plan=image_plan, builder=builder, container_distribution=container_distribution)


__all__ = [
    "ImagePreparationError",
    "PreparedImageSet",
    "builder_transforms_image",
    "prepare_images",
    "validate_image_configuration",
]
