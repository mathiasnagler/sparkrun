"""Console-free undo dispatch and tracking of unresolved setup changes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace

from sparkrun.core.setup_actions import SetupActionContext, SetupActionResult, validate_action_result, aggregate_action_status
from sparkrun.core.setup_manifest import ManifestManager, SetupManifest, remove_phase_hosts
from sparkrun.core.setup_models import FAIL, OK, SKIP
from sparkrun.core.setup_runner import SetupEvent
from sparkrun.core.setup_steps import SetupStep, all_setup_steps

# Shared system configuration is deliberately retained. Post-CX7 mesh changes
# are reversed by the ordinary ssh_mesh phase.
RETAINED_SETUP_PHASES = frozenset({"nvidia_cdi", "ssh_mesh_post_cx7"})


@dataclass
class SetupUndoResult:
    steps: dict[str, str]
    outcomes: dict[str, dict[str, SetupActionResult]]
    remaining: dict[str, tuple[str, ...]]

    @property
    def complete(self) -> bool:
        """No reversible recorded change remains, including unknown plugins."""
        return not self.remaining


def remaining_setup_changes(manifest: SetupManifest) -> dict[str, tuple[str, ...]]:
    return {key: tuple(record.hosts) for key, record in manifest.phases.items() if record.applied and key not in RETAINED_SETUP_PHASES}


def available_undo_steps(manifest: SetupManifest) -> dict[str, SetupStep]:
    """Loaded plugin undo in reverse dependency order, then built-in actions."""
    from sparkrun.core.setup_undo_actions import builtin_undo_steps

    steps = {step.key: step for step in reversed(all_setup_steps()) if step.undo is not None}
    steps.update(builtin_undo_steps(manifest))
    return steps


def run_setup_undo(
    manifest: SetupManifest,
    action_context: SetupActionContext,
    *,
    manifest_mgr: ManifestManager | None = None,
    steps: Mapping[str, SetupStep] | None = None,
    only_steps: set[str] | None = None,
    approve: Callable[[SetupStep, tuple[str, ...]], bool] | None = None,
    credentials: Callable[[], str | None] | None = None,
    progress_callback: Callable[[SetupEvent], None] | None = None,
) -> SetupUndoResult:
    """Undo recorded changes and retain every target without confirmed success.

    A manager reloads authoritative state and records each successful target
    while holding its lock. Without one, operate on a detached manifest only.
    Caller-supplied steps replace the default built-in/loaded plugin undo set.
    Plugin undo uses reverse dependency order, independent of feature gates.
    Only OK confirms removal (including already absent). Preview/decline never
    invokes undo or credentials and leaves every record unresolved.
    """
    statuses: dict[str, str] = {}
    outcomes: dict[str, dict[str, SetupActionResult]] = {}

    def emit(event):
        if progress_callback is not None:
            progress_callback(event)

    recording = manifest_mgr.recording(manifest.cluster) if manifest_mgr and not action_context.dry_run else nullcontext()
    with recording:
        if manifest_mgr is not None:
            manifest = manifest_mgr.load(manifest.cluster, strict=True)
            if manifest is None:
                raise ValueError("Setup undo requires an existing manifest")
        manifest = deepcopy(manifest)
        available = dict(steps) if steps is not None else available_undo_steps(manifest)
        if only_steps is not None and (unknown := only_steps - available.keys()):
            raise ValueError("No teardown implementation for: " + ", ".join(sorted(unknown)))
        if any(key != step.key or not callable(step.undo) for key, step in available.items()):
            raise ValueError("Undo steps must be keyed by their ID and provide an undo callback")
        for key, step in available.items():
            record = manifest.phases.get(key)
            if record is None or not record.applied or key in RETAINED_SETUP_PHASES or (only_steps is not None and key not in only_steps):
                continue
            hosts = tuple(record.hosts)
            per_host = outcomes[key] = {}
            emit(SetupEvent("preview" if action_context.dry_run else "selected", key, step.label, hosts))
            accepted = not action_context.dry_run and (approve is None or approve(step, hosts))
            action = replace(
                action_context,
                sudo_password=(credentials() if credentials else action_context.sudo_password) if accepted and step.requires_sudo else None,
            )
            for host in hosts:
                if not accepted:
                    outcome = SetupActionResult(host, SKIP, "would undo" if action_context.dry_run else "declined by caller")
                else:
                    details = deepcopy(record.extra.get("host_details", {}).get(host, record.extra))
                    try:
                        outcome = validate_action_result(step.undo(host, details, action), host)
                    except Exception as exc:
                        outcome = SetupActionResult(host, FAIL, str(exc))
                    if outcome.status == OK:
                        if manifest_mgr is not None:
                            manifest_mgr.clear_phase_hosts(manifest.cluster, key, [host])
                        remove_phase_hosts(manifest, key, [host])
                per_host[host] = outcome
                emit(SetupEvent("result", key, step.label, (host,), outcome.status, outcome.detail))
            statuses[key] = aggregate_action_status(per_host)
        return SetupUndoResult(statuses, outcomes, remaining_setup_changes(manifest))
