"""Post-measurement failures preserve results, cleanup and measurement identity."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
import json

import pytest

from sparkrun.api import benchmark, resume_benchmark, BenchmarkFinalizationFailed, BenchmarkIntegrationFailed
from sparkrun.benchmarking.run_state import BenchmarkRunState
from sparkrun.core.benchmark_integrations import BenchmarkIntegration, register_benchmark_integration, STATE_KEY
from test_benchmark_startup_collection import bench_env as bench_env
from test_benchmark_api_contract import scheduled_env as scheduled_env


@pytest.fixture(autouse=True)
def integration_registry(monkeypatch):
    monkeypatch.setattr("sparkrun.core.benchmark_integrations._INTEGRATIONS", {})


@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("lifecycle", ["normal", "no_stop", "skip_run"])
def test_export_failure_preserves_results_and_honors_cleanup(request, tmp_path, scheduled, lifecycle):
    env = request.getfixturevalue("scheduled_env" if scheduled else "bench_env")
    publish = Mock()
    register_benchmark_integration(BenchmarkIntegration("review", on_complete=publish))
    with pytest.raises(BenchmarkFinalizationFailed) as caught:
        benchmark(
            replace(
                env.options,
                output_file=str(tmp_path),
                integrations={"review": {}},
                no_stop=lifecycle == "no_stop",
                skip_run=lifecycle == "skip_run",
            ),
            sctx=env.sctx,
        )
    error = caught.value
    assert error.stage == "export" and isinstance(error.__cause__, IsADirectoryError)
    assert error.result.success and error.result.results == ({"rows": [env.rows]} if scheduled else env.rows)
    assert env.stop.call_count == int(lifecycle == "normal")
    publish.assert_not_called()
    if scheduled:
        state = BenchmarkRunState.load(error.result.benchmark_id, str(env.sctx.config.cache_dir))
        assert state.extras["measurement_complete"]
        assert (state.state_dir(str(env.sctx.config.cache_dir)) / "result.yaml").is_file()
        retried = resume_benchmark(error.result.benchmark_id, sctx=env.sctx)
        assert retried.results == error.result.results
        publish.assert_called_once()
        env.fw.parse_results.assert_called_once()  # no remeasurement on publication retry


def test_secondary_format_failure_retains_already_written_paths(bench_env, monkeypatch):
    env = bench_env
    values = {**env.rows, "json": {"rate": 12}, "csv": "rate\n12\n"}
    env.fw.parse_results.side_effect = lambda *a, **kw: values
    original = Path.write_text

    def write(path, *a, **kw):
        if path.suffix == ".csv":
            raise OSError("CSV disk full")
        return original(path, *a, **kw)

    monkeypatch.setattr(Path, "write_text", write)
    with pytest.raises(BenchmarkFinalizationFailed, match="CSV disk full") as caught:
        benchmark(env.options, sctx=env.sctx)
    assert caught.value.stage == "export" and caught.value.result.results == values
    assert set(caught.value.result.outputs) == {"yaml", "json"}
    assert all(Path(path).is_file() for path in caught.value.result.outputs.values())
    env.stop.assert_called_once()


@pytest.mark.parametrize("export_fails", [False, True])
def test_cleanup_failure_is_retained_without_masking_primary_export_error(bench_env, tmp_path, export_fails):
    env = bench_env
    env.stop.side_effect = OSError("stop unavailable")
    options = replace(env.options, output_file=str(tmp_path)) if export_fails else env.options
    with pytest.raises(BenchmarkFinalizationFailed) as caught:
        benchmark(options, sctx=env.sctx)
    assert caught.value.stage == ("export" if export_fails else "cleanup")
    assert caught.value.errors["cleanup"] == "stop unavailable"
    if export_fails:
        assert isinstance(caught.value.__cause__, IsADirectoryError)
    assert caught.value.result.success and caught.value.result.results == env.rows
    env.stop.assert_called_once()


def test_state_commit_failure_still_stops_owned_inference(scheduled_env, monkeypatch):
    env = scheduled_env
    monkeypatch.setattr("sparkrun.api._benchmark._save_completed_results", Mock(side_effect=OSError("state full")))
    with pytest.raises(BenchmarkIntegrationFailed, match="state full") as caught:
        benchmark(env.options, sctx=env.sctx)
    assert isinstance(caught.value, BenchmarkFinalizationFailed) and caught.value.stage == "state"
    assert caught.value.integration == "<state>" and caught.value.result.success
    env.stop.assert_called_once()


def test_export_interrupt_cleans_up_once_and_propagates(bench_env, monkeypatch):
    monkeypatch.setattr("sparkrun.benchmarking.base.export_results", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        benchmark(bench_env.options, sctx=bench_env.sctx)
    bench_env.stop.assert_called_once()


def test_dry_run_never_exports_or_stops(bench_env, monkeypatch):
    export = Mock(side_effect=AssertionError("no export during preview"))
    monkeypatch.setattr("sparkrun.benchmarking.base.export_results", export)
    assert benchmark(replace(bench_env.options, dry_run=True), sctx=bench_env.sctx).success
    export.assert_not_called()
    bench_env.stop.assert_not_called()


def test_completion_notification_has_same_result_bearing_error_for_both_entrypoints(scheduled_env):
    env = scheduled_env
    register_benchmark_integration(BenchmarkIntegration("review", on_complete=lambda _: None))

    def notify(event):
        if event.kind == "run_complete":
            raise RuntimeError("frontend disconnected")

    with pytest.raises(BenchmarkFinalizationFailed) as initial:
        benchmark(replace(env.options, integrations={"review": {}}, progress_callback=notify), sctx=env.sctx)
    with pytest.raises(BenchmarkFinalizationFailed) as resumed:
        resume_benchmark(initial.value.result.benchmark_id, sctx=env.sctx, progress_callback=notify)
    assert initial.value.stage == resumed.value.stage == "notification"
    assert initial.value.result.results == resumed.value.result.results == {"rows": [env.rows]}
    for error in (initial.value, resumed.value):
        assert isinstance(error.__cause__, RuntimeError) and error.result.success
    env.stop.assert_called_once()


@pytest.mark.parametrize("failing_retry", [False, True])
def test_publication_retries_keep_legacy_measurement_interval(monkeypatch, failing_retry):
    from sparkrun.api._context import default_sctx
    from test_distribution_api_contracts import _completed_state

    original, retry_one, retry_two = ("2026-01-0%dT12:00:00+00:00" % day for day in (1, 2, 3))
    monkeypatch.setattr("sparkrun.benchmarking.run_state._now_iso", lambda: original)
    sctx = default_sctx()
    state = _completed_state(sctx, {})
    seen = []

    def complete(context):
        seen.append((context.result.measured_at, context.result.completed_at))
        if failing_retry:
            raise RuntimeError("publication unavailable")

    register_benchmark_integration(BenchmarkIntegration("review", on_complete=complete))
    for clock in (retry_one, retry_two):
        monkeypatch.setattr("sparkrun.benchmarking.run_state._now_iso", lambda clock=clock: clock)
        if failing_retry:
            with pytest.raises(BenchmarkIntegrationFailed):
                resume_benchmark(state.benchmark_id, sctx=sctx)
        else:
            assert resume_benchmark(state.benchmark_id, sctx=sctx).success
    assert seen == [(original, original), (original, original)]
    saved = BenchmarkRunState.load(state.benchmark_id, str(sctx.config.cache_dir))
    assert saved.updated_at == retry_two
    assert saved.extras["measurement_started_at"] == saved.extras["measurement_completed_at"] == original


def test_new_measurement_interval_is_unchanged_by_publication(scheduled_env):
    env = scheduled_env
    seen = []
    register_benchmark_integration(
        BenchmarkIntegration("review", on_complete=lambda ctx: seen.append((ctx.result.measured_at, ctx.result.completed_at)))
    )
    result = benchmark(replace(env.options, integrations={"review": {}}), sctx=env.sctx)
    resume_benchmark(result.benchmark_id, sctx=env.sctx)
    assert seen[0] == seen[1] and all(seen[0])
    assert seen[0][0] <= seen[0][1]


def test_arena_retry_metadata_uses_measurement_interval(monkeypatch):
    from sparkrun.api._context import default_sctx
    from sparkrun.plugins.sparkarena.integration import complete
    from test_distribution_api_contracts import _completed_state

    original = "2026-01-01T12:00:00+00:00"
    monkeypatch.setattr("sparkrun.benchmarking.run_state._now_iso", lambda: original)
    sctx = default_sctx()
    state = _completed_state(sctx, {"local_test": True})
    state.extras[STATE_KEY]["review"]["data"] = {
        "submission_id": "test-local-submission",
        "effective_recipe_text": "model: test/model\n",
        "metadata_json": {"timing": {"start": original, "end": original}},
    }
    state.save(str(sctx.config.cache_dir))
    (state.state_dir(str(sctx.config.cache_dir)) / "result.yaml").write_text("csv: 'rate,12'\n")
    register_benchmark_integration(BenchmarkIntegration("review", on_complete=complete))
    metadata = []
    for day in (2, 3):
        monkeypatch.setattr("sparkrun.benchmarking.run_state._now_iso", lambda day=day: "2026-01-0%dT12:00:00+00:00" % day)
        resume_benchmark(state.benchmark_id, sctx=sctx)
        metadata.append(json.loads((sctx.config.cache_dir / "benchmarks/test-local-submission/metadata.json").read_text()))
    assert metadata[0]["benchmark"]["measured_at"] == metadata[1]["benchmark"]["measured_at"] == original
    assert metadata[0]["timing"] == metadata[1]["timing"] == {"start": original, "end": original, "duration": 0}


def test_failed_stop_result_is_a_cleanup_failure(bench_env):
    from sparkrun.api import StopResult

    bench_env.stop.return_value = StopResult("test-job", ("localhost",), 0, hosts_failed=("localhost",))
    with pytest.raises(BenchmarkFinalizationFailed, match="cleanup incomplete") as caught:
        benchmark(bench_env.options, sctx=bench_env.sctx)
    assert caught.value.stage == "cleanup" and caught.value.result.results == bench_env.rows
    bench_env.stop.assert_called_once()


def test_incomplete_resume_export_failure_preserves_results_without_stopping_unowned_inference(scheduled_env, tmp_path):
    import sys
    from sparkrun.api import BenchmarkFailed

    env = scheduled_env
    register_benchmark_integration(BenchmarkIntegration("review", on_complete=lambda _: None))
    original = env.fw.build_benchmark_command.side_effect
    env.fw.build_benchmark_command.side_effect = lambda *a, **kw: [sys.executable, "-c", "raise SystemExit(1)"]
    with pytest.raises(BenchmarkFailed, match="incomplete"):
        benchmark(replace(env.options, integrations={"review": {}}), sctx=env.sctx)
    state_path = next(env.sctx.config.cache_dir.glob("benchmarks/bench_*/state.yaml"))
    env.fw.build_benchmark_command.side_effect = original
    env.stop.reset_mock()
    output_dir = tmp_path / "invalid-output-directory"
    output_dir.write_text("a file, not a directory")
    env.sctx.config.set("defaults.benchmark_output_dir", str(output_dir))
    with pytest.raises(BenchmarkFinalizationFailed) as caught:
        resume_benchmark(state_path.parent.name, sctx=env.sctx)
    assert caught.value.stage == "export"
    assert caught.value.result.results == {"rows": [env.rows]}
    env.stop.assert_not_called()
    assert resume_benchmark(state_path.parent.name, sctx=env.sctx).results == caught.value.result.results
