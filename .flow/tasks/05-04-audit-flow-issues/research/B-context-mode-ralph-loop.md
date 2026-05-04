# Research B — context-mode + ralph-loop 兼容性验证

## Summary (硬约束 200 字)

**context-mode**: 可装但有坑 (P0 风险). License 是 Elastic 2.0 (源码可用但禁止托管商业服务), 不算纯 OSS, 但允许作为依赖宣告/文档引用. 已知 Issue #415: `pretooluse.mjs` 会因 matcher 级别误删 settings.json 中同 matcher 内的兄弟 hooks — 与 flow 现有 5 个 Python hooks 共存极可能触发. SQLite 默认存 `~/.context-mode/content/`, **不可配置** (与 flow 的 `.flow/.runtime/` 不冲突). PreCompact hook 在 Claude Code 中确实存在 (官方支持).

**ralph-loop (= ralph-wiggum)**: 可装. Anthropic 官方 verified plugin, 在 `anthropics/claude-code/plugins/ralph-wiggum/`. 命令 `/plugin install ralph-wiggum@claude-plugins-official`. 不读 PRD 文件 — 它把首条 prompt 反复重投, 通过 `--completion-promise` 字符串精确匹配 + `--max-iterations` 退出. 通过 Stop hook 在**当前 session 内**循环 (这意味着无法被 flow sub-agent 干净嵌套, 是 P0 风险).

---

## context-mode 详细验证

### 1. 安装路径

