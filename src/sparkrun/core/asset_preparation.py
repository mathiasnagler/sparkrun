"""Shared tuning preparation for launches and preparation-only builds."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def prepare_tuning(
    recipe,
    runtime,
    host_list,
    *,
    cluster,
    registry_mgr=None,
    sync_tuning=True,
    dry_run=False,
    transfer_mode="auto",
    ssh_kwargs=None,
    strict=False,
) -> None:
    """Sync and distribute tuning files, without creating a workload.

    Launches retain best-effort behavior; standalone builds use strict=True
    so failed staging is not reported as successfully prepared.
    """
    ssh_kwargs = ssh_kwargs or {}
    if sync_tuning and not dry_run and registry_mgr is not None:
        from sparkrun.tuning.sync import sync_registry_tuning

        try:
            synced = sync_registry_tuning(
                registry_mgr,
                recipe.runtime,
                dry_run=dry_run,
                registry_name=recipe.source_registry,
            )
            if synced:
                logger.info("Synced %d tuning config(s) from registries.", synced)
        except Exception:
            if strict:
                raise
            logger.debug("Failed to sync tuning configs", exc_info=True)

    # Distribute tuning configs to remote hosts.  The tuning cache lives under
    # the SSH user's $HOME, so it hits the same shared-filesystem conditions the
    # model cache does; its prefs inherit `distribution.model` unless the
    # cluster spells out a `distribution.tuning` block (see
    # ClusterDistributionConfig.tuning_prefs).
    if not runtime.is_delegating_runtime():
        from sparkrun.tuning._common import tuning_configs_present
        from sparkrun.tuning.distribute import distribute_tuning_to_hosts, ensure_remote_tuning_dirs
        from sparkrun.tuning.sync import _get_local_tuning_dir

        _dist_cfg = getattr(cluster, "distribution", None)
        _tuning_prefs = getattr(_dist_cfg, "tuning_prefs", None)
        _tuning_enabled = getattr(_tuning_prefs, "enabled", True)

        try:
            # Create the tuning directory on every host before anything mounts
            # it — and deliberately *outside* the enabled/skip_fan_out checks
            # below.  Those govern whether we copy configs there; the bind
            # mount happens either way, decided from the control node's copy,
            # so a host missing the path has it created root-owned by the
            # Docker daemon and is locked out of its own tuning cache from then
            # on.  Gated by the same predicate as the mount so the two cannot
            # drift apart.
            if tuning_configs_present(_get_local_tuning_dir(recipe.runtime)):
                directory_failed = ensure_remote_tuning_dirs(
                    recipe.runtime,
                    host_list,
                    dry_run=dry_run,
                    **ssh_kwargs,
                )
                if strict and directory_failed:
                    raise RuntimeError("Tuning directory preparation failed on: %s" % ", ".join(directory_failed))
        except Exception:
            if strict:
                raise
            logger.debug("Failed to ensure remote tuning directories", exc_info=True)

        try:
            if not _tuning_enabled:
                logger.debug("Tuning distribution disabled for this cluster; skipping")
                tuning_failed = []
            else:
                tuning_failed = distribute_tuning_to_hosts(
                    recipe.runtime,
                    host_list,
                    dry_run=dry_run,
                    transfer_mode=transfer_mode,
                    preserve_perms=getattr(_tuning_prefs, "preserve_perms", True),
                    skip_fan_out=getattr(_tuning_prefs, "skip_fan_out", False),
                    **ssh_kwargs,
                )
            if tuning_failed:
                if strict:
                    raise RuntimeError("Tuning config distribution failed on: %s" % ", ".join(tuning_failed))
                logger.warning(
                    "Tuning config distribution failed on: %s",
                    ", ".join(tuning_failed),
                )
        except Exception:
            if strict:
                raise
            logger.debug("Failed to distribute tuning configs", exc_info=True)
