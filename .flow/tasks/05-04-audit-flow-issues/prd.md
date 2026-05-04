# 审视 flow 框架现存问题

> Created: 2026-05-04
> Slug: audit-flow-issues
> Type: research
> Complexity: complex

## Goal

对 v0.3.1-alpha 框架做"找问题 + 重画边界"的 audit。Brainstorm 已识别出 7 个子项目。本任务的输出 = **问题清单 + 子项目终稿设计 + 调研报告**，作为 v0.4 实施路线图。本任务 itself 不实施代码改动。

## What I already know

- v0.3.1-alpha 刚完成（commit `d559cef`），sandbox 上做过 token-counter dogfood test
- 当前 README 已贴 MIT License + 英文双语，**实际定位 = iii) 个人主导但欢迎他人 fork**（用户确认）
- 当前框架在 prompt 层硬编码了 superpowers / impeccable / gstack / pr-review-toolkit / planning-with-files 的 skill 引用 → 紧耦合问题
- 当前 task 是共享 working tree + `.current-task` 指针切换 → 多 task 并行会污染工作树
- 已有：`stop.py`、`session-start.py`、`flow:save/pause/resume`、`flow_staleness.py`、3-tier memory promotion（项目→vault→`~/.claude/rules/`）
- 用户下游会做"复杂项目"，pause/resume + 多功能并行 + 长期记忆 + 踩坑沉淀是核心使用场景

## Requirements (用户三大核心)

1. **断点必须能记住** —— 任何中断（compact / 主动 pause / 崩溃）后能精确恢复到上次思路链
2. **长期记忆必须沉淀** —— 跨 session、跨项目；3-tier 自动 promotion 必须可靠
3. **踩坑必须沉淀** —— pitfall 捕获 + auto-load 必须无摩擦

## Acceptance Criteria

- [x] 9 个子项目（含 audit 后新增 #0 P0 修复 + #2b 模型名抽象）的最终设计写入本 prd.md
- [x] 3 份 research 报告产出到 `research/` 子目录
- [x] context-mode 与 ralph-loop 的可装性 / API / license 兼容性确认（两者都"可装但有坑"，已在 ADR 标注）
- [x] flow 当前 hard-coded skill 引用清单产出（26 处，13 个 capability）
- [x] flow 现有代码 redundancy / bug / 死代码扫描产出（3 P0 / 8 P1 / 10 P2）
- [ ] **用户 review 通过本 prd.md → 进 Phase 2 拆解为独立实施 task**

## Definition of Done (本 audit 任务)

- prd.md 包含全部 9 个子项目的设计（#0 P0 修复 + #1-#7 + #2b 模型名抽象）
- 3 份 research 报告完成
- 用户签字 → 进 Phase 2 拆解为多个独立实施 task
- credential grep self-check 通过
- Phase 4 sediment 写入（即使是 "no new ADR" 也要显式写）

## Out of Scope (本 audit 任务)

- **不**改任何代码（实施留给 Phase 2 拆出的子 task）
- **不**实际安装 context-mode / ralph-loop（只验证可装性）
- **不**做对外发布 / 推 GitHub release（v0.4 完成后再说）
- **不**重写 docs/编码框架.md（最后再统一改）

---

## Research Findings（Phase 1 调研产出摘要）

完整报告见 `research/` 子目录。

### A — 代码扫描（详见 `research/A-flow-redundancy-bugs.md`）

**3 个真实 P0 bug**：
1. `claude/hooks/pre-tool-task.py:62` —— `if impl_keywords.search(prompt) or True:` 让分支永真。被早 return 兜住但代码错误必修
2. `scripts/flow_task.py:132-137` —— archive 逻辑顺序错：`shutil.move` 之后 `get_current_task_path()` 永远返回 None（dir 已 move），导致归档**任意** task 都会清空 `.current-task` pointer
3. `scripts/flow_promote.py:283-289` —— frontmatter 重写时 `split_pos+5` 与 `match.start()` 相对偏移混算，写出格式可能错位

