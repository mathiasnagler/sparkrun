"""Which name a benchmark framework asks the endpoint for (issue #298).

``recipe.model`` identifies the *weights*; the served name is what the endpoint
answers to.  sparkrun handed ``recipe.model`` to every framework, which is
right for llama-benchy (its ``--model`` is the tokenizer identity and the
request name rides a separate ``--served-model-name``, issue #257) and wrong
for tool-eval-bench, whose ``--model`` *is* the request name — so a recipe
declaring ``defaults.served_model_name`` 404'd every scenario while the suite
still exited 0.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import textwrap
from unittest.mock import Mock

import pytest
import yaml

from sparkrun.api import benchmark, resume_benchmark
from sparkrun.benchmarking.base import resolve_request_model
from sparkrun.benchmarking.llama_benchy import LlamaBenchyFramework
from sparkrun.benchmarking.run_state import BenchmarkRunState
from sparkrun.benchmarking.tool_eval_bench import ToolEvalBenchFramework
from sparkrun.core.recipe import Recipe
from test_benchmark_api_contract import scheduled_env as scheduled_env
from test_benchmark_startup_collection import bench_env as bench_env

MODEL_ID = "Mia-AiLab/GLM-5.3-Flash-EXL3-TR3-4bpw"
ALIAS = "GLM-5.3-Flash-EXL3"


def _recipe(defaults_block: str = "", command_tail: str = "", model: str = MODEL_ID) -> Recipe:
    text = textwrap.dedent(
        """
        recipe_version: "2"
        model: %s
        runtime: vllm
        container: example/img:latest
        defaults:
          port: 8000
        %s
        command: |
          vllm serve {model} --port {port}%s
        """
    ) % (model, defaults_block, command_tail)
    return Recipe.from_dict(yaml.safe_load(text))


def _request_model(fw, recipe, overrides=None):
    return resolve_request_model(fw, recipe, recipe.build_config_chain(overrides))


# ---------------------------------------------------------------------------
# resolve_request_model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "recipe_kwargs",
    [
        {"defaults_block": "  served_model_name: %s" % ALIAS},
        # Hardcoded in the template, invisible to the config chain.
        {"command_tail": " --served-model-name %s" % ALIAS},
    ],
    ids=["declared", "from-command"],
)
def test_request_name_framework_gets_the_served_name(recipe_kwargs):
    assert _request_model(ToolEvalBenchFramework(), _recipe(**recipe_kwargs)) == ALIAS


def test_cli_override_wins_over_the_recipe():
    recipe = _recipe(defaults_block="  served_model_name: %s" % ALIAS)
    assert _request_model(ToolEvalBenchFramework(), recipe, {"served_model_name": "other"}) == "other"


def test_model_id_is_the_fallback_when_no_alias_exists():
    """The two coincide for every recipe without an alias — which is why the
    served name is a safe *default* rather than a per-framework opt-in."""
    assert _request_model(ToolEvalBenchFramework(), _recipe()) == MODEL_ID


def test_llama_benchy_still_gets_the_model_id():
    """Its ``--model`` loads a tokenizer; handing it an alias would break the
    framework that already handles the served name correctly."""
    recipe = _recipe(defaults_block="  served_model_name: %s" % ALIAS)
    assert _request_model(LlamaBenchyFramework(), recipe) == MODEL_ID


def test_gguf_quant_suffix_stripped_from_the_model_id_fallback():
    """``repo:Q4_K_M`` is a sparkrun spelling; no server answers to it."""
    recipe = _recipe(model="org/model-GGUF:Q4_K_M")
    assert _request_model(ToolEvalBenchFramework(), recipe) == "org/model-GGUF"


def test_declared_alias_containing_a_colon_is_never_split():
    """Only the model-id fallback carries the quant suffix; a declared name is
    free-form and ``qwen3:8b`` is a real alias."""
    recipe = _recipe(defaults_block="  served_model_name: qwen3:8b")
    assert _request_model(ToolEvalBenchFramework(), recipe) == "qwen3:8b"


def test_unaware_framework_defaults_to_the_served_name():
    """A plugin written against an older base class (or an out-of-tree one that
    never heard of the flag) talks to an OpenAI-compatible endpoint, so the
    request name is the answer that fails toward working requests."""

    class _Bare:
        pass

    assert _request_model(_Bare(), _recipe(defaults_block="  served_model_name: %s" % ALIAS)) == ALIAS


# ---------------------------------------------------------------------------
# tool-eval-bench command construction
# ---------------------------------------------------------------------------


def test_tool_eval_bench_sends_the_name_it_is_given():
    cmd = ToolEvalBenchFramework().build_benchmark_command("http://h:8000/v1", ALIAS, {})
    assert cmd[cmd.index("--model") + 1] == ALIAS


def test_explicit_model_arg_overrides_the_resolved_name():
    """``-b model=…`` was silently dropped — the flag looked accepted while
    every scenario asked for something else."""
    cmd = ToolEvalBenchFramework().build_benchmark_command("http://h:8000/v1", MODEL_ID, {"model": ALIAS})
    assert cmd[cmd.index("--model") + 1] == ALIAS
    # ...and it is not also emitted a second time by the generic arg loop.
    assert cmd.count("--model") == 1


# ---------------------------------------------------------------------------
# End to end, through the benchmark API
# ---------------------------------------------------------------------------


@pytest.fixture
def aliased_env(scheduled_env, monkeypatch):
    """``scheduled_env`` with a served-model alias and the real tool framework."""
    env = scheduled_env
    env.recipe.defaults["served_model_name"] = ALIAS
    env.recipe.model = MODEL_ID
    fw = ToolEvalBenchFramework()
    monkeypatch.setattr("sparkrun.core.bootstrap.get_benchmarking_framework", lambda *a, **kw: fw)
    monkeypatch.setattr(fw, "check_prerequisites", lambda: [])
    payload = {
        "schema_version": "1",
        "scores": {"max_points": 2, "scenario_results": [{"scenario_id": "TC-01", "status": "pass"}]},
    }
    commands = []
    build = fw.build_benchmark_command

    def command(target_url, model, args, result_file):
        commands.append(build(target_url, model, args, result_file))
        return [sys.executable, "-c", "from pathlib import Path; Path(%r).write_text(%r)" % (result_file, json.dumps(payload))]

    monkeypatch.setattr(fw, "build_benchmark_command", Mock(side_effect=command))
    env.fw_real = fw
    env.commands = commands
    env.options = replace(env.options, framework=fw.framework_name)
    return env


def test_tool_suite_requests_the_served_name(aliased_env):
    """The reported failure: 15/15 scenarios returned ``[server error 404]``
    against a healthy deployment, and the run reported success."""
    result = benchmark(aliased_env.options, sctx=aliased_env.sctx)
    assert result.success
    cmd = aliased_env.commands[0]
    assert cmd[cmd.index("--model") + 1] == ALIAS
    assert MODEL_ID not in cmd


def test_resume_keeps_asking_for_the_served_name(aliased_env, monkeypatch):
    env = aliased_env
    benchmark(env.options, sctx=env.sctx)
    state_path = next(env.sctx.config.cache_dir.glob("benchmarks/bench_*/state.yaml"))
    state = BenchmarkRunState.load(state_path.parent.name, str(env.sctx.config.cache_dir))
    # Force the recorded schedule to run again rather than replaying artifacts.
    state.completed_indices = []
    state.extras.pop("measurement_complete", None)
    state.save(str(env.sctx.config.cache_dir))
    env.commands.clear()
    resume_benchmark(state.benchmark_id, sctx=env.sctx)
    assert env.commands, "resume did not rebuild the command"
    cmd = env.commands[0]
    assert cmd[cmd.index("--model") + 1] == ALIAS


def test_banner_names_the_served_name(aliased_env, capsys):
    """A suite of 404s reads as a model-quality result unless the summary says
    what the requests actually carry."""
    events = []
    benchmark(replace(aliased_env.options, progress_callback=events.append), sctx=aliased_env.sctx)
    capsys.readouterr()
    text = "\n".join(str(getattr(e, "message", e)) for e in events)
    assert ALIAS in text and MODEL_ID in text


def test_llama_benchy_command_is_unchanged():
    """``--model`` stays the repo id (tokenization) and the alias rides
    ``--served-model-name`` — the issue #257 arrangement, left intact.

    Composed from both seams the common path uses: ``resolve_request_model``
    for the positional and ``prepare_benchmark_args`` for the injected flag.
    """
    fw = LlamaBenchyFramework()
    recipe = _recipe(defaults_block="  served_model_name: %s" % ALIAS)
    chain = recipe.build_config_chain()
    args = fw.prepare_benchmark_args(recipe, chain, {})
    cmd = fw.build_benchmark_command("http://h:8000/v1", _request_model(fw, recipe), args)
    assert cmd[cmd.index("--model") + 1] == MODEL_ID
    assert cmd[cmd.index("--served-model-name") + 1] == ALIAS


def test_exported_metadata_still_records_the_model_id(aliased_env):
    """The request name is routing, not identity: the artifact describes what
    was measured, which is the weights."""
    result = benchmark(aliased_env.options, sctx=aliased_env.sctx)
    exported = yaml.safe_load(Path(result.outputs["yaml"]).read_text())["sparkrun_benchmark"]
    assert exported["recipe"]["model"] == MODEL_ID
