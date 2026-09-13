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
source. Provider conflicts remain launch blockers. Directory plugins with duplicate
top-level names are rejected before import; pre-imported modules from another
origin cannot satisfy a configured source. Directory inventory reads versions
only from the module at that exact path and no longer borrows a same-named
installed distribution's version. Installed entry-point versions remain metadata-based.
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
the plugin. Controller-native handlers pass their resolved executor via
`before_start(executor=...)`; core uses its exact target for confirmed replacement,
including a prior deployment with the same portable ID. Kubernetes performs this
after manifest and feasibility prechecks.

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
Native `RunResult.cluster_id` remains the portable workload ID. Use
`api.stop(cluster_id=result.cluster_id)` for shared teardown; the saved native
resource reference and resolved Kubernetes target survive a new API session and
changes to defaults. `api.status(..., executor="k8s")` reports submitted JobSets
for intent discovery and replacement. Use `result.metadata["k8s_jobset"]` for the
plugin's direct resource/log API. Common `api.logs()` explicitly rejects native
resource records until that surface supports controller-owned log sources.
The manifest retains portable identity, fingerprint, and host scope as annotations.
Explicit JobSet names and generated suffixes are validated before replacement.
Kubernetes lifecycle operations consistently translate operational failures into
`SparkrunError`, while malformed resource arguments remain `ValueError`.

Core completes common fresh-launch `RunResult` identity, fingerprint, timing and
preview fields for native handlers too. Handlers still return actual substrate
outcomes and need no private launch handle. Ensure hits keep unknown fields empty
instead of borrowing the new plan's fingerprint or timeline. Native ports now
resolve recipe defaults as well as explicit overrides and reject invalid values
before preparation; custom command templates must honor that configured port.

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
Scheduled retries clear their previous task artifact before command construction.
Only usable JSON from successful attempts is aggregated, in schedule order;
unrelated or failed files cannot satisfy measurement coverage. A zero exit without
usable output and coverage still missing after the bounded gap pass remain
incomplete and cannot publish. Successful tasks retain their artifacts on resume.
The internal aggregator now accepts selected paths instead of scanning a directory;
the unused `gap_analysis(expected_per_task=...)` argument was removed.

`run_setup_steps()` requires nonempty host keys and matching `HostState.host`
values. Invalid initial mappings fail with `SetupFailed` before callbacks or
manifest access. Reprobes must satisfy the same invariant and return only requested
hosts; invalid results stop further actions while retaining changes already recorded.

## Sparse tasks and interrupted result processing

Base framework coverage now defaults to `None` (artifact success), rather than
reconstructing task indices from consolidated row positions. Frameworks can still
provide semantic coverage keys. Internal `gap_analysis()` requires
`completed_indices`, and `run_schedule(skip_run=...)` is removed. The public
`BenchmarkOptions.skip_run` option remains unchanged; warmup follows measurement
sessions, including the first task of a resumed session.

Resume distinguishes successful commands from committed measurements. Interrupted
consolidation/parsing/commit resumes from the saved task artifacts and original
measurement specification, without live inference, credentials, or command tools.
Only genuinely missing/invalid artifacts become pending. Committed results still
use the existing publication-only retry path.


### Docker policy and vLLM thread defaults

