#!/usr/bin/env bash
# dd-dev PID 1 (under `init: true`, so the background processes left here get reaped).
#
# **sshd is the FOREGROUND process, and that is the whole design.** This container is
# used by getting a session inside it — `ssh -p 2222 dev@<pi>`, Zed's remote, or
# `docker exec` from the Pi host — and running claude or a long `make` inside an
# `abduco` session there. So the thing whose lifetime the container's lifetime should
# equal is the ssh endpoint.
#
# It was the other way round for a while: the remote-control supervisor ran in the
# foreground so `docker logs` would show the Claude session, with sshd pushed into the
# background behind it. That arrangement made the container's life depend on a daemon
# that is now opt-in — see the DD_REMOTE_CONTROL block at the bottom.
set -e

# Git identities: keys + SSH config live in the gitignored .dev-ssh/ dir, which
# rides along with the bind-mounted repo clone. Wire them into ~/.ssh on start.
if [ -d /workspace/.dev-ssh ]; then
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  chmod 600 /workspace/.dev-ssh/id_ed25519_* 2>/dev/null || true
  [ -f /workspace/.dev-ssh/config ] && ln -sf /workspace/.dev-ssh/config "$HOME/.ssh/config"
  # Push over SSH with the keys above WITHOUT editing the shared .git/config
  # remote (keeps the host on HTTPS): rewrite GitHub HTTPS->SSH in the
  # container's own ~/.gitconfig only.
  git config --global url."git@github.com:".insteadOf "https://github.com/"
fi

# Deps are baked into /home/dev/venv at build time; add the editable project now
# that /workspace is mounted. Best-effort so the container still comes up if the
# clone is absent or offline.
[ -f /workspace/pyproject.toml ] && uv sync --frozen || true

# In-container sshd (Zed-remote / direct SSH). Generate the host key once into the
# persisted dd-ssh-host volume so Zed's known_hosts survives rebuilds.
mkdir -p "$HOME/.ssh-host" && chmod 700 "$HOME/.ssh-host"
[ -f "$HOME/.ssh-host/ssh_host_ed25519_key" ] || \
  ssh-keygen -t ed25519 -f "$HOME/.ssh-host/ssh_host_ed25519_key" -N "" -C dd-dev-host

# SSH/Zed sessions don't inherit the entrypoint's env, so publish it (with the venv
# on PATH) to ~/.ssh/environment, which sshd reads via PermitUserEnvironment. Filter
# shell noise; one KEY=value per line, no quotes (PermitUserEnvironment format).
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
{
  echo "PATH=/home/dev/venv/bin:$PATH"
  env | grep -vE '^(PATH|PWD|SHLVL|_|HOME|OLDPWD|HOSTNAME)='
} > "$HOME/.ssh/environment"
chmod 600 "$HOME/.ssh/environment"

# Pre-seed two headless-hostile first-run flags in ~/.claude.json (moved into the
# persisted dd-claude volume via CLAUDE_CONFIG_DIR). Both default to "unset" on a FRESH
# volume, where each blocks claude with a dialog nobody can answer in a headless
# container. Done idempotently, merging into any existing config, BEFORE anything can
# start a claude (while no claude process is writing the file):
#
#   1. projects["<dir>"].hasTrustDialogAccepted — workspace trust. Absent → `claude
#      remote-control --spawn worktree` aborts with "Workspace not trusted". We seed it
#      for /workspace (which also covers the worktrees spawned beneath it). /workspace is
#      not $HOME, so unlike home-directory trust this record is actually persisted.
#   2. remoteDialogSeen (top-level) — the one-time "Enable Remote Control? [y/N]" consent.
#      When falsy, `claude remote-control` opens a readline prompt on stdin; with no
#      interactive stdin the supervisor's daemon can never answer it and re-prompts on
#      every restart. Seeding it true skips the prompt outright. Kept unconditional even
#      though the supervisor is opt-in now: it costs a JSON key, and the moment it would
#      be missed is the one where someone has just set DD_REMOTE_CONTROL=1 and is not
#      watching the container come up.
python3 - "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.claude.json" /workspace <<'PY' || true
import json, os, sys

path, project = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        cfg = {}
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}

dirty = False

entry = cfg.setdefault("projects", {}).setdefault(project, {})
if entry.get("hasTrustDialogAccepted") is not True:
    entry["hasTrustDialogAccepted"] = True
    dirty = True

if cfg.get("remoteDialogSeen") is not True:
    cfg["remoteDialogSeen"] = True
    dirty = True

if dirty:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
PY

# Claude Remote Control — OFF unless asked for. It makes the container drivable from
# claude.ai/code and the Claude mobile app, and it used to be started on every boot as
# the foreground process. It is opt-in now: the way in is an ssh session plus abduco,
# and a daemon nobody is driving is a live claude with a permission classifier for
# company, sitting on this repo's push keys and Railway token.
#
# Turn it on for a spell — a session driven from the app while away from a terminal —
# with DD_REMOTE_CONTROL=1 in .env, then `make dev-up`. It is read at start, so it
# lands on a recreate, not on a bare `docker restart`. The supervisor polls for a login
# rather than failing without one, so ordering with `make dev-login` does not matter;
# see its header for the recycle policy — it only ever restarts a wedged daemon at 0/32.
# setsid + its own log, because its stdout is a TUI that repaints.
if [ "${DD_REMOTE_CONTROL:-0}" = "1" ]; then
  setsid bash /home/dev/rc-supervisor.sh </dev/null >/dev/null 2>&1 &
  echo "[entrypoint] remote-control supervisor started (log: ~/.local/share/remote-control.log)"
else
  echo "[entrypoint] remote control off (DD_REMOTE_CONTROL=1 in .env turns it on)"
fi

# In-container sshd, in the foreground: the container's payload. exec, so sshd IS this
# process — signals from `docker stop` reach it directly and the container's lifetime is
# exactly the endpoint's. -D stops it daemonising, which would point its stderr at
# /dev/null and throw away the -e log that `docker logs dd-dev` is made of.
#
# Nothing beyond this line runs; a start-up failure is in the log above it.
echo "[entrypoint] sshd on :2222 in the foreground — get a session and work in abduco"
echo "[entrypoint]     ssh -p 2222 dev@<pi>                     a fish shell in /workspace"
echo "[entrypoint]     abduco -A claude claude                  a claude that survives the link"
exec /usr/sbin/sshd -D -e -f /home/dev/sshd_config
