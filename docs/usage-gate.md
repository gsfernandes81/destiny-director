# Claude usage-limit gate (PreToolUse hook)

A per-developer Claude Code hook that **pauses agentic execution when the Claude
subscription usage cap gets close to its limit**, so a long autonomous run stops
itself instead of dying mid-task at the limit. It is a personal workflow tool,
not part of the Destiny Director application.

## What lives where (important)

Only **this doc is committed**. The moving parts are **user-level** (in `~/.claude/`)
and are deliberately *not* in the repo — so the hook is never forced on teammates,
CI, or other checkouts. If you move to a new machine (or wipe `~/.claude`), this
doc is how you recreate it.

| Piece | Path | Committed? |
|---|---|---|
| Hook script | `~/.claude/hooks/usage-gate.sh` | No (user home) |
| Hook registration | `~/.claude/settings.json` → `hooks.PreToolUse` | No (user home) |
| Usage cache | `~/.cache/claude-usage.json` | No (regenerated) |
| Bypass grants | `~/.cache/claude-usage-bypass/<session_id>` | No (regenerated) |
| Slash commands | `~/.claude/commands/{usage-bypass,usage-handoff,list-handoffs}.md` | No (user home) |
| Handoff notes | `/workspace/handoffs/` (gitignored, anchored) | No (ephemeral) |
| This doc | `docs/usage-gate.md` | **Yes** |

## How it works

- Registered as a **`PreToolUse`** hook with `matcher: "*"`, so the harness runs it
  **before every tool call** — i.e. before every step of agentic execution. The
  harness enforces it; it does not depend on the model remembering to check.
- It **evaluates every step but only fetches every ~90s** (a TTL cache), so the
  per-step cost is a local file read, not a network round-trip.
- Data source: `GET https://api.anthropic.com/api/oauth/usage` with the Claude
  OAuth token from `~/.claude/.credentials.json` (`claudeAiOauth.accessToken`).
  The response gives `five_hour.utilization` and `seven_day.utilization` as 0–100
  percentages plus `resets_at` timestamps. (The short cap is a **rolling 5-hour**
  window, not "daily"; the long one is the **7-day** weekly cap.)
- Each cap has its own threshold: **5-hour ≥ 80%** or **7-day ≥ 90%**. When a cap
  reaches its threshold the hook exits `2` (blocks the tool call) and tells Claude,
  via stderr, to **pause the session** — inform the user it's paused on the usage cap
  (with the reset time and the `/usage-bypass` option) and stop. The thresholds sit
  **below** the real subscription caps on purpose: that headroom means a pause always
  leaves enough budget to write a handoff (below) even mid-task.
- **No auto-wake.** The hook used to tell Claude to `ScheduleWakeup` and auto-resume,
  but that resumes the *same* session and re-reads the whole transcript **uncached**
  every wake. Instead the pause **offers a handoff**: `/usage-handoff` writes a compact
  resume note and unlocks the worktree, and a fresh, minimal session picks it up via
  `/list-handoffs`. See "Handoff & resume" below.
- **`ScheduleWakeup` / `TaskUpdate` / `TaskCreate` are allowlisted** — otherwise the
  gate would block the very tool Claude needs to schedule its own pause. The **handoff
  hatch** additionally lets the handoff's own tools through at the wall (a `Write` under
  `/workspace/handoffs/`, or a Bash command carrying the `USAGE_GATE_HANDOFF` token) —
  per-call, no persistent grant.
- **Per-session bypass (user-confirmed).** A user can authorise a single session to
  ignore the gate — see the section below. The grant is keyed to the harness
  `session_id`, so it covers exactly that session and no other; it disappears when
  the session ends.
- **Fails open** on any error (network, expired/401 token, changed response shape)
  so it can never wedge a session.

## Per-session bypass

Sometimes a run is worth pushing past the gate (a migration mid-flight, a release
you want finished this evening). The gate supports a bypass that is scoped to **one
session** and requires the **user's explicit go-ahead** — it is never Claude's call
to make unprompted.

