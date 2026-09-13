"""Known recipe-data errors are isolated per file and typed at exact resolution."""

import pytest
import yaml

from sparkrun import api
from sparkrun.core.registry import RegistryEntry
from test_catalog_source_boundaries import context as context, recipe_data


@pytest.mark.parametrize("source", ["local", "registry"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("command", ["invalid"]),
        ("command", 4),
        ("command", False),
        ("defaults", ["invalid"]),
        ("defaults", []),
        ("runtime_config", ["invalid"]),
        ("runtime_config", 3),
        ("runtime_config", []),
    ],
)
def test_malformed_file_does_not_hide_neighbors(context, source, field, value):
    if source == "local":
        root = context.config.config_path.parent / "recipes"
    else:
        entry = RegistryEntry(name="recipes", url="https://example.invalid/recipes.git", subpath="recipes")
        context.registry_manager._save_registries([entry], pending_bootstrap_urls=[])
        root = context.registry_manager._cache_dir(entry.name) / entry.subpath
    root.mkdir(parents=True)
    (root / "good.yaml").write_text(yaml.safe_dump(recipe_data()))
    bad = root / "bad.yaml"
    bad.write_text(yaml.safe_dump(recipe_data(**{field: value})))
    page = api.catalog_recipes(sctx=context)
    assert page["total"] == 1
    assert page["recipes"][0]["source_path"].endswith("good.yaml")
    with pytest.raises(api.SparkrunError) as error:
        api.get_recipe_details(str(bad), sctx=context)
    assert error.value.__cause__ is not None
    with pytest.raises(api.SparkrunError):
        api.resolve_catalog_recipe(str(bad), sctx=context)
    with pytest.raises(api.SparkrunError):
        api.import_recipe(bad.read_text(), sctx=context)
