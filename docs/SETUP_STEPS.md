# Shared setup steps

`core.setup_steps` owns selection, feature policy, prerequisites, and action
dispatch. `core.setup_runner` owns multi-step sequencing, recording changes,
and reprobes before dependent actions. `core.setup_models` contains check/host context models;
`core.setup_probe` obtains target facts and resolves executors. The CLI only
renders results, collects credentials, and confirms changes. The existing
interactive SSH-mesh/CX7 topology adapters consume the same selected plan;
topology prompts remain in the wizard. Generic host actions and plugin actions
run through the shared dispatcher.

A `SetupStep` has:

- A unique `key`, user-facing `label`, and `feature_flag`.
- `checks`: callbacks `(HostState, CheckContext) -> CheckItem | None`.
- Optional `applicability(state, context) -> str`: empty means applicable;
  a nonempty string explains why the step is omitted.
- Optional `requires`: prerequisite step IDs. Missing dependencies and cycles
  are errors; disabled/inapplicable or failing prerequisites block the step.
- Optional `probe_script`: a read-only Bash fragment emitting namespaced
  `KEY=VALUE` facts. It runs in a subshell only when the step is enabled.
- Optional `apply(state, context, action_context) -> SetupActionResult`.
- Optional `undo(host, recorded_details, action_context) -> SetupActionResult`.
- `order` for stable presentation among independent steps and `requires_sudo`
  (default true) for apply/undo credential collection.

Declare feature definitions on the plugin as usual, then register its steps in
`register(v)`. Registration is transactional and must not probe hosts or import
Click. For example:

```python
from sparkrun.core.features import FeatureFlag
from sparkrun.core.setup_models import CheckItem, OK, WARN
from sparkrun.core.setup_steps import SetupStep, register_setup_step

FEATURE_DEFINITIONS = (
    FeatureFlag("setup.steps.example_service", "Check example service", default=True),
)


def check_service(state, context):
    active = state.facts.get("EXAMPLE_SERVICE_ACTIVE") == "1"
    return CheckItem("example_service", "Example service", OK if active else WARN)


def register(v):
    register_setup_step(SetupStep(
        "example_service", "Example service",
        checks=(check_service,),
        feature_flag="setup.steps.example_service",
        probe_script='systemctl is-active --quiet example && echo EXAMPLE_SERVICE_ACTIVE=1 || echo EXAMPLE_SERVICE_ACTIVE=0',
    ))
```

A plugin being off means its steps and feature definitions are not registered.
Turning a step off excludes its probe and findings. Application profiles can
supply baseline and release-channel defaults for registered step features.

`setup features list` omits `setup.steps.*` by default, including in JSON output.
Use the hidden `--all` option (`setup features list --all`, optionally with
`--json`) to include every registered feature. Setup-step flags remain available
to the normal `enable`, `disable`, and `reset` commands.
Applicability runs against each actual target, so mixed clusters need not select
the same steps. Profile defaults do not establish hardware support.

A plugin's complete setup dependency graph is validated inside its registration
transaction, even for disabled steps. Forward references between steps in the
same module are allowed. Cross-module prerequisites must already be registered;
loading does not import or defer a missing provider automatically. Keep tightly
coupled steps in one module, or explicitly arrange provider loading first. A
missing prerequisite or cycle rejects the plugin and rolls back its registry
contributions. Required-plugin failures still block launches, while unrelated
setup/undo remains available. Direct step registration outside a module loader
must complete its graph before calling a planner.

Call `sparkrun.application.initialize()` first to load the selected integrations.

`probe_setup_hosts(hosts, ssh_kwargs=..., config=..., cluster=...)` returns a host
state mapping and a `CheckContext`. The hardware comes from the existing combined
probe, including selected hardware-plugin enrichers. `build_setup_plan(state,
context)` returns selected and skipped entries with reasons and findings.
`apply_setup_step(key, state, context, action_context)` rechecks selection and
prerequisites before invoking the callback. Actions cannot run without identified,
reachable target hardware. Dry-run returns a preview without invoking callbacks.

`SetupActionContext` carries the SSH user/options, ephemeral sudo credentials,
and optional transport dispatcher. Its `run()` method executes a sudo script and
rejects execution during preview. Return `SetupActionResult(host, status, detail,
changed=False, extra={})`; mark `changed` only for actual changes. The wizard
records changed hosts and their details, including partially successful changes,
and refreshes readiness before dependent actions through the shared runner.
Results and manifests must not contain passwords or other credentials.

Undo callbacks receive the details for the specific changed host. They run only
for selected uninstall phases recorded in the application's own manifest. Step
feature settings do not prevent undoing earlier changes while the plugin is
loaded. If a recorded plugin has no available teardown implementation, uninstall
keeps the cluster and manifest so those changes remain discoverable. Supply undo for reversible plugin-owned changes; shared system resources
without a safe reversal (such as NVIDIA CDI configuration) should remain in place.
Do not uninstall pre-existing packages or remove configuration another tool owns.

## Hardware constraints on shared steps

