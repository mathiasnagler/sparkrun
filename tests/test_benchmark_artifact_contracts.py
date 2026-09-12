"""Only measurements from successful current task attempts may be published."""

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from sparkrun.api import BenchmarkFailed, benchmark, resume_benchmark
from sparkrun.benchmarking.run_state import BenchmarkRunState
from sparkrun.benchmarking.scheduler import BenchTask
from test_benchmark_api_contract import scheduled_env as scheduled_env
from test_benchmark_startup_collection import bench_env as bench_env


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "interrupt"])
@pytest.mark.parametrize("retry_output", [None, "invalid JSON", "[]"])
def test_resume_accepts_only_fresh_successful_artifacts(scheduled_env, monkeypatch, failure, retry_output):
    from sparkrun.core.benchmark_integrations import BenchmarkIntegration, register_benchmark_integration
    from sparkrun.utils.data import thaw

    env = scheduled_env
    monkeypatch.setattr("sparkrun.core.benchmark_integrations._INTEGRATIONS", {})
    published = []
    register_benchmark_integration(
        BenchmarkIntegration("artifact-contract", on_complete=lambda ctx: published.append(thaw(ctx.result.results)))
    )
    env.fw.build_task_list.return_value = [BenchTask(i, str(i), {"index": i}) for i in range(2)]
    env.fw.task_coverage_key.side_effect = lambda task: task.index
    env.fw.consolidated_coverage_keys.side_effect = lambda result: {row["index"] for row in result.get("rows", [])}
    phase = "fail"
    calls = []

    def command(target, model, args, *, result_file):
        index = args["index"]
        calls.append(index)
        assert len(calls) <= 2, "failed tasks must not loop within an invocation"
        assert not Path(result_file).exists(), "old output must be removed before command construction"
        value = json.dumps({"index": index, "speed": 42})
        if phase == "retry":
            value = retry_output
        script = "pass" if value is None else "from pathlib import Path; Path(%r).write_text(%r)" % (result_file, value)
        return [sys.executable, "-c", script]

    def process(cmd, **kwargs):
        subprocess.run(cmd, check=True, capture_output=True)
        if phase == "fail" and calls[-1] == 1:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(cmd, 1)
            if failure == "interrupt":
                raise KeyboardInterrupt
            return 7
        return 0

    env.fw.build_benchmark_command.side_effect = command
    monkeypatch.setattr("sparkrun.benchmarking.scheduler.run_benchmark_process", process)
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else BenchmarkFailed):
        benchmark(replace(env.options, timeout=1, export_files=False, integrations={"artifact-contract": {}}), sctx=env.sctx)
    assert calls == [0, 1] and published == []
    state_file = next(env.sctx.config.cache_dir.glob("benchmarks/bench_*/state.yaml"))
    runs = state_file.parent / "runs"
    preserved = (runs / "000.json").read_bytes()
    # Neither a failed attempt nor unrelated/obsolete task artifacts may cover a retry.
    assert json.loads((runs / "001.json").read_text())["index"] == 1
    (runs / "001_old-suffix.json").write_text(json.dumps({"index": 1, "speed": -1}))
    (runs / "stray.json").write_text(json.dumps({"index": 1, "speed": -2}))

    phase = "retry"
    calls.clear()
    with pytest.raises(BenchmarkFailed, match="incomplete"):
        resume_benchmark(state_file.parent.name, sctx=env.sctx, export_files=False)
    assert calls == [1] and published == []
    saved = BenchmarkRunState.load(state_file.parent.name, str(env.sctx.config.cache_dir))
    assert saved.completed_indices == [0] and saved.failed_indices == [1]
    assert (runs / "000.json").read_bytes() == preserved
    assert saved.sessions[-1]["status"] == "partial"

    phase = "success"
    calls.clear()
    result = resume_benchmark(state_file.parent.name, sctx=env.sctx, export_files=False)
    assert calls == [1] and result.success
    assert result.results == {"rows": [{"index": 0, "speed": 42}, {"index": 1, "speed": 42}]}
    assert published == [result.results]
    assert (runs / "000.json").read_bytes() == preserved


def test_measurement_gaps_remain_incomplete_after_one_gap_pass(scheduled_env, monkeypatch):
    env = scheduled_env
    env.fw.consolidated_coverage_keys.side_effect = lambda result: set()
    original = env.fw.build_benchmark_command.side_effect
    calls = []

    def command(*args, **kwargs):
        calls.append(kwargs["result_file"])
        assert len(calls) <= 2, "gap retries must be bounded"
        return original(*args, **kwargs)

    env.fw.build_benchmark_command.side_effect = command
    with pytest.raises(BenchmarkFailed, match="incomplete"):
        benchmark(replace(env.options, export_files=False), sctx=env.sctx)
    assert len(calls) == 2
    state_file = next(env.sctx.config.cache_dir.glob("benchmarks/bench_*/state.yaml"))
    saved = BenchmarkRunState.load(state_file.parent.name, str(env.sctx.config.cache_dir))
    assert saved.completed_indices == [] and saved.failed_indices == [0]
    assert saved.sessions[-1]["status"] == "partial"


@pytest.mark.parametrize("damaged", [None, "not JSON", "[]"])
def test_saved_success_without_usable_artifact_is_retried(tmp_path, damaged):
    from unittest.mock import Mock, patch
    from sparkrun.benchmarking.scheduler import run_schedule
    from test_benchmark_scheduler import _make_state, _make_tasks, _FakeFW, _make_process_runner

    state = _make_state(tmp_path, n_tasks=2)
    state.completed_indices = [0, 1]
    state.save(str(tmp_path))
    runs = state.runs_dir(str(tmp_path))
    (runs / "000.json").write_text('{"preserved": true}')
    if damaged is not None:
        (runs / "001.json").write_text(damaged)
    (runs / "001_obsolete.json").write_text('{"obsolete": true}')
    process = Mock(side_effect=_make_process_runner([0]))
    with patch("sparkrun.benchmarking.scheduler.run_benchmark_process", process):
        result = run_schedule(
            _FakeFW(), _make_tasks(2), state, target_url="http://local", model="m", timeout=1, progress_ui=Mock(), cache_dir=str(tmp_path)
        )
    process.assert_called_once()
    assert process.call_args.args[0][-1] == str(runs / "001.json")
    assert result.success and result.failed_count == 0
    assert result.consolidated["runs"][0] == {"preserved": True}
    assert len(result.consolidated["runs"]) == 2
    assert not any(row.get("obsolete") for row in result.consolidated["runs"])
