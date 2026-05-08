# v0.8.2 P0 core: AFK timeout + Budget enforcement + Phase 2 retry loop

> Created: 2026-05-08
> Slug: v0.8.2-p0-core
> Type: backend  <!-- 后端/UI/数据/文档/部署/调研 -->
> Complexity: complex  <!-- trivial/simple/moderate/complex -->

## Goal

将 v0.8.1 仅有 schema 占位的 **T17 AFK timeout** 与 **T18 Budget enforcement** 真正接通运行时；同时把 Phase 2 dispatch 当前的"gate 跑一次就 fail-fast"改造成 **retry-on-non-pass + 预算感知的有限重试 loop**。三者强耦合：retry-loop 是 budget enforcement 的执行容器；AFK timeout 是 retry 与子 agent 长跑场景的兜底。完成后一次 ship 让"budget 真的会卡你、AFK 真的会停下"。

## What I already know

- v0.8.1 已 ship：`autonomy_orchestrator` + `acceptance_verify` capability 落位；T17/T18 schema 已写但 hook 端不消费（schema-only placeholders）；Phase 2 gate fail-fast。
- `.flow/v0.8.2-backlog.md` §1 已列出 P0 deferred 三项的 v0.8.2 目标语义（24h cap、mechanical activity signals、5 counters + dual-counter、budget-aware retry rounds）。
- 18-class blindspot framework（A–T）已 promote 到 `.flow/pitfalls/claude-review-blindspots.md`，dispatch implementer 时挂载；budget/retry 相关高发盲点：B（state machine）、E（shell=True 边界）、G（disk-state 漂移）、I（复用前 task counter）、J（链式 paper-cut）。
- Estimator 已在 v0.8.1 round-2 修好 1M alias 优先级；budget 计数器对 token 估算的依赖可直接复用 `scripts/common/context_estimator.py`。
- v0.8.1 K-class sentinel 违规 2 次（implementer self-touch `~/.claude/hooks/.review-passed`）——本 task dispatch prompt 必须显式禁止该行为用于 first-pass commit（沿用 backlog §2.2 推荐）。
- Suite 当前基线 822 PASS（717 smoke + 105 unit）；新增 case 用相对引用 "≥ baseline + N"（backlog §2.3 规则）。

## Requirements

**R1 — T17 AFK timeout（运行时接通）**
- `afk_on_timeout: wait | abort`，**default = `wait`**（autonomy 场景常态是等 review；abort 太激进）。
- 24h hard cap（无论 mode 都强制结束 + 落 snapshot；与 budget 共享 hard-stop 路径）。
- Mechanical activity 信号至少包含：(a) 监控目录 file mtime；(b) 子 agent 命令 issuance；(c) subagent heartbeat / progress.md 更新。任一 tick → 重置 AFK 计时。
- timeout 计算复用 R2 paused-clock interval records（不重复实现 clock）。

**R2 — T18 Budget enforcement（5 counters + dual-counter）**
- **5 个 counter（已锁）**：`tokens_in / tokens_out / cost_usd / active_wallclock_minutes / subagent_dispatches`
  - `dispatch_rounds` 不在 budget 内（见 R3，作 retry 配置）。
  - `wallclock` 必须带 `active_` 前缀避免与"真实经过时间"歧义；与 AFK paused-clock 同体系。
  - `subagent_dispatches` 全局计数（含 nested subagent 派单，防 fanout 逃逸）。
- **Snapshot schema 字段（实现前冻结）**：每个 counter 落 `value / limit / hit_at_iso / estimated:bool`；`cost_usd` 额外带 `model_id / pricing_version`；token 计数因 estimator ±20% 粗糙度，阈值需带 slack（建议 90% trip wire warn / 100% hard stop，避免精度幻觉）。
- **Paused-clock 持久化**：暂停区间作 first-class records 落盘 `[{paused_at, resumed_at, reason}, ...]`，不存单值。crash-resume 时按 records 重建累积时间，防双算/漏算。
- **Q3.2 dual-counter（含 invariant 5 条，全部强制）**：
  1. `dispatch_retry_rounds` 限制 implementer 重试循环（见 R3）；
  2. `codex_review_rounds` 独立 hard cap（防 codex 永远 `rejected_with_rationale` 导致 stall）；
  3. 5 budget counter 仍 cap 一切（任何一项命中即终止）；
  4. 任何 terminal hit（budget / retry / review）都写**同一份** hard-stop snapshot（schema 单一）；
  5. 不存在"既不动 dispatch_retry 也不动 codex_review_rounds"的路径——每次 dispatch 必落账于其一。
- 命中时行为：硬停 + 落 snapshot；语义不软告警。

