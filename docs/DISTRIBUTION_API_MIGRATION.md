# Distribution API migration to 0.4.0

This refactor changes Python imports and data-model fields. Update downstream
callers together with their pinned core version; the removed paths and fields do
not have deprecation shims. Existing recipe and configuration YAML keys remain
shared unless noted below.

| Before | After |
| --- | --- |
| `BenchmarkOptions(..., arena=True)` | `BenchmarkOptions(..., integrations={"arena": {}})` |
| `BenchmarkOptions(..., arena=False)` | Omit `integrations` or pass `{}`. |
| `resume_benchmark(id)` returning a raw dict | Returns `api.BenchmarkResult`; use `.results` for measurements and `.integration_results` for publication outcomes. |
| `resume_benchmark(id, emitter=...)` | Use `progress_callback=...` and `decision_callback=...`; emitters are private CLI adapters. |
| `BenchmarkIntegration(on_ready=...)` | `BenchmarkIntegration(on_bind=...)`; this hook runs before launch, not at endpoint readiness. |
| Mutable `context.state: BenchmarkRunState` | Detached `BenchmarkStateInfo` snapshot; persist plugin values through `context.data`. |
| Generic upload `BenchmarkFailed` | `BenchmarkIntegrationFailed` remains catchable as `BenchmarkFailed` and carries the completed public `.result`. |
| `BenchmarkResult.submission_id` | `result.integration_results.get("arena", {}).get("submission_id")` |
| `sparkrun.arena` imports | `sparkrun.plugins.sparkarena` and its corresponding modules |
| `sparkrun.api.k8s` | `sparkrun.plugins.k8s.api` |
| `sparkrun.orchestration.k8s` | `sparkrun.plugins.k8s.orchestration` |
| `sparkrun.orchestration.executors.k8s` | `sparkrun.plugins.k8s.executor` |
| Kubernetes accessors on `SparkrunConfig` | `K8sSettings(config)` from `sparkrun.plugins.k8s.config` |
| Kubernetes fields on generic executor config | `K8sExecutorConfig` from `sparkrun.plugins.k8s.executor`; normal executor resolution selects it automatically. |
| Settings validation in `BenchmarkIntegration.prepare` | Move to `validate(context)`. It sees merged saved/explicit settings on every invocation. `prepare` applies measurement defaults only. |
| `context.result: BenchmarkExecution` | Read-only `BenchmarkMeasurement`, with `recipe_yaml` and `provenance` instead of live recipe/launch/framework objects. |
| Writing `context.result.integration_results[name]` | Write your integration's `context.outcome` mapping. Persist retry data in `context.data`. |
| Run-handler callback with individual resolved keyword arguments | `(options, sctx, *, plan: RunPlan, started_at: float) -> RunResult`. |

Arena benchmark arguments and result data belong to the optional integration.
Enable `integration.arena` to register its CLI and benchmark hooks; Sparkrun
enables it by default. Application profiles can change that policy. The existing
`--arena` CLI workflow is provided by the plugin. Saved Arena benchmark runs are
handled by its resume adapter; this does not restore removed Python model fields.

Kubernetes now has a parent loading gate, `integration.k8s`. On stable and beta,
an old child override such as `executor.k8s: true` alone is insufficient: enable
the parent too. On alpha the parent defaults on, and its children
`executor.k8s`, `cli.setup.k8s`, and `api.run.k8s` default on. Disable individual
children to retain selected capabilities, or disable the parent to omit the
integration. Explicit application/user policy takes precedence. See the
[Kubernetes plugin guide](../src/sparkrun/plugins/k8s/README.md) for details.

