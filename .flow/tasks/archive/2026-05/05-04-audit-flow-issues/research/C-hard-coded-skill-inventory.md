# Research C — Hard-coded Skill 引用清单

## Summary (≤200 字)

总引用 31 处，分布：commands/flow/*.md 13 处、skills/flow/*/SKILL.md 13 处、hooks 0 处（hooks 只引用 flow 自身的 orchestrator skill 和 slash command 列表，不涉及外部 plugin namespace）、scripts 0 处、templates 0 处、docs 100+ 处（不在改动范围）。

按 plugin 分布 top 5：`gstack:` 12 处（codex review/consult/challenge + design-review）、`superpowers:` 8 处、`impeccable:` 7 处（shape/audit/polish/frontend-design）、`yangpeng-claude-skills:` 4 处（save）、`frontend-design:` 1 处。涉及 capability 约 11 个：brainstorm、ux_brief、cross_model_consult、cross_model_review、cross_model_challenge、tdd、worktree、parallel_dispatch、ui_implement、ui_audit、ui_visual_review、session_save、design_review。

改动估算：**S (半天到 1 天)**——只有 prompt-layer .md 文件，无 Python 反序列化/AST 改动；引用集中在 8 个文件里，单点替换。但要先在 config schema 里定下 capability 名 + 默认映射，再批量替换。

## 完整引用表（按 capability 分组）

### Capability: brainstorm（需求头脑风暴）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/start.md | 62 | "Brainstorm — Use" | `superpowers:brainstorming` | `capability:brainstorm` |
| claude/commands/flow/continue.md | 36 | "Invoke ... to keep filling prd.md" | `superpowers:brainstorming` | `capability:brainstorm` |
| claude/skills/flow/flow-phase1-plan/SKILL.md | 17 | "Step 1 — Brainstorm" | `superpowers:brainstorming` | `capability:brainstorm` |

### Capability: ux_brief（UI 设计简报）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/start.md | 63 | "(UI tasks) — Also invoke" | `impeccable:shape` | `capability:ux_brief` |
| claude/skills/flow/flow-phase1-plan/SKILL.md | 23 | "For UI tasks: also invoke" | `impeccable:shape` | `capability:ux_brief` |

### Capability: cross_model_consult（高反悔决策跨模型咨询）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/start.md | 66 | "(High-reversal-cost) ... consult mode" | `gstack:codex` (consult) | `capability:cross_model_consult` |
| claude/skills/flow/flow-phase1-plan/SKILL.md | 86 | "high reversal cost ... consult mode" | `gstack:codex` (consult) | `capability:cross_model_consult` |

### Capability: cross_model_review（diff 跨模型审查）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/finish.md | 21 | "If task qualifies: run" | `gstack:codex` review | `capability:cross_model_review` |
| claude/commands/flow/codex-review.md | 7 | "Manually trigger ... via" | gstack `/codex review` | `capability:cross_model_review` |
| claude/commands/flow/codex-review.md | 35 | "Use ... in review mode" | `gstack:codex` (review) | `capability:cross_model_review` |
| claude/skills/flow/flow-phase3-finish/SKILL.md | 53 | "If triggered: invoke" | `gstack:codex` (review) | `capability:cross_model_review` |

### Capability: cross_model_challenge（卡住时对抗审查）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/continue.md | 57 | "If stuck (same bug 3+ times)" | `gstack:codex` (challenge) | `capability:cross_model_challenge` |
| claude/skills/flow/flow-phase2-execute/SKILL.md | 111 | "Invoke ... GPT-5.5 attacks" | `gstack:codex` (challenge) | `capability:cross_model_challenge` |

### Capability: tdd（测试驱动开发）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/continue.md | 49 | "(write tests first)" | `superpowers:test-driven-development` | `capability:tdd` |
| claude/skills/flow/flow-phase2-execute/SKILL.md | 76 | "If project has test infra ..." | `superpowers:test-driven-development` | `capability:tdd` |

### Capability: worktree（隔离工作树）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/continue.md | 50 | "(when worktree needed)" | `superpowers:using-git-worktrees` | `capability:worktree` |

### Capability: parallel_dispatch（并行 sub-agent 编排）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/continue.md | 51 | "(when N≥2)" | `superpowers:dispatching-parallel-agents` | `capability:parallel_dispatch` |

### Capability: ui_implement（UI 实施 / 前端组件构建）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/continue.md | 52 | "(UI tasks)" | `impeccable:frontend-design` 或 `frontend-design:frontend-design` | `capability:ui_implement` |
| claude/skills/flow/flow-phase2-execute/SKILL.md | 72 | "sub-agent prompt should include" | `impeccable:frontend-design` | `capability:ui_implement` |

