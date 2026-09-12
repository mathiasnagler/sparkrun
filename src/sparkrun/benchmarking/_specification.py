"""Private saved measurement inputs and resume validation; no launch or publication."""

from __future__ import annotations

from sparkrun.benchmarking.metadata import benchmark_recipe_fingerprint, public_benchmark_data
from sparkrun.benchmarking.run_state import BenchmarkStateError, derive_benchmark_id
from sparkrun.core.recipe import Recipe


def measurement_specification(recipe: Recipe, overrides: dict) -> dict:
    """Use the existing full Recipe serialization to preserve in-memory edits."""
    return {
        "version": 1,
        "recipe": public_benchmark_data(recipe.__getstate__()),
        "overrides": public_benchmark_data(overrides),
        "fingerprint": benchmark_recipe_fingerprint(recipe, overrides),
    }


def _saved_recipe(spec: dict) -> tuple[Recipe, dict]:
    if type(spec.get("version")) is not int or spec["version"] != 1:
        raise BenchmarkStateError("Unsupported benchmark measurement specification")
    data, overrides = spec.get("recipe"), spec.get("overrides")
    if not isinstance(data, dict) or not isinstance(overrides, dict) or data.get("_serialization_version") != Recipe._SERIALIZATION_VERSION:
        raise BenchmarkStateError("Invalid saved benchmark recipe specification")
    recipe = Recipe._deserialize(data)
    if benchmark_recipe_fingerprint(recipe, overrides) != spec.get("fingerprint"):
        raise BenchmarkStateError("Saved benchmark recipe no longer matches its fingerprint; explicitly start fresh")
    return recipe, overrides


def _matches_identity(state, recipe, overrides) -> bool:
    from sparkrun.orchestration.job_metadata import generate_cluster_id, PLACEMENT_TOKEN_LEN

    cluster_id = generate_cluster_id(state.intent_id, "0" * PLACEMENT_TOKEN_LEN) if state.intent_id else state.cluster_id
    return state.benchmark_id == derive_benchmark_id(
        cluster_id,
        state.framework,
        state.profile,
        state.base_args,
        state.schedule,
        recipe_fingerprint=benchmark_recipe_fingerprint(recipe, overrides),
        hosts=state.host_list,
    )


def record_job_specification(state, meta: dict | None) -> None:
    """Pin the effective running-job configuration after launcher preparation."""
    if state.measurement_spec is not None and meta and meta.get("recipe_state"):
        actual = Recipe._deserialize(meta["recipe_state"])
        state.measurement_spec["job_fingerprint"] = benchmark_recipe_fingerprint(actual, meta.get("overrides") or {})


def restore_measurement_specification(state, meta: dict | None, *, config) -> tuple[Recipe, dict]:
    """Restore saved inputs, or verify legacy inputs against their benchmark ID.

    Legacy state cannot silently trust a mutable recipe name. If its identity
    cannot be reproduced, the caller must start fresh. No checkpoint is written
    here, including on successful legacy reconstruction.
    """
    if state.measurement_spec is not None:
        recipe, overrides = _saved_recipe(state.measurement_spec)
    else:
        from sparkrun.core.resolve import load_recipe

        recipe, _, _ = load_recipe(config, state.recipe_qualified_name, resolve=False)
        overrides = public_benchmark_data((meta or {}).get("overrides") or {})
    if not _matches_identity(state, recipe, overrides):
        raise BenchmarkStateError("Cannot verify the original benchmark recipe; explicitly start fresh")
    if meta is not None:
        if not state.matches_hosts(meta.get("hosts")):
            raise BenchmarkStateError("Running job hosts differ from the saved benchmark")
        for field in ("model", "runtime"):
            if meta.get(field) and meta[field] != getattr(recipe, field):
                raise BenchmarkStateError("Running job %s differs from the saved benchmark" % field)
        expected = (state.measurement_spec or {}).get("job_fingerprint")
        if expected:
            if not meta.get("recipe_state"):
                raise BenchmarkStateError("Running job recipe provenance is missing")
            actual = Recipe._deserialize(meta["recipe_state"])
            if benchmark_recipe_fingerprint(actual, meta.get("overrides") or {}) != expected:
                raise BenchmarkStateError("Running job configuration differs from the saved benchmark")
    return recipe, overrides
