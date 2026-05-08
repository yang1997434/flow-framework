---
slug: v0.8.2.1-rc-park-fix
status: active   # active | paused | blocked | done
phase: implement    # triage | research | implement | check | verify | sediment
# blocked_by: list of task slugs this task depends on. Used by `flow task status`
# to draw the dependency graph (parent slugs must finish first). Default: empty.
# Example:
#   blocked_by:
#     - capability-registry-and-model-roles
#     - prereq-installer-and-doctor
blocked_by: []
---

# progress.md — v0.8.2.1-rc-park-fix

## Plan

**Dispatch decision**: single Opus subagent + worktree isolation
+ mandatory codex review gate (per session_latest 反思固化的规则：
state-machine + rc value 改动必走 opus mandatory gate）。

**Files in scope (8)**:

| 类型 | 文件 | 操作 |
|------|------|------|
| 新建 | `scripts/common/exit_codes.py` | `Final[int]` 6 常量，零 side effect |
| 编辑 | `scripts/flow_orchestrator.py` | 6 处 site 迁移（L4953/L4980-86/L5226-34/L5354-62/L5418-35/L5571-82）+ import |
| 编辑 | `tests/smoke/test_phase2_retry_loop.py` | 6 处硬钉点全量迁 + 类名/方法名 rename + import |
| 新建 | `tests/smoke/test_exit_codes_module.py` | 6 常量值 + import 风格 + 无 side effect |
| 编辑 | `claude/skills/flow/flow-phase2-execute/SKILL.md` | L168 + L171-178 改 rc=2→5 |
| 新建/扩展 | SKILL.md 契约测试 | 归一化匹配（去 `` ` ``/bold/whitespace）断言 |
| 编辑 | `CHANGELOG.md` | 顶部加 v0.8.2.1 节，含 "Observable change: rc=2→5" |
| 编辑 | `VERSION` | `0.8.2` → `0.8.2.1` |

**Dispatch protocol**：
1. Phase 0：commit 当前 task dir 到 master（`wip: pre-fork PRD commit for v0.8.2.1`）
   —— mitigation for pitfall `worktree-fork-before-prd-commit`
2. fork worktree `feat+v0.8.2.1-rc-park-fix`
3. Agent(subagent_type=general-purpose, model=opus, isolation=worktree)
   prompt 含 inline 完整 spec + Acceptance Criteria + 18-class blindspot
   summary（reviewer 也要看）+ K-class 红线 + 自查 checklist
4. 完成后回主 session：跑 `python3 -m unittest tests/smoke/...` + 现有 939
   suite 不退化检查
5. **Mandatory codex review gate**（opus model）：迭代到 0 P1
6. Phase 3 verify report，FF merge → tag v0.8.2.1 → GitHub release

## Execute Log

<!-- TEMPLATE: 未填写。Phase 2 渐进 append。每个 sub-agent / 主 session 完成一段工作时追加一行。 -->

<!-- 表格示例（首行为表头，自动生效）：
| 时间 (YYYY-MM-DD HH:MM) | Agent | Scope | Outcome |
|------|-------|-------|---------|
-->

## Verify Report

<!-- TEMPLATE: 未填写。Phase 3 末写。各项必须有具体值（pass / fail / 跳过原因），不能留 pending。 -->

## Sediment Notes

<!-- TEMPLATE: 未填写。Phase 4 末写。强制写一段——即使"no new sediment"也要明确写。 -->

## Retro (optional)

<!-- TEMPLATE: 自由格式回顾——什么 worked / didn't / 框架反馈。可省略。 -->

## Files Touched

_Updated 2026-05-08 05:43 (last 20 unique edits)_:

- `.flow/tasks/05-08-v0.8.2.1-rc-park-fix/progress.md`
- `.flow/tasks/05-08-v0.8.2.1-rc-park-fix/prd.md`
- `/tmp/codex-prompt-v0821-r3.txt`
- `/tmp/codex-prompt-v0821-r2.txt`
- `/tmp/codex-prompt-v0821.txt`
- `/home/yangpeng/.claude/projects/-data-Claude-flow-framework/memory/session_latest.md`
- `.flow/pitfalls/hook-blocks-after-reviewer-pass.md`
- `/tmp/flow_pause_save.py`
- `.flow/tasks/05-08-v0.8.2-p0-core/progress.md`
- `/home/yangpeng/.claude/projects/-data-Claude-flow-framework/memory/MEMORY.md`
- `/home/yangpeng/.claude/projects/-data-Claude-flow-framework/memory/feedback_model_alias_subscription_verify.md`
- `/home/yangpeng/.claude/settings.json`
- `CHANGELOG.md`
- `VERSION`
- `.claude/worktrees/feat+v0.8.2-p0-core/tests/smoke/test_phase2_retry_loop.py`
- `.claude/worktrees/feat+v0.8.2-p0-core/scripts/flow_orchestrator.py`
- `.claude/worktrees/feat+v0.8.2-p0-core/claude/capabilities/defaults.json`
- `.claude/worktrees/feat+v0.8.2-p0-core/claude/skills/flow/flow-phase2-execute/SKILL.md`
- `.claude/worktrees/feat+v0.8.2-p0-core/tests/smoke/test_dual_counter_invariants.py`
- `.claude/worktrees/feat+v0.8.2-p0-core/tests/smoke/test_afk_signals.py`
