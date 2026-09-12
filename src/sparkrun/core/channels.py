"""Update-channel definitions — the single source of truth shared by config,
version display, and the CLI self-update flow.

This module is intentionally dependency-free and MUST stay byte-identical across
release branches (`main`/stable, `develop`/beta, `develop-next`/alpha) so a given
channel always resolves to the same source regardless of which build is running.
"""

from __future__ import annotations

CHANNEL_STABLE = "stable"
CHANNEL_BETA = "beta"
CHANNEL_ALPHA = "alpha"

CHANNELS = (CHANNEL_STABLE, CHANNEL_BETA, CHANNEL_ALPHA)

# CLI aliases that normalize onto a canonical channel.
_CHANNEL_ALIASES = {"yolo": CHANNEL_ALPHA}

# Canonical uv requirement per channel. Stable installs from PyPI; beta/alpha
# install from mutable git branches (re-resolved to the latest commit on update).
_REPO_URL = "git+https://github.com/spark-arena/sparkrun"
CHANNEL_REQUIREMENTS = {
    CHANNEL_STABLE: "sparkrun",
    CHANNEL_BETA: "sparkrun @ %s@develop" % _REPO_URL,
    CHANNEL_ALPHA: "sparkrun @ %s@develop-next" % _REPO_URL,
}

# Human-facing version suffix per channel (stable has none).
_CHANNEL_SUFFIXES = {
    CHANNEL_STABLE: "",
    CHANNEL_BETA: "-beta",
    CHANNEL_ALPHA: "-alpha",
}


def normalize_channel(value: str | None) -> str:
    """Return the canonical channel for a raw value.

    Applies aliases (``yolo`` -> ``alpha``) and falls back to ``stable`` for
    missing or unrecognized values, so callers never act on an unknown channel.
    """
    from sparkrun.core.application_profile import get_application_profile

    profile = get_application_profile()
    if profile.id != "sparkrun":
        text = str(value or profile.default_channel).strip().lower()
        if not profile.update_sources and text == profile.default_channel:
            return text
        if text not in profile.update_sources:
            raise ValueError(
                "Distribution %r does not support update channel %r; supported: %s"
                % (
                    profile.id,
                    text,
                    ", ".join(profile.update_sources) or "none",
                )
            )
        return text
    if not value:
        return CHANNEL_STABLE
    text = str(value).strip().lower()
    text = _CHANNEL_ALIASES.get(text, text)
    return text if text in CHANNELS else CHANNEL_STABLE


def channel_requirement(channel: str) -> str:
    """Return the uv requirement string for a channel."""
    from sparkrun.core.application_profile import get_application_profile

    canonical = normalize_channel(channel)
    source = get_application_profile().update_sources.get(canonical)
    if source is None:
        raise ValueError("Distribution %r has no configured update source" % get_application_profile().id)
    return source.requirement


def is_git_channel(channel: str) -> bool:
    """Return whether a channel installs from a mutable git branch."""
    from sparkrun.core.application_profile import get_application_profile

    source = get_application_profile().update_sources.get(normalize_channel(channel))
    return source is not None and source.strategy == "git"


def channel_suffix(channel: str) -> str:
    """Return the human-facing version suffix for a channel (``""`` for stable)."""
    canonical = normalize_channel(channel)
    return _CHANNEL_SUFFIXES.get(canonical, "-" + canonical)


def normalize_feature_channel(value: str | None) -> str:
    """Core feature maturity is independent of a distribution's release channels."""
    text = str(value or CHANNEL_STABLE).strip().lower()
    text = _CHANNEL_ALIASES.get(text, text)
    return text if text in CHANNELS else CHANNEL_STABLE