Embedding applications must select their profile and config before plugin
initialization. Use `sparkrun.application.initialize(profile, config_path=...)`;
implicit initialization also binds the process to its first canonical config.
A later attempt to change it raises an error. Start another process for a
different application or configuration. Plugins needing ownership information
can use the [application/controller identity API](APPLICATION_PROFILES.md#plugin-identity-and-shared-services).

The framework adapter currently depends on `scitrera-app-framework==0.0.69`,
including private state-root and registry rollback internals. Updating that pin
requires re-running application path, plugin rollback, and installed-wheel tests.


Benchmark callers can pass preloaded `Recipe` objects without a name lookup.
Run and resume are silent unless a callback is supplied; the CLI retains its
terminal progress adapter. `decision_callback` separates choices from progress
notifications and takes precedence over legacy resume callbacks. Integration
confirmations with no handler now use their specified default (Arena's quality
confirmation defaults to declining). Headless callers that intentionally accept
quality advice should explicitly handle `integration_confirmation`. See
[BENCHMARK_API.md](BENCHMARK_API.md) for examples.

If plugin bootstrap raises, fix the cause and restart the embedding process.
Repeated initialization in that process raises a clear error chained to the
original failure; it cannot return a partially registered context as success.


The controller seed file is now bound to the canonical config directory when
deriving the opaque ownership ID. A copied/moved directory becomes a different
controller automatically; a restore to the original path keeps its identity.
This changes the opaque IDs emitted by earlier, unmerged versions of this branch.
Consumers testing those versions should refresh their stored controller envelopes;
the final format does not expose the seed or local path and adds no configurable
identity setting.

`initialize(variables=...)` permits injection only on the first initialization.
Subsequent calls reuse that instance and reject a different one. Missing or failed
required integrations now raise exported `api.IntegrationUnavailable`, a
`SparkrunError`, before launch planning.

A shared `api.setup.run_setup_steps` is available for console-free setup
sequencing, change recording, and prerequisite reprobes. See
[setup runner contracts](SETUP_STEPS.md#running-setup-from-another-frontend).


## Final API consistency changes

- Benchmark integration `prepare()` now receives and returns `BenchmarkDefaults`
  from `core.benchmark_integrations`. It exposes only `category`, `framework`,
  `profile`, and read-only `bench_args`. Existing `dataclasses.replace` callbacks
  selecting those fields can keep that pattern. Returning `BenchmarkOptions`
  is an error. Lifecycle/target/callback/integration changes are not preparation.
- Framework results normalize date/datetime values to ISO strings, pathlib paths
  to strings, and tuples to lists, regardless of integration selection. Other
  arbitrary Python objects are rejected at the framework result boundary.
- `BenchmarkIntegrationFailed` also preserves completed measurements when snapshot
  conversion, outcome finalization, or completion-state persistence fails.
  Shared persistence errors use the reserved `integration="<state>"` marker.
  Resume needs saved state; storage failures may require retaining `.result`
  directly in the embedding frontend.
- Setup summaries now use the same `ok`, `warn`, `fail`, `skip` vocabulary as
  action results, with deterministic severity aggregation and explicit no-ops.
  `SetupRunResult.outcomes[step][host]` retains per-host details. Replace checks
  for the old summary strings `failed`/`skipped` with `fail`/`skip`.
- Setup recording requires a consistent cluster name; it is inferred from the
  cluster/context unless explicitly supplied. Omit `manifest_mgr` to explicitly
  disable recording. Orchestration errors use `api.setup.SetupFailed`.
- Cluster SSH overrides no longer mutate a shared context. Inspect the resolved
  cluster or the operation's context instead of expecting `sctx.config.ssh_user`
  to retain a prior cluster's user. `SparkrunContext.for_cluster()` and
  `SparkrunConfig.for_cluster()` create operation views sharing the same
  application/config binding and configured fallback account.
- Run handlers use the default launch path's `SparkrunError` translation.
  `RunOptions.executor_overrides()` supplies the caller layer to the existing
  executor resolver; Kubernetes now honors recipe and cluster target settings.


## Review pass 5: finalization and undo recovery

- Catch `api.BenchmarkFinalizationFailed` for all errors after validated
  measurement, including export, cleanup and completion notification. It carries
  `result`, `stage`, `errors`, and the original cause. Existing
  `BenchmarkIntegrationFailed` catches still handle plugin and `<state>` failures.
- Scheduled measurements commit before optional exports. Failed export still
  honors owned-inference cleanup and does not invoke publication. Publication
  retry loads saved results; it does not retry optional file exports.
- Measurement snapshots add `completed_at`; `measured_at` now uses a persisted
  interval instead of the latest state write. Legacy states pin a one-time
  timestamp fallback. Publication retries do not move either bound.
- `api.setup` now exports caller-facing models, probe/plan helpers and
  `run_setup_undo`/`SetupUndoResult`. Existing core imports remain valid.
- Undo uses the same status validation as apply. Only `ok` clears a host's
  record; “already absent” is `ok`, whereas “not undone” is `skip`/`warn`/`fail`.
  Partial and filtered uninstall retain unresolved records automatically.
- Strict setup recording rejects invalid field types and unsupported manifest
  versions before actions. Supported version-1 legacy defaults remain accepted.
- Low-level built-in teardown implementation moved from the private CLI module
  to `core.setup_undo_actions`; use the public undo runner in new frontends.

See [benchmark behavior](BENCHMARK_API.md) and the
[headless setup example](SETUP_STEPS.md#complete-headless-example).
