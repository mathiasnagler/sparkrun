# Benchmark API for custom frontends

An application can be a CLI, daemon, desktop app, or other Python frontend.
Initialize the application once, then share its context across API calls:

```python
from sparkrun.application import initialize
from sparkrun.api import BenchmarkOptions, benchmark

context = initialize(config_path="/path/to/config.yaml")
outcome = benchmark(
    BenchmarkOptions(recipe="my-recipe", hosts=("worker-1",)),
    sctx=context,
)
print(outcome.results)  # output is owned by this frontend
```

`recipe` accepts a name or a preloaded `Recipe`. Objects are used directly,
including in-memory edits; they are not looked up again by name. As with the
other API operations, recipe resolution/overrides may update the supplied object.
Pass a separate recipe instance if you need to preserve an unmodified template.

`cluster` likewise accepts a name or a preloaded `ClusterDefinition`. In-memory
hosts, user, executor, transport, and plugin settings are retained through
planning and launch. Explicit `hosts` replace the cluster host list while
retaining its policy, as with `api.plan/run`. Cluster SSH users apply only to
that operation; reusing the context for another cluster keeps the configured
default account intact.

`overrides`, `bench_args`, `integrations`, and `state_extras` accept string-keyed
mappings, including read-only mappings. Each selected integration's settings must
also be a mapping. At API entry, these data trees are copied into ordinary
mappings/lists; values may be scalars, nested mappings or sequences. Dates become
ISO strings and paths become strings, using the shared data normalization rules.
Other objects, non-string keys, and non-mapping top-level inputs raise
`SparkrunError` with a field path and `TypeError` cause before integration hooks
or launch. Lists of pairs are not treated as mappings; use `{}` for an empty field.
`resume_benchmark(integrations=...)` follows the same rules; its default `None`
means no caller overrides. Preloaded recipe and cluster objects keep the behavior
above.

Actual measurements check framework prerequisites on both new runs and incomplete
resumes. A missing prerequisite raises `BenchmarkFailed` with the details before
inference launch, state-bound integration hooks, or measurement commands. Preview,
completed-result loading, and publication-only retries do not require measurement
tools. Resumes keep their saved execution policy and reacquire credentials.

## Progress and decisions

The API does not instantiate a terminal progress UI. `progress_callback` receives
`ProgressEvent` objects synchronously on the invoking thread. GUI callers can
forward them onto their own event loop; callbacks should return promptly.
The CLI supplies its own terminal adapter.

| Event | Payload |
| --- | --- |
| `schedule_started` | `benchmark_id`, `total_tasks`, `title` |
| `task_start` | `benchmark_id`, `total_tasks`, zero-based `index`, `label` |
| `task_end` | `benchmark_id`, `total_tasks`, `index`, `success`, `duration_s` |
| `results_update` | `benchmark_id`, `total_tasks`, detached consolidated `results` |
| `schedule_log` | `benchmark_id`, `total_tasks`, `message` |
| `schedule_finished` | `benchmark_id`, `total_tasks`; signals exit, including failure |
| `run_complete` | `benchmark_id`, `success`, `resumed`; emitted on successful API return |
| `banner` / `info` / `warning` / `error` | `line` for banner, otherwise `msg` |
| `progress_step` | `step`, `total`, `label` |

Scheduled task notifications use the same vocabulary for initial runs and
resumes. An unscheduled framework emits informational output without task events.
Logging continues to use Python logging; the embedding application controls its
handlers. Returning from a progress callback does not make a decision.

`decision_callback` receives a frozen `BenchmarkDecision` with `kind`, `message`,
`default`, and an optional `benchmark_id`. Return a boolean:

| Kind | True | False | Default without callback |
| --- | --- | --- | --- |
| `resume_incomplete` | Resume prior tasks | Discard prior state and start fresh | True |
| `remeasure_complete` | Discard completed measurements and start fresh | Reuse measurements | False |
| `integration_confirmation` | Continue the integration | Decline | The integration's explicit default |