**R3 — Phase 2 retry-on-non-pass loop**
- gate 不再 fail-fast；非 pass → 进入 retry round，新 round 携带 reviewer 反馈作 prompt prefix。
- 两个独立 round cap（**不在 budget 5 counter 内**，作 Phase 2 配置）：
  - `phase2.retry.max_dispatch_retry_rounds`（implementer 重试硬上限，default = **3**）
  - `phase2.review.max_codex_review_rounds`（codex `rejected_with_rationale` 累计硬上限，default = **2**；命中即终止 review-side dispatch）
- 任一 round cap 命中或 R2 任一 budget counter 命中 → 终止；落同一份 hard-stop snapshot。
- 每 round 写 progress.md 增量条目，不覆盖前轮结果（含本轮 round 编号 + 消耗的 counter delta）。
- Reviewer 反馈纳入 prompt 时禁透传 18-class 触发清单（防 implementer 直接背答案；只透传 reviewer 给出的具体 finding）。

**R4 — Dispatch 模板硬化**
- 自动 prepend："禁止 implementer `touch ~/.claude/hooks/.review-passed` 用于 first-pass code commit。doc-only / fix-already-reviewed-code 可继续允许。"
- 18-class blindspot 摘要挂载到 reviewer agent 的 system prompt（已 promote，引用即可）。

## Acceptance Criteria

- [ ] **R1.1** T17 wait/abort 两种 mode 各有 e2e 用例；模拟 AFK 24h cap → 强制结束 + 落 hard-stop snapshot
- [ ] **R1.2** T17 三类 mechanical activity 信号（mtime / cmd issuance / heartbeat）各 1 单测，触发"非 AFK"判定 → 重置 AFK 计时
- [ ] **R2.1** 5 个 counter 每个有 hit/near-hit 单测（含 `tokens_in / tokens_out / cost_usd / active_wallclock_minutes / subagent_dispatches`）；任一命中 → 落同一份 hard-stop snapshot
- [ ] **R2.2** Snapshot schema 字段稳定：`{counter_name, value, limit, hit_at_iso, estimated:bool}`，`cost_usd` 额外含 `model_id / pricing_version`
- [ ] **R2.3** Token counter 90% trip wire warn / 100% hard stop（slack 单测覆盖 estimator ±20% 误差幻觉）
- [ ] **R2.4** Paused-clock interval records 落盘；1 个 crash-resume 单测验证不双算/漏算
- [ ] **R2.5** `subagent_dispatches` 全局计数（nested subagent 派单计入；1 个嵌套场景单测）
- [ ] **R3.1** Phase 2 retry loop：集成测试"首轮 fail → 二轮 pass"，progress.md 出现两条 dispatch 条目（含 round 编号 + counter delta）
- [ ] **R3.2** Phase 2 retry loop：达到 budget hard cap → 终止 + 不再起新 round（单测）
- [ ] **R3.3** `max_dispatch_retry_rounds`（default 3）命中单测；`max_codex_review_rounds`（default 2）命中单测
- [ ] **R3.4** Dual-counter 路径覆盖：codex `rejected_with_rationale` → 仅消耗 `codex_review_rounds`，不动 `dispatch_retry_rounds`（单测）；并验证不存在"既不动 retry 也不动 review_rounds"的路径
- [ ] **R4.1** Dispatch 模板单测覆盖 K-class 禁令自动 prepend（first-pass commit 禁 `touch ~/.claude/hooks/.review-passed`）
- [ ] **R4.2** Reviewer agent system prompt 含 18-class blindspot 摘要引用（不透传给 implementer）
- [ ] Suite ≥ baseline + 12 新 case；Lint/typecheck/CI green
- [ ] Codex review pass（走 round-3 plan-pass + final review gate；防 v0.8.1 同类盲点反弹）

## Definition of Done

- Tests added/updated where appropriate
- Lint / typecheck / CI green
- Docs/notes updated if behavior changes
- Credential grep self-check passes
- Phase 4 sediment notes filled in (even if "no new ADR/pattern")

## Out of Scope

- **P1 backlog**：Staleness in-loop gate / Tier 3 cmd notification / Sentinel hardening（留给 v0.8.2.1 或后续）
- **P2/P3 backlog**：HTTP method extend、per-pkg semver staleness、estimator Pro/Max env override、plan suite-count 相对引用规则化（留给 v0.8.3）
- 不在本 task 内调整 capability 总注册（37 caps 表保持不变；只接通已存在的 schema）
- 不重写 hook 系统骨架；仅在现有 hook 内消费 schema
- **Round 2+ implementer re-dispatch（v0.8.3 必收 P0）**：v0.8.2 retry-loop 接通了 budget/AFK/round-caps enforcement + dual-counter invariants + snapshot + transparent feedback rule，但 prod `_prod_impl` 在 round 2+ 仍是 no-op（`return {}`）—— 真正"重派 implementer subagent + 携带 reviewer feedback 作 prompt prefix"涉及 worktree state inheritance、跨 round state mutation、prev-impl 部分完成态语义，是独立 feature 需单独设计。当前 retry rounds 在 prod 形如"计数器+终止器"，没有实质修复机会。Sediment 阶段必须 promote 到 v0.8.3 backlog。

