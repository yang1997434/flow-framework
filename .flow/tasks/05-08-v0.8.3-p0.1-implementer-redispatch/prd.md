# v0.8.3 P0.1 implementer redispatch

> Created: 2026-05-08
> Slug: v0.8.3-p0.1-implementer-redispatch
> Type: backend  <!-- 后端/UI/数据/文档/部署/调研 -->
> Complexity: complex  <!-- trivial/simple/moderate/complex -->

## Goal

实现 v0.8.2 T18 留的 stub：在 reviewer FAIL 后让 implementer subagent 真正在 Round 2+ 被重新派发，
继承 Round 1 的 worktree 状态 + reviewer feedback prompt-prefix，闭合 retry loop 端到端语义。
当前 `_prod_impl` 直接返回 `{}` 导致 Round 2+ 在 prod 路径下是 no-op，feedback 永不进入实施。

## What I already know

**Round 概念已部分到位（v0.8.2 T1/T3/T4）：**
- `state.dispatch_retry_rounds` counter 已存在（`scripts/flow_orchestrator.py:5046`）
- retry loop 已根据 `review_outcome=="fail"` 递增 round 并 `continue`（行 5128-5133）
- `build_implementer_prompt(reviewer_feedback=redacted, is_first_pass=True)` 已构造 prefix（行 5058-5063），
  18-class trigger redact 已通过 `redact_blindspot_index` 实施
- `dispatch_with_retry` 通过 `deps.run_implementer_round(prompt_prefix=prefix)` 把 prefix 传下去

**实际 gap（T18 deferred）：**
- `_phase2_dispatch::_prod_impl`（行 5312-5316）是 stub：`del prompt_prefix; return {}`
- 注释明示："Round 1: auto_dispatch_task already produced facts; no additional impl work needed.
  T18 wires real re-dispatch."
- Round 1 facts 由 `auto_dispatch_task` 在 `_phase2_dispatch` 之前产出（`_cmd_auto_execute` 主循环）
- Round 2+ 需要在**同一个 `WorktreeContext ctx`** 里重新调 implementer subagent

**周边代码点：**
- `claude/capabilities/defaults.json::autonomy_orchestrator` 是 implementer subagent dispatch 的 capability 入口
- `_invoke_subagent_dispatch(ctx, **kw)` 是 prod 派发 shim（行 5146）
- `auto_dispatch_task`（Round 1 entry）位于 `scripts/flow_orchestrator.py` 同模块；产出 `DispatchOutcome{ctx, facts}`
- `state.last_reviewer_feedback` 已在 reviewer FAIL 时缓存（行 5343-5347），prefix 自动消费

**v0.8.2 lessons 必带：**
- pre-fork PRD commit（worktree fork 前先 commit task dir）
- mandatory opus gate（state-machine / contract 改动）
- K-class 红线（reviewer PASS 后才 touch sentinel；brief 列 category 不是单例）

## Requirements

- **R1 — Fresh worktree per round**：Round 2+ 必须从 base branch 起新 `WorktreeContext`，
  不继承 Round 1 的工作树文件。prev round 的 worktree 保留至 task 结束（不立即销毁）。
- **R2 — Prompt = Brief + Feedback**：Round 2+ implementer prompt-prefix 必须包含完整 task brief
  （PRD goal + acceptance criteria + technical notes 从 prd.md/contract.json 渲染）+ redacted
  reviewer feedback。`build_implementer_prompt(task_brief="")` 当前空字串 brief 必须替换为
  从 task_dir 渲染的真正 brief。无 prev-round diff、无 ADR 快照（保持简洁）。
- **R3 — FAIL worktree 保留批量清理**：每轮的 ctx 累积到 `state.failed_rounds`（或类似容器），
  task 结束时（PASS / hard-stop / archive）批量 ExitWorktree。Phase 4 sediment 可读 prev round diff。
- **R4 — Winner = first PASS**：retry loop 在 reviewer outcome=="pass" 立即 break，该轮 ctx 即 winner，
  传给 Gate 7 MergeRunner。后续 round 不被启动。
- **R5 — Round-cap 调低 3 → 2**：默认 `dispatch_retry_rounds_cap=2`（fresh-per-round 成本高）。
  contract 中 `phase2.retry_rounds` 仍可 override；不破坏向后兼容（contract 字段已存在）。
- **R6 — `_phase2_dispatch` 返回 winner ctx**：当前签名 `-> int`；改为 `-> tuple[int, WorktreeContext | None]`
  或在 state 上记录。`_cmd_auto_execute` 用 winner ctx 构造 MergeRunner（行 5611）。
- **R7 — FAIL round diff 进 sediment**：Phase 4 sediment 输入扩展为读取 `state.failed_rounds` 中
  各 ctx 的 diff，作为 "走过的错路" lessons-learned 输入。

