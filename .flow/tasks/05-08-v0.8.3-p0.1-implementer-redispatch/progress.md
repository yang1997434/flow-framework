---
slug: v0.8.3-p0.1-implementer-redispatch
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

# progress.md — v0.8.3-p0.1-implementer-redispatch

## Plan

**Strategy**: single-session main implementation（state-machine 改动跨 8+ 函数 + 跨闭包，
切并行 wave 反而会撞 merge contamination；保 single 实施 + 集中 codex review）。

**Phases of execution**:

1. **Pre-impl commit**：commit task dir 含 prd.md/research/codex-output 到 master（**pre-fork 必做**），
   再 EnterWorktree 隔离实施（worktree branch: `feat+v0.8.3-p0.1-implementer-redispatch`）。
2. **Step A — `RoundRecord` + state 字段**：
   - 在 `RetrySessionState` 加 `current_round_ctx: WorktreeContext | None`、
     `current_round_facts: TaskFacts | None`、`failed_rounds: list[RoundRecord]`。
   - 定义 `RoundRecord` dataclass（worktree_id / path / branch / round_num）。
   - `WorktreeContext` 加 `round_num: int = 1` field（向后兼容）。
3. **Step B — `_dispatch_implementer_fresh_worktree` helper**：
   - 调 `create_task_worktree(round_num=N)` + `_invoke_subagent_dispatch(ctx, ...)` +
     `derive_task_facts(ctx)` + `_write_review_status_to_worktree(ctx, ...)`（同 finally swallow）。
   - 异常分类：worktree-create OSError / dispatch crash → raise `InfraFailureError`（新建）。
4. **Step C — `create_task_worktree` round 鉴别符**：
   - 加可选 `round_num: int = 1` 参数；N>=2 时 worktree_id 加 `+r{N}`。
5. **Step D — `_prod_impl` 真实实现**：
   - Round 1（dispatch_retry_rounds==0）：仍返 `{}`（向后兼容）。
   - Round 2+：调 helper；两阶段提交：先算 new ctx + facts，再 append 老 RoundRecord 到
     `failed_rounds` + swap `current_round_*`。raise InfraFailure → 不动 state, propagate up.
6. **Step E — `_prod_review` 闭包刷新**：
   - 改读 `state.current_round_ctx` / `state.current_round_facts`，不用闭包外 facts。
7. **Step F — `dispatch_with_retry` 返回 winner**：
   - 在 outcome="pass" 时 set `state.winner_ctx = state.current_round_ctx or initial ctx`；
     return type 扩展 `(outcome, snap, winner_ctx)` 或通过 state.winner_ctx 暴露。
   - assert 不变量：rc==0 ⇒ winner_ctx is not None。
8. **Step G — `_phase2_dispatch` 签名**：`-> tuple[int, WorktreeContext | None]`。
9. **Step H — `_cmd_auto_execute` merge wiring**：解包 `(rc, winner_ctx)`，
   `MergeRunner(ctx=winner_ctx, ...)`。
10. **Step I — InfraFailure 终止 path**：
    - 新增 `block_type="phase2_infra_failure"` → notifier.fire_block → rc=3。
    - 不进 retry loop，不破 J-class progress。
11. **Step J — Round-cap 默认 + journal note**：
    - `dispatch_retry_rounds_cap` 默认 3 → 2；journal 写 `cap_reason="fresh-per-round-cost"`。
12. **Step K — `task_brief` 渲染**：
    - 在 retry loop 调 `build_implementer_prompt` 处，从 `task_dir/prd.md` 渲染 brief
      （fallback: `\n".join(criteria)`）。
13. **Step L — 测试**：
    - 扩展 fakes 签名（兼容老 tests）；新加 fakes 验证 ctx 跨轮唯一 + winner ctx flow。
    - 新建 `tests/smoke/test_fresh_worktree_per_round.py`：mini integration with tmp git.
    - 新加 unit test for: 两阶段 commit 中段 raise / InfraFailure terminal / Round 1 aliasing /
      counter monotonicity 混合序列 / worktree id 唯一性 / Round 1 PASS 不 regress.
14. **Step M — Phase 4 sediment 接口**：扩展 sediment-skill 输入读 `state.failed_rounds` 列表
    （即使本任务不 fully wire 模板，至少留口）。
15. **Step N — 文档 + CHANGELOG**：
    - `flow-phase2-execute/SKILL.md` 描述 fresh-per-round 模型。
    - `CHANGELOG.md` v0.8.3 P0.1 entry。
16. **Step O — Codex review GREEN gate**：mandatory opus gate；codex review 必须 GREEN
    含 swallowed-exception self-check（per `claude-review-blindspots`）。

**Final integration commit + tag** 在 master 进行（merge worktree → tag → archive task dir）。

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

_Updated 2026-05-08 09:53 (last 20 unique edits)_:

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
- `/tmp/codex-review-v083-p00-r2.txt`
- `tests/hooks/test_pre_commit_review.py`
- `/home/yangpeng/claude-linux-config/claude/hooks/pre-commit-review.py`
- `/tmp/codex-review-v083-p00.txt`
- `/tmp/hook-quick-test.py`
- `tests/smoke/test_dispatch_template.py`
- `scripts/dispatch_template.py`
- `.flow/pitfalls/hook-blocks-after-reviewer-pass.md`
