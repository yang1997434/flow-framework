# v0.8.5 dispatch telemetry + feedback enrichment

> Created: 2026-05-08
> Slug: v0.8.5-dispatch-telemetry-feedback-enrich
> Type: backend
> Complexity: moderate  <!-- 从 complex 下调：scope 缩到 telemetry + feedback enrich，无并发状态机 -->

> **Pivot 历史**：原立项 `v0.8.5-p0.7-parallel-speculation` (complex)。Phase 1 codex consult R1
> 反对以 A/D 动机做 P0.7（cap=2 时延收益小、K-class sentinel race、state 单 round 模型需重写）。
> 采纳 codex 建议：v0.8.5 改 ship telemetry，P0.7 正式 deferred 至数据触发后再决策。
> 详见 `research/codex-consult-r1-output.md`。

## Goal

为 flow dispatch 状态机加 **observability + feedback enrichment**，让后续 P0.7 等架构决策由
数据驱动而非投机：

1. **Telemetry 埋点**：覆盖 dispatch 关键阶段（worktree create / implementer subagent /
   reviewer / GateRunner / codex review），输出 timing + FAIL reason 分布到结构化日志。
2. **Feedback enrichment**：Round 2 prompt 在已有 PRD brief + reviewer feedback 之外，
   附加 Round 1 的 diff 摘要（lines changed / files touched / 顶层 hunk 标题），直接对应
   v0.8.3 P0.1 ADR Revisit trigger #3「Round N 信息不够」。
3. **基线数据收集**：跑 1-2 个真实 task 后，把 telemetry 结果沉淀为 v0.8.6 P0.7 的决策输入。

## What I already know

- **前置**：v0.8.3 P0.1 已 ship "fresh worktree per round + brief+feedback prompt + winner ctx"。
- **状态机锚点**（不改动）：`_phase2_dispatch` 现签名 `(rc, winner_ctx)`；
  `failed_rounds` 是 `RoundRecord` list；`current_round_*` 双阶段提交；
  `_write_review_status_to_worktree` 每 round 写状态。
- **Codex consult R1 关键结论**（见 `research/codex-consult-r1-output.md`）：
  - cap=2，A 动机站不住；fresh worktree 不是真瓶颈
  - 真正贵：implementer/tests/codex review wall time
  - K-class sentinel + state 单 round 模型 = 真 parallel 要 1-2 周架构改
  - 推荐先 ship telemetry + 信息增强，数据驱动决策
- **Pitfall 库相关**：`dispatch-shim-silent-kw-drop.md`（kwarg fail-closed）、
  `phase-state-triple-bug.md`（state 三元组）。
- **Suite baseline**：985 PASS（880 smoke + 105 unit）。

## Requirements

### R1 — Telemetry 落盘位置

- 每个 task 写自己的 `telemetry.jsonl`，路径：`<task_dir>/telemetry.jsonl`（含 archive 后亦同位置）
- archive 时 telemetry 随 task dir 一起搬，无额外迁移
- `.flow/.gitignore` 增 `tasks/**/telemetry.jsonl` 规则，避免污染 git 历史 + 减少 status 噪声
- 跨 task 聚合靠 ad-hoc `find` glob，v0.8.5 不内建 aggregator

### R2 — Telemetry event schema (v1, frozen)

每行一条 JSON：

```jsonc
{
  "ts": "<ISO8601 UTC>",
  "schema_version": 1,
  "task_slug": "<from progress.md>",
  "round_num": <int, 1-indexed>,
  "phase": "worktree_create | implementer | reviewer | gate_run | codex_review",
  "duration_ms": <int>,
  "outcome": "pass | fail | skip | null",
  "fail_reason_raw": <string or null>,    // 仅 reviewer/gate/codex FAIL 时填，存 verdict 原文
  "fail_category": null,                   // reserved；v0.8.5 始终 null，留 v0.8.6 分类
  "worktree_id": <string or null>          // 非 dispatch phase 可 null
}
```

- 实现不改 reviewer prompt template / 不动 reviewer 契约
- 不显式 sample；每 dispatch round 都打全 5 阶段事件
- 写失败 swallow（telemetry 不阻塞 dispatch）但 swallow 路径要 log warning + 计数器

### R3 — Five-phase 埋点覆盖

- `worktree_create`: 从 `git worktree add` 调用前到调用后
- `implementer`: subagent dispatch 整体 wall time（不细分进 subagent 内部）
- `reviewer`: reviewer agent dispatch wall time
- `gate_run`: GateRunner 调用全流程
- `codex_review`: codex 调用 wall time（若该 round 触发；否则该 phase event 不写）

