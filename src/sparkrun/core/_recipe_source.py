"""Shared recipe-source attribution for frontend and API loaders."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sparkrun.core.config import SparkrunConfig
    from sparkrun.core.recipe import Recipe
    from sparkrun.core.registry import RegistryEntry, RegistryManager


def catalog_root(config: "SparkrunConfig") -> Path:
    return Path(config.config_path).parent / "recipe-catalog"


def is_catalog_import(path: str | Path | None, config: "SparkrunConfig") -> bool:
    """Recognize staged external contents even through a filesystem alias."""
    return path is not None and Path(path).resolve().parent == (catalog_root(config) / "imports").resolve()


def recipe_registry_entry(path: Path, manager: "RegistryManager", *, registry_name: str | None = None) -> "RegistryEntry | None":
    """Keep an authoritative named selection; otherwise classify cache ownership.

    Callers own discovery policy. Catalog callers already have the selected entry
    from their offline inventory and pass it straight to tag_recipe_source.
    """
    name = registry_name if registry_name is not None else manager.registry_for_path(path)
    return manager.get_registry(name) if name is not None else None


def tag_recipe_source(recipe: "Recipe", registry: "RegistryEntry | None", *, config: "SparkrunConfig", external: bool = False) -> None:
    """Attach selected provenance without inferring local authorship for uploads.

    Use on newly loaded recipes. Preloaded recipes keep their original source;
    a later trust decision compares it with the current registry identity.
    """
    recipe.source_registry = registry.name if registry else None
    recipe.source_registry_url = registry.url if registry else None
    recipe.is_url_sourced = recipe.is_url_sourced or external or is_catalog_import(recipe.source_path, config)
