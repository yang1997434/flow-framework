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

| 时间 | Agent | Scope | Outcome |
|------|-------|-------|---------|
| 2026-05-08 09:50 | main | Phase 1 brainstorm + PRD | 4 user 决策 + 16 ACs |
| 2026-05-08 09:53 | sonnet × 3 | Phase 1 research (parallel) | dispatch-entry / merge-runner-ctx / test-fixtures 完整摘要；3 critical findings |
| 2026-05-08 09:55 | codex consult | Phase 1 ADR review | YELLOW: P0×3 + P1×3 + edge×2 + tests×8；全部并入 ACs |
| 2026-05-08 09:59 | main | pre-fork commit `0bb233d` | task dir 含 PRD + research + codex output 落 master |
| 2026-05-08 09:59 | main | EnterWorktree | `feat+v0.8.3-p0.1-implementer-redispatch`（rebased to master HEAD） |
| 2026-05-08 10:00 | main | Step A `RoundRecord` + state 字段 + `WorktreeContext.round_num` | smoke pass |
| 2026-05-08 10:01 | main | Step C `create_task_worktree(round_num)` 鉴别符 | smoke pass: R1 legacy / R2-3 unique |
| 2026-05-08 10:02 | main | Step B `_dispatch_implementer_fresh_worktree` + `InfraFailureError` | 4 path 验证 |
| 2026-05-08 10:05 | main | Step D-K 大批量 refactor: dispatch_with_retry winner / _phase2_dispatch 签名 / _prod_impl 两阶段提交 / _prod_review 闭包刷新 / cap 3→2 / brief renderer / _cmd_auto_execute winner_ctx 流向 | abs-path bug discovered: edits 落到 master！ |
| 2026-05-08 10:07 | main | recovery: `git diff > patch`, revert master, `git apply` 到 worktree | 修正 |
| 2026-05-08 10:08 | main | Step L 测试: 16 unit + 3 mini-integration | pass |
| 2026-05-08 10:09 | main | Step M `flow-phase4-sediment` SKILL.md 加 retry-round 指引 | doc only |
| 2026-05-08 10:10 | main | Step N CHANGELOG + flow-phase2-execute SKILL.md fresh-per-round 段 | doc only |
| 2026-05-08 10:12 | codex review R1 | mandatory opus gate review | YELLOW: 1 prod-adapter integration gap + 1 swallow log |
| 2026-05-08 10:14 | main | 加 prod-adapter integration tests (Round 2 PASS + InfraFailure) + `_render_task_brief` 加 stderr warning | smoke pass |
| 2026-05-08 10:15 | codex review R2 | 复审 | **GREEN** ✅ ready to merge |

## Verify Report

| Item | Status | Detail |
|------|--------|--------|
| Test suite | ✅ 990 PASS | 860 smoke + 105 unit + 25 hooks（baseline 969 + 21 new — 16 unit + 3 mini-integration + 2 prod-adapter integration）|
| Lint / typecheck | ✅ N/A | 项目无 lint pipeline；type hints 在新代码中遵循现有风格 |
| Codex review (mandatory opus gate) | ✅ GREEN | round 2 thread `019e0833-fa60-7d50-8caf-f45d7ed5a50b` |
| Pre-fork PRD commit | ✅ `0bb233d` 落 master 后 EnterWorktree |
| K-class 红线 | ✅ marker 单 use unlink 验证；reviewer-PASS 后才写 marker（`_consume_marker`/`_validate_marker` 全程未触） |
| 18-class trigger redact | ✅ 已在 reviewer feedback 注入前调 `redact_blindspot_index`（v0.8.2 T3 既有，未动） |
| credentials_ref grep | ✅ 0 secrets 入库 |
| Backward compat | ✅ Round 1 worktree 命名 `<slug>+t<n>+<shortsha>` 不变；contract 字段未破坏；test fakes 升级签名后 50/50 老 dispatch 测试 PASS |

## Sediment Notes

**ADR-lite 已在 prd.md** — 不重复。新沉淀：

