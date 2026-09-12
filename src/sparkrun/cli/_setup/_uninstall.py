"""sparkrun setup uninstall — reverse setup wizard changes."""

from __future__ import annotations

from sparkrun.core.application_profile import render_identity_text

from sparkrun.core.application_profile import get_application_profile

import logging
import os
import sys

import click

from sparkrun.cli._common import HIDE_ADVANCED_OPTIONS

from sparkrun.core.setup_undo_actions import BUILTIN_UNDO_ORDER as TEARDOWN_PHASES

logger = logging.getLogger(__name__)

# Phases where default confirmation is N (dangerous)
DANGEROUS_PHASES = {"cx7", "ssh_mesh", "docker_group"}


def _confirm_phase(phase: str, description: str, yes: bool) -> bool:
    """Confirm a teardown phase with the user."""
    default = phase not in DANGEROUS_PHASES
    if yes:
        click.echo("  %s: %s" % (phase, description))
        return True
    return click.confirm("  %s: %s — proceed?" % (phase, description), default=default)


def _check_running_containers(host_list, ssh_kwargs):
    """Check for running sparkrun containers on hosts. Returns list of (host, container_name)."""
    from sparkrun.orchestration.ssh import run_remote_scripts_parallel

    from sparkrun.core.ownership import owns_resource
    from sparkrun.orchestration.executors.docker import _parse_docker_labels

    script = "docker ps --format '{{.Names}}\t{{.Labels}}' 2>/dev/null || true"
    try:
        results = run_remote_scripts_parallel(host_list, script, timeout=15, quiet=True, **ssh_kwargs)
    except Exception:
        return []

    found = []
    for r in results:
        if r.success and r.stdout.strip():
            for name in r.stdout.strip().splitlines():
                container, _, labels = name.strip().partition("\t")
                if owns_resource(container, _parse_docker_labels(labels)):
                    found.append((r.host, container))
    return found


