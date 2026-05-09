直说：我会 ship **A，但把它定义成“结构性 diff map”，不是假装给代码上下文**。

**1. 我心里想的粒度**

我 R1 说 prev-round diff summary 时，想的不是 C，也不是带代码行的 B。更接近：

> `git diff --stat` + changed files + hunk headers / symbols + maybe added/deleted file markers

也就是 **A / A+**。

它的目标不是让 Round 2 implementer 复盘 Round 1 的实现细节，而是回答几个低成本问题：

- Round N-1 动了哪些文件？
- 改动集中在哪些模块？
- 是否动了测试？
- reviewer feedback 说的问题，对应的改动区域大概在哪里？
- implementer 是否应该继续在同一片区域修，还是 Round N-1 根本改错地方了？

这已经是 feedback enrichment。不是空号。

**2. A 会不会等同于只看 reviewer feedback**

不会，但它的增益是“定位和校验”，不是“补全实现”。

如果 reviewer feedback 是：

> Validation still accepts empty task names.

没有 diff summary，Round 2 只能凭 PRD 和反馈去找。  
有 A，它至少能看到 Round 1 改了：

```text
src/task/parser.py | +12 -4
@@ def normalize_task_name(...)
tests/test_task_parser.py | +20
@@ def test_empty_task_name(...)
```

这能告诉它：上一轮很可能在 parser 层修过，但失败仍在 validation 语义；它可以更快判断是逻辑没覆盖、测试错位、还是改错抽象层。

A 不适合替代 reviewer feedback。这个判断是对的：**primary channel 应该还是 reviewer feedback**。diff summary 是审计痕迹和定位线索。

**3. redaction 风险**

我同意你对 B 的担心。1-line context 看起来温和，但安全边界已经从“元数据”跨到了“源代码内容”。

泄漏面差异很大：

- `diff --stat`：主要暴露路径、文件名、规模。
- hunk header：暴露函数/类/方法名，有少量业务语义风险。
- 1-line context：直接暴露字符串、URL、headers、SQL、config、token 拼接、测试 fixture、客户名、内部域名。

B 不是 A 的小升级，是安全类别变化。

v0.8.5 我不会为了 B 做 0.5 task day 的白名单 redactor。原因是白名单 redactor 很容易给人虚假的安全感，而且代码行的敏感信息形态太多。除非你已经要长期支持 code-bearing summaries，否则现在不值得。

我的建议：

- v0.8.5 选 **A**
- 做极轻量 redaction 只覆盖路径和 hunk header 中明显长 token / UUID / email / URL 这类
- 不引入代码行 redaction 体系
- telemetry 里单独记录 `round2_failure_reason=insufficient_previous_diff_context` 之类的信号

**4. 被忽视的边界**

C 的 200 行截断确实可能误导。大 diff 下，前 200 行通常偏向文件顺序，不一定偏向关键失败点。它会制造一种“我看到了 diff”的错觉，但实际可能只看到了无关初始化、imports、测试 fixtures 或第一个文件。

A 的主要风险是 hunk header 质量不稳定：

- JS/TS、Python 通常还行
- 配置文件、JSON、YAML、Markdown 基本没函数名
- 某些 generated / declarative 文件只会显示无意义 header
- 大规模重排会让 stat 很吵

所以我会加两条防护：

1. **明确标注这是 structural summary, no code lines included**，避免 implementer 把它当完整依据。
2. **截断时保留 per-file breadth**，不要让一个大文件吃掉 200 行。比如每文件最多 N 个 hunk headers，最后写 `... truncated`.

最终判断：

- **A 对得起 feedback enrichment。**
- **B 的收益不够稳定，风险明显上升。**
- **C 在 200 行硬截断下容易变成偏置样本。**

我不会打掉 A。v0.8.5 ship A 是合理的；但我会把成功标准写清楚：它 enrich 的是 reviewer feedback 的定位和可核验性，不是给 implementer 第二份源码上下文。