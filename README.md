[English](README.md) | [中文](README.zh-CN.md)

# Flow Framework

> **Personal AI coding harness — orchestrates Claude Code + your skill stack across 4 phases with auto-memory, sub-agent isolation, and cross-model review.**

A composable framework that wraps your existing skill ecosystem (superpowers / impeccable / gstack / pr-review-toolkit / planning-with-files / Trellis-style file persistence) into a coherent 4-phase workflow with automatic memory promotion and pitfall capture.

**Status**: v0.8.1. Autonomy enabled — contract schema + 8-gate safety stack execute end-to-end. v0.7 4-phase workflow + capability registry + parallel subagent dispatch all unchanged.

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

**v0.7+ feature highlights**:

- **v0.7+: dependency-aware parallel subagent dispatch** — declare per-task `writes:` glob in plans; framework verifies disjointness, runs independent tasks in parallel waves with sequential cross-wave integration. See `docs/superpowers/specs/2026-05-05-v0.7-parallel-dispatch-design.md`.
- **v0.8.1 — Autonomy enabled**: Set `autonomy_mode: auto` in your contract
  to enable orchestrator-driven execution. The 8-gate safety stack ships in
  v0.8.1: worktree-per-task isolation, manifest enforcement, codex review,
  acceptance criteria, atomic merge, post-merge verify, AFK + budget caps
  (schema-only; runtime in v0.8.2), crash recovery, and nested-autonomy
  abort. Read `docs/v0.8.1-autonomy-enabled.md` for the full migration
  guide; `docs/v0.8-migration.md` covers the v0.8.0 contract groundwork.

## Quick start

```bash
# Clone
git clone <this-repo> /path/to/flow-framework
cd /path/to/flow-framework

# Install (declarative — driven by dependencies.json)
./install.sh                   # or `./install.sh --dry-run` to preview
flow doctor                    # verify

# In any project:
cd <your-project>
flow init

# Start a task (in Claude Code, after install):
/flow:start "<task description>"
```

`install.sh` handles: marketplaces (`claude plugin marketplace add`) → required plugins (`claude plugin install`) → hooks (merged into `~/.claude/settings.json` with isolated matcher entries) → CLI shim. See [`docs/USAGE.md`](docs/USAGE.md) for full detail.

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

- **[`docs/USAGE.md`](docs/USAGE.md)** — Step-by-step usage guide (install / first-time setup / daily workflow / troubleshooting / cross-machine sync) ← **start here**
- [`docs/编码框架.md`](docs/编码框架.md) — Full design (4 phases, 3 tiers, model routing, pitfalls, SSH adaptation)
- [`docs/Skills-Phase映射.md`](docs/Skills-Phase映射.md) — Complete skill × phase trigger map
- [`docs/框架对比.md`](docs/框架对比.md) — Flow vs Trellis / Cursor / Devin / CrewAI / Aider comparison
- [`docs/调研方法论.md`](docs/调研方法论.md) — Research methodology (sub-agent isolation + file persistence)
- [`docs/Trellis调研.md`](docs/Trellis调研.md) — Trellis architecture deep dive (inspiration source)

## License

MIT (this framework). Underlying tools have their own licenses.

## Status

v0.8.1. Capability registry stable (37 caps incl. `autonomy_orchestrator` + `acceptance_verify` promoted); v0.7 wave-dispatch + disjointness verification stable; v0.8.1 autonomy executes end-to-end behind an 8-gate safety stack. See `CHANGELOG.md` for release history.
