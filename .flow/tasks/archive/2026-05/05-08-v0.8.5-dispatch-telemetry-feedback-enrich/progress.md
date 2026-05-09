---
slug: v0.8.5-dispatch-telemetry-feedback-enrich
status: done     # active | paused | blocked | done
phase: sediment  # triage | research | implement | check | verify | sediment
blocked_by: []
---

# progress.md — v0.8.5-dispatch-telemetry-feedback-enrich

## Plan

**Dispatch 决策**：single sub-agent in worktree。理由：所有改动互锁同一 dispatch 调用栈
（`flow_orchestrator.py::_phase2_dispatch` + `dispatch_template.py`）；并行 agent 必在 monolith
上 conflict。改动估计 9 文件落在 "3-9 = 1 worktree agent" 区间。

**TDD 顺序**（superpowers:test-driven-development）：

| 序 | Step | 文件 | RED→GREEN |
|----|------|------|-----------|
| 1 | Schema 常量 + JSONL writer | `scripts/common/telemetry.py`（新） | 写 schema_version=1 + writer 单元测试（事件 shape / append append-only / opt-out 跳过） |
| 2 | Diff summary helper | `scripts/common/diff_summary.py`（新） | 单元测试：stat + hunk header + per-file breadth 截断 + 200 行硬截断 + 极轻量 redaction (UUID/email/long token) |
| 3 | dispatch_template 接 prev_round_diff_summary | `scripts/dispatch_template.py` | 改 `build_implementer_prompt` 加可选参数；显式 "structural map only" 标注；fixture test 验证 Round 2 prompt 包含 section + 标注 |
| 4 | flow_orchestrator 5 阶段埋点 | `scripts/flow_orchestrator.py` | wrap `worktree_create` / `implementer dispatch` / `reviewer` / `gate_run` / `codex_review` 调用为 timing context manager；每段 emit telemetry event（`outcome`、`fail_reason_raw`） |
| 5 | Round 2+ feedback enrichment 接线 | `scripts/flow_orchestrator.py` | retry loop 在 Round N≥2 时调 `build_diff_summary(prev_worktree_path)` 并传入 `build_implementer_prompt`；integration test 验证 Round 2 prompt 含 diff map |
| 6 | Contract 双开关 + 默认值 | `claude/capabilities/defaults.json` + 配置读取 | 新增 `dispatch.telemetry` / `dispatch.feedback_enrichment` 默认 `on`；off 时跳过；测试 opt-out 路径 |
| 7 | gitignore + schema doc | `.flow/.gitignore` + `templates/telemetry-schema.md`（新） | 加 `tasks/**/telemetry.jsonl`；schema doc 写 v1 字段定义 + 已知限制 |
| 8 | Smoke 全套 | `tests/smoke/test_v085_telemetry_and_feedback_enrich.py`（新） | 端到端：mock dispatch → 跑两 round → 验证 telemetry.jsonl 5 阶段 × 2 round 事件 + Round 2 prompt 含 diff map |

**Acceptance Criteria 映射**（PRD AC1–AC5 → step）：

- AC1（schema 完备 frozen）→ step 1, 8
- AC2（5 阶段全覆盖）→ step 4, 8
- AC3（Round 2 prompt 含 enrichment）→ step 3, 5, 8
- AC4（不破 985 suite）→ step 8 末跑 `pytest tests/`
- AC5（contract opt-out 双开关）→ step 6, 8

**约束 / 不变量**：

- 不改 `_phase2_dispatch` 签名（保 P0.1 接口稳定）
- 不动 K-class sentinel 协议
- telemetry 写失败 swallow + log warning + 计数器（不阻塞 dispatch）
- Round 1 prompt 无 enrichment（无 prev round）
- `dispatch.feedback_enrichment` 与 `dispatch.telemetry` 是独立开关

**Sub-agent dispatch 参数**：

- `subagent_type: general-purpose`
- `model: opus`（按 Phase 2 协议 implementer 用 opus）
- `isolation: worktree`（fresh worktree fork master）
- 输入：本 PRD 全文 + 本 Plan + reconnaissance 结果摘要 + R2 codex 关于 diff map 定位的 framing
- 输出契约：完成 8 步、985 + N 新测试 GREEN、commit 留 master 不动（worktree 内 commit + 报 worktree path / branch）

**Stuck 阈值**：同一测试 RED 3 次未 GREEN → 调 `gstack:codex` mode=challenge 抓盲点。

## Execute Log

