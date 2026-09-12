"""Click-free lifecycle extensions around framework-driven benchmark runs.

Integrations add publishing/reporting behavior; BenchmarkingPlugin still owns
framework command construction, scheduling, and result parsing. State callbacks
run under the benchmark state-directory lock and must store only JSON values.
"""

from __future__ import annotations

from sparkrun.core.registration import enlist_registry_state, register_unique

from copy import deepcopy
from sparkrun.benchmarking.metadata import public_benchmark_data
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, TYPE_CHECKING, Protocol, Mapping
from types import MappingProxyType

if TYPE_CHECKING:
    from sparkrun.api._benchmark_models import BenchmarkOptions, ProgressEvent
    from sparkrun.benchmarking.base import BenchmarkExecution
    from sparkrun.benchmarking.run_state import BenchmarkRunState
    from sparkrun.core.context import SparkrunContext

STATE_KEY = "benchmark_integrations"


class BenchmarkReporter(Protocol):
    """Supported plugin notifications and synchronous confirmation surface."""

    def info(self, msg: str) -> None: ...
    def warning(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...
    def event(self, ev: ProgressEvent) -> None: ...
    def confirm(self, message: str, *, default: bool = False) -> bool: ...


@dataclass(frozen=True)
class BenchmarkStateInfo:
    """State snapshot refreshed before each hook; no save/mutation interface.

    ``extras`` is a detached, read-only top-level mapping for legacy data
    migration. New integrations persist JSON values only in context.data.
    """

    benchmark_id: str
    created_at: str
    updated_at: str
    extras: Mapping[str, Any]


@dataclass(frozen=True)
class BenchmarkMeasurement:
    """Detached publication data; no recipe, runtime, or launcher objects.

    Nested mappings/sequences are read-only. Recipe YAML and redacted provenance
    are available while this invocation has a recipe; integrations persist what
    they need in context.data for publication-only retries.
    """

    benchmark_id: str
    success: bool
    recipe_name: str | None
    framework: str
    category: str
    profile: str | None
    results: Mapping[str, Any]
    outputs: Mapping[str, str]
    cluster_id: str | None
    host_list: tuple[str, ...]
    container_image: str | None
    benchmark_args: Mapping[str, Any]
    resumed: bool
    measured_at: str | None
    recipe_yaml: str | None
    provenance: Mapping[str, Any]
    completed_at: str | None = None


def _measurement_snapshot(execution: BenchmarkExecution) -> BenchmarkMeasurement:
    from sparkrun.utils.data import freeze, normalize_data

    recipe_yaml, provenance = None, {}
    if execution.recipe is not None:
        provenance = execution.generate_metadata(redact_hosts=True, resolve_image=False)
        # Arbitrary launch span attributes are local diagnostics, not part of
        # the public plugin provenance contract.
        provenance.get("timing", {}).pop("launch", None)
        recipe_yaml = provenance["recipe"].pop("text")
    return BenchmarkMeasurement(
        benchmark_id=execution.benchmark_id,
        success=execution.success,
        recipe_name=execution.recipe_name,
        framework=execution.framework_name,
        category=execution.category,
        profile=execution.profile,
        results=freeze(normalize_data(execution.results or {}, path="results")),
        outputs=freeze({k: str(v) for k, v in (execution.outputs or {}).items() if v is not None}),
        cluster_id=execution.cluster_id,
        host_list=tuple(execution.host_list or ()),
        container_image=execution.container_image,
        benchmark_args=freeze(normalize_data(public_benchmark_data(execution.benchmark_args or {}), path="benchmark_args")),
        resumed=execution.resumed,
        measured_at=execution.measured_at,
        completed_at=execution.measurement_completed_at,
        recipe_yaml=recipe_yaml,
        provenance=freeze(normalize_data(provenance, path="provenance")),
    )


class IntegrationCompletionError(Exception):
    """Internal handoff to the API's structured publication error."""

    def __init__(self, integration: str, message: str):
        super().__init__(message)
        self.integration = integration


@dataclass
class BenchmarkIntegrationContext:
    """Per-invocation plugin context.

    prepare applies measurement defaults without result/state. validate sees
    merged settings before on_bind on every invocation. Hooks receive immutable
    measurement/state snapshots; data persists privately for this integration,
    and outcome becomes its own public integration result. Publication-only
    retries may have no recipe YAML/provenance: use persisted data.
    """

    sctx: SparkrunContext
    emitter: BenchmarkReporter
    settings: dict[str, Any]
    dry_run: bool = False
    resumed: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    result: BenchmarkMeasurement | None = None
    outcome: dict[str, Any] = field(default_factory=dict)
    state: BenchmarkStateInfo | None = None


@dataclass(frozen=True)
class BenchmarkDefaults:
    """The only measurement fields an integration may select in prepare().

    Lifecycle, targets, callbacks and integration selection remain caller-owned.
    Use dataclasses.replace to return new defaults; nested args are read-only.
    """

    category: str | None = None
    framework: str | None = None
    profile: str | None = None
    bench_args: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        from sparkrun.utils.data import freeze, normalize_data

        for name in ("category", "framework", "profile"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError("Benchmark default %s must be a string or None" % name)
        if not isinstance(self.bench_args, Mapping):
            raise TypeError("Benchmark default bench_args must be a mapping")
        object.__setattr__(self, "bench_args", freeze(normalize_data(self.bench_args, path="bench_args")))


@dataclass(frozen=True)
class BenchmarkIntegration:
    name: str
    prepare: Callable[[BenchmarkDefaults, BenchmarkIntegrationContext], BenchmarkDefaults] | None = None
    on_bind: Callable[[BenchmarkIntegrationContext], None] | None = None
    on_checkpoint: Callable[[BenchmarkIntegrationContext], None] | None = None
    on_complete: Callable[[BenchmarkIntegrationContext], None] | None = None
    feature_flag: str | None = None
    validate: Callable[[BenchmarkIntegrationContext], None] | None = None


_INTEGRATIONS: dict[str, BenchmarkIntegration] = {}

enlist_registry_state(globals(), "_INTEGRATIONS")


def register_benchmark_integration(integration: BenchmarkIntegration) -> None:
    """Register once per name; conflicting providers fail plugin registration."""
    if not isinstance(integration.name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", integration.name):
        raise ValueError("Invalid benchmark integration name")
    if any(
        callback is not None and not callable(callback)
        for callback in (integration.prepare, integration.validate, integration.on_bind, integration.on_checkpoint, integration.on_complete)
    ):
        raise TypeError("Benchmark integration hooks must be callable")
    register_unique(_INTEGRATIONS, integration.name, integration, description="Benchmark integration")


def registered_benchmark_integrations(v=None) -> dict[str, BenchmarkIntegration]:
    from sparkrun.core.features import feature_gate_enabled

    return {name: spec for name, spec in _INTEGRATIONS.items() if not spec.feature_flag or feature_gate_enabled(spec.feature_flag, v)}


class BenchmarkIntegrationSession:
    """One invocation's selected integrations and their persisted state.

    Explicit settings override saved settings. Saved integrations resume
    automatically when available; unavailable ones are left intact and skipped
    with a warning, so disabling a plugin does not prevent local benchmarking.
    Explicitly requesting an unavailable integration is an error.
    """

    def __init__(self, options: BenchmarkOptions, *, sctx: SparkrunContext, emitter: BenchmarkReporter):
        from sparkrun.api._errors import BenchmarkFailed

        self.options = options
        self.sctx = sctx
        self.emitter = emitter
        self.specs = registered_benchmark_integrations(sctx.variables)
        self.contexts: dict[str, BenchmarkIntegrationContext] = {}
        self.state: BenchmarkRunState | None = None
        self.result: BenchmarkExecution | None = None
        for name, settings in options.integrations.items():
            if name not in self.specs:
                raise BenchmarkFailed("Benchmark integration %r is unavailable or disabled" % name, exit_code=1)
            if not isinstance(settings, dict):
                raise BenchmarkFailed("Settings for benchmark integration %r must be a mapping" % name, exit_code=1)
            self.contexts[name] = BenchmarkIntegrationContext(
                sctx=sctx,
                emitter=emitter,
                settings=deepcopy(settings),
                dry_run=options.dry_run,
            )

    def prepare(self) -> BenchmarkOptions:
        from sparkrun.api._errors import BenchmarkFailed
        from sparkrun.utils.data import thaw

        options = self.options
        for name, context in self.contexts.items():
            callback = self.specs[name].prepare
            if callback:
                defaults = BenchmarkDefaults(options.category, options.framework, options.profile, options.bench_args)
                defaults = callback(defaults, context)
                if not isinstance(defaults, BenchmarkDefaults):
                    raise BenchmarkFailed("Benchmark integration %r prepare must return BenchmarkDefaults" % name)
                options = replace(
                    options,
                    category=defaults.category,
                    framework=defaults.framework,
                    profile=defaults.profile,
                    bench_args=thaw(defaults.bench_args),
                )
        self.options = options
        return options

    def bind(self, result: BenchmarkExecution, state: BenchmarkRunState | None = None, *, resumed=False) -> None:
        """Restore plugin data and bind the record before launching or measuring."""
        if state is not None:
            state.extras = public_benchmark_data(state.extras)
        self.state, self.result = state, result
        saved = state.extras.get(STATE_KEY, {}) if state is not None else {}
        for name, payload in saved.items():
            if name not in self.specs:
                self.emitter.warning("Skipping unavailable benchmark integration %r; saved state is retained" % name)
                continue
            context = self.contexts.setdefault(
                name,
                BenchmarkIntegrationContext(
                    sctx=self.sctx,
                    emitter=self.emitter,
                    settings={},
                    dry_run=self.options.dry_run,
                ),
            )
            context.settings = {**deepcopy(payload.get("settings", {})), **context.settings}
            context.data = deepcopy(payload.get("data", {}))
        for context in self.contexts.values():
            context.resumed = resumed
        # Validation always sees effective settings, including saved values on
        # both automatic and explicit resumes. Measurement defaults are applied
        # only by prepare(), before planning a new benchmark invocation.
        self._call("validate")
        self._call("on_bind")

    def checkpoint(self) -> None:
        """Persist provenance after launch, before any measurements can fail."""
        self._call("on_checkpoint")

    def complete(self) -> None:
        """Called only after a successful measurement (or a dry-run preview)."""
        if self.result.success:
            self._call("on_complete")

    def _call(self, hook: str) -> None:
        errors: list[tuple[str, Exception]] = []
        name = "<state>"
        measurement = None
        try:
            for name, context in self.contexts.items():
                callback = getattr(self.specs[name], hook)
                if callback is None:
                    continue
                if hook != "validate" and self.result is not None and measurement is None:
                    measurement = _measurement_snapshot(self.result)
                context.result = measurement
                context.state = (
                    BenchmarkStateInfo(
                        self.state.benchmark_id, self.state.created_at, self.state.updated_at, MappingProxyType(deepcopy(self.state.extras))
                    )
                    if self.state is not None
                    else None
                )
                callback(context)
        except Exception as exc:
            errors.append((name, exc))
        finally:
            if self.result is not None:
                for name, context in self.contexts.items():
                    if context.outcome:
                        try:
                            from sparkrun.utils.data import normalize_data

                            outcome = normalize_data(context.outcome, path="%s.outcome" % name)
                            if self.result.integration_results is None:
                                self.result.integration_results = {}
                            self.result.integration_results[name] = outcome
                        except Exception as exc:
                            errors.append((name, exc))
            # Rejected overrides cannot poison the last accepted saved settings.
            # Accepted settings are saved by on_bind, even when its hook fails.
            if hook != "validate":
                try:
                    self.save()
                except Exception as exc:
                    errors.append(("<state>", exc))
        # BaseException (including interrupts) propagates through the finally
        # block without being replaced by a secondary persistence failure.
        if errors:
            if self.result is not None and self.result.success:
                for name, error in errors:
                    prior = self.result.integration_errors.get(name)
                    self.result.integration_errors[name] = "%s; %s" % (prior, error) if prior else str(error)
                name, error = errors[0]
                raise IntegrationCompletionError(name, str(error)) from error
            raise errors[0][1]

    def use_context(self, sctx: SparkrunContext) -> None:
        """Use the resolved operation's connection settings for subsequent hooks."""
        self.sctx = sctx
        for context in self.contexts.values():
            context.sctx = sctx

    def save(self) -> None:
        if self.state is None or self.options.dry_run:
            return
        if self.contexts:
            saved = self.state.extras.setdefault(STATE_KEY, {})
            for name, context in self.contexts.items():
                saved[name] = {"settings": deepcopy(context.settings), "data": deepcopy(context.data)}
        self.state.save(str(self.sctx.config.cache_dir))
