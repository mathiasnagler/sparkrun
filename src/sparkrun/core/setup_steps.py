"""Shared, per-host setup plans and plugin step registration.

Registration is cheap and never probes hosts. Checks and actions are loaded only
when a caller builds a plan. Application features express policy; applicability
and prerequisites remain mandatory even when a user explicitly enables a step.
"""

from __future__ import annotations

from sparkrun.core.registration import enlist_registry_state, register_unique

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sparkrun.core.setup_actions import SetupActionContext, SetupActionResult

from sparkrun.core.setup_models import CheckContext, CheckItem, HostState, FAIL, OK, SKIP


@dataclass(frozen=True)
class SetupStep:
    key: str
    label: str
    checks: tuple[Callable[[HostState, CheckContext], CheckItem | None], ...] = ()
    applicability: Callable[[HostState, CheckContext], str] | None = None
    apply: Callable[[HostState, CheckContext, SetupActionContext], SetupActionResult] | None = None
    requires: tuple[str, ...] = ()
    feature_flag: str | None = None
    probe_script: str = ""
    order: int = 100
    undo: Callable[[str, dict, SetupActionContext], SetupActionResult] | None = None
    requires_sudo: bool = True


@dataclass
class PlannedStep:
    step: SetupStep
    host: str
    selected: bool
    reason: str = ""
    checks: list[CheckItem] = field(default_factory=list)
    blocked_by: tuple[str, ...] = ()

    @property
    def needs_action(self) -> bool:
        return (
            self.selected and not self.blocked_by and self.step.apply is not None and any(i.status in (FAIL, "warn") for i in self.checks)
        )


_STEPS: dict[str, SetupStep] = {}

enlist_registry_state(globals(), "_STEPS")
_CONSTRAINTS: dict[str, Callable[[str, HostState, CheckContext], str]] = {}

enlist_registry_state(globals(), "_CONSTRAINTS")
_BUILTIN_KEYS = frozenset(
    {"hardware", "docker", "docker_group", "nvidia_container", "nvidia_cdi", "host_ipc", "earlyoom", "sudoers", "ssh_mesh", "cx7", "rdma"}
)


def register_setup_step(step: SetupStep) -> None:
    """Register a plugin step; conflicts fail the plugin registration transaction."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", step.key) or step.key in _BUILTIN_KEYS:
        raise ValueError("Invalid or reserved setup step ID: %s" % step.key)
    from sparkrun.core.features import get_feature

    if step.feature_flag is None or get_feature(step.feature_flag) is None:
        raise ValueError("Plugin setup steps must declare a registered feature flag")
    if not isinstance(step.probe_script, str) or any(not callable(check) for check in step.checks):
        raise TypeError("Setup steps require callable checks and a shell probe string")
    if any(callback is not None and not callable(callback) for callback in (step.applicability, step.apply, step.undo)):
        raise TypeError("Setup applicability, apply, and undo hooks must be callable")
    register_unique(_STEPS, step.key, step, description="Setup step")


def register_setup_constraint(name: str, constraint: Callable[[str, HostState, CheckContext], str]) -> None:
    """Add hardware-specific exclusions to core or plugin steps.

    A constraint returns an empty string to leave a step's eligibility unchanged,
    or a reason to exclude it. It cannot enable a step or suppress the mandatory
    hardware check. Registration performs no probes and rolls back with plugins.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        raise ValueError("Invalid setup constraint ID: %s" % name)
    if not callable(constraint):
        raise TypeError("Setup constraints must be callable")
    register_unique(_CONSTRAINTS, name, constraint, description="Setup constraint")


def setup_constraint_reason(key: str, state: HostState, ctx: CheckContext) -> str:
    """Return the first plugin exclusion for a step on measured target hardware."""
    if key == "hardware":
        return ""
    for constraint in _CONSTRAINTS.values():
        reason = constraint(key, state, ctx)
        if not isinstance(reason, str):
            raise TypeError("Setup constraints must return a string")
        if reason:
            return reason
    return ""


def step_enabled(key: str, config=None) -> bool:
    from sparkrun.core.features import is_feature_enabled

    return is_feature_enabled("setup.steps." + key, config=config)


def _host_executor(state, ctx):
    if ctx.strict and state.facts.get("CHECK_OS") != "Linux":
        return "host actions require a Linux target"
    if ctx.strict and not state.hardware:
        return "hardware has not been identified"
    if ctx.strict and state.host not in ctx.executor_names:
        return "executor could not be resolved"
    if ctx.executor_names.get(state.host, "docker") not in {"docker", "local"}:
        return "executor manages its own hosts"
    return ""


def _docker(state, ctx):
    return _host_executor(state, ctx) or (
        "not using the Docker executor" if ctx.executor_names.get(state.host, "docker") != "docker" else ""
    )


def _earlyoom(state, ctx):
    return _host_executor(state, ctx) or (
        "requires apt and systemd"
        if ctx.strict and (state.facts.get("CHECK_APT") != "1" or state.facts.get("CHECK_SYSTEMD") != "1")
        else ""
    )


def _mesh(state, ctx):
    return _host_executor(state, ctx) or ("only needed for a multi-host cluster" if not ctx.multi_host else "")


def _hardware_check(state, ctx):
    if not ctx.strict:
        return None
    if not state.reachable:
        return CheckItem("hardware", "Target hardware", FAIL, state.error or "host is unreachable")
    if state.hardware is None:
        return CheckItem(
            "hardware",
            "Target hardware",
            FAIL,
            state.error or "hardware probe did not return usable metadata",
            "Check SSH access and the selected hardware integration",
        )
    return CheckItem(
        "hardware", "Target hardware", OK, ", ".join(a.model for a in state.hardware.accelerators) or "no accelerator detected"
    )


