"""Experimental ``sparkrun run`` path for the Kubernetes JobSet launcher.

Gated behind the ``api.run.k8s`` feature flag (registered by the enabled k8s plugin).  When the
resolved executor is ``k8s`` *and* the flag is on, :func:`sparkrun.api.run`
delegates here instead of the SSH-oriented ``launch_inference`` path.

This first integration is intentionally narrow: **solo (single-pod)**
launches on a **homogeneous** cluster.  It reuses the runtime's own
``resolve_container`` / ``generate_command`` to produce the image + serve
command, resolves the single GPU class from the node inventory, and submits
a Kueue-admitted JobSet via :func:`sparkrun.plugins.k8s.api.launch_jobset`.

Multi-node and hybrid runs raise a clear error pointing at
``sparkrun setup k8s launch`` (the lower-level surface that already handles
per-rank GPU-class layout).  Kueue + JobSet must be installed first
(``sparkrun setup k8s kueue --install``).
"""

from __future__ import annotations

import logging
import json
from sparkrun.core.run_handlers import BeforeStart
from typing import TYPE_CHECKING

from sparkrun.api._errors import SparkrunError
from sparkrun.api._models import RunOptions, RunPlan, RunResult

if TYPE_CHECKING:
    from sparkrun.core.context import SparkrunContext

logger = logging.getLogger(__name__)


def run_k8s(
    options: RunOptions,
    sctx: "SparkrunContext",
    *,
    plan: RunPlan,
    started_at: float,
    before_start: BeforeStart | None = None,
) -> RunResult:
    """Submit a solo k8s JobSet launch and return a :class:`RunResult`."""
    from .api._ops import _launch_jobset

    recipe, runtime = plan.recipe, plan.runtime
    host_list, placement, is_solo = plan.host_list, plan.placement, plan.is_solo
    cluster_id = plan.cluster_id

    world_size = 1 if is_solo else (placement.total_ranks if placement is not None else (len(host_list) or 1))
    if world_size != 1:
        raise SparkrunError(
            "The experimental k8s run path (api.run.k8s) currently supports solo "
            "single-pod launches only; use `sparkrun setup k8s launch` for multi-node "
            "(requested world_size=%d)." % world_size
        )

    overrides = dict(options.overrides)
    raw_port = recipe.build_config_chain(overrides).get("port")
    if raw_port is None:
        raw_port = 8000
    try:
        if isinstance(raw_port, bool) or not isinstance(raw_port, (str, int)):
            raise ValueError
        serve_port = int(raw_port)
        if not 1 <= serve_port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise SparkrunError("Inference port must be an integer between 1 and 65535") from exc
    overrides["port"] = serve_port
    image = runtime.resolve_container(recipe, overrides)
    serve_command = runtime.generate_command(recipe, overrides, is_cluster=False, num_nodes=1)

    # Use the same caller/recipe/cluster/default chain as other executor paths.
    from sparkrun.orchestration.executor import resolve_executor

    executor = resolve_executor(
        cli_overrides=options.executor_overrides(),
        recipe=recipe,
        cluster=plan.cluster,
        runtime=runtime,
        config=sctx.config,
        v=sctx.variables,
        rootless=not options.rootful,
        auto_user=not options.rootful,
    )
    exec_cfg = executor.config
    client = executor._client()
    if not options.dry_run:
        _pin_client_target(client)
        exec_cfg.kubeconfig, exec_cfg.k8s_context = client.kubeconfig, client.context
    namespace = client.namespace

    if options.ensure:
        from sparkrun.api._intent import find_running_intent
        from sparkrun.api._run import _already_running_result
        from sparkrun.orchestration.job_metadata import load_job_metadata
        from dataclasses import replace

        snapshot = executor.query_status(list(plan.candidate_hosts))
        if snapshot.errors:
            raise SparkrunError("Cannot check existing native workloads: %s" % snapshot.errors)
        match = find_running_intent(plan.intent_id, plan.candidate_hosts, status=snapshot)
        if match is not None:
            result = _already_running_result(match, plan=plan, options=options, started_at=started_at, sctx=sctx)
            saved = load_job_metadata(match.cluster_id, cache_dir=str(sctx.config.cache_dir)) or {}
            return replace(result, executor="k8s", recipe_fingerprint=saved.get("recipe_fingerprint", ""))

    model = _resolve_single_gpu_class(client)

    env = {str(k): str(v) for k, v in (getattr(recipe, "env", {}) or {}).items()}

    from .orchestration.names import native_jobset_name

    resource = {"kind": "JobSet", "name": native_jobset_name(cluster_id, model)}

    def record_before_start():
        from sparkrun.orchestration.job_metadata import save_job_metadata

        if before_start is not None:
            before_start(executor=executor)
        # Persist before submission: an interrupted/ambiguous apply must still
        # be recoverable through the common stop API after process restart.
        save_job_metadata(
            cluster_id,
            recipe,
            list(host_list),
            overrides=overrides,
            cache_dir=str(sctx.config.cache_dir),
            container_image=image,
            runtime=runtime,
            recipe_fingerprint=plan.recipe_fingerprint,
            cluster_name=plan.cluster.name if plan.cluster else None,
            executor=executor,
            native_resource=resource,
            sctx=sctx,
        )

    result = _launch_jobset(
        client,
        name=resource["name"],
        annotations={
            "sparkrun.cluster_id": cluster_id,
            "sparkrun.recipe_fingerprint": plan.recipe_fingerprint,
            "sparkrun.hosts": json.dumps(list(host_list)),
            "sparkrun.recipe": getattr(recipe, "qualified_name", "") or "",
            "sparkrun.runtime": runtime.runtime_name,
            "sparkrun.container_image": image,
        },
        labels=sctx.controller_identity.labels(),
        rank_models=[model],
        image=image,
        serve_command=serve_command,
        env=env,
        namespace=namespace,
        dry_run=options.dry_run,
        before_start=record_before_start,
    )

    metadata = {
        "recipe": getattr(recipe, "qualified_name", None) or getattr(recipe, "name", None),
        "model": getattr(recipe, "model", None),
        "container_image": image,
        "k8s_jobset": result.name,
        "k8s_gpu_class": model,
        "k8s_feasible": result.feasible,
    }
    return RunResult(
        cluster_id=cluster_id,
        host_list=tuple(host_list),
        placement=placement,
        scheduler=plan.scheduler,
        runtime=runtime.runtime_name,
        executor="k8s",
        started_at=started_at,
        dry_run=options.dry_run,
        is_solo=True,
        rc=0,
        serve_command=serve_command,
        container_image=image,
        serve_port=serve_port,
        metadata=metadata,
    )


