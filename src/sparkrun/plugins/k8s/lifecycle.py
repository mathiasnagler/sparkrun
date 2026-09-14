"""Translate portable run identities to Kubernetes controller resources."""

from __future__ import annotations

import json
import time

from sparkrun.core.application_profile import get_application_profile
from sparkrun.core.cluster_status import ClusterStatus, ContainerDetail, HostOccupancy, RunningWorkload
from sparkrun.core.ownership import OWNER_LABEL, owns_resource
from sparkrun.orchestration.job_metadata import parse_cluster_id

from .orchestration.errors import K8sError, OwnershipError
from .orchestration.launch import jobset_status, stop_jobset
from .orchestration.names import validate_resource_name


def _native_jobsets(client):
    """List only this application's objects with a portable run identity."""
    response = client.run_json(["get", "jobsets", "-l", "%s=%s" % (OWNER_LABEL, get_application_profile().id), "-o", "json"])
    for resource in response.get("items", []):
        meta = resource.get("metadata", {})
        cluster_id = meta.get("annotations", {}).get("sparkrun.cluster_id")
        if cluster_id and owns_resource(cluster_id, meta.get("labels")):
            yield resource


def stop_native_workload(client, cluster_id, reference=None) -> int | None:
    """Delete the JobSet controller, check ownership/identity, and confirm absence."""
    if reference is not None:
        if not isinstance(reference, dict) or reference.get("kind") != "JobSet":
            raise K8sError("Unsupported native Kubernetes workload reference")
        name = reference.get("name")
        if not isinstance(name, str):
            raise K8sError("Kubernetes JobSet reference requires a name")
        validate_resource_name(name)
        resource = jobset_status(client, name)
        resources = [resource] if resource else []
    else:
        # Recover after a lost local metadata file; never guess a Pod name for
        # a JobSet discovered by portable identity.
        resources = [r for r in _native_jobsets(client) if r["metadata"]["annotations"]["sparkrun.cluster_id"] == cluster_id]
        if not resources:
            return None  # Permit the older, directly launched Pod path.
    removed = 0
    for resource in resources:
        meta = resource["metadata"]
        name = meta["name"]
        if meta.get("annotations", {}).get("sparkrun.cluster_id") != cluster_id:
            raise OwnershipError("Kubernetes JobSet %r belongs to another workload" % name)
        result = stop_jobset(client, name)
        if not result.success:
            raise K8sError("Failed to delete JobSet %s: %s" % (name, result.stderr.strip()))
        if jobset_status(client, name):
            raise K8sError("Deletion of JobSet %s was not confirmed" % name)
        removed += 1
    return removed


def query_native_status(client, hosts) -> ClusterStatus:
    """Report submitted JobSets, including queued workloads, in caller host scope.

    Hosts are control-plane launch scopes recorded by the native handler, not
    guessed Kubernetes node placements. Unreachable control planes are errors.
    """
    if not hosts:
        return ClusterStatus(executor="k8s", queried_at=time.time())
    by_host = {host: [] for host in hosts}
    try:
        for resource in _native_jobsets(client):
            meta = resource["metadata"]
            annotations = meta["annotations"]
            cluster_id = annotations["sparkrun.cluster_id"]
            try:
                intent_id, _ = parse_cluster_id(cluster_id)
                launch_hosts = json.loads(annotations.get("sparkrun.hosts", "[]"))
            except (ValueError, TypeError):
                continue
            if not isinstance(launch_hosts, list) or not all(isinstance(h, str) for h in launch_hosts):
                continue
            conditions = resource.get("status", {}).get("conditions", [])
            if any(c.get("type") in {"Completed", "Failed"} and str(c.get("status")).lower() == "true" for c in conditions):
                continue
            state = "Terminating" if meta.get("deletionTimestamp") else "Submitted"
            detail = ContainerDetail(
                name=meta["name"],
                role="jobset",
                status=state,
                image=annotations.get("sparkrun.container_image", ""),
                executor="k8s",
            )
            workload = RunningWorkload(
                cluster_id=cluster_id,
                intent_id=intent_id,
                recipe_name=annotations.get("sparkrun.recipe"),
                runtime_name=annotations.get("sparkrun.runtime"),
                container_ids=(meta.get("uid", meta["name"]),),
                containers=(detail,),
            )
            for host in launch_hosts:
                if host in by_host:
                    by_host[host].append(workload)
    except Exception as exc:
        return ClusterStatus(executor="k8s", queried_at=time.time(), errors={host: str(exc) for host in hosts})
    return ClusterStatus(
        hosts=tuple(HostOccupancy(host=h, workloads=tuple(ws), used_slots=len(ws)) for h, ws in by_host.items()),
        executor="k8s",
        queried_at=time.time(),
    )
