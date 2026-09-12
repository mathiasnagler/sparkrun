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

## Existing launch limits

The automatic `run` JobSet path currently supports a single pod and a homogeneous
GPU class. Multi-rank/hybrid layouts use the lower-level `setup k8s launch` command.
Kueue and JobSet must be installed before actual submission. The Pod-command
executor remains experimental and does not support Ray clusters.

Tests use mocked Kubernetes calls and dry-run manifests; they do not provision a
cluster or establish physical-device compatibility.
