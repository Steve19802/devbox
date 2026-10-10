#!/bin/sh
# devbox-proc.sh - in-container process supervisor for the DevBox MCP server.
#
# PURPOSE
#   Supervise detached background processes and expose a small, machine-readable
#   interface over them. This script is streamed to the target container over
#   stdin, it is never installed on disk there:
#
#       docker exec -i [-e VAR=...] <container> sh -s -- <subcmd> [args] < devbox-proc.sh
#
#   Consequences:
#     * Must be strict POSIX sh (dash / bash --posix / busybox ash). No arrays,
#       no [[ ]], no $'..', no echo -e, no GNU-only flags.
#     * Because the script itself is read from stdin, EVERY background/detached
#       child must have its stdin redirected from /dev/null, otherwise it would
#       swallow the remaining commands on the script's stdin.
#     * Detached children must also have stdout/stderr redirected away from the
#       exec pipes so that `docker exec ... start` returns immediately.
#
# STATE DIR
#   ${DEVBOX_PROC_DIR:-/tmp/devbox-proc}/<id>/  (created with umask 077)
#       cmd         raw supervised command line
#       name        display name
#       started_at  epoch seconds
#       pid         pid of the session leader (== process group id)
#       log         combined stdout+stderr, appended by the writer
#       log_base    bytes dropped by log rotation (absolute offset base, starts 0)
#       exitcode    written once the supervised command has finished
#
# SUBCOMMANDS
#   start <id>                     env: DEVBOX_CMD (required),
#                                       DEVBOX_PROC_NAME (optional),
#                                       DEVBOX_MAX_LOG_BYTES (optional, default 10485760)
#   ps                             one JSON object per line, sorted by started_at
#   logs <id> <tail_lines> [offset]  JSON header line, then raw log bytes
#   stop <id> <grace_secs>         TERM the group, KILL after grace; prints JSON record
#   fg <token>                     run DEVBOX_CMD in the foreground (execute_container_command)
#   fgkill <token> <grace_secs>    kill the process group of a foreground run
#   count                          number of running supervised processes
#
# EXIT CODES
#   0 ok | 2 usage/invalid | 3 not found | 4 already exists | 5 missing dependency
#   Errors are printed to stderr as one line: devbox-proc: <message>

umask 077

STATE_ROOT=${DEVBOX_PROC_DIR:-/tmp/devbox-proc}

err() {
    printf 'devbox-proc: %s\n' "$1" >&2
}

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

# short sleep that also works where fractional sleep is unsupported
snooze() {
    sleep 0.1 2>/dev/null || sleep 1
}

# echo the path of a fresh temp file (caller removes it)
make_tmp() {
    _t=$(mktemp "${TMPDIR:-/tmp}/devbox-proc.XXXXXX" 2>/dev/null) && {
        printf '%s' "$_t"
        return 0
    }
    _t="${TMPDIR:-/tmp}/devbox-proc.$$"
    : > "$_t"
    printf '%s' "$_t"
}

# read a whole (possibly multi-line) state file; empty if missing
read_field() {
    if [ -f "$1" ]; then
        cat "$1" 2>/dev/null
    fi
}

# ^[a-z0-9][a-z0-9-]{0,39}$  -> 0 ok, 1 invalid
validate_id() {
    case "$1" in
        '') return 1 ;;
    esac
    case "$1" in
        [!a-z0-9]*) return 1 ;;
    esac
    case "$1" in
        *[!a-z0-9-]*) return 1 ;;
    esac
    [ "${#1}" -le 40 ] || return 1
    return 0
}

need_id() {
    if ! validate_id "$1"; then
        err "invalid id '$1'"
        exit 2
    fi
}

# JSON string escaper: stdin -> stdout, no trailing newline added.
# Escapes backslash, double quote, and encodes newline/tab/CR as \n \t \r;
# other control characters are stripped.
json_escape() {
    _tab=$(printf '\t')
    _cr=$(printf '\r')
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' \
        -e "s/$_tab/\\\\t/g" -e "s/$_cr/\\\\r/g" \
        | tr -d '\000-\010\013\014\016-\037\177' \
        | awk 'BEGIN { ORS = "" } { if (NR > 1) printf "\\n"; printf "%s", $0 }'
}

