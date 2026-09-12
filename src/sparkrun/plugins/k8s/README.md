# Kubernetes plugin

The in-tree `k8s` plugin owns the Kubernetes executor, kubectl acquisition,
cluster inventory/probing, service-account setup, Kueue/JobSet operations, and
the Kubernetes API launch path. Its Python package is `sparkrun.plugins.k8s`.

## Activation

The loading gate is `integration.k8s`:

| Application | Stable | Beta | Alpha |
| --- | --- | --- | --- |
| Sparkrun | Off | Off | On |

These are **core feature channels**, selected by `features.channel`. Sparkrun
uses its update channel when that setting is absent. Application profile defaults
can override these defaults independently of the core feature channel. Explicit
configuration or product-specific environment settings can override profile defaults.

```bash
sparkrun setup features enable integration.k8s
sparkrun setup k8s --help
sparkrun setup plugins list
```

The corresponding environment override is `SPARKRUN_FEATURE_INTEGRATION_K8S=1`.
Alternate applications use their own command and environment prefix.

When disabled, startup does not import the plugin or register its executor,
setup commands, run handler, or child features. Only the loading gate appears
in `setup features list`. Child feature overrides cannot load the parent plugin.
After enabling the plugin, these features register with a default of **on**:

- `executor.k8s`: makes the executor available for `executor: k8s` workloads.
- `cli.setup.k8s`: exposes `setup k8s` and its subcommands.
- `api.run.k8s`: routes Kubernetes runs through the JobSet launcher.

Each child can be disabled independently. Disabling `api.run.k8s` retains the
older Pod-command executor path. Disabling the parent removes all Kubernetes
registrations on the next invocation; saved Kubernetes configuration is retained.

## Package layout and Python API

- `executor.py`: `K8sExecutor` and `K8sExecutorConfig`.
- `config.py`: `K8sSettings(config)` reads/writes the existing `k8s:` config block.
- `api/`: console-free operations, data models, and typed API errors.
- `orchestration/`: kubectl, target discovery, manifests, probes, and scheduling.
- `run.py`: the JobSet launch handler registered with core.
- `cli.py`: a lazy builder registered under `setup`, keeping Click out of API startup.

Python callers import the plugin API explicitly:

```python
from sparkrun.plugins.k8s import api as k8s
from sparkrun.plugins.k8s.config import K8sSettings
from sparkrun.plugins.k8s.executor import K8sExecutorConfig
```

The former `sparkrun.api.k8s`, `sparkrun.orchestration.k8s`, and
`sparkrun.orchestration.executors.k8s` modules moved here. Kubernetes config
accessors now belong to `K8sSettings`, and Kubernetes executor fields belong to
`K8sExecutorConfig`; the persisted YAML keys are unchanged. The normal
`resolve_executor()` chain chooses that config class automatically.

## Submission and logs

`launch_jobset()` and `run_launcher_job()` return after submission and do not
attach to terminal output. Their former `follow` argument is removed in 0.4.0.
Read logs explicitly through the same plugin API:

```python
from contextlib import closing
from sparkrun.plugins.k8s import api as k8s

submitted = k8s.run_launcher_job(
    name="example",
    image="my-launcher:latest",
    command=["my-workload"],
)
with closing(k8s.logs(name=submitted.job_name, kind="job", namespace=submitted.namespace, follow=True)) as lines:
    for line in lines:
        consume(line)  # Send the LogLine to your application's UI or log sink.
```

For a JobSet use `name=submitted.name, kind="jobset"` (the default kind).
Forward the same kubeconfig/context/namespace used for submission. Ownership and
target checks run when `logs()` is called; the local reader starts on iteration.
`follow=False` reads available output; `tail=N` limits kubectl's requested tail.
Kubectl stdout/stderr are merged into captured `LogLine` records; a nonzero reader
exit raises `SparkrunError` after any diagnostic lines. Closing the iterator stops
and reaps the local reader, leaving the Kubernetes workload running.

The CLI's `setup k8s launch --follow` and `setup k8s run-job --follow` render this
iterator and stop reading on Ctrl-C. Dry-run never reads logs. The native
`api.run()` / `run` JobSet path returns after submission regardless of
`RunOptions.follow`; use the explicit log API for attachment.

## Resource identity and result contracts

Native `api.run()` keeps its portable `RunResult.cluster_id` unchanged and derives
a bounded Kubernetes JobSet name with a digest. The actual resource name is in
`result.metadata["k8s_jobset"]`; pass it to `logs()`, `jobset_status()`, and
`stop_jobset()` with the original target settings. The JobSet annotations retain
`sparkrun.cluster_id` and `sparkrun.recipe_fingerprint`. Explicit `launch_jobset()`
callers supply a Kubernetes name directly and may provide `annotations`.

Before replacement/submission, JobSet naming checks cover the headless Service
and the longest generated pod hostname, including replicated-job and index
suffixes. The checks follow [JobSet's DNS naming rules](https://jobset.sigs.k8s.io/docs/concepts/#dns-hostnames-for-pods).
Invalid explicit names fail during preparation. Portable workload IDs are never
renamed globally to meet these substrate rules.

The native serve port comes from the recipe configuration plus `overrides["port"]`,
with the same 8000 fallback as the core launcher. The resolved integer is passed
to command generation and returned in `RunResult.serve_port`; values outside
1–65535 fail before cluster preparation. Custom commands must honor the recipe's
configured port (for example, with a `{port}` template placeholder).

Lifecycle/log operations reject foreign application ownership before mutation
or reading. Operational failures raise `SparkrunError` subtypes with their cause;
invalid resource arguments raise `ValueError`, and interrupts propagate unchanged.

## Existing launch limits

The automatic `run` JobSet path currently supports a single pod and a homogeneous
GPU class. Multi-rank/hybrid layouts use the lower-level `setup k8s launch` command.
Kueue and JobSet must be installed before actual submission. The Pod-command
executor remains experimental and does not support Ray clusters.

Tests use mocked Kubernetes calls and dry-run manifests; they do not provision a
cluster or establish physical-device compatibility.
