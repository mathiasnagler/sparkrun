"""Arena as a benchmark integration: CLI/API parity, persistence, and retries."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from sparkrun.api import BenchmarkOptions, ResumeMode, benchmark
from sparkrun.api._benchmark import _NullProgressEmitter, resume_benchmark
from sparkrun.api._context import default_sctx
from sparkrun.api._errors import BenchmarkFailed
from sparkrun.benchmarking.run_state import BenchmarkRunState
from sparkrun.benchmarking.scheduler import BenchTask, ScheduleRunResult
from sparkrun.core.benchmark_integrations import BenchmarkIntegrationSession, BenchmarkIntegrationContext, STATE_KEY
from sparkrun.core.recipe import Recipe
from sparkrun.plugins.sparkarena.integration import validate_recipe_for_submission
from test_benchmark_startup_collection import bench_env as bench_env


@pytest.mark.parametrize(
    "argv,settings",
    [
        (["benchmark", "perf", "r", "--arena", "--local-test"], {"arena": {"local_test": True}}),
        (["benchmark", "run", "r", "--arena"], {"arena": {}}),
        (["arena", "benchmark", "r", "--local-test"], {"arena": {"local_test": True}}),
        (["arena", "benchmark", "run", "r"], {"arena": {}}),
        (["benchmark", "run", "r"], {}),
    ],
)
def test_cli_routes_options_through_shared_api(monkeypatch, argv, settings):
    from sparkrun.cli import main

    received = []

    def execute(options, **kwargs):
        received.append(options)
        raise SystemExit(99)

    monkeypatch.setattr("sparkrun.api._benchmark._execute_benchmark", execute)
    result = CliRunner().invoke(main, [*argv, "--hosts", "localhost", "--dry-run"])
    assert result.exit_code == 99, result.output
    assert received[0].integrations == settings
    assert received[0].dry_run


def test_local_test_requires_arena():
    from sparkrun.cli import main

    result = CliRunner().invoke(main, ["benchmark", "run", "r", "--hosts", "localhost", "--local-test"])
    assert result.exit_code == 2
    assert "--local-test requires --arena" in result.output


@pytest.mark.parametrize(
    "settings,profile,category,expected_profile",
    [
        ({}, None, None, "@official/spark-arena-v2"),
        ({}, "custom", None, "custom"),
        ({"local_test": True}, None, None, None),
        ({}, None, "evals", "@official/spark-arena-v2"),
    ],
)
def test_arena_defaults(settings, profile, category, expected_profile):
    options = BenchmarkOptions(recipe="r", profile=profile, category=category, integrations={"arena": settings})
    session = BenchmarkIntegrationSession(options, sctx=default_sctx(), emitter=_NullProgressEmitter())
    prepared = session.prepare()
    assert prepared.profile == expected_profile
    assert prepared.category == (category or "performance")
    assert options.profile == profile  # caller's frozen options are preserved


@pytest.fixture
def arena_env(bench_env, monkeypatch):
    env = bench_env
    env.sctx.config.set("defaults.benchmark_output_dir", str(Path(env.options.output_file).parent))
    env.options = replace(env.options, integrations={"arena": {"local_test": True}}, export_files=False, decision_callback=lambda _: True)
    env.rows = {"rows": [{"tokens_per_second": 123}], "csv": "tokens_per_second\n123\n"}
    env.fw.build_task_list.return_value = [BenchTask(0, "first"), BenchTask(1, "second")]
    env.fw.detect_version.return_value = None
    env.fw.measured_nothing.return_value = False
    monkeypatch.setattr("sparkrun.core.bootstrap.get_benchmarking_frameworks_for_category", lambda *a, **k: [env.fw])
    monkeypatch.setattr("sparkrun.benchmarking.progress_ui.BenchmarkProgressUI", lambda **kw: nullcontext())
    monkeypatch.setattr("sparkrun.orchestration.primitives.resolve_image_sha", lambda *a, **kw: None)
    env.upload = Mock(return_value=(True, "ignored"))
    env.token = Mock(return_value="test-token")
    env.auth = Mock()
    monkeypatch.setattr("sparkrun.plugins.sparkarena.auth.load_refresh_token", env.token)
    monkeypatch.setattr("sparkrun.plugins.sparkarena.auth.exchange_token", env.auth)
    monkeypatch.setattr("sparkrun.plugins.sparkarena.upload.upload_benchmark_results", env.upload)
    env.schedule_success = True

    def schedule(**kwargs):
        state = kwargs["state"]
        # The ID and provenance must exist before any scheduler failures.
        assert state.extras[STATE_KEY]["arena"]["data"]["submission_id"]
        state.completed_indices = [0, 1] if env.schedule_success else [0]
        state.save(kwargs["cache_dir"])
        return ScheduleRunResult(env.schedule_success, len(state.completed_indices), 0, state, env.rows)

    env.schedule = Mock(side_effect=schedule)
    monkeypatch.setattr("sparkrun.benchmarking.scheduler.run_schedule", env.schedule)
    monkeypatch.setattr(
        "sparkrun.orchestration.job_metadata.load_job_metadata",
        lambda *a, **kw: {
            "hosts": ["localhost"],
            "port": 8000,
            "overrides": dict(env.launch.overrides),
            "recipe_state": env.launch.recipe.__getstate__(),
            "effective_container_image": env.launch.container_image,
        },
    )
    monkeypatch.setattr("sparkrun.orchestration.job_metadata.check_job_running", lambda **kw: SimpleNamespace(running=True))
    return env


def _state(env, benchmark_id=None):
    if benchmark_id is None:
        paths = list((env.sctx.config.cache_dir / "benchmarks").glob("bench_*/state.yaml"))
        assert len(paths) == 1
        benchmark_id = paths[0].parent.name
    state = BenchmarkRunState.load(benchmark_id, str(env.sctx.config.cache_dir))
    assert state is not None
    return state


def test_local_test_writes_real_artifacts_and_never_uploads(arena_env):
    env = arena_env
    result = benchmark(env.options, sctx=env.sctx)
    summary = result.integration_results["arena"]
    assert not summary["uploaded"] and summary["local_test"]
    state = _state(env, result.benchmark_id)
    data = state.extras[STATE_KEY]["arena"]["data"]
    assert data["submission_id"] == summary["submission_id"]
    directory = env.sctx.config.cache_dir / "benchmarks" / summary["submission_id"]
    assert (directory / "benchmark.csv").read_text() == env.rows["csv"]
    assert (directory / "recipe.yaml").read_text() == data["effective_recipe_text"]
    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["cluster"]["hosts_redacted"]
    assert "localhost" not in json.dumps(metadata)
    assert metadata["timing"]["start"] and metadata["timing"]["end"]
    env.auth.assert_not_called()
    env.upload.assert_not_called()


def test_failed_run_saves_submission_and_resume_reuses_it(arena_env):
    env = arena_env
    env.schedule_success = False
    with pytest.raises(BenchmarkFailed, match="incomplete"):
        benchmark(env.options, sctx=env.sctx)
    state = _state(env)
    original = state.extras[STATE_KEY]["arena"]
    assert original["data"]["effective_recipe_text"]
    assert original["data"]["metadata_json"]
    env.upload.assert_not_called()
    env.schedule_success = True
    results = resume_benchmark(state.benchmark_id, sctx=env.sctx)
    assert results.results == env.rows
    assert results.integration_results["arena"]["submission_id"] == original["data"]["submission_id"]
    resumed = _state(env, state.benchmark_id)
    assert resumed.extras[STATE_KEY]["arena"]["data"]["submission_id"] == original["data"]["submission_id"]
    assert resumed.extras[STATE_KEY]["arena"]["settings"]["local_test"]
    env.upload.assert_not_called()


def test_upload_retry_does_not_need_running_inference_and_is_idempotent(arena_env, monkeypatch):
    env = arena_env
    # Explicit profile avoids fetching the official profile in this hermetic test.
    monkeypatch.setattr("sparkrun.plugins.sparkarena.integration.ARENA_BENCHMARK_PROFILE", None)
    env.options = replace(env.options, integrations={"arena": {}})
    env.upload.return_value = (False, "failed")
    from sparkrun.api import BenchmarkIntegrationFailed

    with pytest.raises(BenchmarkIntegrationFailed, match="failed to upload") as failure:
        benchmark(env.options, sctx=env.sctx)
    state = _state(env)
    completed = failure.value.result
    assert completed.success and completed.results == env.rows
    assert completed.benchmark_id == state.benchmark_id
    assert failure.value.integration == "arena"
    assert "arena" in completed.integration_errors
    assert not completed.integration_results["arena"]["uploaded"]
    sid = state.extras[STATE_KEY]["arena"]["data"]["submission_id"]
    assert state.extras["measurement_complete"]
    env.upload.return_value = (True, sid)
    monkeypatch.setattr("sparkrun.orchestration.job_metadata.check_job_running", Mock(side_effect=AssertionError("must not probe")))
    for _ in range(2):
        completed = resume_benchmark(state.benchmark_id, sctx=env.sctx)
        assert completed.results == env.rows and completed.success and completed.resumed
        assert completed.integration_results["arena"]["uploaded"]
        assert not completed.integration_errors
    assert env.schedule.call_count == 1
    assert env.upload.call_count == 2  # failed initial attempt and one successful retry
    assert {c.kwargs["submission_id"] for c in env.upload.call_args_list} == {sid}
    assert _state(env).extras[STATE_KEY]["arena"]["data"]["uploaded"]


def test_run_resume_keeps_original_id_and_fresh_gets_new_id(arena_env):
    env = arena_env
    first = benchmark(env.options, sctx=env.sctx)
    second = benchmark(replace(env.options, resume=ResumeMode.IF_EXISTS), sctx=env.sctx)
    assert first.integration_results["arena"]["submission_id"] == second.integration_results["arena"]["submission_id"]
    third = benchmark(env.options, sctx=env.sctx)
    assert first.integration_results["arena"]["submission_id"] != third.integration_results["arena"]["submission_id"]


def test_dry_run_has_no_auth_upload_or_persistent_submission(arena_env):
    env = arena_env
    result = benchmark(replace(env.options, dry_run=True), sctx=env.sctx)
    assert result.success
    env.auth.assert_not_called()
    env.upload.assert_not_called()
    env.schedule.assert_not_called()
    assert not list(env.sctx.config.cache_dir.glob("benchmarks/*/state.yaml"))


def test_dry_run_resume_never_executes_schedule_or_changes_state(arena_env, monkeypatch):
    env = arena_env
    env.schedule_success = False
    with pytest.raises(BenchmarkFailed):
        benchmark(env.options, sctx=env.sctx)
    state = _state(env)
    path = state.state_dir(str(env.sctx.config.cache_dir)) / "state.yaml"
    before = path.read_bytes()
    env.schedule.reset_mock()
    monkeypatch.setattr("sparkrun.orchestration.job_metadata.check_job_running", Mock(side_effect=AssertionError("must not probe")))
    assert resume_benchmark(state.benchmark_id, dry_run=True, sctx=env.sctx).results == {}
    assert path.read_bytes() == before
    env.schedule.assert_not_called()
    env.upload.assert_not_called()


def test_disabled_plugin_allows_plain_resume_and_retains_extras(arena_env, monkeypatch):
    env = arena_env
    env.schedule_success = False
    with pytest.raises(BenchmarkFailed):
        benchmark(env.options, sctx=env.sctx)
    state = _state(env)
    saved = state.extras[STATE_KEY]
    monkeypatch.setenv("SPARKRUN_FEATURE_INTEGRATION_ARENA", "0")
    env.schedule_success = True
    env.schedule.side_effect = lambda **kw: ScheduleRunResult(True, 2, 0, kw["state"], env.rows)
    assert resume_benchmark(state.benchmark_id, sctx=env.sctx).results == env.rows
    assert _state(env).extras[STATE_KEY] == saved
    env.upload.assert_not_called()
    with pytest.raises(BenchmarkFailed, match="disabled"):
        benchmark(env.options, sctx=env.sctx)


@pytest.mark.parametrize("answer,dry_run,allowed", [(True, False, True), (False, False, False), (False, True, True)])
def test_submission_validation_reports_suggestions_and_respects_prompt(answer, dry_run, allowed):
    recipe = Recipe(
        {
            "name": "Deprecated name",
            "model": "org/model",
            "runtime": "vllm-distributed",
            "container": "image",
            "command": "vllm serve org/model",
        }
    )
    emitter = Mock()
    emitter.confirm.return_value = answer
    context = BenchmarkIntegrationContext(default_sctx(), emitter, {}, dry_run=dry_run)
    if allowed:
        validate_recipe_for_submission(recipe, context=context)
    else:
        with pytest.raises(BenchmarkFailed, match="Aborted"):
            validate_recipe_for_submission(recipe, context=context)
    output = "\n".join(c.args[0] for c in emitter.info.call_args_list)
    assert "deprecated-recipe-name" in output and "restated-model-arg" in output
    if dry_run:
        emitter.confirm.assert_not_called()


def test_invalid_recipe_cannot_be_submitted():
    context = BenchmarkIntegrationContext(default_sctx(), Mock(), {})
    with pytest.raises(BenchmarkFailed, match="cannot be submitted"):
        validate_recipe_for_submission(Recipe({"runtime": "vllm-distributed", "container": "x"}), context=context)
    context.emitter.confirm.assert_not_called()


def test_dry_run_fresh_preserves_existing_state(arena_env):
    env = arena_env
    result = benchmark(env.options, sctx=env.sctx)
    state = _state(env, result.benchmark_id)
    paths = list(state.state_dir(str(env.sctx.config.cache_dir)).rglob("*"))
    before = {p: p.read_bytes() for p in paths if p.is_file()}
    benchmark(replace(env.options, dry_run=True, resume=ResumeMode.FRESH), sctx=env.sctx)
    assert {p: p.read_bytes() for p in before} == before


def test_caller_state_extras_survive_failed_run_and_resume(arena_env):
    env = arena_env
    extras = {"experiment": {"name": "author's experiment"}}
    env.schedule_success = False
    with pytest.raises(BenchmarkFailed):
        benchmark(replace(env.options, state_extras=extras), sctx=env.sctx)
    state = _state(env)
    assert state.extras["experiment"] == extras["experiment"]
    assert set(extras) == {"experiment"}
    env.schedule_success = True
    resume_benchmark(state.benchmark_id, sctx=env.sctx)
    assert _state(env).extras["experiment"] == extras["experiment"]


def test_publication_retry_auth_failure_retains_completed_result(arena_env, monkeypatch):
    from sparkrun.api import BenchmarkIntegrationFailed

    env = arena_env
    monkeypatch.setattr("sparkrun.plugins.sparkarena.integration.ARENA_BENCHMARK_PROFILE", None)
    env.options = replace(env.options, integrations={"arena": {}})
    env.upload.return_value = (False, "failed")
    with pytest.raises(BenchmarkIntegrationFailed) as initial:
        benchmark(env.options, sctx=env.sctx)
    env.auth.side_effect = RuntimeError("credentials expired")
    with pytest.raises(BenchmarkIntegrationFailed, match="credentials expired") as retry:
        resume_benchmark(initial.value.result.benchmark_id, sctx=env.sctx)
    result = retry.value.result
    assert result.success and result.resumed
    assert result.results == env.rows
    assert result.benchmark_id == initial.value.result.benchmark_id
    assert result.category == initial.value.result.category
    assert result.container_image == initial.value.result.container_image
    assert "arena" in result.integration_errors
    assert env.schedule.call_count == 1
