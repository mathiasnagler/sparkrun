"""Known operational failures retain one API error boundary."""

from unittest.mock import Mock

import pytest

from sparkrun import api
from sparkrun.core.cluster_manager import ClusterError
from sparkrun.core.parallelism import ParallelismConfig
from sparkrun.core.scheduler import SchedulingRequest


def _request():
    return SchedulingRequest(parallelism=ParallelismConfig(), hosts=("localhost",))


def test_schedule_missing_application_module_has_public_error(monkeypatch):
    monkeypatch.setenv("SPARKRUN_APPLICATION_PROFILE", "missing_schedule_test_application:PROFILE")
    with pytest.raises(api.SparkrunError, match="Application initialization failed") as caught:
        api.schedule(_request())
    assert isinstance(caught.value.__cause__, ModuleNotFoundError)


@pytest.mark.parametrize("failure", [RuntimeError("registration failed"), KeyboardInterrupt(), SystemExit(2)])
def test_schedule_bootstrap_errors_and_interrupts(monkeypatch, failure):
    import sparkrun.core.bootstrap as bootstrap

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(bootstrap, "_register_plugins", fail)
    expected = api.SparkrunError if isinstance(failure, Exception) else type(failure)
    with pytest.raises(expected) as caught:
        api.schedule(_request())
    if isinstance(failure, Exception):
        assert caught.value.__cause__ is failure
    else:
        assert caught.value is failure


def test_schedule_contexts_agree_and_explicit_context_does_not_initialize(monkeypatch):
    implicit = api.schedule(_request())
    context = api.default_sctx()
    monkeypatch.setattr("sparkrun.application.initialize", lambda *a, **kw: pytest.fail("explicit context reinitialized"))
    explicit = api.schedule(_request(), sctx=context)
    assert implicit.assignment == explicit.assignment
    assert implicit.scheduler_name == explicit.scheduler_name


@pytest.mark.parametrize("operation", ["status", "plan", "capacity"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("failure", ["deleted", "malformed"])
def test_cluster_lookup_errors_stay_in_api_family(tmp_path, monkeypatch, operation, explicit, failure):
    context = api.default_sctx()
    context.cluster_manager.create("lab", ["10.0.0.1"])
    if failure == "deleted":
        context.cluster_manager.delete("lab")
        with pytest.raises(ClusterError):
            context.cluster_manager.get("lab")
    else:
        context.cluster_manager._cluster_path("lab").write_text("hosts: [")
    remote = Mock(side_effect=AssertionError("failed selection attempted remote work"))
    monkeypatch.setattr("sparkrun.orchestration.executor.query_status_for_cluster", remote)
    monkeypatch.setattr("sparkrun.api._resolve.prepare_transport", remote)
    sctx = context if explicit else None
    calls = {
        "status": lambda: api.status(["10.0.0.1"], cluster="lab", sctx=sctx),
        "plan": lambda: api.plan(api.RunOptions(recipe="not-resolved", cluster="lab"), sctx=sctx),
        "capacity": lambda: api.catalog_cluster_capacity("lab", sctx=sctx),
    }
    with pytest.raises(api.SparkrunError, match="Cannot load cluster 'lab'") as caught:
        calls[operation]()
    from yaml import YAMLError

    assert isinstance(caught.value.__cause__, ClusterError if failure == "deleted" else YAMLError)
    remote.assert_not_called()


def test_cluster_lookup_does_not_wrap_programming_errors(monkeypatch):
    context = api.default_sctx()
    cause = RuntimeError("unexpected manager bug")
    monkeypatch.setattr(context.cluster_manager, "get", Mock(side_effect=cause))
    with pytest.raises(RuntimeError) as caught:
        api.catalog_cluster_capacity("lab", sctx=context)
    assert caught.value is cause