The first two apply only to `ResumeMode.AUTO`; other resume modes express the
choice directly. `decision_callback` is the single decision interface in 0.4.0;
`on_prompt_required` and `on_complete_state` have been removed. It receives a
frozen value, never mutable benchmark state. Recipe trust remains a separate
`trust` option.

```python
def decide(request):
    if request.kind == "integration_confirmation":
        return show_confirmation_in_my_frontend(request.message)
    return request.default

options = BenchmarkOptions(
    recipe=loaded_recipe,
    progress_callback=queue_progress_event,
    decision_callback=decide,
    integrations={"arena": {}},
)
```

Without a decision handler, Arena's quality confirmation defaults to declining.
A headless application that intentionally accepts that advice must provide its
own handler. The CLI retains its existing interactive/noninteractive policy.

## Results and publication retries

Both `benchmark(options)` and `resume_benchmark(id, ...)` return the public
`BenchmarkResult`: measurement `results`, `outputs`, benchmark/framework/profile
identity, host/image information, `resumed`, `already_complete`, and integration outcomes.
`run_result` is present only when this invocation launched inference. Result
metadata comes from the recorded measurement, including `profile=None` and empty
benchmark arguments; publication retry defaults cannot relabel it. Success and
attached failure results use the same detached conversion.

Framework results use string-keyed mappings, lists, and primitive scalar values.
Tuples become lists; Python dates/datetimes become ISO strings, and pathlib paths
become strings. This normalization happens before export, state persistence, and
integration hooks, with the same behavior when integrations are disabled. Other
application objects are rejected with `BenchmarkFailed` at the framework boundary.
Plugin snapshots expose recursively read-only versions of these values.

On resume, explicit integration settings override persisted settings before
validation. A partial override need not repeat required values already saved.
All available saved integrations are validated, including automatic resumes.
Measurement-default hooks are not rerun by `resume_benchmark(id)`.

`success` describes validated measurement completion (or a successful dry-run
preview), independently of export and publication. A later failure raises
`BenchmarkFinalizationFailed`, a `BenchmarkFailed` subclass, with:

- `result`: the completed public measurements and successfully written output paths.
- `stage`: `export`, `cleanup`, `notification`, `integration`, or `state`.
- `errors`: the primary stage's message and any secondary cleanup error.
- The original exception as `__cause__`.

`BenchmarkIntegrationFailed` remains a subtype for plugin and completion-state
failures, so existing catches keep working. Its `integration` names the plugin
or reserved `<state>` marker; `result.integration_errors` retains their details.
Catch the broader `BenchmarkFinalizationFailed` to handle all post-measurement
failures. A cleanup failure does not replace an earlier export/state failure.
Interrupts propagate unchanged.

Validated scheduled results are saved before optional YAML/CSV/JSON export.
Inference owned by this invocation is stopped on every exit after launch unless
`no_stop` is set, including checkpoint, readiness, parser and progress callback
failures before finalization. Cleanup does not depend on successful rendering.
Launch success uses the public `RunResult.rc`, including plugin implementations
without a private launch handle. A nonzero real launch status fails before
checkpoint hooks, readiness waits, or measurement. Both run and resume preserve
the original interruption even if cancellation notification fails.
`skip_run`, already-running deployments, and resume-by-ID do not stop someone
else's inference. Early failures keep their original cause; a secondary cleanup
failure is logged and attached as an exception note. Interrupts are preserved.
A failed export prevents publication in that invocation; retrying saved
integrations does not repeat measurement or repair the failed optional exports.
Storage failures may require retaining the error's attached result through your
own frontend because recovery by ID requires successfully saved state.

```python
from sparkrun.api import BenchmarkFinalizationFailed, resume_benchmark

try:
    outcome = benchmark(options, sctx=context)
except BenchmarkFinalizationFailed as exc:
    show_measurements(exc.result.results)
    save_retry_id(exc.result.benchmark_id)
    show_finalization_error(exc.stage, exc.errors)

# Later, when completed state was saved and publication needs retrying:
outcome = resume_benchmark(saved_benchmark_id, sctx=context)
```

