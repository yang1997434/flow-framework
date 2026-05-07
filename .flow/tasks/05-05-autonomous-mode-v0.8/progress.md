# progress.md — autonomous-mode-v0.8

## Plan

来源：本会话与用户共同 brainstorm 出的 v0.8 自治模式设计，经 codex 两轮 review（round 2 RED → 重排）。

设计 doc: `prd.md`（含 §1 architecture / §2 surface area / §3 phasing v0.8.0→v0.8.3）。

**当前阶段**：v0.8.0 已 ship+merge（master a536864）。v0.8.1 进入 focused-design 阶段。

**v0.8.1 路径**（codex consult A-prime verdict，session `019dfc03-...`）：
1. ✅ 写 `design/v0.8.1-execution-semantics.md` — 16 open questions（covering codex 7 focus areas）
2. ✅ 走完 16 个 Q（4 轮 codex consult，全 lock）+ 1 §6/§7 矛盾修
3. → 用 `superpowers:writing-plans` 写 `plans/v0.8.1-safety-stack.md`
4. → codex round 3（GREEN gate，预留 session `019dfb47-...`）— 审 design 矩阵 + plan
5. → subagent-driven dev（按 v0.8.0 T1-T11 模式）
6. → ship

## Execute Log

| 时间 (YYYY-MM-DD HH:MM) | Agent | Scope | Outcome |
|------|-------|-------|---------|
| 2026-05-05 22:30 | main | brainstorm dialogue | 7 条核心决策（stop conditions / multi-turn codex / autonomy unit / retry / context bound / Phase 3-4 / trigger） |
| 2026-05-05 22:35 | main + codex | round 1 review | codex 标 7 blocker + 19 silent-degeneration mode |
| 2026-05-05 22:40 | main | §1+§2+§3 完整设计 | 吸收 round 1 反馈 + 用户 Q1/Q2（acceptance_criteria + E2E）|
| 2026-05-05 22:45 | main + codex | round 2 review | **RED** —— 分期不安全 + advisory enforcement + 自报问题 |
| 2026-05-05 22:50 | main | 重排 5→4 ship + worktree + orchestrator 派生事实 | 写 prd.md |
| 2026-05-06 06:33 | main | v0.8.0 ship merged (PR #12 → master a536864) | tag v0.8.0 |
| 2026-05-06 14:00 | main + codex (consult) | v0.8.1 kickoff path consult | A-prime: focused interaction design + failure matrix |
| 2026-05-06 14:30 | main | v0.8.1 execution semantics matrix (codex 7 focus areas) | `design/v0.8.1-execution-semantics.md` — 16 open questions, all with defensible defaults |
| 2026-05-06 14:45 | main | round 1 lock — Q1.1/Q1.2/Q1.3 all default YES | 3 → DECIDED |
| 2026-05-06 14:55 | main + codex | round 2 consult — Q2.1/Q2.2/Q3.1/Q3.2 | codex 2-refine（Q2.1 pause / Q3.2 dual-counter）+ 2-agree |
| 2026-05-06 15:00 | main | round 2 lock — accept all codex refinements | 4 → DECIDED, 9 open / 7 decided |
| 2026-05-06 15:15 | main + codex | round 3 consult — Q4.1/Q4.2/Q5.1/Q5.2/Q5.3 | codex 2-refine（Q4.1 +shortsha / Q5.3 archive subdir）+ 3-agree |
| 2026-05-06 15:20 | main | round 3 lock — accept codex refinements + Q5.3 path tweak | 5 → DECIDED, 4 open / 12 decided |
| 2026-05-06 15:35 | main + codex | round 4 consult — Q6.1/Q6.2/Q7.1/Q7.2 + 全文一致性检查 | codex 3-refine + 1-agree + **抓出 §6/§7 致命矛盾**（post-`auto_engaged` crash recovery 必须 block 不是 fallback interactive） |
| 2026-05-06 15:45 | main | round 4 lock — accept all codex refinements + 修矛盾 | 5 → DECIDED + 1 矛盾修，**0 open / 16 decided + 1 contradiction fix**。准备进 plan 阶段 |
| 2026-05-06 16:00 | main + codex | round-3 design-pass review (reserved session 019dfb47-...) | **RED #1** —— 12 R findings + 8 S suggestions |
| 2026-05-06 16:15 | main + codex | round-3 R-fix shape consult (R3/R4/R10 critical) | concrete fix shapes locked: R3 9-step transactional, R4 ephemeral verification worktree, R10 auto_prepare.lock; R12 conceded conditional |
| 2026-05-06 16:45 | main | round-3 RED #1 全修 — 12 R + 8 S 应用到 design doc | doc 322→570 行；新增 §8 file schemas；§3 gates 6→8；§6 9-step；新事件/状态/字段 全部 in。准备 round-3 verdict #2 |
| 2026-05-06 17:00 | main + codex | round-3 verdict #2 review | **YELLOW** — 12 R structurally resolved，8 Y + 4 S（minor，可在 plan 写时修） |
| 2026-05-06 17:30 | main | 全修 8 Y + 4 S → effective GREEN | doc ~530 行最终态。Y1 e2e timeout routing / Y4 verification worktree lifecycle / Y5 9-step gap behavior / Y6 unit tests / Y7 criterion_hash / Y8 auto_prepare_consumed / S1 global block / S2 run_id 全字段 / S3 regression post_merge_skip 禁 / S4 split smoke vs unit。Design DONE。**进 plan 阶段** |
| 2026-05-06 17:45 | main | plan Batch 1 — skeleton + 22-task index | `plans/v0.8.1-safety-stack.md` 166 行；8 group / 22 task / 15 new test 全索引。Batches 2-8 pending |
| 2026-05-06 18:00 | main + codex | Batch 1 sanity review (resume session 019dfb47) | YELLOW — 9 issues：Y1 dependency col / Y5 split T12 / Y2 staleness explicit / Y3-Y4-Y7-Y9 wording; Y6/Y8 deferred to detail |
| 2026-05-06 18:10 | main | Batch 1 → 23-task index 修订 | T12 split → T12 (gate harness) + T13 (codex review)；新增 Depends-on 列；event 数 9→10；T20 5 staleness triggers 显式；T22 SKILL 标注 codex sandbox limit；T23 release 加 7 项 validation checklist。**全 task DAG 顺序在末尾标注**。准备 Batch 2 |
| 2026-05-06 18:30 | main | plan Batch 2 — T1-T3 Group A Contract schema TDD detail | doc 166→713 行 (+547 lines)。T1 字段加 + 9 testcase / T2 ceiling enforce + 3 cases / T3 forward-compat 用 v0.8.0 tag worktree (Y9 fix)。Group A complete |
| 2026-05-07 (T15 codex round-2 fix) | main | T15 codex round-1 RED → fix-pass | 4 个 finding 全修：[P1] Fix-1 引入 `_now_iso_micro()` 把 pre_merge + 9a post_merge checkpoint 时间戳切到微秒精度（消除 same-second `FileExistsError` TOCTOU；先前作为 T15 P2 deferred 的 microsecond-race concern 至此 obsolete）；[P2] Fix-2 把 `attempt_id` 改成 task-scoped (`post_merge_<run_id>_<task_id>`)；[P2] Fix-3 用 `(orig_idx, crit)` 对保留原始 contract 索引；[P2] Fix-4 删除 9b 的 `Path.rename` 文件系统 fallback（保留 git-usable 原路径 + WARN）。Tests 636 → 639（+3 new in `test_post_merge_verify_failed_blocks.py::TestGate8CodexRound1Fixes`）。 |

## Verify Report

(待 v0.8.0 ship 后填)

## Sediment Notes

(待整体 v0.8 完成后填)

## Retro

(待 v0.8 完成后填)