## Out of Scope

- **并发推测 dispatch**（split to v0.8.3 P0.7）：本任务保持 sequential。
- **Best-of-N scoring**（拒）：不引入 winner 评分函数。
- **手动 winner 选择**（拒）：与 autonomous orchestrator 语义冲突。
- **跨 task 重用 worktree**：每个 task 独立，不跨 task 优化。
- **Round 1 也走 fresh path**（保 v0.8.2 兼容）：Round 1 仍由 `auto_dispatch_task` 在
  `_phase2_dispatch` 之前产出 ctx；只有 Round 2+ 走 fresh re-dispatch。

## Implications

- "worktree state inheritance" 原任务描述需重新解释为：**不继承文件状态**，仅继承 reviewer feedback 文本
- "prev-impl 部分完成态语义" 简化为：每轮独立 atomic 实施，无部分完成
- `RetryDeps.run_implementer_round` 的 prod 实现要新建 worktree + 调 implementer subagent + 把新 ctx
  写回 state；`_prod_impl` 不再是 stub
- 测试 fixtures（`deps_factory` fakes）天然不受 worktree 改动影响（fakes 不创建真实 worktree），
  但需要新增针对 prod path 的 integration test

## Acceptance Criteria

- [ ] **AC1 — Round 2+ 真实派发**：reviewer FAIL 后 `_prod_impl` 实际调 implementer subagent；
  集成测试验证 Round 2 产出 non-empty deltas（可观察 diff or progress.md round 2 事件）
- [ ] **AC2 — Fresh worktree per round**：Round 2 ctx.path 与 Round 1 ctx.path 不同；
  Round 2 worktree 是从 base branch fork 出来（无 Round 1 修改可见）
- [ ] **AC3 — Prompt 包含 brief + feedback**：Round 2 prompt-prefix 含 PRD goal/criteria 渲染 +
  redacted reviewer feedback；空字串 task_brief 已修正
- [ ] **AC4 — Winner ctx 传给 Gate 7**：reviewer PASS round N 的 ctx (N>=2) 传给 MergeRunner，
  merge 的 diff 是 Round N 的 diff，不是 Round 1 的
- [ ] **AC5 — `_phase2_dispatch` 签名扩展**：返回 `(rc, winner_ctx)`；老 caller 不破坏（向后兼容）
- [ ] **AC6 — Round-cap 默认 2**：contract 无显式 `phase2.retry_rounds` 时 cap=2；
  contract override 仍生效
- [ ] **AC7 — FAIL round 累积**：task 结束时 `state.failed_rounds` 含所有 FAIL 轮的 ctx 引用；
  archive 时批量 ExitWorktree
- [ ] **AC8 — Phase 4 sediment 含 FAIL diff**：sediment 输入新增 `failed_rounds` 列表；模板/skill
  能消费（即使本任务不 fully wire 模板，至少留口）
- [ ] **AC9 — Test suite 全绿**：existing 969 tests + 新增 integration tests for fresh-per-round
  覆盖 AC1-AC4 路径
- [ ] **AC10 — Codex review GREEN**：state-machine 改动 mandatory opus gate；codex review
  必须 GREEN（含 swallowed-exception self-check）

## Definition of Done

- Tests added/updated where appropriate
- Lint / typecheck / CI green
- Docs/notes updated if behavior changes
- Credential grep self-check passes
- Phase 4 sediment notes filled in (even if "no new ADR/pattern")

## Research References

- **research/dispatch-entry.md**：`auto_dispatch_task` 9-step 非幂等（worktree_id 碰撞会 crash）；
  Round 2+ 不能 re-call auto_dispatch_task。推荐 Option B：抽 `_dispatch_implementer_fresh_worktree`
  helper，跳过 auto_engaged event。`is_first_pass=True` 当前 no-op；`task_brief=""` 是 Round 2+
  bug（要从 prd.md 读 + acceptance_criteria fallback）。
- **research/merge-runner-ctx.md**：MergeRunner 不依赖 Round 1 ctx 身份，只用 `worktree_path` /
  `branch` / `integration_target` 字段。**CRITICAL G2**：worktree_id `<slug>+t{n}+{shortsha}`
  在 integration_target 不前进时多轮碰撞 → 必须加 round 鉴别符。**CRITICAL G3**：再调
  auto_dispatch_task 会产二次 auto_engaged event → CrashRecoveryDispatcher 误分类。
  **G4**：`_prod_review` 闭包 captured Round 1 facts；ctx 切换时必须刷新（否则 gate 1 baseline 用陈旧 facts）。
