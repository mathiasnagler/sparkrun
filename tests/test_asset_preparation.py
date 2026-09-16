"""Builds fail on incomplete tuning staging; launches retain best effort."""

from types import SimpleNamespace

import pytest

from sparkrun.core.asset_preparation import prepare_tuning
from sparkrun.core.cluster_manager import ClusterDefinition


@pytest.mark.parametrize("phase", ["sync", "directory", "distribution"])
@pytest.mark.parametrize("strict", [False, True])
def test_tuning_failure_policy(monkeypatch, phase, strict):
    recipe = SimpleNamespace(runtime="vllm", source_registry=None)
    runtime = SimpleNamespace(is_delegating_runtime=lambda: False)
    monkeypatch.setattr("sparkrun.tuning._common.tuning_configs_present", lambda path: True)

    def sync(*a, **k):
        if phase == "sync":
            raise RuntimeError("registry unreachable")
        return 0

    monkeypatch.setattr("sparkrun.tuning.sync.sync_registry_tuning", sync)
    monkeypatch.setattr("sparkrun.tuning.distribute.ensure_remote_tuning_dirs", lambda *a, **k: ["h2"] if phase == "directory" else [])
    monkeypatch.setattr("sparkrun.tuning.distribute.distribute_tuning_to_hosts", lambda *a, **k: ["h2"] if phase == "distribution" else [])

    def prepare():
        prepare_tuning(recipe, runtime, ["h1", "h2"], cluster=ClusterDefinition("test", ["h1", "h2"]), registry_mgr=object(), strict=strict)

    if strict:
        with pytest.raises(RuntimeError, match="registry unreachable|h2"):
            prepare()
    else:
        prepare()
