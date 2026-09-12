"""Console-free host actions shared by setup commands and plugin step plans."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Any
from sparkrun.core.setup_models import OK, FAIL, WARN, SKIP

_DOCKER_GROUP_SCRIPT = """\
#!/bin/bash
set -uo pipefail
TARGET_USER="{user}"
if id -nG "$TARGET_USER" 2>/dev/null | grep -qw docker; then
    echo "DOCKER_GROUP=already_member"
else
    sudo -n usermod -aG docker "$TARGET_USER" 2>/dev/null
    if [ $? -eq 0 ]; then
        echo "DOCKER_GROUP=added"
    else
        echo "DOCKER_GROUP=needs_sudo"
        exit 1
    fi
fi
"""

_DOCKER_GROUP_FALLBACK_SCRIPT = """\
#!/bin/bash
set -euo pipefail
TARGET_USER="{user}"
usermod -aG docker "$TARGET_USER"
echo "DOCKER_GROUP=added"
"""


def _cdi_summary(stdout: str) -> str:
    """Extract the status line from nvidia CDI generate output.

    Returns the first recognized status line (GENERATED/SKIPPED/ERROR) so the
    wizard can show a one-line result per host instead of the raw script dump.
    """
    for kw in ("GENERATED:", "SKIPPED:", "ERROR:"):
        for line in stdout.strip().splitlines():
            line = line.strip()
            if line.startswith(kw):
                return line
    return stdout.strip()[:80]


def _docker_group_summary(stdout: str, user: str | None = None) -> str:
    """Extract status from docker-group script output."""
    label = "'%s' " % user if user else ""
    for line in stdout.strip().splitlines():
        if line.startswith("DOCKER_GROUP="):
            val = line.split("=", 1)[1]
            if val == "already_member":
                return "%salready a member" % label
            elif val == "added":
                return "added %sto docker group" % label
    return stdout.strip()[:80]


# ---------------------------------------------------------------------------
# Earlyoom process patterns — used to build --prefer / --avoid arguments
# ---------------------------------------------------------------------------

# Processes that should be killed first on OOM (inference workloads).
EARLYOOM_PREFER_PATTERNS = [
    "vllm",
    "VLLM",
    "sglang",
    "llama-server",
    "llama-cli",
    "trtllm",
    "tritonserver",
    "ray",
    "python3",
    "python",
]

# Processes to protect from OOM kill (system services).
EARLYOOM_AVOID_PATTERNS = [
    "systemd",
    "sshd",
    "dockerd",
    "containerd",
    "dbus-daemon",
    "NetworkManager",
]


def _earlyoom_summary(stdout: str) -> str:
    """Extract key status lines from earlyoom install output.

    Filters out noisy apt-get progress (Reading database ...) and
    returns only the meaningful status lines (INSTALLED/PRESENT/CONFIGURED/OK/ERROR).
    """
    keywords = ("INSTALLING:", "INSTALLED:", "PRESENT:", "CONFIGURED:", "OK:", "ERROR:")
    lines = [line.strip() for line in stdout.strip().splitlines() if any(line.strip().startswith(kw) for kw in keywords)]
    return "; ".join(lines) if lines else stdout.strip()[:100]


def _build_earlyoom_regex(patterns: list[str]) -> str:
    """Build a regex pattern string for earlyoom --prefer/--avoid.

    earlyoom uses POSIX extended regex matching against ``/proc/pid/comm``.
    Wraps patterns in ``(...)`` so matching is unanchored and can match
    anywhere in the process name.
    """
    return "(%s)" % "|".join(patterns)


@dataclass
class SetupActionContext:
    user: str
    ssh_kwargs: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    sudo_password: str | None = None
    dispatch: Callable | None = None

    def run(self, host: str, script: str, timeout: int = 300):
        if self.dry_run:
            raise RuntimeError("A setup preview cannot execute a host action")
        if self.dispatch:
            return self.dispatch(host, script, self.sudo_password or "", timeout=timeout)
        from sparkrun.orchestration.sudo import run_sudo_script_on_host

        return run_sudo_script_on_host(host, script, self.sudo_password or "", ssh_kwargs=self.ssh_kwargs, timeout=timeout)


@dataclass
class SetupActionResult:
    host: str
    status: str
    detail: str
    changed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def aggregate_action_status(outcomes: dict[str, SetupActionResult]) -> str:
    """Deterministic severity; skipped hosts are neutral beside useful work."""
    statuses = {outcome.status for outcome in outcomes.values()}
    return next((status for status in (FAIL, WARN, OK, SKIP) if status in statuses), SKIP)


def validate_action_result(result: SetupActionResult, host: str) -> SetupActionResult:
    """Shared apply/undo result contract; invalid results never imply success."""
    from sparkrun.core.setup_manifest import validate_setup_details

    if not isinstance(result, SetupActionResult) or result.host != host:
        raise TypeError("Setup action must return SetupActionResult for its target host")
    if result.status not in {OK, WARN, FAIL, SKIP}:
        raise ValueError("Setup action returned invalid status: %r" % result.status)
    if type(result.changed) is not bool or not isinstance(result.detail, str):
        raise TypeError("Setup result requires boolean changed and string detail")
    validate_setup_details(result.extra)
    return result


def docker_group(state, ctx, action):
    from sparkrun.utils.shell import validate_unix_username

    validate_unix_username(action.user)
    if state.facts.get("CHECK_DOCKER_INSTALLED") != "1":
        return SetupActionResult(state.host, SKIP, "Install Docker before configuring access")
    if state.facts.get("CHECK_DOCKER_USABLE") == "1":
        return SetupActionResult(state.host, SKIP, "Docker access already works without a group change")
    if state.facts.get("CHECK_DOCKER_GROUP") == "1":
        return SetupActionResult(state.host, SKIP, "Already in docker group; check daemon availability and re-login")
    result = action.run(state.host, _DOCKER_GROUP_FALLBACK_SCRIPT.format(user=action.user), timeout=30)
    return SetupActionResult(
        state.host,
        OK if result.success else FAIL,
        _docker_group_summary(result.stdout, action.user) if result.success else result.stderr[:300],
        result.success,
    )


def nvidia_cdi(state, ctx, action):
    from sparkrun.core.setup_steps import setup_constraint_reason

    if reason := setup_constraint_reason("nvidia_cdi", state, ctx):
        return SetupActionResult(state.host, SKIP, reason)
    if ctx.gpu_access_modes.get(state.host) != "cdi":
        return SetupActionResult(state.host, SKIP, "CDI generation requires a qualified target using CDI")
    from sparkrun.scripts import read_script

    result = action.run(state.host, read_script("nvidia_cdi_generate_fallback.sh"), timeout=120)
    changed = result.success and "GENERATED:" in result.stdout
    return SetupActionResult(
        state.host,
        OK if changed else (SKIP if result.success else FAIL),
        _cdi_summary(result.stdout) if result.success else result.stderr[:300],
        changed,
    )


def sudoers(state, ctx, action):
    from sparkrun.scripts import read_script
    from sparkrun.utils.shell import validate_unix_username
    from sparkrun.core.application_profile import get_application_profile

    validate_unix_username(action.user)
    namespace = get_application_profile().resource_namespace
    outputs = []
    files = []
    failed = False
    for name, label, args in (
        ("fix_permissions_sudoers.sh", "chown", {"user": action.user, "cache_dir": ""}),
        ("clear_cache_sudoers.sh", "dropcaches", {"user": action.user}),
    ):
        result = action.run(state.host, read_script(name).format(**args))
        outputs.append((result.stdout if result.success else result.stderr).strip()[:300])
        if result.success:
            files.append("/etc/sudoers.d/%s-%s-%s" % (namespace, label, action.user))
        else:
            failed = True
    return SetupActionResult(state.host, FAIL if failed else OK, "; ".join(outputs), bool(files), {"files": files})


def earlyoom(state, ctx, action):
    from sparkrun.scripts import read_script

    from sparkrun.core.setup_steps import setup_constraint_reason

    if reason := setup_constraint_reason("earlyoom", state, ctx):
        return SetupActionResult(state.host, SKIP, reason)
    script = read_script("earlyoom_install_fallback.sh").format(
        prefer=_build_earlyoom_regex(EARLYOOM_PREFER_PATTERNS), avoid=_build_earlyoom_regex(EARLYOOM_AVOID_PATTERNS)
    )
    result = action.run(state.host, script)
    return SetupActionResult(
        state.host,
        OK if result.success else FAIL,
        _earlyoom_summary(result.stdout) if result.success else result.stderr[:300],
        result.success,
        {"installed_package": "INSTALLING:" in result.stdout},
    )
