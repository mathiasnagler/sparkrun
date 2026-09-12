"""Resolve the effective kube target (kubeconfig / context / namespace).

Precedence (highest first):

1. Explicit arguments (CLI flags).
2. sparkrun config ``k8s.{kubeconfig,context,namespace}``.
3. Environment ``KUBECONFIG`` (kubeconfig only).
4. ``None`` — let ``kubectl`` fall back to its own defaults
   (``~/.kube/config`` and the current-context).
"""

from __future__ import annotations

from ..config import K8sSettings

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sparkrun.core.config import SparkrunConfig


@dataclass
class KubeTarget:
    """A resolved kube target.  Any field may be ``None`` (kubectl default)."""

    kubeconfig: str | None = None
    context: str | None = None
    namespace: str | None = None


def resolve_kube_target(
    config: "SparkrunConfig | None" = None,
    *,
    kubeconfig: str | None = None,
    context: str | None = None,
    namespace: str | None = None,
) -> KubeTarget:
    """Combine CLI args, config, and env into a :class:`KubeTarget`."""
    k8s_cfg = K8sSettings(config).k8s_defaults if config is not None else {}

    resolved_kubeconfig = kubeconfig or k8s_cfg.get("kubeconfig") or os.environ.get("KUBECONFIG") or None
    if resolved_kubeconfig:
        resolved_kubeconfig = os.path.expanduser(str(resolved_kubeconfig))

    resolved_context = context or k8s_cfg.get("context") or None
    resolved_namespace = namespace or k8s_cfg.get("namespace") or None

    return KubeTarget(
        kubeconfig=resolved_kubeconfig,
        context=resolved_context,
        namespace=resolved_namespace,
    )


__all__ = ["KubeTarget", "resolve_kube_target"]


def _pin_client_target(client) -> None:
    """Persist explicit context/path selection instead of mutable shell defaults."""
    from sparkrun.api._errors import SparkrunError
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
