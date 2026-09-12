"""Persist measurement semantics before processing and publication can fail."""

from dataclasses import replace
import sys
import pytest
from sparkrun import api
from test_benchmark_startup_collection import bench_env as bench_env
from test_benchmark_api_contract import scheduled_env as scheduled_env


@pytest.mark.parametrize("entrypoint", ["resume", "initial"])
@pytest.mark.parametrize("failure", ["decoder", "consolidation", "commit", "partial"])
def test_processing_recovery_preserves_selected_category_and_measurement_interval(scheduled_env, monkeypatch, entrypoint, failure):
    from datetime import datetime, timezone, timedelta
    from sparkrun.api import benchmark, resume_benchmark, ResumeMode
    from sparkrun.benchmarking.run_state import BenchmarkRunState
    from sparkrun.core.benchmark_integrations import BenchmarkIntegration, register_benchmark_integration

    env = scheduled_env
    now = [datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)]
    started = now[0].isoformat()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0]

    monkeypatch.setattr("sparkrun.api._benchmark.datetime", Clock)
    monkeypatch.setattr("sparkrun.benchmarking.run_state.datetime", Clock)
    monkeypatch.setattr("sparkrun.core.benchmark_integrations._INTEGRATIONS", {})
    env.fw.categories = ("performance", "tools")
    monkeypatch.setattr("sparkrun.core.bootstrap.get_benchmarking_frameworks_for_category", lambda *_: [env.fw])
    published = []
    register_benchmark_integration(BenchmarkIntegration("review", on_complete=lambda ctx: published.append(ctx.result.category)))
    original_command = env.fw.build_benchmark_command.side_effect

    attempts = []

    def command(*args, **kwargs):
        now[0] += timedelta(minutes=5)
        attempts.append(True)
        if failure == "partial" and len(attempts) == 1:
            return [sys.executable, "-c", "raise SystemExit(7)"]
        return original_command(*args, **kwargs)

    env.fw.build_benchmark_command.side_effect = command
    options = replace(env.options, category="tools", export_files=False, integrations={"review": {}})
    with monkeypatch.context() as initial_failure:
        if failure == "decoder":
            initial_failure.setattr(env.fw.parse_results, "side_effect", RuntimeError("interrupted decoder"))
        elif failure == "consolidation":
            original = env.fw.consolidate_per_task_results.side_effect

            def consolidate(rows):
                if rows:
                    raise RuntimeError("interrupted consolidation")
                return original(rows)

            initial_failure.setattr(env.fw.consolidate_per_task_results, "side_effect", consolidate)
        elif failure == "commit":

            def commit(*args, **kwargs):
                raise RuntimeError("interrupted commit")

            initial_failure.setattr("sparkrun.api._benchmark._save_completed_results", commit)
        with pytest.raises(api.SparkrunError):
            benchmark(options, sctx=env.sctx)
    directory = next(env.sctx.config.cache_dir.glob("benchmarks/bench_*/state.yaml")).parent
    state = BenchmarkRunState.load(directory.name, str(env.sctx.config.cache_dir))
    assert state.completed_indices == ([] if failure == "partial" else [0])
    assert not state.extras.get("measurement_complete")
    assert state.extras["benchmark_category"] == "tools"
    assert state.extras["measurement_started_at"] == started
    if failure == "partial":
        assert "measurement_completed_at" not in state.extras
    else:
        assert state.extras["measurement_completed_at"] == "2026-09-12T12:05:00+00:00"
    now[0] = datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc)
    result = (
        resume_benchmark(directory.name, sctx=env.sctx, export_files=False)
        if entrypoint == "resume"
        else benchmark(replace(options, resume=ResumeMode.REQUIRED), sctx=env.sctx)
    )
    assert result.success and result.category == "tools" and published == ["tools"]
    restored = BenchmarkRunState.load(directory.name, str(env.sctx.config.cache_dir))
    assert restored.extras["measurement_started_at"] == started
    expected_end = "2026-09-12T13:05:00+00:00" if failure == "partial" else "2026-09-12T12:05:00+00:00"
    assert restored.extras["measurement_completed_at"] == expected_end
    assert env.fw.build_benchmark_command.call_count == (2 if failure == "partial" else 1)


def test_incomplete_native_resume_rejects_host_endpoint_before_query(scheduled_env, monkeypatch):
    from unittest.mock import Mock
    from sparkrun.benchmarking.run_state import BenchmarkRunState

    env = scheduled_env
    env.fw.build_benchmark_command.side_effect = lambda **kw: [sys.executable, "-c", "raise SystemExit(7)"]
    with pytest.raises(api.BenchmarkFailed):
        api.benchmark(env.options, sctx=env.sctx)
    directory = next(env.sctx.config.cache_dir.glob("benchmarks/bench_*/state.yaml")).parent
    state = BenchmarkRunState.load(directory.name, str(env.sctx.config.cache_dir))
    assert not state.completed_indices
    monkeypatch.setattr(
        "sparkrun.orchestration.job_metadata.load_job_metadata",
        lambda *a, **kw: {
            "hosts": ["localhost"],
            "port": 8000,
            "executor": "k8s",
            "native_resource": {"kind": "JobSet", "name": "test"},
            "recipe_state": env.launch.recipe.__getstate__(),
            "overrides": dict(env.launch.overrides),
        },
    )
    query = Mock(side_effect=AssertionError("must reject before endpoint discovery"))
    monkeypatch.setattr("sparkrun.orchestration.job_metadata.check_job_running", query)
    with pytest.raises(api.BenchmarkFailed, match="reachable inference endpoint"):
        api.resume_benchmark(state.benchmark_id, sctx=env.sctx)
    query.assert_not_called()
    assert env.fw.build_benchmark_command.call_count == 1
