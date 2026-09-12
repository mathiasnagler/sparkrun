"""Substrate naming rules; portable workload IDs remain unchanged."""

import hashlib
import re


def validate_resource_name(name: str) -> None:
    """Validate a Kubernetes DNS-subdomain resource reference."""
    if not isinstance(name, str) or len(name) > 253 or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", name):
        raise ValueError("Kubernetes operations require a resource name")
    if any(not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", part) for part in name.split(".")):
        raise ValueError("Kubernetes operations require a resource name")


def _validate_label(name: str, *, initial_letter: bool = False) -> None:
    # JobSet's headless Service also needs the RFC1035 initial letter.
    pattern = r"[a-z](?:[-a-z0-9]*[a-z0-9])?" if initial_letter else r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
    if not isinstance(name, str) or len(name) > 63 or not re.fullmatch(pattern, name):
        raise ValueError(
            "JobSet resource name %r must be a DNS label%s (at most 63 characters)"
            % (name, " starting with a letter" if initial_letter else "")
        )


def validate_jobset_names(name: str, replicated_jobs) -> None:
    """Check the Service and longest generated pod hostname before submission."""
    _validate_label(name, initial_letter=True)
    for job, replicas in replicated_jobs:
        _validate_label(job)
        if type(replicas) is not int or replicas < 1:
            raise ValueError("JobSet replicas must be a positive integer")
        _validate_label("%s-%s-%d-0" % (name, job, replicas - 1))


def native_jobset_name(cluster_id: str, model: str) -> str:
    """Project a solo portable ID to a bounded name, retaining a digest on truncation."""
    _validate_label(model)
    max_length = 63 - len("-%s-0-0" % model)
    digest = hashlib.sha256(cluster_id.encode()).hexdigest()[:16]
    prefix = "run-" + re.sub(r"[^a-z0-9-]+", "-", cluster_id.lower()).strip("-")
    if max_length < 20:
        raise ValueError("GPU model token is too long for a native JobSet name")
    name = prefix[: max_length - len(digest) - 1].rstrip("-") + "-" + digest
    validate_jobset_names(name, [(model, 1)])
    return name
