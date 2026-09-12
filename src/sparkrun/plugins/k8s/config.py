"""Kubernetes settings stored in the active application's existing config file."""

from __future__ import annotations

from typing import Any

from sparkrun.core.application_profile import thaw
from sparkrun.core.config import SparkrunConfig


class K8sSettings:
    """Typed access to the ``k8s:`` block; construction does not read or write files."""

    def __init__(self, config: SparkrunConfig):
        self.config = config

    @property
    def k8s_defaults(self) -> dict[str, Any]:
        """CLI / setup-time Kubernetes defaults (the ``k8s:`` block).

        Distinct from :attr:`executor_config` (which feeds executor-time
        ``kubeconfig`` / ``k8s_*`` overrides): this block holds the target
        a plain ``sparkrun setup k8s ...`` invocation defaults to, plus
        fallback target settings for executor resolution and native lifecycle.
        Explicit executor settings take precedence. This block also contains
        the ``kubectl`` binary settings (``path`` / ``version`` / per-
        context ``pinned`` versions).  Empty dict when unset.
        """
        cfg = self.config.effective_data.get("k8s")
        return dict(cfg) if isinstance(cfg, dict) else {}

    @property
    def k8s_launcher_image(self) -> str | None:
        """Container image for the in-cluster launcher Job (``k8s.launcher_image``).

        The job-driven launch path runs sparkrun's orchestration inside
        this image (typically a published sparkrun container).  ``None``
        when unset — callers must then supply an explicit image.
        """
        val = self.k8s_defaults.get("launcher_image")
        return str(val) if val else None

    def _kubectl_settings(self) -> dict[str, Any]:
        kubectl = self.k8s_defaults.get("kubectl")
        return kubectl if isinstance(kubectl, dict) else {}

    def _k8s_subsection(self, key: str) -> dict[str, Any]:
        sub = self.k8s_defaults.get(key)
        return sub if isinstance(sub, dict) else {}

    @property
    def kueue_version(self) -> str | None:
        """Pinned Kueue release to install (``k8s.kueue.version``)."""
        val = self._k8s_subsection("kueue").get("version")
        return str(val) if val else None

    @property
    def jobset_version(self) -> str | None:
        """Pinned JobSet release to install (``k8s.jobset.version``)."""
        val = self._k8s_subsection("jobset").get("version")
        return str(val) if val else None

    @property
    def kubectl_path(self) -> str | None:
        """Explicit ``kubectl`` binary path override (``k8s.kubectl.path``)."""
        val = self._kubectl_settings().get("path")
        return str(val) if val else None

    @property
    def kubectl_version(self) -> str | None:
        """Pinned ``kubectl`` version (``k8s.kubectl.version``)."""
        val = self._kubectl_settings().get("version")
        return str(val) if val else None

    def kubectl_pinned_version(self, context: str | None) -> str | None:
        """Server-matched ``kubectl`` version pinned for *context*, if any."""
        if not context:
            return None
        pinned = self._kubectl_settings().get("pinned")
        if isinstance(pinned, dict):
            val = pinned.get(context)
            return str(val) if val else None
        return None

    def pin_kubectl_version(self, context: str, version: str) -> None:
        """Persist a per-context ``kubectl`` version pin under ``k8s.kubectl.pinned``."""
        k8s = thaw(self.config._data.get("k8s"))
        if not isinstance(k8s, dict):
            k8s = {}
        kubectl = k8s.get("kubectl")
        if not isinstance(kubectl, dict):
            kubectl = {}
        pinned = kubectl.get("pinned")
        if not isinstance(pinned, dict):
            pinned = {}
        pinned[context] = version
        kubectl["pinned"] = pinned
        k8s["kubectl"] = kubectl
        self.config.set("k8s", k8s)