Both initial and resumed calls preserve results when the final `run_complete`
callback raises, using `stage="notification"`. Earlier progress/decision callback
exceptions abort the current operation and propagate through its API error
boundary; there may not yet be a validated measurement to attach. Callbacks
should not use exceptions to communicate ordinary decisions.

Measurement timestamps are distinct from state bookkeeping. New runs record
the effective `benchmark_category` and `measurement_started_at` before task
execution, then `measurement_completed_at` when the last required task succeeds,
before consolidation or decoding. These live in state extras; integration snapshots expose them as `measured_at` and `completed_at`. Publication
retries preserve this interval while `updated_at` continues advancing. A resume
that measures remaining tasks retains the start of the earlier data and records
the completion of the combined measurement. Legacy states lacking the interval
pin their first observed pre-retry state timestamp as a best available fallback;
that cannot reconstruct lost historical timing. Arena uses the same interval.

Completed scheduled measurements can retry saved integrations without inference
running. Both `benchmark(..., resume=IF_EXISTS)` and `resume_benchmark(id)` reuse
validated results without launch, readiness, stop, framework prerequisites, or
inference credentials. The initial entry point resolves its inputs to find the
measurement ID; resume-by-ID does not reload the recipe. Incomplete resumes by
ID require a verified saved recipe specification and running inference.
Unschedulable/single-call frameworks do
not persist resumable task state, though their publication errors still carry
completed measurements. An already-complete ID with no available integrations
returns its validated saved result with `already_complete=True`. It does not
rewrite saved state, reload the recipe, regenerate exports, or contact inference.
The CLI renders the no-op message; API callers receive a normal successful result.
Saved data for unavailable integrations remains intact.

`resume_benchmark(id, export_files=False)` disables optional exports after resumed
measurement. `output_file="/path/to/result.yaml"` chooses the export base path.
These options do not turn completed result loading or publication retry into an
export-repair operation. The default `export_files=True` matches initial execution.
A corrupt/missing validated result file still raises `BenchmarkFailed`.

## Authentication and checkpoint recovery

`BenchmarkOptions.api_key_env` names the environment/Variables key containing the
inference credential. An explicitly named missing or empty key is an error for
real measurement; previews do not resolve authentication. When
omitted, the runtime's resolved recipe credential can supply authentication.
A missing optional dotenv parser does not change the missing-key diagnostic;
when installed, its `.env` source remains available.
Authentication is injected only into framework command construction. It is not
part of task definitions, measurement arguments, benchmark IDs, result metadata,
exports, or publication snapshots. Core command/stream logging redacts the active
key. Shared metadata projections remove API-key fields and the runtime-native
`auth_token` alias, and redact literal `--api-key`/`--auth-token` arguments.
Credential templates remain intact. Frameworks must also keep execution
credentials out of their own artifacts and custom recipe fields.

For authentication, checkpoints record only `api_key_env` and whether it is required.
`resume_benchmark(id, api_key_env="INFERENCE_API_KEY")` can supply or replace that
reference; otherwise resume resolves the saved reference again. Rotating the
variable's value does not change measurement identity. Runtime authentication
can be resolved again from the running job's recipe and recorded launch overrides.
Legacy structured credential fields are removed when loaded; an incomplete run
without a recoverable credential reference requires a current `api_key_env`.
Completed result loading and publication-only retries do not resolve credentials.
Serialized plugin documents are sanitized by their owner when bound; core does
not decode arbitrary caller/plugin strings as recipe YAML.

New scheduled checkpoints preserve a credential-free recipe specification and
serving overrides, including in-memory recipe edits. Resume-by-ID restores these
inputs without consulting the current recipe source. Before state-bound hooks or
measurement, it verifies the measurement fingerprint/ID and checks running-job
hosts, model/runtime, and the effective job fingerprint when recorded. Local
exports include serving overrides. The checkpoint and returned result record the
actual launched job ID while retaining the stable intent used for measurement
identity.

