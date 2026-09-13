"""Resolve implicit launch principals without connecting to workload hosts."""

import subprocess

from sparkrun.orchestration.ssh import _local_user, build_ssh_cmd, should_run_locally


def resolve_ssh_user(hosts, *, ssh_user=None, ssh_key=None, ssh_options=None):
    """Resolve one principal for the current single-user cluster contract.

    Local dispatch uses the OS identity. Remote defaults come from OpenSSH's
    effective configuration, including aliases and configured -F/-o options.
    Different implicit users across hosts require an explicit cluster user;
    guessing one would redirect part of the launch into another namespace.
    """
    # build_ssh_cmd makes an explicit principal authoritative for every
    # transport consumer, independently of aliases, options and rotated keys.
    if ssh_user:
        return ssh_user
    users = set()
    for host in dict.fromkeys(hosts):
        if should_run_locally(host, ssh_user):
            users.add(_local_user())
            continue
        cmd = build_ssh_cmd(host, ssh_user=ssh_user, ssh_key=ssh_key, ssh_options=ssh_options)
        try:
            result = subprocess.run([cmd[0], "-G", *cmd[1:]], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
            user = next((line.split(None, 1)[1].strip() for line in result.stdout.splitlines() if line.startswith("user ")), "")
            if result.returncode or not user:
                raise ValueError("OpenSSH did not return an effective user")
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ValueError("Cannot resolve SSH user for %r; configure cluster.user or ssh.user" % host) from error
        if "@" in host and not ssh_user:
            raise ValueError("For user-scoped launches, supply the user through cluster.user or ssh.user instead of a user@host address")
        users.add(user)
    if len(users) != 1:
        raise ValueError("A user-scoped launch requires one effective SSH user across its hosts; configure cluster.user or ssh.user")
    return users.pop()