| 时间 (UTC) | Agent | Scope | Outcome |
|------|-------|-------|---------|
| 2026-05-09 ~01:00 | sub-agent (opus, worktree `agent-aa426c11070d2f2f5`) | 8 步 TDD 全跑 | 4 commits（391bec9 / c573ead / 05564b0 / c646333）；steps 4-8 squash 到 c646333；suite 985→1025 PASS（+40 new test）；自报 AC1-5 evidence 全有；自用 `_commit_helper.py mark-commit` (caller-IS-reviewer mode) |
| 2026-05-09 ~01:30 | main session | trust-but-verify | worktree clean / master HEAD 未变 / pytest 全套 OK / 4 atomic commit； `_commit_helper.py` docstring 确认 mark-commit 允许 trusted-automation 但需后续 reviewer pass |
| 2026-05-09 ~01:35 | codex review (gpt-5.x via `flow:codex-review`) | static review on `master..HEAD` diff | **0 P0 / 4 P1 / 2 P2**：见下方 Verify Report |

## Verify Report

### Cross-model review (Codex GPT-5.x) — 2026-05-09

**结论**: 0 P0、**4 P1 must-fix**、2 P2 must-fix（按主 session 判断升级）。原文 → `research/codex-review-r3-output.md`。

| 编号 | 级别 | 描述 | 主 session 判断 |
|------|------|------|---------------|
| I1 | P1 | `codex_review` event 在生产路径几乎不写：`_prod_review` 对所有非 pass GateRunner verdict 返回 `"fail"`，`dispatch_with_retry` 的 `if review_outcome == "rejected_with_rationale"` 在生产从不命中；测试路径写 `duration_ms=0` 是假数据 | **must-fix** — 直接破坏 ADR Trigger 1 的 codex_review 时延数据 |
| I2 | P1 | Schema `outcome` 枚举漂移：PRD 定 `pass\|fail\|skip\|null`，实现写了 `blocked` / `rejected_with_rationale`，schema doc 跟着扩枚举 | **must-fix** — 违反 R2 frozen schema 不变量 |
| I3 | P1 | diff map 只看 committed diff（`git diff --stat base..HEAD`），漏 staged/unstaged/untracked；失败 round 没 commit 时 Round 2 prompt 无结构摘要 | **must-fix** — enrichment 在多数真实生产路径失效 |
| I4 | P1 | Round N>2 enrichment 错指源：`failed_rounds[-1]` 在 Round 3 prompt 时仍是 Round 1（因 Round 2 record 在 Round 3 创建后才 append）；`RoundRecord` 缺 base commit，`HEAD~1` fallback 不代表整轮 diff | **must-fix** — 即使 cap=2 默认下 Round 3+ 罕见，但 RoundRecord schema 改是低成本，且语义错误会污染 telemetry round_num 字段 |
| I5 | P2→P1 | Round 1 `worktree_create` 没 telemetry：埋点只包了 Round 2+ helper；多数 task 只跑 Round 1 → trigger 1 (worktree p50) 不可评估 | **must-fix** (升级) — 若 Round 1 不埋点，整个 telemetry 数据集前端缺失 |
| I6 | P2→P1 | 新 smoke tests 用 fake deps 直调 `dispatch_with_retry`，未覆盖生产 gate/worktree/codex_review；这是上面 4 个生产漏洞测试通过的根因 | **must-fix** (升级) — 否则 fix 仍可能假阳性 |

### Suite count

- pytest 后台跑：unit 140 OK + smoke half（`set -e` 保证 smoke 已通过才到 unit）
- 总数与 sub-agent 自报对齐：1025 PASS（885 smoke + 140 unit），+40 new from 985 baseline

### Master 隔离

- master HEAD 未变（仍 `b4a7e8f`）
- master working tree 仅 `.flow/tasks/05-08-v0.8.5-...` untracked（task dir，预期）
- worktree 内 4 commit 干净，HEAD `c646333`

### 自 mark-commit 评估

- `_commit_helper.py mark-commit` docstring 明确允许 "trusted automation paths"；但 SECURITY 警告"only use when caller IS the trusted reviewer"
- v0.8.5 此处 sub-agent 是 implementer-as-self-reviewer 模式 → 必须有 codex external review 兜底
- codex review 已抓 6 issue → **证明 self-review 不充分**，K-class sentinel 协议设计有效（implementer 自盲点 + external reviewer 兜底）
- v0.8.6 应考虑：Phase 2 sub-agent 默认禁用 mark-commit 模式，强制走"reviewer mark + caller commit" 双步

### Codex review R2 (2026-05-09 ~02:00)

R1 6 issues 修复后第二轮：**RED**（1 P1 + 3 P2）。详情 → `research/codex-review-r4-output.md`。

