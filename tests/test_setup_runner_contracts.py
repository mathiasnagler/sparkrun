"""Setup preflight, recording, and deterministic result contracts."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from threading import Event
from unittest.mock import Mock

import pytest
from sparkrun.api.setup import run_setup_steps, SetupFailed
from sparkrun.core.features import FeatureFlag, register_feature
from sparkrun.core.setup_actions import SetupActionContext, SetupActionResult
from sparkrun.core.setup_manifest import ManifestManager
from sparkrun.core.setup_models import CheckItem, WARN, OK, FAIL, SKIP
from sparkrun.core.setup_steps import SetupStep, register_setup_step
from test_setup_steps import state_context


def _step(action, status=WARN):
    register_feature(FeatureFlag("setup.steps.review", "review", default=True))
    register_setup_step(
        SetupStep(
            "review", "review", checks=(lambda *_: CheckItem("review", "review", status),), apply=action, feature_flag="setup.steps.review"
        )
    )


@pytest.mark.parametrize("text", ["distribution: jetsonrun\ncluster: lab\nphases: {}\n", "[invalid\n", "cluster: other\nphases: {}\n"])
def test_manifest_rejection_precedes_approval_credentials_and_actions(tmp_path, text):
    state, context = state_context()
    action, approve, credentials = Mock(), Mock(), Mock()
    _step(action)
    manager = ManifestManager(tmp_path / "clusters")
    path = manager.clusters_dir / "lab.manifest.yaml"
    path.write_text(text)
    with pytest.raises(SetupFailed, match="Cannot use setup manifest"):
        run_setup_steps(
            {state.host: state},
            context,
            SetupActionContext("tester"),
            manifest_mgr=manager,
            approve=approve,
            credentials=credentials,
            only_steps={"review"},
        )
    for callback in (action, approve, credentials):
        callback.assert_not_called()
    assert path.read_text() == text


@pytest.mark.parametrize("existing", [False, True])
def test_setup_records_changes_and_preserves_existing_phases(tmp_path, monkeypatch, existing):
    state, context = state_context()
    details = {"files": ["created"], "phase": "plugin-owned-value"}
    _step(lambda s, *_: SetupActionResult(s.host, OK, "done", changed=True, extra=details))
    manager = ManifestManager(tmp_path / "clusters")
    if existing:
        manager.record_phase("lab", "tester", ["other"], "old")
    probe = Mock(return_value=({state.host: state}, context))
    monkeypatch.setattr("sparkrun.core.setup_probe.probe_setup_hosts", probe)
    result = run_setup_steps({state.host: state}, context, SetupActionContext("tester"), manifest_mgr=manager, only_steps={"review"})
    manifest = manager.load("lab", strict=True)
    assert manifest.phases["review"].extra["host_details"][state.host] == details
    assert ("old" in manifest.phases) is existing
    assert result.outcomes["review"][state.host].changed
    probe.assert_called_once()


def test_preview_never_locks_or_writes_manifest(tmp_path):
    state, context = state_context()
    action, approve, credentials = Mock(), Mock(), Mock()
    _step(action)
    manager = ManifestManager(tmp_path / "clusters")
    manager.recording = Mock(side_effect=AssertionError("preview cannot lock"))
    result = run_setup_steps(
        {state.host: state},
        context,
        SetupActionContext("tester", dry_run=True),
        manifest_mgr=manager,
        approve=approve,
        credentials=credentials,
        only_steps={"review"},
    )
    assert result.outcomes["review"][state.host].detail == "would apply review"
    assert list(manager.clusters_dir.iterdir()) == []
    for callback in (action, approve, credentials, manager.recording):
        callback.assert_not_called()


@pytest.mark.parametrize("bad_name", [None, "other"])
def test_inconsistent_recording_inputs_fail_before_actions(tmp_path, bad_name):
    state, context = state_context()
    action = Mock()
    _step(action)
    if bad_name is None:
        context = replace(context, cluster_name=None)
    manager = ManifestManager(tmp_path / "clusters")
    with pytest.raises(SetupFailed, match="cluster name"):
        run_setup_steps(
            {state.host: state}, context, SetupActionContext("tester"), cluster_name=bad_name, manifest_mgr=manager, only_steps={"review"}
        )
    action.assert_not_called()
    assert list(manager.clusters_dir.iterdir()) == []


@pytest.mark.parametrize("first_status", [WARN, FAIL, SKIP])
def test_step_aggregation_is_order_independent(first_status):
    first, context = state_context()
    second = replace(first, host="h2")
    context.executor_names[second.host] = "docker"
    _step(lambda s, *_: SetupActionResult(s.host, first_status if s.host == first.host else OK, "host detail"))
    expected = OK if first_status == SKIP else first_status
    for states in ({first.host: first, second.host: second}, {second.host: second, first.host: first}):
        result = run_setup_steps(states, context, SetupActionContext("tester"), only_steps={"review"})
        assert result.steps == {"review": expected}
        assert result.outcomes["review"][first.host].status == first_status
        assert result.outcomes["review"][second.host].detail == "host detail"


def test_satisfied_and_topology_steps_have_explicit_outcomes():
    state, context = state_context()
    action = Mock()
    _step(action, status=OK)
    result = run_setup_steps({state.host: state}, context, SetupActionContext("tester"), only_steps={"review", "ssh_mesh"})
    assert result.steps == {"ssh_mesh": SKIP, "review": OK}
    assert result.outcomes["review"][state.host].detail == "already satisfied"
    assert "topology adapter" in result.outcomes["ssh_mesh"][state.host].detail
    action.assert_not_called()


def test_manifest_recording_lock_serializes_other_managers(tmp_path):
    first, second = ManifestManager(tmp_path), ManifestManager(tmp_path)
    attempted = Event()

    def record():
        attempted.set()
        second.record_phase("lab", "tester", ["other"], "second")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with first.recording("lab"):
            future = pool.submit(record)
            assert attempted.wait(timeout=2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.05)
            first.record_phase("lab", "tester", ["first"], "first")
        future.result(timeout=3)
    assert set(first.load("lab", strict=True).phases) == {"first", "second"}
    # The stable lock file survives deletion of the atomically replaced data.
    first.delete("lab")
    assert (tmp_path / "lab.manifest.lock").exists()


def test_wizard_preflight_rejects_before_ssh_access(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from sparkrun.cli._setup._wizard import setup_wizard
    from sparkrun.core.cluster_manager import ClusterManager
    from sparkrun.core.config import get_config_root

    manager = ClusterManager(get_config_root())
    manager.create(name="lab", hosts=["h1"], user="tester")
    path = manager.clusters_dir / "lab.manifest.yaml"
    path.write_text("distribution: jetsonrun\ncluster: lab\nphases: {}\n")
    access = Mock(side_effect=AssertionError("preflight must run first"))
    monkeypatch.setattr("sparkrun.cli._setup._ssh._ensure_ssh_access", access)
    result = CliRunner().invoke(setup_wizard, ["--cluster", "lab", "--yes", "--user", "tester"])
    assert result.exit_code != 0 and "another application" in result.output
    access.assert_not_called()
