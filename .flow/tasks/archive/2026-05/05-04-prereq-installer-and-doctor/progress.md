# progress.md — prereq-installer-and-doctor

## Plan

(single, main session implements) — 子项目 #1 of v0.4 roadmap，来源 `05-04-audit-flow-issues/prd.md`。
按 audit 期间敲定的全自动方案：发现 `claude` CLI 自带非交互的 `plugin marketplace add` / `plugin install`，因此真·全自动可达。

## Execute Log

| 时间 (YYYY-MM-DD HH:MM) | Agent | Scope | Outcome |
|------|-------|-------|---------|
| 2026-05-04 | main | 写 `dependencies.json` | 4 marketplaces / 2 required + 2 optional plugins / 4 required + 3 optional system commands |
| 2026-05-04 | main | 写 `scripts/flow_install.py` | 5 子命令：check-system / register-marketplaces / install-plugins / install-hooks / all；--dry-run 支持；幂等去重 |
| 2026-05-04 | main | 写 `scripts/flow_doctor.py` | 4 段诊断：system / plugins / hook isolation (Issue #415) / user overrides；exit code 0/1/2 |
| 2026-05-04 | main | `claude/hooks/settings.template.json` 替代旧 snippet | 5 hook event，每个 own matcher entry；`{{REPO_ROOT}}` 占位 |
| 2026-05-04 | main | 重写 `install.sh` | 瘦身为 orchestrator，调 flow_install.py；--dry-run / --skip-plugins / --skip-hooks 三个 flag |
| 2026-05-04 | main | `scripts/flow.py` 加 install / doctor 路由 | + 2 subcmd |
| 2026-05-04 | main | 写 `tests/smoke/test_install_logic.py` | 13 cases：deps schema + template render + merge_hooks 4 case + import sanity |
| 2026-05-04 | main | 更新 README.md + docs/USAGE.md | 反映 v0.4 自动安装；删除"hooks 是 opt-in"的过时段落 |
| 2026-05-04 | main | **新增** scripts/flow_selftest.py | 用户提需求"装完检查是否真可用"；5 类功能性验证：hook dry-fire / flow init / task 轮转 / claude plugin list / doctor 委托 |
| 2026-05-04 | main | install.sh 末尾自动跑 selftest | fail-loud：selftest 失败则 install 退出 2 |
| 2026-05-04 | main | 实地装一次 hook 验证完整链路 | 7 个 flow hook 全部独立 matcher entry，doctor 检测全 ✓，selftest 全 ✓ |

## Verify Report

- ✅ Smoke 测试全集：23/23 通过（含 #0 的 10 + #1 的 13）
- ✅ `install.sh --dry-run` 输出符合预期：marketplace 4 个、plugin 4 个、hook merge、symlink、shim
- ✅ `flow doctor` 跑通：系统命令全 ✓、4 个 plugin 全部 ✓、hook 区段如实报告 "no flow hooks found"（dry-run 后未真装）
- ✅ `flow help` 显示 install / doctor 已接入主 CLI
- ✅ Hook 模板渲染后是合法 JSON，所有 7 个命令都不再有 `{{REPO_ROOT}}` 残留
- ✅ `merge_hooks` 单测覆盖：空 settings / 重复装 / 用户已有 hook / 其它 settings key 保留
- ✅ Issue #415 mitigation：每个 flow hook 都在独立 matcher entry（template + doctor 双重保证）
- ✅ Credential grep self-check pass（4 个 source + 1 json + 1 template + 2 docs，无 inline secret）
- ✅ **实地装一遍 hook 验证完成**：备份 `settings.json.flow-bak.20260504-055250` 已落盘；merge 后 PreToolUse / PostToolUse 各有 user + flow 各 1 entry 共存（matcher 不同 = 物理隔离）；SessionStart / UserPromptSubmit / Stop 各为 flow 独占 entry。doctor 报告全 ✓
- ✅ **selftest 实跑**：5 类全过 —— hooks dry-fire 全 OK（session-start.py 输出 865 字节合法 JSON，其它 noop exit 0）/ flow init 在 tempdir 产出 7 dirs / task create+archive 轮转含 P0-2 fix 验证 / claude plugin list 显示 superpowers + context-mode 已装 / doctor 报 all checks passed
- ⚠️ `dependencies.json` 当前只列 4 个 marketplace；用户的私有 plugin（如 gstack / yangpeng-claude-skills）未列入 —— 留给用户自行 append

## Sediment Notes

**Pattern**: "声明式 manifest + Python orchestrator + 薄 bash 入口" 这个三段结构未来可复用到其它"装系统依赖"场景（pre-commit hooks、CI 工具集等）。考虑 v0.4 完成后 promote 到 vault `patterns/`。

**Pitfall**: `claude` CLI 的 `plugin marketplace add` 与 `plugin install` 是非交互可脚本化的 —— 这跟 research B 报告的 "ctx 是 in-session MCP tool 不能脚本化" 不一样。容易混淆。建议捕一条 pitfall：**`claude plugin *` 子命令族 vs `/plugin install` 斜杠命令族 vs 各 plugin 自己的 in-session MCP tool**，三者权限和能力都不同。

**ADR**: 无新 ADR（在 audit 任务的 ADR 框架下落地，未引入新决策）。

## Retro

- ✅ Worked: 先 grep `claude --help` 才能发现 plugin 子命令存在 —— **没事就翻一翻 CLI help**
- ✅ Worked: `merge_hooks` 抽成纯函数后单测可写（同 #0 的 frontmatter 抽函数模式）
- ✅ Worked: `--dry-run` 全程支持，避免污染本地环境
- ❌ Didn't: 一开始想自己写 marketplace.json 解析逻辑去自动 install plugin，差点重复造轮子
- 💡 框架反馈：v0.3.1 版本的 README 说 "hooks NOT auto-installed (modifies settings.json)" —— 这是个老旧设计取舍，v0.4 应该全文搜索这类"opt-in / 手动" 措辞，统一改成 "auto-installed with backup"
