"""tool-eval-bench benchmarking framework plugin for sparkrun.

Runs `SeraphimSerapis/tool-eval-bench`_ against an OpenAI-compatible
endpoint to measure tool-call correctness across a deterministic suite of
scenarios (69 by default, plus 19 optional Hard Mode scenarios).

.. _SeraphimSerapis/tool-eval-bench: https://github.com/SeraphimSerapis/tool-eval-bench
"""

from __future__ import annotations

import csv
import io
import json
import logging
import shutil
from logging import Logger
from typing import Any

from scitrera_app_framework import Variables

from sparkrun.benchmarking.base import BenchmarkingPlugin

logger = logging.getLogger(__name__)

# Pinned upstream ref. Bump as needed; users can override per-call with
# ``-b ref=<tag-or-branch>`` (the value is consumed by the plugin and not
# forwarded to the subprocess).
_DEFAULT_REF = "v2.6.0"
_GIT_URL = "https://github.com/SeraphimSerapis/tool-eval-bench"

# CLI value shapes in v2.6.0. All other values are scalar; commas in labels,
# JSON and paths must not become additional command arguments.
_LIST_ARGS = {"scenarios", "categories"}
_CSV_ARGS = {"depth", "concurrency", "spec_prompts", "mmlu_subjects"}
_REPEAT_ARGS = {"scenario_pack"}
_HIDDEN_ARGS = {"ref", "framework_pinned_version"}
_REMOVED_ARGS = {"experimental_async", "llm_judge", "perf_legacy", "perf_legacy_only"}
_PERF_TRIGGER_ARGS = {"perf", "perf_only"}


