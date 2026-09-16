"""Opt-in recipe preparation hooks for builds, independent of launch strategies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from sparkrun.api._build_models import BuildOptions, BuildPlan
    from sparkrun.core.context import SparkrunContext
    from sparkrun.core.recipe import Recipe


@dataclass(frozen=True)
class BuildContext:
    """Build-only plugin inputs.

    Recipe is an operation-local copy. A hook may declare additional
    distribution resources there and return builder-context defaults. Hooks
    must honor options.dry_run and must not activate, capture, evict, or
    reserve inference workloads. Execution-strategy hooks are never called.
    """

    options: BuildOptions
    plan: BuildPlan
    recipe: Recipe
    sctx: SparkrunContext


def prepare_build_context(context: BuildContext) -> dict[str, Any]:
    """Run only build hooks owned by recipe items actually present."""
    from sparkrun.core.recipe_items import registered_recipe_items

    values: dict[str, Any] = {"engine": context.plan.runtime.get_family()}
    contributed: dict[str, Any] = {}
    for registration in registered_recipe_items():
        if registration.key not in context.recipe.plugin_items or registration.build_preparation is None:
            continue
        result = registration.build_preparation(context)
        if result is None:
            continue
        if not isinstance(result, Mapping):
            raise TypeError("recipe build_preparation must return a mapping or None")
        for key, value in result.items():
            if key in contributed and contributed[key] != value:
                raise ValueError("conflicting builder context for %r" % key)
            contributed[key] = value
    values.update(contributed)
    values.update(context.options.builder_context)
    return values