### Capability: ui_audit（UI 自审）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/finish.md | 22 | "If UI: ... + polish + ..." | `impeccable:audit` + `polish` | `capability:ui_audit`（含 polish 子步） |
| claude/skills/flow/flow-phase3-finish/SKILL.md | 55 | "For UI tasks: also invoke" | `impeccable:audit` + `impeccable:polish` | `capability:ui_audit` |

### Capability: ui_visual_review（UI 真浏览器视觉审计）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/finish.md | 22 | "If UI: ... + ..." | `gstack:design-review` | `capability:ui_visual_review` |
| claude/skills/flow/flow-phase3-finish/SKILL.md | 55 | "real browser visual audit" | `gstack:design-review` | `capability:ui_visual_review` |

### Capability: session_save（会话断点保存）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/commands/flow/continue.md | 77 | "Auto-save: invoke" | `yangpeng-claude-skills:save` | `capability:session_save` |
| claude/commands/flow/finish.md | 46 | "Invoke ... to write breakpoint" | `yangpeng-claude-skills:save` | `capability:session_save` |
| claude/commands/flow/pause.md | 37 | "Invoke ... skill" | `yangpeng-claude-skills:save` | `capability:session_save` |
| claude/skills/flow/flow-phase4-sediment/SKILL.md | 88 | "Invoke ... — write breakpoint" | `yangpeng-claude-skills:save` | `capability:session_save` |

### Capability: deploy_chain（部署任务默认 skill 链——仅在 orchestrator 表里出现）

| File | Line | 上下文片段 | 当前引用 | 建议抽象后 |
|------|------|-----------|---------|------------|
| claude/skills/flow/flow-orchestrator/SKILL.md | 40 | "deploy / ops" 行 | `gstack:ship + canary` | `capability:deploy + capability:canary` 或 task-type 默认链 yaml |

## 例外清单

### 文档（不改，重构后再统一刷新）

- `docs/编码框架.md`：35 处引用——这是用户阅读的设计文档，等 capability schema 定下来后整体重写一节"capability 与默认映射"。
- `docs/Skills-Phase映射.md`：60+ 处引用——本身就是 skill 列表，可保留作"默认映射来源"，加一节说明 capability 抽象。
- `docs/调研方法论.md`：5 处引用——同上。
- `docs/USAGE.md` line 132：1 处 `superpowers:brainstorming`、`impeccable:shape`——更新成 capability 名。

### 模板 / 配置

- `templates/flow.config.yaml.template:11` `default_skill_chain: backend`——这是字符串 key，**已经是抽象层**，不需改；但要扩展成 capability 映射 schema。
- `templates/flow.config.yaml.template:38` `model: gpt-5.5`、`templates/flow.config.local.yaml.template:23` `claude_default_model: claude-opus-4-7`、line 24 `codex_cli_path`——模型名 / CLI 路径在 config 里没问题，建议挪进 capability 实现细节字段。

### 系统命令（非 skill，但建议也走 capability 抽象层）

- `gstack:codex` 多处——背后是 `codex` CLI（`templates/flow.config.local.yaml.template:24` 的 `codex_cli_path`），如果用户没装 codex 应当 fallback。建议 capability 实现里支持 fallback 链。
- `git`（commit/diff/log/status/stash 等）——纯 git，不抽象。
- `/clear`（claude/skills/flow/flow-phase2-execute/SKILL.md:112）——Claude Code 内置命令，不抽象。

### 模型名 / agent 类型（半硬编码）

| File | Line | 引用 |
|------|------|------|
| claude/skills/flow/flow-phase1-plan/SKILL.md | 47-48 | `subagent_type: "general-purpose"`, `model: "sonnet"` |
| claude/skills/flow/flow-phase2-execute/SKILL.md | 52-53 | `subagent_type: "general-purpose"`, `model: "opus"` |
| claude/skills/flow/flow-phase2-execute/SKILL.md | 92-93 | `subagent_type: "general-purpose"`, `model: "sonnet"` |
| claude/skills/flow/flow-phase3-finish/SKILL.md | 18-19 | `subagent_type: "general-purpose"`, `model: "sonnet"` |
| claude/commands/flow/start.md | 64 | `general-purpose` sub-agents (model: sonnet) |
| claude/commands/flow/continue.md | 53 | `model: opus`, `subagent_type: general-purpose` |
| claude/commands/flow/continue.md | 61 | `Agent(subagent_type: "general-purpose", model: "sonnet")` |
| scripts/flow_triage.py | 8 | 注释 "Claude (Haiku model)" |

