"""Profile policy, option precedence, and actual Bash/worker delivery contracts."""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from sparkrun.orchestration.executor import DockerExecutor, ExecutorConfig, resolve_executor
from sparkrun.orchestration.executors._seccomp import IO_URING_SYSCALLS, io_uring_profile


POLICY = {"defaultAction": "SCMP_ACT_ERRNO", "syscalls": [{"names": ["read", "write"], "action": "SCMP_ACT_ALLOW"}]}


@pytest.fixture
def policy_file(tmp_path):
    # Both the filename and policy data must remain data, never shell code.
    path = tmp_path / "policy ' $(touch injected).json"
    path.write_text(json.dumps({**POLICY, "comment": "' $(touch injected) `touch injected`"}))
    return path


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, pathlib, sys\n"
        'if sys.argv[1] == "inspect": sys.exit(1)\n'
        'if sys.argv[1] != "run": sys.exit(0)\n'
        "args = sys.argv[2:]\n"
        'options = [args[i + 1] for i, a in enumerate(args) if a == "--security-opt"]\n'
        'policies = [o[8:] for o in options if o.startswith("seccomp=")]\n'
        "assert len(policies) == 1, options\n"
        "profile = json.loads(pathlib.Path(policies[0]).read_text())\n"
        'print("POLICY=" + json.dumps(profile, sort_keys=True))\n'
        'sys.exit(int(os.environ.get("FAKE_DOCKER_RC", "0")))\n'
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])

    def run(script, cwd=None):
        result = subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True, cwd=cwd or tmp_path, timeout=10)
        return result

    return run


def test_default_is_pinned_moby_plus_only_io_uring():
    raw = files("sparkrun.orchestration.executors").joinpath("seccomp/default.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74"
    upstream = json.loads(raw)
    result = json.loads(io_uring_profile())
    added = result["syscalls"].pop()
    assert result == upstream
    assert added == {"names": list(IO_URING_SYSCALLS), "action": "SCMP_ACT_ALLOW"}
    assert result["defaultAction"] == "SCMP_ACT_ERRNO"
    assert {"SCMP_ARCH_X86_64", "SCMP_ARCH_AARCH64"} <= {a["architecture"] for a in result["archMap"]}


def test_default_is_readable_by_cli_and_preserves_stdio(fake_docker):
    command = resolve_executor(auto_user=False).run_cmd("image")
    result = fake_docker(command + "\nprintf 'AFTER\\n'\n")
    assert result.returncode == 0, result.stderr
    assert "AFTER" in result.stdout
    received = json.loads(next(line[7:] for line in result.stdout.splitlines() if line.startswith("POLICY=")))
    assert received == json.loads(io_uring_profile())


@pytest.mark.parametrize("layer", ["cli", "recipe", "cluster", "runtime", "config"])
@pytest.mark.parametrize("rootless", [False, True])
def test_custom_policy_survives_resolution_layers(layer, rootless, policy_file, fake_docker):
    setting = {"security_opt": ["seccomp=" + str(policy_file)]}
    kwargs = {"rootless": rootless, "auto_user": False}
    if layer == "cli":
        kwargs["cli_overrides"] = setting
    elif layer == "runtime":
        kwargs["runtime"] = SimpleNamespace(default_executor=lambda: "docker", default_executor_config=lambda: setting)
    else:
        kwargs[layer] = SimpleNamespace(executor="docker", default_executor="docker", executor_config=setting)
    executor = resolve_executor(**kwargs)
    executor.prepare_launch()
    expected = json.loads(policy_file.read_text())
    policy_file.unlink()
    result = fake_docker(executor.run_cmd("image"))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.removeprefix("POLICY=")) == expected
    assert not (policy_file.parent / "injected").exists()


@pytest.mark.parametrize("spelling", ["separate", "combined", "equals", "legacy"])
def test_raw_policy_overrides_config_without_reading_superseded_file(spelling, policy_file, fake_docker):
    from shlex import quote

    value = "seccomp=" + str(policy_file)
    opts = {
        "separate": ["--security-opt", quote(value)],
        "combined": ["--security-opt " + quote(value)],
        "equals": [quote("--security-opt=" + value)],
        "legacy": ["--security-opt", quote(value.replace("seccomp=", "seccomp:", 1))],
    }[spelling]
    executor = DockerExecutor(ExecutorConfig(security_opt=["seccomp=/missing", "no-new-privileges"]))
    executor.prepare_launch(extra_opts=opts)
    expected = json.loads(policy_file.read_text())
    policy_file.unlink()
    command = executor.run_cmd("image", extra_opts=opts + ["--security-opt label=disable", "--shm-size 8g"])
    result = fake_docker(command)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.removeprefix("POLICY=")) == expected
    assert "--security-opt no-new-privileges" in command
    assert "--security-opt label=disable" in command
    assert "--shm-size 8g" in command


