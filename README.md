[English](README.md) | [中文](README.zh-CN.md)

# Flow Framework

> **Personal AI coding harness — orchestrates Claude Code + your skill stack across 4 phases with auto-memory, sub-agent isolation, and cross-model review.**

A composable framework that wraps your existing skill ecosystem (superpowers / impeccable / gstack / pr-review-toolkit / planning-with-files / Trellis-style file persistence) into a coherent 4-phase workflow with automatic memory promotion and pitfall capture.

**Status**: v0.3.0-alpha. Foundation laid; expect iteration as real projects expose gaps.

## What it does

| Capability | How |
|------------|-----|
| **Sub-agent isolation** | Phase 2 dispatches `Agent(isolation: "worktree")` with explicit scope; main session integrates |
| **Multi-line parallel** | git worktrees + per-worktree sub-agent, scope must not overlap |
| **Memory across sessions** | 3-tier promotion (`.flow/` → vault `patterns/` → `~/.claude/rules/`) + auto-save hooks |
| **Cross-model review** | Phase 3 invokes `/codex review` (GPT-5.5) on critical changes |
| **Token routing** | Triage = Haiku, research = Sonnet, implement = Opus, etc. |
| **Pitfall library** | Standalone `pitfalls/` tree + `trigger_paths` auto-loading |
| **Credential safety** | Vault never holds secrets; `~/.flow/credentials.local` + grep self-check |
| **Remote SSH ready** | Relative paths, no GUI deps, machine-id-keyed runtime |

## Quick start

```bash
# Clone (private repo or local)
git clone <this-repo> ~/projects/flow-framework
cd ~/projects/flow-framework

# Install (creates symlinks under ~/.claude/, ~/.flow/, etc.)
./install.sh

# In any project:
cd <your-project>
python3 ~/projects/flow-framework/scripts/flow_init.py
# Or once installed: flow init

# Start a task:
# (in Claude Code, after install)
/flow:start "<task description>"
```

## 4-Phase Workflow

```
[Triage] ─→ Phase 1 Plan ─→ Phase 2 Execute ─→ Phase 3 Finish ─→ Phase 4 Sediment
trivial             brainstorm    sub-agent      verify+codex    promote+save
└─→ skip              research      worktree       review          archive
                      ADR-lite      check          commit
```

See [docs/编码框架.md](docs/编码框架.md) for full design (also mirrored in personal vault).

## Slash commands

After install, in Claude Code:

| Command | What it does |
|---------|--------------|
| `/flow:start <task>` | Triage + create `.flow/tasks/<slug>/` + run Phase 1 |
| `/flow:continue` | Advance current task to next phase step |
| `/flow:resume` | Resume from breakpoint with staleness check |
| `/flow:finish` | Run Phase 3 verify + Phase 4 sediment + auto-save |
| `/flow:pitfall <symptom>` | Capture a pitfall to project or vault |
| `/flow:promote <file> <tier>` | Manually promote knowledge between tiers |
| `/flow:codex-review` | Manually trigger cross-model review |
| `/flow:pause` | Save state before context switch |

## Hooks (auto-active after install)

| Hook | Trigger | What it does |
|------|---------|--------------|
| `session-start.py` | Session start / clear / compact | Inject Quick Read Guide + active task + relevant pitfalls |
| `user-prompt-submit.py` | Each user message | Detect "走 Flow"/"flow:" keywords → route to orchestrator |
| `post-tool-bash.py` | After git commit | Run credential grep self-check |
| `stop.py` | Session end | Auto-save current task progress to journal |

## Repo layout

```
flow-framework/
├── docs/             # Design source (mirror of vault)
├── claude/           # Installed to ~/.claude/
│   ├── commands/flow/
│   ├── skills/flow/
│   └── hooks/
├── scripts/          # Python utilities
├── templates/        # File templates (prd, progress, pitfall, etc.)
├── install.sh / uninstall.sh
└── VERSION
```

## Documentation

- [`docs/编码框架.md`](docs/编码框架.md) — Full design (4 phases, 3 tiers, model routing, pitfalls, SSH adaptation)
- [`docs/Skills-Phase映射.md`](docs/Skills-Phase映射.md) — Complete skill × phase trigger map
- [`docs/框架对比.md`](docs/框架对比.md) — Flow vs Trellis / Cursor / Devin / CrewAI / Aider comparison
- [`docs/调研方法论.md`](docs/调研方法论.md) — Research methodology (sub-agent isolation + file persistence)

## License

MIT (this framework). Underlying tools have their own licenses.

## Status

v0.3.0-alpha. Designed, partially implemented, not yet tested on a real coding project.
Real-project shakedown will produce v0.3.1.