| 编号 | 级别 | 描述 | 主 session 处理 |
|------|------|------|---------------|
| I6 | P1 | "Production path" smoke 仍部分 fake：line 280 直调 `_build_prev_round_diff_summary`，绕过 `dispatch_with_retry` / `_prod_impl` / prompt builder / subagent shim | **must-fix** R3 |
| I3-A | P2→must-fix | `_collect_unstaged()` 用 `git diff HEAD` 含 staged → 双计 | **must-fix** R3（real correctness bug） |
| I3-B | P2 | `_collect_untracked()` 用 `git status --porcelain` 把新目录折成 `?? dir/`，漏 nested files | defer v0.8.6 + known-limits |
| I5 | P2 | `auto_dispatch_task` `telemetry_emit_fn` kwarg 缺 unknown-kwarg 爆炸断言 | defer v0.8.6 + known-limits |

R1 verified OK（仍 correct in R2）：I1 / I2 / I4。

### Codex review R3 (2026-05-09 ~02:50)

R2 fix（I6 + I3-A）后第三轮：**GREEN — 0 P0 / 0 P1 / 0 P2**。详情 → `research/codex-review-r5-output.md`。

确认事项：
- I3-A correct（bare `git diff` 等价 working-vs-index，测试真覆盖双计 bug）
- I6 mock 边界声明真兑现：`dispatch_with_retry` / `_prod_impl` / `_prod_review` / `_dispatch_implementer_fresh_worktree` / `GateRunner.run_phase2` / `_build_prev_round_diff_summary` 全真路径
- dup hunk-block 同意 cosmetic，defer v0.8.6 defensible
- I1 / I2 / I4 R2 verified 仍 correct（codex 重新检查）
- 无 host worktree 污染（`TemporaryDirectory` 隔离）

**Codex nuance**（写测试范围声明，非 bug）：
- I6 测试 mock `_run_shell_with_pgkill` 是 gate1/4/6 共享，所以拦截 baseline/smoke
- Round 1 RED payload malformed (缺 file/line_range/class/message) → gate4 返 `inconclusive` → `_prod_review` maps `"fail"`
- **不破 I6**，但不应声称该测试验证了 parsed RED-issue path。已加 known-limits 注。

### Suite ground-truth (R3 verified)

- `bash tests/smoke/run.sh` 独立跑：888 smoke + 174 unit = **1062 PASS**（0 fail / 0 regression / 1059 baseline + 3 new I3-A unit）
- master HEAD 仍 `b4a7e8f`，worktree 12 commits（4 step + 6 R1 fix + 2 R3 fix），HEAD `8564a55`

### Final ship readiness

- [x] AC1–AC5 全部 evidence backed by tests
- [x] suite GREEN (1062 PASS, +77 net new tests, 0 regression vs 985 baseline)
- [x] codex review GREEN (R3 final verdict 0 P0 / 0 P1 / 0 P2)
- [x] master 隔离干净
- [x] known-limits 文档化（I3-B / I5 / dup hunk-block / parsed RED-issue path）
- [x] PRD ADR Decision 锁 P0.7 deferred + 3 数据触发条件
- [ ] Master merge / tag v0.8.5（Phase 4 起手）
- [ ] Sediment notes + auto-save
- [ ] Task archive

## Sediment Notes

### 新沉淀

#### 1. Pitfall 候选（拟写新文件）

**`.flow/pitfalls/fake-deps-test-can-fake-production-coverage.md`**
- 症状：sub-agent 写新 smoke test 时倾向用 fake deps 直调内部函数（如 `_build_prev_round_diff_summary`），声称是"production path 覆盖"。但绕过了 `dispatch_with_retry` / `_prod_impl` / `_prod_review` / GateRunner / subagent shim，留下 4 个生产 bug 全部假阳性通过测试。
- 根因：fake deps 是 unit test 工具，被误用到 integration scope。
- 修复：integration test 必须以**真生产入口**（`auto_dispatch_task` / `_phase2_dispatch`）驱动，仅 mock 最外层不可避免的 IO（subagent shim、外部 CLI）。
- 预防：codex review 的 mock-boundary check 是兜底；自检：所有 smoke test 必须能解释"如果 production 代码 X 出 bug，我这个测试会 RED 吗？"
- 来源：v0.8.5 codex R1 + R2 连两轮抓到的同一类问题（I6 在 R1 报告，R2 又因为 fix 仍 fake 而 reopen）。

**`.flow/pitfalls/telemetry-frozen-schema-normalize-at-write-boundary.md`**
- 症状：PRD 锁定 frozen schema `outcome ∈ {pass, fail, skip, null}`，实现处直接把 GateRunner verdict.status (`blocked` / `rejected_with_rationale`) 写进 telemetry → schema 漂移。Schema doc 跟着错改扩枚举。
- 根因：emit 处缺中间映射层；frozen schema 规则在多个调用点散落，单点 enforce 困难。
- 修复：normalize 必须发生在**写入边界**（`emit_event` 内），对调用方透明；原始字符串放副字段（`fail_reason_raw`），无信息丢失。
- 预防：单元测试 `VALID_OUTCOMES` 集合 + emit 时 normalize 前后断言。
- 来源：v0.8.5 codex R1 I2。

