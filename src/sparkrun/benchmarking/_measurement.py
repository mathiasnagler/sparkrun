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


def capture_launch_context(execution, launch=None, metadata=None):
    """Capture the submitted launch or selected running deployment, without guessing."""
    if launch is not None:
        execution.container_image = launch.container_image or None
        execution.recipe = launch.recipe
        execution.overrides = dict(launch.overrides or {})
        execution.runtime_info = dict(launch.runtime_info or {})
    elif metadata is not None:
        if metadata.get("effective_container_image") or not execution.image_context_known:
            execution.container_image = metadata.get("effective_container_image") or None
        execution.overrides = dict(metadata.get("overrides") or execution.overrides or {})
        execution.runtime_info = dict(metadata.get("runtime_info") or execution.runtime_info)
        if metadata.get("recipe_state"):
            from sparkrun.core.recipe import Recipe

            execution.recipe = Recipe._deserialize(metadata["recipe_state"])
    else:
        execution.container_image = None
    execution.image_context_known = True
