"""Completed measurements survive reporting and persistence failures."""

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from sparkrun.api import benchmark, resume_benchmark, BenchmarkFailed, BenchmarkIntegrationFailed
from sparkrun.core.benchmark_integrations import BenchmarkIntegration, register_benchmark_integration
from test_benchmark_startup_collection import bench_env as bench_env
from test_benchmark_api_contract import scheduled_env as scheduled_env


@pytest.fixture(autouse=True)
def integrations(monkeypatch):
    monkeypatch.setattr("sparkrun.core.benchmark_integrations._INTEGRATIONS", {})


def test_result_values_are_normalized_with_or_without_integrations(bench_env):
    env = bench_env
    values = {
        "rate": 12,
        "observed_at": datetime(2026, 9, 11, tzinfo=timezone.utc),
        "date": date(2026, 9, 11),
        "file": Path("result.json"),
        "samples": (1, 2),
    }
    normalized = {"rate": 12, "observed_at": "2026-09-11T00:00:00+00:00", "date": "2026-09-11", "file": "result.json", "samples": [1, 2]}
    env.fw.parse_results.side_effect = lambda *a, **kw: values
    seen = []
    register_benchmark_integration(BenchmarkIntegration("review", on_complete=lambda ctx: seen.append(ctx.result)))
    for selected in ({}, {"review": {}}):
        result = benchmark(replace(env.options, integrations=selected, export_files=False), sctx=env.sctx)
        assert result.success and result.results == normalized
    assert seen[0].results["observed_at"] == normalized["observed_at"]
    assert seen[0].results["samples"] == (1, 2)
    assert isinstance(values["observed_at"], datetime)


@pytest.mark.parametrize("selected", [{}, {"review": {}}])
def test_invalid_framework_values_fail_consistently_before_completion(bench_env, selected):
    env = bench_env
    env.fw.parse_results.side_effect = lambda *a, **kw: {"invalid": object()}
    complete = Mock()
    register_benchmark_integration(BenchmarkIntegration("review", on_complete=complete))
    with pytest.raises(BenchmarkFailed, match="Invalid results.*results.invalid"):
        benchmark(replace(env.options, integrations=selected, export_files=False), sctx=env.sctx)
    complete.assert_not_called()


def test_snapshot_failure_carries_completed_measurements(bench_env, monkeypatch):
    from sparkrun.benchmarking.base import BenchmarkExecution

    env = bench_env
    snapshot = Mock(side_effect=ValueError("provenance unavailable"))
    monkeypatch.setattr(BenchmarkExecution, "generate_metadata", snapshot)
    complete = Mock()
    register_benchmark_integration(BenchmarkIntegration("review", on_complete=complete))
    with pytest.raises(BenchmarkIntegrationFailed, match="provenance unavailable") as caught:
        benchmark(replace(env.options, integrations={"review": {}}, export_files=False), sctx=env.sctx)
    assert caught.value.integration == "review"
    assert caught.value.result.success and caught.value.result.results == env.rows
    assert caught.value.result.integration_errors == {"review": "provenance unavailable"}
    env.fw.parse_results.assert_called_once()
    snapshot.assert_called_once()  # Absent bind/checkpoint hooks need no snapshot.
    complete.assert_not_called()


def test_snapshots_do_not_resolve_builder_images(bench_env):
    env = bench_env
    env.launch.builder = Mock()
    env.launch.runtime_info["observed_at"] = datetime(2026, 9, 11, tzinfo=timezone.utc)
    seen = []
    register_benchmark_integration(BenchmarkIntegration("review", on_complete=lambda ctx: seen.append(ctx.result)))
    assert benchmark(replace(env.options, integrations={"review": {}}, export_files=False), sctx=env.sctx).success
    env.launch.builder.resolve_long_term_image.assert_not_called()
    assert seen[0].provenance["recipe"]["container"] == env.recipe.container
    assert seen[0].provenance["runtime_info"]["observed_at"] == "2026-09-11T00:00:00+00:00"


def test_outcome_copy_failure_preserves_measurements(bench_env):
    def complete(ctx):
        ctx.outcome["invalid"] = object()

    register_benchmark_integration(BenchmarkIntegration("review", on_complete=complete))
    with pytest.raises(BenchmarkIntegrationFailed, match="review.outcome.invalid") as caught:
        benchmark(replace(bench_env.options, integrations={"review": {}}, export_files=False), sctx=bench_env.sctx)
    assert caught.value.result.success and caught.value.result.results == bench_env.rows
    assert caught.value.result.integration_results == {}


