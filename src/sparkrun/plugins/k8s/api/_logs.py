"""Explicit, console-free log consumption for native Kubernetes resources."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Literal

from sparkrun.api._context import resolve_sctx
from sparkrun.api._errors import SparkrunError
from sparkrun.core.application_profile import resource_name
from sparkrun.core.log_source import LogLine, LogSource, MODE_STDOUT
from sparkrun.orchestration.logs import read_log_command
from ._errors import _operation_errors
from ..orchestration.manifests import require_application_owner
from ..orchestration.names import validate_resource_name

if TYPE_CHECKING:
    from sparkrun.core.context import SparkrunContext


def logs(
    sctx: SparkrunContext | None = None,
    *,
    name: str,
    kind: Literal["jobset", "job"] = "jobset",
    namespace: str | None = None,
    kubeconfig: str | None = None,
    context: str | None = None,
    follow: bool = False,
    tail: int | None = None,
) -> Iterator[LogLine]:
    """Read an owned JobSet or launcher Job independently of submission.

    Target/ownership checks run at call time; reading starts on iteration.
    Both stdout and stderr become structured lines. Close the iterator when
    stopping early (for example, with contextlib.closing). Closing terminates
    only the local log reader and leaves the Kubernetes workload running.
    """
    from ._ops import make_client
    from sparkrun.plugins.k8s.orchestration.launch import JOBSET_NAME_LABEL

    if kind not in ("jobset", "job"):
        raise ValueError("Kubernetes log kind must be 'jobset' or 'job'")
    validate_resource_name(name)
    if tail is not None and (type(tail) is not int or tail < 0):
        raise ValueError("Kubernetes log tail must be a nonnegative integer or None")
    with _operation_errors("Kubernetes log resolution"):
        sctx = resolve_sctx(sctx)
        ns = namespace or resource_name("")
        client = make_client(sctx, kubeconfig=kubeconfig, context=context, namespace=ns)
        resource = client.run_json(["get", kind, name, "-o", "json", "--ignore-not-found"])
        if not resource:
            raise SparkrunError("No Kubernetes %s named %r" % (kind, name))
        require_application_owner(resource, kind=kind, name=name)
        command = client.base_args() + ["logs"]
        if kind == "jobset":
            command += ["-l", "%s=%s" % (JOBSET_NAME_LABEL, name), "--all-containers", "--prefix"]
        else:
            command += ["job/" + name]
        if follow:
            command.append("--follow")
        if tail is not None:
            command.append("--tail=%d" % tail)
    source = LogSource(host=client.label, container=name, role=kind, mode=MODE_STDOUT, path=None)

    def read():
        with _operation_errors("Kubernetes log reading"):
            yield from read_log_command(command, source, check=True)

    return read()
