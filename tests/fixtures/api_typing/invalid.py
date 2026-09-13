"""Each annotated misuse must produce a diagnostic at the marked line."""

from sparkrun import api
from sparkrun.api import CatalogPage, CatalogRecipe, ResolvedCatalogRecipe, SparkrunContext


def misuse(context: SparkrunContext, page: CatalogPage, row: CatalogRecipe, resolved: ResolvedCatalogRecipe) -> None:
    api.catalog_recipes(filters={"unknown_facet": "2"}, sctx=context)  # expect: reportArgumentType
    page["total"] = "many"  # expect: reportGeneralTypeIssues
    print(page["next_offset"] + 1)  # expect: reportOptionalOperand
    print(row["quantization"])  # expect: reportTypedDictNotRequiredAccess
    recipe, _overrides = resolved
    print(recipe.not_a_recipe_field)  # expect: reportAttributeAccessIssue