**8 条 P1 / 10 条 P2** —— 重点：路径硬编码 `~/projects/flow-framework` 散布 7 文件（实际仓在 `/data/Claude/`）、`scripts/config.py` 是无 caller 的死代码、`flow_save.py` dirty check 逻辑反、`session-start.py` pitfalls 字典序前 10 失去 `trigger_paths` 设计意图。

**测试覆盖**：`tests/smoke/` 全空，**零测试**。v0.4 重构前必须建 fixture，否则改炸不可见。

### B — 外部依赖兼容性（详见 `research/B-context-mode-ralph-loop.md`）

| Plugin | 判断 | 关键发现 |
|--------|------|---------|
| **context-mode** | 可装但有坑 | License = Elastic 2.0；PreCompact hook **确认存在**；`ctx <cmd>` 是 in-session MCP tool **不能脚本化**，须用 `context-mode <cmd>` CLI；**P0 风险 = Issue #415**，flow 5 个 hook **必须独立 matcher entry** |
| **ralph-loop** | 可装但嵌套不行 | Anthropic 官方 verified；不读 PRD 文件，只反复重投首条 prompt；用 Stop hook 循环，**与 flow stop.py 直接冲突**；社区共识：嵌套场景用 **bash loop 自实现** |

### C — Hard-coded skill 引用清单（详见 `research/C-hard-coded-skill-inventory.md`）

- **总量 26 处**，集中在 **10 个 prompt-layer .md 文件**（5 commands + 5 skills）
- Hooks / scripts / templates **完全没有**外部 plugin 引用 → 抽象工作面比想象中小
- Top: `gstack:` 12 / `superpowers:` 8 / `impeccable:` 7 / `yangpeng-claude-skills:` 4 / `frontend-design:` 1
- 归纳出 **13 个 capability**（已成 schema 草稿）
- 高风险点：`claude/commands/flow/continue.md` 单文件 **7 处** —— 改炸全流程的雷
- 工作量估 **半天**
- 顺手发现 **7 处模型名硬编码**（`model: "sonnet"/"opus"`）→ 搭车做

---

## 子项目终稿设计（按实施优先级排序，含 audit 后新增）

### #0 P0 bug 修复 + 最小测试 fixture (P0 — **重构前置硬要求**) 🆕

**决策**：v0.4 任何重构动手前，先把 audit 发现的 3 个 P0 bug 修掉，并建立最小回归测试 fixture，否则在 26 处 prompt 引用 + 5 hook + 1885 行 Python 上做大重构会撞机不可见。

**实现轮廓**：
- 修 P0-1 `pre-tool-task.py:62` 的 `or True`
- 修 P0-2 `flow_task.py:132-137` 归档逻辑顺序，`get_current_task_path` 必须在 `shutil.move` **前**调用并缓存判断
- 修 P0-3 `flow_promote.py:283-289` frontmatter 切片偏移；同时加 unit test 覆盖该函数（agent A 建议 follow-up #1）
- 建 `tests/smoke/` 至少覆盖：flow init / task create / task archive / phase detection / staleness / promote frontmatter
- 跑 fixture 通过 → 重构开工许可

**估时**：1-1.5 天（含写测试）

### #1 前置依赖全自动安装 (P0)

**决策**：`install.sh` 全自动 `git clone` + 跑各 skill 的 install。

**实现轮廓**：
- `install.sh` 读 `dependencies.yaml`（新增）→ 列出 plugin marketplaces + skill repos + 系统命令（gh, codex CLI）
- `claude plugin install <marketplace>/<name>` 能装的优先走官方
- 不能走官方的 git clone 到 `~/.claude/plugins/...`
- 系统命令缺失时 fail-loud 提示安装路径
- 提供 `flow doctor` 子命令，复检环境一致性

**需要调研**：context-mode + ralph-loop 是否走 marketplace install 路径（research B）