### R4 — Feedback enrichment（structural diff map, no code lines）

> **定位**（codex R2 锁定）：本字段 enrich 的是 reviewer feedback 的 **定位 + 可核验性**，
> **不是** 给 implementer 第二份源码上下文。primary channel 仍是 reviewer feedback。

Round 2+ 的 implementer prompt 在已有 PRD brief + reviewer feedback 之外，附加新 section
`Round N-1 structural diff map (no code lines)`，仅含：

- 文件修改清单（`git diff --stat` 风格：path / +N / -M / 总数）
- 每文件的顶层 `@@` hunk header（即函数 / 方法 / 类 名标识）
- **不含**任何代码行（不含 hunk 的 added/removed/context line）
- prompt 顶部加显式标注：`This is a structural map only; no code content. Use reviewer
  feedback as the primary signal for what to change.`

截断与稳定性：
- 摘要硬上限 200 行
- **per-file breadth 截断**：每文件最多 10 条 hunk header，超出标 `[... +N more hunks in this file]`；
  避免单大文件耗尽 200 行
- 文件数超出仍截断，末尾标 `[... truncated, N more files]`
- Round 1 prompt 完全不附加（无 prev round）

Redaction（极轻量，只防元数据偶发泄漏）：
- 路径中匹配长 token（≥32 hex/base64 char）/ UUID / email / URL secret-style query string → 替换为 `<REDACTED-TOKEN>`
- hunk header 同规则
- **不做代码行 redaction 体系**（v0.8.5 无代码行进 prompt，无需）

已知限制（PRD 文档中显式承认）：
- Python / JS / TS hunk header 由 git 默认 textconv 支持，质量好
- JSON / YAML / Markdown / 配置文件 hunk header 通常为空或无意义；仅靠 stat 行定位
- 大规模 rename / 重排时 stat 信号高噪声

成功标准：
- AC3 验证 prompt 含上述 section 与显式标注
- 不是"Round 2 一定 PASS 率上升"——telemetry 收据后才知道 enrichment 实际效用

### R5 — Contract opt-out

`flow.config.yaml` / contract 增字段 `dispatch.telemetry: on | off`，默认 `on`。
`off` 时 telemetry 写入完全跳过（含文件创建），但 feedback enrichment 仍生效（独立开关
`dispatch.feedback_enrichment: on | off` 默认 `on`）。

## Acceptance Criteria

<!-- 待 brainstorm 后细化；下面为占位草图 -->

- [ ] **AC1 — Telemetry 字段完备**：每次 `_phase2_dispatch` 跑完写一行 JSON event 含 task_slug、
      round_num、phase、duration_ms、fail_reason（若有）、worktree_id；字段集 frozen 在 schema 文件。
- [ ] **AC2 — 关键阶段全覆盖**：worktree_create / implementer_dispatch / reviewer / gate_run /
      codex_review 五个阶段都打 timing。
- [ ] **AC3 — Feedback enrichment 在 Round 2 prompt 中可见**：Round 2 prompt 包含 Round 1 的
      diff 摘要 section（fixture test 验证）。
- [ ] **AC4 — 不破现有 985 suite**：所有现存测试 GREEN；新增 telemetry 测试覆盖事件 schema +
      duration 计算 + Round 2 enrich。
- [ ] **AC5 — Telemetry 默认 on，但可 contract opt-out**：`dispatch.telemetry: off` 在 contract
      可关；默认 on（只是写文件，无外部副作用）。

## Definition of Done

- Tests added/updated where appropriate
- Lint / typecheck / CI green
- Docs/notes updated if behavior changes
- Credential grep self-check passes
- Phase 4 sediment notes filled in (telemetry schema 沉淀；P0.7 deferred ADR 沉淀；至少 1 个
  数据驱动的 follow-up 候选写入 backlog)

## Out of Scope

- **Parallel speculation dispatch（P0.7 本体）** —— deferred 至 v0.8.6+ pending telemetry data。
- 真正的 winner-selection 算法（first PASS / quality-gradient / Best-of-N scoring）。
- K-class sentinel ledger / 多写者协议改造。
- Telemetry 跨 task 聚合 dashboard / report 生成（v0.8.5 只产生 raw events，分析靠手工或 ad-hoc 脚本）。
- AFK/budget/crash-recovery 在并发场景下的扩展。
- 远程 telemetry 上报（OTel / 外部服务）—— 只本地落盘。

## Research References

