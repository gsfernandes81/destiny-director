#!/usr/bin/env bash
# Claude Remote Control supervisor for the Pi dev container. Baked into the image at
# /home/dev/rc-supervisor.sh and started by the entrypoint ONLY under
# DD_REMOTE_CONTROL=1, in the background behind sshd — which is the foreground process
# and the one whose lifetime the container's lifetime equals. This script loops forever
# (it re-launches the daemon on exit), but nothing depends on that any more: it is a
# daemon you turned on, not the thing keeping the container up. Its output goes to the
# log below rather than to `docker logs`, which is sshd's.
#
# It runs `claude remote-control --spawn worktree --no-create-session-in-dir` as a
# long-lived service so you can drive this container from claude.ai/code or the Claude
# mobile app, with nothing to type in a `docker exec`.
#
# WHY A REAL SUPERVISOR (not just `cmd || restart`): Claude Code's remote-control
# server has a known class of upstream hangs where the PROCESS STAYS ALIVE but wedges
# and stops accepting new sessions (anthropics/claude-code#51267, #40416, #37321 —
# "remote becomes unresponsive, can't start a new session"). A restart-on-exit loop
# never recovers that, because the process never exits. So this watchdog also RECYCLES
# a daemon that is alive-but-wedged — but only when doing so is free.
#
# THE SAFETY CONSTRAINT (deliberate, and the whole point): recycle ONLY at a literal
# 0/32 sessions. A single idle-but-attached session (1/32 doing nothing) must NOT
# trigger a recycle — killing it forces a painful remote session recovery. So a wedged
# daemon that still holds a live session is LEFT ALONE until that session ends. We
# never trade away live work to unstick the server.
#
# THE POLICY: a freshly started daemon is exempt until it has actually served ≥1
# session (a brand-new daemon isn't wedged, and this stops a perpetually-idle daemon
# from being churned). Once it has been used and then drops to 0 sessions, we give it
# RC_IDLE_RECYCLE_SECS of *continuous* idle and then recycle it exactly ONCE. Net
# effect: whenever you return after an idle gap you meet a fresh, unwedged daemon,
# but an untouched daemon is never restarted for no reason.
#
# NOTE ON PERMISSION MODE: we run with the DEFAULT permission mode (prompts kept on),
# per the maintainer's choice. The upside is no blanket auto-approve; the tradeoff is
# that a permission prompt awaiting a local keypress can itself wedge a daemon *while a
# session is live* (#51267) — and per the constraint above we will not kill that live
# session to recover it. That wedge clears on its own once the session ends (then the
# idle recycle fires). Set RC_PERMISSION_MODE to override (e.g. acceptEdits).
set -u

LOG="$HOME/.local/share/remote-control.log"
mkdir -p "$(dirname "$LOG")"

RC_POLL_SECS=${RC_POLL_SECS:-30}            # how often to sample the session count
RC_IDLE_RECYCLE_SECS=${RC_IDLE_RECYCLE_SECS:-300}   # continuous 0/32 before recycle
RC_REPO=${RC_REPO:-/workspace}              # repo where --spawn worktree operates
RC_PERMISSION_MODE=${RC_PERMISSION_MODE:-}  # empty => daemon default (keep prompting)
RC_LOG_MAX_BYTES=${RC_LOG_MAX_BYTES:-5242880}  # rotate the logfile past this (5MiB)

