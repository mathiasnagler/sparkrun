"""Real tool-eval framework scheduling, recovery, and version identity contracts."""

from dataclasses import replace
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from sparkrun.api import BenchmarkFailed, ResumeMode, benchmark, resume_benchmark
from sparkrun.benchmarking.run_state import BenchmarkRunState
from sparkrun.benchmarking.tool_eval_bench import ToolEvalBenchFramework
from test_benchmark_api_contract import scheduled_env as scheduled_env
from test_benchmark_startup_collection import bench_env as bench_env


def test_tool_suite_retries_failed_attempt_and_reuses_completed_output(scheduled_env, monkeypatch):
    env = scheduled_env
    fw = ToolEvalBenchFramework()
    monkeypatch.setattr("sparkrun.core.bootstrap.get_benchmarking_framework", lambda *a, **kw: fw)
    monkeypatch.setattr(fw, "check_prerequisites", lambda: [])
    payload = {
        "schema_version": "1",
        "tool_eval_bench_version": "2.6.0",
        "final_score": 50,
        "scores": {"scenario_results": [{"scenario_id": "TC-01", "status": "fail", "failure_kind": "timeout"}]},
    }
    commands = []
    failed = True
    actual_command = fw.build_benchmark_command

    def command(target_url, model, args, result_file):
        commands.append(actual_command(target_url, model, args, result_file))
        source = "from pathlib import Path; Path(%r).write_text(%r); raise SystemExit(%d)" % (
            result_file,
            json.dumps(payload),
            1 if failed else 0,
        )
        return [sys.executable, "-c", source]

    monkeypatch.setattr(fw, "build_benchmark_command", command)
    options = replace(env.options, framework=fw.framework_name, bench_args={"label": "first, second"})
    with pytest.raises(BenchmarkFailed, match="incomplete"):
        benchmark(options, sctx=env.sctx)
    state_path = next(env.sctx.config.cache_dir.glob("benchmarks/bench_*/state.yaml"))
    state = BenchmarkRunState.load(state_path.parent.name, str(env.sctx.config.cache_dir))
    assert state.failed_indices == [0] and state.completed_indices == []
    assert state.base_args["ref"] == "v2.6.0"
    assert state.base_args["label"] == "first, second"
    failed = False
    result = resume_benchmark(state.benchmark_id, sctx=env.sctx)
    assert result.success and result.resumed and result.results["json"] == payload
    assert result.category == "tools"
    assert len(commands) == 2
    assert "--json-file" in commands[1] and "first, second" in commands[1]
    assert commands[1][commands[1].index("--from") + 1].endswith("@v2.6.0")
    assert json.loads(Path(result.outputs["json"]).read_text()) == payload
    assert "timeout" in Path(result.outputs["csv"]).read_text()

    forbidden = Mock(side_effect=AssertionError("completed output must not require execution"))
    monkeypatch.setattr(fw, "build_benchmark_command", forbidden)
    monkeypatch.setattr(fw, "check_prerequisites", forbidden)
    reused = resume_benchmark(state.benchmark_id, sctx=env.sctx)
    assert reused.already_complete and reused.results["json"] == payload
    forbidden.assert_not_called()


def test_tool_version_participates_in_measurement_identity(scheduled_env, monkeypatch):
    env = scheduled_env
    fw = ToolEvalBenchFramework()
    monkeypatch.setattr("sparkrun.core.bootstrap.get_benchmarking_framework", lambda *a, **kw: fw)
    options = replace(env.options, framework=fw.framework_name, dry_run=True, resume=ResumeMode.FRESH)
    old = benchmark(replace(options, bench_args={"ref": "v2.5.0"}), sctx=env.sctx)
    new = benchmark(options, sctx=env.sctx)
    assert old.benchmark_id != new.benchmark_id
    assert all(call.args[0].dry_run for call in env.run.call_args_list)
