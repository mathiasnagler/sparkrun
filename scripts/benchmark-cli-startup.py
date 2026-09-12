#!/usr/bin/env python3
"""Compare fresh CLI processes against a source snapshot, without modifying installs.

Run with the core development environment:
  .venv/bin/python scripts/benchmark-cli-startup.py

Samples are interleaved, with warm OS/bytecode caches. Each timed sample starts a
new interpreter, including for Click's real Bash completion protocol. No inference,
network-backed completion, or shell startup time is measured.
"""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--baseline", default="HEAD")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("/tmp/application-profile-cli-benchmark.json"))
    args = parser.parse_args()
    if args.samples < 2 or args.warmups < 1:
        parser.error("at least two samples and one warmup are required")
    python = str(Path(args.python).absolute())
    baseline = subprocess.check_output(["git", "rev-parse", args.baseline], cwd=ROOT, text=True).strip()
    archive = subprocess.check_output(["git", "archive", baseline, "src"], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="application-profile-cli-benchmark-") as temp:
        work = Path(temp)
        snapshot = work / "baseline"
        snapshot.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(snapshot, filter="data")
        # Call the same public console functions as the installed entry points.
        variants = {
            "baseline-sparkrun": (snapshot / "src", "sparkrun", "from sparkrun.cli import main; main(prog_name='sparkrun')"),
            "current-sparkrun": (ROOT / "src", "sparkrun", "from sparkrun.application import main; main()"),
            "alternate-profile": (
                ROOT / "src",
                "profile-test-app",
                "from sparkrun.application import run_cli; "
                "from sparkrun.core.application_profile import ApplicationProfile; "
                "run_cli(ApplicationProfile(id='profile-test-app', display_name='Profile test application', "
                "command='profile-test-app', package='profile-test-app', registries=(), bootstrap_registry_urls=(), "
                "feature_defaults={'cli.tune': False, 'integration.arena': False, 'integration.k8s': False}))",
            ),
            "alternate-next": (
                ROOT / "src",
                "profile-test-app",
                "from sparkrun.application import run_cli; "
                "from sparkrun.core.application_profile import ApplicationProfile, UpdateSource; "
                "run_cli(ApplicationProfile(id='profile-test-app', display_name='Profile test application', "
                "command='profile-test-app', package='profile-test-app', registries=(), bootstrap_registry_urls=(), "
                "default_channel='next', update_sources={'stable': UpdateSource('profile-test-app'), "
                "'next': UpdateSource('profile-test-app')}, "
                "feature_defaults={'cli.tune': False, 'integration.arena': False, 'integration.k8s': False}, "
                "feature_channel_defaults={'next': {'cli.tune': False}}))",
            ),
        }
        scenarios = {
            "help": (["--help"], None, "Usage:"),
            "root-completion": ([], ["r"], "plain,recipe"),
            "nested-completion": ([], ["recipe", "se"], "plain,search"),
            "option-completion": ([], ["benchmark", "run", "--pro"], "plain,--profile"),
        }
        environments = {}
        for variant, (source, _command, _code) in variants.items():
            home = work / variant
            for namespace in ("sparkrun", "profile-test-app"):
                config = home / ".config" / namespace
                config.mkdir(parents=True)
                (config / "config.yaml").write_text("{}\n")
                (config / "registries.yaml").write_text("config_version: 1\nregistries: []\n")
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("SPARKRUN_", "PROFILE_TEST_APP_", "_SPARKRUN_", "_PROFILE_TEST_APP_", "COMP_", "SAF_", "XDG_"))
                and key not in {"STATEFUL_ROOT", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "APP_NAME", "RUN_ID"}
            }
            env.update(
                HOME=str(home),
                PYTHONPATH=str(source),
                SPARKRUN_NO_TELEMETRY="1",
                SPARKRUN_NO_EXTERNAL_PLUGINS="1",
                SPARKRUN_NO_INSTALLED_PLUGINS="1",
                PYTHONHASHSEED="0",
                LC_ALL="C.UTF-8",
            )
            environments[variant] = env

        def run(variant, scenario):
            _source, command, code = variants[variant]
            argv, words, expected = scenarios[scenario]
            env = environments[variant].copy()
            if words is not None:
                env.update(
                    {
                        f"_{command.upper().replace('-', '_')}_COMPLETE": "bash_complete",
                        "COMP_WORDS": " ".join([command, *words]),
                        "COMP_CWORD": str(len(words)),
                    }
                )
            start = time.perf_counter_ns()
            result = subprocess.run([python, "-c", code, *argv], cwd=work, env=env, capture_output=True, text=True, timeout=30)
            elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
            if result.returncode or expected not in result.stdout:
                raise RuntimeError(f"{variant}/{scenario}: {result.returncode}\n{result.stdout}\n{result.stderr}")
            return elapsed_ms

        pairs = [(variant, scenario) for variant in variants for scenario in scenarios]
        for _ in range(args.warmups):
            for pair in pairs:
                run(*pair)
        samples = {pair: [] for pair in pairs}
        rng = random.Random(42)
        for iteration in range(args.samples):
            rng.shuffle(pairs)
            for pair in pairs:
                samples[pair].append(run(*pair))
            if (iteration + 1) % 5 == 0:
                print(f"Finished {iteration + 1}/{args.samples} rounds", flush=True)

        rows = []
        for scenario in scenarios:
            baseline_median = statistics.median(samples["baseline-sparkrun", scenario])
            for variant in variants:
                values = samples[variant, scenario]
                median = statistics.median(values)
                rows.append(
                    dict(
                        variant=variant,
                        scenario=scenario,
                        median_ms=median,
                        p95_ms=statistics.quantiles(values, n=100, method="inclusive")[94],
                        delta_ms=median - baseline_median,
                        delta_percent=100 * (median / baseline_median - 1),
                        samples_ms=values,
                    )
                )
        report = dict(
            baseline=baseline,
            current_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            current_has_worktree_changes=bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
            python=subprocess.check_output([python, "--version"], text=True).strip(),
            platform=platform.platform(),
            samples=args.samples,
            warmups=args.warmups,
            rows=rows,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        for row in rows:
            print(
                f"{row['scenario']:20} {row['variant']:18} median={row['median_ms']:7.2f} ms p95={row['p95_ms']:7.2f} ms delta={row['delta_ms']:+6.2f} ms ({row['delta_percent']:+5.1f}%)"
            )
        print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
