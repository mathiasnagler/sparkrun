"""Recorded setup rejects corrupt state and retains unfinished undo targets."""

from itertools import permutations
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from sparkrun.api.setup import (
    CheckItem,
    FAIL,
    OK,
    SKIP,
    WARN,
    ManifestManager,
    SetupActionContext,
    SetupActionResult,
    SetupFailed,
    SetupStep,
    run_setup_steps,
    run_setup_undo,
)
from sparkrun.core.features import FeatureFlag, register_feature
from sparkrun.core.setup_steps import register_setup_step
from test_setup_steps import state_context


@pytest.mark.parametrize(
    "body",
    [
        "hosts: broken\n",
        "hosts: [1]\n",
        "user: []\n",
        "version: 2\n",
        "version: true\n",
        "phases: []\n",
        "phases: {review: {hosts: broken}}\n",
        "phases: {review: {extra: []}}\n",
        "phases: {review: {applied: 'false'}}\n",
        "phases: {review: {timestamp: 1}}\n",
        "phases: {review: {extra: {host_details: broken}}}\n",
        "phases: {review: {extra: {host_details: {h1: {files: broken}}}}}\n",
        "phases: {review: {extra: {host_details: {h1: {installed_package: 'false'}}}}}\n",
    ],
)
def test_manifest_shape_rejection_precedes_all_actions(tmp_path, monkeypatch, body):
    state, context = state_context()
    action, approve, credentials, probe = Mock(), Mock(), Mock(), Mock()
    register_feature(FeatureFlag("setup.steps.review", "review", default=True))
    register_setup_step(
        SetupStep(
            "review", "review", checks=(lambda *_: CheckItem("review", "review", WARN),), apply=action, feature_flag="setup.steps.review"
        )
    )
    manager = ManifestManager(tmp_path / "clusters")
    path = manager.clusters_dir / "lab.manifest.yaml"
    original = "distribution: sparkrun\ncluster: lab\n" + body
    path.write_text(original)
    monkeypatch.setattr("sparkrun.core.setup_probe.probe_setup_hosts", probe)
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
    for callback in (action, approve, credentials, probe):
        callback.assert_not_called()
    assert path.read_text() == original


def test_legacy_manifest_and_plugin_details_roundtrip_but_invalid_writes_do_not(tmp_path):
    manager = ManifestManager(tmp_path)
    path = tmp_path / "lab.manifest.yaml"
    path.write_text("hosts: [h1]\nphases: {review: {hosts: [h1], extra: {plugin_value: {arbitrary: null}}}}\n")
    manifest = manager.load("lab", strict=True)
    manager.save(manifest)
    assert manager.load("lab", strict=True).phases["review"].extra == {"plugin_value": {"arbitrary": None}}
    original = path.read_text()
    with pytest.raises(ValueError, match="hosts must be a list"):
        manager.save(replace(manifest, hosts="broken"))
    assert path.read_text() == original


def _undo_env(tmp_path, monkeypatch, phases, *, statuses=None):
    from sparkrun.core.cluster_manager import ClusterManager

    clusters = ClusterManager(tmp_path / "config")
    clusters.create("lab", ["h1", "h2"], user="tester")
    manifests = ManifestManager(clusters.clusters_dir)
    callbacks = {}
    for name, hosts in phases.items():
        details = {}
        for host in hosts:
            marker = tmp_path / (name + "-" + host)
            marker.write_text("installed")
            details[host] = {"created": str(marker)}
        manifests.record_phase("lab", "tester", hosts, name, host_details=details)

        def undo(host, detail, action, name=name):
            status = (statuses or {}).get(host, OK)
            if status == OK:
                Path(detail["created"]).unlink(missing_ok=True)
            return SetupActionResult(host, status, "removed" if status == OK else "not removed")

        callbacks[name] = Mock(side_effect=undo)
        register_feature(FeatureFlag("setup.steps." + name, name, default=True))
        register_setup_step(SetupStep(name, name, feature_flag="setup.steps." + name, undo=callbacks[name], requires_sudo=False))
    monkeypatch.setattr("sparkrun.core.config.get_config_root", lambda *a, **kw: tmp_path / "config")
    monkeypatch.setattr("sparkrun.cli._setup._uninstall._check_running_containers", lambda *_: [])
    return clusters, manifests, callbacks


