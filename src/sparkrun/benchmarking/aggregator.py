"""Benchmark result aggregation: merge per-task JSON files into a framework-shaped dict."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sparkrun.benchmarking.base import BenchmarkingPlugin
    from sparkrun.benchmarking.scheduler import BenchTask

logger = logging.getLogger(__name__)


def read_task_result(path: Path) -> dict[str, Any] | None:
    """Read one task artifact, returning None for missing or unusable JSON."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("aggregator: skipping %s — %s", path.name, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("aggregator: skipping %s — top-level value is not a dict", path.name)
        return None
    return data


def consolidate_results(result_files: Iterable[Path], fw: "BenchmarkingPlugin") -> dict[str, Any]:
    """Combine explicitly accepted task artifacts in the caller's order.

    The scheduler owns artifact selection; this function never scans a directory.
    Unusable files are logged and skipped. Frameworks own the result schema.
    """
    return fw.consolidate_per_task_results([data for path in result_files if (data := read_task_result(path)) is not None])


def gap_analysis(
    task_list: list["BenchTask"],
    consolidated: dict[str, Any],
    fw: "BenchmarkingPlugin",
) -> list["BenchTask"]:
    """Return tasks whose coverage key is absent from the consolidated dict.

    Coverage is defined by the framework via ``fw.task_coverage_key`` and
    ``fw.consolidated_coverage_keys``.  When a task's coverage key is ``None``
    or missing, it is also flagged as a gap (with a warning) — this preserves
    the "tasks with malformed run_args are surfaced" behavior.
    """
    observed = fw.consolidated_coverage_keys(consolidated)

    gaps: list[BenchTask] = []
    for task in task_list:
        try:
            key = fw.task_coverage_key(task)
        except Exception:  # pragma: no cover — defensive: don't abort the run on plugin bug
            logger.warning("gap_analysis: task %d (%s) coverage_key raised; treating as gap", task.index, task.label)
            gaps.append(task)
            continue

        if key is None or (isinstance(key, tuple) and any(v is None for v in key)):
            logger.warning("gap_analysis: task %d (%s) coverage key contained None", task.index, task.label)
            gaps.append(task)
            continue

        if key not in observed:
            gaps.append(task)

    return gaps