@click.command("uninstall", hidden=HIDE_ADVANCED_OPTIONS)
@click.argument("cluster_name", required=False, default=None)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmations (full teardown)")
@click.option("--dry-run", "-n", is_flag=True, help="Preview without executing")
@click.option("--keep-cluster", is_flag=True, help="Keep local cluster definition")
@click.option("--phase", "phases", multiple=True, help="Only uninstall specific phase(s)")
@click.option("--force", is_flag=True, help="Proceed even if running containers detected")
@click.pass_context
def setup_uninstall(ctx, cluster_name, yes, dry_run, keep_cluster, phases, force):
    """Reverse setup wizard changes for a cluster.

    Undoes setup phases in reverse order: earlyoom, sudoers, docker group,
    CX7 networking, SSH mesh. Each phase can be confirmed individually.

    Dangerous phases (CX7, SSH mesh, docker group) default to N unless --yes.

    \b
    Examples:
      {app_command} setup uninstall
      {app_command} setup uninstall mylab
      {app_command} setup uninstall --dry-run
      {app_command} setup uninstall --phase earlyoom --phase sudoers
      {app_command} setup uninstall --yes --keep-cluster
    """
    from sparkrun.core.cluster_manager import ClusterManager
    from sparkrun.core.config import SparkrunConfig, get_config_root
    from sparkrun.core.setup_manifest import ManifestManager
    from sparkrun.orchestration.primitives import build_ssh_kwargs

    from sparkrun.api.setup import SetupFailed

    config = SparkrunConfig()
    config_root = get_config_root()
    cluster_mgr = ClusterManager(config_root)
    manifest_mgr = ManifestManager(cluster_mgr.clusters_dir)

    # ── Resolve cluster ──────────────────────────────────────────
    if not cluster_name:
        cluster_name = cluster_mgr.get_default()
    if not cluster_name:
        click.echo("Error: No cluster specified and no default cluster set.", err=True)
        click.echo(render_identity_text("Usage: {app_command} setup uninstall [CLUSTER_NAME]"), err=True)
        sys.exit(1)

    from contextlib import nullcontext

    try:
        with manifest_mgr.recording(cluster_name) if not dry_run else nullcontext():
            try:
                cluster_def = cluster_mgr.get(cluster_name)
            except Exception:
                click.echo("Error: Cluster '%s' not found." % cluster_name, err=True)
                sys.exit(1)

            host_list = list(cluster_def.hosts)
            user = cluster_def.user or os.environ.get("USER", "root")

            click.echo()
            click.echo("Uninstall setup for cluster '%s'" % cluster_name)
            click.echo("=" * 48)
            click.echo("  Hosts: %s" % ", ".join(host_list))
            click.echo("  User:  %s" % user)
            click.echo()

            # ── Load manifest ────────────────────────────────────────────
            try:
                manifest = manifest_mgr.load(cluster_name, strict=True)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            if manifest is None and get_application_profile().id != "sparkrun":
                raise click.ClickException("No setup manifest owned by this application; refusing to guess which host changes to undo")
            if manifest is None:
                click.echo("Warning: No setup manifest found for '%s'." % cluster_name)
                click.echo("Will infer from cluster definition. Dangerous phases default to N.")
                click.echo()

            # ── Build SSH kwargs ─────────────────────────────────────────
            ssh_kwargs = build_ssh_kwargs(config)
            if user:
                ssh_kwargs["ssh_user"] = user

            # ── Container check ──────────────────────────────────────────
            if not dry_run:
                running = _check_running_containers(host_list, ssh_kwargs)
                if running:
                    click.echo(render_identity_text("Warning: Running {app_command} containers detected:"))
                    for host, name in running:
                        click.echo("  %s: %s" % (host, name))
                    click.echo()
                    if not force:
                        if not yes and not click.confirm("Continue with uninstall? (use --force to suppress)", default=False):
                            click.echo("Aborted.")
                            return
                        elif yes:
                            click.echo("Proceeding despite running containers (--yes).")
                    else:
                        click.echo("Proceeding (--force).")
                    click.echo()

            # ── Sudo helper ──────────────────────────────────────────────
            sudo_password = None

            def _ensure_sudo_password():
                nonlocal sudo_password
                if sudo_password is not None:
                    return sudo_password
                if dry_run:
                    return None

                from sparkrun.orchestration.ssh import run_remote_scripts_parallel

                try:
                    test_results = run_remote_scripts_parallel(host_list, "sudo -n true", quiet=True, timeout=10, **ssh_kwargs)
                    if all(r.success for r in test_results):
                        return None
                except Exception:
                    pass

                sudo_password = click.prompt("[sudo] password for %s" % user, hide_input=True)
                return sudo_password

            from sparkrun.api.setup import run_setup_undo
            from sparkrun.core.setup_actions import SetupActionContext
            from sparkrun.core.setup_manifest import SetupManifest, PhaseRecord
            from sparkrun.core.setup_undo import remaining_setup_changes, available_undo_steps

            recorded = manifest is not None
            if manifest is None:
                # Explicit legacy inference stays frontend-owned. No invented
                # plugin records, and partial cleanup still retains the cluster.
                manifest = SetupManifest(
                    1, cluster_name, "", "", user, host_list, {key: PhaseRecord(True, "", list(host_list)) for key in TEARDOWN_PHASES}
                )
            steps = available_undo_steps(manifest)
            phase_filter = set(phases) if phases else None
            if phase_filter and phase_filter - steps.keys():
                raise click.ClickException(
                    "No teardown implementation for: %s; load the owning plugin before uninstalling its changes"
                    % ", ".join(sorted(phase_filter - steps.keys()))
                )
            unhandled = remaining_setup_changes(manifest).keys() - steps.keys()
            if unhandled:
                click.echo("Keeping cluster and manifest: no teardown implementation for %s." % ", ".join(sorted(unhandled)))

            def approve(step, hosts):
                return _confirm_phase(step.key, "Undo %s on %d host(s)" % (step.label, len(hosts)), yes)

            def progress(event):
                if event.kind in {"selected", "preview"}:
                    click.echo(("[dry-run] " if dry_run else "") + "Phase: %s" % event.step)
                    click.echo("-" * 30)
                elif event.kind == "result":
                    click.echo("    [%s] %s: %s" % (event.status.upper(), event.hosts[0], event.detail))

            result = run_setup_undo(
                manifest,
                SetupActionContext(user, ssh_kwargs, dry_run=dry_run),
                manifest_mgr=manifest_mgr if recorded else None,
                steps=steps,
                only_steps=phase_filter,
                approve=approve,
                credentials=_ensure_sudo_password,
                progress_callback=progress,
            )
            summary = {
                key: {"ok": "removed", "skip": "skipped", "fail": "failed"}.get(status, status) for key, status in result.steps.items()
            }
            if dry_run and not keep_cluster and not (remaining_setup_changes(manifest).keys() - (phase_filter or steps.keys())):
                click.echo("[dry-run] Cluster would be deleted after successful teardown of every recorded phase.")
            if not result.complete:
                keep_cluster = True
                click.echo("Keeping cluster and manifest: unresolved setup changes: %s." % ", ".join(result.remaining))

            # ── Cluster cleanup ──────────────────────────────────────────
            if not keep_cluster:
                desc = "Delete cluster '%s' and manifest" % cluster_name
                if yes or click.confirm("  %s — proceed?" % desc, default=True):
                    if not dry_run:
                        cluster_mgr.delete(cluster_name)
                        manifest_mgr.delete(cluster_name)
                    summary["cluster"] = "deleted"
                    click.echo("  Cluster '%s' %s." % (cluster_name, "would be deleted" if dry_run else "deleted"))
                else:
                    summary["cluster"] = "kept"
            else:
                summary["cluster"] = "kept"

            # ── Summary ──────────────────────────────────────────────────
            click.echo()
            click.echo("Uninstall Summary")
            click.echo("=" * 48)
            for key, val in summary.items():
                click.echo("  %-14s %s" % (key + ":", val))
            click.echo()
    except (ValueError, OSError, SetupFailed) as exc:
        raise click.ClickException(str(exc)) from exc
