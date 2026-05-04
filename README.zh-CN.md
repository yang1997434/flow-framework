[English](README.md) | [中文](README.zh-CN.md)

# Flow Framework

> **个人 AI 编码 harness——把 Claude Code + 你的 skill 生态编排成一个 4-phase 工作流，含自动记忆、sub-agent 隔离、跨模型审查。**

一个组合式框架——把你现有的 skill 生态（superpowers / impeccable / gstack / pr-review-toolkit / planning-with-files / Trellis 风格文件持久化）整合进 4-phase 工作流，加自动记忆 promotion 和踩坑捕获。

**状态**：v0.3.0-alpha。基础铺好；真实项目会暴露 gap，迭代到 v0.3.1。

## 解决什么

| 能力 | 怎么做 |
|------|-------|
| **Sub-agent 隔离** | Phase 2 派 `Agent(isolation: "worktree")`，主 session 整合 |
| **多线并行** | git worktrees + 每 worktree 一 sub-agent，scope 互不重叠 |
| **跨 session 记忆** | 3 层 promotion（`.flow/` → vault `patterns/` → `~/.claude/rules/`）+ auto-save hook |
| **跨模型审查** | Phase 3 关键改动调 `/codex review`（GPT-5.5）|
| **Token 路由** | Triage 用 Haiku，调研 Sonnet，实施 Opus |
| **踩坑库** | 独立 `pitfalls/` 树 + `trigger_paths` 自动加载 |
| **凭据安全** | vault 不存密码 + `~/.flow/credentials.local` + grep 自检 |
| **远程 SSH 友好** | 相对路径、无 GUI 依赖、machine-id 隔离 |

## 快速上手

```bash
# clone
git clone <this-repo> ~/projects/flow-framework
cd ~/projects/flow-framework

# 安装（symlink 到 ~/.claude/、~/.flow/ 等）
./install.sh

# 在任意项目内：
cd <your-project>
python3 ~/projects/flow-framework/scripts/flow_init.py
# 或安装后：flow init

# 起一个任务：
# (Claude Code 里)
/flow:start "<任务描述>"
```

## 4-Phase 工作流

```
[Triage] ─→ Phase 1 Plan ─→ Phase 2 Execute ─→ Phase 3 Finish ─→ Phase 4 Sediment
trivial             brainstorm    sub-agent      verify+codex    promote+save
└─→ skip              research      worktree       review          archive
                      ADR-lite      check          commit
```

完整设计见 [docs/编码框架.md](docs/编码框架.md)（也镜像到个人 vault）。

## Slash 命令

安装后在 Claude Code：

| 命令 | 作用 |
|-----|------|
| `/flow:start <task>` | Triage + 建 `.flow/tasks/<slug>/` + 跑 Phase 1 |
| `/flow:continue` | 推进当前任务到下一步 |
| `/flow:resume` | 从断点恢复 + 跑 staleness 检查 |
| `/flow:finish` | Phase 3 verify + Phase 4 sediment + auto-save |
| `/flow:pitfall <symptom>` | 捕获踩坑到项目或 vault |
| `/flow:promote <file> <tier>` | 手动 promote 知识 |
| `/flow:codex-review` | 手动触发跨模型审 |
| `/flow:pause` | 上下文切换前保存 |

## Hooks（安装后自动生效）

| Hook | 触发 | 作用 |
|------|------|------|
| `session-start.py` | session 启动 / clear / compact | 注入 Quick Read Guide + 当前任务 + 相关踩坑 |
| `user-prompt-submit.py` | 每轮用户消息 | 检测 "走 Flow"/"flow:" 关键词 → 路由 |
| `post-tool-bash.py` | git commit 后 | 跑凭据 grep 自检 |
| `stop.py` | session 结束 | 自动 save 当前任务进度到 journal |

## 仓库布局

```
flow-framework/
├── docs/             # 设计源（vault 镜像）
├── claude/           # 安装到 ~/.claude/
│   ├── commands/flow/
│   ├── skills/flow/
│   └── hooks/
├── scripts/          # Python 工具
├── templates/        # 模板（prd / progress / pitfall 等）
├── install.sh / uninstall.sh
└── VERSION
```

## 文档

- [`docs/编码框架.md`](docs/编码框架.md) — 完整设计
- [`docs/Skills-Phase映射.md`](docs/Skills-Phase映射.md) — Skill × Phase trigger 全表
- [`docs/框架对比.md`](docs/框架对比.md) — Flow vs Trellis / Cursor / Devin / CrewAI / Aider 横向对比
- [`docs/调研方法论.md`](docs/调研方法论.md) — 调研方法论

## License

MIT（本框架）。底层工具各自 license。
