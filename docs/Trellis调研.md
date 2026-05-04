---
title: "Trellis 调研记录"
date: 2026-05-04
type: research
tags:
  - 工具
  - 调研
  - 协作
status: done
---

# Trellis 调研记录

> 上游：[mindfold-ai/trellis](https://github.com/mindfold-ai/trellis)（npm `@mindfoldhq/trellis`，调研时版本 0.5.0-beta.19，AGPL-3.0）
> 文档：https://docs.trytrellis.app/

## TL;DR

- **Trellis 是什么**：仓库级 AI 编码脚手架。往 repo 里塞 `.trellis/` 目录（spec / tasks / workspace / workflow.md），再为 Claude Code / Cursor / Codex / OpenCode / Pi 等 agent 平台生成对应的 hook + skill + slash command。
- **核心创新**：**per-task JSONL 上下文清单 + sub-agent 中心化**。每个任务自带 `implement.jsonl` / `check.jsonl`，hook 自动把里面列的 spec 文件注入到 sub-agent prompt。主 session 默认不写代码，由 `trellis-implement` / `trellis-check` sub-agent 执行。
- **本次决策**：**不替换现有方案，分层引入**。现有体系（`~/.claude/rules` + Obsidian vault + gstack + impeccable + superpowers）仍是主干，trellis 只在未来真实团队仓库里**局部装**。
- **当前不接入**——理由：版本仍在 0.5.x beta 快速迭代；目前没有真实团队项目；本次调研已经吸收了它的方法论（见 [[调研方法论]]）。

## 它解决什么问题

把通常塞在 `CLAUDE.md` / `AGENTS.md` / `.cursorrules` 里的大段 system prompt，**拆成按任务/层级渐进加载的 wiki**，让多 agent 平台共享同一份团队规范，不再各自为政。

适用于：多人 + 多 agent + 长生命周期单仓库（如生产 monorepo）。

## 实地勘察（2026-05-04 在 `/tmp/trellis-test` 沙盒做的）

```bash
trellis init -u yangpeng -y --claude --cursor --codex
# 共生成 138 个模板文件
```

### 产物全貌

| 路径 | 进 git？ | 作用 |
|------|---------|------|
| `.trellis/spec/` | ✅ | 团队规范，默认 backend 5 + frontend 6 个模板文件 + guides 跨层指南 |
| `.trellis/tasks/` | ✅ | PRD + jsonl 上下文清单 + research/。init 自动建了 `00-bootstrap-guidelines` 引导填 spec |
| `.trellis/workspace/yangpeng/` | ✅（默认共享！） | 个人 journal，每 2000 行自动滚动 |
| `.trellis/.developer` `.runtime/` | ❌ gitignored | 本地身份 + session 状态 |
| `.trellis/scripts/*.py` | ✅ | trellis 写进 repo 的 Python 工具集（task / context / workspace 全套）|
| `.trellis/workflow.md` | ✅ | Plan→Execute→Finish 三阶段定义，每步标 `[required/once/repeatable]` |
| `.claude/` `.cursor/` `.codex/` `.agents/` | ✅ | 各 agent 平台的 skill / sub-agent / hook / slash command |
| `AGENTS.md` | ✅ | 根入口，带 `<!-- TRELLIS:START/END -->` 标记，`trellis update` 只覆盖块内 |

### 三个 hook（接管了 Claude Code 的整个 hook 链）

| Hook | 时机 | 行为 |
|------|------|------|
| `SessionStart` | startup/clear/compact | 注入 workflow + 当前任务状态 + spec 索引 + 强制让首条回复说固定中文 "Trellis SessionStart 已注入..." |
| `PreToolUse(Task/Agent)` | 派 sub-agent 之前 | 读该任务的 `implement.jsonl` / `check.jsonl`，把 spec 文件**自动塞进 sub-agent 的 prompt** |
| `UserPromptSubmit` | 每轮用户消息 | 按当前 task 状态注入 workflow-state breadcrumb |

### Workflow 三阶段

```text
Phase 1 Plan    → 1.0 Create task → 1.1 Brainstorm → 1.2 Research → 1.3 Configure context → 1.4 Completion criteria
Phase 2 Execute → 2.1 Implement (sub-agent) → 2.2 Check (sub-agent) → 2.3 Rollback
Phase 3 Finish  → 3.1 Verify → 3.2 Debug retro → 3.3 Spec update → 3.4 Commit → 3.5 Wrap-up
```

## 与现有体系的冲突清单（按严重度排）

### 🔴 阻断性冲突

| 冲突 | 说明 |
|------|------|
| **Skill 重名 + 双重指令** | trellis 注册 `trellis-brainstorm` / `trellis-check` / `trellis-update-spec` 等 skill 到 `.claude/skills/`。和 superpowers 的 `brainstorming` / `verification-before-completion` 等同时存在时，主 session 会被两套近义但不一致的规训冲撞。**实操上必须二选一。** |
| **主 session 写代码 vs sub-agent 中心化** | trellis workflow 反复要求"主 session 不写代码、派 trellis-implement sub-agent"。和现在用 superpowers + planning-with-files 在主 session 直接工作的习惯**正面冲突**。 |

### 🟡 摩擦性冲突

| 冲突 | 说明 |
|------|------|
| **三个 hook 全占** | SessionStart / UserPromptSubmit / PreToolUse(Task) 全被 trellis 钉死。以后加 LSP / 自定义提醒类 hook 都要手工合并 `.claude/settings.json` |
| **强制首条中文回复** | SessionStart hook 注入"首条必须说 Trellis SessionStart 已注入..."的硬规则 |
| **中文触发词写死在 workflow.md** | "重构 / 抽成 / 独立 / 跳过 trellis / 别走流程" 这些直接进 system prompt |

### 🟢 可控冲突

| 冲突 | 说明 |
|------|------|
| `workspace/<name>/` 默认进 git | 多人协作可见个人 journal。手工 `.gitignore` 或接受 |
| Spec 模板偏 web | 默认 backend / frontend 双层模板，对生信 / CLI / 单文件项目用不上，删即可 |
| AGPL-3.0 license | `.trellis/scripts/*.py` 是 trellis 提供的代码进了你 repo——闭源商业项目要核查 |

## 替换可行性分析

| 现有组件 | trellis 能不能替 | 结论 |
|---------|----------------|------|
| `~/.claude/rules/*.md`（全局规则） | ❌ trellis 是项目级 | **不能替** |
| Obsidian vault（跨项目知识库） | ❌ trellis workspace 是项目内 | **不能替** |
| gstack `/browse` `/qa` `/ship` `/review` | ❌ 完全不碰浏览器/部署/CR | **不能替** |
| impeccable UI skills | ❌ 不做 UI 设计 | **不能替** |
| superpowers TDD/verification/code-reviewer | ⚠️ trellis 自有 check 但更弱 | **不建议替** |
| superpowers brainstorming/plans | ✅ trellis-brainstorm 接近且更结构化 | **能替（互斥）** |
| planning-with-files | ✅ trellis tasks/PRD/research 完整覆盖 | **能替** |
| 项目内 CLAUDE.md | ✅ AGENTS.md + spec/ 完整覆盖 | **能替** |
| yangpeng save/resume（项目内部分） | ✅ workspace journal 自带 | **部分能替** |

**结论**：完整替换是伪命题——它只能替"项目级"那一层，全局层（rules / vault / gstack / impeccable / superpowers 核心）必须保留。

## 决策：分层引入

```text
┌─────────────────────────────────────────────────────┐
│ 全局层（保留不动）                                   │
│  ~/.claude/rules + Obsidian + gstack + impeccable   │
│  + superpowers (核心: TDD / verification / learning) │
└─────────────────────────────────────────────────────┘
                       ↓
            ┌──────────┴──────────┐
            ↓                     ↓
   ┌────────────────┐    ┌────────────────────┐
   │ 单人项目        │    │ 团队仓库            │
   │ 照旧            │    │ 装 trellis +        │
   │ 不装 trellis    │    │ 在该项目内关掉      │
   │                 │    │ superpowers 的      │
   │                 │    │ brainstorm/plans    │
   └────────────────┘    └────────────────────┘
```

**单人项目**：照旧。不装 trellis。superpowers + planning-with-files + Obsidian + gstack 已足够。

**团队仓库**（未来）：装 trellis，并：
- 在该项目内**关掉** superpowers 的 brainstorming / writing-plans / executing-plans skills（避免和 trellis 同名 skill 双重规训）
- **保留** superpowers 的 verification-before-completion / test-driven-development / receiving-code-review / requesting-code-review 等正交能力
- 把团队规范从 CLAUDE.md 切片成 `.trellis/spec/`
- 团队成员每人 `trellis init -u <name>`

## 何时回来重评

触发任一条即重新评估：

- [ ] 第一个真实团队仓库立项（届时直接走"团队仓库"分支）
- [ ] Trellis 发布 1.0 stable（当前 0.5.x beta，模板和 workflow 仍在变）
- [ ] 半年到（2026-11-04），看一次 changelog 和 GitHub stars/issues 趋势
- [ ] 出现一个新对手产品（多 agent 平台共享 spec 这个赛道）

## 沉淀产出

本次调研最大的收获不是 trellis 本身，是**它的方法论**——见 [[调研方法论]]。即使不装 trellis，它的 sub-agent 隔离 / 文件持久化 / research-first 三条原则可以**用现有工具落地**。

## 参考

- 上游 repo：https://github.com/mindfold-ai/trellis
- 官方文档：https://docs.trytrellis.app/
- 调研当天沙盒：`/tmp/trellis-test`（已清理）
- 装包命令：`npm i -g @mindfoldhq/trellis@beta`