@pytest.mark.parametrize("status", [SKIP, WARN, FAIL, "invalid"])
def test_cli_incomplete_undo_retains_manifest(tmp_path, monkeypatch, status):
    from sparkrun.cli._setup._uninstall import setup_uninstall

    clusters, manifests, callbacks = _undo_env(tmp_path, monkeypatch, {"review": ["h1"]}, statuses={"h1": status})
    result = CliRunner().invoke(setup_uninstall, ["lab", "--yes"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    callbacks["review"].assert_called_once()
    assert manifests.load("lab", strict=True).phases["review"].hosts == ["h1"]
    assert clusters.get("lab") and (tmp_path / "review-h1").exists()
    assert "deleted" not in result.output and "review:        removed" not in result.output


def test_selective_uninstall_then_retry_only_visits_remaining_changes(tmp_path, monkeypatch):
    from sparkrun.cli._setup._uninstall import setup_uninstall

    _, manifests, callbacks = _undo_env(tmp_path, monkeypatch, {"review_a": ["h1"], "review_b": ["h2"]})
    first = CliRunner().invoke(setup_uninstall, ["lab", "--yes", "--phase", "review_a"])
    assert first.exit_code == 0, first.output
    assert set(manifests.load("lab", strict=True).phases) == {"review_b"}
    callbacks["review_a"].assert_called_once()
    callbacks["review_b"].assert_not_called()
    second = CliRunner().invoke(setup_uninstall, ["lab", "--yes"])
    assert second.exit_code == 0, second.output
    callbacks["review_a"].assert_called_once()
    callbacks["review_b"].assert_called_once()
    assert manifests.load("lab", strict=True) is None
    assert "deleted" in second.output


def test_undo_records_each_success_before_notifications_and_preserves_other_hosts(tmp_path, monkeypatch):
    _, manager, callbacks = _undo_env(tmp_path, monkeypatch, {"review": ["h1", "h2"]}, statuses={"h2": WARN})

    def event(ev):
        if ev.kind == "result" and ev.hosts == ("h1",):
            assert manager.load("lab", strict=True).phases["review"].hosts == ["h2"]

    result = run_setup_undo(manager.load("lab"), SetupActionContext("tester"), manifest_mgr=manager, progress_callback=event)
    assert not result.complete and result.remaining == {"review": ("h2",)}
    assert result.outcomes["review"]["h1"].status == OK
    assert result.outcomes["review"]["h2"].status == WARN
    callbacks["review"].side_effect = lambda host, *_: SetupActionResult(host, OK, "already absent")
    result = run_setup_undo(manager.load("lab"), SetupActionContext("tester"), manifest_mgr=manager)
    assert result.complete and manager.load("lab").phases == {}
    assert callbacks["review"].call_args.args[0] == "h2"


@pytest.mark.parametrize("dry_run", [False, True])
def test_declined_or_preview_undo_keeps_records_without_credentials(tmp_path, monkeypatch, dry_run):
    _, manager, callbacks = _undo_env(tmp_path, monkeypatch, {"review": ["h1"]})
    credentials = Mock()
    before = (manager.clusters_dir / "lab.manifest.yaml").read_text()
    result = run_setup_undo(
        manager.load("lab"),
        SetupActionContext("tester", dry_run=dry_run),
        manifest_mgr=manager,
        approve=lambda *_: False,
        credentials=credentials,
    )
    assert result.remaining == {"review": ("h1",)}
    assert result.outcomes["review"]["h1"].status == SKIP
    callbacks["review"].assert_not_called()
    credentials.assert_not_called()
    assert (manager.clusters_dir / "lab.manifest.yaml").read_text() == before


def test_builtin_undo_failures_keep_only_unfinished_host_records(tmp_path, monkeypatch):
    from sparkrun.cli._setup._uninstall import setup_uninstall
    from sparkrun.orchestration.ssh import RemoteResult

    _, manager, _ = _undo_env(tmp_path, monkeypatch, {})
    manager.record_phase("lab", "tester", ["h1", "h2"], "earlyoom")
    monkeypatch.setattr("sparkrun.orchestration.ssh.run_remote_scripts_parallel", lambda *a, **kw: [RemoteResult("h1", 0, "", "")])
    teardown = Mock(side_effect=lambda host, *_: SetupActionResult(host, OK if host == "h1" else FAIL, "done"))
    monkeypatch.setattr("sparkrun.core.setup_undo_actions.undo_earlyoom", teardown)
    result = CliRunner().invoke(setup_uninstall, ["lab", "--yes"])
    assert result.exit_code == 0, result.output
    assert manager.load("lab", strict=True).phases["earlyoom"].hosts == ["h2"]
    assert teardown.call_count == 2


def test_docker_group_removal_failure_is_not_reported_as_already_absent(tmp_path):
    import os
    import subprocess
    from sparkrun.core.setup_undo_actions import undo_docker_group
    from sparkrun.orchestration.ssh import RemoteResult

    # Execute the actual generated script with inert replacements for both
    # account commands. No local account or group mutation can occur.
    for command, body in {"id": "echo 'tester docker'", "gpasswd": "echo 'removal failed' >&2; exit 1"}.items():
        path = tmp_path / command
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o700)

    def sudo(host, script, password, *, timeout):
        completed = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        )
        return RemoteResult(host, completed.returncode, completed.stdout, completed.stderr)

    result = undo_docker_group("h1", {}, SetupActionContext("tester", dispatch=sudo))
    assert result.status == FAIL and "removal failed" in result.detail


