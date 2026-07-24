#!/usr/bin/env bash
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

# `make atlas-migration-plan` uses a dedicated throwaway scratch schema on the
# sibling MySQL as Atlas's dev database (ATLAS_DEV_URL, set in docker-compose.dev.yml)
# — Atlas won't create it itself. Create it idempotently, best-effort with a bounded
# retry so the container still comes up if MySQL isn't ready yet.
/home/dev/venv/bin/python - <<'PY' 2>/dev/null || true
import asyncio, asyncmy
async def main():
    for _ in range(15):
        try:
            conn = await asyncmy.connect(
                host="mysql", port=3306, user="root", password="devroot"
            )
            async with conn.cursor() as cur:
                await cur.execute("CREATE DATABASE IF NOT EXISTS atlas_dev")
            conn.close()
            return
        except Exception:
            await asyncio.sleep(2)
asyncio.run(main())
PY

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

# Claude Remote Control: drive this container's sessions from claude.ai/code or the
# Claude mobile app. Launched in the BACKGROUND so it never blocks container start or
# Zed's sshd (the resilient foreground process below) — if the supervisor dies, the
# container and SSH stay up. The supervisor idles until Claude is authenticated, then
# runs (and health-recycles) `claude remote-control --spawn worktree`. See the script
# header for the recycle policy — it only ever restarts a wedged daemon at 0/32.
bash /home/dev/rc-supervisor.sh &

# sshd becomes the foreground process, keeps the container alive, and serves SSH; -e
# routes its log to `docker logs`. All work still also reachable via `docker exec`.
exec /usr/sbin/sshd -D -e -f /home/dev/sshd_config
