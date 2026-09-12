# API migration to 0.4.0

`0.4.0` is a breaking Python API release. Update downstream callers and pin
compatible core/integration versions together. Removed imports and model fields
have no deprecation shims. Existing recipe and configuration YAML keys remain
shared unless noted below. The tables also cover pre-release plugin contracts.

## Supported imports

| Consumer | Supported surface |
| --- | --- |
| Application authors | `sparkrun.application`: `initialize`, `run_cli`, `ApplicationProfile`, `UpdateSource`, `APPLICATION_PROFILE_API_VERSION`, and application/controller identity helpers. |
| Workload and benchmark callers | `sparkrun.api`: operation functions, options, results, decisions/events, and operational errors. |
| Setup callers | `sparkrun.api.setup`: checks/models, probe/plan helpers, apply/undo runners, manifest manager, statuses, and `SetupFailed`. |
| Benchmark integration authors | `sparkrun.core.benchmark_integrations`: registration, `BenchmarkIntegration`, `BenchmarkDefaults`, context and immutable measurement/state snapshots. |
| Setup extension authors | `sparkrun.core.setup_steps`: step/constraint registration; caller-facing types are also exported by `api.setup`. |
| Other extension authors | Documented registries such as `core.cli_registry`, `core.run_handlers`, `core.hardware_probe_extensions`, and `core.features`; shared transactions and `PLUGIN_API_VERSION` in `core.registration`. |
| Kubernetes callers | `sparkrun.plugins.k8s.api`, `plugins.k8s.config`, and `plugins.k8s.executor`. |

Documented core extension modules remain supported imports. Private API/CLI
modules are implementation details; installed plugins should not depend on the
bundled Arena adapter's private command helpers.

## Application initialization and identity

```python
from sparkrun.application import initialize, ApplicationProfile, UpdateSource

profile = ApplicationProfile(
    id="example-app", display_name="Example App", command="example-app",
    package="example-app", profile_ref="example_app.profile:PROFILE",
    update_sources={"stable": UpdateSource("example-app")},
)
context = initialize(profile, config_path="/path/to/example-app/config.yaml")
```

An application can be a CLI, daemon, desktop app, or another Python frontend.
Select the profile/config before reading settings or initializing plugins. One
process binds to one application/profile and canonical config file. Repeated
initialization reuses its plugin registry; switching the binding raises an error.
`api.default_sctx()` delegates to `initialize()` and creates a fresh context view
of that same binding. Pass an existing context to share its cached managers.

`initialize(variables=...)` supports injection on the first call only. Later calls
may omit it or pass the same instance. A fatal bootstrap error poisons the
initialization; repair the cause and restart the process. Optional plugin failures
are contained and reported in inventory. Required integration failures prevent
launch and raise `api.IntegrationUnavailable`.

One controller identity represents an application using a canonical config
**directory**. A private seed and the directory derive its opaque ID. Files in
that directory share it; copying/moving to another directory changes it; restoring
the seed at the original path preserves it. There is no configurable controller
selector. Child processes inherit the selected profile/config. Shared services
can use `(application.id, controller_id, resource_id)` for reconciliation without
switching the core process's profile. Existing executor ownership enforcement is
application-scoped; emitting a controller envelope does not expand that policy.
See [application profiles](APPLICATION_PROFILES.md).

Cluster SSH overrides are operation-local. `SparkrunContext.for_cluster()` and
`SparkrunConfig.for_cluster()` preserve the original configured fallback user;
inspect the resolved cluster/operation context rather than expecting a previous
operation to mutate `sctx.config.ssh_user`.

## Optional integration moves and selection

| Previous surface | 0.4.0 surface |
| --- | --- |
| `sparkrun.arena` | `sparkrun.plugins.sparkarena` and corresponding auth/upload modules. |
| `sparkrun.api.k8s` | `sparkrun.plugins.k8s.api`. |
| `sparkrun.orchestration.k8s` | `sparkrun.plugins.k8s.orchestration`. |
| `sparkrun.orchestration.executors.k8s` | `sparkrun.plugins.k8s.executor`. |
| Kubernetes accessors on `SparkrunConfig` | `K8sSettings(config)` from `plugins.k8s.config`. |
| Kubernetes fields on generic executor config | `K8sExecutorConfig`; normal executor resolution selects it. |
| Run-handler callback with individual resolved arguments | `(options, sctx, *, plan: RunPlan, started_at: float) -> RunResult`. Inspect public `rc`; the private launch handle is optional. |
| `core.installed_plugins.registration_transaction(v)` | `core.registration.registry_transaction(v)`. All loaders use one shared import/registration boundary. |

