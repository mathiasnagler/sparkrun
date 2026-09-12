"""Explicit, metadata-first discovery of installed cross-cutting integrations."""

from __future__ import annotations

from sparkrun.core.registration import enlist_registry_state, load_and_register_plugin

import logging
import os
from dataclasses import dataclass
from importlib.metadata import entry_points

from sparkrun.core.application_profile import get_application_profile

logger = logging.getLogger(__name__)
ENTRY_POINT_GROUP = "sparkrun.plugins"


class PluginConflictError(RuntimeError):
    """Two providers claim one stable ID; import order cannot resolve it."""

    def __init__(self, message):
        super().__init__(message)
        _conflicts.append(message)


class RequiredIntegrationError(RuntimeError):
    """A launch would silently lose a required integration."""


@dataclass
class InstalledIntegration:
    name: str
    module: str
    package: str | None
    version: str | None
    selected: bool
    selection_source: str
    required: bool = False
    loaded: bool = False
    failure: str | None = None
    entry_point: object = None


_inventory: list[InstalledIntegration] = []
_claims: dict[tuple[int, str, str], type] = {}

enlist_registry_state(globals(), "_claims")
_attempted: set[int] = set()
_conflicts: list[str] = []


def reset_installed_plugins() -> None:
    _inventory.clear()
    _claims.clear()
    _attempted.clear()
    _conflicts.clear()


def claim_implementation(cls: type, v) -> None:
    """Check concrete selectors, including aliases, independent of gate state."""
    from sparkrun.orchestration.telemetry._base import TelemetryProvider

    selectors = ("runtime_name", "builder_name", "executor_name", "scheduler_name", "transport_name", "framework_name")
    if issubclass(cls, TelemetryProvider):
        selectors += ("scope",)
    for attr in selectors:
        name = getattr(cls, attr, None)
        if not name:
            continue
        names = (name, *getattr(cls, attr.removesuffix("_name") + "_aliases", ()))
        for selector in names:
            key = (id(v), attr, selector)
            owner = _claims.get(key)
            if owner is not None and owner is not cls:
                raise PluginConflictError(
                    "Conflicting %s %r: %s.%s and %s.%s"
                    % (
                        attr,
                        selector,
                        owner.__module__,
                        owner.__qualname__,
                        cls.__module__,
                        cls.__qualname__,
                    )
                )
            _claims[key] = cls


def discover_installed_plugins(config=None) -> list[InstalledIntegration]:
    """Enumerate metadata only. Disabled entry points are never loaded."""
    from sparkrun.core.config import SparkrunConfig

    config = config if config is not None else SparkrunConfig()
    profile = get_application_profile()
    overrides = config.get("integrations", {})
    if not isinstance(overrides, dict) or any(type(value) is not bool for value in overrides.values()):
        raise ValueError("integrations must map stable integration IDs to true or false")
    selected = set(profile.integrations) | set(profile.required_integrations)
    selected.update(name for name, enabled in overrides.items() if enabled)
    selected.difference_update(name for name, enabled in overrides.items() if not enabled)
    # Hard test containment is intentionally shared across distributions.
    disabled = os.environ.get("SPARKRUN_NO_INSTALLED_PLUGINS", "").lower() in {"1", "true", "yes"}
    out = []
    found = set()
    for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: (e.name, e.value, getattr(e.dist, "name", "") or "")):
        package = ep.dist.name if ep.dist else None
        version = ep.dist.version if ep.dist else None
        chosen = ep.name in selected and not disabled
        source = "config" if ep.name in overrides else "distribution" if ep.name in selected else "unset"
        row = InstalledIntegration(
            ep.name, ep.value, package, version, chosen, source, ep.name in profile.required_integrations, entry_point=ep
        )
        previous = next((p for p in out if p.name == ep.name), None)
        if previous is not None:
            reason = "Integration %r is provided by both %s (%s) and %s (%s)" % (
                ep.name,
                previous.package,
                previous.module,
                package,
                ep.value,
            )
            previous.failure = row.failure = reason
        found.add(ep.name)
        out.append(row)
    for name in sorted((selected | set(profile.required_integrations)) - found):
        out.append(
            InstalledIntegration(
                name,
                "",
                None,
                None,
                name in selected and not disabled,
                "config" if name in overrides else "distribution",
                name in profile.required_integrations,
                failure="Integration %r is not installed" % name,
            )
        )
    for row in out:
        if row.required and not row.selected:
            row.failure = "Required integration %r is disabled" % row.name
    return out


def installed_plugin_inventory(config=None) -> list[InstalledIntegration]:
    return list(_inventory) if _inventory else discover_installed_plugins(config)


def load_installed_plugins(v, *, config=None) -> None:
    if id(v) in _attempted:
        return
    _inventory[:] = discover_installed_plugins(config)
    _attempted.add(id(v))

    for row in _inventory:
        if row.failure or not row.selected:
            continue
        try:
            load_and_register_plugin(row.entry_point.load, v, require_api_version=True)
            row.loaded = True
        except Exception as exc:
            row.failure = "%s: %s" % (type(exc).__name__, exc)
            logger.warning("Integration %s from %s failed: %s", row.name, row.package, row.failure)


def require_integrations() -> None:
    """Fail dependent launches while keeping help/version/inventory usable."""
    failures = [
        "%s (%s): %s" % (p.name, p.package or "missing", p.failure or "not loaded")
        for p in installed_plugin_inventory()
        if p.required and not p.loaded
    ]
    # Ambiguous names cannot be arbitrated even when the integration is optional.
    failures.extend(
        p.failure for p in _inventory if p.selected and p.failure and ("Conflicting" in p.failure or "provided by both" in p.failure)
    )
    failures.extend(_conflicts)
    if failures:
        raise RequiredIntegrationError("Integration requirements are not satisfied: " + "; ".join(failures))
