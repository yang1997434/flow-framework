---
slug: v0.8.3-p0.2-brief-sentinel-fullset
status: active   # active | paused | blocked | done
phase: implement   # triage | research | implement | check | verify | sediment
# blocked_by: list of task slugs this task depends on. Used by `flow task status`
# to draw the dependency graph (parent slugs must finish first). Default: empty.
# Example:
#   blocked_by:
#     - capability-registry-and-model-roles
#     - prereq-installer-and-doctor
blocked_by: []
---

# progress.md — v0.8.3-p0.2-brief-sentinel-fullset

## Plan

**Dispatch**: single implementer subagent in fresh worktree
（紧耦合：dispatch shim + orchestrator wire + tests + docs 同步改动；
拆 N 个 agent 反而冲突风险高 + scope 互交叉）。

### Implementer scope（覆盖 PRD 全部 ACs）

1. `scripts/flow_subagent_dispatch.py`
   - `invoke()` 改签名：移除 `**_kw`；加 `prompt_prefix: str = ""` + `round_num: int = 1`
   - 类型校验 (str-only)；worktree-layout assertion
   - Formatter().parse() fail-closed 检查
   - 写文件 `<repo_root>/.flow/.runtime/<slug>+<task_id>+r<round>/dispatch_prefix.txt`
   - `_resolve_cmd_template` docstring + RuntimeError 文案更新

2. `scripts/flow_orchestrator.py`
   - `auto_dispatch_task` 加 `prompt_prefix: str = ""` 参数 + 透传给 `dispatch_fn`
   - `_cmd_auto_execute`（line ~5945-5975）：在 recovery proceed 后 + 调
     `auto_dispatch_task` 前 build prefix（`_render_task_brief` +
     `build_implementer_prompt`）

3. `claude/skills/flow/flow-phase2-execute/SKILL.md`
   - § "Implementer prompt — K-class sentinel prohibition" 加 transport 段
   - operator template 范例（`cat {prompt_prefix_file}` 必须真拼进 prompt）

4. `claude/capabilities/defaults.json`
   - `autonomy_orchestrator` 文档 / placeholder list 更新

5. `tests/smoke/test_subagent_dispatch_shim.py` — 10 新 unit
   - `test_invoke_writes_prefix_file_at_repo_root_runtime`
   - `test_invoke_substitutes_prefix_file_placeholder`
   - `test_invoke_raises_when_prefix_nonempty_template_lacks_placeholder` (4 子断言)
   - `test_invoke_raises_on_unknown_kwargs`
   - `test_invoke_raises_on_non_str_prefix`
   - `test_invoke_no_file_when_prefix_empty`
   - `test_invoke_round_discriminator_in_path`
   - `test_invoke_prefix_file_byte_for_byte`
   - `test_invoke_path_contains_dot_runtime`
   - `test_invoke_raises_on_unexpected_worktree_layout`

6. `tests/smoke/test_v083_p02_dispatch_wireup.py` — 2 新 integration
   - `test_round1_auto_dispatch_passes_prefix_through`
   - `test_round2_fresh_worktree_passes_prefix_through`

7. `CHANGELOG.md` — v0.8.3 P0.2 条目 + breaking change 警告

8. `.flow/pitfalls/dispatch-shim-silent-kw-drop.md` — 新 pitfall

### Constraints handed to implementer
- mandatory opus gate（state-machine + dispatch boundary）
- K_CLASS_SENTINEL_PROHIBITION 文本 invariant — 不动
- `auto_dispatch_task` 现有 test 必须不破（`prompt_prefix=""` default 路径）
- 全套 1002 PASS 目标（969 baseline + 21 P0.1 + 12 P0.2）
- 沿用 P0.1 pitfall：worktree 内 Edit **必用 worktree 路径前缀**（`edit-absolute-path-resolves-master.md`）

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

_Updated 2026-05-08 21:27 (last 20 unique edits)_:

- `.flow/tasks/05-08-v0.8.3-p0.2-brief-sentinel-fullset/progress.md`
- `.flow/tasks/05-08-v0.8.3-p0.2-brief-sentinel-fullset/codex-consult-r3-input.md`
- `.flow/tasks/05-08-v0.8.3-p0.2-brief-sentinel-fullset/prd.md`
- `.flow/tasks/05-08-v0.8.3-p0.2-brief-sentinel-fullset/codex-consult-r2-input.md`
- `.flow/tasks/05-08-v0.8.3-p0.2-brief-sentinel-fullset/codex-consult-r1-input.md`
- `.flow/pitfalls/edit-absolute-path-resolves-master.md`
- `/tmp/v083-p01-prefork-msg.txt`
- `.flow/tasks/05-08-v0.8.3-p0.1-implementer-redispatch/progress.md`
- `.flow/tasks/05-08-v0.8.3-p0.1-implementer-redispatch/prd.md`
- `.flow/tasks/05-08-v0.8.3-p0.1-implementer-redispatch/codex-consult-r1-prompt.md`
- `.flow/tasks/05-08-v0.8.3-p0.1-implementer-redispatch/research/merge-runner-ctx.md`
- `.flow/tasks/05-08-v0.8.3-p0.1-implementer-redispatch/research/test-fixtures.md`
- `.flow/tasks/05-08-v0.8.3-p0.1-implementer-redispatch/research/dispatch-entry.md`
- `/home/yangpeng/.claude/projects/-data-Claude-flow-framework/memory/MEMORY.md`
- `/home/yangpeng/.claude/projects/-data-Claude-flow-framework/memory/session_latest.md`
- `/tmp/sediment-msg.txt`
- `.flow/tasks/05-08-v0.8.3-p0.0-hook-fix/progress.md`
- `/tmp/flow-commit-msg.txt`
- `/tmp/dt-c.py`
- `/tmp/dotfiles-commit-msg.txt`
