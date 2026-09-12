"""Console-free reversals of built-in, recorded setup changes."""

from __future__ import annotations

import re
from typing import Any

from sparkrun.core.setup_actions import SetupActionContext, SetupActionResult
from sparkrun.core.setup_manifest import SetupManifest
from sparkrun.core.setup_models import FAIL, OK, SKIP
from sparkrun.core.setup_steps import SetupStep
from sparkrun.utils.shell import quote, validate_unix_username

BUILTIN_UNDO_ORDER = ("earlyoom", "sudoers", "docker_group", "cx7", "ssh_mesh")


def _result(host, result):
    return SetupActionResult(
        host,
        OK if result.success else FAIL,
        (result.stdout.strip() or "removed or already absent") if result.success else result.stderr.strip()[:300],
    )


def undo_earlyoom(host: str, details: dict[str, Any], action: SetupActionContext) -> SetupActionResult:
    from sparkrun.scripts import read_script

    script = read_script("earlyoom_uninstall.sh").format(remove_package="true" if details.get("installed_package") else "false")
    return _result(host, action.run(host, script))


def undo_sudoers(host: str, details: dict[str, Any], action: SetupActionContext) -> SetupActionResult:
    from sparkrun.core.application_profile import get_application_profile

    validate_unix_username(action.user)
    namespace = get_application_profile().resource_namespace
    files = details.get("files") or ["/etc/sudoers.d/%s-%s-%s" % (namespace, key, action.user) for key in ("chown", "dropcaches")]
    script = "#!/bin/bash\nset -euo pipefail\n"
    for path in files:
        script += "if [ -f %s ]; then rm -f -- %s; fi\n" % (quote(path), quote(path))
    return _result(host, action.run(host, script))


def undo_docker_group(host: str, details: dict[str, Any], action: SetupActionContext) -> SetupActionResult:
    validate_unix_username(action.user)
    script = (
        "#!/bin/bash\nset -euo pipefail\n"
        'target_groups=$(id -nG -- "%s")\n'
        'if printf "%%s\\n" "$target_groups" | tr " " "\\n" | grep -qx docker; then\n'
        '  gpasswd -d "%s" docker\n  echo "REMOVED: %s from docker group"\n'
        'else\n  echo "SKIPPED: %s not in docker group"\nfi\n' % (action.user, action.user, action.user, action.user)
    )
    return _result(host, action.run(host, script))


def undo_cx7(host: str, details: dict[str, Any], action: SetupActionContext) -> SetupActionResult:
    from sparkrun.scripts import read_script
    from sparkrun.orchestration.ssh import run_remote_script

    result = _result(host, action.run(host, read_script("cx7_unconfigure.sh")))
    # The unconfigure script reports foreign configuration left in place in its
    # stdout. Preserve that detail without treating it as state owned by us.
    if result.status == OK and details.get("cx7_ips"):
        script = "\n".join("ssh-keygen -R %s 2>/dev/null || true" % quote(ip) for ip in details["cx7_ips"])
        cleaned = run_remote_script(host, script, timeout=30, quiet=True, **action.ssh_kwargs)
        if not cleaned.success:
            return _result(host, cleaned)
    return result


def builtin_undo_steps(manifest: SetupManifest) -> dict[str, SetupStep]:
    """Bind mesh key discovery to all original peers, including cleaned hosts."""
    mesh = manifest.phases.get("ssh_mesh")
    peers = tuple(dict.fromkeys([*mesh.extra.get("mesh_hosts", []), *mesh.hosts])) if mesh else ()
    key_data = None

    def undo_mesh(host, details, action):
        nonlocal key_data
        from sparkrun.orchestration.ssh import run_remote_scripts_parallel, run_remote_script

        if key_data is None:
            script = "cat ~/.ssh/id_ed25519.pub 2>/dev/null || cat ~/.ssh/id_rsa.pub 2>/dev/null || echo ''"
            results = run_remote_scripts_parallel(peers, script, timeout=15, quiet=True, **action.ssh_kwargs)
            by_host = {result.host: result for result in results}
            if any(peer not in by_host or not by_host[peer].success for peer in peers):
                return SetupActionResult(host, FAIL, "could not collect keys from every original mesh peer")
            keys = set()
            for result in results:
                if result.stdout.strip():
                    parts = result.stdout.strip().splitlines()[0].split()
                    if len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9+/=]+", parts[1]):
                        return SetupActionResult(host, FAIL, "mesh peer returned an invalid public key")
                    keys.add(parts[1])
            key_data = sorted(keys)
        if not key_data:
            return SetupActionResult(host, SKIP, "no mesh keys available; removal is unconfirmed")
        script = "#!/bin/bash\nset -euo pipefail\nif [ -f ~/.ssh/authorized_keys ]; then\n"
        script += "\n".join("sed -i '\\|%s|d' ~/.ssh/authorized_keys" % key for key in key_data)
        script += "\nfi\necho MESH_KEYS_REMOVED=1\n"
        return _result(host, run_remote_script(host, script, timeout=30, quiet=True, **action.ssh_kwargs))

    callbacks = {
        "earlyoom": undo_earlyoom,
        "sudoers": undo_sudoers,
        "docker_group": undo_docker_group,
        "cx7": undo_cx7,
        "ssh_mesh": undo_mesh,
    }
    # Preserve built-in tie ordering within the shared reverse dependency walk.
    return {
        key: SetupStep(key, key.replace("_", " "), undo=callbacks[key], requires_sudo=key != "ssh_mesh", order=-index)
        for index, key in enumerate(BUILTIN_UNDO_ORDER)
    }
