#!/bin/bash
# nv-monitor wrapper script — runs nv-monitor + prom2json on a remote host
# and outputs one JSON line per poll cycle to stdout.
#
# Usage: bash -s -- <port> <interval> < this_script
#
# Output format (one JSON object per line):
#   {"metrics":[...],"sparkrun_jobs":"2","sparkrun_job_names":"container1|container2"}

set -euo pipefail

PORT=${1:-29110}
INTERVAL=${2:-2}

NV_MONITOR="$HOME/.cache/@CACHE_NAMESPACE@/bin/nv-monitor"
PROM2JSON="$HOME/.cache/@CACHE_NAMESPACE@/bin/prom2json"

# Verify binaries exist
if [ ! -x "$NV_MONITOR" ]; then
    echo '{"error":"nv-monitor binary not found"}' >&2
    exit 1
fi
if [ ! -x "$PROM2JSON" ]; then
    echo '{"error":"prom2json binary not found"}' >&2
    exit 1
fi

# Kill any existing nv-monitor on this port
pkill -f "nv-monitor.*-p $PORT" 2>/dev/null || true
sleep 0.3

# Start nv-monitor in background
"$NV_MONITOR" -n -p "$PORT" &
NV_PID=$!

# Clean up nv-monitor on any exit (SSH disconnect, signal, etc.)
cleanup() {
    kill "$NV_PID" 2>/dev/null || true
    wait "$NV_PID" 2>/dev/null || true
}
trap cleanup EXIT HUP TERM INT

# Wait for nv-monitor to bind the port
for _i in 1 2 3 4 5; do
    if curl -sf --max-time 1 "http://localhost:$PORT/metrics" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

# Polling loop — outputs one JSON line per cycle
while kill -0 "$NV_PID" 2>/dev/null; do
    METRICS=$(curl -sf --max-time 2 "http://localhost:$PORT/metrics" 2>/dev/null | "$PROM2JSON" 2>/dev/null) || true
    if [ -n "$METRICS" ]; then
        # Docker container info
        CONTAINERS=$(docker ps --filter "name=^@RESOURCE_NAMESPACE@_" --format "{{.Names}}" 2>/dev/null | paste -sd"|" -) || true
        COUNT=0
        if [ -n "$CONTAINERS" ]; then
            COUNT=$(echo "$CONTAINERS" | tr '|' '\n' | grep -c . 2>/dev/null) || true
        fi
        # Single JSON line to stdout
        printf '{"metrics":%s,"sparkrun_jobs":"%s","sparkrun_job_names":"%s"}\n' \
            "$METRICS" "$COUNT" "$CONTAINERS"
    fi
    sleep "$INTERVAL"
done

echo '{"error":"nv-monitor process exited"}' >&2
exit 1
