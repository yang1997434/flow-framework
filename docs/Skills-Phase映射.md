---
title: "Flow × Skills 映射全表"
date: 2026-05-04
type: reference
tags:
  - 工具
  - 框架
  - skills
  - 索引
status: active
---

# Flow 框架 × Skills 映射全表

> [[编码框架|Flow]] 的伴随文档——把当前安装的**所有 skills/plugins**按 Phase + 任务类型映射，回答"什么时候用什么"。
> 防止框架变"空头编排"。

## 总览

当前 skill 库（按提供方）：

| 提供方 | 数量 | 主用途 |
|--------|------|-------|
| `superpowers/*` | 16 | 流程纪律（TDD / 调研 / 计划 / 审查 / worktree）|
| `gstack/*` | ~45 | 工程流（QA / Browser / Ship / Review / 部署 / 运维）|
| `impeccable/*` | 21 | UI / 设计质量 |
| `document-skills/*` | 16 | 文档 / 设计 / 测试 / 模板 |
| `pr-review-toolkit/*` | 7 | 专项 review agent（6 视角）|
| `baoyu-skills/*` | 17 | 内容 / 图像 / 发布（多平台）|
| `obsidian/*` | 5 | vault 操作 / 内容抓取 |
| `actionbook/*` | 3 | 浏览器自动化 / 数据提取 |
| `planning-with-files/*` | 3 | 文件协议任务规划 |
| `yangpeng-claude-skills/*` | 3 | 个人定制（save / resume / maruiao-thinking）|
| `pr-review-toolkit/*`, `code-review/*`, `review-loop/*` | 多 | 各类 PR review |
| 其他独立 | 多 | karpathy-guidelines / claude-api / prompt-engineering / frontend-design |

---

## 🟢 一直挂着的 skills（任何 phase 都要遵守）

| Skill | 角色 |
|-------|------|
| `superpowers:using-superpowers` | 总纪律——所有 skill 优先识别使用 |
| `andrej-karpathy-skills:karpathy-guidelines` | 通用反 LLM 错误指南（surgical 改动 / 不过度复杂化）|
| `yangpeng-claude-skills:save` | 任何会话结束 / 上下文将满时写断点 |
| `yangpeng-claude-skills:resume` | 任何会话开始时恢复断点 |

---

## Phase 1 — Plan / Brainstorm

### 任务级 trigger 表

| 触发 | Skill | 用途 |
|------|-------|------|
| 任何 simple+ 任务 | `superpowers:brainstorming` | 一次问一个问题，落 prd.md |
| 个人创意 / 雏形 / 抛想法 | `yangpeng-claude-skills:maruiao-thinking` | 你的定制版头脑风暴 |
| 创业 / 产品策略 | `gstack:office-hours` | YC 6 forcing questions |
| 复杂系统设计——CEO 视角 | `gstack:plan-ceo-review` | 重新框定问题 / 找 10 星产品 |
| 复杂系统设计——Eng 视角 | `gstack:plan-eng-review` | 架构 / 数据流 / 边界情况 |
| 复杂系统设计——DX 视角 | `gstack:plan-devex-review` | 开发者体验 / 接入摩擦 |
| 复杂系统设计——Design 视角 | `gstack:plan-design-review` | 视觉 / 信息层级 / 微交互 |
| 想要全部视角自动跑 | `gstack:autoplan` | CEO + 设计 + Eng + DX 4 个 review 串跑 |
| **UI / 视觉任务**——简报 | `impeccable:shape` | UX 设计简报（写代码前） |
| **UI 任务**——首次建立设计语言 | `impeccable:teach-impeccable` | 一次性建立项目设计 context |
| **UI 任务**——完整设计探索 | `gstack:design-shotgun` | 多 AI 设计变体 + 反馈 + 迭代 |
| **UI 任务**——完整设计咨询 | `gstack:design-consultation` | 出 DESIGN.md 设计源 |
| 多步 / 长任务的规划 | `planning-with-files` | task_plan / findings / progress 文件协议 |
| 写实施计划 | `superpowers:writing-plans` | 计划写作纪律 |
| 高反悔成本决策 | `gstack:codex` (consult) | GPT-5.5 异源二次意见 |
| 任务涉及部署 | `gstack:setup-deploy` | 配置 deploy 信息（一次性）|

### Research（Phase 1 子阶段）