def test_builtin_undo_is_available_to_headless_callers_and_silent(tmp_path, capsys):
    from sparkrun.orchestration.ssh import RemoteResult

    manager = ManifestManager(tmp_path)
    manager.record_phase("lab", "tester", ["h1"], "earlyoom", installed_package=False)
    dispatch = Mock(return_value=RemoteResult("h1", 0, "removed owned config", ""))
    capsys.readouterr()
    result = run_setup_undo(manager.load("lab"), SetupActionContext("tester", dispatch=dispatch), manifest_mgr=manager)
    assert result.complete and result.outcomes["earlyoom"]["h1"].status == OK
    dispatch.assert_called_once()
    assert manager.load("lab").phases == {}
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_mesh_retry_uses_keys_from_already_cleaned_peers(tmp_path, monkeypatch):
    from sparkrun.orchestration.ssh import RemoteResult

    manager = ManifestManager(tmp_path)
    manager.record_phase("lab", "tester", ["h1", "h2"], "ssh_mesh")
    manager.record_phase("lab", "tester", ["unrelated-host"], "another_setup_phase")
    collect = Mock(return_value=[RemoteResult("h1", 0, "ssh-ed25519 AAAA", ""), RemoteResult("h2", 0, "ssh-ed25519 BBBB", "")])
    remove = Mock(side_effect=lambda host, *_a, **_kw: RemoteResult(host, 0 if host == "h1" else 1, "", "failed"))
    monkeypatch.setattr("sparkrun.orchestration.ssh.run_remote_scripts_parallel", collect)
    monkeypatch.setattr("sparkrun.orchestration.ssh.run_remote_script", remove)
    result = run_setup_undo(manager.load("lab"), SetupActionContext("tester"), manifest_mgr=manager)
    assert result.remaining == {"ssh_mesh": ("h2",), "another_setup_phase": ("unrelated-host",)}
    remove.reset_mock()
    remove.side_effect = lambda host, *_a, **_kw: RemoteResult(host, 0, "removed", "")
    result = run_setup_undo(manager.load("lab"), SetupActionContext("tester"), manifest_mgr=manager)
    assert result.remaining == {"another_setup_phase": ("unrelated-host",)}
    assert collect.call_args.args[0] == ("h1", "h2")
    remove.assert_called_once()
    assert remove.call_args.args[0] == "h2"
    assert "AAAA" in remove.call_args.args[1] and "BBBB" in remove.call_args.args[1]


