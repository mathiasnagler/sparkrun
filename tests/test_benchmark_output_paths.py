"""Exported references remain usable across working directories and recovery."""

from dataclasses import replace
from pathlib import Path

import pytest

from sparkrun.api import benchmark, resume_benchmark, BenchmarkFinalizationFailed
from sparkrun.benchmarking.run_state import BenchmarkRunState
from sparkrun.core.benchmark_integrations import BenchmarkIntegration, register_benchmark_integration
from test_benchmark_startup_collection import bench_env as bench_env
from test_benchmark_api_contract import scheduled_env as scheduled_env


@pytest.mark.parametrize("path_source", ["relative", "absolute", "configured"])
def test_export_paths_survive_completed_resume(scheduled_env, tmp_path, monkeypatch, path_source):
    env = scheduled_env
    monkeypatch.setattr("sparkrun.core.benchmark_integrations._INTEGRATIONS", {})
    seen = []
    register_benchmark_integration(BenchmarkIntegration("paths", on_complete=lambda ctx: seen.append(dict(ctx.result.outputs))))
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    env.fw.consolidate_per_task_results.side_effect = lambda rows: {"rows": rows, "json": {"rows": rows}, "csv": "speed\n42\n"}
    env.sctx.config.set("defaults.benchmark_output_dir", "artifacts")
    output = {"relative": "artifacts/run.yaml", "absolute": str(first / "artifacts/run.yaml"), "configured": None}[path_source]
    result = benchmark(replace(env.options, output_file=output, integrations={"paths": {}}), sctx=env.sctx)
    assert set(result.outputs) == {"yaml", "json", "csv"}
    assert seen == [result.outputs]
    assert all(Path(path).is_absolute() and Path(path).is_file() for path in result.outputs.values())
    saved = BenchmarkRunState.load(result.benchmark_id, str(env.sctx.config.cache_dir))
    assert saved.extras["benchmark_outputs"] == result.outputs
    monkeypatch.chdir(second)
    env.run.reset_mock()
    resumed = resume_benchmark(result.benchmark_id, sctx=env.sctx)
    env.run.assert_not_called()
    assert resumed.outputs == result.outputs
    assert all(Path(path).is_file() for path in resumed.outputs.values())


def test_partial_export_error_keeps_absolute_successful_paths(scheduled_env, tmp_path, monkeypatch):
    env = scheduled_env
    monkeypatch.chdir(tmp_path)
    env.fw.consolidate_per_task_results.side_effect = lambda rows: {"rows": rows, "json": {"rows": rows}, "csv": "speed\n42\n"}
    original = Path.write_text

    def write(path, data, *args, **kwargs):
        if path.suffix == ".csv":
            raise OSError("CSV storage failed")
        return original(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    with pytest.raises(BenchmarkFinalizationFailed, match="CSV storage failed") as caught:
        benchmark(replace(env.options, output_file="artifacts/run.yaml"), sctx=env.sctx)
    result = caught.value.result
    assert caught.value.stage == "export" and result.success
    assert set(result.outputs) == {"yaml", "json"}
    assert all(Path(path).is_absolute() and Path(path).is_file() for path in result.outputs.values())


def test_legacy_relative_outputs_are_not_rebased_on_resume(scheduled_env, tmp_path, monkeypatch, caplog):
    env = scheduled_env
    first = benchmark(env.options, sctx=env.sctx)
    state = BenchmarkRunState.load(first.benchmark_id, str(env.sctx.config.cache_dir))
    state.extras["benchmark_outputs"]["json"] = "unknown-original-directory/result.json"
    state.save(str(env.sctx.config.cache_dir))
    monkeypatch.chdir(tmp_path)
    resumed = resume_benchmark(first.benchmark_id, sctx=env.sctx)
    assert resumed.outputs == first.outputs
    assert "original directory is unknown" in caplog.text
    assert resumed.success and resumed.already_complete
