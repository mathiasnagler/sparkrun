"""Preparation-only operation contracts; these do not represent serving jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sparkrun.core.cluster_manager import ClusterDefinition
    from sparkrun.core.recipe import Recipe
    from sparkrun.core.validation import RecipeIssue
    from sparkrun.orchestration.executor import ExecutorTarget
    from sparkrun.runtimes.base import RuntimePlugin


@dataclass(frozen=True)
class BuildOptions:
    """Build environments and stage assets on every selected host.

    Host selection does not use GPU occupancy or inference node-count limits.
    Builders may run temporary build/probe processes, but this operation never
    starts inference, captures snapshots, or runs launch-strategy hooks.
    """

    recipe: str | Recipe
    hosts: tuple[str, ...] | None = None
    cluster: str | ClusterDefinition | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    trust: bool = False
    rebuild: bool | None = None
    executor: str | None = None
    executor_config: dict[str, Any] | None = None
    cache_dir: str | None = None
    local_cache_dir: str | None = None
    transfer_mode: str | None = None
    transfer_interface: str | None = None
    topology: str | None = None
    sync_tuning: bool = True
    preserve_model_perms: bool | None = None
    skip_model_fan_out: bool | None = None
    builder_context: dict[str, Any] = field(default_factory=dict)
    """Builder inputs; explicit values override plugin-provided defaults."""

    def executor_overrides(self) -> dict[str, Any]:
        values = {"executor": self.executor} if self.executor else {}
        values.update(self.executor_config or {})
        return values


@dataclass(frozen=True)
class BuildPlan:
    """Resolved preparation targets, without an inference placement or job ID."""

    recipe: Recipe
    runtime: RuntimePlugin
    cluster: ClusterDefinition
    host_list: tuple[str, ...]
    executor_target: ExecutorTarget
    needs_image: bool
    options: BuildOptions = field(repr=False)
    issues: tuple[RecipeIssue, ...] = ()


@dataclass(frozen=True)
class BuildResult:
    """Completed preparation, or a dry-run preview, without starting inference.

    Empty images are normal for native executors such as local. Models lists
    the repositories selected for staging, excluding disabled resources.
    A dry run reports intended references, not verified resident content.
    """

    recipe: str
    host_list: tuple[str, ...]
    executor: str
    dry_run: bool
    container_image: str | None = None
    images_by_node: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    effective_cache_dir: str = ""
    local_cache_dir: str = ""
    environment_file: str | None = None
    timeline: dict[str, Any] = field(default_factory=dict)


__all__ = ["BuildOptions", "BuildPlan", "BuildResult"]
