"""Small shared helpers for plugin registration conflicts and rollback state.

Registries enlist their module namespace and container names beside their
storage declarations. Reading through the namespace honors deliberate container
replacement (including test isolation). Registration runs before application
workers; this is not a concurrent registry mutation API.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import ModuleType
from typing import Any, TypeVar, TYPE_CHECKING

if TYPE_CHECKING:
    from scitrera_app_framework import Variables
    from sparkrun.core.registry_defaults import DeclarationTier

# Module entry-point compatibility, independent of application profile schemas.
PLUGIN_API_VERSION = 1

K = TypeVar("K")
V = TypeVar("V")
_SOURCES: list[tuple[dict[str, Any], str]] = []
_ACTIVE: ContextVar[tuple[_Snapshot, ...]] = ContextVar("sparkrun_registration_snapshots", default=())


def register_unique(registry: MutableMapping[K, V], key: K, value: V, *, description: str) -> None:
    """Allow identical registration; reject a second provider for the same key."""
    if key in registry and registry[key] != value:
        from sparkrun.core.installed_plugins import PluginConflictError

        raise PluginConflictError("%s %r is already registered" % (description, key))
    registry[key] = value


def enlist_registry_state(namespace: dict[str, Any], *names: str) -> None:
    """Enlist mutable registry containers, including imports during a transaction."""
    for name in names:
        value = namespace[name]
        if not isinstance(value, (dict, list, set)):
            raise TypeError("Registration state must be a dict, list, or set")
        if not any(source is namespace and key == name for source, key in _SOURCES):
            _SOURCES.append((namespace, name))
        for snapshot in _ACTIVE.get():
            snapshot.capture(value)


class _Snapshot:
    def __init__(self):
        self.saved: list[tuple[Any, Any]] = []
        self.seen: set[int] = set()

    def capture(self, value):
        # SAF's pinned adapter is intentionally confined here. Preserve live
        # Variables/plugin identities while restoring their nested containers.
        from scitrera_app_framework import Variables

        if id(value) in self.seen:
            return
        self.seen.add(id(value))
        if isinstance(value, Variables):
            for state in (value._local, value._fallback_defaults, value._type_fns, value._keys):
                self.capture(state)
        elif isinstance(value, dict):
            self.saved.append((value, dict(value)))
            for child in value.values():
                self.capture(child)
        elif isinstance(value, (list, set)):
            self.saved.append((value, value.copy()))
            for child in value:
                self.capture(child)

    def restore(self):
        for value, previous in reversed(self.saved):
            value.clear()
            if isinstance(value, list):
                value.extend(previous)
            else:
                value.update(previous)


@contextmanager
def registry_transaction(*extra_state):
    """Restore enlisted containers on failure; arbitrary plugin I/O is excluded."""
    snapshot = _Snapshot()
    for namespace, name in _SOURCES:
        snapshot.capture(namespace[name])
    for value in extra_state:
        snapshot.capture(value)
    token = _ACTIVE.set((*_ACTIVE.get(), snapshot))
    try:
        yield
    except BaseException:
        snapshot.restore()
        raise
    finally:
        _ACTIVE.reset(token)


def load_and_register_plugin(
    loader: Callable[[], ModuleType],
    v: Variables | None,
    *,
    tier: DeclarationTier | None = None,
    require_api_version: bool = False,
) -> ModuleType:
    """Import, validate and register one module in a single transaction.

    Discovery, trust tier and optional failure reporting belong to the caller.
    Installed entry points require a version declaration. Legacy directory and
    bundled modules may omit it, but any declared version must be compatible.
    Exceptions propagate after rollback; arbitrary plugin I/O is not undone.
    """
    with registry_transaction(v):
        from sparkrun.core.external_plugins import _register_plugin_module

        module = loader()
        if not isinstance(module, ModuleType):
            raise TypeError("Plugin loaders must return a module")
        if require_api_version or hasattr(module, "SPARKRUN_PLUGIN_API_VERSION"):
            api_version = getattr(module, "SPARKRUN_PLUGIN_API_VERSION", None)
            if type(api_version) is not int or api_version != PLUGIN_API_VERSION:
                raise ValueError("Plugin API %r is incompatible with supported API %s" % (api_version, PLUGIN_API_VERSION))
        _register_plugin_module(module, v, tier=tier)
    return module
