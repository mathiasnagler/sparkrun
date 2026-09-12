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
`measurement_started_at` and `measurement_completed_at` in state extras;
integration snapshots expose them as `measured_at` and `completed_at`. Publication
retries preserve this interval while `updated_at` continues advancing. A resume
that measures remaining tasks retains the start of the earlier data and records
the completion of the combined measurement. Legacy states lacking the interval
pin their first observed pre-retry state timestamp as a best available fallback;
that cannot reconstruct lost historical timing. Arena uses the same interval.

Completed scheduled measurements can retry saved integrations without inference
running or reloading the recipe. Incomplete measurement resumes still require a
resolvable recipe and running inference. Unschedulable/single-call frameworks do
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
Authentication is injected only into framework command construction. It is not
part of task definitions, measurement arguments, benchmark IDs, result metadata,
exports, or publication snapshots. Core command/stream logging redacts the active
key. Shared metadata projections remove API-key fields and the runtime-native
`auth_token` alias, and redact literal `--api-key`/`--auth-token` arguments.
Credential templates remain intact. Frameworks must also keep execution
credentials out of their own artifacts and custom recipe fields.

New checkpoints record only `api_key_env` and whether authentication is required.
`resume_benchmark(id, api_key_env="INFERENCE_API_KEY")` can supply or replace that
reference; otherwise resume resolves the saved reference again. Rotating the
variable's value does not change measurement identity. Runtime authentication
can be resolved again from the recipe and recorded launch overrides. Legacy
checkpoints with embedded keys are sanitized when loaded; an incomplete run
without a recoverable credential reference requires a current `api_key_env`.
Completed result loading and publication-only retries do not resolve credentials.
Old authenticated runs may have IDs derived from their key; resume those by ID.

Resume-by-ID uses the saved timeout and failure policy. Optional `timeout=...`
and `exit_on_first_fail=...` keywords override them for the next execution and
subsequent resumes. Old checkpoints without these fields use the historical
14,400-second timeout and `exit_on_first_fail=False`. These execution overrides
do not turn publication-only retry into measurement. Initial `benchmark()` calls
continue to resolve their own caller/profile policy, including implicit resumes.

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
Core reserves `framework_version`, `benchmark_integrations`, `benchmark_category`,
`benchmark_outputs`, `measurement_complete`, `measurement_started_at`,
`measurement_completed_at`, `container_image`, `container_image_sha`,
`container_image_longterm_ref`, and `container_image_longterm_pinned`.
Do not seed or overwrite these keys. Integration-specific persistence belongs
in `context.data`; older Arena metadata is read only for migration compatibility.

See [plugin lifecycle contracts](PLUGINS.md#benchmark-integrations) and
[migration notes](DISTRIBUTION_API_MIGRATION.md) for plugin and caller changes.
