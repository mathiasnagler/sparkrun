# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/sparkroute/LICENSE_EXCEPTION.

"""Optional host application profile API; legacy hosts retain their Sparkrun behavior."""

from __future__ import annotations

import importlib
import os
from pathlib import Path


def host_api():
    """Only a missing application profile module means legacy; other import errors propagate."""
    try:
        return importlib.import_module("sparkrun.core.application_profile")
    except ModuleNotFoundError as exc:
        if exc.name != "sparkrun.core.application_profile":
            raise
        if os.environ.get("SPARKRUN_APPLICATION_PROFILE"):
            raise RuntimeError("This child requires a host with application profile API support") from exc
        return None


def child_environment(config_path: Path | None = None) -> dict[str, str]:
    """Same-controller gateway and operation children retain profile/config identity."""
    api = host_api()
    environment = dict(os.environ)
    if api is not None:
        environment.update(api.child_environment(config_path=config_path))
    return environment


def worker_context(config_path: Path):
    if host_api() is not None:
        from sparkrun.application import initialize

        return initialize(config_path=config_path)
    from sparkrun import api
    from sparkrun.core.cluster_manager import ClusterManager
    from sparkrun.core.config import SparkrunConfig

    context = api.default_sctx()
    context.config = SparkrunConfig(config_path)
    context.cluster_manager = ClusterManager(config_path.parent)
    return context