- **The grant is a file named for the session id** under
  `~/.cache/claude-usage-bypass/<session_id>`. While it exists, that session's tool
  calls are never blocked. A different session with a fuller cap is still gated
  normally — the grant does not leak.
- **How the user confirms.** When (and only when) the user explicitly asks to bypass
  the gate for the session, Claude runs a Bash command carrying the
  `USAGE_GATE_BYPASS` token. The hook recognises it, records the grant for the
  current `session_id`, and — crucially — lets that command through **even if
  already over the cap**, so the hatch works exactly when it's needed:

  ```bash
  echo "USAGE_GATE_BYPASS: authorise this Claude session to bypass the usage gate (user-approved)."
  ```

- **To revoke** (restore normal thresholds for the session):

  ```bash
  echo "USAGE_GATE_BYPASS_REVOKE: restore normal usage-gate thresholds for this session."
  ```

- **Trade-off:** bypassing re-accepts the original risk — the run can still hard-stop
  at the *real* subscription cap mid-task. The gate only defers that; bypass removes
  the guard rail for that one session. The stderr note on authorising says as much.

The behavioural directive ("only on the user's explicit say-so") lives in the
developer's Claude memory, not in `CLAUDE.md`, since the whole hook is a personal
workflow tool and not forced on teammates or CI.

### `/usage-bypass` slash command

The bypass is fronted by a **user-level slash command** so the user triggers it
explicitly — typing `/usage-bypass` *is* the confirmation. Like the hook it is
user-level (`~/.claude/commands/usage-bypass.md`) and **not committed**, so this doc
is how to recreate it.

- `/usage-bypass` → authorise the bypass for the current session.
- `/usage-bypass revoke` → clear it, restoring normal thresholds.

This works from **any client attached to this box** — a terminal session, or a
web/mobile client remote-controlling the same server-mode Claude. All spawned
sessions run on this filesystem, so they read the same `~/.claude/commands/` and the
same hook, and each carries its own `session_id` (so the grant stays scoped to the
one session you drove). It does **not** apply to a cloud sandbox that merely clones
the repo — that has neither `~/.claude/commands/` nor the hook.

**Verbal fallback.** Slash commands don't render in every client (e.g. Claude on the
web). Every command here is equally invokable in **plain language** — "bypass the
usage gate for this session", "write a handoff and pause", "list/resume handoffs" —
because the behaviour lives in the hook + the developer's Claude memory, not in the
command file. The `.md` command is just a convenience wrapper around the same steps.

Recreate `~/.claude/commands/usage-bypass.md`:

```markdown
---
description: Authorise (or revoke) a usage-gate bypass for THIS Claude session
argument-hint: "[revoke]"
---

The user is explicitly authorising a usage-gate bypass for the current session by
invoking this command. Invoking it IS the required user confirmation — act on it
directly, do not ask again.

Argument: "$ARGUMENTS"

- If the argument is `revoke`, run exactly this Bash command:
  `echo "USAGE_GATE_BYPASS_REVOKE: restore normal usage-gate thresholds for this session (user-approved)."`
  Then tell the user the bypass is revoked and the normal thresholds (5-hour ≥ 80%,
  7-day ≥ 90%) apply to this session again.

- Otherwise (no argument), run exactly this Bash command:
  `echo "USAGE_GATE_BYPASS: authorise this Claude session to bypass the usage gate (user-approved)."`
  Then tell the user this session will no longer be paused by the usage gate until it
  ends — and note the trade-off: the run can still hard-stop at the *real* subscription
  cap mid-task.

Mechanism: the PreToolUse usage-gate hook (`~/.claude/hooks/usage-gate.sh`) intercepts
that echo, records/clears a grant file keyed to this session's `session_id`
(`~/.cache/claude-usage-bypass/<session_id>`), and lets the command through even when
already over the cap. It is scoped to this one session only. See docs/usage-gate.md.
```

## Handoff & resume

> **Remote control left this container on 2026-08-25.** It is reached by ssh with
> the work held in an `abduco` session now, and no daemon spawns a worktree per
> session. The wording below describes how this was originally exercised; the gate
> itself is unchanged, and a worktree you make yourself (`git worktree add`) puts
> you in the same shape it assumes.

