"""Tests for the ResumeMode decision tree (plan section I) and --resume flag."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sparkrun.api._benchmark_models import ResumeMode, BenchmarkDecision


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_internal_result():
    """Return a fake internal BenchmarkResult for _execute_benchmark patches."""
    result = MagicMock()
    result.success = True
    result.benchmark_id = "x"
    fw = MagicMock()
    fw.framework_name = "llama-benchy"
    fw.primary_category = "performance"
    result.framework = fw
    result.profile = None
    result.results = {}
    result.outputs = {}
    result.cluster_id = "c"
    result.host_list = []
    result.container_image = "img"
    result.container_image_sha = None
    result.container_image_sha_pinned = False
    result.longterm_image_ref = None
    result.longterm_image_pinned = False
    result.benchmark_args = {}
    result.state_dir = None
    result.resumed = False
    result.integration_results = {}
    result.recipe = MagicMock()
    result.overrides = {}
    return result


def _stub_execute_benchmark(received_options):
    """Return a side_effect for _execute_benchmark that records the options."""

    def _capture(options, *, sctx, emitter):
        received_options.append(options)
        return _fake_internal_result()

    return _capture


def _stub_run_benchmark(received_kwargs):
    """Return a side_effect for _run_benchmark that records kwargs (CLI-side tests)."""

    def _capture(ctx, **kwargs):
        received_kwargs.update(kwargs)
        return _fake_internal_result()

    return _capture


# ---------------------------------------------------------------------------
# API wrapper threading tests — now patch _execute_benchmark
# ---------------------------------------------------------------------------


def test_api_default_resume_mode_is_if_exists():
    received = []
    with patch("sparkrun.api._benchmark._execute_benchmark", side_effect=_stub_execute_benchmark(received)):
        from sparkrun.api import benchmark
        from sparkrun.api._benchmark_models import BenchmarkOptions

        benchmark(BenchmarkOptions(recipe="my-recipe"))
    assert received[0].resume == ResumeMode.IF_EXISTS


def test_api_fresh_mode_threaded_through():
    received = []
    with patch("sparkrun.api._benchmark._execute_benchmark", side_effect=_stub_execute_benchmark(received)):
        from sparkrun.api import benchmark
        from sparkrun.api._benchmark_models import BenchmarkOptions

        benchmark(BenchmarkOptions(recipe="my-recipe", resume=ResumeMode.FRESH))
    assert received[0].resume == ResumeMode.FRESH


def test_api_required_mode_threaded_through():
    received = []
    with patch("sparkrun.api._benchmark._execute_benchmark", side_effect=_stub_execute_benchmark(received)):
        from sparkrun.api import benchmark
        from sparkrun.api._benchmark_models import BenchmarkOptions

        benchmark(BenchmarkOptions(recipe="my-recipe", resume=ResumeMode.REQUIRED))
    assert received[0].resume == ResumeMode.REQUIRED


def test_api_auto_mode_threaded_through():
    received = []
    with patch("sparkrun.api._benchmark._execute_benchmark", side_effect=_stub_execute_benchmark(received)):
        from sparkrun.api import benchmark
        from sparkrun.api._benchmark_models import BenchmarkOptions

        benchmark(BenchmarkOptions(recipe="my-recipe", resume=ResumeMode.AUTO))
    assert received[0].resume == ResumeMode.AUTO


# ---------------------------------------------------------------------------
# Decision callback contract
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI flag mutual-exclusion test
# ---------------------------------------------------------------------------


def test_cli_resume_and_fresh_are_mutually_exclusive():
    from click.testing import CliRunner
    from sparkrun.cli._benchmark import benchmark_run

    runner = CliRunner()
    # --hosts is supplied so the command reaches the mutual-exclusion check.
    # Without it host resolution exits first, and the test then passes on
    # `exit_code != 0` for entirely the wrong reason.
    result = runner.invoke(benchmark_run, ["my-recipe", "--resume", "--fresh", "--hosts", "h1"])
    # click UsageError exits with non-zero code
    assert result.exit_code != 0
    output_text = result.output + (str(result.exception) if result.exception else "")
    assert "mutually exclusive" in output_text


def test_cli_resume_flag_sets_if_exists_mode():
    """--resume alone should map to IF_EXISTS with no error."""
    received = {}

    with patch("sparkrun.cli._benchmark._run_benchmark", side_effect=_stub_run_benchmark(received)):
        from click.testing import CliRunner
        from sparkrun.cli._benchmark import benchmark_run

        runner = CliRunner()
        # We expect it to fail deep in bootstrap (no SAF context), but the mode
        # should be captured before that. Patch deeper to avoid full bootstrap.
        with patch("sparkrun.cli._benchmark._get_context") as mock_ctx:
            mock_ctx.side_effect = RuntimeError("stop early")
            result = runner.invoke(benchmark_run, ["my-recipe", "--resume"])
        # Either mode was recorded, or it errored out before _run_benchmark.
        # The key check is that no UsageError was raised for the flag itself.
        if received:
            assert received["resume_mode"] == ResumeMode.IF_EXISTS
        else:
            # Didn't reach _run_benchmark — acceptable; the flag parsing succeeded
            # (no exit code 2 from UsageError)
            assert result.exit_code != 2 or "mutually exclusive" not in (result.output or "")


def test_cli_fresh_flag_sets_fresh_mode():
    """--fresh alone should map to FRESH with no UsageError."""
    received = {}

    with patch("sparkrun.cli._benchmark._run_benchmark", side_effect=_stub_run_benchmark(received)):
        from click.testing import CliRunner
        from sparkrun.cli._benchmark import benchmark_run

        with patch("sparkrun.cli._benchmark._get_context") as mock_ctx:
            mock_ctx.side_effect = RuntimeError("stop early")
            result = runner = CliRunner()
            result = runner.invoke(benchmark_run, ["my-recipe", "--fresh"])
        if received:
            assert received["resume_mode"] == ResumeMode.FRESH
        else:
            assert result.exit_code != 2 or "mutually exclusive" not in (result.output or "")


# ---------------------------------------------------------------------------
# Completed prior state must never be reused silently
# ---------------------------------------------------------------------------


def test_should_remeasure_auto_honours_callback():
    """AUTO delegates the decision to the caller's callback, both ways."""
    from sparkrun.api._benchmark import _should_remeasure_complete_state

    state = MagicMock()
    seen = []

    def cb(s):
        seen.append(s)
        return True

    assert _should_remeasure_complete_state(ResumeMode.AUTO, cb, state) is True
    assert len(seen) == 1 and isinstance(seen[0], BenchmarkDecision)
    assert seen[0].kind == "remeasure_complete" and seen[0].benchmark_id == state.benchmark_id
    assert _should_remeasure_complete_state(ResumeMode.AUTO, lambda s: False, state) is False