class ToolEvalBenchFramework(BenchmarkingPlugin):
    """tool-eval-bench framework — deterministic tool-call correctness.

    Invokes the pinned tool's ``run --json-file`` command and preserves its
    versioned result envelope. Synthesizes per-scenario CSV for result export.
    """

    framework_name = "tool-eval-bench"
    categories = ("tools",)
    default_args: dict[str, Any] = {
        "ref": _DEFAULT_REF,
        "parallel": 1,
        "timeout": 120,
        "max_turns": 8,
        "temperature": 0.0,
    }
    passthrough_args: set[str] = set()

    def initialize(self, v: Variables, logger_arg: Logger) -> ToolEvalBenchFramework:
        return self

    def check_prerequisites(self) -> list[str]:
        missing: list[str] = []
        if shutil.which("uvx") is None:
            missing.append("uvx not found on PATH. Install uv: https://docs.astral.sh/uv/getting-started/installation/")
        return missing

    def apply_session_warmup_state(self, run_args: dict[str, Any], *, is_first_task: bool) -> dict[str, Any]:
        """tool-eval-bench: warmup + coherence checks run once per session on
        the first task; subsequent tasks suppress them via ``no_warmup`` /
        ``skip_coherence``.
        """
        out = dict(run_args)
        if not is_first_task:
            out.setdefault("no_warmup", True)
            out.setdefault("skip_coherence", True)
        return out

    def build_benchmark_command(
        self,
        target_url: str,
        model: str,
        args: dict[str, Any],
        result_file: str | None = None,
    ) -> list[str]:
        """Assemble the ``uvx tool-eval-bench`` argv.

        ``--json-file`` produces the scheduler-owned artifact. Standalone
        command construction without a result file uses JSON on stdout.
        """
        removed = _REMOVED_ARGS.intersection(args)
        if removed:
            raise ValueError("tool-eval-bench v2.6 removed arguments: %s" % ", ".join(sorted(removed)))
        ref = args.get("framework_pinned_version") or args.get("ref") or _DEFAULT_REF
        wants_perf = any(args.get(k) for k in _PERF_TRIGGER_ARGS)
        if wants_perf:
            from_spec = "tool-eval-bench[perf] @ git+%s@%s" % (_GIT_URL, ref)
        else:
            from_spec = "git+%s@%s" % (_GIT_URL, ref)

        cmd = [
            "uvx",
            "--from",
            from_spec,
            "tool-eval-bench",
            "run",
            "--base-url",
            target_url,
            "--model",
            model,
            "--json",
        ]

        if result_file:
            cmd.extend(["--json-file", result_file])

        for key, value in args.items():
            if key in _HIDDEN_ARGS or key in {"base_url", "model", "json", "json_file"} or value is None:
                continue
            flag = "--" + key.replace("_", "-")
            if isinstance(value, bool):
                if value:
                    cmd.append(flag)
            elif key in _REPEAT_ARGS:
                for item in value if isinstance(value, list) else [value]:
                    cmd.extend([flag, str(item)])
            elif isinstance(value, list):
                if key in _LIST_ARGS:
                    cmd.extend([flag, *(str(item) for item in value)])
                elif key in _CSV_ARGS:
                    cmd.extend([flag, ",".join(str(item) for item in value)])
                else:
                    raise ValueError("tool-eval-bench argument %r requires a scalar value" % key)
            elif key == "backend_kwargs" and isinstance(value, dict):
                cmd.extend([flag, json.dumps(value, separators=(",", ":"))])
            else:
                cmd.extend([flag, str(value)])

        return cmd

    def build_task_list(self, base_args, schedule):
        """Run the complete tool suite as one resumable invocation."""
        from copy import deepcopy
        from sparkrun.benchmarking.scheduler import BenchTask
        from sparkrun.core.benchmark_profiles import BenchmarkError

        entries = [{}] if schedule is None else schedule
        if len(entries) != 1 or not isinstance(entries[0], dict):
            raise BenchmarkError("tool-eval-bench requires one schedule entry for its complete suite")
        entry = deepcopy(entries[0])
        return [BenchTask(0, "tool evaluation suite", {**deepcopy(base_args), **deepcopy(entry)}, entry)]

    def consolidate_per_task_results(self, per_task_jsons):
        """Keep the tool's original JSON schema for the single suite artifact."""
        if len(per_task_jsons) > 1:
            raise ValueError("tool-eval-bench expects one suite artifact")
        return per_task_jsons[0] if per_task_jsons else {}

    def interpret_arg(self, key: str, value: str) -> Any:
        from sparkrun.utils import coerce_value

        if key in _LIST_ARGS | _CSV_ARGS | _REPEAT_ARGS:
            return [coerce_value(v.strip()) for v in value.split(",")]
        return coerce_value(value)

    def estimate_test_count(self, args: dict[str, Any]) -> int | None:
        scenarios = args.get("scenarios")
        if isinstance(scenarios, list) and scenarios:
            return len(scenarios)
        if args.get("scenario_pack") or args.get("categories"):
            return None
        if args.get("hardmode_only"):
            return 19
        if args.get("short"):
            return 34 if args.get("hardmode") else 15
        if args.get("hardmode"):
            return 88
        return 69

    def parse_results(
        self,
        stdout: str,
        stderr: str,
        result_file: str | None = None,
    ) -> dict[str, Any]:
        from pathlib import Path

        json_data: dict[str, Any] = {}
        text = Path(result_file).read_text() if result_file else (stdout or "").strip()
        if text:
            try:
                json_data = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Failed to parse tool-eval-bench JSON output")

        if not isinstance(json_data, dict):
            raise ValueError("tool-eval-bench result must be a JSON object")
        schema = json_data.get("schema_version")
        if schema is not None and schema != "1":
            raise ValueError("Unsupported tool-eval-bench result schema: %r" % schema)
        scores = json_data.get("scores", {})
        scenario_results = scores.get("scenario_results", []) if isinstance(scores, dict) else []
        csv_text = scenario_results_to_csv(scenario_results) if scenario_results else ""

        return {
            "json": json_data,
            "csv": csv_text,
            "stdout": stdout,
        }


_CSV_HEADERS = [
    "scenario_id",
    "status",
    "points",
    "duration_seconds",
    "ttft_ms",
    "turn_count",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "summary",
    "failure_kind",
    "turn_budget_exceeded",
]


def scenario_results_to_csv(scenario_results: list[dict[str, Any]]) -> str:
    """Render tool-eval-bench scenario results as CSV.

    Optional fields (``ttft_ms``, token counts) emit as empty strings when
    absent, mirroring the convention used by ``llama_benchy.json_to_csv``.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_HEADERS, extrasaction="ignore")
    writer.writeheader()
    for r in scenario_results:
        row = {h: r.get(h, "") for h in _CSV_HEADERS}
        # Strip newlines so each scenario is one CSV row in grep-style consumers.
        if isinstance(row.get("summary"), str):
            row["summary"] = row["summary"].replace("\n", " ").strip()
        writer.writerow(row)
    return buf.getvalue()
