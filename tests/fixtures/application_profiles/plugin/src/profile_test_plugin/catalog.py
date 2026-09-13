"""Headless consumer of the catalog's public dictionary contracts."""

from sparkrun import api
from sparkrun.api import CatalogPage, CatalogRecipeDetails, ResolvedCatalogRecipe, SparkrunContext


def browse(context: SparkrunContext) -> CatalogPage:
    return api.catalog_recipes(local_only=True, sctx=context)


def preview(reference: str, context: SparkrunContext) -> tuple[CatalogRecipeDetails, ResolvedCatalogRecipe]:
    overrides = {"tensor_parallel": "1"}
    details = api.get_recipe_details(reference, overrides, sctx=context)
    resolved = api.resolve_catalog_recipe(reference, overrides, sctx=context)
    return details, resolved
