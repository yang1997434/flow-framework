# Codex consult — v0.8.5 P0.7 parallel speculation 动机定位

## 角色

你是 cross-model second opinion。我在 flow-framework（一个 Claude Code 的工作流框架）准备做
P0.7 "parallel speculation dispatch"。希望你基于背景判断 **核心动机应该是哪一条**，并对设计
目标 / 复杂度边界给出独立判断。

## 背景

flow-framework 在 v0.8 系列演进了一套 dispatch 状态机：

1. **Phase 2 retry loop**（v0.8.1 ship）：reviewer FAIL → loop until cap，每 round cap 默认 2。
2. **fresh worktree per round**（v0.8.3 P0.1 ship）：每个 retry round 都从 base branch fork
   独立 worktree，不继承上一轮文件状态；只继承 reviewer feedback 文本 + PRD brief。
   - State 双阶段提交：`current_round_*` 与 `failed_rounds: list[RoundRecord]`。
   - winner ctx 显式从 `_phase2_dispatch` 返回 `(rc, winner_ctx)`，传 Gate 7 merge。
   - K-class sentinel `~/.claude/hooks/.review-passed` 是单次消费，pre-commit 校验。
3. **现状**：dispatch 是 **sequential** —— round 1 → review → 若 FAIL → fork 新 worktree →
   round 2 → review → ...。每 round 串行等待。

P0.1 PRD 的 ADR 中 explicit split 出 P0.7（"并发推测 dispatch"），并标了 **Revisit triggers**：
- fresh worktree 创建 latency 在生产 >30s/round → 转 P0.7 并发
- "Round N 信息不够"（feedback alone 不足以重新实施）→ 加 prev round diff 摘要 OR diversity 路径

## 我的当前候选

我列了 4 条核心动机方向给用户：

A. **降 wall-clock 时延** — 主要为加速：fresh worktree 创建 + 多轮 sequential 重试太慢，
   并发 N 路缩短到 1 路时间。
B. **提高 PASS 命中率（diversity）** — 主要为多样性：同一 brief+feedback 让 N 个 implementer
   各自尝试，先到 reviewer PASS 的胜出，靠路径多样性提高一次成功率。
C. **两者都要（平衡）** — N 路 prompt 之间要有差异化（temperature / 不同 hint），同时
   winner-takes-all。
D. **最小可用** — N=2 sequential→parallel 的最小切换，纯加速；diversity 留 v0.8.6+。

## 我的疑虑

- **资源 cost**：N 倍 worktree（N 个 git fork）+ N 倍 subagent token + N 倍可能的 codex review
  调用。如果 P0.1 已经 fresh-per-round 在 wall-clock 上不算瓶颈（fork 是 git checkout，几秒级），
  那 A 的收益边际可能很小。
- **K-class sentinel 并发竞态**：N 个 round 都 PASS 时，`.review-passed` sentinel 会 race。
  当前是 single-use 设计。这是个真实的不变量破坏点。
- **State 写入并发**：`current_round_*` 双阶段提交是为 sequential 设计；N 路并发完成顺序
  不确定 → state writer 需大改，否则 race condition。
- **Diversity 的实际效果未知**：如果 reviewer FAIL 主要因 brief 模糊或 reviewer rule 太严，
  N 路相同 prompt 会"齐步 FAIL"，diversity 收益为 0。
- **Winner 选择语义**："先 PASS 的赢" 看似简单，但若 N 路都 PASS / 部分 PASS 部分 FAIL，
  还要看是否有 quality gradient（时间最早 ≠ 质量最好）。

## 想要你回答

1. **哪条动机最值得做**？给独立判断，不必照顾我的列表。如果你觉得现在不该做这个 feature，
   也直说。
2. **如果做，最小化版本应是什么**？（目标 = 多大 scope 在多少天内可 ship）
3. **资源 cost 估算合理吗**？fresh worktree fork 时延在 git fork(local) + subagent dispatch 真
   是瓶颈吗，还是 reviewer round-trip 才是瓶颈？
4. **K-class sentinel 并发竞态怎么处理**？N 路 PASS 同时写 sentinel 是 dispatch 不变量破坏点，
   有什么干净方案？
5. **被忽视的风险或反例**？（包括 "不该做这个 feature" 的可能）

请直接给意见，不要照顾礼貌。本项目历史上 codex 的尖锐反对意见多次救了我们。