Legacy checkpoints without a specification can resume only when the current
recipe and recorded overrides reproduce their original measurement ID. Successful
reconstruction is saved as a specification on the next normal checkpoint. Changed
or unverifiable inputs require an explicit fresh run; no measurement hook or
command runs and validation does not rewrite the original state. This includes
legacy incomplete IDs whose hashes included a now-removed credential. Completed
results remain loadable by ID without reconstructing their recipe.

Resume-by-ID uses the saved timeout and failure policy. Optional `timeout=...`
and `exit_on_first_fail=...` keywords override them for the next execution and
subsequent resumes. Old checkpoints without these fields use the historical
14,400-second timeout and `exit_on_first_fail=False`. These execution overrides
do not turn publication-only retry into measurement. Initial `benchmark()` calls
continue to resolve their own caller/profile policy, including implicit resumes.

`exit_on_first_fail=False` continues to remaining scheduled tasks after a nonzero
exit or timeout. Each failed task is skipped for the rest of that invocation; a
later resume retries it while retaining completed tasks. An incomplete schedule
raises `BenchmarkFailed`. The existing single bounded gap-analysis pass may still
remeasure completed tasks with missing measurement coverage.
Coverage still missing after that pass leaves the schedule incomplete.

The scheduler removes a pending task's old JSON output before constructing its
next command. A zero exit with missing, invalid, or non-object JSON is a failed
attempt and remains resumable. Only usable artifacts of successful tasks enter
aggregation, in schedule order; failed/interrupted output and unrelated files in
`runs/` cannot satisfy coverage. Previously completed tasks with usable artifacts
are preserved. Saved successes whose artifacts are missing or unusable are retried
when measurement resumes. Successful publication requires the complete schedule.
The default framework coverage uses stable task identity, so failed tasks cannot
shift the identity of surviving rows. Frameworks may add semantic coverage checks.

A completed schedule whose result processing was interrupted is resumable too.
Resume restores the saved specification and finishes consolidation, parsing,
validation, and commit from accepted artifacts without a running inference server,
inference credentials, or command prerequisites. Publication runs only after that commit.
If artifacts are missing or invalid, only the affected tasks need measurement;
successful artifacts remain intact. This path is shared by resume-by-ID and
`benchmark()` reuse of an existing checkpoint.

Session warmup is independent of inference ownership. The first successful task
of a resumed measurement session still gets warmup; `BenchmarkOptions.skip_run`
controls inference launch, not scheduler warmup.

For single-call frameworks, any nonzero exit raises `BenchmarkFailed`, regardless
of `exit_on_first_fail`, even if the command emitted valid partial measurements.
Those outputs never trigger successful-completion/publication hooks. Inference
cleanup still runs on the failure path.

Scheduled and single-call frameworks share one process runner. Each task's timeout
covers process execution and output draining, with stdout/stderr drained together.
On timeout, interruption, or output-callback failure, the runner terminates its
owned POSIX process group and bounds its wait for the immediate child. Worker
pipes cannot hold the state lock past that cleanup. Plugins must keep workers in
the owned group; detached services require their own lifecycle management. Output
callbacks run synchronously and must return promptly.

Execution and resume distinguish absent state from unusable state. Missing state
permits a new benchmark; malformed YAML, invalid field shapes, unsupported schema
versions, or read errors raise `SparkrunError` without replacing the checkpoint.
The explicit `ResumeMode.FRESH` path can discard malformed prior state without
first decoding it. A fresh dry run previews that operation and preserves files.
Low-level `BenchmarkRunState.load(..., strict=True)` raises `BenchmarkStateError`;
lenient reads remain available for inventory. Loading never rewrites files;
sanitized state is persisted on the next normal save.

## Caller metadata