这些已经被 `.flow/config.yaml phases.*.model` 字段覆盖了一部分，但 prompt 里仍然硬编码——建议 prompt 改成"按 config.phases.<phase>.model 选择"，把模型名变成运行时变量。

## 重构改动量估算

### 高优先级（必改才能跑 v0.4）—— 8 个文件

```
claude/commands/flow/start.md         3 处
claude/commands/flow/continue.md      7 处
claude/commands/flow/finish.md        3 处
claude/commands/flow/pause.md         1 处
claude/commands/flow/codex-review.md  2 处
claude/skills/flow/flow-orchestrator/SKILL.md           1 处
claude/skills/flow/flow-phase1-plan/SKILL.md            3 处
claude/skills/flow/flow-phase2-execute/SKILL.md         3 处
claude/skills/flow/flow-phase3-finish/SKILL.md          2 处
claude/skills/flow/flow-phase4-sediment/SKILL.md        1 处
```

合计 26 处需要替换。文件数 10 个（不是 8——上面写错了，实际是 10）。

### 中优先级（不影响运行但建议同步）—— 1 个文件

- `templates/flow.config.yaml.template` 中扩展 capability 映射 section
- `.flow/config.yaml`（已存在）同步

### 低优先级（文档，最后批改）—— 4 个文件

- `docs/编码框架.md`
- `docs/Skills-Phase映射.md`
- `docs/调研方法论.md`
- `docs/USAGE.md`

### 估计工作量：**半天**

理由：
1. 引用全在 markdown 里，无需改 Python AST。
2. 替换模式高度规则（`<plugin>:<skill>` → `capability:<name>` + 在 SKILL/command 顶部加一句"resolve via .flow/config.yaml capabilities map"）。
3. capability 数量小（13 个），容易在一个 yaml block 里覆盖。
4. 主要风险是 **runtime resolution**：当用户 yaml 里没配某 capability 时，是 fallback 到原 skill 名还是报错？这是设计决策，不是代码工作量。

## 建议 capability schema 草稿

```yaml
# .flow/config.yaml — v0.4 新增 section
capabilities:
  # Phase 1
  brainstorm:
    default: superpowers:brainstorming
    fallback: []  # 用户找不到默认时 prompt 里只放 capability 名让模型自适应
  ux_brief:
    default: impeccable:shape
    skip_if_not_available: true  # UI 任务可选
  cross_model_consult:
    default: gstack:codex
    args: { mode: consult }
    requires_cli: codex
    skip_if_not_available: true
  
  # Phase 2
  tdd:
    default: superpowers:test-driven-development
    skip_if: { project_has_tests: false }
  worktree:
    default: superpowers:using-git-worktrees
  parallel_dispatch:
    default: superpowers:dispatching-parallel-agents
  ui_implement:
    default: impeccable:frontend-design
    fallback: [frontend-design:frontend-design]
  cross_model_challenge:
    default: gstack:codex
    args: { mode: challenge }
    requires_cli: codex
    skip_if_not_available: true
  
  # Phase 3
  cross_model_review:
    default: gstack:codex
    args: { mode: review }
    requires_cli: codex
  ui_audit:
    default: impeccable:audit
    follow_with: impeccable:polish
  ui_visual_review:
    default: gstack:design-review
    requires: chrome
    skip_if_not_available: true
  
  # Phase 4
  session_save:
    default: yangpeng-claude-skills:save
    fallback: [superpowers:checkpoint]  # 假设的 fallback
```

## 风险点 / 需要小心的文件

1. **`claude/commands/flow/continue.md`**——单文件 7 处引用，是 capability 密度最高的入口；改坏全流程报废，必须先用 grep 验证替换完整性。
2. **`claude/skills/flow/flow-phase3-finish/SKILL.md`**——同时涉及 cross_model_review、ui_audit、ui_visual_review 三个 capability 协同。注意 `capability:ui_audit` 是否要把 polish 拆成子 capability。
3. **模型名硬编码**（7 处 `model: "sonnet"/"opus"`）建议第二批改——和 capability 重构正交，但同样是"prompt 层硬编码反模式"，可纳入 v0.4 范围避免再迭代一次。
4. **`yangpeng-claude-skills:save`**——这个 plugin 是用户私有 namespace，跨用户分发 v0.4 时必然要 capability 化（其他用户没这个 plugin），这是 capability 抽象**最强的业务理由**。
