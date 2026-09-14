"""Spark Arena benchmark policy and upload lifecycle (no CLI imports)."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import yaml

from sparkrun.api._errors import BenchmarkFailed
from sparkrun.core.application_profile import render_identity_text
from sparkrun.core.benchmark_integrations import BenchmarkDefaults
from sparkrun.utils.data import thaw
from . import ARENA_BENCHMARK_PROFILE


def validate(context):
    unknown = set(context.settings) - {"local_test"}
    if unknown or ("local_test" in context.settings and type(context.settings["local_test"]) is not bool):
        raise BenchmarkFailed("Arena settings support only a boolean 'local_test'", exit_code=1)


def prepare(defaults: BenchmarkDefaults, context) -> BenchmarkDefaults:
    return replace(
        defaults,
        category=defaults.category or "performance",
        profile=defaults.profile or (None if context.settings.get("local_test") else ARENA_BENCHMARK_PROFILE),
    )


def _count_phrase(counts):
    return " and ".join(
        "%d %s%s" % (counts[level], level, "" if counts[level] == 1 else "s") for level in ("warning", "suggestion") if counts[level]
    )


def validate_recipe_for_submission(recipe, *, context):
    from sparkrun.core.validation import ERROR, WARNING, SUGGESTION, validate_recipe

    issues = validate_recipe(recipe, config=context.sctx.config, v=context.sctx.variables)
    if not issues:
        return
    context.emitter.info("Validating recipe before submission: %s" % recipe.qualified_name)
    for issue in issues:
        context.emitter.info("%s  %s: %s" % (issue.severity, issue.code, issue.message))
    counts = {level: sum(i.severity == level for i in issues) for level in (ERROR, WARNING, SUGGESTION)}
    if counts[ERROR]:
        raise BenchmarkFailed("The recipe has %d error(s) and cannot be submitted." % counts[ERROR], exit_code=1)
    context.emitter.info(
        "The recipe contains %s. We suggest that you address all of these issues before submitting a "
        "benchmark; problematic recipes may not be published to Spark Arena." % _count_phrase(counts)
    )
    if context.dry_run:
        context.emitter.info("[dry-run] Not prompting; nothing will be submitted.")
    elif not context.emitter.confirm("Continue with the benchmark and submission?", default=False):
        raise BenchmarkFailed("Aborted.", exit_code=1)


def bind(context):
    # This serialized recipe is Arena-owned data. Core must not interpret
    # identically named strings in other plugins' or callers' JSON payloads.
    if text := context.data.get("effective_recipe_text"):
        from sparkrun.benchmarking.metadata import public_recipe_text

        try:
            context.data["effective_recipe_text"] = public_recipe_text(text)
        except (ValueError, TypeError, yaml.YAMLError):
            raise BenchmarkFailed("Saved Arena recipe is invalid; start a fresh benchmark before publishing") from None
    from .auth import load_refresh_token, exchange_token
    from .upload import generate_submission_id

    if context.data.get("uploaded"):
        return
    if context.dry_run:
        context.emitter.info("[dry-run] Skipping authentication; nothing will be submitted.")
    elif context.settings.get("local_test"):
        context.emitter.info("[local-test] Skipping authentication — upload will be simulated.")
    else:
        token = load_refresh_token()
        if not token:
            raise BenchmarkFailed(render_identity_text("Not logged in. Run '{app_command} arena login' first."), exit_code=1)
        try:
            exchange_token(token)
        except RuntimeError as exc:
            raise BenchmarkFailed(
                render_identity_text("Authentication failed: %s. Run '{app_command} arena login'." % exc), exit_code=1
            ) from exc
        context.emitter.info("Authentication verified.")

    # A retry uses the originally validated recipe snapshot, even if the local
    # recipe has subsequently changed. New submissions validate the resolved recipe.
    if not context.data.get("effective_recipe_text"):
        if context.result.recipe_yaml is None:
            raise BenchmarkFailed("Arena submission provenance is missing; rerun the benchmark with --arena.", exit_code=1)
        from sparkrun.core.recipe import Recipe

        validate_recipe_for_submission(Recipe(yaml.safe_load(context.result.recipe_yaml)), context=context)
    if not context.dry_run:
        # Preserve IDs written by older versions when explicitly resuming them.
        legacy = context.state.extras if context.state is not None else {}
        context.data.setdefault("submission_id", legacy.get("submission_id") or generate_submission_id())
        checkpoint(context)


def checkpoint(context):
    if context.dry_run or context.data.get("uploaded"):
        return
    if context.resumed and context.data.get("effective_recipe_text"):
        return
    result = context.result
    if result.recipe_yaml is None:
        return
    context.data["effective_recipe_text"] = result.recipe_yaml
    context.data["metadata_json"] = thaw(result.provenance)


def complete(context):
    if context.dry_run:
        context.emitter.info("[dry-run] Would upload results to Spark Arena")
        return
    result = context.result
    submission_id = context.data["submission_id"]
    summary = context.outcome = {
        "submission_id": submission_id,
        "uploaded": bool(context.data.get("uploaded")),
        "local_test": bool(context.settings.get("local_test")),
    }
    if context.data.get("uploaded"):
        context.emitter.info("Results already uploaded (submission: %s)" % submission_id)
        return
    checkpoint(context)
    metadata = deepcopy(context.data.get("metadata_json") or {})
    recipe_text = context.data.get("effective_recipe_text")
    csv = (result.results or {}).get("csv")
    if not recipe_text or not metadata or not csv:
        raise BenchmarkFailed("Arena upload requires a recipe, metadata, and CSV results; submission state was retained.", exit_code=1)
    if context.resumed:
        from datetime import datetime

        metadata.setdefault("benchmark", {}).update(resumed=True, measured_at=result.measured_at)
        timing = metadata.setdefault("timing", {})
        for key in ("startup", "serve_ready", "launch"):
            timing.pop(key, None)
        if context.state is not None:
            timing["start"] = timing.get("start") or context.state.created_at
            timing["end"] = result.completed_at or result.measured_at
            if not timing["end"]:
                timing["end"] = timing["start"]
            timing["duration"] = (datetime.fromisoformat(timing["end"]) - datetime.fromisoformat(timing["start"])).total_seconds()

    context.data["metadata_json"] = metadata
    cache_dir = context.sctx.config.cache_dir / "benchmarks" / submission_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    files = [(cache_dir / "recipe.yaml", "recipes"), (cache_dir / "benchmark.csv", "logs"), (cache_dir / "metadata.json", "metadata")]
    files[0][0].write_text(recipe_text)
    files[1][0].write_text(csv)
    files[2][0].write_text(json.dumps(metadata, indent=2))
    if context.settings.get("local_test"):
        context.emitter.info("[local-test] Benchmark files written to: %s" % cache_dir)
        context.emitter.info("[local-test] Skipping actual upload.")
        return
    from .auth import load_refresh_token
    from .upload import upload_benchmark_results

    token = load_refresh_token()
    if token is None:
        raise BenchmarkFailed("Spark Arena credentials are unavailable. Log in and resume the benchmark to retry.", exit_code=1)
    context.emitter.info("Uploading results to Spark Arena...")
    try:
        success, sid = upload_benchmark_results(token, files, submission_id=submission_id)
    except RuntimeError as exc:
        raise BenchmarkFailed("Upload failed: %s. Resume the benchmark to retry." % exc, exit_code=1) from exc
    if not success:
        raise BenchmarkFailed("Some files failed to upload (submission: %s). Resume the benchmark to retry." % sid, exit_code=1)
    context.data["uploaded"] = True
    summary["uploaded"] = True
    context.emitter.info("Results uploaded successfully (submission: %s)" % sid)