@pytest.mark.parametrize("mode", ["failure", "decline", "filter", "mapping"])
def test_undo_keeps_transitive_prerequisites_per_host(tmp_path, mode):
    manager = ManifestManager(tmp_path)
    callbacks = {}
    for key, requires in (("base", ()), ("middle", ("base",)), ("leaf", ("middle",))):
        register_feature(FeatureFlag("setup.steps." + key, key, default=True))

        def undo(host, details, action, key=key):
            status = FAIL if key == "leaf" and host == "h1" and mode == "failure" else OK
            return SetupActionResult(host, status, "done")

        callbacks[key] = Mock(side_effect=undo)
        register_setup_step(
            SetupStep(key, key, requires=requires, undo=callbacks[key], requires_sudo=False, feature_flag="setup.steps." + key)
        )
    # No middle record: the dependency guard must still see leaf -> middle -> base.
    for key in ("base", "leaf"):
        manager.record_phase("lab", "tester", ["h1", "h2"], key)
    result = run_setup_undo(
        manager.load("lab"),
        SetupActionContext("tester"),
        manifest_mgr=manager,
        only_steps={"base"} if mode == "filter" else None,
        steps={"base": SetupStep("base", "base", undo=callbacks["base"], requires_sudo=False)} if mode == "mapping" else None,
        approve=(lambda step, hosts: step.key != "leaf") if mode == "decline" else None,
    )
    assert result.remaining["base"] == (("h1",) if mode == "failure" else ("h1", "h2"))
    assert result.outcomes["base"]["h1"].status == SKIP
    assert "leaf" in result.outcomes["base"]["h1"].detail
    if mode == "failure":
        callbacks["base"].assert_called_once()
        assert callbacks["base"].call_args.args[0] == "h2"
        callbacks["leaf"].side_effect = lambda host, *_: SetupActionResult(host, OK, "removed")
        retry = run_setup_undo(manager.load("lab"), SetupActionContext("tester"), manifest_mgr=manager)
        assert retry.complete
        assert callbacks["base"].call_args.args[0] == "h1"
    else:
        callbacks["base"].assert_not_called()


@pytest.mark.parametrize("returncode", [0, 23])
def test_docker_group_command_status_controls_recording(tmp_path, monkeypatch, returncode):
    import os
    import subprocess
    from sparkrun.orchestration.ssh import RemoteResult

    stub = tmp_path / "usermod"
    stub.write_text(f"#!/bin/sh\necho stub-result >&2\nexit {returncode}\n")
    stub.chmod(0o700)

    def dispatch(host, script, password, **kwargs):
        result = subprocess.run(["/bin/bash"], input=script, text=True, capture_output=True, env={**os.environ, "PATH": str(tmp_path)})
        return RemoteResult(host=host, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)

    state, context = state_context(CHECK_DOCKER_GROUP="0", CHECK_DOCKER_USABLE="0")
    manager = ManifestManager(tmp_path / "manifests")
    monkeypatch.setattr("sparkrun.core.setup_probe.probe_setup_hosts", lambda *a, **kw: ({state.host: state}, context))
    result = run_setup_steps(
        {state.host: state}, context, SetupActionContext("tester", dispatch=dispatch), manifest_mgr=manager, only_steps={"docker_group"}
    )
    outcome = result.outcomes["docker_group"][state.host]
    assert outcome.status == (OK if returncode == 0 else FAIL)
    assert outcome.changed is (returncode == 0)
    manifest = manager.load("lab")
    assert bool(manifest and "docker_group" in manifest.phases) is (returncode == 0)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("invalid", ["foreign_application", "version", "details", "hosts", "applied", "cluster"])
def test_detached_undo_validates_before_callbacks(invalid, dry_run):
    from copy import deepcopy
    from sparkrun.api.setup import SetupManifest, PhaseRecord

    manifest = SetupManifest(1, "lab", "", "", "tester", ["h1"], {"review": PhaseRecord(True, "", ["h1"])})
    if invalid == "foreign_application":
        manifest.distribution = "other-application"
    elif invalid == "version":
        manifest.version = 999
    elif invalid == "details":
        manifest.phases["review"].extra = {"files": "not-a-list"}
    elif invalid == "hosts":
        manifest.phases["review"].hosts = "h1"
    elif invalid == "applied":
        manifest.phases["review"].applied = "true"
    else:
        manifest.cluster = "../lab"
    before = deepcopy(manifest)
    undo, approve, credentials, progress = Mock(), Mock(), Mock(), Mock()
    with pytest.raises(SetupFailed):
        run_setup_undo(
            manifest,
            SetupActionContext("tester", dry_run=dry_run),
            steps={"review": SetupStep("review", "review", undo=undo)},
            approve=approve,
            credentials=credentials,
            progress_callback=progress,
        )
    for callback in (undo, approve, credentials, progress):
        callback.assert_not_called()
    assert manifest == before


