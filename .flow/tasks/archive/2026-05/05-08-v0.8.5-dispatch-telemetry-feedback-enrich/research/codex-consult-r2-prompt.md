# Codex consult R2 — Round N-1 diff summary 详尽度

## 背景

接 R1，v0.8.5 已 pivot 到 telemetry + feedback enrichment（你建议的方向）。

现在卡在 **feedback enrichment 的 diff 摘要详尽度** 这个点：Round 2+ implementer prompt 在
PRD brief + reviewer feedback 之外，附加 Round N-1 的 diff 摘要。问题是该附加多详细。

约束：
- 200 行硬截断（已定）
- 不附加完整全 diff（已定）
- Round 1 不附加（已定）

候选：
- **A（保守）**：只 `git diff --stat` + 顶层 `@@` hunk 标题（含函数/方法名），**无代码行**
- **B（中）**：A + 改动点上下 1 行 context（即 hunk header + 改动行的紧邻 1 行）
- **C（激进）**：完整 unified diff（200 行内不截）

## 我倾向 A，理由如下，请反驳

1. **redaction 简单**：stat 输出是 `path +N -M` + 函数名 hunk header，函数名很少泄密；代码
   行才是 redaction 主要面。A 几乎零 redaction 风险。
2. **feedback 是 primary channel**：reviewer 的职责就是讲清楚 round 1 为啥 FAIL。如果 feedback
   写得好，diff 是冗余；如果 feedback 不好，应修 reviewer 而不是用 diff 补救。
3. **prompt 经济性**：现 implementer prompt 已含 PRD brief + AC + reviewer feedback。+200 行
   diff = ~3000 tokens，是 ~2-3x 增长，有机会成本（context 留给 implementer 自己思考）。
4. **escape hatch**：A → B → C 是 1 LOC 升级。v0.8.5 ship A，telemetry 收据后若发现 Round 2
   仍因"信息不够" FAIL，再升级；不要现在就猜该多详细。
5. **B 的"上下文 1 行"看似温和实际危险**：`if api_key == "...":`、`url = f"...{token}..."` 这种
   是常见 single-line 句式，1 行 context 一样会泄。比 stat 风险高一个量级。

## 想要你回答

1. **哪个粒度对得起"feedback enrichment"这个目标**？你 R1 提 prev-round diff summary 时心里
   想的是哪个粒度？
2. **A 是否会让 Round 2 实际上和"只看 reviewer feedback"差不多**（即 enrichment 是空号）？
3. **redaction 风险**：你怎么看 1-line context 的 secret leak 面 vs stat-only？v0.8.5 应不应该
   做白名单 redactor（再消耗 0.5 task day）还是直接选最安全粒度？
4. **被忽视的边界**？比如：

   - implementer 模型读 stat 时能否 reconstruct 改动意图，还是 stat 太稀疏导致它要"猜"
   - 200 行截断对 C 在大改动 task 上是否变成"截了反而误导"

请直说。我倾向 A，但如果 A 让 enrichment 失去意义，请直接打掉。
