"""Optional executor-specific API launch handlers contributed by loaded plugins.

The normal host launch remains the default. A handler takes over the run after
recipe/placement resolution, using the same context and result contract.
"""

from __future__ import annotations

from sparkrun.core.registration import enlist_registry_state, register_unique

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
import re

if TYPE_CHECKING:
    from sparkrun.api._models import RunOptions, RunPlan, RunResult
    from sparkrun.core.context import SparkrunContext
    from sparkrun.orchestration.executors._base import Executor


class BeforeStart(Protocol):
    def __call__(self, *, executor: Executor | None = None) -> None:
        """Replace prior deployments after preparation.

        Controller-native handlers pass their resolved executor so replacement
        uses that exact target, confirms teardown, and includes the current ID.
        Container launchers omit it and retain their per-container cleanup.
        """
        ...


class RunCallback(Protocol):
    def __call__(
        self,
        options: RunOptions,
        sctx: SparkrunContext,
        *,
        plan: RunPlan,
        started_at: float,
        before_start: BeforeStart | None,
    ) -> RunResult:
        """Validate/stage, call before_start immediately before launch, then launch.

        Core owns replacement; the callback is idempotent and absent in previews.
        Callback failure must abort submission. Handlers must not call it before
        validation and preparation have succeeded. Return actual substrate
        outcomes (cluster ID, hosts, command, image, port); core completes shared
        identity components, fingerprint, operation timing, and preview metadata.
        A private launch_result handle is optional. For handler-discovered reuse,
        return already_running=True with the existing deployment's identity and
        any verified metadata; core preserves its fingerprint/timeline and never
        substitutes proposed placement. Do not call before_start for reuse.
        """
        ...


@dataclass(frozen=True)
class RunHandler:
    executor: str
    run: RunCallback
    feature_flag: str | None = None


_RUN_HANDLERS: dict[str, RunHandler] = {}

enlist_registry_state(globals(), "_RUN_HANDLERS")


def register_run_handler(handler: RunHandler) -> None:
    if not isinstance(handler.executor, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", handler.executor):
        raise ValueError("Invalid executor run handler name")
    if not callable(handler.run):
        raise TypeError("Run handler requires a callable")
    register_unique(_RUN_HANDLERS, handler.executor, handler, description="Run handler for executor")


def registered_run_handlers(config) -> dict[str, RunHandler]:
    return {
        name: handler
        for name, handler in _RUN_HANDLERS.items()
        if not handler.feature_flag or config.is_feature_enabled(handler.feature_flag)
    }