走 marketplace, 非 git clone. 命令是两步:
```
/plugin marketplace add mksglu/context-mode
/plugin install context-mode@context-mode
```
备选: `claude mcp add context-mode -- npx -y context-mode` (仅 MCP 模式, 无 hook).
install.sh 的 raw 文件 fetch 返回 404 ([https://github.com/mksglu/context-mode/blob/main/install.sh](https://github.com/mksglu/context-mode/blob/main/install.sh)) — 仓库可能没有显式 install.sh, 安装由 plugin manifest + npx 完成. **非交互可行**: 检测到 Node ≥22.13 自动用 `node:sqlite`, 否则源码编译; OpenClaw 集成支持 `npm run install:openclaw [path]` 程序化安装.

### 2. License

**Elastic License 2.0 (源码可用, 非 OSI 认证 OSS)**. 允许使用、fork、修改、分发, 但禁止 (a) 作为托管/管理服务对外提供 (b) 移除 license 声明. 作为 flow 的依赖宣告 + 文档引用 OK; 但如果 flow 自身是商业 SaaS 转售并把 context-mode 包成黑箱, 会触碰条款. 来源: [GitHub README](https://github.com/mksglu/context-mode).

### 3. Hook 命名空间冲突

**有 P0 冲突风险**, 已在上游被报告:

- **Issue #415**: `pretooluse.mjs over-deletes settings.json: drops sibling user hooks when matcher contains context-mode hook` ([https://github.com/mksglu/context-mode/issues/415](https://github.com/mksglu/context-mode/issues/415)). 当 context-mode 的 cache-heal hook 与用户自定义 hook 处于同一 matcher entry 时, 整个 matcher entry 在某次 PreToolUse 触发时被整段删除.
- **Issue #164**: `start.mjs writes CLAUDE.md even for hook-capable platforms (Claude Code)` — 重复注入 routing 规则到 CLAUDE.md, 污染 git tree ([Issue #164](https://github.com/mksglu/context-mode/issues/164)).

flow 现有 5 个 hook (stop.py / session-start.py / user-prompt-submit.py / pre-tool-task.py / post-tool-bash.py) — 其中 PreToolUse 的 matcher 如果与 context-mode 的 sandbox matcher (`run_shell_command|read_file|read_many_files|grep_search|search_file_content|web_fetch`) 重叠, 就会触发 #415. 没有显式 hook 优先级配置 — Claude Code hooks 是按 settings.json 顺序串行执行.

**Workaround (来自 #415 讨论)**: `chown root:wheel + chmod 644` 锁 settings.json — 不优雅. 正解是 flow 的 hooks 用独立 matcher entry, 不与 context-mode 共享 entry.

### 4. SQLite db 位置

默认 `~/.context-mode/content/`, **官方文档明确说不可用户配置**, 14 天自动清理. 与 flow 的 `.flow/.runtime/` (项目内 per-task) 物理隔离 — 不冲突. 但意味着 flow 不能"接管"它的存储层, 只能并存. 来源: [Context Mode 官方说明](https://github.com/mksglu/context-mode).

### 5. ctx 命令脚本化

**部分可行**:
- `ctx stats` / `ctx doctor` / `ctx upgrade` / `ctx purge` — 是 in-session MCP tool, **不是独立 CLI**, 必须在 Claude Code session 内由 LLM 调用. 不能被 shell 脚本直接调用.
- `context-mode doctor` / `context-mode upgrade` / `context-mode insight` — 是独立 CLI, 可脚本化.
- `ctx insight` 会启动浏览器 UI — 在 headless / CI 环境会卡住.
- `scripts/ctx-debug.sh` 用于诊断, 输出报告.

**结论**: flow 的自动化诊断脚本要用 `context-mode <cmd>` 而非 `ctx <cmd>`.

### 6. PreCompact hook 真的存在吗?

**确认存在于 Claude Code 官方支持**. 来源: [Hooks reference](https://code.claude.com/docs/en/hooks). 接收 `trigger` (manual/auto) + `custom_instructions`. 支持 `{"decision":"block"}` 或 exit code 2 阻断. 配置示例:
```json
{ "hooks": { "PreCompact": [ { "hooks": [ {"type":"command","command":"...","async":true} ] } ] } }
```
context-mode 用它做 snapshot 前置. flow 也想用, 应**避免与 context-mode 在同一 matcher entry 内** — 用独立 entry 即可共存.

### 7. 冲突历史

除了上述 #164, #415, 还有 Claude Code 上游 [Issue #34391](https://github.com/anthropics/claude-code/issues/34391) 跟踪 Context Mode 的整合问题. 多平台兼容性差异 (Cursor SessionStart 被 validator 拒绝, Antigravity/Zed 无 hook 支持) — Claude Code 是它支持最完善的平台之一, 但仍有上述两个高严重度 bug.

---

## ralph-loop 详细验证

### 1. 是 Anthropic 官方维护吗?

**是**. 官方 plugin 名叫 **ralph-wiggum** (页面用 "Ralph Loop" 作展示名), 仓库路径 `anthropics/claude-code/plugins/ralph-wiggum/`. claude.com 页面标 "Anthropic Verified" / "Made by Anthropic" ([Ralph Loop 插件页](https://claude.com/plugins/ralph-loop)).

`snarktank/ralph` 是**第三方独立项目**, 由 Ryan Carson 基于 Geoffrey Huntley 的 Ralph pattern 实现, MIT license — 与官方 plugin 不是同一物.
`coleam00/ralph-loop-quickstart` 是**第三方教程仓库**, 提供 bash 脚本 (`ralph.sh`) 实现, 文档明确建议**用 bash loop 而非 Anthropic plugin** 以获得更好的 context isolation — 这是反向信号: **官方 plugin 在 context 隔离上不够干净**.

### 2. 安装路径

```
/plugin marketplace add anthropics/claude-code
/plugin install ralph-wiggum@claude-plugins-official
```
非交互可完成 (plugin install 不需要交互式 prompt).

### 3. PRD 输入格式

**不读 PRD 文件**. 这是关键差异: 官方 ralph-loop 把"首条 prompt"作为唯一输入, 每轮 iteration 重新投递同一 prompt, 依靠**文件系统状态 + git history** 在 iterations 之间携带进度. 不读 `prd.md` / `prd.json` / `progress.txt`.

`snarktank/ralph` (第三方) 才读 `prd.md` / `prd.json` (含 `passes: true` 字段) / `progress.txt`. flow 当前用 `prd.md` (markdown) — **与官方 plugin 不直接兼容**, flow 需要把 PRD 内容作为 prompt 文本注入, 或自己在外层 wrapper 把 PRD 转换为 prompt.

### 4. 完成检测

**`--completion-promise` 精确字符串匹配**. Claude 必须输出与该字符串完全相等的内容才会退出. 多个来源 (paddo.dev, 官方 README) 都警告: 这个机制**脆弱不可靠**, 必须把 `--max-iterations` 当作实际兜底. 不支持 checkbox / passes:true 这种结构化检测.

### 5. 失败行为

**没有显式失败处理**. 失败被当作下一轮的输入数据, 循环不中断. 唯一退出条件: (a) completion-promise 命中 (b) max-iterations 达到 (c) 用户手动 `/cancel-ralph`. 多个来源警告"没设 max-iterations 等于醒来一笔 $500 API 账单".

### 6. 能否被 flow 嵌套调用?

**P0 风险点 — 不干净**. 关键证据: 官方 README 说 "Ralph creates a self-referential feedback loop **inside your current session** using a Stop hook". 这意味着:

- ralph-loop 注册 Stop hook 拦截 session 结束, 反复重投 prompt
- flow 当前也有 stop.py hook
- **两个 Stop hook 会冲突** (P0)
- flow 主 session 派 sub-agent → sub-agent 内调 `/ralph-loop`, sub-agent 的 session 终止条件被 ralph 接管, flow 主 session 拿不到 sub-agent 的"结束"信号

`coleam00/ralph-loop-quickstart` 的指南**明确推荐用 bash 脚本 `ralph.sh` 而非这个官方 plugin** 来获得 fresh context window per iteration — 这是社区共识: 嵌套场景应该用 bash loop, 而非 plugin.

**集成结论**: flow 在 Phase 2 提供 ralph 模式时, 应该:
- 选项 A: 不依赖官方 plugin, 自己实现 bash loop (类似 `coleam00/ralph-loop-quickstart`)
- 选项 B: 仅在 flow 主 session **不再注册 Stop hook** 时使用官方 plugin, 且仅在主 session, 不能在 sub-agent 内嵌套

### 7. License

- 官方 ralph-wiggum: 跟随 `anthropics/claude-code` 仓库 license (MIT, Anthropic 官方源码 license). 没有在 plugin README 内单独声明.
- 第三方 snarktank/ralph: MIT.
- 第三方 coleam00/ralph-loop-quickstart: 仓库未显式声明 license, **未找到, 待人工实测**.

---

## 集成方案落地建议

### install.sh 应写

```bash
#!/usr/bin/env bash
set -euo pipefail

# 1. 注册 marketplace (幂等)
claude plugin marketplace add mksglu/context-mode || true
claude plugin marketplace add anthropics/claude-code || true

# 2. 安装 context-mode (硬依赖)
claude plugin install context-mode@context-mode

# 3. 安装 ralph-wiggum (可选 — 由 flow.config.yaml 的 phase2.executor 决定)
if [ "${FLOW_INSTALL_RALPH:-0}" = "1" ]; then
  claude plugin install ralph-wiggum@claude-plugins-official
fi

# 4. 验证 (非交互)
context-mode doctor || echo "WARN: context-mode doctor failed, please run interactively"

# 5. 提示用户独立配置 settings.json hooks (避免 #415 冲突)
echo "IMPORTANT: flow's hooks must be in a SEPARATE matcher entry from context-mode hooks."
echo "See docs/integration.md for the safe settings.json layout."
```

### flow.config.yaml 应加字段

```yaml
dependencies:
  context-mode:
    version: "^1.0.106"
    license: "Elastic-2.0"
    install_via: "claude-plugin"
    sqlite_path: "~/.context-mode/content/"  # documented, not configurable
    flow_hook_isolation: "separate-matcher-entry"  # mandatory — see issue 415

phase2:
  executors:
    interactive: { default: true }
    parallel-subagents: {}
    ralph:
      backend: "bash-loop"        # 或 "anthropic-plugin"
      bash_loop_script: ".flow/scripts/ralph.sh"  # 推荐, 避开 Stop hook 冲突
      anthropic_plugin:
        completion_promise: "FLOW_PHASE2_DONE"
        max_iterations: 20
        warning: "do not nest inside sub-agents — Stop hook conflict"
      prd_to_prompt_adapter: "scripts/prd-md-to-prompt.sh"  # 把 prd.md 转 prompt
```

### 运行时建议

- flow 的 5 个 hooks 必须放在独立 matcher entry, **绝不**与 `context-mode-cache-heal` 共用 matcher.
- flow 的 PreToolUse matcher 避开 context-mode 的 sandbox matcher 列表; 或用更严格的 matcher pattern.
- `.flow/.runtime/` 不与 `~/.context-mode/content/` 冲突 — 可并存. 如果未来 flow 想消费 context-mode 的 SQLite, 用 read-only 模式打开, 不要写入.

---

## 风险 / 已知坑

### P0
1. **context-mode Issue #415**: 在与 flow 的 PreTool/PostTool/Stop/SessionStart hooks 同 matcher entry 时会误删 — flow 必须用独立 matcher entry.
2. **ralph-loop 用 Stop hook in-session 循环**: 与 flow 的 stop.py 冲突. flow 必须二选一: 要么自己别注册 Stop hook (改用 PostToolUse + 状态机), 要么用 bash loop 实现 ralph 模式而非用 plugin.
3. **ralph-loop 不能嵌套到 sub-agent 内** — 用 plugin 形态时 sub-agent session 结束信号被 ralph 接管, flow 拿不到完成回调.

### P1
4. **context-mode SQLite 不可配置** — flow 不能"接管"持久化层, 只能并存. 用户卸载 flow 时 `~/.context-mode/content/` 不会被清理 (它是 context-mode 自己的家).
5. **ralph-loop completion-promise 字符串匹配脆弱** — 必须强制 `--max-iterations`, 否则可能跑空轮直到达到 token / 费用上限.
6. **官方 ralph-wiggum 的 PRD 输入实际上是 prompt 文本**, 不读 prd.md — flow 需要 adapter 把 prd.md 转成 prompt 字符串.
7. **context-mode Issue #164**: start.mjs 在 Claude Code 上误写 CLAUDE.md, 污染 git tree — 升级到含修复的版本 (≥1.0.106 之后), 或初始化时 git ignore.

### P2
8. **Elastic 2.0 不是 OSI 认证 OSS** — flow 文档需要明确"context-mode 是源码可用依赖, 非 OSS", 避免下游 (尤其是商业 SaaS 用户) 误用.
9. **`ctx insight` 启动浏览器** — 在 CI/headless 环境不能调用, 用 `ctx stats` / `context-mode doctor` 替代.
10. **coleam00/ralph-loop-quickstart 无 license 声明** — 如果 flow 想直接借用其 ralph.sh 脚本, 需联系作者澄清, 或自己重写一份.

---

## 来源

- [context-mode README (mksglu/context-mode)](https://github.com/mksglu/context-mode)
- [context-mode Issue #415 — pretooluse.mjs over-deletes settings.json](https://github.com/mksglu/context-mode/issues/415)
- [context-mode Issue #164 — start.mjs writes CLAUDE.md](https://github.com/mksglu/context-mode/issues/164)
- [context-mode platform-support.md](https://github.com/mksglu/context-mode/blob/main/docs/platform-support.md)
- [Claude Code 上游 Issue #34391 — Context Mode integration](https://github.com/anthropics/claude-code/issues/34391)
- [Mert 博客 — Stop Burning Your Context Window](https://mksg.lu/blog/context-mode)
- [Claude Code Hooks reference (PreCompact 官方文档)](https://code.claude.com/docs/en/hooks)
- [Claude Code Hooks: Complete Guide to All 12 Lifecycle Events](https://claudefa.st/blog/tools/hooks/hooks-guide)
- [Ralph Loop plugin 页 (Anthropic verified)](https://claude.com/plugins/ralph-loop)
- [anthropics/claude-code plugins/ralph-wiggum/README.md](https://github.com/anthropics/claude-code/blob/main/plugins/ralph-wiggum/README.md)
- [snarktank/ralph (第三方)](https://github.com/snarktank/ralph)
- [coleam00/ralph-loop-quickstart (第三方教程)](https://github.com/coleam00/ralph-loop-quickstart)
- [paddo.dev — Ralph Wiggum: Autonomous Loops for Claude Code](https://paddo.dev/blog/ralph-wiggum-autonomous-loops/)
- [Ralph Wiggum technique — atcyrus](https://www.atcyrus.com/stories/ralph-wiggum-technique-claude-code-autonomous-loops)