- **research/test-fixtures.md**：现有 3 个 dispatch 测试都是 full-fake；无 integration test。
  推荐 (a)+(b)：unit fake spy 验证 ctx 跨轮唯一 + winner ctx 流向 MergeRunner；mini integration
  test 用 tmp git repo 验证 fresh worktree 真无 Round 1 修改。无 conftest.py，新建 `tests/smoke/helpers.py`。

## Decision (ADR-lite)

**Context**: v0.8.2 T18 留了 `_prod_impl` stub，导致 Phase 2 retry loop 在 prod 路径下 Round 2+ 是
no-op（reviewer feedback 永不被实施）。需要决定 Round 2+ 重派发的 worktree 模型 + winner-ctx 流向
+ 现有 `auto_dispatch_task` 不可重入的副作用（auto_engaged event / worktree_id 碰撞）。

**Decision**: **Fresh worktree per round + 旁路 auto_dispatch_task + 抽 helper + winner ctx 显式流向**。

具体：
1. 新建 `_dispatch_implementer_fresh_worktree(*, state, task_dir, contract, manifest, criteria, prompt_prefix, round_num) -> (WorktreeContext, dict)`，
   绕过 `auto_dispatch_task`（避 auto_engaged 二次 event + worktree_id 碰撞），直接调 `create_task_worktree`（带 round 鉴别符）+
   `_invoke_subagent_dispatch` + `derive_task_facts`。
2. `_prod_impl(state, prompt_prefix)`：在 round 2+ 调上述 helper，把新 ctx append 到
   `state.failed_rounds`（旧的） + 暂存 `state.current_round_ctx`（新的）；返回 deltas。
3. `_prod_review` 改为读 state 的 current_round_ctx + 重新 derive facts（避免 closure 陈旧 facts G4）。
4. `dispatch_with_retry` 在 outcome="pass" 时把 `state.current_round_ctx` 提为 winner，回传
   到 `_phase2_dispatch` 调用方。`_phase2_dispatch` 签名扩展 `-> tuple[int, WorktreeContext | None]`。
5. `_cmd_auto_execute` 用 winner ctx 构造 MergeRunner。
6. Round-cap 默认 3 → 2（fresh 成本高）。
7. Worktree id 加 round 鉴别符：`<slug>+t{n}+r{round}+{shortsha}`（保唯一）。

**Rejected**:
- A: 重入 `auto_dispatch_task`（**会 crash**：worktree_id 碰撞 + auto_engaged 二次 event）。
- 复用同一 worktree（前置决策已拒，contamination 风险）。
- Best-of-N scoring（拒，简单首 PASS = winner）。
- 并发推测 dispatch（拒，split to v0.8.3 P0.7）。

**Consequences**:
- Short-term cost: ~3-5 天工作；改 dispatch 状态机核心 + 测试覆盖；mandatory opus gate + codex review；
  现有 RetryDeps 协议略扩展；test fakes 需更新但 backward-compatible。
- Long-term benefit: 闭合 v0.8.2 deferred 的 retry loop；prod 路径下 reviewer feedback 真正进入实施；
  为 P0.7 并发推测奠基（fresh-per-round 是它的前置）。
- Reversibility: 中等。改完后 RetryDeps 协议扩展（加 `current_round_ctx` field on state）后续不易回退；
  但若发现致命问题，可保 R1-R4 框架 + 让 _prod_impl fall back to "noop and log warning" → 等价于回到 stub。

**Revisit triggers**:
- fresh worktree 创建 latency 在生产环境 >30s/round → 考虑复用部分缓存或转 P0.7 并发
- FAIL 轮 worktree 累积导致磁盘 / git index 性能问题 → 改为立即 cleanup + branch retain
- 用户在 1-2 个 task 上反馈 "Round N 信息不够"（feedback alone 不足以重新实施）→ 加 prev round diff 摘要

## Codex consult R1 (verdict YELLOW) — 必须采纳的调整

> Thread `019e0811-eea0-71e1-82cf-1da385ca86e2`，full output 见
> `codex-consult-r1-output.jsonl`，prompt 见 `codex-consult-r1-prompt.md`

**P0（接受）**：
1. **`_write_review_status_to_worktree` 必须 replicate**：`auto_dispatch_task` 的 finally 块写
   per-round review status 到 worktree。helper 必须照搬（同 swallow 语义）。
2. **State 两阶段提交**：`new_ctx/new_facts` 先计算成功，再 append 老的到 `failed_rounds` +
   swap `current_round_*`。中间任何 exception 必须保 state coherent（fail-rollback）。
3. **Winner null guard**：`dispatch_with_retry` 返回 `(outcome, winner_ctx)` 并 assert `winner_ctx is not None`
   on PASS。`_cmd_auto_execute` merge 入口强约束非 None。

