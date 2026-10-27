#!/usr/bin/env bash
# Interactive login walkthrough for dd-dev. Baked into the image at /home/dev/login.sh and
# run from a terminal on zero with infra's dev/ Makefile:
#
#     cd ~/infra/dev && make login C=dd-dev
#
# Every step is IDEMPOTENT: it reads the current state and only prompts when something is
# NOT already set up, so re-running is safe and usually silent. Nothing here writes into a
# secrets directory — since 2026-10-27 dd-dev is a service in infra's dev/compose.yaml, and
# what it needs comes from the hub, the host or a persisted volume:
#
#   GitHub          -> /run/github, the hub's token for this repo  (checked, nothing to do)
#   the workspace   -> the dd-workspace volume, .env linked in from ~/.env.d  (checked)
#   ssh identities  -> /run/dd-secrets, read-only: ~/.dd-dev-secrets on zero  (reported)
#   Railway         -> the dd-railway volume (~/.railway)
#   Claude Code     -> the login infra's compose names (shared across the dev containers
#                      by default)
#
# A script in the image rather than a Makefile recipe because every step needs a tty and
# every credential store it fills is in here, not on zero. Never fatal: a step that fails
# says so and the next one runs.
set -u

bold() { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
info() { printf '    %s\n' "$1"; }
ask()  { # ask "question" -> 0 on yes; empty answer means yes
  local reply
  read -r -p "  $1 [Y/n] " reply || return 1
  [ -z "$reply" ] || [ "$reply" = y ] || [ "$reply" = Y ]
}

REPO="${DEV_REPO:-gsfernandes81/destiny-director}"
SECRETS="${DEV_SECRETS_DIR:-/run/dd-secrets}"

# ── 1/5  GitHub: the hub's token ─────────────────────────────────────────────
# Nothing to log into: the hub mints this container an hourly token for this repo and no
# other, and git (git-credential-hub) and gh (the base's wrapper) both use it. So no deploy
# key, no `gh auth login`, and NOT `gh auth status` as the check — it fails for an
# installation token, which has no user, while every repo command works.
bold "1/5  GitHub (the hub's token)"
if [ -s /run/github/token ]; then
  if git ls-remote "https://github.com/$REPO.git" HEAD >/dev/null 2>&1; then
    ok "the hub's token for $REPO works — git and gh need nothing else."
  else
    warn "a token is here but GitHub refuses it for $REPO."
    info "Is the App installed on that repo? On zero: cd ~/infra/dev && make status C=hub"
  fi
else
  warn "no token from the hub yet."
  info "The hub writes one at its start and every 30 min; on zero,"
  info "cd ~/infra/dev && make status C=hub says why it has not."
fi

# ── 2/5  the workspace and .env ──────────────────────────────────────────────
# /workspace is this container's own volume, cloned at start from DEV_REPO with the hub's
# token; a start before the token arrived leaves it unchecked-out. A new volume is seeded
# with the image's copies of pyproject.toml and uv.lock, so "empty" means "only those".
bold "2/5  the workspace (/workspace) and .env"
if [ ! -d /workspace ]; then
  warn "no /workspace at all — is this dd-dev? infra's dev/compose.yaml mounts dd-workspace there."
elif [ -d /workspace/.git ]; then
  ok "$(git -C /workspace remote get-url origin 2>/dev/null) at $(git -C /workspace log --oneline -1 2>/dev/null)"
else
  others=$(cd /workspace 2>/dev/null && ls -A | grep -vxE 'pyproject\.toml|uv\.lock|\.env')
  if [ -n "$others" ]; then
    warn "/workspace holds files but no checkout; not cloning $REPO over them."
  elif ask "Clone $REPO into /workspace now?"; then
    tmp=/workspace/.dev-clone
    rm -rf "$tmp"
    if git clone --quiet --no-checkout "https://github.com/$REPO.git" "$tmp" \
       && mv "$tmp/.git" /workspace/.git && rmdir "$tmp" \
       && git -C /workspace checkout --quiet -f; then
      ok "cloned; the next start adds the project to the venv (or: uv sync --frozen)."
    else
      rm -rf "$tmp"
      warn "the clone failed — see step 1. A restart retries it: make restart C=dd-dev"
    fi
  fi
fi
# .env is a link into the env volume (DEV_ENV_FILES); every make target that runs the bots,
# the tests or the migrations reads it, and nothing puts it into the environment.
if [ -L /workspace/.env ] && [ -s /workspace/.env ]; then
  ok ".env -> $(readlink /workspace/.env)"
elif [ -f /workspace/.env ]; then
  warn ".env is a regular file, not the link into ~/.env.d — the env volume has an older copy."
  info "If this one is the newest: mv /workspace/.env ~/.env.d/.env, then on zero"
  info "cd ~/infra/dev && make restart C=dd-dev"
else
  warn "no .env — make test, the devbots and the migrations all need one (.env-example)."
  info "On zero: sudo docker cp <the .env> dd-dev:/home/dev/.env.d/.env"
  info "then: cd ~/infra/dev && make restart C=dd-dev   (the restart links it in)"
fi

# ── 3/5  ssh identities: reported ────────────────────────────────────────────
# Read-only from here, and only for remotes the hub's token cannot reach (origin needs none).
# The base copies each private key in the mount into ~/.ssh and prepends its
# ssh_config.fleet to ~/.ssh/config at start; adding one is done on zero, in
# ~/.dd-dev-secrets, and takes effect at the next restart.
bold "3/5  ssh identities ($SECRETS)"
keys=$(grep -l -- '-----BEGIN .*PRIVATE KEY-----' "$SECRETS"/id_* 2>/dev/null | xargs -r -n1 basename | paste -sd ' ')
if [ -f "$SECRETS/ssh_config.fleet" ]; then
  ok "ssh_config.fleet present; keys: ${keys:-none}"
  for h in $(awk 'tolower($1) == "host" { for (i = 2; i <= NF; i++) print $i }' \
               "$SECRETS/ssh_config.fleet" | grep '^github\.com'); do
    if ssh -o BatchMode=yes -o ConnectTimeout=10 -T "git@$h" 2>&1 | grep -q "successfully authenticated"; then
      ok "$h authenticates."
    else
      warn "$h does not authenticate — its key is not on that GitHub account?"
    fi
  done
else
  ok "none — origin needs none. Keys: ${keys:-none}."
  info "For a remote over ssh, add the key and an ssh_config.fleet naming it in"
  info "~/.dd-dev-secrets on zero, then make restart C=dd-dev."
fi

# ── 4/5  Railway CLI ─────────────────────────────────────────────────────────
# Checked the way the Makefile's railway targets run it: bare. .env is NOT this container's
# environment any more, so a RAILWAY_API_TOKEN in it does not reach a bare `railway`; a
# browserless login, persisted in the dd-railway volume, does.
bold "4/5  Railway CLI"
if railway whoami >/dev/null 2>&1; then
  ok "$(railway whoami 2>&1 | head -1)"
else
  warn "Not logged in."
  if grep -q '^RAILWAY_API_TOKEN=.' /workspace/.env 2>/dev/null; then
    info ".env has a RAILWAY_API_TOKEN, but .env is not exported into this container,"
    info "so a bare railway (and every railway make target) does not see it."
  fi
  ask "Run 'railway login --browserless' now?" && railway login --browserless
fi

# ── 5/5  Claude Code ─────────────────────────────────────────────────────────
# Shared across the dev containers by default (infra's CLAUDE_UNDOCUMENTED_SHARED_LOGINS),
# so a login made here is a login everywhere and is often already done.
bold "5/5  Claude Code"
if claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
  ok "logged in — \`claude\` will start without a login flow."
else
  warn "Not logged in."
  info "No browser in here: it prints a URL, and you paste back the code."
  # GIT_USER_EMAIL is set on the service in infra's compose; without it, claude asks.
  ask "Run 'claude auth login' now?" \
    && claude auth login --claudeai ${GIT_USER_EMAIL:+--email "$GIT_USER_EMAIL"}
fi

bold "Done."
cat <<'EOF'

  dd-dev is reached through the hub. From the phone or a PC:

      ssh dev                 the menu: Enter on dd-dev for its sessions,
                              n for a new one, s for a shell in /workspace

  Every session the menu opens is a zmx session: Ctrl-\ detaches, and a dropped
  connection costs nothing — ssh in again and press Enter.

  From a terminal on zero, in ~/infra/dev:
      make up S=dd-dev        start it (it rests stopped), with dd-postgres
      make shell C=dd-dev     a shell, without the hub
      make status C=dd-dev    what is running and logged in
      make login C=dd-dev     this walkthrough again
      make stop C=dd-dev      stop it; volumes stay
EOF
exit 0
