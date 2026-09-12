"""Default :class:`SparkrunContext` factory for the library API.

Each ``api.*`` function accepts an optional ``sctx`` argument; when
``None`` (the common one-shot case), :func:`default_sctx` builds a
fresh session bundling the SAF :class:`Variables` and a
:class:`SparkrunConfig`.  Callers that issue multiple ``api.*`` calls
in sequence can construct an :class:`SparkrunContext` once and pass
it in to share state (config, registry manager, cluster manager).

Default context construction delegates to ``sparkrun.application.initialize``.
Contexts share the process's application/config binding and plugin registry.
Bootstrap failures are translated to SparkrunError; interrupts pass through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sparkrun.core.context import SparkrunContext


def default_sctx() -> "SparkrunContext":
    """Build a fresh :class:`SparkrunContext` for a one-shot API call.

    Initialises the SAF plugin registry (idempotent — uses the module
    singleton if already bootstrapped) and instantiates a
    :class:`SparkrunConfig` for the existing application binding, or the
    selected application's default config path on first initialization.
    """
    from sparkrun.application import initialize
    from sparkrun.api._errors import SparkrunError

    try:
        return initialize()
    except SparkrunError:
        raise
    except Exception as exc:
        raise SparkrunError("Application initialization failed: %s" % exc) from exc


def resolve_sctx(sctx: "SparkrunContext | None") -> "SparkrunContext":
    """Return *sctx* if non-None, else a freshly-built default."""
    return sctx if sctx is not None else default_sctx()


__all__ = ["default_sctx", "resolve_sctx"]