@pytest.mark.parametrize("policy", ["builtin", "unconfined"])
def test_explicit_docker_special_policies_are_preserved(policy):
    executor = resolve_executor(cli_overrides={"security_opt": ["seccomp=" + policy]})
    executor.prepare_launch()
    command = executor.run_cmd("image")
    assert "--security-opt seccomp=" + policy in command
    assert "<(" not in command
    assert command.count("seccomp=") == 1


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        "{}",
        "{",
        '{"defaultAction":"bogus"}',
        '{"defaultAction":"SCMP_ACT_ERRNO","syscalls":{}}',
        '{"defaultAction":"SCMP_ACT_ERRNO","syscalls":[{}]}',
        '{"defaultAction":"SCMP_ACT_ERRNO","value":NaN}',
    ],
)
def test_bad_local_profile_fails_preparation(value, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(value)
    executor = DockerExecutor(ExecutorConfig(security_opt=["seccomp=" + str(path)]))
    with pytest.raises(ValueError, match="Cannot load Docker seccomp profile"):
        executor.prepare_launch()


@pytest.mark.parametrize(
    "options",
    [
        {"security_opt": ["seccomp=io-uring", "seccomp=unconfined"]},
        {"extra_opts": ["--security-opt seccomp=io-uring", "--security-opt=seccomp=unconfined"]},
        {"extra_opts": ["--security-opt"]},
        {"security_opt": ["seccomp="]},
    ],
)
def test_ambiguous_or_empty_policy_rejected(options):
    executor = DockerExecutor(ExecutorConfig(security_opt=options.get("security_opt")))
    with pytest.raises(ValueError):
        executor.prepare_launch(extra_opts=options.get("extra_opts"))


@pytest.mark.parametrize("dry_run", [False, True])
def test_custom_policy_reaches_every_cluster_node_without_source_path(monkeypatch, tmp_path, policy_file, fake_docker, dry_run):
    from sparkrun.orchestration.ssh import RemoteResult
    from sparkrun.runtimes._cluster_ops import ClusterContext, launch_containers_parallel

    expected = json.loads(policy_file.read_text())
    executor = resolve_executor(cli_overrides={"security_opt": ["seccomp=" + str(policy_file)]}, auto_user=False)
    executor.prepare_launch()
    policy_file.unlink()
    hosts = ["head", "worker1", "worker2"]
    received = {}

    def remote(host, script, **kwargs):
        assert str(policy_file) not in script
        assert kwargs["dry_run"] == dry_run
        if dry_run:
            received[host] = None
            return RemoteResult(host, 0, "[dry-run]", "")
        directory = tmp_path / host
        directory.mkdir()
        result = fake_docker(script, directory)
        assert result.returncode == 0, result.stderr
        received[host] = json.loads(next(line[7:] for line in result.stdout.splitlines() if line.startswith("POLICY=")))
        assert not (directory / "injected").exists()
        return RemoteResult(host, result.returncode, result.stdout, result.stderr)

    monkeypatch.setattr("sparkrun.orchestration.ssh.run_remote_script", remote)
    ctx = ClusterContext(hosts, hosts[0], hosts[1:], 3, {}, {}, {}, "sparkrun_test", "image", dry_run, None)
    assert launch_containers_parallel(ctx, [(h, "node_" + str(i)) for i, h in enumerate(hosts)], executor, None) == 0
    assert received == {h: None if dry_run else expected for h in hosts}


@pytest.mark.parametrize("kind", ["solo", "node", "ray_head", "ray_worker"])
def test_rejected_profile_from_docker_is_a_launch_failure(fake_docker, monkeypatch, tmp_path, kind):
    monkeypatch.setenv("FAKE_DOCKER_RC", "125")
    executor = DockerExecutor()
    if kind.startswith("ray"):
        net = tmp_path / "net"
        (net / "eth0").mkdir(parents=True)
        monkeypatch.setenv("SPARKRUN_NET_SYSFS", str(net))
        monkeypatch.setenv("SPARKRUN_MGMT_IFACE", "eth0")
        ip = tmp_path / "bin/ip"
        ip.write_text("#!/bin/bash\nprintf 'inet 10.0.0.1/24\\n'\n")
        ip.chmod(0o755)
    if kind == "ray_head":
        script = executor.generate_ray_head_script("image", "test")
    elif kind == "ray_worker":
        script = executor.generate_ray_worker_script("image", "test", "10.0.0.1")
    elif kind == "node":
        script = executor.generate_node_script("image", "test", "sleep infinity")
    else:
        script = executor.generate_launch_script("image", "test", "sleep infinity")
    result = fake_docker(script)
    assert result.returncode == 125
    assert "launched successfully" not in result.stdout


@pytest.mark.parametrize("source", ["config", "raw"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_policy_aborts_launcher_before_replacement(monkeypatch, tmp_path, source, dry_run):
    from sparkrun.core.config import SparkrunConfig
    from sparkrun.core.launcher import launch_inference
    from sparkrun.core.recipe import Recipe
    from sparkrun.runtimes.vllm_distributed import VllmDistributedRuntime

    config_path = tmp_path / "config.yaml"
    config_path.write_text("cache_dir: " + str(tmp_path / "cache"))
    config = SparkrunConfig(config_path)
    recipe = Recipe.from_dict({"name": "test", "runtime": "vllm-distributed", "model": "org/model", "container": "image"})
    runtime = VllmDistributedRuntime()
    events = []
    monkeypatch.setattr(runtime, "prepare", lambda *args, **kw: None)
    monkeypatch.setattr(runtime, "run", lambda **kw: events.append("run"))
    monkeypatch.setattr("sparkrun.core.launcher.resolve_effective_cache_dir", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr("sparkrun.orchestration.distribution.resolve_auto_transfer_mode", lambda *a, **kw: SimpleNamespace(mode="local"))
    monkeypatch.setattr("sparkrun.orchestration.distribution.distribute_from_config", lambda *a, **kw: (None, {}, {}, {}))
    monkeypatch.setattr("sparkrun.orchestration.primitives.try_clear_page_cache", lambda *a, **kw: None)
    option = "seccomp=" + str(tmp_path / "missing.json")
    with pytest.raises(ValueError, match="Cannot load Docker seccomp profile"):
        launch_inference(
            recipe=recipe,
            runtime=runtime,
            host_list=["head", "worker"],
            overrides={},
            config=config,
            dry_run=dry_run,
            sync_tuning=False,
            executor_config={"security_opt": [option]} if source == "config" else None,
            extra_docker_opts=["--security-opt", option] if source == "raw" else None,
            before_start=lambda: events.append("replace"),
            trust=True,
        )
    assert not events