| 触发 | Skill | 用途 |
|------|-------|------|
| 深度研究产 HTML 报告 | `gstack:active-research` | 完整研究流水线 |
| 调研多 source、要 findings 落盘 | `planning-with-files` | findings.md 协议 |
| 抓结构化网页数据 | `actionbook:extract` | 生成 Playwright 脚本 |
| 抓 X / Twitter | `baoyu-skills:baoyu-danger-x-to-markdown` | 专门 X 适配器 |
| 抓 YouTube | `baoyu-skills:baoyu-youtube-transcript` | 字幕 / 章节 / 翻译 |
| 抓 HN / 通用网页 | `baoyu-skills:baoyu-url-to-markdown` | Chrome CDP + 适配器 |
| 抓普通博客 / 文档 | `obsidian:defuddle` | 轻量去杂乱 |
| 网站交互自动化 | `actionbook:actionbook` | 预验证 action manuals |
| 调研结果存 vault | `obsidian:obsidian-markdown` | wiki link / callouts / Obsidian 语法 |

---

## Phase 2 — Execute / Implementation

| 触发 | Skill | 用途 |
|------|-------|------|
| 任何代码改动（默认）| `andrej-karpathy-skills:karpathy-guidelines` | 反 LLM 常见错（不过度复杂 / surgical change）|
| 改动有 / 应有测试 | `superpowers:test-driven-development` | 先测试后实现 |
| 执行写好的实施计划 | `superpowers:executing-plans` | 计划执行纪律 |
| 派 N 个 sub-agent 并行 | `superpowers:subagent-driven-development` + `dispatching-parallel-agents` | sub-agent 编排 |
| 进入 worktree | `superpowers:using-git-worktrees` | worktree 创建 + 安全 |
| **UI / 组件 / 页面**——构建 | `impeccable:frontend-design` | 高质量、不平庸 UI |
| **UI**——艺术性 React | `frontend-design:frontend-design` | 同上不同包 |
| **UI**——P3 海报 / web artifact | `document-skills:web-artifacts-builder` | 多组件 React + shadcn |
| **UI**——SVG / 静态艺术 | `document-skills:canvas-design` | PNG / PDF 设计 |
| **UI**——主题套用 | `document-skills:theme-factory` | 10 预设主题 |
| **UI**——按 Anthropic 品牌 | `document-skills:brand-guidelines` | Anthropic 颜色字体 |
| **UI**——algorithmic art | `document-skills:algorithmic-art` | p5.js / 流场 / 粒子 |
| 卡住 debug | `superpowers:systematic-debugging` | 系统化 4 阶段 root cause |
| 卡住——更深入调研 | `gstack:investigate` | 完整调试流水线 |
| 卡 3+ 次 | `gstack:codex` (challenge) | GPT-5.5 攻击假设 + 后备 `/clear` |
| 调本地 web app | `document-skills:webapp-testing` | Playwright 本地调试 |
| 浏览器交互 / 真实测试 | `gstack:browse` | 公网网页 QA |
| 完整 QA 流程 | `gstack:qa` | 系统 QA + 自动 fix |
| 写 Claude SDK / API 应用 | `claude-api` | Anthropic SDK 缓存 / thinking / 工具 |
| 写 prompt | `prompt-engineering` | prompt 设计 |
| 写新 skill | `superpowers:writing-skills` | skill 创作纪律 |
| 写 skill 模板 | `document-skills:skill-creator` | 引导 skill 创建 |
| 建 MCP server | `document-skills:mcp-builder` | FastMCP / Python / TS |
| 调 Figma | `mcp__claude_ai_Figma__*` (MCP，非 skill) | 设计读取 / 写入 |

### 文档 / Office 类输出（Phase 2 当任务是文档时）

| 触发 | Skill |
|------|-------|
| 写 .docx | `document-skills:docx` |
| 写 .pptx | `document-skills:pptx` |
| 写 .xlsx | `document-skills:xlsx` |
| 写 PDF / 提取 PDF | `document-skills:pdf` |
| 写 Slack GIF | `document-skills:slack-gif-creator` |
| 写公司内部沟通 | `document-skills:internal-comms` |

---

## Phase 3 — Finish / Verify

### 通用 verify

| 触发 | Skill | 用途 |
|------|-------|------|
| 任何任务 final | `superpowers:verification-before-completion` | 跑验证不胡说 pass |
| 主动请审 | `superpowers:requesting-code-review` | 请审纪律 |
| 接收审查反馈 | `superpowers:receiving-code-review` | 反馈处理纪律 |

### 选哪个 reviewer

| 场景 | Skill |
|------|-------|
| 日常小 PR / 改动 | `code-review:code-review`（5 Sonnet 并行 + Haiku 置信度）|
| 大 PR / 多模块 | `pr-review-toolkit:review-pr`（6 专家 agent）|
| 长迭代 feature（实现→审查→修复→再审）| `review-loop:review-loop` |
| Pre-landing 合入前 diff（SQL/LLM/副作用）| `gstack:review` |
| 跨模型异源审 | `gstack:codex` (review，GPT-5.5)|
| 安全敏感 | `gstack:security-review` |
| 部署前最后检查 | `gstack:health` 综合质量分 |
| 性能基线 | `gstack:benchmark` |

