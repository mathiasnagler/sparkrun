#!/bin/bash
# Mandatory, read-only OS facts used to select setup steps.
# --- Host setup prerequisites ---
command -v apt-get >/dev/null 2>&1 && echo "CHECK_APT=1" || echo "CHECK_APT=0"
[ -d /run/systemd/system ] && echo "CHECK_SYSTEMD=1" || echo "CHECK_SYSTEMD=0"
command -v netplan >/dev/null 2>&1 && echo "CHECK_NETPLAN=1" || echo "CHECK_NETPLAN=0"
echo "CHECK_OS=$(uname -s)"

echo "CHECK_DISCOVERY_COMPLETE=1"