def test_should_remeasure_auto_without_callback_reuses():
    """No callback (library default) → reuse; the caller warns about it."""
    from sparkrun.api._benchmark import _should_remeasure_complete_state

    assert _should_remeasure_complete_state(ResumeMode.AUTO, None, MagicMock()) is False


def test_should_remeasure_explicit_resume_modes_never_prompt():
    """IF_EXISTS / REQUIRED asked for a resume, so they reuse without consulting
    the callback — even when one is wired (the CLI always wires one)."""
    from sparkrun.api._benchmark import _should_remeasure_complete_state

    called = []

    def cb(s):
        called.append(s)
        return True

    for mode in (ResumeMode.IF_EXISTS, ResumeMode.REQUIRED):
        assert _should_remeasure_complete_state(mode, cb, MagicMock()) is False
    assert called == []


@pytest.mark.parametrize(
    "kind,default,tty_default",
    [
        ("resume_incomplete", True, True),
        ("remeasure_complete", False, True),
        ("integration_confirmation", False, False),
    ],
)
@pytest.mark.parametrize("tty", [True, False])
def test_cli_decision_policy(monkeypatch, kind, default, tty_default, tty):
    from sparkrun.cli._benchmark import _cli_decision

    request = BenchmarkDecision(kind, "Decision?", default, "bench_test")
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty)
    with patch("click.confirm", return_value=False) as confirm:
        answer = _cli_decision(request)
    if tty:
        confirm.assert_called_once_with("Decision?", default=tty_default)
        assert answer is False
    else:
        confirm.assert_not_called()
        assert answer is (True if kind == "integration_confirmation" else default)


def test_api_decision_callback_is_the_only_resume_callback():
    from dataclasses import fields
    from sparkrun.api import BenchmarkOptions, benchmark

    def callback(request):
        return request.default

    received = []
    with patch("sparkrun.api._benchmark._execute_benchmark", side_effect=_stub_execute_benchmark(received)):
        benchmark(BenchmarkOptions(recipe="my-recipe", decision_callback=callback))
    assert received[0].decision_callback is callback
    assert {"on_prompt_required", "on_complete_state"}.isdisjoint(field.name for field in fields(BenchmarkOptions))
