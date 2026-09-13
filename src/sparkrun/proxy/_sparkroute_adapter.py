"""Operational-error compatibility for the unmodified SparkRoute 0.1.1 snapshot.

Remove this adapter when a verified upstream snapshot implements the shared
error contract. Keep AdminError identity/metadata so upstream startup still
recognizes reconciliation failures after the process has started.
"""

from sparkrun.plugins.sparkroute.admin import AdminError
from sparkrun.plugins.sparkroute.engine import SparkrouteEngine
from .contracts import GatewayOperationError


class _AdminOperationError(AdminError, GatewayOperationError):
    """A provider transport/refusal error that also follows the host contract."""


class SparkrouteGateway(SparkrouteEngine):
    def reconcile(self, aliases: dict[str, str] | None = None, *, reason: str = "") -> tuple[int, int]:
        try:
            return super().reconcile(aliases, reason=reason)
        except AdminError as exc:
            raise _AdminOperationError(str(exc), status=exc.status, code=exc.code) from exc