### #2 Capability 抽象层（方案 B） (P0)

**决策**：解耦能力与具体 skill。框架内核引用抽象 capability，默认映射到具体 skill，用户可在 `flow.config.local.yaml` 覆盖。

**实现轮廓**：
- 新增 `flow.config.yaml` 顶层 `capabilities:` 段
  ```yaml
  capabilities:
    brainstorm: superpowers:brainstorming
    code_review: pr-review-toolkit:review-pr
    cross_model_check: gstack:codex
    plan_writing: superpowers:writing-plans
    ui_design: impeccable:frontend-design
    # ... 完整列表 10-15 个，调研 C 产出
  ```
- flow 内部所有 skill 调用走 `flow.invoke('brainstorm')` 间接调用，根据 config 解析具体 skill
- 提供命令对比新装 skill 与现有 capability mapping → 接子项目 #3

**需要调研**：当前 flow 代码里所有 hard-coded skill 引用（research C — ✅ 完成）

### #2b 模型名抽象 (P1) 🆕

**决策**：与 capability registry 同批做。把 `claude/skills/flow/*.md` 与 `claude/commands/flow/*.md` 里 7 处 `model: "sonnet"/"opus"` 硬编码替换为抽象 role（如 `model_role: triage | research | implement | review`），通过 `flow.config.yaml` 映射到具体模型 id。

**理由**：模型迭代速度比 skill 还快（参考 Opus 4.7 / Sonnet 4.6 / Haiku 4.5 的频繁 release）；不抽象掉 v0.5 还要再迭代一次。

**估时**：合并入 #2，增量约 1-2 小时。

### #3 Skill Compatibility Diff Hook (P1)

**决策**：每次新装 plugin / skill / MCP 时自动比对，输出"重叠 / 替换 / 修工作流"建议。**不**自动改配置，只建议。

**实现轮廓**：
- 触发：**SessionStart hook 比对快照** (a-i)
  - `~/.flow/.runtime/skill-snapshot.json` 存上次扫描结果
  - 对比 `~/.claude/plugins/` 当前清单
- 比对成本：**(skill_id, version) 对只跑一次比对，结果落盘缓存** (b-i)
- 比对粒度：**读 SKILL.md 前 N 行 + capability registry 比对**（N=100），(c-ii)
- 输出：写到 `.flow/.runtime/skill-diff-pending.md`，SessionStart 把摘要注入 system-reminder
- 决定权：**只建议，不自动改配置**（容易误判，必须人 review）

**依赖**：子项目 #2 必须先完成（capability registry 是比对基准）

### #4 Multi-branch / 多 task 并行 — worktree-per-task (P0)

**决策**：默认每 task 独立 git worktree；可在 `flow.config.yaml` 配置改为 branch / shared 模式。

**实现轮廓**：
- `flow task create` 自动 `git worktree add ../<repo>-flow-<slug> -b flow/<slug>`
- 切换 task = `flow switch <task>` 帮你 cd（或交互式选）
- Phase 2 sub-agent 的 worktree 在 task worktree 内部再开（双层）
- `flow.config.yaml`:
  ```yaml
  task_isolation: worktree   # default | branch | shared
  ```
- **新增** `flow status` 子命令：树形列出所有 task 状态 (active / paused / blocked / done)
- **新增** task 间轻量依赖：progress.md frontmatter 加 `blocked_by: [task-slug]`，`flow status` 用它画依赖

**需要调研**：现有 flow_task.py 的 create 逻辑 + 配套脚本是否已经预留了 worktree 钩子（research A）

### #5 Session 持久化 — 委托给 context-mode (P0)

**决策**：硬依赖 context-mode (Q7-a-i)。flow 的 stop.py / session-start.py 大幅瘦身，原始恢复层让出。