def _pin_client_target(client) -> None:
    """Persist explicit context/path selection instead of mutable shell defaults."""
    import os
    from pathlib import Path

    source = client.kubeconfig or os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")
    if os.pathsep in source:
        raise SparkrunError("Native runs require a single kubeconfig file; select kubeconfig explicitly")
    client.kubeconfig = str(Path(source).expanduser().resolve())
    if not client.context:
        result = client.run(["config", "current-context"], check=True)
        client.context = result.stdout.strip()
        if not client.context:
            raise SparkrunError("Native runs require a Kubernetes context")


def _resolve_single_gpu_class(client) -> str:
    """Return the cluster's single GPU model, or raise if 0 / >1 classes."""
    from sparkrun.plugins.k8s.api._errors import ClusterUnreachable
    from sparkrun.plugins.k8s.orchestration.errors import K8sError
    from sparkrun.plugins.k8s.orchestration.inventory import probe_nodes

    try:
        nodes = probe_nodes(client, gpu_only=True)
    except K8sError as exc:
        raise ClusterUnreachable(str(exc)) from exc

    classes = sorted({n.hardware.accelerators[0].model for n in nodes if n.has_accelerators})
    if len(classes) != 1:
        raise SparkrunError(
            "The k8s run path needs exactly one GPU class in the cluster (found %s); "
            "use `sparkrun setup k8s launch` with an explicit layout for hybrid clusters." % (classes or "none")
        )
    return classes[0]


__all__ = ["run_k8s"]
