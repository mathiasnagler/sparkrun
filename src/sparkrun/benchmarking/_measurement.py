"""Private durable measurement context shared by execution and recovery.

Recipe specifications identify requested measurements. These fields describe
what actually ran, and must be saved before commands or publication can fail.
"""

from datetime import datetime, timezone

from sparkrun.benchmarking.metadata import public_benchmark_data

_CONTEXT_FIELDS = {
    "category": "benchmark_category",
    "measured_at": "measurement_started_at",
    "measurement_completed_at": "measurement_completed_at",
    "container_image": "container_image",
    "container_image_sha": "container_image_sha",
    "container_image_sha_pinned": "container_image_sha_pinned",
    "longterm_image_ref": "container_image_longterm_ref",
    "longterm_image_pinned": "container_image_longterm_pinned",
    "runtime_info": "measurement_runtime_info",
    "overrides": "measurement_overrides",
}


def persist_measurement_context(execution, state):
    """Serialize effective values; unknown image is intentional and stays unknown."""
    state.extras.update(
        public_benchmark_data(
            {
                key: getattr(execution, attr)
                for attr, key in _CONTEXT_FIELDS.items()
                if attr not in {"measured_at", "measurement_completed_at"} or getattr(execution, attr) is not None
            }
        )
    )
    state.extras["measurement_context_version"] = 2
    if execution.recipe is not None:
        state.extras["measurement_recipe_state"] = public_benchmark_data(execution.recipe.__getstate__())


def restore_measurement_context(state, *, category=""):
    """Restore recorded context; only legacy timestamps may be inferred."""
    if state.completed_indices and not state.extras.get("measurement_context_version"):
        fallback = state.updated_at or state.created_at or datetime.now(timezone.utc).isoformat()
        state.extras.setdefault("measurement_started_at", fallback)
        if state.is_complete(len(state.schedule)):
            state.extras.setdefault("measurement_completed_at", fallback)
    result = {attr: state.extras[key] for attr, key in _CONTEXT_FIELDS.items() if key in state.extras}
    result["category"] = state.extras.get("benchmark_category") or category
    result.setdefault("measured_at", None)
    result.setdefault("measurement_completed_at", None)
    # Legacy digest-only records still have evidence of the measured image.
    if not result.get("container_image") and state.extras.get("measurement_context_version", 0) < 2:
        result["container_image"] = state.extras.get("container_image_sha")
    result.setdefault("container_image_sha_pinned", bool(state.extras.get("container_image_sha")))
    result["image_context_known"] = state.extras.get("measurement_context_version", 0) >= 2
    return result


def _image_digest(reference):
    """Recognize a digest reference without resolving mutable tags or doing I/O."""
    value = (reference or "").rsplit("@", 1)[-1]
    return value if value.startswith("sha256:") else None


def validate_image_references(references, image):
    """Reject known image changes; return whether equivalence was established."""
    from sparkrun.benchmarking.run_state import BenchmarkStateError

    references = {value for value in references if value}
    if not references or not image:
        return False  # missing evidence stays unknown
    if image in references:
        return True
    digest = _image_digest(image)
    if digest and digest in {_image_digest(ref) for ref in references}:
        return True
    raise BenchmarkStateError("Running job image differs from the saved benchmark; explicitly start fresh")


def capture_launch_context(execution, launch=None, metadata=None, *, container_image=None, state=None):
    """Accept fresh launch context, or verify a candidate against recorded context.

    Resuming preserves the original measurement as a unit, including unknown
    provenance. A current deployment supplies validation evidence, never a new
    attribution for rows already measured. Digest references can establish image
    equivalence; different mutable references require an explicit fresh run.
    """
    if launch is not None:
        image = launch.container_image or None
        recipe = launch.recipe
        overrides = dict(launch.overrides or {})
        runtime_info = dict(launch.runtime_info or {})
    elif metadata is not None:
        image = metadata.get("effective_container_image") or container_image or None
        recipe = execution.recipe
        if metadata.get("recipe_state"):
            from sparkrun.core.recipe import Recipe

            recipe = Recipe._deserialize(metadata["recipe_state"])
        overrides = dict(metadata.get("overrides", execution.overrides) or {})
        runtime_info = dict(metadata.get("runtime_info", execution.runtime_info) or {})
    else:
        image = container_image or None
        recipe, overrides, runtime_info = execution.recipe, execution.overrides, execution.runtime_info
    if execution.resumed:
        validate_image_references((execution.container_image, execution.container_image_sha, execution.longterm_image_ref), image)
        if state is not None:
            from sparkrun.benchmarking._specification import validate_job_specification

            candidate = {**metadata, "effective_container_image": image} if metadata is not None else None
            if launch is not None:
                candidate = {
                    "hosts": launch.host_list,
                    "model": recipe.model,
                    "runtime": recipe.runtime,
                    "recipe_state": recipe.__getstate__(),
                    "overrides": overrides,
                    "effective_container_image": image,
                }
            validate_job_specification(state, candidate)
        # Legacy absence is now an explicit unknown, including automatic
        # resumes with a new LaunchResult that must not supply a fallback.
        execution.image_context_known = True
        return
    execution.container_image = image
    execution.recipe, execution.overrides, execution.runtime_info = recipe, overrides, runtime_info
    execution.container_image_sha = execution.longterm_image_ref = None
    execution.container_image_sha_pinned = execution.longterm_image_pinned = False
    execution.image_context_known = True
