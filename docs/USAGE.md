# Flow Framework Usage Guide

Concrete how-to for installing and using Flow on a new machine.

## Table of contents

- [1. Installation (new machine)](#1-installation-new-machine)
- [2. First-time project setup](#2-first-time-project-setup)
- [3. Daily task workflow](#3-daily-task-workflow)
- [4. Hook installation (optional)](#4-hook-installation-optional)
- [5. Troubleshooting](#5-troubleshooting)
- [6. Cross-machine sync](#6-cross-machine-sync)

---

## 1. Installation (new machine)

### Prerequisites

- Python 3.9+
- git
- bash
- (optional) `gh` CLI for GitHub-related ops
- (optional) Claude Code installed at `~/.claude/`

### Steps

```bash
# 1. Clone
git clone git@github.com:yang1997434/flow-framework.git ~/projects/flow-framework
cd ~/projects/flow-framework

# 2. Install (creates symlinks under ~/.claude/, sets up ~/.flow/)
./install.sh

# 3. Verify
flow version              # should print: flow 0.3.0
ls -la ~/.claude/commands/flow ~/.claude/skills/flow

# 4. Fill in your machine_id + remote_targets
$EDITOR ~/.flow/credentials.local
# This file is chmod 600, gitignored. Set machine_id to e.g. "ccs-maruiao".
```

### What install.sh does

- Symlinks `claude/commands/flow` → `~/.claude/commands/flow` (8 slash commands)
- Symlinks `claude/skills/flow` → `~/.claude/skills/flow` (orchestrator + 4 phase skills)
- Creates `~/.flow/` (chmod 700) and stub `credentials.local` (chmod 600)
- Creates `~/.local/bin/flow` CLI dispatcher

### What install.sh does NOT do

- **Does not modify `~/.claude/settings.json`** — hooks are opt-in (see [Section 4](#4-hook-installation-optional))
- **Does not write any credentials** — you fill them in manually

### Uninstall

```bash
cd ~/projects/flow-framework
./uninstall.sh
```

Removes symlinks. Leaves `~/.flow/credentials.local` and project `.flow/` directories alone.

---

## 2. First-time project setup

```bash
cd <your-project>            # any git repo or non-git dir
flow init                    # bootstraps .flow/ structure
```

### What this creates

```
<project>/
├── .flow/
│   ├── config.yaml          # project-level config (committed)
│   ├── tasks/               # active tasks
│   │   └── archive/         # archived tasks (committed)
│   ├── ADRs/                # architectural decisions (committed)
│   ├── patterns/            # reusable patterns (committed)
│   ├── pitfalls/            # project-level gotchas (committed)
│   ├── workspace/           # per-developer journals
│   │   ├── <user>/          # your journal (gitignored by default)
│   │   └── .gitkeep
│   └── .runtime/            # session state (gitignored)
└── .gitignore               # auto-appended Flow block
```

### .gitignore additions

`flow init` appends this block to `.gitignore`:

```
# Flow Framework — runtime + machine-local + per-user workspace
.flow/.runtime/
.flow/.current-task
.flow/config.local.yaml
.flow/workspace/*
!.flow/workspace/.gitkeep
.flow/**/*.tmp
.flow/**/.backup-*
```

This means: spec / ADRs / patterns / pitfalls **are committed**; per-user journals are **not** by default.

### Team mode (optional)

Edit `.flow/config.yaml` → `team_mode: true` to commit per-developer journals (visible to teammates).

---

## 3. Daily task workflow

### Start a task

In Claude Code:

```
/flow:start "<task description>"
```

Or just say: `走 Flow: <task>` / `use flow: <task>` (if hooks installed).

This:
1. Triages complexity (trivial / simple / moderate / complex) and task type (backend / frontend / data / doc / deploy / research)
2. **Trivial → exits framework**, just does the work
3. **Simple+ → creates** `.flow/tasks/<MM-DD-slug>/{prd.md, progress.md}`
4. Runs Phase 1 brainstorm (loads `superpowers:brainstorming`, plus `impeccable:shape` for UI tasks)

### Advance through phases

```
/flow:continue
```

Reads `progress.md` state, determines current phase, runs next step.

| Phase 1 → Phase 2 | Phase 2 → Phase 3 | Phase 3 → Phase 4 |
|-------|-------|-------|
| Brainstorm complete, prd.md confirmed | Implementation done, all sub-agents reported | Verify passed, commit done |

### Take a break

```
/flow:pause
```

Saves progress to journal without archiving. Resume later with `/flow:resume`.

### Resume later

```
/flow:resume
```

Reads active task, runs staleness check (do referenced files still exist?), shows current state.

### Capture a pitfall

When you hit a "would have saved time if I'd known" moment:

```
/flow:pitfall <one-line symptom>
```

Walks you through filling in: Symptom / Root cause / Fix / Prevention / Why it matters / `trigger_paths`.

### Finish a task

```
/flow:finish
```

This is the "task is complete" command:

1. Runs Phase 3 final verify (fresh-context sub-agent on diff + prd.md)
2. (If triggered) runs `/codex review` cross-model
3. Drafts commit message → confirms with you → commits
4. Runs Phase 4 sediment (asks about ADR / pattern / pitfall promotion)
5. Auto-saves journal entry
6. Archives task to `.flow/tasks/archive/<YYYY-MM>/`

### Promote knowledge between tiers

```
/flow:promote .flow/patterns/foo.md vault     # project → vault
/flow:promote ~/data/knowledge-base/patterns/bar.md rules  # vault → ~/.claude/rules/
```

### Trigger Codex review manually

```
/flow:codex-review
```

Use when phase 3 didn't auto-trigger but you want a cross-model second opinion.

---

## 4. Hook installation (optional)

Hooks add automation:

| Hook | Adds |
|------|------|
| `session-start.py` | Quick Read Guide + active task auto-loaded each session |
| `user-prompt-submit.py` | Detect "走 Flow" / "flow:" keywords without explicit slash command |
| `post-tool-bash.py` | Credential grep self-check after `git commit` |
| `stop.py` | Auto-save current task progress on session end |

### Install

1. Open `~/.claude/settings.json`
2. **If you don't have a `hooks` section**: append the contents of `claude/hooks/settings.json.snippet`
3. **If you have hooks already**: merge carefully — preserve existing handlers

Example merge for SessionStart:

```json
"SessionStart": [
  { "matcher": "startup", "hooks": [
    { "type": "command", "command": "your-existing-hook.sh" },
    { "type": "command", "command": "python3 ~/projects/flow-framework/claude/hooks/session-start.py", "timeout": 10 }
  ]}
]
```

### Disable

Remove the relevant entries from `settings.json`. Or use `~/.claude/settings.local.json` to override.

---

## 5. Troubleshooting

### Symptom: `/flow:start` not recognized

- Verify symlinks: `ls -la ~/.claude/commands/flow` should show a symlink to the repo
- If not: re-run `./install.sh`
- If still not: restart Claude Code

### Symptom: `flow init` in project says "command not found"

- Verify `~/.local/bin` is in `PATH`: `echo $PATH | tr ':' '\n' | grep .local/bin`
- If not: add `export PATH="$HOME/.local/bin:$PATH"` to your shell rc
- Or call directly: `python3 ~/projects/flow-framework/scripts/flow_init.py`

### Symptom: hooks not firing

- Verify hooks in `~/.claude/settings.json` — both file path and JSON syntax
- Test the hook manually: `echo '{}' | python3 ~/projects/flow-framework/claude/hooks/session-start.py`
- Hooks fail silently by design — check Claude Code logs if available

### Symptom: credential grep false positives

The grep pattern matches things like `password:` `api_key=` `token: "..."`. False positives common in:
- Code that uses these words for non-credential purposes (e.g. `password_min_length: 8`)
- Documentation / templates

Fix:
- Rename the matched key (e.g. `pwd_min` instead of `password_min`)
- Or add the false-positive file to a project-level allowlist (future feature)

### Symptom: stale memory references

Run manually: `python3 ~/projects/flow-framework/scripts/flow_staleness.py --scope project`

This reports any `path` references that no longer exist. v0.3.1 will add interactive update.

---

## 6. Cross-machine sync

If you work across multiple machines (e.g., via SSH), each machine needs its own install:

```bash
# Machine A
git clone git@github.com:yang1997434/flow-framework.git ~/projects/flow-framework
cd ~/projects/flow-framework && ./install.sh

# Machine B
git clone git@github.com:yang1997434/flow-framework.git ~/projects/flow-framework
cd ~/projects/flow-framework && ./install.sh

# Each machine fills in its own ~/.flow/credentials.local with its machine_id
# Project .flow/ syncs via the project's own git history
```

### Updating the framework

```bash
cd ~/projects/flow-framework
git pull
# Symlinks already point at the repo, no re-install needed
# (unless install.sh itself changed, in which case run ./install.sh again)
```

### Cross-machine vault

The framework references `~/data/knowledge-base/` as the personal vault for `pitfalls/` and `patterns/`. Set up sync separately (e.g., git, Syncthing, rsync).