When the gate pauses a run, the token-efficient way to continue is **not** to resume
the same (now-huge) session but to write a compact **handoff** and let a **fresh,
minimal session** pick up only that. This is built for `claude remote-control --spawn
worktree`, where each session runs in its own git worktree under
`/workspace/.claude/worktrees/bridge-cse_<id>/` that the harness **locks**.

### The handoff hatch
At the wall the gate blocks *every* tool, so the handoff's own tools are allowlisted:
a `Write` whose `file_path` starts with `/workspace/handoffs/`, or a Bash command
containing the literal token `USAGE_GATE_HANDOFF`. Unlike the bypass this is
**per-call and creates no grant**, so ordinary work stays blocked. (Reads are *not*
hatched — so the handoff procedure carries its own template and never depends on
reading a file over the cap.)

### Where handoffs live
`/workspace/handoffs/<UTC-stamp>__<branch-slug>__<task-slug>.md`, e.g.
`20260714T223145Z__feat-usage-gate-handoff__resume-trigger.md`. The folder is
gitignored with an **anchored** `/handoffs/` entry so it matches only the repo-root
folder — which sits *outside* every spawned worktree and therefore **survives**
`git worktree remove`/`prune`. (A bare `handoffs/`, like `scratch/`, would match a
folder inside a worktree and be pruned with it.)

### `/usage-handoff` — write a handoff (at the wall)
`~/.claude/commands/usage-handoff.md` (user-level, not committed). It: (1) runs one
Bash command carrying `USAGE_GATE_HANDOFF` to read provenance (`git rev-parse
--show-toplevel`, branch, HEAD) and, if the tree is dirty, capture it as a
`wip: usage-gate handoff` commit; (2) `Write`s the handoff from the template below,
filling every section; (3) runs a final `USAGE_GATE_HANDOFF` Bash to `git worktree
unlock` (last, so a failed write never unlocks prematurely); (4) reports and stops.

**Uncommitted work → wip commit, then unlock.** Unlocking lets the worktree be
pruned, which deletes its working tree. So dirty work is committed first
(`git add -A && git commit -m "wip: …"`): the commit object and branch ref live in the
shared `.git` and survive pruning, so the fresh session recovers via
`git checkout <branch>` (un-wip with `git reset --soft HEAD^`). It captures
untracked/binary that an embedded diff would miss, is bounded by `.gitignore`, and is
**local — never auto-pushed**.

Handoff template (verbatim — the command inlines this so it needs no `Read` at the wall):

```markdown
---
schema: usage-gate-handoff/v1
generated_at: <UTC ISO 8601>
triggering_cap: <five_hour|seven_day|both>
cap_utilization: "<e.g. seven_day=90%>"
resume_after: <max(resets_at), UTC ISO 8601>
worktree_path: <git rev-parse --show-toplevel>
worktree_id: <basename of worktree_path>
branch: <branch>
merge_target: dev
head_before_handoff: <short sha before wip commit>
wip_commit: <short sha | "none (clean)">
dirty_at_block: <true|false>
pushed: <true|false>
worktree_unlocked: <true after unlock>
---

# Handoff: <one-line task title>

## End goal
<Concrete objective of this run, 1-2 sentences.>

## What I was doing last
<The specific step in progress when the gate fired.>

## Exact next action
<The single next command/edit to run on resume. Copy-pasteable where possible.>

## Progress / state
<Done vs remaining, checklist form. Note the wip commit's role.>

## Key decisions & constraints
<Decisions already made; invariants to preserve; things NOT to change.>

## Files touched
<Absolute paths + one-line note each; committed vs wip-committed.>

## How to verify
<Exact commands/tests to confirm correctness after resume.>

## Uncommitted work
<Either "captured in wip commit <sha> on <branch>" + `git diff --stat`, or "none — clean".>

## Open questions
<Anything unresolved the fresh session must decide, with context to decide it.>
```

All sections are mandatory — the point is a handoff a cold session can act on without
guesswork.

### `/list-handoffs` — resume (the only resume mechanism)
`~/.claude/commands/list-handoffs.md` (user-level, not committed). Lists
`/workspace/handoffs/*.md` newest-first with parsed front-matter, lets the user pick
one (by index/substring argument, or interactively), then follows the **resume
checklist** for incoming agents:

