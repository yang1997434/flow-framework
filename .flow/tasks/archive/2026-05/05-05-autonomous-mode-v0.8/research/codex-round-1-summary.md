# Codex Round 1 — original 7 decisions review

> 2026-05-05  
> Codex session: `019dfb47-c6da-7e61-9ad6-788af8856ca7`  
> 输入 prompt: `/tmp/codex-flow-autonomy-prompt.txt`（保留供复现）

## Verdict

7 个 blocker + 19 个 silent-degeneration mode + missing-controls 清单。

## 7 Blockers

1. Stop-condition 分类太 fuzzy 且无审计 —— "spec 变更 vs 实现细节"边界含混（error handling / defaults / persistence / permissions / retries / retention / timeout / logging / CLI flags / 命名 / 兼容性 都可能"看起来是实现细节"实际是 spec）
2. 通知 / blocked-state 机制完全缺失 —— "autonomous mode without notification is silent stall mode"
3. Retry action 边界未定义 —— "fix codex review" 可变成任意 refactor / scope 扩大 / 删测试 / 弱化断言 / 改 reviewer prompt
4. Decisions log schema / 完整性未定义 —— freeform Markdown 会腐烂；缺 stable IDs / append-only / validation / conflict handling
5. /clear + resume 持久状态模型不完整 —— 缺单一事实源；progress.md vs decisions log 冲突时谁赢未定义
6. Shared-file 所有权保证未覆盖 all-task subagent dispatch —— v0.7 wave-dispatch SHARED_ARTIFACTS denylist 假设有界并行集，不是连续自治
7. Phase 3 retry 太宽松 —— Phase 3 失败常暗示集成 / 设计错配，不是小补丁

## 19 Silent-degeneration modes

- Agent 把所有事都归类成"实现细节"以避免停下
- Summary 变得乐观、省略不确定性
- Codex review 因 prompt 重复 + context 压缩而变浅
- Retry 产出"取悦 reviewer 但恶化设计"的 minimal patch
- Decisions log 变成无人能解析的 prose
- `autonomy: auto` 在 stale task 上变粘性
- Phase 3 修复在"实现完成后"引入新行为
- Shared file 累积来自无关 subagent 的不协调编辑
- 用户只在灾难时被打扰，scope 慢漂移没人看
- AFK 时停在 stop condition 上沉默卡死
- Resume 重建了不完整的世界观，从错假设继续
- "一个 feature/PR" 因没 budget 边界扩张成多 feature
- ...等

## Missing Controls 建议（已吸收）

- Autonomy contract（Phase 1 末尾产出）：allowed/forbidden scope / known forks / default choices / escalation triggers / irreversible actions / max budgets
- Risk tiers per task（dispatch 前打）
- File ownership manifests per task/wave
- Structured checkpoints（每 task 前后 / retry 前后 / Phase 3/4 前）
- Review issue tracking（用 ID 不用 reviewer 散文）
- Fail-closed parsing（autonomy 字段 + decisions log）
- Scope budget enforcement（changed files / new deps / public interfaces / migrations / 生成物 / task count / elapsed time）
- User preference capture（Phase 1：bias toward minimal change / 兼容 / no new deps / 性能 / UX / 架构风格）
- Staleness 处理（resumed `auto` tasks）

## Triage（codex 给的）

**Blockers**：上述 7 条
**Nice-to-fix**：autonomy 模式细化 / risk tiers / summary token 预算 / consult 强制 adversarial / `auto` 过期 / structured review issue IDs / retry churn 检测
**YAGNI**：复杂 push 通知集成 / 决策日志形式化验证 / 多 agent voting / 跨文件语义冲突自动检测 / 自治状态富 UI / 跨任务长期记忆图谱

## 处置

全部纳入设计 §1（架构）+ §2（surface）+ §3（YAGNI 显式列出）。详 prd.md。
