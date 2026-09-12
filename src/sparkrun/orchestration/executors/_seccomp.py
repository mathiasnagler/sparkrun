"""Docker seccomp policies carried with each generated Bash launch command.

Docker reads a profile on the CLI host and sends its contents to the daemon.
Process substitution supplies that file without requiring a persistent copy or
the controller's filesystem path on every worker. Only policy data is quoted
into the script; none of it is evaluated as shell code.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
import json
from pathlib import Path
from shlex import quote, split

IO_URING_PROFILE = "io-uring"
IO_URING_SYSCALLS = ("io_uring_enter", "io_uring_register", "io_uring_setup")


@lru_cache(maxsize=1)
def io_uring_profile() -> str:
    """Pinned Moby default plus one unconditional io_uring allow rule."""
    source = files("sparkrun.orchestration.executors").joinpath("seccomp/default.json")
    profile = json.loads(source.read_text(encoding="utf-8"))
    profile["syscalls"].append({"names": list(IO_URING_SYSCALLS), "action": "SCMP_ACT_ALLOW"})
    return json.dumps(profile, separators=(",", ":"))


def _reject_constant(value: str):
    raise ValueError("invalid JSON constant %s" % value)


def read_profile(value: str) -> str:
    """Snapshot a controller-local policy, validating its basic Docker shape.

    Docker/libseccomp still validate architecture, syscall, and argument support
    on the target. Custom policies are never amended with our io_uring rule.
    """
    if value == IO_URING_PROFILE:
        return io_uring_profile()
    try:
        content = Path(value).expanduser().read_text(encoding="utf-8")
        profile = json.loads(content, parse_constant=_reject_constant)
        if not isinstance(profile, dict) or not isinstance(profile.get("defaultAction"), str):
            raise ValueError("expected an object with a defaultAction")
        actions = {
            "SCMP_ACT_" + action for action in ("KILL", "KILL_PROCESS", "KILL_THREAD", "TRAP", "ERRNO", "TRACE", "ALLOW", "LOG", "NOTIFY")
        }
        if profile["defaultAction"] not in actions:
            raise ValueError("unknown defaultAction")
        rules = profile.get("syscalls", [])
        if not isinstance(rules, list):
            raise ValueError("syscalls must be an array")
        for rule in rules:
            if (
                not isinstance(rule, dict)
                or not isinstance(rule.get("names"), list)
                or not rule["names"]
                or any(not isinstance(name, str) or not name for name in rule["names"])
                or not isinstance(rule.get("action"), str)
                or rule["action"] not in actions
            ):
                raise ValueError("each syscall rule needs names and a valid action")
        return json.dumps(profile, separators=(",", ":"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Cannot load Docker seccomp profile %r: %s" % (value, exc)) from exc


def profile_value(option: str) -> str | None:
    """Accept Docker's current '=' and legacy ':' security option spelling."""
    if option.startswith(("seccomp=", "seccomp:")):
        value = option[8:]
        if not value:
            raise ValueError("Docker seccomp profile must not be empty")
        return value
    return None


def split_options(security_opt: list[str] | None, extra_opts: list[str] | None) -> tuple[list[str], list[str], str]:
    """Select one policy; raw launch flags override executor configuration.

    Return other configured security options, other raw tokens, and the policy.
    Multiple policies in either input are rejected instead of depending on
    Docker's version-specific handling of duplicate seccomp flags.
    """
    configured = []
    policies = []
    for option in security_opt or ():
        value = profile_value(option)
        if value is None:
            configured.append(option)
        else:
            policies.append(value)
    tokens = [token for option in extra_opts or () for token in split(option)]
    remaining = []
    overrides = []
    cursor = 0
    while cursor < len(tokens):
        token = tokens[cursor]
        width = 1
        option = None
        if token == "--security-opt":
            if cursor + 1 == len(tokens):
                raise ValueError("--security-opt requires a value")
            option = tokens[cursor + 1]
            width = 2
        elif token.startswith("--security-opt="):
            option = token[len("--security-opt=") :]
        value = profile_value(option) if option is not None else None
        if value is None:
            remaining.extend(tokens[cursor : cursor + width])
        else:
            overrides.append(value)
        cursor += width
    if len(policies) > 1 or len(overrides) > 1:
        raise ValueError("Specify only one Docker seccomp profile per configuration or extra-options list")
    return configured, remaining, next(iter(overrides or policies), IO_URING_PROFILE)


def profile_option(value: str, snapshots: dict[str, str]) -> str:
    """Render a single Bash argument, snapshotting each local input only once."""
    if value in {"unconfined", "builtin"}:
        return quote("seccomp=" + value)
    if value not in snapshots:
        snapshots[value] = read_profile(value)
    return f"seccomp=<(printf '%s' {quote(snapshots[value])})"