**实现轮廓**：
- `install.sh` 自动 `/plugin marketplace add mksglu/context-mode` + `/plugin install context-mode@context-mode`（或 git clone fallback）
- **关键**：flow 的 5 个 Python hook 在 `settings.json` 中**必须各自独立 matcher entry**，不能与 context-mode 的 hook 共用 matcher（避开 Issue #415 误删兄弟 hook 的坑）
- 删除 / 简化 flow 自己 stop.py / session-start.py 的"原始捕获"逻辑
- flow 的 SessionStart hook 只保留：读 `.flow/.current-task` + progress.md → 输出 brief 给 Claude（半主动 resume）
- 双层架构最终态：
  - **Layer 1 原始恢复层** = context-mode（PreCompact / PostToolUse / SessionStart → SQLite，存 `~/.context-mode/content/`）
  - **Layer 2 语义检查点层** = flow（事件驱动三档保存，见 #7）
- `install.sh` 不要尝试调 `ctx <cmd>`（in-session MCP tool），改用 `context-mode <cmd>` CLI

**P0 风险（research B 发现）**：
- Issue #415 — matcher 隔离必须做对，否则 flow 自己的 hook 会被静默删除
- License = Elastic 2.0 → 项目 LICENSE 注明 "context-mode is Elastic 2.0, not bundled, installed at runtime"

### #6 Phase 2 执行模式系统 — Ralph Loop 模式 (P1)

**决策**（research B 调整后）：加入 ralph 模式 (Q7-b-i)，但**不调用官方 plugin**，flow 自己写 bash loop wrapper。理由：官方 plugin 用 in-session Stop hook 实现循环，与 flow 的 stop.py 直接冲突 + 不能在 sub-agent 嵌套；社区共识（coleam00 quickstart）也是 bash loop 自实现更可控。

**实现轮廓**：
- `flow.config.yaml` 加 `phase2_mode: interactive | ralph-loop | parallel-subagents`，task 创建时可单独覆盖
- 默认 `interactive`（当前行为）
- `ralph-loop` 模式：`scripts/flow_ralph.sh` 自实现：
  - 读取 task 的 `prd.md` checklist + `progress.md` 已完成项
  - while loop：每轮 fresh `claude --headless` 调用，prompt 锁定，max_iterations 兜底
  - 完成检测：`completion-promise` 字符串 + checklist 全勾兜底
  - 失败一轮：log 错误 + 进下一轮（不阻塞）
  - 不调用官方 ralph-loop plugin → 不会触发 stop.py 冲突
- `parallel-subagents` 模式：当前 Phase 2 sub-agent dispatch 流程
- task-level 覆盖：`flow task create --mode ralph-loop`

**降级理由**：从"调官方 plugin"降级为"自实现 bash loop"看似工作量↑，实际↓：避开两个 P0 冲突 + 学习成本零（脚本逻辑 50 行内）+ 可控性↑

### #7 事件驱动保存策略 — 三档 (P0)

**决策**：拒绝定时保存，纯事件驱动，三档成本：

| 档 | 触发 | 内容 | 成本 |
|---|------|------|------|
| **Lv1** | git commit / Edit /  Write 防抖批量 / sub-agent dispatch / pitfall | 结构化追加到 progress.md | 0 |
| **Lv2** | Phase 切换 / Decision 时刻 / Cross-model review pass-fail | 模板 + 少量 LLM | 低（500-1K token） |
| **Lv3** | `/flow:pause` / `/flow:finish` / Stop hook（5 分钟冷却）/ PreCompact | 全量 LLM 蒸馏 | 中（3-5K token） |

**Heartbeat 兜底**：Lv3 距上次 > 30 分钟 **且** 期间 ≥ 50 次工具调用 → 跑简版 Lv3 (~1K token)

**Edge cases**：
- Lv3 LLM 失败 → fail-soft 写 error.log，不阻塞 phase 切换
- 两 session 并发改 progress.md → append-only event log + Lv3 时压缩，不上锁
- 调研 phase 全是读文件 → 只 Lv1 trickle，不强制 Lv3
- pause/resume 反复横跳 → 5 分钟冷却窗口

