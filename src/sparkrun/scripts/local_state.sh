# LocalExecutor state acquisition. 0 = present/alive, 1 = confirmed absent/dead,
# 2 = failed acquisition or invalid data. Callers must inspect all three values.
# These functions run in generated subshells; they do not change shell options.

_sr_local_error() {
    printf 'Cannot inspect native state: %s\n' "$*" >&2
    return 2
}

_sr_local_directory() {
    if [ -d "$1" ]; then
        [ -r "$1" ] && [ -x "$1" ] && ls -A -- "$1" >/dev/null && return 0
        _sr_local_error "directory $1 is not readable/searchable"
        return 2
    fi
    # A failed stat alone cannot prove absence: inspect the parent namespace.
    local rc
    if _sr_local_entry "$1"; then
        _sr_local_error "$1 is not an accessible directory"
        return 2
    else
        rc=$?
        return "$rc"
    fi
}

_sr_local_entry() (
    local parent leaf entry rc
    parent=$(dirname -- "$1") || return 2
    leaf=$(basename -- "$1") || return 2
    if [ "$parent" = "$1" ] || [ "$leaf" = . ] || [ "$leaf" = .. ]; then
        _sr_local_error "cannot traverse $1"
        return 2
    fi
    if _sr_local_directory "$parent"; then :; else
        rc=$?
        return "$rc"
    fi
    # Include dangling links and entries whose stat failed. Confirm absence by
    # enumerating a readable parent, not by interpreting a failed file read.
    shopt -s nullglob dotglob
    for entry in "$parent"/*; do
        [ "${entry##*/}" = "$leaf" ] && return 0
    done
    return 1
)

_sr_local_read() {
    local rc pattern
    _sr_value=
    if [ ! -f "$1" ]; then
        if _sr_local_entry "$1"; then
            _sr_local_error "$1 is not a regular readable file"
            return 2
        else
            rc=$?
            return "$rc"
        fi
    fi
    case "$2" in
        pid) pattern='[1-9][0-9]{0,9}' ;;
        owner) pattern='[a-z][a-z0-9-]{0,47}' ;;
    esac
    # Check bytes before command substitution (which strips NULs). A damaged
    # record must not become a valid PID or owner after shell text conversion.
    if LC_ALL=C grep -a -q -v -x -E "$pattern" -- "$1"; then
        _sr_local_error "invalid $2 record in $1"
        return 2
    else
        rc=$?
        [ "$rc" -eq 1 ] || { _sr_local_error "cannot read $1"; return 2; }
    fi
    if ! _sr_value=$(cat -- "$1"); then
        _sr_local_error "cannot read $1"
        return 2
    fi
    if [[ ! $_sr_value =~ ^$pattern$ ]]; then
        _sr_local_error "invalid $2 record in $1"
        return 2
    fi
    # PID 1 cannot be our launched child; negating it would signal all processes.
    if [ "$2" = pid ] && (( _sr_value <= 1 || _sr_value > 2147483647 )); then
        _sr_local_error "invalid PID in $1"
        return 2
    fi
    return 0
}

_sr_local_state() {
    local rc
    _sr_pid= _sr_owner= _sr_pid_present=0 _sr_owner_present=0
    if _sr_local_read "$1" pid; then
        _sr_pid=$_sr_value
        _sr_pid_present=1
    else
        rc=$?
        [ "$rc" -eq 1 ] || return "$rc"
    fi
    if _sr_local_read "$1.owner" owner; then
        _sr_owner=$_sr_value
        _sr_owner_present=1
    else
        rc=$?
        [ "$rc" -eq 1 ] || return "$rc"
    fi
    return 0
}

# kill -0 can distinguish ESRCH from EPERM even when process listings are
# restricted. Both process IDs and negative process-group IDs are supported.
_sr_local_exists() {
    local detail
    if detail=$(LC_ALL=C kill -0 -- "$1" 2>&1); then
        return 0
    fi
    if [[ $detail == *'No such process' ]]; then
        return 1
    fi
    _sr_local_error "cannot establish liveness for $1: $detail"
    return 2
}

