"""Application-facing extension boundaries, independent of CLI presentation."""

from dataclasses import FrozenInstanceError, replace
from unittest.mock import Mock
from contextlib import nullcontext

import pytest


def test_registration_enlistment_restores_nested_and_late_state():
    from sparkrun.core.registration import enlist_registry_state, registry_transaction

    original = object()
    namespace = {"items": {"nested": [original]}}
    enlist_registry_state(namespace, "items")
    nested = namespace["items"]["nested"]
    late = {"items": {}}
    with pytest.raises(RuntimeError), registry_transaction():
        nested.append("new")
        enlist_registry_state(late, "items")
        late["items"]["new"] = True
        with pytest.raises(ValueError), registry_transaction():
            nested.append("inner")
            raise ValueError("inner failure")
        assert nested == [original, "new"]
        raise RuntimeError("outer failure")
    assert namespace["items"]["nested"] is nested and nested == [original]
    assert late["items"] == {}
    # Replacing a registry container must not leave rollback using a stale alias.
    namespace["items"] = {}
    with pytest.raises(RuntimeError), registry_transaction():
        namespace["items"]["replacement"] = True
        raise RuntimeError("failure")
    assert namespace["items"] == {}


def test_benchmark_hooks_receive_immutable_data_and_private_outcomes(monkeypatch):
    from sparkrun.api import BenchmarkOptions
    from sparkrun.api._benchmark import _NullProgressEmitter
    from sparkrun.api._context import default_sctx
    from sparkrun.benchmarking.base import BenchmarkExecution
    from sparkrun.core.benchmark_integrations import BenchmarkIntegration, BenchmarkIntegrationSession, register_benchmark_integration

    monkeypatch.setattr("sparkrun.core.benchmark_integrations._INTEGRATIONS", {})
    execution = BenchmarkExecution(success=True, results={"rows": [{"speed": 42}]}, benchmark_id="bench_contract")
    snapshots = []

    def complete(context):
        snapshots.append(context.result)
        with pytest.raises(FrozenInstanceError):
            context.result.success = False
        with pytest.raises(TypeError):
            context.result.results["rows"][0]["speed"] = 0
        assert not hasattr(context.result, "launch_result")
        assert not hasattr(context.result, "recipe")
        assert not hasattr(context.result, "integration_results")
        context.outcome["published"] = context.settings["label"]
        context.data["private"] = context.settings["label"]

    for name in ("first", "second"):
        register_benchmark_integration(BenchmarkIntegration(name, on_complete=complete))
    session = BenchmarkIntegrationSession(
        BenchmarkOptions(recipe="r", integrations={name: {"label": name} for name in ("first", "second")}),
        sctx=default_sctx(),
        emitter=_NullProgressEmitter(),
    )
    session.bind(execution)
    session.complete()
    assert execution.results == {"rows": [{"speed": 42}]}
    assert execution.integration_results == {"first": {"published": "first"}, "second": {"published": "second"}}
    execution.results["rows"][0]["speed"] = 10
    assert snapshots[0].results["rows"][0]["speed"] == 42
    assert session.contexts["first"].data == {"private": "first"}
    assert session.contexts["second"].data == {"private": "second"}


@pytest.mark.parametrize("dry_run,approve", [(False, True), (False, False), (True, True)])
def test_headless_setup_records_changes_and_reprobes_dependencies(monkeypatch, capsys, dry_run, approve):
    from sparkrun.api.setup import run_setup_steps
    from sparkrun.core.features import FeatureFlag, register_feature
    from sparkrun.core.setup_actions import SetupActionContext, SetupActionResult
    from sparkrun.core.setup_models import CheckItem, OK, FAIL
    from sparkrun.core.setup_steps import SetupStep, register_setup_step
    from test_setup_steps import state_context, approve_test_steps

    state, context = state_context()
    log = []
    complete = set()
    for name, requires in (("first", ()), ("second", ("first",))):
        flag = "setup.steps." + name
        register_feature(FeatureFlag(flag, name, default=True))

        def check(state, ctx, name=name):
            return CheckItem(name, name, OK if state.facts.get(name) == "1" else FAIL)

        def action(state, ctx, action_context, name=name):
            assert action_context.sudo_password == "ephemeral"
            if name == "second":
                assert state.facts["first"] == "1", "Dependents require fresh observed facts"
            complete.add(name)
            log.append(name)
            return SetupActionResult(state.host, OK, "done", changed=True, extra={"created": name})

        register_setup_step(SetupStep(name, name, checks=(check,), apply=action, requires=requires, feature_flag=flag))

    approve_test_steps(monkeypatch, "first", "second")

    def reprobe(*args, **kwargs):
        log.append("probe")
        return {state.host: replace(state, facts={**state.facts, **{name: "1" for name in complete}})}, context

    probe = Mock(side_effect=reprobe)
    monkeypatch.setattr("sparkrun.core.setup_probe.probe_setup_hosts", probe)
    manifests, credentials, confirmation = Mock(), Mock(return_value="ephemeral"), Mock(return_value=approve)
    manifests.recording.return_value = nullcontext()
    events = []
    result = run_setup_steps(
        {state.host: state},
        context,
        SetupActionContext("tester", dry_run=dry_run),
        cluster_name="lab",
        manifest_mgr=manifests,
        credentials=credentials,
        approve=confirmation,
        progress_callback=events.append,
        only_steps={"first", "second"},
    )
    assert capsys.readouterr().out == ""
    if dry_run or not approve:
        probe.assert_not_called()
        credentials.assert_not_called()
        manifests.record_phase.assert_not_called()
        if dry_run:
            confirmation.assert_not_called()
    else:
        assert log == ["first", "probe", "second", "probe"]
        assert result.steps == {"first": OK, "second": OK}
        assert manifests.record_phase.call_count == 2
        assert manifests.record_phase.call_args.kwargs["host_details"] == {state.host: {"created": "second"}}
        assert all("ephemeral" not in repr(event) for event in events)


def test_setup_records_partial_changes_before_progress_failure(monkeypatch):
    from sparkrun.api.setup import run_setup_steps
    from sparkrun.core.features import FeatureFlag, register_feature
    from sparkrun.core.setup_actions import SetupActionContext, SetupActionResult
    from sparkrun.core.setup_models import CheckItem, FAIL
    from sparkrun.core.setup_steps import SetupStep, register_setup_step
    from test_setup_steps import state_context, approve_test_steps

    state, context = state_context()
    register_feature(FeatureFlag("setup.steps.partial", "partial", default=True))
    register_setup_step(
        SetupStep(
            "partial",
            "partial",
            feature_flag="setup.steps.partial",
            requires_sudo=False,
            checks=(lambda *_: CheckItem("partial", "partial", FAIL),),
            apply=lambda s, *_: SetupActionResult(s.host, FAIL, "partially applied", changed=True, extra={"created": "owned"}),
        )
    )
    manifests = Mock()
    manifests.recording.return_value = nullcontext()

    approve_test_steps(monkeypatch, "partial")

    def progress(event):
        if event.kind == "result":
            raise RuntimeError("frontend failed")

    from sparkrun.api.setup import SetupFailed

    with pytest.raises(SetupFailed, match="frontend failed"):
        run_setup_steps(
            {state.host: state},
            context,
            SetupActionContext("tester"),
            cluster_name="lab",
            manifest_mgr=manifests,
            only_steps={"partial"},
            progress_callback=progress,
        )
    manifests.record_phase.assert_called_once()