def _executor_check(state, ctx):
    if not ctx.strict or state.hardware is None or not state.reachable:
        return None
    name = ctx.executor_names.get(state.host)
    return CheckItem(
        "executor",
        "Cluster executor",
        OK if name else FAIL,
        name or "executor could not be resolved",
        "Check the cluster executor configuration and enabled executor plugins",
    )


def builtin_steps() -> tuple[SetupStep, ...]:
    from sparkrun.core import setup_checks as c
    from sparkrun.core import setup_actions as a

    return (
        SetupStep("hardware", "Target hardware", (_hardware_check, _executor_check), order=0),
        SetupStep("docker", "Docker installed", (c._check_docker_installed,), _docker, order=10),
        SetupStep(
            "docker_group", "Docker access", (c._check_docker_group, c._check_docker_usable), _docker, a.docker_group, ("docker",), order=20
        ),
        SetupStep("nvidia_container", "NVIDIA Container Toolkit", (c._check_nvidia_ctk,), _docker, order=30),
        SetupStep("nvidia_cdi", "NVIDIA CDI spec", (c._check_cdi_spec,), _docker, a.nvidia_cdi, ("docker", "nvidia_container"), order=40),
        SetupStep("host_ipc", "Host IPC", (c._check_host_ipc,), _host_executor, order=50),
        SetupStep("earlyoom", "earlyoom OOM protection", (c._check_earlyoom,), _earlyoom, a.earlyoom, order=60),
        SetupStep("sudoers", "Scoped sudoers rules", (c._check_sudoers,), _host_executor, a.sudoers, order=70),
        SetupStep("ssh_mesh", "SSH mesh", (c._check_ssh_mesh,), _mesh, order=80),
        SetupStep("cx7", "CX7 networking", (c._check_cx7,), _mesh, order=90),
        SetupStep("rdma", "RDMA fabric", (c._check_rdma,), _mesh, order=95),
    )


def all_setup_steps() -> tuple[SetupStep, ...]:
    return _order_setup_steps({s.key: s for s in (*builtin_steps(), *_STEPS.values())})


def _order_setup_steps(steps: dict[str, SetupStep]) -> tuple[SetupStep, ...]:
    """Validate and order a complete step graph for apply or undo."""
    ordered: list[SetupStep] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key):
        if key in visiting:
            raise ValueError("Setup step dependency cycle at %s" % key)
        if key in visited:
            return
        if key not in steps:
            raise ValueError("Unknown setup prerequisite: %s" % key)
        visiting.add(key)
        for dependency in steps[key].requires:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)
        ordered.append(steps[key])

    for step in sorted(steps.values(), key=lambda s: (s.order, s.key)):
        visit(step.key)
    return tuple(ordered)


def build_setup_plan(state: HostState, ctx: CheckContext) -> list[PlannedStep]:
    from sparkrun.core.features import is_feature_enabled
    from sparkrun.core.application_profile import get_application_profile

    plan: dict[str, PlannedStep] = {}
    for step in all_setup_steps():
        enabled = step.key == "hardware" or is_feature_enabled(step.feature_flag or "setup.steps." + step.key, config=ctx.config)
        reason = "disabled by application or user policy" if not enabled else ""
        if not reason and not state.reachable:
            reason = "host is unreachable" if step.key != "hardware" else ""
        if not reason:
            reason = setup_constraint_reason(step.key, state, ctx)
        if not reason and step.applicability:
            reason = step.applicability(state, ctx)
        checks = [item for check in step.checks if (item := check(state, ctx)) is not None] if not reason else []
        blocked = tuple(
            key
            for key in step.requires
            if not plan[key].selected or plan[key].blocked_by or any(i.status == FAIL for i in plan[key].checks)
        )
        if blocked and not reason:
            reason = "prerequisites unavailable: " + ", ".join(blocked)
            checks = []
        command = get_application_profile().command
        checks = [replace(item, guidance=item.guidance.replace("sparkrun ", command + " ")) for item in checks]
        plan[step.key] = PlannedStep(step, state.host, not reason, reason, checks, blocked)
    return list(plan.values())


def evaluate_host(state: HostState, ctx: CheckContext) -> list[CheckItem]:
    return [item for entry in build_setup_plan(state, ctx) if entry.selected for item in entry.checks]


def apply_setup_step(key: str, state: HostState, ctx: CheckContext, action_context: SetupActionContext) -> SetupActionResult:
    """Recheck policy/applicability/prerequisites before invoking an action."""
    from sparkrun.core.setup_actions import SetupActionResult

    entry = next((s for s in build_setup_plan(state, ctx) if s.step.key == key), None)
    if entry is None:
        raise ValueError("Unknown setup step: %s" % key)
    if not entry.needs_action:
        return SetupActionResult(
            state.host, SKIP, entry.reason or ("blocked by " + ", ".join(entry.blocked_by) if entry.blocked_by else "no action needed")
        )
    if state.hardware is None or not state.reachable:
        return SetupActionResult(state.host, SKIP, "target hardware is unknown or unreachable")
    if action_context.dry_run:
        return SetupActionResult(state.host, SKIP, "would apply " + entry.step.label)
    from sparkrun.core.setup_actions import validate_action_result

    return validate_action_result(entry.step.apply(state, ctx, action_context), state.host)


def setup_probe_script(config=None) -> str:
    """Only enabled plugin steps contribute read-only shell probes."""
    from sparkrun.core.features import is_feature_enabled

    return "\n".join(
        "(\n%s\n)" % s.probe_script for s in _STEPS.values() if s.probe_script and is_feature_enabled(s.feature_flag, config=config)
    )