1. Read the whole handoff first (End goal, Exact next action, Key decisions, Open Qs).
2. Locate the work — `git rev-parse --verify <branch>`; `git show <wip_commit>` if any.
3. Branch/worktree hygiene — if the handoff's `worktree_id` still exists (stale, it was
   unlocked on handoff), `git worktree remove <path>` then `git worktree prune` to free
   the branch.
4. Adopt the branch — `git switch <branch>` (or `git switch -c <branch>-resume <branch>`).
5. Restore uncommitted state — if dirty, `git reset --soft HEAD^` un-wips.
6. Sanity-check — `git status`, `git log --oneline -5`, run the handoff's *How to verify*.
7. Do the work — the *Exact next action*, honouring Key decisions & constraints.
8. On completion — `rm <handoff path>`; merge `<branch>` → `dev` (conventional commit,
   rename a hash branch first); delete a fully-executed plan; don't leave the worktree
   locked.
9. Pausing again → run `/usage-handoff` (supersedes the old handoff), then stop.

`/list-handoffs` reads files and runs git, which the gate blocks over cap (only handoff
*writing* is hatched) — so resume when you have budget or just after a reset; if you must
resume while over cap, `/usage-bypass` first.

**Verbal use.** As with the bypass, all of this is invokable in plain language ("write a
handoff and pause", "list/resume handoffs") for clients without slash commands. The
`/usage-handoff` procedure and template also live in the developer's Claude memory so a
verbal request at the wall needs no file read.

### Gotcha worth remembering

The endpoint is documented as `claude.ai/api/oauth/usage`, but that host is behind
a Cloudflare managed challenge and returns HTML/`403` to non-browser clients. Use
the **`api.anthropic.com`** host, which serves the same endpoint without a challenge.

## Setup / reinstall instructions

1. Create the hook script:

   ```bash
   mkdir -p ~/.claude/hooks
   # Paste the script from the "Hook script" section below into:
   #   ~/.claude/hooks/usage-gate.sh
   chmod +x ~/.claude/hooks/usage-gate.sh
   ```

2. Register it in `~/.claude/settings.json` (merge — keep existing keys like `theme`):

   ```bash
   python3 - <<'PY'
   import json, pathlib
   p = pathlib.Path.home() / ".claude" / "settings.json"
   d = json.loads(p.read_text()) if p.exists() else {}
   d.setdefault("hooks", {})["PreToolUse"] = [
       {"matcher": "*", "hooks": [
           {"type": "command", "command": "$HOME/.claude/hooks/usage-gate.sh"}
       ]}
   ]
   p.write_text(json.dumps(d, indent=2) + "\n")
   PY
   ```

3. Verify:

   ```bash
   echo '{"tool_name":"Bash"}' | ~/.claude/hooks/usage-gate.sh; echo "exit=$?"   # expect 0
   cat ~/.cache/claude-usage.json                                                # usage JSON
   ```

   A fresh Claude Code session picks up `~/.claude/settings.json` on start.

4. Recreate the slash commands (user-level, not committed) — `usage-bypass.md` is in the
   "Per-session bypass" section above; the two handoff commands are in the next section.

### Recreate the handoff commands

`~/.claude/commands/usage-handoff.md`:

````markdown
---
description: Write a usage-gate resume handoff, capture uncommitted work, and unlock this worktree
argument-hint: "[short-task-slug]"
---

Write a resume handoff so a FRESH minimal session can continue this task cheaply after
the usage cap resets, then unlock this worktree. Do it now, in the fewest tool calls.

This procedure is also invokable **verbally** (say "write a usage handoff" / "hand off
and pause") — it does not depend on this slash command, which is why the template is
inlined below (do NOT rely on reading any file: over the cap the gate blocks `Read`).
Everything below IS permitted over the cap, because the usage-gate hook allowlists a
`Write` under `/workspace/handoffs/` and any Bash command containing the literal token
`USAGE_GATE_HANDOFF` (per-call, no persistent bypass).

**Step 1 — gather provenance + capture uncommitted work.** Run ONE Bash command that
contains the literal string `USAGE_GATE_HANDOFF` (so the hatch lets it through). It must:
`TOP=$(git rev-parse --show-toplevel)`; read `BRANCH=$(git -C "$TOP" branch --show-current)`
and `HEAD0=$(git -C "$TOP" rev-parse --short HEAD)`; and if `git -C "$TOP" status
--porcelain` is non-empty, `git -C "$TOP" add -A && git -C "$TOP" commit -q -m "wip:
usage-gate handoff ($ARGUMENTS)"`, then set `WIP=$(git -C "$TOP" rev-parse --short HEAD)`
and capture `git -C "$TOP" diff --stat HEAD^` — else `WIP="none (clean)"`. Print
`TOP`, `BRANCH`, `HEAD0`, `WIP`, dirty true/false, the diffstat, and a timestamp from
`date -u +%Y%m%dT%H%M%SZ`.

**Step 2 — write the handoff.** Build the path
`/workspace/handoffs/<stamp>__<branch-slug>__<slug>.md` where `stamp` is the timestamp
from step 1, `branch-slug` is `BRANCH` with `/` and spaces replaced by `-`, and `slug`
is `$ARGUMENTS` (or a short kebab summary of the task if empty). `Write` this exact
template, filling **every** section from the current conversation — no placeholders.
Populate the front-matter from step 1; set `triggering_cap`, `cap_utilization`, and
`resume_after` from the gate's pause message; `merge_target: dev`; `worktree_unlocked:
false` for now:

```markdown
---
schema: usage-gate-handoff/v1
generated_at: <UTC ISO 8601>
triggering_cap: <five_hour|seven_day|both>
cap_utilization: "<e.g. seven_day=90%>"
resume_after: <max(resets_at), UTC ISO 8601>
worktree_path: <TOP>
worktree_id: <basename of TOP>
branch: <BRANCH>
merge_target: dev
head_before_handoff: <HEAD0>
wip_commit: <WIP>
dirty_at_block: <true|false>
pushed: <true|false>
worktree_unlocked: false
---

# Handoff: <one-line task title>

## End goal
<Concrete objective of this run, 1-2 sentences.>

## What I was doing last
<The specific step in progress when the gate fired.>

## Exact next action
<The single next command/edit to run on resume. Copy-pasteable where possible.>

## Progress / state
<Done vs remaining, checklist form. Note the wip commit's role.>

## Key decisions & constraints
<Decisions already made; invariants to preserve; things NOT to change.>

## Files touched
<Absolute paths + one-line note each; committed vs wip-committed.>

## How to verify
<Exact commands/tests to confirm correctness after resume.>

## Uncommitted work
<Either "captured in wip commit <WIP> on <BRANCH>" + the diff --stat, or "none — clean".>

## Open questions
<Anything unresolved the fresh session must decide, with context to decide it.>
```

**Step 3 — unlock the worktree LAST.** Run a final Bash command containing
`USAGE_GATE_HANDOFF` that does `git worktree unlock "$TOP"` (ignore "not locked" errors).
Last, so a failed Write never leaves the worktree prematurely unlocked. Then edit the
handoff's `worktree_unlocked:` to `true`.

**Step 4 — report + stop.** Tell the user: the handoff path, that the worktree is
unlocked, the `resume_after` time, and that a fresh session resumes it with
`/list-handoffs` (or by asking verbally to "list/resume handoffs"). Then STOP — do not
start or retry any other work.
````

`~/.claude/commands/list-handoffs.md`:

````markdown
---
description: List usage-gate handoffs and resume from one the user picks
argument-hint: "[index | filename-substring]"
---

Help the user resume a paused task from a handoff note under `/workspace/handoffs/`.
This is also invokable **verbally** (say "list handoffs" / "resume a handoff") — it does
not depend on the slash command, which matters where slash commands are unavailable
(e.g. Claude on the web).

**Step 1 — list.** Glob `/workspace/handoffs/*.md` (newest first — filenames start with a
sortable UTC timestamp). If none exist, say so and stop. Otherwise parse each file's
YAML front-matter and present an indexed table: index, title (the `# Handoff:` line),
`branch`, `worktree_id`, `generated_at`, `resume_after`, and dirty/wip (`wip_commit`).

**Step 2 — choose.** If `$ARGUMENTS` is given, resolve it as a 1-based index or a
filename substring and pick that handoff. If it's empty, show the table and ask the user
which to resume (they may also say "just listing" — then stop without resuming).

**Step 3 — resume.** Read the chosen handoff in full, then follow this checklist:

1. **Read it all first** — internalise End goal, Exact next action, Key decisions &
   constraints, and Open questions before touching anything.
2. **Locate the work** — `git rev-parse --verify <branch>`; and if `wip_commit` ≠
   "none (clean)", `git show <wip_commit>` to see the captured changes.
3. **Branch / worktree hygiene** — `git worktree list`. If the handoff's `worktree_id`
   still exists (a stale leftover from the paused session — it was unlocked on handoff),
   remove it: `git worktree remove <path>` (add `--force` only if it refuses and you've
   confirmed nothing there is unsaved), then `git worktree prune`. This frees `<branch>`.
4. **Adopt the branch** — `git switch <branch>` in your current worktree (now free). If
   you'd rather not move your worktree's branch, `git switch -c <branch>-resume <branch>`.
5. **Restore uncommitted state** — if the handoff was dirty (a `wip:` commit exists) and
   you want to continue as if it were never committed, `git reset --soft HEAD^` (returns
   the changes to staged). If it was clean, skip.
6. **Sanity-check** — `git status`, `git log --oneline -5`, and run the handoff's *How to
   verify* commands to confirm you match its described state before proceeding.
7. **Do the work** — carry out *Exact next action*, honouring Key decisions & constraints.
   Raise any Open questions with the user if they block progress.
8. **On completion** — `rm <handoff path>` (it has served its purpose; keep
   `/workspace/handoffs/` tidy). Then follow repo rules: merge `<branch>` into `dev` with
   a conventional commit (rename any opaque hash branch first, per CLAUDE.md), and delete
   a fully-executed plan from `plans/`. Don't leave your worktree locked.
9. **Pausing again?** — run `/usage-handoff` to write a fresh handoff (it supersedes this
   one), then stop.

Note: this command reads files and runs git, which the usage gate blocks when over cap
(the handoff *hatch* only covers writing handoffs). Run `/list-handoffs` when you have
budget or just after a reset; if you must resume while still over cap, authorise
`/usage-bypass` first.
````

### Tuning knobs (top of the script)

- `THRESHOLD_FIVE_HOUR=80` / `THRESHOLD_SEVEN_DAY=90` — per-window pause thresholds
  (pause at/above this percentage); kept below the real caps for handoff headroom.
- `TTL=90` — seconds between live fetches (raise to fetch less often; evaluation
  every step is free either way).
- Allowlist `case` line — tools that are never blocked; plus the handoff-hatch line
  (`[ "$HANDOFF" = "yes" ] && exit 0`) for the handoff writer's own tools.

## Revert / uninstall instructions

1. Remove the hook registration (leaves other settings intact):

   ```bash
   python3 - <<'PY'
   import json, pathlib
   p = pathlib.Path.home() / ".claude" / "settings.json"
   d = json.loads(p.read_text())
   hooks = d.get("hooks", {})
   hooks.pop("PreToolUse", None)
   if not hooks:
       d.pop("hooks", None)
   p.write_text(json.dumps(d, indent=2) + "\n")
   PY
   ```

2. Delete the script and cache:

   ```bash
   rm -f ~/.claude/hooks/usage-gate.sh ~/.cache/claude-usage.json
   rm -f ~/.claude/commands/usage-bypass.md ~/.claude/commands/usage-handoff.md ~/.claude/commands/list-handoffs.md
   rm -rf ~/.cache/claude-usage-bypass
   rm -rf /workspace/handoffs   # ephemeral handoff notes (gitignored)
   rmdir ~/.claude/hooks 2>/dev/null || true
   ```

3. Start a new Claude Code session (settings are read at session start). To disable
   temporarily without uninstalling, just `chmod -x ~/.claude/hooks/usage-gate.sh`
   and it fails open, or remove the `PreToolUse` block per step 1.

## Caveats

- **Undocumented endpoint.** `/api/oauth/usage` is not a published API — field names
  or the host could change. The hook fails open by design so a shape change degrades
  to "no gate," never to a stuck session.
- **Token expiry.** Claude Code refreshes `claudeAiOauth.accessToken` in place; the
  hook re-reads the file each fetch. An expired token → 401 → fail open. Re-`/login`
  if the gate silently stops working.
- **Not a daily cap.** The short window is rolling 5-hour, not calendar-daily.

## Hook script

Full contents of `~/.claude/hooks/usage-gate.sh` (kept here because the script is
not committed):

```bash
#!/usr/bin/env bash
# usage-gate.sh — Claude Code PreToolUse hook.
#
# Pauses agentic execution when the 5-hour or 7-day Claude subscription usage
# cap reaches >= its per-window threshold. Fires before EVERY tool call, but
# fetches usage at most once per TTL seconds (cached), so the per-step cost is a
# local file read.
#
# On block it PAUSES the session (no ScheduleWakeup auto-resume — that re-reads the
# whole transcript uncached every wake). Instead it offers a HANDOFF: run
# /usage-handoff to write a compact resume note under /workspace/handoffs/ + unlock
# the worktree, then a fresh session resumes via /list-handoffs. The handoff's own
# tools are allowlisted here (the "handoff hatch") so it can be written at the wall.
#
# FAILS OPEN on any error (network, expired token, changed response shape) so it
# can never wedge a session.
#
# This file is NOT in version control. Setup + revert: docs/usage-gate.md in the
# Destiny Director repo.

set -u
CACHE="$HOME/.cache/claude-usage.json"
TTL=90          # seconds between live fetches
# Per-window thresholds: pause at/above this utilization percentage. Kept below the
# real caps so a pause always leaves headroom to write a handoff mid-task.
THRESHOLD_FIVE_HOUR=80
THRESHOLD_SEVEN_DAY=90

BYPASS_DIR="$HOME/.cache/claude-usage-bypass"
mkdir -p "$(dirname "$CACHE")" "$BYPASS_DIR"

# The hook payload arrives as JSON on stdin. Parse it once into: TOOL (for the
# allowlist), SESSION (to scope a bypass to exactly one session), ACTION (a
# user-issued authorise/revoke request, spotted by a token in a Bash command), and
# HANDOFF (whether this call is part of writing a handoff — see the hatch below).
INPUT=$(cat)
{ read -r TOOL; read -r SESSION; read -r ACTION; read -r HANDOFF; } < <(printf '%s' "$INPUT" | python3 -c "
import json, sys
try: d = json.load(sys.stdin)
except Exception: d = {}
tool = d.get('tool_name', '') or ''
sid = d.get('session_id', '') or ''
ti = d.get('tool_input', {}) or {}
cmd = ti.get('command', '') if isinstance(ti, dict) else ''
fp = ti.get('file_path', '') if isinstance(ti, dict) else ''
if tool == 'Bash' and 'USAGE_GATE_BYPASS_REVOKE' in cmd:
    action = 'revoke'
elif tool == 'Bash' and 'USAGE_GATE_BYPASS' in cmd:
    action = 'authorize'
else:
    action = 'none'
handoff = 'yes' if (tool == 'Write' and fp.startswith('/workspace/handoffs/')) \
                or (tool == 'Bash' and 'USAGE_GATE_HANDOFF' in cmd) else 'no'
print(tool); print(sid); print(action); print(handoff)
" 2>/dev/null)

# ALLOWLIST: never block the tools Claude needs to schedule its own pause / track
# state, or it can't defer and just spins on blocked calls.
case "$TOOL" in
  ScheduleWakeup|TaskUpdate|TaskCreate) exit 0 ;;
esac

# HANDOFF HATCH: let the handoff writer's own tools through even when over the cap —
# a Write to /workspace/handoffs/, or a Bash command carrying the USAGE_GATE_HANDOFF
# token (see /usage-handoff + docs/usage-gate.md). This is PER-CALL and creates NO
# persistent grant (unlike the bypass), so real work stays gated. Placed before the
# usage fetch so it costs nothing.
[ "$HANDOFF" = "yes" ] && exit 0

# PER-SESSION BYPASS. A user may authorise THIS session to ignore the gate. The
# grant is a file named for the session id, so it covers exactly one session and
# never leaks to another. Claude issues the authorise/revoke request ONLY on the
# user's explicit say-so (see docs/usage-gate.md), by running a Bash command that
# carries the USAGE_GATE_BYPASS token; this hook turns that into the grant and
# lets the command through even when already over the cap (so the hatch works
# precisely when it's needed).
GRANT="$BYPASS_DIR/${SESSION:-__nosession__}"
case "$ACTION" in
  authorize)
    [ -n "$SESSION" ] && : > "$GRANT"
    echo "USAGE GATE: bypass AUTHORISED for this session; the gate will not block it until the session ends. Note the run can still hard-stop at the real subscription cap." >&2
    exit 0 ;;
  revoke)
    [ -n "$SESSION" ] && rm -f "$GRANT"
    echo "USAGE GATE: bypass REVOKED for this session; normal thresholds apply again." >&2
    exit 0 ;;
esac

# Already-authorised session → never block.
[ -n "$SESSION" ] && [ -f "$GRANT" ] && exit 0

# Refresh the cache only when missing or older than TTL
# ("check every step, fetch every few steps").
now=$(date +%s)
mtime=$(stat -c %Y "$CACHE" 2>/dev/null || echo 0)
if [ ! -f "$CACHE" ] || [ $((now - mtime)) -ge "$TTL" ]; then
  TOKEN=$(python3 -c "import json;print(json.load(open('$HOME/.claude/.credentials.json'))['claudeAiOauth']['accessToken'])" 2>/dev/null) || exit 0
  [ -n "$TOKEN" ] || exit 0
  # NOTE: the api.anthropic.com host serves this endpoint; claude.ai is behind a
  # Cloudflare challenge and returns HTML/403 to non-browser clients.
  curl -sf --max-time 10 -H "Authorization: Bearer $TOKEN" \
       https://api.anthropic.com/api/oauth/usage -o "$CACHE.tmp" 2>/dev/null \
    && mv "$CACHE.tmp" "$CACHE" || { rm -f "$CACHE.tmp" 2>/dev/null; exit 0; }
fi

python3 - "$CACHE" "$THRESHOLD_FIVE_HOUR" "$THRESHOLD_SEVEN_DAY" <<'PY'
import json, sys
cache = sys.argv[1]
thresholds = {"five_hour": float(sys.argv[2]), "seven_day": float(sys.argv[3])}
try:
    d = json.load(open(cache))
except Exception:
    sys.exit(0)  # fail open
blocking = []
for k, thresh in thresholds.items():
    blk = d.get(k) or {}
    u = blk.get("utilization")
    if u is not None and u >= thresh:
        blocking.append((k, u, blk.get("resets_at") or ""))
if blocking:
    caps = ", ".join(f"{k} at {u:.0f}%" for k, u, _ in blocking)
    resets = [r for (_, _, r) in blocking if r[:1].isdigit()]
    resume_at = max(resets) if resets else "the usage reset"
    # PAUSE + offer a handoff. No ScheduleWakeup auto-wake (it re-reads the whole
    # transcript uncached); instead let a fresh session resume from a handoff.
    sys.stderr.write(
        f"USAGE GATE: session PAUSED — {caps} (>= threshold). Do NOT retry tools, "
        f"schedule a wakeup, or start new work. You MAY run /usage-handoff to write a "
        f"resume handoff and unlock this worktree so a fresh session can continue "
        f"cheaply (resume later with /list-handoffs), or just pause. Either way tell "
        f"the user this session is paused on the Claude usage cap and can resume after "
        f"{resume_at} (or immediately if they authorise /usage-bypass), then stop.\n"
    )
    sys.exit(2)  # exit 2 => block the tool call; stderr is shown to Claude
sys.exit(0)
PY
```
