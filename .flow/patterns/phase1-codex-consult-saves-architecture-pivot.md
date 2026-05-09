---
title: Phase 1 codex consult catches architecture-pivot decisions
class: workflow / decision-making
tags: [phase1, brainstorm, codex-consult, ADR, complex-task]
trigger:
  - complexity == complex
  - scope >= 1 week
  - task description 含 "implement X 因为 Y" 推理链
applicable_to:
  - architectural / multi-layer changes
  - novel concurrency / state-machine extensions
  - pre-build features for unverified bottleneck
discovered: 2026-05-09 (v0.8.5 from P0.7 to telemetry pivot)
---

# Phase 1 codex consult saves architecture-pivot decisions

## 模式

Phase 1 brainstorm 中、ADR 草案前，对架构性决策（complexity=complex、scope ≥ 1 周）
**必须** 调 `codex consult` 做 cross-model second opinion。

不是"代码审查"——是"动机审查"：在还没投入时间实施前，让独立模型挑战立项前提。

## 步骤

1. **Triage** 后确认任务是 complex / architectural / multi-week
2. 写 codex consult prompt，含：
   - 当前已知约束（前置 task、现有代码不变量、数据点）
   - 立项的候选动机方向（A/B/C/D 列出）
   - 你的疑虑（edge case / 资源 cost / 风险）
   - 显式邀请 codex 反驳：「你觉得现在不该做这个 feature，也直说」
3. 喂 codex（`codex exec - < prompt-file`）
4. 读 codex 反驳的核心论点：
   - 如果 codex 攻击你的根本动机站不住 → **重新 triage**，可能改 scope
   - 如果 codex 接受动机但建议 minimal 路径 → 采纳并把"defer 重型部分"写进 ADR
   - 如果 codex 完全同意 → 该决策可信度高，可以快速进 Phase 2
5. ADR 沉淀 codex 论点 + 拒绝/采纳的决策；触发条件由数据驱动而非"作者直觉"

## 反例

trivial / simple 任务 **不需要** codex consult：
- round-trip overhead（写 prompt + 读 output）≥ 任务实施时间
- 决策逆转代价低，事后调整即可

## v0.8.5 实例（典型救场）

**原立项**：v0.8.5 P0.7 parallel speculation dispatch（complex，~1-2 周）
- 动机候选 A: 降 wall-clock 时延
- 动机候选 B: 提高 PASS 命中率（diversity）
- 动机候选 C: 两者
- 动机候选 D: 最小切换

**Codex consult R1 反驳**：
- A 站不住：cap=2 sequential 最多 1 retry；fresh worktree 不是真瓶颈，瓶颈是
  implementer/tests/codex review wall time
- B 仅在真 diversity（lane prompt 显式不同）时收益真实
- 整体建议：v0.8.5 不 ship full P0.7。先 ship telemetry，数据驱动再决策
- 若坚持要做：N=2 implementer hedge + 串行 reviewer + controller-only marker
  + lane prompt 显式差异化，3-5 天 scope

**采纳结果**：
- v0.8.5 pivot 到 dispatch telemetry + feedback enrichment（moderate, 1-2 天）
- P0.7 deferred 到 v0.8.6，3 数据触发条件写进 ADR
- **节省 ~1-2 周架构投机** + 后续 P0.7 决策有数据支持

## 关键论据：codex 攻击哪些维度通常奏效

1. **资源 cost vs 收益边际**（"你说省 50% 时延，但实际只压缩 1 个 retry round"）
2. **既有不变量被破坏的风险**（"K-class sentinel 是 single-writer，并发 N 路 race"）
3. **数据触发条件未被验证**（"你说 latency >30s 才需要并发，但你测过吗"）
4. **过度耦合**（"做 X 同时要改 AFK + budget + crash recovery，分一起做"）
5. **scope 与 release name 不匹配**（"叫 P0.7 小 feature 但实际是 1-2 周架构改"）

## 衍生 patterns

- **Two-stage codex consult**：
  - R1 = 动机审查（Phase 1 brainstorm 中）
  - R2 = 代码审查（Phase 3 verify 中，`flow:codex-review`）
  - 两者 framing 不同，但都是 cross-model 兜底
- **Pivot 友好的 PRD 结构**：PRD top 含"Pivot 历史"section，记录 codex 论据
  + 重新 triage 后的新 scope；archive 时保留作未来 RFC 起点

## 反模式

- ❌ codex consult 只问"你觉得我的方案好吗"——没给动机方向，codex 只能宽泛回答
- ❌ codex consult 后 cherry-pick 同意自己的论点——失去独立判断价值
- ❌ trivial / simple 任务也调 codex consult——浪费 round-trip token
- ❌ 用 codex consult 替代用户决策——consult 是输入，不是 oracle

## 参考

- v0.8.5 codex R1 prompt + output: `.flow/tasks/archive/2026-05/05-08-v0.8.5-dispatch-telemetry-feedback-enrich/research/codex-consult-r1-{prompt,output}.md`
- v0.8.5 PRD pivot history: 同 task 内 `prd.md` top section
- v0.8.5 commit: `66d687d v0.8.5: dispatch telemetry + feedback enrichment`（含 ADR-lite + 3 数据触发）