# --- log plumbing -------------------------------------------------------------------
# Our stdout/stderr USED to be `docker logs`, back when this ran as the container's
# foreground process, and it kept the live Claude TUI verbatim for that reason. sshd is
# the foreground process now and `docker logs` is its; the entrypoint starts us under
# setsid with stdout on /dev/null, so the verbatim stream goes nowhere and the raw
# passthrough below is vestigial. It is left intact rather than ripped out because it
# costs nothing and is what makes this script still readable when run by hand from a
# `docker exec` — which is the one context that gets the TUI back.
#
# The PERSISTED logfile is therefore the whole of the record now, and the only way to
# read it is `make dev-rc-log`. It is the forensic record you grep from a
# `docker exec` days later. Teeing the raw stream into it was wrong on both counts — the
# TUI repaints itself every few seconds, so the file grew ~5MB/day unbounded AND buried
# the handful of real supervisor lines under megabytes of cursor-control escapes.
#
# So the logfile gets a cleaned, de-duplicated, size-rotated view:
#   * ANSI CSI/OSC sequences stripped, blank lines dropped;
#   * `[rc-supervisor]` lines ALWAYS kept — they are the state changes we care about;
#   * every other line kept only if it isn't a repeat of one already seen, which
#     collapses the TUI repaint block to a single copy. This is deliberately keyed on
#     the line text rather than on matching known TUI wording, so an upstream rewording
#     of the TUI can't silently start leaking noise back in.
#   * the dedupe table is CLEARED on each supervisor line (i.e. on a real state change)
#     and capped, so a genuine error recurring later still gets recorded rather than
#     being suppressed forever by a match from hours ago.
#   * suppressed repeats are counted and reported, so the file never implies a quiet
#     period that wasn't.
# Rotation keeps one previous generation ($LOG.1) — enough to span a recycle or two
# without the file ever being a disk risk on the Pi.
#
# NOTE the plumbing shape below: `tee` still owns the docker-logs path exactly as before,
# and the filter hangs off tee's SECOND output. The filter is line-oriented (awk), so
# putting it inline would hold a partial line — a spinner or prompt that repaints without
# a trailing newline — until the line completed, making the live TUI feel laggy. Keeping
# tee in front means the interactive path is byte-for-byte unchanged and only the file
# branch pays for filtering.
rc_log_to_file() {
  awk -v logf="$LOG" -v maxbytes="$RC_LOG_MAX_BYTES" '
    function emit(line) {
      if (bytes + length(line) + 1 > maxbytes) {   # rotate BEFORE overflowing
        close(logf)
        system("mv -f \"" logf "\" \"" logf ".1\" 2>/dev/null")
        bytes = 0
      }
      print line >> logf
      fflush(logf)
      bytes += length(line) + 1
    }
    function flush_suppressed() {
      if (nsupp > 0) { emit("  (suppressed " nsupp " repeated TUI line(s))"); nsupp = 0 }
    }
    BEGIN {
      # Start accounting from the file as it already is, so an oversized log left by a
      # previous build rotates away on the first write instead of growing further.
      # `wc -c <file>` (not a redirect) so wc itself reports a missing file to the
      # stderr we are discarding, rather than the shell announcing it on ours.
      cmd = "wc -c \"" logf "\" 2>/dev/null"
      if ((cmd | getline line) > 0) { split(line, f, " "); bytes = f[1] + 0 }
      close(cmd)
      nseen = 0; nsupp = 0
    }
    {
      line = $0
      gsub(/\033\][^\007\033]*(\007|\033\\)/, "", line)   # OSC (e.g. the ]8;; links)
      gsub(/\033\[[0-9;?]*[ -\/]*[@-~]/, "", line)        # CSI (cursor moves, colour)
      gsub(/\r/, "", line)
      sub(/[ \t]+$/, "", line)
      if (line ~ /^[ \t]*$/) next
      if (line ~ /\[rc-supervisor\]/) {
        flush_suppressed()
        delete seen; nseen = 0                 # state change → forget the old repaints
        emit(line)
        next
      }
      if (line in seen) { nsupp++; next }
      if (nseen >= 500) { delete seen; nseen = 0 }        # bound the table
      seen[line] = 1; nseen++
      flush_suppressed()
      emit(line)
    }
    END { flush_suppressed() }
  '
}

exec > >(tee >(rc_log_to_file) ) 2>&1

