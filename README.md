# Crewvia

**Crewvia** is a portable, task-driven multi-agent system built on top of Claude Code CLI.
Agents are dynamically assigned names, collaborate on kanban-style task cards, and integrate
with [Taskvia](https://taskvia.vercel.app) for approval flows and knowledge logging.
No tmux required — it works standalone or with any terminal multiplexer.

---

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Setup](#setup)
4. [Starting Agents](#starting-agents)
5. [Taskvia Integration](#taskvia-integration)
6. [Customizing worker-names.yaml](#customizing-worker-namesyaml)
7. [Autonomous Improvement System](#autonomous-improvement-system)
8. [Architecture](#architecture)
9. [Directory Structure](#directory-structure)

---

## Overview

Crewvia follows a two-role model:

| Role | Description |
|------|-------------|
| **Director** | Decomposes tasks, creates cards, assigns Workers, manages kanban column transitions (Backlog → In Progress → Done) |
| **Worker** | Executes assigned cards, requests approval for risky tool calls via PreToolUse hooks, posts knowledge logs to Taskvia |

Key design principles:

- **Task-first** — Agents are card executors; the task card is the unit of work
- **Name continuity** — Workers with the same skill set inherit the same name across sessions (e.g., all `[ops, bash]` Workers are called "Kai"), enabling knowledge accumulation
- **Taskvia-optional** — The system runs in standalone mode when `TASKVIA_TOKEN` is not set
- **tmux-optional** — tmux is supported but not required; any terminal works

---

## Prerequisites

Make sure the following are installed before setting up Crewvia:

| Tool | Purpose | Install |
|------|---------|---------|
| **Claude Code CLI** | Runs the AI agents | [docs.anthropic.com](https://docs.anthropic.com/en/docs/claude-code) |
| **jq** | JSON parsing in hooks/scripts | `brew install jq` / `apt install jq` |
| **curl** | HTTP requests to Taskvia API | Usually pre-installed |

Optional but recommended:

- **tmux** — for running multiple agents in split panes

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/tyz-works/crewvia.git
cd crewvia
```

### 2. Set environment variables

Crewvia uses the following environment variables. Add them to your shell profile
(e.g., `~/.zshrc`, `~/.bashrc`) or set them per-session:

| Variable | Required | Description |
|----------|----------|-------------|
| `CREWVIA_TASKVIA` | Optional | Taskvia mode: `enabled`, `disabled`, or `ask` (default). Overrides config and flags |
| `TASKVIA_URL` | Optional | Taskvia WebUI URL (default: `https://taskvia.vercel.app`) |
| `TASKVIA_TOKEN` | Optional | Taskvia API authentication token. Required when `CREWVIA_TASKVIA=enabled` |
| `AGENT_NAME` | Set by `start.sh` | Name assigned to this agent instance |
| `TASK_TITLE` | Set at runtime | Human-readable title of the current task card |
| `TASK_ID` | Set at runtime | Card ID of the current task (e.g., `card-042`) |
| `CREWVIA_APPROVAL_CHANNEL` | Optional | Approval notification channel: `taskvia` (default) / `ntfy` / `both` |
| `NTFY_URL` | Required for ntfy | ntfy server base URL (e.g., `https://ntfy.elni.net`) |
| `NTFY_TOPIC` | Required for ntfy | ntfy topic name. Use `taskvia-approval-` prefix for ACL compatibility |
| `NTFY_USER` | Required for ntfy | ntfy Basic auth username (required if server uses `auth-default-access: deny-all`) |
| `NTFY_PASS` | Required for ntfy | ntfy Basic auth password |
| `APPROVAL_TOKEN_TTL_SECONDS` | Optional | One-time token TTL in seconds (default: `900`) |
| `CREWVIA_VERIFICATION_UI` | Optional | Set in **Taskvia's** Vercel env (not crewvia). `disabled` hides all verification UI and redirects `/verification-queue` to `/` |

Example `.env`-style configuration:

```bash
export TASKVIA_URL="https://taskvia.vercel.app"
export TASKVIA_TOKEN="tvk_xxxxxxxxxxxxxxxxxxxx"

# ntfy push notifications (optional — set CREWVIA_APPROVAL_CHANNEL=ntfy or both)
export CREWVIA_APPROVAL_CHANNEL="both"
export NTFY_URL="https://ntfy.elni.net"
export NTFY_TOPIC="taskvia-approval-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export NTFY_USER="taskvia"
export NTFY_PASS="your-ntfy-password"
```

> **Standalone mode**: Run with `./crewvia --no-taskvia` or set `CREWVIA_TASKVIA=disabled` to skip
> all approval gates and log posting. No token needed. Suitable for local development and CI.
>
> If `TASKVIA_TOKEN` is not set and mode is `enabled`, Crewvia falls back to standalone automatically.

### 3. Grant execute permissions

```bash
chmod +x hooks/*.sh scripts/*.sh
```

### 4. Register hooks in Claude Code settings

Crewvia relies on Claude Code's hook system for approval gating and knowledge logging.
Add the following to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/crewvia/hooks/pre-tool-use.sh"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/crewvia/hooks/post-tool-use.sh"
          }
        ]
      }
    ]
  }
}
```

Replace `/path/to/crewvia` with the absolute path to your Crewvia installation.

> **Note**: Claude Code merges hook configurations, so existing hooks are preserved.
> You can verify the active hooks with `claude config list`.

---

## Starting Agents

The recommended way to launch agents is via the `./crewvia` launcher script,
which handles Taskvia mode selection, token loading, and agent startup in one step.

### Quick start

```bash
# Start Director (prompts for Taskvia mode if config/crewvia.yaml has taskvia: ask)
./crewvia

# Start Director with Taskvia approval flow enabled
./crewvia --taskvia

# Start Director in standalone mode (no Taskvia, no token needed)
./crewvia --no-taskvia

# Start a Worker
./crewvia worker docs research
./crewvia --no-taskvia worker code python
```

> **Taskvia mode priority**: `CREWVIA_TASKVIA` env var > `--taskvia/--no-taskvia` flag > `config/crewvia.yaml taskvia:` key > interactive prompt (`ask`)

Alternatively, use `scripts/start.sh` directly — the script sets `AGENT_NAME`
based on the role and skill set, then starts Claude Code.

### Start as Director

```bash
./scripts/start.sh director
```

The Director reads `agents/director.md` as its system prompt and begins
managing the kanban board. Only one Director should run per project.

### Start as Worker

```bash
./scripts/start.sh worker <skill1> <skill2> ...
```

Examples:

```bash
# Worker specializing in ops and bash scripting
./scripts/start.sh worker ops bash

# Worker specializing in Python development
./scripts/start.sh worker code python

# Worker for documentation and research
./scripts/start.sh worker docs research
```

The skill set is hashed to a consistent name from `config/worker-names.yaml`,
so the same skills always produce the same name (e.g., `ops bash` → "Kai").

### Running with tmux (optional)

When `./crewvia` starts in tmux mode, it automatically attaches you to the Director
window after launching. No manual `tmux attach` needed:

- **Outside tmux**: `exec tmux attach-session` opens the Director window in your terminal.
- **Inside tmux** (e.g. nested session): `tmux switch-client` switches to the `crewvia` session.

To manually set up a session instead:

```bash
# Create a session with Director + 3 Workers
tmux new-session -s crewvia -n director
tmux send-keys "cd /path/to/crewvia && ./scripts/start.sh director" Enter

tmux new-window -t crewvia -n workers
tmux split-window -h
tmux split-window -v
tmux select-pane -t crewvia:workers.1
tmux send-keys "cd /path/to/crewvia && ./scripts/start.sh worker ops bash" Enter
tmux select-pane -t crewvia:workers.2
tmux send-keys "cd /path/to/crewvia && ./scripts/start.sh worker code python" Enter
tmux select-pane -t crewvia:workers.3
tmux send-keys "cd /path/to/crewvia && ./scripts/start.sh worker docs research" Enter
```

---

## Taskvia Integration

[Taskvia](https://taskvia.vercel.app) is a lightweight kanban WebUI that serves as the
approval gateway and knowledge log for Crewvia agents.

### Getting your TASKVIA_TOKEN

1. Visit [taskvia.vercel.app](https://taskvia.vercel.app)
2. Sign in with your account
3. Navigate to **Settings → API Tokens**
4. Click **Generate new token**
5. Copy the token and set `TASKVIA_TOKEN` in your environment

### Getting your TASKVIA_URL

For the hosted service, use the default:
```
https://taskvia.vercel.app
```

For a self-hosted instance, set `TASKVIA_URL` to your deployment URL.

### Approval flow (PreToolUse hook)

When a Worker is about to execute a tool call, `hooks/pre-tool-use.sh` sends an
approval request to Taskvia. The card moves to **Awaiting Approval** on the kanban board.
You approve or deny from the WebUI (or Slack/Discord integration if configured).

```
Worker wants to run tool
        ↓
POST /api/request  →  card appears in "Awaiting Approval"
        ↓
poll /api/status/:id every 1s (up to 600s)
        ↓
approved → exit 0 (tool executes)
denied   → exit 1 (tool blocked, Claude Code shows error)
timeout  → exit 1 (blocked after 10 minutes)
```

### Knowledge logging (PostToolUse hook)

After tool execution, Workers post discoveries to `POST /api/log`. Log types:

| Type | Description | Retention |
|------|-------------|-----------|
| `knowledge` | Discoveries, patterns, caveats | Pushed to Obsidian |
| `improvement` | Improvement proposals | Pushed to Obsidian |
| `work` | Routine work logs | Temporary, discarded |

### Mobile push notifications via ntfy (optional)

Crewvia can send approval requests as push notifications to your iPhone via a
[self-hosted ntfy server](https://ntfy.sh). This enables one-tap approve/deny without
opening the Taskvia WebUI.

**How it works** (`CREWVIA_APPROVAL_CHANNEL=ntfy` or `both`):

```
Worker triggers tool call
        ↓
POST /api/request  { notify: true }  →  Taskvia
                                          ├─ Redis: approval card created
                                          ├─ Redis: one-time token (TTL 900s)
                                          └─ ntfy: push notification sent
                                                    ↓
                                          [iPhone ntfy app]
                                          ✓承認 / ✗却下 action button
                                                    ↓
                                          POST /api/approve-token/<token>
                                             or /api/deny-token/<token>
                                                    ↓
                                          Redis: card status → approved/denied
        ↓
poll /api/status/:id every 1s  →  detect approved/denied  →  exit 0/1
```

**Setup**:

1. Set up a self-hosted ntfy server (see [ntfy docs](https://docs.ntfy.sh/))
2. Create a topic with the `taskvia-approval-` prefix (e.g., `taskvia-approval-my32chartoken`)
3. Subscribe to the topic in the ntfy iOS app
4. Set environment variables:

```bash
export CREWVIA_APPROVAL_CHANNEL="ntfy"   # or "both" for ntfy + WebUI
export NTFY_URL="https://ntfy.example.com"
export NTFY_TOPIC="taskvia-approval-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export NTFY_USER="taskvia"
export NTFY_PASS="your-ntfy-password"
```

5. Set the same `NTFY_*` variables in Taskvia's environment (Vercel dashboard or `.env.local`)

> **Mode comparison**:
>
> | Mode | Approve via | Use case |
> |------|-------------|---------|
> | `taskvia` (default) | Browser WebUI | Desktop-first workflows |
> | `ntfy` | iPhone push notification | Mobile-first, away from desk |
> | `both` | Either WebUI or iPhone | Maximum flexibility |

### Verification push (crewvia QA → Taskvia)

After a Worker completes a task, the QA layer pushes verification results to Taskvia so Admirals can monitor status in the Board UI.

```
plan.sh verify-result <task_id> <verdict> [rework_count]
       ↓
scripts/taskvia-verification-sync.sh
       ↓
POST $TASKVIA_URL/api/verification    (Bearer $TASKVIA_TOKEN)
       ↓
Taskvia Board: verification badge updates within 5 seconds (polling)
```

**Prerequisites**: The approval card (`POST /api/request`) must already exist for the `task_id` before pushing verification data. The Board reads `approval:{id}` to render a card — `POST /api/verification` alone will not make a card appear.

**no-op when**: `TASKVIA_TOKEN` is unset or `CREWVIA_TASKVIA=disabled` — verification push is silently skipped.

**E2E testing**: Use `pnpm dev` (localhost) with a shared Upstash Redis. Vercel Preview URLs may be SSO-protected and block external POSTs (see [Troubleshooting](#troubleshooting)).

### Standalone mode (Taskvia disabled)

Run in standalone mode when you don't need approval gating — useful for local development,
CI pipelines, or when you haven't set up a Taskvia account yet.

**Three ways to enable standalone mode** (listed by priority):

```bash
# 1. Environment variable (highest priority)
CREWVIA_TASKVIA=disabled ./crewvia

# 2. CLI flag
./crewvia --no-taskvia

# 3. Permanent setting in config/crewvia.yaml
#    taskvia: disabled
```

When standalone mode is active:
- PreToolUse hook exits `0` immediately (all tools allowed without approval)
- PostToolUse hook skips log posting to Taskvia
- `TASKVIA_TOKEN` is not required and not read
- `scripts/taskvia-sync.sh` exits silently (no sync)
- `scripts/fetch-requests.sh` and `scripts/process-request.sh` exit with an error
  (these scripts require Taskvia by design)

> **Legacy behavior**: If `TASKVIA_TOKEN` is simply not set (and `CREWVIA_TASKVIA` is
> not `disabled`), Crewvia also falls back to standalone automatically — this preserves
> backwards compatibility for existing setups.

---

## Customizing worker-names.yaml

`config/worker-names.yaml` controls the name pool and optional customizations.

### Default behavior

Without customizations, all names in the pool can become either an Director or Worker.
Names are assigned deterministically based on the skill set hash:

```
skills: [ops, bash]  →  always assigned the same name (e.g., "Kai")
skills: [code, python]  →  always assigned a different name (e.g., "Luca")
```

This ensures **name continuity**: the same "Kai" personality accumulates knowledge
about `ops` and `bash` tasks across multiple sessions.

### Customization examples

Edit `config/worker-names.yaml` to customize name behavior:

```yaml
# Pin a name to a specific role
customizations:
  - name: Kai
    role: director    # This name is always Director, never a Worker

# Fix a name to specific skills
  - name: Luca
    role: worker
    skills: [code, python]   # Luca is always the Python coder

# Disable a name (remove from pool)
  - name: Sora
    disabled: true

# Allow a name for both roles (default behavior, explicit)
  - name: Mira
    role: any
```

### Adding new names

Add entries to the `names` list in `worker-names.yaml`:

```yaml
names:
  - Kai
  - Luca
  - Sora
  - Mira
  - Yuki       # ← new entry
  - Tariq      # ← new entry
```

The pool covers first names from East Asia, South Asia, Middle East, Europe,
the Americas, Africa, and Slavic regions — 50 names by default.

### Skills reference

| Tag | Capability |
|-----|-----------|
| `ops` | Infrastructure, server operations |
| `bash` | Shell scripting, command execution |
| `code` | General-purpose coding |
| `python` | Python development |
| `typescript` | TypeScript / JavaScript |
| `research` | Information gathering, analysis |
| `database` | DB operations, queries |
| `cloud` | Cloud platforms (AWS, OCI, GCP) |
| `docs` | Documentation writing |

---

## Autonomous Improvement System

Workers can propose and (within limits) self-execute minor improvements discovered
during task execution. The scope is controlled by `config/autonomous-improvement.yaml`.

### How it works

```
Worker discovers an improvement opportunity
           ↓
Check against autonomous-improvement.yaml
           ↓
allowed?  →  Report to Director as "improvement proposal"
              Director adds card to Backlog (low priority)
              Auto-executes when scheduled
           ↓
requires_approval?  →  POST /api/log with type: "improvement"
                        Logged to Obsidian for human review
                        Executed only when user explicitly requests it
```

### Default allowed improvements (no approval needed)

- `docs` — Documentation and README updates
- `refactor` — Code refactoring with no behavioral change
- `comment` — Adding/improving inline comments and log messages
- `test` — Adding new test cases

### Default requires-approval improvements

- `external` — Changes to external services or APIs
- `config` — Modifications to configuration files
- `delete` — Any deletion of files or directories
- `dependency` — Adding, updating, or removing packages
- `new_file` — Creating new files or directories

### Rate limiting

`max_per_day: 5` caps the number of autonomous improvements per day to prevent
runaway self-modification. Proposals exceeding the limit are escalated to
`requires_approval` automatically.

Set `max_per_day: 0` to disable autonomous improvements entirely.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Taskvia WebUI                           │
│     Backlog │ In Progress │ Awaiting Approval │ Done            │
└──────────────────────────┬──────────────────────────────────────┘
                           │ REST API
        ┌──────────────────┼──────────────────────┐
        │                  │                      │
┌───────▼──────┐   ┌───────▼──────┐       ┌───────▼──────┐
│ Director │   │   Worker A   │       │   Worker B   │
│   (1 agent)  │   │  ops, bash   │       │ code, python │
│              │   │   "Kai"      │       │   "Luca"     │
└──────┬───────┘   └──────┬───────┘       └──────┬───────┘
       │ ▲                │ ▲ assign              │ ▲ assign
       │ │notify          │ │ (tmux send-keys)    │ │ (tmux send-keys)
       │ │                └─┴─────────────────────┘─┘
       │ │                          │
       │ └──────────────────────────┤
       │                   ┌────────▼────────┐
       │                   │   Dispatcher    │
       │                   │  (bg daemon,    │
       └──────────────────►│   5-sec poll)   │
          notify (Worker   └─────────────────┘
          needed / done)
```

### Dispatcher モデル（tmux モード）

tmux モードでは `scripts/dispatcher.sh` が常駐バックグラウンドプロセスとして動作する。
Director が直接 Worker にタスクを送る代わりに、Dispatcher がタスクを割り当てる。

| コンポーネント | 役割 |
|---|---|
| **Director** | ミッション受領・プラン作成・Worker 起動・Dispatcher 通知への応答 |
| **Dispatcher** | 5秒ごとにタスク状況を確認し、idle Worker にタスクを割り当てる |
| **Worker** | Dispatcher からの assign を受け取り、`plan.sh pull` で取得して実行 |

### Communication flow

1. Director decomposes mission → registers tasks with `plan.sh add`
2. Director spawns Workers with required skills via `bash scripts/start.sh worker <skill>`
3. Dispatcher (started automatically) polls every 5 seconds for idle Workers + unblocked tasks
4. Dispatcher assigns tasks to idle Workers via `tmux send-keys`
5. Worker pulls assigned task via `plan.sh pull --task <id> --mission <slug>`, executes it
6. Before risky tool calls, PreToolUse hook requests approval from Taskvia
7. Worker reports completion via `plan.sh done`, then waits for next Dispatcher assign
8. Dispatcher notifies Director when a new Worker skill is needed or all missions are complete
9. Director responds to Dispatcher notifications (spawns Workers / archives mission)

### Kanban card structure

```json
{
  "card_id": "card-042",
  "column": "backlog",
  "assigned_to": "Kai",
  "priority": "high",
  "task": "Deploy updated API to OCI",
  "skills_required": ["ops", "bash"],
  "tool": "Bash(oci iam ...)",
  "blocked_by": ["card-038"]
}
```

Column transitions:
```
Backlog → In Progress → Awaiting Approval → Done
```

---

## Directory Structure

```
crewvia/
├── config/
│   ├── worker-names.yaml          # Name pool and customizations
│   ├── skills.yaml                # Skill tag definitions
│   └── autonomous-improvement.yaml  # Self-improvement scope settings
├── hooks/
│   ├── pre-tool-use.sh            # PreToolUse hook — Taskvia approval gate
│   └── post-tool-use.sh           # PostToolUse hook — knowledge log posting
├── agents/
│   ├── director.md            # Director system prompt
│   └── worker.md                  # Worker system prompt
├── scripts/
│   └── start.sh                   # Agent launcher (sets AGENT_NAME, starts Claude Code)
├── CLAUDE.md                      # System spec and design decisions
└── README.md                      # This file
```

---

## Troubleshooting

### ntfy push notifications not arriving on iPhone

**Step 1 — Verify ntfy server directly**

```bash
curl -u <NTFY_USER>:<NTFY_PASS> \
  -H "Title: test" -d "hello" \
  https://<NTFY_URL>/<NTFY_TOPIC>
# 200 → server is reachable
# 401/403 → check NTFY_USER / NTFY_PASS (server may use auth-default-access: deny-all)
```

> **Note**: Always test with `POST`, not `GET`. `GET` hits the subscribe endpoint and will
> return a streaming response (not 401/200 as a publish test). This distinction matters when
> scripting health checks (lesson from E2E testing).

**Step 2 — Verify Taskvia → ntfy path**

```bash
curl -X POST https://taskvia.vercel.app/api/request \
  -H "Authorization: Bearer <TASKVIA_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"tool":"test","agent":"debug","task_title":"test","notify":true}'
# Check: does the iPhone receive a notification within ~5 seconds?
# If not, the NTFY_* vars in Taskvia's environment are likely wrong or empty.
```

**Step 3 — Check Taskvia production environment variables (most common cause)**

> **⚠️ Lesson L1**: Vercel production environment variables are **completely independent** from
> `.env.local`. Even if ntfy works in local development, you must separately add `NTFY_URL`,
> `NTFY_TOPIC`, `NTFY_USER`, and `NTFY_PASS` to Vercel's **Settings > Environment Variables**
> for the **Production** environment.
>
> `vercel env ls` shows that a variable *exists* but cannot confirm its value is non-empty —
> Encrypted variables can be stored with an empty value. Run `vercel env pull` or send a live
> test notification to verify.

### ntfy notification arrives but approve/deny doesn't register

- The one-time token may have expired (`APPROVAL_TOKEN_TTL_SECONDS`, default 900 s)
- Debug via Upstash Redis: `SCAN 0 MATCH approval_token:*` — if the token key is missing, it has expired
- The `request_id` field in `approval_token:<token>` links back to `approval:<id>` for cross-referencing

### Silent failure — no notification and no error

> **⚠️ Lesson L2**: `publishApprovalRequest()` in Taskvia returns silently when `NTFY_URL` or
> `NTFY_TOPIC` is empty (`if (!ntfyUrl || !topic) return;`). No error is logged. To confirm
> whether the function ran:
>
> - Check Upstash Redis for `approval_token:*` keys. If a token exists, the function executed
>   past the early-return guard — the issue is downstream (network, wrong topic, auth).
> - If no token exists, `NTFY_URL` or `NTFY_TOPIC` is empty in the Taskvia environment.
>   Check Vercel dashboard → Settings → Environment Variables.

### Verification badge not appearing on Board (Lesson L6)

The Board renders cards from `approval:index` → `approval:{id}`. The verification badge is overlaid onto an existing card using `task_id`. If you only call `POST /api/verification` without a prior `POST /api/request`, no card will appear on the Board.

**Fix**: Always create the approval card first, then push verification data using the same `task_id`.

### Approval card disappears before verification push (Lesson L7)

`approval:{id}` has a TTL of 600 seconds. If the verification push is delayed more than ~10 minutes after card creation, the card expires and the index becomes an orphan.

**Fix**: Push verification results immediately after the QA run completes. In practice, complete the full `pending → verifying → verified` cycle within one 10-minute window.

### POST to Vercel Preview is blocked (Lesson L4)

Vercel Preview URLs may be protected by Team SSO, blocking external HTTP POSTs from scripts.

**Fix**: Run E2E tests against `pnpm dev` (localhost) using the same Upstash Redis instance. To bypass SSO on Preview, set `VERCEL_AUTOMATION_BYPASS_SECRET` in Vercel project settings.

### `vercel curl` does not support POST (Lesson L5)

`vercel curl` (≤ v50.37.3) ignores `-X POST` and only performs GET requests.

**Fix**: Use standard `curl` with `Authorization: Bearer <TASKVIA_TOKEN>`:

```bash
curl -X POST "$TASKVIA_URL/api/verification" \
  -H "Authorization: Bearer $TASKVIA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "task_id": "...", "verdict": "pass", ... }'
```

### `CREWVIA_VERIFICATION_UI` has no effect (Lesson L8)

This variable is read **server-side** by Taskvia (Server Component / Server Action). It does **not** need the `NEXT_PUBLIC_` prefix. Set it in **Taskvia's** Vercel environment variables, not in crewvia.

```bash
# ✅ Correct — server-side env var in Taskvia's Vercel project
CREWVIA_VERIFICATION_UI=disabled

# ❌ Wrong — NEXT_PUBLIC_ prefix is unnecessary and will not work
NEXT_PUBLIC_CREWVIA_VERIFICATION_UI=disabled
```

---

## Contributing

Issues and pull requests are welcome on [GitHub](https://github.com/tyz-works/crewvia).

---

## License

MIT
