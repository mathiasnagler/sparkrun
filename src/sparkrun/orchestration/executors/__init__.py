"""Executor plugins — discovered via SAF ``find_types_in_modules``.

Concrete executor implementations live in sibling modules:

- :mod:`.docker` — :class:`DockerExecutor` (default).
- :mod:`.local` — :class:`LocalExecutor` (experimental, no container).
Optional executors are registered by their owning integrations, including
:mod:`sparkrun.plugins.k8s`.

The common ABC + config + extension-point constant live in
:mod:`._base` and are re-exported by
:mod:`sparkrun.orchestration.executor` as the public surface.
"""
