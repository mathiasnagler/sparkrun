"""Exported references remain usable across working directories and recovery."""

from dataclasses import replace
from pathlib import Path
import json
import sys

import yaml

import pytest

from sparkrun.api import benchmark, resume_benchmark, BenchmarkFailed, BenchmarkFinalizationFailed
from sparkrun.benchmarking.run_state import BenchmarkRunState
from sparkrun.core.benchmark_integrations import BenchmarkIntegration, register_benchmark_integration
from test_benchmark_startup_collection import bench_env as bench_env
from test_benchmark_api_contract import scheduled_env as scheduled_env


@pytest.mark.parametrize("path_source", ["relative", "absolute", "configured"])
@pytest.mark.parametrize(
    "filename,sidecar_stem",
    [
        ("run.yaml", "run"),
        ("run.yml", "run"),
        ("run.YAML", "run"),
        ("run.json", "run.json"),
        ("run.csv", "run.csv"),
        ("run", "run"),
    ],
)
def test_export_paths_survive_completed_resume(scheduled_env, tmp_path, monkeypatch, path_source, filename, sidecar_stem):
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
    output = {"relative": "artifacts/" + filename, "absolute": str(first / "artifacts" / filename), "configured": None}[path_source]
    result = benchmark(replace(env.options, output_file=output, integrations={"paths": {}}), sctx=env.sctx)
    assert set(result.outputs) == {"yaml", "json", "csv"}
    assert seen == [result.outputs]
    assert len(set(result.outputs.values())) == 3
    assert "sparkrun_benchmark" in yaml.safe_load(Path(result.outputs["yaml"]).read_text())
    assert json.loads(Path(result.outputs["json"]).read_text()) == result.results["json"]
    assert Path(result.outputs["csv"]).read_text() == "speed\n42\n"
    if path_source != "configured":
        assert Path(result.outputs["yaml"]).name == filename
        assert Path(result.outputs["json"]).name == sidecar_stem + ".json"
        assert Path(result.outputs["csv"]).name == sidecar_stem + ".csv"
    assert all(Path(path).is_absolute() and Path(path).is_file() for path in result.outputs.values())
    saved = BenchmarkRunState.load(result.benchmark_id, str(env.sctx.config.cache_dir))
    assert saved.extras["benchmark_outputs"] == result.outputs
    monkeypatch.chdir(second)
    env.run.reset_mock()
    resumed = resume_benchmark(result.benchmark_id, sctx=env.sctx)
    env.run.assert_not_called()
    assert resumed.outputs == result.outputs
    assert all(Path(path).is_file() for path in resumed.outputs.values())


@pytest.mark.parametrize("filename", ["run.yaml", "run.csv"])
def test_partial_export_error_keeps_absolute_successful_paths(scheduled_env, tmp_path, monkeypatch, filename):
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
        benchmark(replace(env.options, output_file="artifacts/" + filename), sctx=env.sctx)
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


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_aliased_exports_fail_before_overwriting_files(scheduled_env, tmp_path, link_kind):
    env = scheduled_env
    primary = tmp_path / "run.yaml"
    sidecar = tmp_path / "run.json"
    primary.write_text("previous export")
    if link_kind == "symlink":
        sidecar.symlink_to(primary)
    else:
        sidecar.hardlink_to(primary)
    env.fw.consolidate_per_task_results.side_effect = lambda rows: {"rows": rows, "json": {"rows": rows}}
    with pytest.raises(BenchmarkFinalizationFailed, match="same file") as caught:
        benchmark(replace(env.options, output_file=str(primary)), sctx=env.sctx)
    assert caught.value.stage == "export"
    assert caught.value.result.outputs == {}
    assert primary.read_text() == sidecar.read_text() == "previous export"


@pytest.mark.parametrize("suffix", ["json", "csv"])
def test_resumed_measurement_preserves_primary_and_sidecars(scheduled_env, tmp_path, suffix):
    env = scheduled_env
    original = env.fw.build_benchmark_command.side_effect
    env.fw.build_benchmark_command.side_effect = lambda *a, **kw: [sys.executable, "-c", "raise SystemExit(1)"]
    with pytest.raises(BenchmarkFailed):
        benchmark(replace(env.options, export_files=False), sctx=env.sctx)
    path = next(env.sctx.config.cache_dir.glob("benchmarks/bench_*/state.yaml"))
    env.fw.build_benchmark_command.side_effect = original
    env.fw.consolidate_per_task_results.side_effect = lambda rows: {"rows": rows, "json": {"rows": rows}, "csv": "speed\n42\n"}
    primary = tmp_path / ("resumed." + suffix)
    result = resume_benchmark(path.parent.name, sctx=env.sctx, output_file=str(primary))
    assert result.success and result.resumed and not result.already_complete
    assert result.outputs == {"yaml": str(primary), "json": str(primary) + ".json", "csv": str(primary) + ".csv"}
    assert "sparkrun_benchmark" in yaml.safe_load(primary.read_text())
    assert json.loads(Path(result.outputs["json"]).read_text()) == result.results["json"]
    assert Path(result.outputs["csv"]).read_text() == "speed\n42\n"
    state = BenchmarkRunState.load(result.benchmark_id, str(env.sctx.config.cache_dir))
    assert state.extras["benchmark_outputs"] == result.outputs
