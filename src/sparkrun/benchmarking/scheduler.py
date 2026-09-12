"""Benchmark scheduling: per-task abstraction and the executor loop.

Frameworks that opt into batched/scheduled execution build a list of
:class:`BenchTask` instances via
:meth:`sparkrun.benchmarking.base.BenchmarkingPlugin.build_task_list`. The
scheduler then dispatches each task as a single benchmark subprocess,
persists per-task JSON, and updates :class:`BenchmarkRunState` so the run
can be resumed after a crash.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from sparkrun.benchmarking._credentials import BenchmarkCredentials
from sparkrun.benchmarking._process import run_benchmark_process
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sparkrun.benchmarking.aggregator import consolidate_results, gap_analysis, read_task_result
from sparkrun.benchmarking.run_state import BenchmarkRunState

if TYPE_CHECKING:
    from sparkrun.benchmarking.base import BenchmarkingPlugin
    from sparkrun.benchmarking.progress import BenchmarkProgress

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BenchTask:
    """One scheduled benchmark invocation.

    Attributes:
        index: 0-based position in the schedule. Stable across resumes.
        label: Short human description for the progress UI (e.g. ``"d=4096 c=2"``).
        run_args: Framework-specific args dict; opaque to the scheduler.
        schedule_entry: Raw per-task override dict from the YAML schedule
            (or the auto-generated default). Persisted in run state so the
            schedule can be reconstructed on resume.
    """

    index: int
    label: str
    run_args: dict[str, Any] = field(default_factory=dict)
    schedule_entry: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScheduleRunResult:
    """Result returned by :func:`run_schedule`."""

    success: bool
    completed_count: int
    failed_count: int
    state: BenchmarkRunState
    consolidated: dict[str, Any]


def run_schedule(
    fw: "BenchmarkingPlugin",
    tasks: list[BenchTask],
    state: BenchmarkRunState,
    *,
    target_url: str,
    model: str,
    timeout: int | None,
    progress_ui: "BenchmarkProgress",
    cache_dir: str | None = None,
    exit_on_first_fail: bool = False,
    skip_run: bool = False,
    credentials: BenchmarkCredentials | None = None,
) -> ScheduleRunResult:
    """Iterate pending tasks. Returns when the schedule is complete or aborts.

    Args:
        fw: The benchmarking plugin that can construct subprocess commands.
        tasks: Ordered list of :class:`BenchTask` instances to execute.
        state: Resumable run state — mutated and saved throughout execution.
        target_url: Inference endpoint URL forwarded to the benchmark command.
        model: Model name forwarded to the benchmark command.
        timeout: Per-task subprocess timeout in seconds, or ``None`` for no limit.
        credentials: Ephemeral authentication injected only into command construction.
        progress_ui: Task event sink (the keyword is retained for compatibility).
        cache_dir: Override for the sparkrun cache directory root.
        exit_on_first_fail: Stop immediately after the first task failure.
            Otherwise attempt the remaining tasks, skipping failures for this
            invocation. A later resume can retry those failed tasks.
        skip_run: When ``True``, the warmup/coherence steps are suppressed even
            for the first task of the session.

    Returns:
        :class:`ScheduleRunResult` describing the outcome.
    """
    credentials = credentials or BenchmarkCredentials()
    total = len(tasks)
    result_files = [state.runs_dir(cache_dir) / ("%03d%s.json" % (idx, fw.result_filename_suffix(task))) for idx, task in enumerate(tasks)]

    def _consolidate() -> dict[str, Any]:
        return consolidate_results((path for idx, path in enumerate(result_files) if idx in state.completed_indices), fw)

    # Saved successes need usable artifacts too. Failed/interrupted artifacts
    # and unrelated files in runs/ never contribute to measurement coverage.
    for idx in tuple(state.completed_indices):
        if read_task_result(result_files[idx]) is None:
            state.mark_failed(idx, "missing or invalid result artifact")
    consolidated = _consolidate()

    # Session bookkeeping — mark this execution session.
    state.mark_session_started()
    state.save(cache_dir)

    failed_this_run: set[int] = set()
    session_first_task = True

    def _do_loop() -> tuple[bool, bool]:
        """Inner loop over pending tasks.

        Returns:
            (aborted, exit_requested) — aborted=True means we should stop immediately.
        """
        nonlocal session_first_task, consolidated

        while True:
            idx = state.next_pending(total, exclude=failed_this_run)
            if idx is None:
                break

            task = tasks[idx]
            progress_ui.start_task(idx, task.label)

            # Build per-task args, applying framework's warmup/coherence rule.
            run_args = fw.apply_session_warmup_state(task.run_args, is_first_task=session_first_task)

            # Pin framework version to whatever was resolved on the first run
            # of this benchmark, so resumes don't drift onto a newer release.
            pinned_version = state.extras.get("framework_version")
            if pinned_version:
                run_args["framework_pinned_version"] = pinned_version

            result_file: Path = result_files[idx]
            log_file = result_file.with_suffix(".log")
            state.mark_started(idx)
            state.save(cache_dir)
            # Invalidate before command construction: builders may inspect or
            # produce the target file, and an interrupted retry must stay pending.
            result_file.unlink(missing_ok=True)
            cmd = fw.build_benchmark_command(target_url, model, credentials.arguments(run_args), result_file=str(result_file))

            t_start = time.monotonic()
            try:
                log_fh = open(log_file, "w")
                try:

                    def output(line, sink=log_fh):
                        sink.write(line)
                        sink.flush()

                    rc = run_benchmark_process(cmd, timeout=timeout, credentials=credentials, stdout=output)
                except subprocess.TimeoutExpired:
                    logger.warning("Task %d (%s) timed out after %s seconds", idx, task.label, timeout)
                    duration_s = time.monotonic() - t_start
                    failed_this_run.add(idx)
                    state.mark_failed(idx, "timeout after %ds" % timeout)
                    state.save(cache_dir)
                    progress_ui.end_task(idx, success=False, duration_s=duration_s)
                    if exit_on_first_fail:
                        state.mark_session_ended("partial")
                        state.save(cache_dir)
                        return True, False
                    continue
                finally:
                    log_fh.close()

                duration_s = time.monotonic() - t_start
                if rc == 0 and read_task_result(result_file) is not None:
                    state.mark_completed(idx)
                    state.save(cache_dir)
                    progress_ui.end_task(idx, success=True, duration_s=duration_s)
                    consolidated = _consolidate()
                    progress_ui.update_results_table(consolidated)
                    session_first_task = False
                else:
                    failed_this_run.add(idx)
                    state.mark_failed(idx, "exit code %d" % rc if rc else "missing or invalid result artifact")
                    state.save(cache_dir)
                    progress_ui.end_task(idx, success=False, duration_s=duration_s)
                    if exit_on_first_fail:
                        state.mark_session_ended("partial")
                        state.save(cache_dir)
                        return True, False

            except KeyboardInterrupt:
                return False, True  # signal KeyboardInterrupt to caller

        return False, False

    try:
        aborted, interrupted = _do_loop()

        if interrupted:
            state.mark_session_ended("interrupted")
            state.save(cache_dir)
            raise KeyboardInterrupt

        if aborted:
            # exit_on_first_fail already called mark_session_ended("partial") inside loop.
            return ScheduleRunResult(
                success=False,
                completed_count=len(state.completed_indices),
                failed_count=len(state.failed_indices),
                state=state,
                consolidated=consolidated,
            )

        # Post-loop gap analysis — done at most once.
        gaps = gap_analysis(tasks, consolidated, fw)
        if gaps:
            progress_ui.log("Found %d gap(s); re-queueing" % len(gaps))
            for gap_task in gaps:
                if gap_task.index in state.completed_indices:
                    state.completed_indices.remove(gap_task.index)
            state.save(cache_dir)
            # Re-enter the loop for gap tasks.
            aborted, interrupted = _do_loop()
            if interrupted:
                state.mark_session_ended("interrupted")
                state.save(cache_dir)
                raise KeyboardInterrupt
            if aborted:
                return ScheduleRunResult(
                    success=False,
                    completed_count=len(state.completed_indices),
                    failed_count=len(state.failed_indices),
                    state=state,
                    consolidated=consolidated,
                )

        # A bounded gap pass may still leave successful commands without the
        # requested measurements. Persist those gaps as failures for resume.
        consolidated = _consolidate()
        for task in gap_analysis(tasks, consolidated, fw):
            if task.index in state.completed_indices:
                state.mark_failed(task.index, "missing measurement coverage")
        consolidated = _consolidate()
        progress_ui.update_results_table(consolidated)

        if state.is_complete(total):
            state.mark_session_ended("completed")
        else:
            state.mark_session_ended("partial")
        state.save(cache_dir)

        return ScheduleRunResult(
            success=state.is_complete(total),
            completed_count=len(state.completed_indices),
            failed_count=len(state.failed_indices),
            state=state,
            consolidated=consolidated,
        )

    except KeyboardInterrupt:
        # Already handled mark_session_ended("interrupted") above; just re-raise.
        raise

    except Exception:
        logger.exception("Unexpected error in run_schedule for benchmark %s", state.benchmark_id)
        state.mark_crash()
        state.mark_session_ended("crashed")
        state.save(cache_dir)
        return ScheduleRunResult(
            success=False,
            completed_count=len(state.completed_indices),
            failed_count=len(state.failed_indices),
            state=state,
            consolidated=consolidated,
        )
