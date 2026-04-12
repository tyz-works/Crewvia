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
5. [Managing Tasks with plan.sh](#managing-tasks-with-plansh)
6. [Taskvia Integration](#taskvia-integration)
7. [Watchdog](#watchdog)
8. [Customizing worker-names.yaml](#customizing-worker-namesyaml)
9. [Autonomous Improvement System](#autonomous-improvement-system)
10. [Architecture](#architecture)
11. [Directory Structure](#directory-structure)

---

## Overview

Crewvia follows a two-role model:

| Role | Description |
|------|-------------|
| **Orchestrator** | Decomposes tasks, creates cards, assigns Workers, manages kanban column transitions (Backlog → In Progress → Done) |
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
| `TASKVIA_URL` | Optional | Taskvia WebUI URL (default: `https://taskvia.vercel.app`) |
| `TASKVIA_TOKEN` | Optional | Taskvia API authentication token. If unset, standalone mode is used |
| `AGENT_NAME` | Set by `start.sh` | Name assigned to this agent instance |
| `TASK_TITLE` | Set at runtime | Human-readable title of the current task card |
| `TASK_ID` | Set at runtime | Card ID of the current task (e.g., `card-042`) |

Example `.env`-style configuration:

```bash
export TASKVIA_URL="https://taskvia.vercel.app"
export TASKVIA_TOKEN="tvk_xxxxxxxxxxxxxxxxxxxx"
```

> **Standalone mode**: If `TASKVIA_TOKEN` is not set, approval requests are skipped
> (PreToolUse hook exits 0 immediately) and knowledge logs are printed to stdout only.

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

Use `scripts/start.sh` to launch agents. The script sets the `AGENT_NAME` environment
variable based on the role and skill set, then starts Claude Code.

### Start as Orchestrator

```bash
./scripts/start.sh orchestrator
```

The Orchestrator reads `agents/orchestrator.md` as its system prompt and begins
managing the kanban board. Only one Orchestrator should run per project.

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

```bash
# Create a session with Orchestrator + 3 Workers
tmux new-session -s crewvia -n orchestrator
tmux send-keys "cd /path/to/crewvia && ./scripts/start.sh orchestrator" Enter

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

## Managing Tasks with plan.sh

`scripts/plan.sh` is the task plan management CLI. It supports multiple concurrent missions
and is the primary interface for Orchestrators and Workers to manage task flow.

### Basic usage

```bash
# Initialize a new mission
./scripts/plan.sh init "My mission title"

# Add a task to the current mission
./scripts/plan.sh add "Task title" --skills docs,research --priority medium

# Add a task blocked by another
./scripts/plan.sh add "Task B" --skills code --blocked-by t001

# Pull the next available task (Workers call this in a loop)
./scripts/plan.sh pull --skills docs,research --agent Jiwon

# Mark a task as done
./scripts/plan.sh done t002 "Updated README with new sections" --mission 20260412-example

# Show mission status
./scripts/plan.sh status
./scripts/plan.sh status --mission 20260412-example
./scripts/plan.sh status --all   # show all active missions
```

### Multi-mission support

Multiple missions can be active simultaneously. Each mission lives under
`queue/missions/<slug>/` and has its own task list:

```
queue/
  state.yaml              # active mission slugs + default_mission
  missions/
    20260412-auth/        # mission slug (auto-generated or explicit)
      mission.yaml        # title, status, next_task_id
      tasks/
        t001.md           # frontmatter + Description / Result
        t002.md
    20260412-refactor/
      mission.yaml
      tasks/
        t001.md
  archive/                # completed missions moved here by plan.sh archive
```

When multiple missions are active, `pull` picks the highest-priority available task
across all missions. Pass `--mission <slug>` to scope operations to a single mission.

### `--target-dir` option

Workers can be scoped to a specific external project directory using `--target-dir`.
This enables a single Crewvia installation to coordinate work across multiple repositories.

```bash
# Add a task targeting an external project
./scripts/plan.sh add "Update config" --skills ops --target-dir /path/to/other-repo

# Pull only tasks for a specific target directory
./scripts/plan.sh pull --skills ops --target-dir /path/to/other-repo
```

When `TARGET_DIR` is set in the environment (done automatically by `start.sh`),
`pull` filters tasks to only return those matching that directory.
Tasks with no `target_dir` are treated as crewvia-local tasks.

> **Important**: Workers operating on a target project must use absolute paths
> (`$CREWVIA_REPO/scripts/plan.sh`) for all plan.sh calls, since their working
> directory is set to the target project.

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

### Standalone mode (no TASKVIA_TOKEN)

When `TASKVIA_TOKEN` is not set:
- PreToolUse hook exits `0` immediately (all tools allowed)
- PostToolUse hook prints log entries to stdout only
- No approval UI is available; suitable for trusted local development

---

## Watchdog

`scripts/watchdog.sh` monitors Worker liveness using heartbeat files in `registry/heartbeats/`.
It is automatically started by `start.sh` when launching an Orchestrator.

### How it works

Each Worker periodically writes a Unix timestamp to `registry/heartbeats/<AgentName>`.
The watchdog checks all heartbeat files at a configurable interval and alerts when a
Worker has not updated its heartbeat recently.

```
registry/heartbeats/
  Kai          # last heartbeat timestamp (Unix seconds)
  Luca
  Jiwon
```

When a Worker goes stale:
- A warning is printed to stderr: `[watchdog] STALE: Kai — last heartbeat 720秒前`
- If `TASKVIA_TOKEN` is set, an `alert` log is posted to Taskvia

### Configuration

```bash
# Default settings (started automatically with Orchestrator)
./scripts/watchdog.sh

# Custom threshold and interval
./scripts/watchdog.sh --threshold 300 --interval 30
```

| Option | Default | Description |
|--------|---------|-------------|
| `--threshold` | `600` (10 min) | Seconds without heartbeat before a Worker is considered stale |
| `--interval` | `60` (1 min) | How often the watchdog polls heartbeat files |

### Manual start

The watchdog runs automatically when you start an Orchestrator. To run it manually
(e.g., in standalone mode without tmux):

```bash
bash scripts/watchdog.sh --threshold 300 &
```

---

## Customizing worker-names.yaml

`config/worker-names.yaml` controls the name pool and optional customizations.

### Default behavior

Without customizations, all names in the pool can become either an Orchestrator or Worker.
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
    role: orchestrator    # This name is always Orchestrator, never a Worker

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
allowed?  →  Report to Orchestrator as "improvement proposal"
              Orchestrator adds card to Backlog (low priority)
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
┌─────────────────────────────────────────────────────────────┐
│                        Taskvia WebUI                        │
│     Backlog │ In Progress │ Awaiting Approval │ Done        │
└──────────────────────────┬──────────────────────────────────┘
                           │ REST API
        ┌──────────────────┼──────────────────┐
        │                  │                  │
┌───────▼──────┐   ┌───────▼──────┐   ┌───────▼──────┐
│ Orchestrator │   │   Worker A   │   │   Worker B   │
│   (1 agent)  │   │  ops, bash   │   │ code, python │
│              │   │   "Kai"      │   │   "Luca"     │
└──────┬───────┘   └──────┬───────┘   └──────┬───────┘
       │ assign            │ pre-tool-use      │ post-tool-use
       │ cards             │ approval hook     │ knowledge hook
       └───────────────────┴───────────────────┘
                     Claude Code CLI
```

### Communication flow

1. Orchestrator pulls cards from Taskvia Backlog → moves to In Progress
2. Orchestrator assigns card to Worker with matching skills
3. Worker executes task; before risky tools, PreToolUse hook requests approval
4. Taskvia card moves to **Awaiting Approval**; user approves/denies in WebUI
5. Worker completes task; PostToolUse hook posts knowledge log
6. Worker reports completion to Orchestrator
7. Orchestrator moves card to **Done** in Taskvia

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
│   ├── orchestrator.md            # Orchestrator system prompt
│   └── worker.md                  # Worker system prompt
├── scripts/
│   ├── start.sh                   # Agent launcher (sets AGENT_NAME, starts Claude Code)
│   ├── plan.sh                    # Task plan management CLI (multi-mission, target-dir)
│   ├── watchdog.sh                # Worker liveness monitor (heartbeat-based)
│   ├── taskvia-sync.sh            # queue → Taskvia sync
│   ├── assign-name.sh             # Skill-hash → Worker name assignment
│   └── git-helpers.sh             # Shared git utilities
├── queue/
│   ├── state.yaml                 # Active mission slugs + default_mission
│   ├── missions/<slug>/           # Per-mission task files
│   │   ├── mission.yaml           # Title, status, next_task_id
│   │   └── tasks/tNNN.md          # Task frontmatter + Description / Result
│   └── archive/                   # Completed missions
├── registry/
│   ├── workers.yaml               # Worker skills and experience (task_count)
│   └── heartbeats/                # Worker liveness files (written by Workers, read by watchdog)
├── knowledge/
│   └── <skill>.md                 # Per-skill knowledge base (injected into Worker prompts)
├── CLAUDE.md                      # System spec and design decisions
└── README.md                      # This file
```

---

## Contributing

Issues and pull requests are welcome on [GitHub](https://github.com/tyz-works/crewvia).

---

## License

MIT
