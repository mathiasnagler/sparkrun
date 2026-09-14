"""Manual pruning resolves the same cluster and SSH settings as other CLI calls."""

from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from sparkrun.cli import main
from sparkrun.core.cluster_manager import ClusterManager
from sparkrun.orchestration.ssh import RemoteResult


@pytest.mark.parametrize("explicit", [False, True])
def test_prune_uses_effective_cluster_and_ssh_user(tmp_path, monkeypatch, explicit):
    manager = ClusterManager(tmp_path / "config")
    manager.create("lab", ["h1"], user="cluster-user")
    manager.set_default("lab")
    cluster = manager.get("lab")
    cluster.runtime_cache = {"dir": "/cache/compiled", "prune": {"max_age_days": 7}}
    # Keep real host/default-cluster resolution while supplying a detached cluster record.
    monkeypatch.setattr(manager, "get", lambda name: cluster)
    monkeypatch.setattr("sparkrun.cli._common._get_cluster_manager", lambda *a, **kw: manager)
    probe = Mock(return_value="/home/cluster-user/.cache/sparkrun")
    generate = Mock(return_value="sweep")
    remote = Mock(return_value=[RemoteResult("h1", 0, "REMOVE\t1024\ttree\n", "")])
    monkeypatch.setattr("sparkrun.orchestration.primitives.probe_remote_sparkrun_cache", probe)
    monkeypatch.setattr("sparkrun.orchestration.runtime_cache.generate_runtime_cache_sweep_script", generate)
    monkeypatch.setattr("sparkrun.orchestration.ssh.run_remote_scripts_parallel", remote)
    args = ["setup", "prune-runtime-cache", "--dry-run"] + (["--cluster", "lab"] if explicit else [])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    generate.assert_called_once_with("/cache/compiled", max_age_days=7, dry_run=True, purge_all=False)
    assert probe.call_args.kwargs["ssh_user"] == "cluster-user"
    assert remote.call_args.args == (["h1"], "sweep")
    assert remote.call_args.kwargs["ssh_user"] == "cluster-user"
    assert "Would remove 1 tree(s)" in result.output
