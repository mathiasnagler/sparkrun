"""Public plugin identity contracts, independent of any gateway integration."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
import os
import subprocess
import sys

import pytest

from sparkrun.application import (
    ApplicationProfile,
    ApplicationIdentity,
    ControllerIdentity,
    get_application_identity,
    get_controller_identity,
)
from sparkrun.core.application_profile import SPARKRUN, get_application_profile
from sparkrun.core.config import SparkrunConfig
from sparkrun.core.context import SparkrunContext


def test_shared_service_can_retain_two_application_controllers():
    jetson = ApplicationProfile(id="jetsonrun", display_name="Jetsonrun", command="jetsonrun", package="jetsonrun")
    controllers = [ControllerIdentity(get_application_identity(profile), "lab") for profile in (SPARKRUN, jetson)]
    # Simulate two clients publishing the same local resource name to one service.
    received = [ControllerIdentity.from_dict(json.loads(json.dumps(c.to_dict()))) for c in controllers]
    resources = {(*c.scope_key, "model-a"): c.labels() for c in received}
    assert len(resources) == 2
    for controller in received:
        assert sum(controller.owns(labels) for labels in resources.values()) == 1
        assert not controller.owns({"sparkrun.distribution": controller.application.id})
    assert get_application_profile() is SPARKRUN
    assert received == controllers
    with pytest.raises(FrozenInstanceError):
        received[0].controller_id = "other"


def test_different_config_directories_do_not_own_each_other(tmp_path):
    one = get_controller_identity(SparkrunConfig(tmp_path / "one" / "config.yaml"))
    two = get_controller_identity(SparkrunConfig(tmp_path / "two" / "config.yaml"))
    assert one.scope_key != two.scope_key
    assert not one.owns(two.labels())


def test_application_descriptor_is_pure_and_does_not_export_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    profile = ApplicationProfile(
        id="jetsonrun",
        display_name="Jetsonrun",
        command="jetsonrun",
        package="jetsonrun",
        defaults={"proxy": {"master_key": "secret"}},
        telemetry_key="private",
        profile_ref="example:PROFILE",
    )
    descriptor = get_application_identity(profile)
    assert ApplicationIdentity.from_dict(descriptor.to_dict()) == descriptor
    assert set(descriptor.to_dict()) == {"id", "command", "package", "resource_namespace"}
    assert not list(tmp_path.iterdir())


def test_controller_id_is_stable_across_contexts_threads_and_processes(tmp_path):
    config = SparkrunConfig(tmp_path / "site.yaml")
    other_config = SparkrunConfig(tmp_path / "other.yaml")
    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(get_controller_identity, [config, other_config] * 8))
    identity = identities[0]
    assert all(item == identity for item in identities)
    context = SparkrunContext(variables=None, config=config)
    assert context.controller_identity == identity
    assert context.application_identity == identity.application
    code = """
import json, sys
from sparkrun.application import initialize, get_controller_identity
context = initialize(config_path=sys.argv[1])
assert context.controller_identity == get_controller_identity()
print(json.dumps(context.controller_identity.to_dict()))
"""
    result = subprocess.run([sys.executable, "-c", code, str(other_config.config_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert ControllerIdentity.from_dict(json.loads(result.stdout)) == identity
    assert not config.config_path.exists(), "Generating identity must not materialize profile defaults in config.yaml"
    sidecars = list(tmp_path.glob(".controllers/*.id"))
    assert len(sidecars) == 1
    sidecar = sidecars[0]
    if os.name != "nt":
        assert sidecar.stat().st_mode & 0o777 == 0o600


def test_config_files_share_identity_but_applications_do_not(tmp_path):
    one = SparkrunConfig(tmp_path / "one.yaml")
    two = SparkrunConfig(tmp_path / "two.yaml")
    assert get_controller_identity(one) == get_controller_identity(two)
    # Construct a second config in a child process, retaining the first descriptor
    # in this process. A shared service never switches the global active profile.
    code = """
import json, sys
from sparkrun.application import ApplicationProfile, initialize
profile = ApplicationProfile(id="jetsonrun", display_name="Jetsonrun", command="jetsonrun", package="jetsonrun")
print(json.dumps(initialize(profile, config_path=sys.argv[1]).controller_identity.to_dict()))
"""
    result = subprocess.run([sys.executable, "-c", code, str(one.config_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    alternate = ControllerIdentity.from_dict(json.loads(result.stdout))
    assert alternate.controller_id != get_controller_identity(one).controller_id
    assert alternate.application.id == "jetsonrun"


def test_config_cannot_override_identity_or_export_secrets(tmp_path):
    config = SparkrunConfig(tmp_path / "site.yaml")
    config.set("controller.id", "old-override")
    config.set("plugins.example.secret", "do-not-export")
    identity = get_controller_identity(config)
    config.set("controller.id", "another-override")
    assert get_controller_identity(config) == identity
    assert identity.controller_id not in {"old-override", "another-override"}
    assert "do-not-export" not in json.dumps(identity.to_dict())


def test_config_directory_alias_uses_the_same_identity(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    first = get_controller_identity(SparkrunConfig(directory / "one.yaml"))
    second = get_controller_identity(SparkrunConfig(alias / "two.yaml"))
    assert first == second
    assert len(list(directory.glob(".controllers/*.id"))) == 1


def test_corrupt_persisted_id_is_not_silently_replaced(tmp_path):
    config = SparkrunConfig(tmp_path / "site.yaml")
    get_controller_identity(config)
    sidecar = next(tmp_path.glob(".controllers/*.id"))
    sidecar.write_text("invalid identity!")
    with pytest.raises(ValueError, match="controller_id"):
        get_controller_identity(config)
    assert sidecar.read_text() == "invalid identity!"


@pytest.mark.parametrize("version", [None, True, 0, 2, "1"])
def test_unsupported_wire_schema_is_rejected(version):
    data = ControllerIdentity(get_application_identity(), "lab").to_dict()
    data["schema_version"] = version
    with pytest.raises(ValueError, match="schema_version"):
        ControllerIdentity.from_dict(data)


def test_job_metadata_publishes_selected_controller(tmp_path):
    from sparkrun.core.recipe import Recipe
    from sparkrun.orchestration.job_metadata import save_job_metadata, load_job_metadata

    config = SparkrunConfig(tmp_path / "site.yaml")
    context = SparkrunContext(variables=None, config=config)
    # A context keeps the identity already advertised during this session.
    identity = context.controller_identity
    config.set("defaults.port", 9000)
    recipe = Recipe({"model": "example/model", "runtime": "vllm-ray", "container": "example/image"})
    cluster_id = "sparkrun_" + "a" * 16 + "_" + "b" * 12
    save_job_metadata(cluster_id, recipe, ["localhost"], cache_dir=str(tmp_path), sctx=context)
    metadata = load_job_metadata(cluster_id, cache_dir=str(tmp_path))
    controller = ControllerIdentity.from_dict(metadata["controller"])
    assert controller == identity
    assert controller == get_controller_identity(SparkrunConfig(tmp_path / "other.yaml"))
    assert controller.application.id == metadata["distribution"]