1. **Pitfall: worktree 内 Edit 必须用 worktree 绝对路径**（`/data/Claude/flow-framework/.claude/worktrees/<branch>/...`）。我用绝对路径 `/data/Claude/flow-framework/scripts/...` 时所有 Edit 落到 master 而非 worktree（cwd 在 worktree 但路径解析到 master）。Workaround：用 `git diff > patch`、`git checkout -- <files>` 还原 master、worktree 里 `git apply patch` 恢复。**此 pitfall 应促进 → `.flow/pitfalls/edit-absolute-path-resolves-master.md`**。
2. **Lesson: codex 会幻觉不存在的函数**（round 1 提到 `_write_review_status_to_worktree` — grep 全仓 0 命中）。**总是 grep 验证 codex claim 的具体符号 / 函数名是否存在**，否则会浪费迭代实施"必复制"伪 P0 项。
3. **Lesson: 写代码前先 grep 现有符号**（codex blindspot I-class — repeating earlier task's mistake；codex blindspot S-class — wire-up gap）。本次 plan 阶段没确认 `_write_review_status_to_worktree` 是否存在就把它列入 ACs，浪费一轮 codex 修正才发现是幻觉。
4. **Pattern: `RoundRecord.from_ctx()` 类方法工厂** — 把"从 ctx 构造识别记录"的转换逻辑放在 RoundRecord 自己身上，调用方写 `state.failed_rounds.append(RoundRecord.from_ctx(prev_ctx))` 即可，比 `RoundRecord(worktree_id=ctx.worktree_id, ...)` 4-行展开干净。可推广到其他需要 forensic record 的 frozen dataclass。
5. **Lesson: codex consult 给威胁模型 + in-scope/out-of-scope** 极有效（feedback memory 已沉淀；本次 round 1 verdict 给到 YELLOW 的 8 条具体 P0/P1 + 8 测试推荐都直接采纳，round 2 GREEN 一次过）。validates `feedback_codex_threat_model_required.md`。
6. **Bug: Round 1 hardcoded `task_brief=""`** — 是 v0.8.2 T4 留的 placeholder，Round 1 因为 prod_impl no-op 表现为良性，但 Round 2+ 实施时实际进入 build_implementer_prompt → subagent 没 task context。修复：state.task_brief 由 `_phase2_dispatch` 在 session 开始时 render 一次（prefer prd.md → fallback criteria → fallback 空）。

**已调整 PRD ACs：AC11 dropped**（codex round 1 幻觉，确认实际函数不存在）。

## Retro

**Worked**：
- Phase 1 brainstorm one-question-at-a-time 把 4 个核心决策（worktree 策略 / prompt 组成 / FAIL 处置 / winner 规则）锁干净
- 3 个 sonnet research sub-agent 并发省了主 session 大量 grep 时间，捞出 G2/G3/G4 三个 critical findings
- codex consult R1（plan 阶段）→ R2（实施后审查）的双轮 GREEN gate 守住了 state-machine 改动
- pre-fork commit + EnterWorktree 的 v0.8.2 工作流复用没出问题（除了 abs-path bug）

**Didn't work**：
- 我习惯性给 Edit 用 `/data/Claude/flow-framework/...` 绝对路径而不是 worktree 路径 → 所有大批量 refactor 落到 master，发现后用 patch 恢复浪费 ~10 分钟
- codex round 1 幻觉 `_write_review_status_to_worktree` → 我没立刻 grep 验证就采纳到 ACs（AC11），浪费一轮才剔除

**框架反馈**：
- Flow 应该在 EnterWorktree 后弹一条 "前面所有 Edit 必须用 worktree 路径前缀" 的提醒，或者 hook 在 cwd 是 worktree 时检测 Edit 写到 master 路径并 warn。
- codex consult 输出可以加一段 "claims to verify before adopting" — 把它具体提到的函数名 / 文件路径列出来，让主 session 能 grep 验证再纳入 ACs。

## Files Touched

_Updated 2026-05-08 10:40 (last 20 unique edits)_:

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
- `/tmp/codex-review-v083-p00-r2.txt`
- `tests/hooks/test_pre_commit_review.py`
- `/home/yangpeng/claude-linux-config/claude/hooks/pre-commit-review.py`
- `/tmp/codex-review-v083-p00.txt`
- `/tmp/hook-quick-test.py`

## Commits

- [2026-05-08 10:00] `0bb233d` chore(v0.8.3 P0.1): pre-fork PRD commit for implementer-redispatch