### 专项 reviewer（pr-review-toolkit 子 agent）

按需单独触发：

| 关注点 | Agent |
|--------|-------|
| 风格 / 编码标准 | `pr-review-toolkit:code-reviewer` |
| 简化机会 | `pr-review-toolkit:code-simplifier` |
| 注释准确度 | `pr-review-toolkit:comment-analyzer` |
| 测试覆盖 | `pr-review-toolkit:pr-test-analyzer` |
| 沉默失败 / 错误处理 | `pr-review-toolkit:silent-failure-hunter` |
| 类型设计 | `pr-review-toolkit:type-design-analyzer` |

### UI 任务的 verify 子链

| 触发 | Skill | 用途 |
|------|-------|------|
| **UI 改动—自审** | `impeccable:audit` | 无障碍 / 性能 / 一致性 / 反 pattern |
| **UI 改动—终修** | `impeccable:polish` | 对齐 / 间距 / 一致性最终 |
| **UI 改动—UX 评估** | `impeccable:critique` | 视觉层级 / 信息架构 / 认知负荷 |
| **UI 改动—视觉实测** | `gstack:design-review` | 真浏览器视觉审计 + iteratively fix |
| **UI 文案—优化** | `impeccable:clarify` | UX copy / error messages |
| **UI—响应式** | `impeccable:adapt` | 多屏 / 多设备适配 |
| **UI—生产加固** | `impeccable:harden` | 错误状态 / 空状态 / i18n / 文本溢出 |
| **UI—动效** | `impeccable:animate` | 微交互 / 过渡 |
| **UI—配色** | `impeccable:colorize` |  |
| **UI—字体排版** | `impeccable:typeset` |  |
| **UI—去复杂** | `impeccable:distill` | 删元素 / 减噪 |
| **UI—增强** | `impeccable:bolder` | 太保守 → 加视觉冲击 |
| **UI—减弱** | `impeccable:quieter` | 太冲击 → 降强度 |
| **UI—惊喜** | `impeccable:delight` | 加微小惊喜 |
| **UI—极致打磨** | `impeccable:overdrive` | 60fps / 物理 / shader 类 |
| **UI—系统化** | `impeccable:extract` | 提可复用组件 + token |
| **UI—布局节奏** | `impeccable:layout` | 间距 / 视觉节奏 / 层级 |

### QA / 部署后

| 触发 | Skill | 用途 |
|------|-------|------|
| 部署后 QA 测试 | `gstack:qa` | 系统 QA + iteratively fix |
| 仅报告不修复 | `gstack:qa-only` | QA report only |
| 部署后金丝雀监控 | `gstack:canary` | 实时监控 + alert |
| 性能基线对比 | `gstack:benchmark` | Web Vitals + 资源 size |

---

## Phase 4 — Sediment

| 触发 | Skill | 用途 |
|------|-------|------|
| 任务结束 / 上下文将满 | `yangpeng-claude-skills:save` | 写 session 断点 |
| 周回顾 | `gstack:retro` | 全周提交 / 工作模式 / 质量趋势 |
| 项目 learning 沉淀 | `gstack:learn` | 跨 session 知识管理 |
| 发布版本 | `gstack:document-release` | 跨 README / ARCHITECTURE / CLAUDE.md 同步 |
| 写 changelog | `gstack:changelog-generator` | git 历史 → 用户友好 |
| 提炼新 skill | `superpowers:writing-skills` + `document-skills:skill-creator` | 沉淀新 skill |
| 完成分支 | `superpowers:finishing-a-development-branch` | 选 merge / PR / cleanup |
| Ship 流程 | `gstack:ship` | tests + diff + version + CHANGELOG + push + PR |
| Land + deploy | `gstack:land-and-deploy` | merge + deploy + canary |

---

## 横切（任何 phase 可触发）

### 安全护栏

| 触发 | Skill |
|------|-------|
| 操作 prod / 高风险命令前 | `gstack:careful` |
| 限定编辑目录 | `gstack:freeze` / `unfreeze` |
| 全栈安全（destructive + scope）| `gstack:guard` |
| 项目初始化 CLAUDE.md | `init`（built-in） |
| 安全审计 | `gstack:cso`（CSO mode）|
| 中途存断点 | `gstack:checkpoint` |

### 工具配置

| 触发 | Skill |
|------|-------|
| 加 Bash / MCP allowlist | `update-config`（hook / permission / env）|
| 调键位 | `keybindings-help` |
| 减权限弹窗 | `fewer-permission-prompts` |
| 状态行 | `statusline-setup` |
| 简化代码 | `simplify` |
| 提交评审 | `review` / `security-review`（built-in commands）|
| 查 Claude Code / SDK / API 用法 | `claude-code-guide`（agent）|

### Loop / Schedule（自动化）