A hardware plugin can exclude unsupported core or plugin steps with
`register_setup_constraint(name, callback)`. The callback receives
`(step_key, HostState, CheckContext)` and returns an empty string to leave the
normal eligibility checks unchanged, or a nonempty reason to exclude that step.
It cannot enable a step or suppress mandatory hardware identification.

```python
from sparkrun.core.setup_steps import register_setup_constraint


def constrain_setup(key, state, context):
    managed = state.hardware and any(
        "vendor-managed-os" in accelerator.capabilities
        for accelerator in state.hardware.accelerators
    )
    if managed and key == "earlyoom":
        return "The hardware integration manages OOM protection"
    return ""


def register(v):
    register_setup_constraint("vendor-managed-os", constrain_setup)
```

Constraints are registered only while the plugin is loaded and participate in
registration rollback. Disabling an advisory step does not remove the plugin's
hardware constraints. Plans and actions apply these restrictions per host;
CX7/RDMA follow-up probes also honor their corresponding exclusions. Callbacks
must use the supplied facts without probing or modifying hosts. Device-specific
implementations and qualification tests belong to the hardware integration.


## Running setup from another frontend

`sparkrun.api.setup.run_setup_steps(states, context, action_context, ...)` drives
the same generic/plugin actions as the CLI. Obtain states/context using
`probe_setup_hosts`. Pass the resolved cluster as `cluster=`; its name is inferred
from the cluster or `context.cluster_name`. Optional `cluster_name=` must agree
with them. Mismatched names are rejected before actions.

Pass `manifest_mgr=ManifestManager(...)` to record changes for later teardown;
recording requires a resolved cluster name. `manifest_mgr=None` explicitly disables
recording, as for standalone CLI setup commands using only `--hosts`. A real
recorded operation strictly validates existing manifest ownership/content before
approval, credentials, or actions. It holds the manifest lock through actions,
recording, and reprobes. Manifest writes use atomic replacement. Wizard topology
actions and uninstall participate in the same lock; `ManifestManager.recording(name)`
is available for frontends orchestrating their own recorded actions.

`only_steps` optionally restricts selected IDs; normal feature, applicability,
and prerequisite checks still apply. Plans are indexed once for each facts/context
revision and refreshed after reprobes; each actual action still rechecks its
eligibility. The returned `SetupRunResult` contains:

- `steps`: step ID to aggregate status using `fail > warn > ok > skip`.
  Aggregation is independent of host order; skipped hosts are neutral when other
  hosts perform work. Already-satisfied steps report `ok`. An explicitly requested
  blocked/inapplicable step reports `fail` for that host.
- `outcomes`: step ID to host to `SetupActionResult`, retaining status, detail,
  `changed`, and recorded `extra` data. Declined actions report `skip` with a reason.
  SSH-mesh/CX7 steps report `skip` and name the required frontend topology adapter.
- Refreshed host `states`, `context`, final `plans`, and the readiness `findings`
  count. Action status and final observed readiness remain distinct.

Per-host action failures are recorded while other eligible steps continue.
Partially successful changes are recorded per host before frontend notification.
Reprobes decide whether dependent actions can proceed; a successful action return
is not assumed to establish its prerequisites.

Optional callbacks are synchronous on the invoking thread:

- `approve(step, hosts) -> bool`: allow eligible actions. Omitted means apply.
- `credentials() -> str | None`: obtain an ephemeral sudo password when needed.
  Alternatively, supply it in `SetupActionContext`.
- `progress_callback(SetupEvent)`: receive `preview`, `selected`, `result`, or
  `error` events with step/label/hosts/status/detail, never credentials.

The API translates orchestration, preflight, recording, and callback failures to
`SetupFailed`, a `SparkrunError`, preserving the cause. Existing typed API errors
and interrupts propagate unchanged. Per-host action failures belong in the
returned outcomes instead of aborting the entire operation.

Dry runs invoke no actions, credential/approval callbacks, manifest locks/writes,
or reprobes. They still return outcomes and emit preview events. The lower-level
`apply_setup_step` performs only one checked action and does not record/reprobe
for its caller. Action callbacks must return one of `ok`, `warn`, `fail`, or `skip`.


## Complete headless example

The caller-facing models, status constants, probe/plan helpers, apply dispatcher,
and recording runners are exported by `sparkrun.api.setup`. They are the same
core types; plugin registration remains in `core.setup_steps`.

```python
import getpass
from sparkrun.application import initialize
from sparkrun.api.setup import (
    ManifestManager, SetupActionContext, probe_setup_hosts, run_setup_steps,
)
from sparkrun.orchestration.primitives import build_ssh_kwargs

session = initialize(config_path="/path/to/config.yaml")
cluster = session.cluster_manager.get("lab")
operation = session.for_cluster(cluster)
ssh = build_ssh_kwargs(operation.config)
states, checks = probe_setup_hosts(
    cluster.hosts, ssh_kwargs=ssh, config=operation.config, cluster=cluster,
)
records = ManifestManager(session.cluster_manager.clusters_dir)
action = SetupActionContext(operation.config.ssh_user or getpass.getuser(), ssh)
result = run_setup_steps(
    states, checks, action, cluster=cluster, manifest_mgr=records,
    approve=lambda step, hosts: frontend_approve(step.label, hosts),
    credentials=frontend_sudo_password,
)
```

