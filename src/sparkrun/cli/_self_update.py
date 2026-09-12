"""Update-channel self-update orchestration for the sparkrun CLI.

Wraps uv-tool install/upgrade behavior for the stable/beta/alpha channels and
provides identity-based update detection: PyPI stable compares the package
version, while git channels compare the resolved commit (their `pyproject`
version is static, so a version-string compare would never detect a change).

The uv argv here is validated by the Phase 0 spike: `uv tool install
"<git req>" --force` re-resolves a mutable branch to its newest commit and
rebuilds even when the version is unchanged.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import re
import sys
from pathlib import Path

from sparkrun.core.application_profile import get_application_profile

import click

from sparkrun.core.channels import (
    CHANNEL_STABLE,
    channel_requirement,
    is_git_channel,
    normalize_channel,
)
from sparkrun.core.version import installed_identity


def resolve_uv() -> str | None:
    """Return the uv executable path, or None if uv is not installed."""
    return shutil.which("uv")


def owning_executable() -> str:
    """The executable inside the running tool environment, independent of PATH."""
    return str(Path(sys.prefix) / "bin" / get_application_profile().command)


def installed_tool_executable(uv: str) -> Path | None:
    """Resolve the owning installed tool, including after an install from uvx."""
    try:
        check = subprocess.run([uv, "tool", "list", "--show-paths"], capture_output=True, text=True)
    except OSError:
        return None
    if check.returncode:
        return None
    expected = re.sub(r"[-_.]+", "-", get_application_profile().package).lower()
    for line in check.stdout.splitlines():
        match = re.fullmatch(r"([^ ]+) v[^ ]+ \((.+)\)", line.strip())
        if match and re.sub(r"[-_.]+", "-", match[1]).lower() == expected:
            return Path(match[2]).resolve() / "bin" / get_application_profile().command
    return None


def is_uv_tool_install(uv: str) -> bool:
    """Require an exact owning package AND the current interpreter environment."""
    executable = installed_tool_executable(uv)
    return executable is not None and executable.parent.parent == Path(sys.prefix).resolve()


def channel_from_flags(stable: bool, beta: bool, alpha: bool, yolo: bool) -> str | None:
    """Return the requested canonical channel from mutually-exclusive flags.

    Returns None when no flag is set. Raises ``click.ClickException`` if flags
    select conflicting channels (``--yolo`` and ``--alpha`` do not conflict —
    both normalize to ``alpha``).
    """
    requested = {
        normalize_channel(chan)
        for chan, enabled in (
            (CHANNEL_STABLE, stable),
            ("beta", beta),
            ("alpha", alpha),
            ("yolo", yolo),
        )
        if enabled
    }
    if not requested:
        return None
    if len(requested) > 1:
        raise click.ClickException("Choose only one of --stable, --beta, --alpha, --yolo.")
    return requested.pop()


def install_argv(uv: str, channel: str) -> list[str]:
    """Build the uv argv to install or switch to a channel."""
    return [uv, "tool", "install", channel_requirement(channel), "--force"]


def update_argv(uv: str, channel: str) -> list[str]:
    """Build the uv argv to update within a channel.

    Stable uses ``uv tool upgrade``; git channels reinstall with ``--force`` so
    the mutable branch re-resolves to its newest commit.
    """
    if not is_git_channel(channel) and channel_requirement(channel) == get_application_profile().package:
        return [uv, "tool", "upgrade", get_application_profile().package]
    return install_argv(uv, channel)


def new_binary_identity() -> tuple[str | None, str | None]:
    """Query the freshly installed binary for ``(version, commit)`` via JSON.

    After a uv reinstall the running process holds stale code, so we shell out
    to the newly installed on-PATH binary's hidden ``setup version --json``.
    """
    try:
        res = subprocess.run(
            [owning_executable(), "setup", "version", "--json"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None, None
    if res.returncode != 0:
        return None, None
    try:
        data = json.loads(res.stdout.strip() or "{}")
    except ValueError:
        return None, None
    identity = data.get("distribution")
    if identity is not None and identity.get("id") != get_application_profile().id:
        return None, None
    return data.get("version"), data.get("commit")


def identity_changed(
    channel: str,
    old: tuple[str | None, str | None],
    new: tuple[str | None, str | None],
) -> bool:
    """Return whether the install actually changed, by the same rule as `describe_change`.

    A zero exit from uv means the command worked, not that anything was installed:
    ``uv tool upgrade`` prints "Nothing to upgrade" and exits 0, and git channels
    reinstall with ``--force`` on every run regardless of whether the branch moved.
    So the identity is the only evidence an upgrade happened.

    An indeterminate new identity (the ``setup version --json`` probe failed) returns
    False: callers use this to claim an upgrade in telemetry and to decide whether the
    running process holds stale code, and both are better off assuming nothing changed
    than reporting an upgrade that may not have occurred.
    """
    old_version, old_commit = old
    new_version, new_commit = new
    if is_git_channel(channel):
        if new_commit is None:
            return False
        return old_commit != new_commit
    if new_version is None:
        return False
    return old_version != new_version


def describe_change(channel: str, old: tuple[str | None, str | None], new: tuple[str | None, str | None]) -> str:
    """Return a human message describing the version/commit change after update."""
    old_version, old_commit = old
    new_version, new_commit = new
    if is_git_channel(channel):
        if new_commit is None:
            return f"{get_application_profile().command} %s updated (could not determine new commit)." % channel
        if old_commit == new_commit:
            return f"{get_application_profile().command} %s is already on the latest commit (%s)." % (channel, new_commit[:7])
        old_disp = old_commit[:7] if old_commit else "unknown"
        return f"{get_application_profile().command} %s updated: commit %s -> %s" % (channel, old_disp, new_commit[:7])
    # stable / PyPI
    if new_version is None:
        return f"{get_application_profile().command} updated (could not determine new version)."
    if old_version == new_version:
        return f"{get_application_profile().command} %s is already the latest version." % new_version
    return f"{get_application_profile().command} updated: %s -> %s" % (old_version, new_version)


def warn_if_downgrade(current_channel: str, requested_channel: str) -> None:
    """Warn when switching from a git channel to stable, which may downgrade."""
    if requested_channel == CHANNEL_STABLE and is_git_channel(current_channel):
        click.echo(
            "Note: switching to the stable channel may downgrade from a preview build (stable tracks the latest PyPI release).",
            err=True,
        )


def capture_old_identity() -> tuple[str, str | None]:
    """Return the currently-running build's ``(version, commit)`` identity."""
    return installed_identity()