**P1（接受）**：
4. **`failed_rounds` 用轻量记录不存 raw ctx**：定义
   `RoundRecord(worktree_id, path, branch, round_num)` typed tuple/dataclass，
   避免 state writer 序列化 raw `WorktreeContext` 的 brittleness。
5. **Round id 作 first-class field**：`WorktreeContext` 加 `round_num: int` 字段（or 加进 `RoundRecord`），
   后续 cleanup / journal query 不解析字符串。
6. **Round-cap 改默认 + journal note**：cap 从 3→2 时在每 run 的 journal 写显式 reason 行
   （便于后续 audit）。

**P0 边缘 case 处理**：
7. **Infra exception 分类**：subagent dispatch crash / worktree-create fail → 必须显式终止 retry loop
   （不 silent loop）+ 计数器或 outcome 标记。当前 retry loop 只在 `review_outcome=='fail'` 时
   `dispatch_retry_rounds += 1`，infra exception 跳过 increment 会破坏 J-class progress 不变量。
   方案：把 helper 的 exception catch 分类为 `infra_failure` outcome → mapped to a terminal block
   path（不进 retry loop）。
8. **Round 1 PASS aliasing**：Round 1 reviewer PASS 时 `winner_ctx is ctx_round1`（同对象）。
   后续 phase2_dispatch / merge 读 winner_ctx 不能 mutate winner_ctx 字段。加 docstring 警告 +
   test 验证 immutability。

**新增 testing 项目**：
- T-A: helper replicate `_write_review_status_to_worktree`
- T-B: dispatch fail 中段 → state coherent (current/failed 不半 swap)
- T-C: worktree create 失败 → 终止性确定行为，不 wrong merge
- T-D: winner E2E（Round 2 PASS → Round 2 ctx merged，不是 Round 1）
- T-E: Round 1 PASS aliasing 不 regress
- T-F: counter monotonicity 混合序列 (`rejected_with_rationale → fail → pass`)
- T-G: worktree id `+rN` 唯一 + Round 1 legacy 命名 cleanup 兼容
- T-H: state.failed_rounds crash recovery 持久化（or 显式 non-persistence contract）

## ACs 修订（采纳 codex 后）

补充：
- [ ] **AC11 — `_write_review_status_to_worktree` 在 helper 中调用**（与 auto_dispatch_task 等价）
- [ ] **AC12 — 两阶段提交**：单元测试模拟 helper 中段 raise，验证 state.current_round_ctx 不被半 swap
- [ ] **AC13 — Winner non-None on PASS**：assert in `_cmd_auto_execute` + test 覆盖
- [ ] **AC14 — `RoundRecord` 替代 raw ctx 在 failed_rounds**：state writer 序列化通过
- [ ] **AC15 — Infra failure 走 terminal**：subagent crash → block_type=`phase2_infra_failure`，rc=3，不 silent re-loop
- [ ] **AC16 — Round 1 aliasing immutable**：winner_ctx Round 1 path 不 mutate

## Technical Notes

- **Files to modify**:
  - `scripts/flow_orchestrator.py` 主战场：新增 `_dispatch_implementer_fresh_worktree` helper、改写 `_prod_impl`、改 `_prod_review` 关 facts 闭包、改 `dispatch_with_retry` return 增 winner ctx、改 `_phase2_dispatch` 签名、改 `_cmd_auto_execute` 用 winner ctx
  - `scripts/dispatch_template.py::build_implementer_prompt` 调用点：传真的 task_brief（从 prd.md/criteria）
  - `scripts/flow_orchestrator.py::create_task_worktree` 函数：worktree_id 加 round 鉴别符
  - `tests/test_phase2_retry_loop.py` 等 3 个 dispatch 测试：扩展 fakes 签名
  - `tests/smoke/test_fresh_worktree_per_round.py`（新增）：mini integration test 用 tmp git
  - `claude/skills/flow/flow-phase2-execute/SKILL.md` 文档更新（提 fresh-per-round 模型）
  - `CHANGELOG.md`：v0.8.3 P0.1 entry
- **Constraints**:
  - mandatory opus gate（state-machine 改动）
  - codex review GREEN 必经
  - K-class 红线：不在 reviewer PASS 前 touch sentinel
  - pre-fork PRD commit（worktree fork 前先 commit task dir）
  - 18-class trigger redact 在 feedback 注入前已实施（`redact_blindspot_index`），不需重做
- **Related ADRs / pitfalls**:
  - v0.8.2 T18 deferred decision（本任务闭合）
  - `claude-review-blindspots.md`（codex 必抓 swallowed exception）
  - `worktree-fork-before-prd-commit.md`（commit timing 红线）
  - `subagent-misread-brief-do-not-add-modules.md`（implementer subagent brief 必带 K-class brief）
- **credentials_ref**: 无（纯框架内部改动，无外部 secret）