#### 2. Pattern 候选（拟写）

**`.flow/patterns/phase1-codex-consult-saves-architecture-pivot.md`**
- 触发：scope 是 architectural / multi-week 类（complex），用户立项 token 用了"做 X 因为 Y"句式
- 步骤：Phase 1 brainstorm 进 ADR 草案前 → 调 `codex consult` → 给 codex 候选动机方向（A/B/C/D）+ 已知约束 → 让 codex 反驳/独立判断
- v0.8.5 实例：原立项 P0.7 parallel speculation；codex R1 论证 cap=2 时延收益小、K-class race 等；建议 ship telemetry 数据驱动 → 节省 1-2 周架构投机；P0.7 deferred 转 v0.8.6
- 反例：trivial / simple 任务不需要（codex round-trip overhead 不划算）
- 价值密度：≥1 周开发的架构决策，codex consult 1-2 round 几乎稳救场

#### 3. ADR 已沉淀（PRD 内）

P0.7 deferred + 3 数据触发条件，PRD `## Decision (ADR-lite)` 内；未独立 ADR 文档，交由 v0.8.6 在 trigger fire 时启动 RFC 时另立。

#### 4. 现有 pitfall 命中

- `dispatch-shim-silent-kw-drop.md` — sub-agent 在 R1 实现时直接遵守，新 kwarg `telemetry_emit_fn` 全用 explicit named param + `None` default
- `phase-state-triple-bug.md` — I4 fix 时 sub-agent 主动验证 P0.1 two-phase commit 不变量未破
- `edit-absolute-path-resolves-master.md` — sub-agent 全程 worktree 路径前缀，无 master 误写
- `claude-review-blindspots.md` — 18 类盲点列表中至少 3 类在 codex R1/R2/R3 命中（fake-coverage、frozen-schema-drift、staged-double-count）

#### 5. 元教训

- **3 轮 codex review 是 v0.8.5 真实路径**（R1 RED 4 P1 → R2 RED 1 P1+3 P2 → R3 GREEN）；不是预期的"一次过"。这次显式记录：implementer self-review 漏抓 6/6 真实 bug；K-class sentinel "external reviewer 兜底" 设计完全救场。
- **Pivot 决策（P0.7 → telemetry）的元价值**：v0.8.5 自身就是"telemetry 想解决无数据决策，自己却差点 ship 假数据"的反例——codex 抓的 6 issue 里 3 个直接破坏 telemetry 数据可用性。
- **CHANGELOG 必须 grep verify**：commit msg / CHANGELOG 里列的"修改文件"自查 git diff 验证是否真改（v0.8.5 一度误声 `claude/capabilities/defaults.json` 修改，amend 修正）。

## Retro (optional)

<!-- TEMPLATE: 自由格式回顾——什么 worked / didn't / 框架反馈。可省略。 -->

## Files Touched

_Updated 2026-05-09 03:20 (last 20 unique edits)_:

- `/tmp/v085-msg.txt`
- `CHANGELOG.md`
- `.flow/tasks/05-08-v0.8.5-dispatch-telemetry-feedback-enrich/progress.md`
- `.flow/tasks/05-08-v0.8.5-dispatch-telemetry-feedback-enrich/prd.md`
- `.flow/tasks/05-08-v0.8.5-dispatch-telemetry-feedback-enrich/research/codex-consult-r2-prompt.md`
- `.flow/tasks/05-08-v0.8.5-p0.7-parallel-speculation/research/codex-consult-r1-prompt.md`
- `.flow/tasks/05-08-v0.8.5-p0.7-parallel-speculation/prd.md`
- `/tmp/v0831-tag-msg.txt`
- `/tmp/v0831-hotfix-msg.txt`
- `.flow/pitfalls/test-time-bomb-hardcoded-date-vs-real-now.md`
- `.flow/tasks/05-08-v0.8.3.1-hotfix-afk-park-rc5/prd.md`
- `tests/smoke/test_phase2_retry_loop.py`
- `.flow/tasks/05-08-v0.8.4-p0.6-commit-helper/progress.md`
- `/tmp/v084-p06-flow-msg.txt`
- `/tmp/v084-p06-dotfiles-msg.txt`
- `/home/yangpeng/claude-linux-config/claude/CLAUDE.md`
- `/home/yangpeng/claude-linux-config/claude/rules/code-commit.md`
- `/home/yangpeng/.claude/projects/-data-Claude-flow-framework/memory/project_v0_8_3_status.md`
- `tests/hooks/test_commit_helper.py`
- `/home/yangpeng/claude-linux-config/claude/hooks/_commit_helper.py`

## Commits

- [2026-05-09 03:21] `66d687d` v0.8.5: dispatch telemetry + feedback enrichment