# status of a supervised process: running | exited | lost
status_of() {
    if [ -f "$1/exitcode" ]; then
        printf 'exited'
        return
    fi
    _p=$(read_field "$1/pid")
    if [ -n "$_p" ] && kill -0 "$_p" 2>/dev/null; then
        printf 'running'
    else
        printf 'lost'
    fi
}

# emit the `ps` JSON record for one id
emit_record() {
    _id=$1
    _dir=$STATE_ROOT/$_id
    _name=$(read_field "$_dir/name")
    _cmd=$(read_field "$_dir/cmd")
    _pid=$(read_field "$_dir/pid")
    [ -n "$_pid" ] || _pid=0
    _started=$(read_field "$_dir/started_at")
    [ -n "$_started" ] || _started=0
    _status=$(status_of "$_dir")
    if [ "$_status" = exited ]; then
        _ec=$(read_field "$_dir/exitcode")
        [ -n "$_ec" ] || _ec=0
    else
        _ec=null
    fi
    _now=$(date +%s)
    _uptime=$((_now - _started))
    [ "$_uptime" -ge 0 ] 2>/dev/null || _uptime=0
    printf '{"id":"%s","name":"%s","cmd":"%s","pid":%s,"status":"%s","exit_code":%s,"started_at":%s,"uptime":%s}\n' \
        "$(printf '%s' "$_id" | json_escape)" \
        "$(printf '%s' "$_name" | json_escape)" \
        "$(printf '%s' "$_cmd" | json_escape)" \
        "$_pid" "$_status" "$_ec" "$_started" "$_uptime"
}

# signal a whole process group, fall back to the bare pid
send_sig() {
    # $1 = signal name/number, $2 = pgid/pid
    kill -"$1" -"$2" 2>/dev/null || kill -"$1" "$2" 2>/dev/null
}

# TERM a process group, then KILL after grace seconds.
# $1 = pgid, $2 = grace seconds, $3 = optional sentinel file (e.g. exitcode):
# when the sentinel appears the supervised leader has already recorded its exit
# and we stop waiting early. kill -0 keeps reporting a process that is only a
# zombie, so the sentinel is what lets stop return promptly on real containers
# whose PID 1 does not reap orphans.
terminate_group() {
    _pg=$1
    _grace=$2
    _sentinel=${3:-}
    send_sig TERM "$_pg"
    _i=0
    while [ "$_i" -lt "$_grace" ]; do
        if [ -n "$_sentinel" ] && [ -f "$_sentinel" ]; then
            break
        fi
        kill -0 -"$_pg" 2>/dev/null || break
        sleep 1
        _i=$((_i + 1))
    done
    # Force-kill anything still in the group (no-op when it is already empty).
    kill -0 -"$_pg" 2>/dev/null && send_sig KILL "$_pg"
    return 0
}

# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

# The runner is a session leader (setsid), so its $$ is the process group id.
# Values (dir, max bytes) are passed as positional args; the command itself is
# taken from the inherited DEVBOX_CMD environment variable, never interpolated.
RUNNER='
dir=$1
max=$2
printf "%s" "$$" > "$dir/pid"
(
  while :; do
    sleep 5
    sz=$(wc -c < "$dir/log" 2>/dev/null)
    [ -n "$sz" ] || sz=0
    if [ "$sz" -gt "$max" ]; then
      keep=$((sz / 2))
      tail -c "$keep" "$dir/log" > "$dir/.rotate" 2>/dev/null
      cat "$dir/.rotate" > "$dir/log" 2>/dev/null
      rm -f "$dir/.rotate"
      base=$(cat "$dir/log_base" 2>/dev/null)
      [ -n "$base" ] || base=0
      printf "%s" "$((base + sz - keep))" > "$dir/log_base"
    fi
  done
) &
wd=$!
finish() {
  kill "$wd" 2>/dev/null
  printf "143" > "$dir/exitcode"
  exit 143
}
trap finish TERM
trap finish INT
sh -c "$DEVBOX_CMD" </dev/null >>"$dir/log" 2>&1
rc=$?
printf "%s" "$rc" > "$dir/exitcode"
kill "$wd" 2>/dev/null
trap - TERM INT
kill -TERM -$$ 2>/dev/null
exit 0
'

