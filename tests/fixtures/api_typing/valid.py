"""Consumer-only static contracts; this example is type checked, never executed."""

from typing import Any, assert_type

from sparkrun import api
from sparkrun.api import (
    CatalogFacet,
    CatalogPage,
    CatalogRecipe,
    CatalogRecipeDetails,
    ResolvedCatalogRecipe,
    SparkrunContext,
)
from sparkrun.core.recipe import Recipe


def browse(context: SparkrunContext, reference: str) -> CatalogPage:
    filters: dict[CatalogFacet, str] = {"tp": "2", "quantization": "fp8"}
    page = api.catalog_recipes(filters=filters, sctx=context)
    assert_type(page, CatalogPage)
    assert_type(page["total"], int)
    assert_type(page["next_offset"], int | None)
    offset = page["next_offset"]
    if offset is not None:
        assert_type(offset, int)
    for row in page["recipes"]:
        assert_type(row, CatalogRecipe)
        assert_type(row["name"], str | None)
        if row["name"] is not None:
            assert_type(row["name"].upper(), str)
        if "quantization" in row:
            assert_type(row["quantization"], str | None)
            if row["quantization"] is not None:
                assert_type(row["quantization"].lower(), str)
        # Browse summaries deliberately preserve raw fallback values.
        assert_type(row["tp"], object)
        if isinstance(row["tp"], int):
            assert_type(row["tp"] + 1, int)
    details = api.get_recipe_details(reference, sctx=context)
    assert_type(details, CatalogRecipeDetails)
    assert_type(details["trusted"], bool)
    assert_type(details["recipe_revision"], str)
    resolved = api.resolve_catalog_recipe(reference, {"tensor_parallel": 2}, sctx=context)
    assert_type(resolved, ResolvedCatalogRecipe)
    recipe, overrides = resolved
    assert_type(recipe, Recipe)
    assert_type(overrides, dict[str, Any])
    return page