log() { printf '%s [rc-supervisor] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# is_descendant <pid> <ancestor> — walk the PPID chain (bounded) so we only ever count
# sessions belonging to OUR daemon, never an unrelated `claude` from a `docker exec`.
is_descendant() {
  local p=$1 anc=$2 i=0 pp
  while [ "${p:-0}" -gt 1 ] && [ "$i" -lt 20 ]; do
    pp=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
    [ -z "$pp" ] && return 1
    [ "$pp" = "$anc" ] && return 0
    p=$pp; i=$((i + 1))
  done
  return 1
}

# session_count <daemon_pid> — how many sessions this daemon is running (the "X" in
# X/32). Measured TWO independent ways; we take the MAX so we can only ever OVER-count,
# never UNDER-count. Under-counting would mean recycling while a session is live —
# exactly the outcome the safety constraint forbids.
#   (1) `claude agents --json` entries whose pid descends from the daemon. This reads
#       LOCAL state, so it can't hang on a wedged daemon (but could go stale).
#   (2) session-helper processes (`claude.exe --sdk-url .../sessions/`) that descend
#       from the daemon — the backstop if (1) is stale.
session_count() {
  local rc=$1 n_agents=0 n_procs=0 pid
  if command -v python3 >/dev/null 2>&1; then
    for pid in $(claude agents --json 2>/dev/null | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    data = []
for item in data if isinstance(data, list) else []:
    if isinstance(item, dict) and item.get("pid"):
        print(item["pid"])
' 2>/dev/null); do
      is_descendant "$pid" "$rc" && n_agents=$((n_agents + 1))
    done
  fi
  for pid in $(pgrep -f -- '--sdk-url .*/sessions/' 2>/dev/null); do
    is_descendant "$pid" "$rc" && n_procs=$((n_procs + 1))
  done
  [ "$n_agents" -ge "$n_procs" ] && echo "$n_agents" || echo "$n_procs"
}

# kill_daemon <pid> — reap the daemon and its whole process group. Only ever called at
# 0 sessions, so there is no live session helper to lose.
kill_daemon() {
  local rc=$1 _
  kill -TERM -"$rc" 2>/dev/null || kill -TERM "$rc" 2>/dev/null || true
  for _ in $(seq 1 10); do kill -0 "$rc" 2>/dev/null || return 0; sleep 1; done
  kill -KILL -"$rc" 2>/dev/null || kill -KILL "$rc" 2>/dev/null || true
}

# Idle until Claude is authenticated (`make dev` / login.sh writes creds into the
# shared dd-claude volume). Poll auth every 10s; do nothing else until then.
until claude auth status >/dev/null 2>&1; do sleep 10; done
log "authenticated; supervising remote-control (poll=${RC_POLL_SECS}s idle-recycle=${RC_IDLE_RECYCLE_SECS}s)"

perm_args=()
[ -n "$RC_PERMISSION_MODE" ] && perm_args=(--permission-mode "$RC_PERMISSION_MODE")

while true; do
  # Drop admin entries for worktrees whose dirs are already gone — combats the
  # orphaned-environment buildup behind #37321. Safe to run at any time.
  git -C "$RC_REPO" worktree prune || true

  # setsid → the daemon leads its own process group (pgid == its pid), so session
  # helpers share that pgid and `kill -TERM -<pid>` reaps the whole tree on recycle.
  setsid claude remote-control --spawn worktree --no-create-session-in-dir \
    "${perm_args[@]}" &
  rc_pid=$!
  log "started remote-control pid=$rc_pid (spawn=worktree, no-create-session-in-dir)"

  used=0        # has this daemon served ≥1 session since it started?
  idle_since=0  # $SECONDS when it last dropped to 0 sessions (0 = not currently idle)
  recycled=0    # has the one-shot idle recycle already fired this idle stretch?

  while kill -0 "$rc_pid" 2>/dev/null; do
    sleep "$RC_POLL_SECS"
    n=$(session_count "$rc_pid")
    if [ "${n:-0}" -gt 0 ]; then
      used=1; idle_since=0
      continue
    fi
    # 0 sessions from here down.
    [ "$used" = 1 ] || continue      # fresh, never-used daemon → nothing to recover
    [ "$recycled" = 1 ] && continue  # already did the one-shot recycle this stretch
    if [ "$idle_since" = 0 ]; then
      idle_since=$SECONDS
    elif [ $((SECONDS - idle_since)) -ge "$RC_IDLE_RECYCLE_SECS" ]; then
      log "idle at 0/32 for ${RC_IDLE_RECYCLE_SECS}s after use → recycling pid=$rc_pid"
      kill_daemon "$rc_pid"
      recycled=1
      break  # fall through to the outer loop, which starts a fresh daemon
    fi
  done

  if [ "$recycled" != 1 ]; then
    log "remote-control pid=$rc_pid exited on its own; restarting in 10s"
    sleep 10
  fi
done