do_start() {
    [ $# -ge 1 ] || { err "usage: start <id>"; exit 2; }
    _id=$1
    need_id "$_id"
    if [ -z "${DEVBOX_CMD:-}" ]; then
        err "DEVBOX_CMD is required"
        exit 2
    fi
    command -v setsid >/dev/null 2>&1 || { err "setsid not available"; exit 5; }

    mkdir -p "$STATE_ROOT" 2>/dev/null
    _dir=$STATE_ROOT/$_id
    if ! mkdir "$_dir" 2>/dev/null; then
        err "process '$_id' already exists"
        exit 4
    fi

    printf '%s' "$DEVBOX_CMD" > "$_dir/cmd"
    printf '%s' "${DEVBOX_PROC_NAME:-$_id}" > "$_dir/name"
    date +%s > "$_dir/started_at"
    : > "$_dir/log"
    printf '0' > "$_dir/log_base"

    _max=${DEVBOX_MAX_LOG_BYTES:-10485760}
    case "$_max" in '' | *[!0-9]*) _max=10485760 ;; esac

    # Fully detach: new session, stdin from /dev/null, output away from pipes.
    setsid sh -c "$RUNNER" devbox-proc-runner "$_dir" "$_max" </dev/null >/dev/null 2>&1 &

    # Make sure pid is recorded before we report (poll up to ~2s).
    _n=0
    while [ ! -s "$_dir/pid" ] && [ "$_n" -lt 20 ]; do
        snooze
        _n=$((_n + 1))
    done

    emit_record "$_id"
    exit 0
}

# ---------------------------------------------------------------------------
# ps
# ---------------------------------------------------------------------------

do_ps() {
    _tmp=$(make_tmp)
    if [ -d "$STATE_ROOT" ]; then
        for _d in "$STATE_ROOT"/*; do
            [ -d "$_d" ] || continue
            _b=${_d##*/}
            case "$_b" in .*) continue ;; esac
            _sa=$(read_field "$_d/started_at")
            [ -n "$_sa" ] || _sa=0
            printf '%s\t%s\n' "$_sa" "$_d" >> "$_tmp"
        done
    fi
    sort -n -k1,1 "$_tmp" | while IFS='	' read -r _sa _d; do
        emit_record "${_d##*/}"
    done
    rm -f "$_tmp"
    exit 0
}

# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

do_logs() {
    [ $# -ge 2 ] || { err "usage: logs <id> <tail_lines> [offset]"; exit 2; }
    _id=$1
    _lines=$2
    _offset=${3:-}
    need_id "$_id"
    case "$_lines" in '' | *[!0-9]*) err "invalid tail_lines '$_lines'"; exit 2 ;; esac
    if [ -n "$_offset" ]; then
        case "$_offset" in *[!0-9]*) err "invalid offset '$_offset'"; exit 2 ;; esac
    fi

    _dir=$STATE_ROOT/$_id
    [ -d "$_dir" ] || { err "process '$_id' not found"; exit 3; }

    _base=$(read_field "$_dir/log_base")
    [ -n "$_base" ] || _base=0
    _size=$(wc -c < "$_dir/log" 2>/dev/null)
    [ -n "$_size" ] || _size=0
    _next=$((_base + _size))
    _status=$(status_of "$_dir")

    printf '{"id":"%s","status":"%s","next_offset":%s,"log_base":%s}\n' \
        "$(printf '%s' "$_id" | json_escape)" "$_status" "$_next" "$_base"

    if [ -n "$_offset" ]; then
        _pos=$((_offset - _base))
        [ "$_pos" -lt 0 ] && _pos=0
        # tail -c is 1-based; +N starts at byte N. Cap at the size measured for
        # the header so a growing log never returns bytes beyond next_offset.
        _len=$((_size - _pos))
        if [ "$_len" -gt 0 ]; then
            tail -c +$((_pos + 1)) "$_dir/log" 2>/dev/null | head -c "$_len"
        fi
    else
        tail -n "$_lines" "$_dir/log" 2>/dev/null
    fi
    exit 0
}

# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

do_stop() {
    [ $# -ge 2 ] || { err "usage: stop <id> <grace_secs>"; exit 2; }
    _id=$1
    _grace=$2
    need_id "$_id"
    case "$_grace" in '' | *[!0-9]*) err "invalid grace '$_grace'"; exit 2 ;; esac

    _dir=$STATE_ROOT/$_id
    [ -d "$_dir" ] || { err "process '$_id' not found"; exit 3; }

    _pid=$(read_field "$_dir/pid")
    if [ -n "$_pid" ] && [ ! -f "$_dir/exitcode" ]; then
        terminate_group "$_pid" "$_grace" "$_dir/exitcode"
    fi

    emit_record "$_id"
    exit 0
}

# ---------------------------------------------------------------------------
# fg / fgkill
# ---------------------------------------------------------------------------

do_fg() {
    [ $# -ge 1 ] || { err "usage: fg <token>"; exit 2; }
    _token=$1
    need_id "$_token"
    if [ -z "${DEVBOX_CMD:-}" ]; then
        err "DEVBOX_CMD is required"
        exit 2
    fi
    command -v setsid >/dev/null 2>&1 || { err "setsid not available"; exit 5; }

    _fgdir=$STATE_ROOT/.fg
    mkdir -p "$_fgdir" 2>/dev/null
    _tf=$_fgdir/$_token

    # A background child of a non-interactive shell is not a group leader, so
    # setsid execs in place and $! is the session/pgid. stdout/stderr pass through.
    setsid sh -c "$DEVBOX_CMD" </dev/null &
    _pid=$!
    printf '%s' "$_pid" > "$_tf"
    trap 'rm -f "$_tf"' EXIT HUP INT TERM

    wait "$_pid"
    _rc=$?
    rm -f "$_tf"
    exit "$_rc"
}

do_fgkill() {
    [ $# -ge 2 ] || { err "usage: fgkill <token> <grace_secs>"; exit 2; }
    _token=$1
    _grace=$2
    need_id "$_token"
    case "$_grace" in '' | *[!0-9]*) err "invalid grace '$_grace'"; exit 2 ;; esac

    _tf=$STATE_ROOT/.fg/$_token
    [ -f "$_tf" ] || { err "foreground process '$_token' not found"; exit 3; }
    _pid=$(read_field "$_tf")
    [ -n "$_pid" ] || { err "foreground process '$_token' not found"; exit 3; }

    terminate_group "$_pid" "$_grace"
    exit 0
}

# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------

do_count() {
    _n=0
    if [ -d "$STATE_ROOT" ]; then
        for _d in "$STATE_ROOT"/*; do
            [ -d "$_d" ] || continue
            _b=${_d##*/}
            case "$_b" in .*) continue ;; esac
            [ -f "$_d/exitcode" ] && continue
            _p=$(read_field "$_d/pid")
            [ -n "$_p" ] || continue
            if kill -0 "$_p" 2>/dev/null; then
                _n=$((_n + 1))
            fi
        done
    fi
    printf '%s\n' "$_n"
    exit 0
}

# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

if [ $# -lt 1 ]; then
    err "missing subcommand"
    exit 2
fi

_sub=$1
shift

case "$_sub" in
    start) do_start "$@" ;;
    ps) do_ps "$@" ;;
    logs) do_logs "$@" ;;
    stop) do_stop "$@" ;;
    fg) do_fg "$@" ;;
    fgkill) do_fgkill "$@" ;;
    count) do_count "$@" ;;
    *)
        err "unknown subcommand '$_sub'"
        exit 2
        ;;
esac