@pytest.mark.parametrize("recorded", [False, True])
def test_undo_result_carries_persistable_partial_manifest(tmp_path, monkeypatch, recorded):
    from copy import deepcopy
    from sparkrun.orchestration.ssh import RemoteResult

    manager = ManifestManager(tmp_path)
    manager.record_phase(
        "lab",
        "tester",
        ["h1", "h2"],
        "ssh_mesh",
        host_details={"h1": {"plugin_value": [1]}, "h2": {"plugin_value": [2]}},
    )
    manager.record_phase("lab", "tester", ["other"], "unavailable_plugin")
    original = manager.load("lab", strict=True)
    original.updated = "2000-01-01T00:00:00+00:00"
    manager.save(original)
    before = deepcopy(original)
    collect = Mock(return_value=[RemoteResult("h1", 0, "ssh-ed25519 AAAA", ""), RemoteResult("h2", 0, "ssh-ed25519 BBBB", "")])
    remove = Mock(side_effect=lambda host, *_a, **_kw: RemoteResult(host, 0 if host == "h1" else 1, "", "failed"))
    monkeypatch.setattr("sparkrun.orchestration.ssh.run_remote_scripts_parallel", collect)
    monkeypatch.setattr("sparkrun.orchestration.ssh.run_remote_script", remove)
    result = run_setup_undo(original, SetupActionContext("tester"), manifest_mgr=manager if recorded else None)
    assert original == before
    assert result.manifest.updated != before.updated
    assert result.manifest.phases["ssh_mesh"].hosts == ["h2"]
    assert result.manifest.phases["ssh_mesh"].extra == {
        "host_details": {"h2": {"plugin_value": [2]}},
        "mesh_hosts": ["h1", "h2"],
    }
    assert result.remaining == {"ssh_mesh": ("h2",), "unavailable_plugin": ("other",)}
    if recorded:
        assert result.manifest == manager.load("lab", strict=True)
    else:
        assert manager.load("lab", strict=True) == before
        # External persistence needs no replay of per-host result transformations.
        manager.save(result.manifest)
    remove.reset_mock()
    remove.side_effect = lambda host, *_a, **_kw: RemoteResult(host, 0, "removed", "")
    retry = run_setup_undo(manager.load("lab", strict=True), SetupActionContext("tester"), manifest_mgr=manager if recorded else None)
    assert collect.call_args.args[0] == ("h1", "h2")
    remove.assert_called_once()
    assert remove.call_args.args[0] == "h2"
    assert "AAAA" in remove.call_args.args[1] and "BBBB" in remove.call_args.args[1]
    assert retry.remaining == {"unavailable_plugin": ("other",)}
    assert set(retry.manifest.phases) == {"unavailable_plugin"}
    assert result.manifest.phases["ssh_mesh"].hosts == ["h2"]  # prior snapshot remains detached


@pytest.mark.parametrize("recorded", [False, True])
def test_undo_preview_returns_unchanged_manifest(tmp_path, recorded):
    manager = ManifestManager(tmp_path)
    manager.record_phase("lab", "tester", ["h1"], "earlyoom")
    original = manager.load("lab", strict=True)
    action = Mock()
    result = run_setup_undo(
        original, SetupActionContext("tester", dry_run=True, dispatch=action), manifest_mgr=manager if recorded else None
    )
    assert result.manifest == original == manager.load("lab", strict=True)
    assert result.manifest is not original
    result.manifest.phases.clear()
    assert "earlyoom" in original.phases and "earlyoom" in manager.load("lab", strict=True).phases
    action.assert_not_called()


@pytest.mark.parametrize("order", permutations(("provider", "middle", "consumer")))
def test_custom_undo_orders_dependencies_independently_of_mapping(tmp_path, order):
    manager = ManifestManager(tmp_path)
    calls = []
    steps = {}
    for name, requires in (("provider", ()), ("middle", ("provider",)), ("consumer", ("middle",))):

        def undo(host, details, action, name=name):
            calls.append(name)
            return SetupActionResult(host, OK, "removed")

        steps[name] = SetupStep(name, name, requires=requires, undo=undo, requires_sudo=False)
        manager.record_phase("lab", "tester", ["h1"], name)
    original = manager.load("lab")
    result = run_setup_undo(original, SetupActionContext("tester"), steps={name: steps[name] for name in order})
    assert result.complete
    assert calls == ["consumer", "middle", "provider"]
    assert all(record.applied for record in original.phases.values())
