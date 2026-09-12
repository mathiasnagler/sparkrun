"""Shared setup script builders; actions live in core.setup_actions."""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Docker group membership scripts (inline — too short for separate .sh files)
# ---------------------------------------------------------------------------

from sparkrun.core.setup_actions import (  # noqa: F401
    _DOCKER_GROUP_SCRIPT,
    _DOCKER_GROUP_FALLBACK_SCRIPT,
    _cdi_summary,
    _docker_group_summary,
    _earlyoom_summary,
    _build_earlyoom_regex,
    EARLYOOM_PREFER_PATTERNS,
    EARLYOOM_AVOID_PATTERNS,
)