Docker now uses the bundled `io-uring` seccomp profile. Locally supplied
`executor_config.security_opt: ["seccomp=/path/policy.json"]` files are
snapshotted and carried to every Docker execution node in its launch script.
Explicit `seccomp=builtin` and `seccomp=unconfined` remain available. See
[executor configuration](EXECUTORS.md#docker-seccomp-profiles-04) for precedence,
path resolution, and preview behavior. Atlas no longer forces unconfined mode.

`Executor.prepare_launch(extra_opts=...)` is a default no-op hook for local
input validation before replacement. `apply_runtime_adjustments()` now receives
`defaults`, a read-only lower-priority `Variables` chain; plugins accepting the
documented `**kwargs` need no change. Docker uses it to preserve configured
security policies when adding its rootless defaults.

`vllm-distributed` no longer defines `OMP_NUM_THREADS=4`. The image/runtime can
choose its own thread count. Explicit `recipe.env.OMP_NUM_THREADS` values still
pass through unchanged, including clustered runs.

## Destination and recovery contracts

`RunPlan.executor_target` now records an `ExecutorTarget` (exported from
`sparkrun.orchestration.executor`). Native plugins derive destination identity
and snapshot connection settings through `Executor.resolve_target()`. Planning,
ensure, and replacement use the same target as launch. Native Kubernetes IDs
now distinguish namespaces, contexts, and canonical kubeconfig paths; use the
ID returned by `run()` for durable lifecycle calls. Existing saved native IDs
remain stoppable using their recorded targets. Controller identity remains one
per configuration directory, with no new configurable IDs.

Native Kubernetes does not expose a serving endpoint, so new benchmarks,
skip-run, and pending-task resumes reject it before using control-plane hosts as
an inference URL. Finished-artifact recovery and publication-only resume-by-ID
remain supported. Control-plane-only providers should set
`Executor.supports_host_endpoint = False` until they can supply a usable endpoint.

Initial job-metadata persistence is now required before submission. Preparation
and replacement-callback failures preserve the active record; writes are atomic.
A failed initial write aborts launch. Submission failure after replacement can
still leave the old workload stopped; the recorded new target supports recovery.

Benchmark recovery preserves the selected category and original measurement
interval through partial execution, consolidation, decoding, and final commit
failures. Legacy records retain their documented timestamp fallback. The private
`BenchmarkExecution` record uses `outputs` exclusively; the unused
`output_csv`/`output_json`/`output_yaml` aliases are removed. Public
`BenchmarkResult` is unchanged.

### Scoped observations and effective measurement context

Low-level `load_running_snapshot()` now returns `RunningSnapshot | None`, with
`cluster_ids` and per-executor `coverage`, instead of an `(ids, hosts)` tuple.
`save_running_snapshot(observation, ...)` accepts that same value; use
`ClusterStatus.observation` from a real status query. Do not reconstruct absence
coverage from hostnames alone. Existing unscoped cache files are ignored.
`prune_job_metadata(observation=...)` restricts pruning to confirmed absences;
omitting it is deliberate age-based cleanup and is unsuitable for automatic use.

Custom local PID directories now contribute to destination identity and therefore
to deterministic workload IDs. User-scoped targets also include the resolved SSH
user, so local default-path IDs change in 0.4.0. Docker's default IDs are unchanged.
Implicit principals are resolved before a new user-scoped launch: the OS user
for local dispatch, or OpenSSH's effective configuration (`ssh -G`) for remote
hosts. A cluster must resolve to one user; unresolved or mixed defaults require
an explicit `cluster.user`/`ssh.user` before submission. Existing jobs remain
addressable using their saved IDs, executor configuration, and recorded user.

Benchmark results add `measured_at` and `completed_at`. Runtime-default and
builder-selected images now survive interrupted result processing. Exports use
the same effective image/recipe projection as integrations; raw declarations are
retained separately. Exported `recipe.hash` remains a hash of exported text,
with `declared_hash` added for declared inputs. See the
[provenance contract](BENCHMARK_API.md#effective-measurement-provenance).


### Operation transport and preview results

`api.status()`, `status_report()`, and `stop_all()` now honor application SSH
configuration and the selected cluster's user when `ssh_kwargs` is omitted.
Explicit kwargs override individual keys; `None` or an empty value explicitly
clears that key. Resolving an operation does not mutate the shared context or
carry a cluster user into the next operation. Launch planning, intent discovery,
stop, and logs use the same operation-scoping helper.

`stop_all(discovered=...)` keeps the observed connection when defaults change.
Callers may override authentication settings (for example a rotated SSH key),
but an explicit different SSH user requires a fresh discovery. A manually built
snapshot without coverage still needs its matching caller-supplied context.
The observation cache format is now version 2; old caches are discarded rather
than treated as evidence about another remote user.

`StopAllResult.dry_run` explicitly identifies preview counts/host lists. Those
fields describe planned work when true and confirmed outcomes otherwise.
Discovery errors remain errors in previews, including when some workloads were
found; `success` is false whenever discovery or requested teardown failed.

`ClusterStatus.observation_errors` is the shared incomplete-discovery view for
classification, occupancy scheduling, strict recipe-based lifecycle discovery,
and strict replacement checks. `find_running_intent()` and ensure remain best
effort; `None` can also mean discovery failed. For verified absence, acquire
status and reject `observation_errors` before interpreting a miss. Custom
occupancy consumers should inspect it before using partial host observations.
See [executor coverage](EXECUTORS.md#status-observation-coverage) and the
[measurement provenance contract](BENCHMARK_API.md#effective-measurement-provenance).


Saved local jobs retain their recorded SSH principal even after their named
cluster changes user. Explicit incompatible user overrides and legacy records
with no principal fail without deleting metadata; use fresh status discovery to
inspect the intended namespace. Authentication-key rotation remains supported.
Metadata now persists `executor_user_scoped`; old local records derive that policy
from the executor. Placement, observation matching, and lifecycle recovery share
one private destination definition.

Pending benchmark resume validates effective serving configuration on automatic,
direct, skip-run, and artifact-gap paths. The initial job fingerprint is immutable;
a new launch cannot replace it to accept changed preparation output. Verified image
pins can change reference spelling while the remaining serving inputs stay equal.
Publication continues to use recorded measurement context, including runtime info.


Plans now retain the resolved user-scoped principal through execution even when
defaults or the nested cluster definition change after preview. Credential rotation
remains independent of identity. New local jobs resolve implicit users and record
them for stop/log/liveness recovery; unverifiable old records remain conservative.

Raw telemetry and live monitors now share status's per-key transport override
contract, including omitted context and explicit clears. A monitor with a failed
peer executor preserves known workloads but reports incomplete observation and
zero confirmed free slots.

Benchmark baseline recording consumes the same normalized deployment evidence as
resume validation. API-only run results retain their image-equivalence evidence
without requiring a private launch handle or a redundant image field in metadata.


### Effective transport, local paths, and deployment evidence

Explicit SSH users now take precedence over user settings in `ssh.options`, as
well as SSH config files. This applies to saved-job lifecycle operations and new
launches, and to direct SSH, embedded transfer scripts, pipelines, and rsync.
`build_ssh_opts_string(ssh_user=...)` now includes that user instead of ignoring it.
Implicit users still resolve from OpenSSH configuration or the local OS account;
rotating authentication keys does not change the selected namespace.

Local execution now uses one remote-path normalization/rendering policy for target
identity, launch, status, logs, and teardown. Leading remote-home prefixes are
equivalent; spaces and shell metacharacters are literal. Managed `pid_file` overrides
are rejected before submission; configure a per-workload `pid_dir` instead. Existing
fixed-file jobs retain only command-level recovery support, and failed discovery
cannot authorize automatic metadata deletion. See the [local path and recovery
contract](EXECUTORS.md#local-only-docker--k8s-ignore) before migrating an old job.
`log_file` remains an intentional shared append log; `log_dir` keeps logs separate.

Benchmark candidate assembly checks all available actual-image evidence for
agreement, including metadata when a private launch handle is present. Missing
sources retain their fallback behavior. Conflicting nonempty references fail before
new measurement commands; existing artifacts and historical provenance remain
unchanged. Equivalent digest aliases are accepted. This adds no public result or
context type and preserves complete-artifact recovery and publication-only retries.


### Native filesystem state and recovery

The [native path, state, and lifecycle contract](EXECUTORS.md#native-paths-state-and-lifecycle)
is the reference for local execution and legacy fixed-file recovery.

PID/log paths retain symlink-parent traversal and literal relative home-like
names. Their locations bind to the script's entry directory/home before workload
setup. Changing `working_dir` or workload `HOME` no longer relocates state. Use
absolute or home-relative control paths when invocation directories can differ.
Missing/unreadable working directories or environment files, and nonzero activation
results, now abort launch/exec before the payload and leave existing claims intact.

State acquisition errors cannot establish free capacity or authorize metadata
pruning. Empty, invalid, or unreadable owner markers do not receive the confirmed
missing legacy-owner fallback. PID 1 is rejected as invalid workload state.

Native status and teardown include surviving process-group members after their
leader exits. Teardown retains records on failed inspection or surviving workers;
legacy standalone PID recovery remains supported. Low-level `status_cmd()` returns
0 for a live PID/group, 1 for absent/dead, and 2 for acquisition failure. Do not
interpret every nonzero result as absence. Public status/stop types and the managed
`pid_file` rejection remain unchanged.
