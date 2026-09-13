"""Optional Spark Arena commands and benchmark submission integration."""

from __future__ import annotations

from sparkrun import __version__ as __version__

SPARKRUN_PLUGIN_API_VERSION = 1

FEATURE_FLAG = "integration.arena"
ARENA_BENCHMARK_PROFILE = "@official/spark-arena-v2"


def _load_command():
    from .cli import build_arena_command

    return build_arena_command()


def _load_options():
    from .cli import build_benchmark_options

    return build_benchmark_options()


def _decode_options(values):
    if not values.get("arena_flag"):
        if values.get("local_test"):
            raise ValueError("--local-test requires --arena")
        return None
    return {"local_test": values["local_test"]} if values.get("local_test") is not None else {}


def register(v):
    from sparkrun.core.benchmark_integrations import BenchmarkIntegration, register_benchmark_integration
    from sparkrun.core.cli_registry import CliOptionSpec, register_cli_command, register_cli_options
    from .integration import prepare, validate, bind, checkpoint, complete

    register_cli_command(_load_command, name="arena")
    for target in ("benchmark.run", "benchmark.resume"):
        register_cli_options(CliOptionSpec("arena", target, _load_options, _decode_options, FEATURE_FLAG))
    register_benchmark_integration(
        BenchmarkIntegration(
            "arena",
            prepare=prepare,
            validate=validate,
            on_bind=bind,
            on_checkpoint=checkpoint,
            on_complete=complete,
            feature_flag=FEATURE_FLAG,
        )
    )