The example's two `frontend_*` functions are supplied by the embedding application.
For one checked action without recording/reprobes, use `apply_setup_step`;
`run_setup_steps` supplies the recorded multi-step lifecycle. Probe/plan and
single-action helpers are low-level operations whose validation errors remain
`ValueError`/`TypeError`; the recording runners provide the typed `SetupFailed`
error boundary.

Recorded operations validate manifest schema/version, scalar fields, host lists,
phase records, and core-owned detail shapes before approval, credentials, or
host actions. Valid legacy defaults and arbitrary plugin detail keys are retained.
Corrupt or future-version manifests are preserved and rejected.

## Undo and partial cleanup

`api.setup.run_setup_undo(manifest, action_context, manifest_mgr=...)` executes
loaded plugin undo in reverse dependency order, then built-in undo. The CLI uses
this same runner; built-in scripts and mesh-key handling are console-free.
Feature flags do not disable undoing recorded changes while their plugin is
loaded. The supplied manifest identifies the cluster; a manager reloads its
current validated state under the recording lock. With `manifest_mgr=None`, the
runner validates the supplied snapshot and leaves persistence to the caller.
Both routes reject foreign ownership, unsupported versions, and invalid field
shapes before callbacks. The original input object is never mutated.

```python
from sparkrun.api.setup import run_setup_undo

manifest = records.load("lab", strict=True)
if manifest is not None:
    undone = run_setup_undo(
        manifest, action, manifest_mgr=records,
        approve=lambda step, hosts: frontend_approve(step.label, hosts),
        credentials=frontend_sudo_password,
    )
    show_remaining_changes(undone.remaining)
```

`SetupUndoResult.steps` contains aggregate statuses; `outcomes` retains each
host's status/detail; `remaining` maps unresolved phase IDs to host tuples.
`manifest` is the detached updated snapshot, including timestamps, unresolved
per-host details, and original mesh peers needed for retry. With a manager it
matches the authoritative saved state at return. In preview it is unchanged.
A caller with its own storage can persist it directly:

```python
undone = run_setup_undo(manifest, action)  # caller owns storage and locking
external_store.save(undone.manifest)
```

The snapshot is returned on normal completion, including per-host failures.
Use a manifest manager when successful targets must be saved incrementally
before a later orchestration or frontend callback can raise.
`complete` means no reversible recorded change remains. Successful hosts are
removed from their phase records before frontend result notification. No later
host success can clear a warning/failure on another host. Missing plugins,
filtered phases, declined actions, and invalid results remain unresolved.
Caller-supplied `steps` mappings are also executed in reverse dependency order,
regardless of insertion order. Their prerequisites are merged with registered
knowledge, then the shared setup graph validator orders execution. Unknown
prerequisites and cycles fail before callbacks. `order` breaks ties between
independent steps; built-in undo retains its established tie ordering.
`only_steps` restricts execution, never authorizes discarding other records.
Unresolved recorded dependents block prerequisite undo on the same host. This
includes transitive dependencies and dependents outside `only_steps` or an
explicit `steps` mapping. Blocked outcomes are `skip` with the dependent IDs in
the detail, and their records remain. Independent hosts/steps can still complete.
Approval and credentials are requested only for hosts eligible for actual undo.
The CLI deletes the cluster/manifest only after all reversible recorded changes
are resolved, unless `--keep-cluster` requests retention. The runner itself
never deletes the cluster or manifest.

Undo must return `ok` for confirmed removal or an already-absent change. Return
`skip`, `warn`, or `fail` when cleanup remains unconfirmed. Apply and undo validate
the same `SetupActionResult` shape/status. Per-host callback exceptions become
failed outcomes; orchestration/recording/notification failures raise `SetupFailed`
and interrupts propagate. Preview invokes no undo, credentials, or approvals,
and does not change records. Its unresolved records describe actual current
state, not a claim that hypothetical cleanup already succeeded.

NVIDIA CDI configuration is deliberately retained shared state; post-CX7 mesh
tracking is covered by ordinary mesh teardown. These two phases do not prevent
completion. Mesh retries still collect keys from the original peer list even
when an earlier attempt already cleaned some hosts. A missing/unreachable key
source remains an explicit incomplete cleanup.

## Hard prerequisites versus advisory checks

A disabled/inapplicable prerequisite or a `FAIL` check blocks dependents.
`WARN` is advisory even when it offers an action. For a hard prerequisite, write:

```python
from sparkrun.api.setup import CheckItem, OK, FAIL

def check_required_service(state, context):
    ready = state.facts.get("MY_SERVICE_READY") == "1"
    return CheckItem("my_service", "Required service", OK if ready else FAIL)
```

A dependent step declares `requires=("my_service",)`. The runner reprobes after
reported changes, so successful action status alone does not satisfy that
requirement. Use `WARN` for optional recommendations that do not prevent use.