@pytest.mark.parametrize("hook_fails", [False, True])
def test_resume_save_failure_preserves_primary_error_and_results(monkeypatch, hook_fails):
    from sparkrun.api._context import default_sctx
    from sparkrun.benchmarking.run_state import BenchmarkRunState
    from test_distribution_api_contracts import _completed_state

    sctx = default_sctx()
    state = _completed_state(sctx, {})
    original_save = BenchmarkRunState.save
    failing = False

    def save(self, *a, **kw):
        if failing:
            raise OSError("disk full")
        return original_save(self, *a, **kw)

    monkeypatch.setattr(BenchmarkRunState, "save", save)

    def complete(ctx):
        nonlocal failing
        failing = True
        if hook_fails:
            raise RuntimeError("publication rejected")

    register_benchmark_integration(BenchmarkIntegration("review", on_complete=complete))
    with pytest.raises(BenchmarkIntegrationFailed) as caught:
        resume_benchmark(state.benchmark_id, sctx=sctx)
    assert caught.value.result.success and caught.value.result.results == {"requests_per_second": 12}
    assert caught.value.result.integration_errors["<state>"] == "disk full"
    assert caught.value.integration == ("review" if hook_fails else "<state>")
    if hook_fails:
        assert caught.value.result.integration_errors["review"] == "publication rejected"
        assert "publication rejected" in str(caught.value)


def test_completed_result_persistence_failure_carries_measurements(scheduled_env, monkeypatch):
    monkeypatch.setattr("sparkrun.api._benchmark._save_completed_results", Mock(side_effect=OSError("disk full")))
    with pytest.raises(BenchmarkIntegrationFailed, match="disk full") as caught:
        benchmark(replace(scheduled_env.options, export_files=False), sctx=scheduled_env.sctx)
    assert caught.value.integration == "<state>"
    assert caught.value.result.success and caught.value.result.results == {"rows": [scheduled_env.rows]}


def test_interrupt_is_not_masked_by_save_failure(monkeypatch):
    from sparkrun.api._context import default_sctx
    from sparkrun.core.benchmark_integrations import BenchmarkIntegrationSession
    from test_distribution_api_contracts import _completed_state

    sctx = default_sctx()
    state = _completed_state(sctx, {})

    def complete(ctx):
        raise KeyboardInterrupt()

    register_benchmark_integration(BenchmarkIntegration("review", on_complete=complete))
    original = BenchmarkIntegrationSession.save

    def save(self):
        if self.contexts["review"].result is not None:
            raise OSError("disk full")
        original(self)

    monkeypatch.setattr(BenchmarkIntegrationSession, "save", save)
    with pytest.raises(KeyboardInterrupt):
        resume_benchmark(state.benchmark_id, sctx=sctx)


def test_prepare_receives_only_immutable_measurement_defaults(bench_env):
    from sparkrun.core.benchmark_integrations import BenchmarkDefaults, BenchmarkIntegrationSession
    from sparkrun.api._benchmark import _NullProgressEmitter

    options = replace(
        bench_env.options, bench_args={"nested": [1]}, dry_run=True, integrations={"review": {}}, decision_callback=lambda _: False
    )

    def prepare(defaults, ctx):
        assert isinstance(defaults, BenchmarkDefaults)
        for field in ("dry_run", "integrations", "hosts", "recipe", "progress_callback", "decision_callback"):
            assert not hasattr(defaults, field)
        with pytest.raises(TypeError):
            defaults.bench_args["nested"][0] = 99
        return replace(defaults, category="performance", profile="selected", bench_args={**defaults.bench_args, "extra": True})

    register_benchmark_integration(BenchmarkIntegration("review", prepare=prepare))
    session = BenchmarkIntegrationSession(options, sctx=bench_env.sctx, emitter=_NullProgressEmitter())
    prepared = session.prepare()
    assert prepared.category == "performance" and prepared.profile == "selected"
    assert prepared.bench_args == {"nested": [1], "extra": True}
    assert prepared.dry_run is True and prepared.integrations == options.integrations
    assert prepared.decision_callback is options.decision_callback and prepared.hosts == options.hosts
    assert options.bench_args == {"nested": [1]}


def test_prepare_rejects_returning_full_options_before_launch(bench_env):
    register_benchmark_integration(BenchmarkIntegration("review", prepare=lambda *_: replace(bench_env.options, dry_run=True)))
    with pytest.raises(BenchmarkFailed, match="prepare must return BenchmarkDefaults"):
        benchmark(replace(bench_env.options, integrations={"review": {}}), sctx=bench_env.sctx)
    bench_env.run.assert_not_called()