`BenchmarkOptions.state_extras` is copied into newly created scheduled state.
Use application-owned names (for example, `my_app.experiment`) for caller metadata.
JSON strings remain strings even when named `recipe_yaml` or
`effective_recipe_text`; these names do not impose a YAML schema on caller data.
Generic credential-field/command redaction still applies.
Core reserves `framework_version`, `benchmark_integrations`, `benchmark_category`,
`benchmark_outputs`, `measurement_complete`, `measurement_started_at`,
`measurement_completed_at`, `measurement_context_version`, `container_image`, `container_image_sha`,
`container_image_longterm_ref`, and `container_image_longterm_pinned`.
Do not seed or overwrite these keys. Integration-specific persistence belongs
in `context.data`; older Arena metadata is read only for migration compatibility.

See [plugin lifecycle contracts](PLUGINS.md#benchmark-integrations) and
[migration notes](DISTRIBUTION_API_MIGRATION.md) for plugin and caller changes.

## Executor endpoint requirements

Benchmarking currently requires launch hosts to identify a reachable serving
endpoint. A provider that only submits control-plane resources declares
`Executor.supports_host_endpoint = False`. Native Kubernetes benchmarking is
therefore rejected before submission, including `skip_run=True`, rather than
constructing a controller-local URL. Resuming pending native tasks is rejected
before endpoint discovery or measurement commands. Resume-by-ID can still finish
accepted artifacts or retry publication without a live inference endpoint.

The private orchestration record is `BenchmarkExecution`, with one `outputs`
mapping. Its former `output_csv`, `output_json`, and `output_yaml` properties
have been removed; public `BenchmarkResult.outputs` is unchanged.

## Effective measurement provenance

The execution path records the actual launch image, available digest/archival
reference, effective serving overrides, runtime information, and effective
recipe before measurement commands run. Skip-run uses the selected deployment's
saved effective image. Requested recipe specifications continue to determine
measurement identity; resolved launch values describe what was measured.

One private context serializer/restorer owns these values together with category
and measurement timestamps. Context version 2 preserves an intentionally unknown
image without substituting the recipe declaration. Legacy processing-only state
with no image evidence returns an empty public `container_image` and a null image
in exported/plugin provenance. It does not need live inference to finish parsing.

`BenchmarkResult.measured_at` and `completed_at` expose the recorded measurement
interval, matching the integration snapshot. Publication retries do not advance
these timestamps. Partial recovery retains the initial start and updates completion
when the remaining tasks finish.

YAML exports and integration provenance share one effective recipe projection.
Its image preference is recorded archival reference, digest, then actual launch
reference. `raw_container` preserves the declaration. Exported recipe text includes
the effective image and serving overrides without modifying the original Recipe.
The YAML recipe `hash` describes that text, and `declared_hash` describes the
unmodified declared input. Integration provenance retains its existing declared
`recipe.hash` and adds `recipe.effective_hash` for its separate `recipe_yaml`.
The exported benchmark mapping also includes category and the measurement interval.

Additional core-reserved state extras are `container_image_sha_pinned`,
`measurement_runtime_info`, `measurement_overrides`, and `measurement_recipe_state`.
These are private persistence details, not an integration extension interface.


Pending-task resume compares the currently reported serving image with recorded
measurement provenance before accepting more commands. Known different references
require an explicit fresh benchmark. Identical references and references sharing
a recorded SHA-256 digest are accepted; different mutable tags are not assumed
equivalent. This comparison uses recorded references, not a live content audit of
an unchanged mutable tag.

Direct resume, automatic resume, and processing recovery that discovers missing
artifacts use the same check. Accepted recovery preserves the original recipe,
overrides, runtime information, image reference, digest, and archival reference
as one measurement context. New live metadata cannot relabel old rows. Unknown
historical image provenance stays unknown, even when a later deployment provides
an image. Processing-only recovery with complete artifacts remains independent
of live inference and does not perform this deployment check.