| 触发 | Skill |
|------|-------|
| 周期任务 / 监控 | `loop` |
| 远程 routine | `schedule`（cron 远程 agent）|

---

## 任务类型 → 默认 skill 链

按任务类型决定 Phase 的 skill 默认装配：

### 后端 / API / CLI

```
Phase 1: superpowers:brainstorming → planning-with-files → ADR-lite
Phase 2: karpathy-guidelines + TDD + subagent-driven-development
Phase 3: code-review:code-review (小) 或 review-pr (大) + verification-before-completion
Phase 4: save + learn
```

### 前端 / UI / 视觉

```
Phase 1: brainstorming → impeccable:shape → 可选 design-consultation
Phase 2: impeccable:frontend-design / frontend-design + impeccable 子技能按需
Phase 3: impeccable:audit + polish + gstack:design-review
Phase 4: save + extract（提取设计 token + 复用组件）
```

### 数据 / 脚本

```
Phase 1: brainstorming + planning-with-files
Phase 2: karpathy-guidelines + 慎用 sub-agent（数据脚本通常单线）
Phase 3: verification-before-completion + 手动检查 sample
Phase 4: save
```

### 文档 / 内容

```
Phase 1: doc-coauthoring → planning-with-files
Phase 2: 按格式选 docx / pptx / xlsx / pdf / canvas-design / web-artifacts-builder
Phase 3: impeccable:clarify + 内部 review
Phase 4: 按目标平台选 baoyu-post-to-{x, weibo, wechat} 或 changelog-generator
```

### 部署 / 运维

```
Phase 1: setup-deploy（一次性）+ careful 护栏
Phase 2: ship 或 land-and-deploy
Phase 3: canary + benchmark + qa
Phase 4: retro + document-release
```

### 调研 / 学习

```
Phase 1: planning-with-files (plan-zh / plan)
Phase 2: active-research + obsidian:defuddle / actionbook:extract / baoyu-* 抓取
Phase 3: 写 vault 笔记 + obsidian-markdown
Phase 4: 加 vault MOC 索引
```

---

## 当前覆盖审计——v0.2 框架的 skill 集成是不是充分？

**充分覆盖的**：
- ✅ Phase 1 brainstorm（superpowers + maruiao-thinking）
- ✅ Phase 2 sub-agent 派发（superpowers + worktree）
- ✅ Phase 3 codex 异源审（gstack）
- ✅ Phase 4 save / resume

**v0.2 名义提了但没具体化**（需要在 phase 描述里显式 invoke）：
- ⚠️ verification-before-completion
- ⚠️ planning-with-files
- ⚠️ karpathy-guidelines
- ⚠️ 各种 review skill（code-review / review-pr / review-loop）

**v0.6.0 新整合的**：
- ✅ 19 个 capability 全部进 registry（详见 `docs/specs/2026-05-05-capability-registry-v0.6-design.md`）
- ✅ Phase 3 verify_completion 必触（关闭 "false done" 安全口子）
- ✅ Phase 1 hat-shifted brainstorming（Engineer / DX / Security 视角）
- ✅ Phase 3 code review 按 diff size 路由（small / large）
- ✅ Cross-cutting safety_guardrails + weekly_retro
- ✅ Deploy 任务 dev_setup + land_and_deploy + post_deploy_qa 全链
- ✅ Phase 4 changelog_gen + branch_finish

**v0.6.0 仍未整合（推 v0.7）**：
- ❌ 安全护栏 hook 自动触发（safety_guardrails 当前是文档触发；v0.7 加 Bash hook）
- ❌ release_docs（gstack:document-release）
- ❌ project_learnings（gstack:learn —— 等 /flow:promote 重叠问题厘清）
- ❌ security_audit / cso（等 task-type tagging）
- ❌ 文档输出类（docx / pptx / pdf —— 任务类型扩展时引入）
- ❌ 内容 / 发布类（baoyu-* —— 内容创作非 Flow 主线）

**结论**：v0.6.0 是"代码任务全链 + UI 任务覆盖 + 部署任务全链"。v0.7 关注 hook-based 自动化与跨项目 learning 沉淀。

---

## 推荐的写法（给 v0.2.1 用）

不需要每个 phase 都列 30 个 skill——那样过度。**用这个原则**：

1. **每个 Phase 的"基础 skill"放进框架文档**（5-10 个，必须知道）
2. **任务类型 dispatch 表也放进框架文档**（决定加哪条 skill 链）
3. **完整 trigger 表放本文档**（Skills-Phase映射.md），框架文档**只链过来**

这样 `编码框架.md` 不至于膨胀到 1000 行，又不丢全 skill 整合。

## 参考

- [[编码框架]]
- [[框架对比]]
- 全 skill 列表来源：当前 Claude Code session 的 system reminder