- `research/codex-consult-r1-output.md` — Codex R1 反对意见 + minimal P0.7 草图（保留作 v0.8.6 起点）
- `research/codex-consult-r1-prompt.md` — Codex R1 prompt（含动机选项 + 当前已知约束）
- `.flow/tasks/archive/2026-05/05-08-v0.8.3-p0.1-implementer-redispatch/prd.md` — P0.1 ADR
  含 P0.7 split 决策 + Revisit triggers

## Decision (ADR-lite)

**Context**:
v0.8.3 P0.1 ship 后，唯一 backlog 是 P0.7 parallel speculation。Phase 1 brainstorm 时 codex
consult R1 论证：当前 cap=2 + sequential 模式下，并发收益边际小（fresh worktree 非瓶颈），但
代价大（K-class sentinel race / state 单 round 模型 / AFK/budget/crash 全要改）；并指出
P0.1 ADR Revisit triggers 都未被数据验证，pre-build 是投机架构。

**Decision**:
v0.8.5 不实现 P0.7。改 ship dispatch telemetry + feedback enrichment（Round 1 diff summary
进 Round 2 prompt）。P0.7 正式 deferred 至 v0.8.6+，**触发条件由数据驱动**：
- 触发 1: telemetry 显示 worktree create p50 >15s OR implementer wall p95 >5min（单 round 太慢，
  并发有真实收益空间）
- 触发 2: Round 2 FAIL rate >40% AND FAIL reason 集中在 implementation-path-dependent（即同
  brief+feedback 不同实现路径会成功，diversity 收益真实）
- 触发 3: 用户在 ≥2 个 task 主动反馈"想要并发推测"（需求拉动）

拒绝方案：
- 「先 telemetry 后 minimal P0.7 合一个 release」（拒）：合 release 会逼时间表妥协，分 release 更干净
- 「直接做 codex 推荐的 minimal P0.7」（拒）：仍属 3-5 天 scope + 6 类风险点 + 数据无支撑
- 「跳 v0.8.5 直接 v0.9.0」（拒）：丢失 telemetry 这个对所有后续决策都有用的基础设施

**Consequences**:
- Short-term cost: 1-2 task days（telemetry 埋点 + schema + Round 2 prompt 改 + 测试）；
  无新状态机，风险面小。
- Long-term benefit: dispatch 可观测 → 后续所有架构决策（P0.7 / round-cap 调整 / reviewer 优化）
  都能数据驱动，减少投机；feedback enrichment 直接缓解 P0.1 已知的"信息不够"问题；
  telemetry schema 是 v0.9 codex/reviewer 改造的前置基础设施。
- Reversibility: 高。Telemetry 是只读旁路，可一键 off；feedback enrichment 是 prompt
  附加段，去掉即回退。
- 对 P0.1 假设：原 fresh-per-round 设计基于"feedback alone 足以重新实施"。enrichment 让这条
  假设更稳；若 enrichment 后 Round 2 PASS 率显著上升，反过来证明 P0.7 必要性更低。

**Revisit triggers**:
- 三个数据触发条件中任一满足 → 启动 v0.8.6 P0.7 RFC（采用 codex minimal 草图：N=2
  implementer hedge + 串行 reviewer + controller-only marker + lane prompt 显式差异化）
- Telemetry 落地后 30 天内无 task 命中触发条件 → 把 P0.7 移到 long-term backlog 或撤项
- 出现 telemetry 性能开销（>5% dispatch wall 增加）→ 改为 sampling / opt-in

## Technical Notes

- **Files to inspect**（待 Phase 2 计划阶段细化）：
  - `scripts/auto_dispatch.py`（或同名 dispatch entry — Phase 2 grep 验证）
  - `claude/skills/flow/flow-phase2-execute/SKILL.md` 若 prompt template 在 skill 内
  - `_phase2_dispatch` / `dispatch_with_retry` / `_write_review_status_to_worktree` 调用栈
  - 现有 logging utils（若有）—— 优先复用，不新建框架
- **Constraints**:
  - 不改 `_phase2_dispatch` 签名（保 P0.1 稳定接口）
  - 不动 K-class sentinel 协议
  - Telemetry 只本地落盘到 task dir 内（避免跨 repo / 跨 task 污染）
- **Related ADRs / pitfalls**:
  - `dispatch-shim-silent-kw-drop.md`（任何新 kwarg 必带 placeholder + fail-closed assertion）
  - `phase-state-triple-bug.md`（动 state writer 时双阶段提交不变量）
  - P0.1 ADR（fresh-per-round 设计原意）
- credentials_ref: 不涉及。