**Resume 模式**：半主动 (Q6-b-i) —— SessionStart 自动 load 状态 + Claude 主动问 user "上次到 X，要继续吗"

---

## Decision (ADR-lite)

**Context**: v0.3.1-alpha 在 prompt 层硬编码 skill 生态、共享工作树、自建 session 持久化层。Brainstorm 中识别出三个根本性问题：(1) skill 生态在演进，硬编码会很快过时；(2) 多 task 并行被脏工作树污染；(3) 重复造轮子做 session 持久化，而 context-mode 已经做得更好。

**Decision**:
1. **吸收外部能力** —— 硬依赖 `context-mode`（session 持久化层）+ 集成 `ralph-loop`（Phase 2 备选执行模式）
2. **解耦内部** —— 引入 capability registry，所有 skill 引用走抽象层
3. **物理隔离 task** —— 默认 worktree-per-task，配置可选 branch / shared
4. **重画 flow unique value** —— flow = 4-phase 编排 + capability registry + 记忆分层 + 跨模型检查；session/context 让出给 context-mode

**Rejected**:
- 自建 PreCompact / SQLite 持久化层（NIH，context-mode 已有更鲁棒的实现）
- 完全 plugin 化（YAGNI for v0.4）
- 紧耦合 + 版本锚定（不解决 skill 演进问题，只是延后）

**Consequences**:
- **Short-term cost**:
  - **#0 前置**：修 3 个 P0 + 建 fixture（1-1.5 天）
  - 重构 install.sh（依赖管理 + matcher entry 隔离）
  - 抽离 capability registry（10 个 prompt-layer 文件 + 模型名搭车，半天）
  - 引入 worktree-per-task（flow_task.py 涨到 350+ 行需拆文件）
  - 自实现 ralph bash loop wrapper（50 行内）
- **Long-term benefit**: skill 生态变化只改 yaml 不改代码 + 多 task 并行干净 + session 持久化交给专精工具 + 框架定位清晰 + 模型迭代不再触发代码改动
- **Reversibility**: 可逆。capability registry 改回硬编码是 mechanical refactor；context-mode 切回自建持久化只是把删掉的 hook 加回来；ralph bash loop 删掉即可

**Revisit triggers**:
- context-mode Issue #415 未在 v0.4 实施前修复 → 维持当前的 hook matcher 隔离方案（不变设计，加监控）
- context-mode license 改为更严格 → 切换到自建 Layer 1（成本中等）
- ralph-loop 官方 plugin 修复 Stop hook 冲突 → 评估是否切回官方实现
- capability registry 在实际使用中证明抽象层成本高于收益 → 退回紧耦合 + 版本锚

## Technical Notes

- **Files to inspect (research A)**:
  - `scripts/flow_task.py`、`scripts/flow_init.py`、`scripts/flow_save.py`、`scripts/flow_promote.py`、`scripts/flow_staleness.py`、`scripts/flow_conflict.py`
  - `claude/skills/flow/*.md`、`claude/commands/flow/*.md`、`claude/hooks/*.py`
  - `templates/*.template`
- **Hard-coded skill 引用清单 (research C)**: 全文搜索 `superpowers:`、`impeccable:`、`gstack:`、`pr-review-toolkit:`、`planning-with-files:` 字符串
- **External**: `https://github.com/mksglu/context-mode`、`https://claude.com/plugins/ralph-loop`、`https://github.com/snarktank/ralph`
- **credentials_ref**: 无新凭据需求；context-mode 自身 `~/.flow/credentials.local` 模式不变

## Research References

- `research/A-flow-redundancy-bugs.md` —— 当前 flow 代码冗余 / bug / 死代码扫描（in-flight）
- `research/B-context-mode-ralph-loop.md` —— 外部 plugin 可装性 + API 验证（in-flight）
- `research/C-hard-coded-skill-inventory.md` —— flow 内部 hard-coded skill 引用清单（in-flight）