Arena's `integration.arena` gate registers its CLI and benchmark hooks; Sparkrun
enables it by default. Kubernetes requires the parent `integration.k8s` gate.
On stable/beta, enabling only `executor.k8s` is insufficient. On alpha, the parent
and its `executor.k8s`, `cli.setup.k8s`, and `api.run.k8s` children default on.
Explicit profile/user policy overrides defaults. See the
[Kubernetes guide](../src/sparkrun/plugins/k8s/README.md).

Installed integrations are selected by stable ID; installation alone does not
load them. All plugin sources use registration rollback, including import-time
contributions to enlisted registries. Failed plugins are not reported as loaded
and independent plugins may continue. Inventory retains import/registration failure
messages for all three sources, with directory/package provenance. Listing does
not import disabled modules; a successful reload clears a prior failure for that
source. Provider conflicts remain launch blockers.
Arbitrary plugin I/O is outside rollback. The SAF adapter depends on
`scitrera-app-framework==0.0.69`, including private state-root/registry internals;
changing that pin requires profile, rollback, and installed-wheel checks.
Plugin modules declare literal integer `SPARKRUN_PLUGIN_API_VERSION = 1`, checked
against `core.registration.PLUGIN_API_VERSION`, independently of the application
profile API version. Installed modules require it; other sources validate it when
present. See [framework registration and identifier terminology](PLUGINS.md#installing-a-benchmark-framework).

Run-handler callbacks now receive `before_start` as a required keyword argument.
For real launches call this core-owned, idempotent callback after validation and
staging, immediately before submission; let failures abort the launch. It is `None`
in dry-run. Core no longer replaces a running deployment before dispatching to
the plugin. Kubernetes forwards it to `launch_jobset(before_start=...)` so its
manifest and feasibility prechecks finish first.

The 0.4.0 `RunOptions` cleanup removes two ineffective fields:

- Replace `RunOptions(port=9001)` with `RunOptions(overrides={"port": 9001})`.
  This single input participates in planning, intent/fingerprint generation,
  ensure matching, command generation, and handler dispatch.
- `diagnostics_path` is removed from library options. The CLI's
  `--collect-diagnostics PATH` still owns its collector; embedding applications
  manage diagnostics collection separately.

`RunOptions.trust` is a boolean, defaulting to `False`. It never requests an
interactive prompt. False retains automatic trust for local recipes and trusted
registries; `True` explicitly authorizes hooks and other trust-gated execution.
Frontends must complete any prompt before calling the API. API execution now
rejects untrusted hooks before preparation/launch even with a TTY attached, instead
of reaching legacy interactive prompts. The CLI uses `--trust` for this explicit
authorization; local/trusted-registry automatic trust remains unchanged.

`RunOptions.follow` now defaults to `False`. It controls only legacy foreground
runtime attachment (`detached=False`); detached API launches return without
following. Read detached logs through `api.logs()` or a plugin's explicit iterator.
The CLI selects its own log policy. Native handlers can return only `RunResult`;
the CLI renders that public result without requiring a private launch handle or
running the legacy host lifecycle.

Kubernetes `launch_jobset()` and `run_launcher_job()` no longer accept `follow`.
Both return after submission. Use `plugins.k8s.api.logs(name=..., kind="jobset")`
or `kind="job"`, optionally with `follow=True`, to consume structured `LogLine`
records. Close the iterator when stopping early. `setup k8s launch --follow` and
`setup k8s run-job --follow` render that iterator in the CLI. Native `run` JobSet
submission returns without attachment; use the explicit plugin log API.

Run handlers reuse the supplied plan and standard executor resolver.
`RunOptions.executor_overrides()` supplies the caller layer. Do not independently
merge recipe/cluster/default layers or repeat placement in a handler.

The unused per-kind package entry-point groups were removed from host metadata;
installed plugins use `sparkrun.plugins` module entry points. Setup-step graphs
must be valid when module registration completes. Same-module forward references
work; cross-module providers must already be registered. Failed validation rolls
back the module, including when the affected step's feature is disabled.

## Benchmark callers

| Previous surface | 0.4.0 surface |
| --- | --- |
| `BenchmarkOptions(arena=True)` | `BenchmarkOptions(integrations={"arena": {}})`. Omit or use `{}` to disable explicit selection. |
| `resume_benchmark(id)` returning a raw dict | `BenchmarkResult`; measurements in `.results`, publication outcomes in `.integration_results`. |
| `resume_benchmark(id, emitter=...)` | `progress_callback=...` and `decision_callback=...`. Emitters are private frontend adapters. |
| `BenchmarkOptions(on_prompt_required=..., on_complete_state=...)` | One `decision_callback(BenchmarkDecision) -> bool`. The old fields are removed. |
| `BenchmarkResult.submission_id` | `result.integration_results.get("arena", {}).get("submission_id")`. |
| `BenchmarkFailed(exit_code=0)` for completed resume | Successful saved `BenchmarkResult` with `already_complete=True`. |

Benchmark authentication is now execution-only. Use `api_key_env` rather than
persisting API keys in task args or publication data. Resume saves/reuses the
variable name and resolves its current value; it accepts `api_key_env`, `timeout`,
and `exit_on_first_fail` keyword overrides. Completed publication retries need no
inference credential, and implicit completed-result reuse performs no inference
launch or cleanup. Legacy structured keys are removed from loaded snapshots;
plugin-owned serialized documents are handled by their owner at binding.

Incomplete resumes restore a credential-free saved recipe specification and
verify it against measurement identity and running-job metadata. Legacy state
without a specification must reproduce its original identity from current inputs;
otherwise start fresh. Old incomplete IDs whose hashes included credentials
cannot be verified after removing those credentials. Completed results remain
loadable by ID.

Checkpoints now distinguish absent state from unreadable/invalid state. Implicit
resume raises a typed operational error for the latter; only explicit fresh
execution replaces it. See [benchmark authentication and recovery](BENCHMARK_API.md#authentication-and-checkpoint-recovery).

Preloaded `Recipe` and `ClusterDefinition` objects retain their in-memory edits;
recipe resolution may mutate the supplied recipe. Use a separate instance when
you need to preserve a template. Benchmark run/resume are silent unless a
callback is supplied. `BenchmarkDecision` distinguishes `resume_incomplete`,
`remeasure_complete`, and `integration_confirmation`; callbacks see a frozen
value, not mutable persistence state. Missing callbacks use each decision's
default. Arena quality confirmation defaults to declining in headless use. Recipe
trust remains a separate option.

`resume_benchmark(id, export_files=False)` suppresses optional file exports after
resumed measurement; `output_file=...` selects their base path. Complete result
loading/publication retry never remeasures or regenerates exports. A completed ID
with no available integration work returns its saved result without changing its
state. The CLI renders its own “nothing to resume” message.

Validated scheduled measurements commit before optional exports. Failures after
validation raise `BenchmarkFinalizationFailed` with `.result`, `.stage`, `.errors`,
and original cause. `BenchmarkIntegrationFailed` remains its subtype for plugin
and shared-state errors; shared persistence uses `integration="<state>"`.
Successful partial output paths remain attached. Publication retry requires saved
measurements; storage failures may require retaining `.result` directly.

Owned inference cleanup runs on all exits after launch, including early plugin,
framework and frontend callback errors. `no_stop`, `skip_run`, and resume-by-ID
retain their ownership policies. Secondary cleanup errors preserve the original
failure. Publication retry does not repair failed optional exports.

## Benchmark integration authors

| Previous contract | 0.4.0 contract |
| --- | --- |
| `BenchmarkIntegration(on_ready=...)` | `on_bind`, before launch; endpoint readiness is a different event. |
| Settings validation in `prepare()` | `validate(context)` sees merged saved/explicit settings on every invocation. |
| `prepare()` returning `BenchmarkOptions` | Return `BenchmarkDefaults`: category, framework, profile, read-only bench args only. |
| Live `context.result: BenchmarkExecution` | Read-only `BenchmarkMeasurement`, including recipe YAML and redacted provenance. |
| Mutable `context.state: BenchmarkRunState` | Detached `BenchmarkStateInfo`; persist private JSON values in `context.data`. |
| Writing other integrations' result mappings | Write your own `context.outcome`; core produces detached public results. |

Normalization converts dates/datetimes to ISO strings, pathlib paths to strings,
and tuples to lists, and rejects other arbitrary framework result objects.
Measurement snapshots are recursively read-only. `measured_at` and `completed_at`
use a persisted interval; publication retries do not advance it. Legacy states
pin a one-time timestamp fallback. Publication-only hooks may lack recipe YAML
and provenance and should use data saved by their earlier hooks.

See [benchmark API behavior and reserved state keys](BENCHMARK_API.md) and
[plugin contracts](PLUGINS.md).

## Setup recording and undo

`api.setup` exports caller-facing types, statuses, probe/plan helpers,
`run_setup_steps`, and `run_setup_undo`. Existing documented core imports remain
valid. Built-in reversals live in core; new frontends should use the public runner
rather than private CLI teardown helpers.

Use `ok`, `warn`, `fail`, and `skip` consistently. Per-host outcomes retain details;
step summaries aggregate severity. Recording needs one consistent cluster name
and a `ManifestManager`; omitting the manager explicitly disables recording.
Strict manifest validation rejects invalid shapes/unsupported versions before
changes, while accepting supported version-1 legacy defaults. Detached undo
inputs receive the same ownership/schema checks even without a manager.
`SetupUndoResult.manifest` now returns the detached updated state for caller-owned
persistence; with a manager it matches saved state. The input remains unchanged.

Only confirmed `ok` undo clears a host's record. Failed, skipped, declined,
filtered, and unknown plugin changes remain recorded. A prerequisite cannot be
removed while a recorded dependent remains on the same host, including transitive
dependents outside the selected filter. Successful independent hosts continue.
The CLI deletes cluster state only after complete reversible teardown, subject to
`--keep-cluster`; the runner itself never deletes cluster definitions.

Hard prerequisites use `FAIL` checks; `WARN` is advisory. Reprobes, rather than an
action's success flag alone, determine whether dependent apply actions are ready.
See [the complete headless example](SETUP_STEPS.md#complete-headless-example).

## Errors and release ownership

Operational API failures use `SparkrunError` subtypes. Implicit API initialization
wraps bootstrap failures with their original cause; explicit `initialize()` exposes
bootstrap errors directly. Pure model/plan validation, including unsupported
`materialize()` layouts, may raise `ValueError` or `TypeError`. Setup runner
orchestration errors use `SetupFailed`; per-host action failures are returned
outcomes. Benchmark result-bearing failures are described above. Interrupts and
`SystemExit` propagate unchanged.

Core and bundled K8s/Arena declarations share the owning `sparkrun` distribution
version. Independently released plugins, including vendored SparkRoute, retain
their own versions. Downstream applications should declare a compatible core
range or exact pin rather than infer compatibility from their own version.

Installed plugin inventory rows are frozen reporting snapshots; entry-point
handles are private. Replace any mutation of `InstalledIntegration` rows with
explicit configuration before initialization. Profile integration lists,
bootstrap URL lists, and environment-alias lists require list/tuple values;
use `("arena",)` instead of `"arena"` for a single integration.

Benchmark data inputs (`overrides`, `bench_args`, `integrations`, `state_extras`)
now consistently accept string-keyed mappings and copy serializable nested data
before hooks. Invalid shapes raise field-specific errors instead of silently
falling back to empty overrides. Resume integration settings use the same rules.
Incomplete resumes now enforce the same framework prerequisites as initial
measurements; preview and completed-result/publication paths remain independent
of those tools. Custom setup undo mappings now derive reverse dependency order
from the shared graph, independent of insertion order.

## Measurement failure and setup targeting

A nonzero single-call benchmark exit now raises `BenchmarkFailed` under either
`exit_on_first_fail` setting, even when partial output parses. Successful-completion
and publication hooks do not run. For schedules, false means attempt remaining
tasks once per invocation; failed tasks stay resumable and retry on the next run.
Timeouts follow the same rule. The bounded measurement-gap pass remains available.

`run_setup_steps()` requires nonempty host keys and matching `HostState.host`
values. Invalid initial mappings fail with `SetupFailed` before callbacks or
manifest access. Reprobes must satisfy the same invariant and return only requested
hosts; invalid results stop further actions while retaining changes already recorded.
