**结论：现在不该以 A/D 为动机做 P0.7。**

A「降 wall-clock」单独站不住。当前默认 cap 已经是 `2`，实际基本是 Round 1 + Round 2，不是很多轮 sequential retry 可压缩。fresh worktree 本身通常也不是主瓶颈；真正贵的是 implementer subagent、tests/smoke、codex review round-trip。

最值得做的动机只有一个：**B，但必须是真 diversity，不是 N 路同 prompt 抽奖**。如果只是同一 brief+feedback 并发 N 次，失败原因是需求模糊、review rule 严、测试环境问题时会齐步 FAIL。C 太贪，容易把 feature 做成状态机重写。D 纯加速不值得付这个复杂度。

我的建议：**v0.8.5 不 ship production parallel speculation。先 ship telemetry + retry 信息增强。** 先量：
- worktree create duration
- implementer duration
- GateRunner/reviewer duration
- FAIL gate 分布
- Round 2 是否因“反馈不够”失败

如果数据显示 FAIL 多数是 implementation-path-dependent，再做 P0.7。

**最小可用版本**
如果一定要做，scope 应该是：

1. `N=2`，默认 off，contract opt-in。
2. 只在 Round 1 FAIL 后触发一次，不递归 speculation。
3. **并发 implementer，串行 reviewer**。两个 fresh worktree 同时跑；controller 按 lane 顺序 review，首个 PASS 赢。这样避免 sentinel 并发写，也避免 reviewer/gate side effects 并发。
4. lane prompt 必须显式不同：
   - lane A: smallest targeted fix
   - lane B: alternate approach / test-first / broader root-cause fix
5. state 不共享 `current_round_*`。每个 lane 返回独立 `LaneRecord(ctx, facts, verdict, feedback, deltas)`，controller 最后单点写 `winner_ctx`。
6. 非 winner worktree 保留到 Gate 8 成功后再清理；失败时保留 forensic。

这个版本我估 **3-5 天**才算能安全 ship。真正“parallel reviewers + first PASS wins”需要 marker protocol、budget、AFK、crash recovery 全改，至少 **1-2 周**，不该叫 P0.7 小 feature。

**资源 cost**
你的 cost 估算是合理的，但 A 的收益被高估了。

`git worktree add` 本地一般是秒级，除非大 repo、LFS、submodule、巨量文件导致 checkout >30s。真正瓶颈更可能是：
- implementer model wall time
- GateRunner 的 tests/smoke
- codex review latency
- rate limit / CLI session contention

N=2 基本就是 2x implementer token/cost。若 reviewer 也并发，就是 2x review/test/codex 成本。wall-clock 只有在“至少一路很快 PASS”时收益明显；如果全 FAIL 或资源争用，可能更慢。

**K-class Sentinel**
不要让并发 lane 写 `~/.claude/hooks/.review-passed.json`。

当前 marker writer 是单全局文件，而且 tmp path 也是固定的：`~/.claude/hooks/.review-passed.json.tmp`。并发写不只是 last-writer-wins，还可能 tmp 互相踩。pre-commit hook又按 repo/head/tree 校验并单次 unlink 消费。这协议天然是 single-writer。

干净方案：

- **最小方案**：lane reviewer 不写 marker。它只返回 structured PASS token 给 controller。controller 选 winner 后，由 controller 单独写一次 marker。
- **以后真要 parallel reviewer**：改成 marker ledger，例如 `.review-passed.d/<uuid>.json`，hook 扫描匹配当前 repo/head/tree 的 token 并只消费匹配文件。但这是 hook 安全面扩大，不该塞进 P0.7-min。

**被忽视的风险**
最大风险不是 git worktree，是状态机语义：

- `current_round_ctx/current_round_facts` 是单当前 round 模型，并发会破坏它。
- retry cap 语义会变模糊：N lanes 算一次 retry bundle，还是 N 次 attempt？
- budget 必须 preflight；否则 N 路一启动就可能越过 `subagent_dispatches/cost` 上限。
- AFK pause/resume 对并发 codex review 不天然安全。
- late PASS hazard：winner 已选后，慢 lane 继续跑并写状态/marker/日志。
- crash recovery 会复杂很多：多个 live worktrees、多个 lane terminal states，不再是一个 orphan。
- “first PASS wins”不是质量选择。除非你定义 PASS 是足够强的 threshold，否则会丢掉更小、更稳的 later PASS。

我会把 ADR 写成：**P0.7 deferred pending telemetry；v0.8.5 只做 measurement + maybe prev-round diff summary。** 真要上 speculation，就从 `N=2 implementer hedge + serial review + controller-only marker` 开始。