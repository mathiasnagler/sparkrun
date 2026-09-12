"""Read-only setup probing shared by the readiness command and wizard."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import shlex
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sparkrun.core.config import SparkrunConfig
    from sparkrun.core.cluster_manager import ClusterDefinition

from sparkrun.core.setup_models import CheckContext, HostState

logger = logging.getLogger(__name__)


def resolve_setup_context(
    states: Mapping[str, HostState],
    *,
    config: SparkrunConfig,
    cluster: ClusterDefinition | None = None,
    cluster_name: str | None = None,
    strict: bool = True,
) -> CheckContext:
    from sparkrun.orchestration.executor import resolve_executor

    ctx = CheckContext(cluster_name=cluster_name, multi_host=len(states) > 1, config=config, strict=strict)
    for host, state in states.items():
        if state.hardware is None:
            continue
        try:
            executor = resolve_executor(cluster=cluster, config=config, host_hardware=state.hardware, rootless=False, auto_user=False)
            ctx.executor_names[host] = executor.executor_name
            ctx.gpu_access_modes[host] = executor.config.gpu_access_mode
            if executor.executor_name == "docker":
                ctx.ipc_exposure[host] = executor.config.ipc or ""
            elif executor.executor_name == "local":
                ctx.ipc_exposure[host] = "host"
        except Exception:
            logger.debug("Could not resolve setup executor for %s", host, exc_info=True)
    return ctx


def probe_setup_hosts(
    hosts: Sequence[str],
    *,
    ssh_kwargs: dict[str, Any],
    config: SparkrunConfig,
    cluster: ClusterDefinition | None = None,
    cluster_name: str | None = None,
) -> tuple[dict[str, HostState], CheckContext]:
    """Probe only selected capabilities, never modifying cluster or host state."""
    from sparkrun.core.hardware_probe import generate_combined_probe_script, _parse_probe_result
    from sparkrun.core.setup_steps import step_enabled, setup_probe_script, setup_constraint_reason
    from sparkrun.orchestration.ssh import run_remote_script
    from sparkrun.scripts import read_script
    from sparkrun.utils.text import parse_kv_output

    hardware_script = generate_combined_probe_script()
    extension_script = setup_probe_script(config)

    def probe(host):
        peers = " ".join(h for h in hosts if h != host) if step_enabled("ssh_mesh", config) else ""
        readiness = read_script("setup_check.sh").format(peers=shlex.quote(peers))
        script = "(\n" + hardware_script + "\n)\n(\n" + readiness + "\n)\n" + extension_script
        state = HostState(host=host)
        try:
            result = run_remote_script(host, script, timeout=60, quiet=True, **ssh_kwargs)
        except Exception as exc:
            state.reachable = False
            state.error = "Setup probe failed: %s" % exc
            return state
        if not result.success or "CHECK_COMPLETE=1" not in result.stdout:
            state.reachable = False
            state.error = result.stderr.strip()[:160] or "setup probe did not complete"
            return state
        state.facts = parse_kv_output(result.stdout)
        try:
            if "SPARKRUN_PROBE_ACCEL_END" in result.stdout:
                state.hardware = _parse_probe_result(result.stdout)
        except Exception as exc:
            state.error = "Hardware identification failed: %s" % exc
        return state

    if not hosts:
        return {}, resolve_setup_context({}, config=config, cluster=cluster, cluster_name=cluster_name)
    with ThreadPoolExecutor(max_workers=min(len(hosts), 16)) as pool:
        states = dict(zip(hosts, pool.map(probe, hosts), strict=True))

    context = resolve_setup_context(states, config=config, cluster=cluster, cluster_name=cluster_name)
    reachable = [h for h, state in states.items() if state.reachable]
    if len(reachable) > 1 and step_enabled("cx7", config):
        from sparkrun.orchestration.networking import detect_cx7_for_hosts

        try:
            candidates = [h for h in reachable if not setup_constraint_reason("cx7", states[h], context)]
            if candidates:
                for host, detection in detect_cx7_for_hosts(candidates, ssh_kwargs=ssh_kwargs).items():
                    states[host].cx7 = detection
        except Exception:
            logger.debug("Setup CX7 probe failed", exc_info=True)
    if len(reachable) > 1 and step_enabled("rdma", config):
        from sparkrun.api.setup._rdma import _run_probe

        try:
            candidates = [h for h in reachable if not setup_constraint_reason("rdma", states[h], context)]
            for host, facts in (_run_probe(candidates, ssh_kwargs, dry_run=False) if candidates else {}).items():
                states[host].rdma = facts
        except Exception:
            logger.debug("Setup RDMA probe failed", exc_info=True)
    return states, context