_sr_local_workload_exists() {
    local rc present=1 target
    for target in "-$1" "$1"; do
        if _sr_local_exists "$target"; then
            present=0
        else
            rc=$?
            [ "$rc" -eq 1 ] || return "$rc"
        fi
    done
    return "$present"
}

_sr_local_alive() {
    [ -n "$1" ] || return 1
    local rc rows pid pgid state extra found=0
    if _sr_local_workload_exists "$1"; then :; else
        rc=$?
        return "$rc"
    fi
    # setsid launches PID == PGID. Inspect the whole group even when its leader
    # is dead. Matching the PID as well retains legacy single-process recovery
    # without signalling the unrelated process group that contains that PID.
    if ! rows=$(LC_ALL=C ps -e -o pid=,pgid=,stat=); then
        _sr_local_error "cannot read process group status for PID $1"
        return 2
    fi
    while read -r pid pgid state extra; do
        if [ "$pid" != "$1" ] && [ "$pgid" != "$1" ]; then continue; fi
        if [[ ! $pid =~ ^[1-9][0-9]*$ || ! $pgid =~ ^[1-9][0-9]*$ || ! $state =~ ^[A-Za-z] ]] || [ -n "$extra" ]; then
            _sr_local_error "invalid process group status for PID $1"
            return 2
        fi
        found=1
        [[ $state == Z* || $state == X* ]] || return 0
    done <<< "$rows"
    # A group containing only exited zombies is stopped, even if not yet reaped.
    [ "$found" -eq 1 ] && return 1
    # A listing can race exit or hide processes. Only confirmed absence is dead.
    if _sr_local_workload_exists "$1"; then
        _sr_local_error "cannot find process group status for PID $1"
        return 2
    else
        rc=$?
        return "$rc"
    fi
}

# Stop either a recorded workload or a newly spawned child being rolled back.
# _sr_was_running is the count contribution; success always verifies absence.
_sr_local_stop() {
    local pid=$1 rc i
    _sr_was_running=0
    if _sr_local_alive "$pid"; then
        _sr_was_running=1
        kill -TERM -- -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
        for i in 1 2 3 4 5 6 7 8 9 10; do
            if _sr_local_alive "$pid"; then :; else
                rc=$?; [ "$rc" -eq 1 ] && break; return "$rc"
            fi
            sleep 1
        done
        if _sr_local_alive "$pid"; then
            kill -KILL -- -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
            for i in 1 2 3 4 5 6 7 8 9 10; do
                if _sr_local_alive "$pid"; then :; else
                    rc=$?; [ "$rc" -eq 1 ] && break; return "$rc"
                fi
                sleep 0.1
            done
        else rc=$?; [ "$rc" -eq 1 ] || return "$rc"; fi
    else rc=$?; [ "$rc" -eq 1 ] || return "$rc"; fi
    if _sr_local_alive "$pid"; then
        printf 'Native workload still present: %s\n' "${2:-PID/group $pid}" >&2
        return 1
    else rc=$?; [ "$rc" -eq 1 ] || return "$rc"; fi
    return 0
}

# Write in the destination directory, then replace only with a complete record.
# Failed writes leave an existing record intact; pending files are not PID files.
_sr_local_write_record() {
    local temporary
    temporary=$(mktemp -- "$2.pending.XXXXXX") || return 1
    if printf %s "$1" > "$temporary" && mv -fT -- "$temporary" "$2"; then
        return 0
    fi
    rm -f -- "$temporary" || printf 'Cannot remove pending native record: %s\n' "$temporary" >&2
    return 1
}

_sr_local_commit_pid() {
    if _sr_local_write_record "$1"$'\n' "$2"; then return 0; fi
    printf 'Cannot persist native PID %s in %s; rolling back launch\n' "$1" "$2" >&2
    if _sr_local_stop "$1"; then
        # The launcher owns this child; reap it after verified group shutdown.
        wait "$1" 2>/dev/null || true
        printf 'Rolled back native launch: PID/group %s\n' "$1" >&2
    else
        printf 'Native launch rollback could not be confirmed: PID/group %s; manual recovery required\n' "$1" >&2
    fi
    return 1
}