## Research References

- Codex consult session `019e0619-834f-7513-ab52-157d19f0a332`（2026-05-08，budget counter design）—— 反驳了 PRD 初稿把 `dispatch_rounds` 列入 5 counter；拍出 `active_` 前缀必要性、estimator slack 必要性、`codex_review_rounds` 独立 cap 必要性、paused-clock interval records、cost pricing_version
- `.flow/v0.8.2-backlog.md` §1 + §2.1 + §2.2 —— v0.8.1 deferred + 后续观察
- `.flow/pitfalls/claude-review-blindspots.md` 类 J（链式 paper-cut）—— Q3.2 dual-counter 缺 cap 的本质属于此类

## Decision (ADR-lite)

### ADR-1 — Budget = 5 resource counters, retry/review rounds = separate config

**Context**: v0.8.1 schema 把 `dispatch_rounds` 和 token/cost 等资源量糊在同一个 budget 体系里讨论。codex consult（session `019e0619...`）和 18-class J 类（链式 paper-cut）都指出：loop control 与 resource consumption 语义不同，混管会让任一维度 hit 后终止逻辑分裂。

**Decision**:
- 5 budget counter（resource）：`tokens_in / tokens_out / cost_usd / active_wallclock_minutes / subagent_dispatches`
- 2 round cap（loop control，独立）：`max_dispatch_retry_rounds=3` / `max_codex_review_rounds=2`
- 任一命中 → 同一份 hard-stop snapshot
- **Rejected 候选**：`tokens_total`（合并丢失诊断维度）/ `tool_call_count`（噪声 + provider-coupled）/ `dispatch_rounds` 进 budget（语义不干净）

**Consequences**:
- Short-term cost: 7 个独立配置项 + snapshot schema 多字段；hook 端消费逻辑两套（counter / round）。
- Long-term benefit: 诊断粒度高（read-heavy vs write-heavy 可分）；codex stall paradox 被 invariant 5 闭环；nested fanout 不逃逸 budget。
- Reversibility: 中等。Schema 字段冻结后改名要走迁移；逻辑切换可逆。

**Revisit triggers**:
- 用户报告 5 counter 不够诊断（出现具体未覆盖事故 → 加第 6 维度或拆字段）
- estimator 精度大幅提升（实测 < ±5%）→ trip wire 可收紧到 95%
- nested subagent 嵌套深度 > 3 频繁出现 → 考虑加 `nested_depth` 维度
- 模型 pricing 模型变化（如按 cache tier 计价）→ `cost_usd` 字段需扩

## Technical Notes

- **Files to inspect (Phase 2)**:
  - `claude/capabilities/defaults.json` —— 37 cap 注册表（不动）
  - `claude/skills/flow/flow-phase2-execute/SKILL.md` —— Phase 2 dispatch 主逻辑（fail-fast → retry-loop 改造点）
  - `claude/skills/flow/autonomy_orchestrator/` —— v0.8.1 已落 capability，T17/T18 schema 在此消费
  - `scripts/common/context_estimator.py` —— token 计数复用（`_resolve_limit` 4-rung 已 1M-aware；budget 直接调用，注意 ±20% slack）
  - hooks 路径（pre-commit `.review-passed` 体系不动；本 task 仅 dispatch 模板硬化）
- **Constraints**:
  - 不调整 capability 总注册表（37 caps 不动）
  - 不重写 hook 系统骨架
  - paused-clock 必须 first-class records（不存累积单值）
  - 5 counter snapshot schema 实现前必须冻结（acceptance R2.2）
  - K-class sentinel 违规绝迹：dispatch 模板自动 prepend `touch .review-passed` 禁令（first-pass 限定）
- **Related ADRs / pitfalls**:
  - 18-class blindspot framework（A–T）—— reviewer system prompt 挂载（不透传 implementer）
  - 类 B（state machine）/ G（disk-state 漂移）/ I（复用前 task counter）/ J（链式 paper-cut）四类高发盲点 → dispatch 模板 self-check checklist
  - v0.8.1 K-class 2 次违规历史 → 模板硬化必做
- **credentials_ref**: 无（本 task 不涉及 secrets / API key）
