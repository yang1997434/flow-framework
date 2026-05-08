# Codex Round 2 — revised §1+§2+§3 review

> 2026-05-05（同一 session 续聊）  
> Codex session: `019dfb47-c6da-7e61-9ad6-788af8856ca7`  
> 输入 prompt: `/tmp/codex-flow-autonomy-prompt-v2.txt`  
> Verdict: **RED**

## RED 的核心论点

> "the final design direction is better, but the phased rollout ships autonomy before the safety mechanisms that make autonomy defensible."

具体：

- v0.8.0 的"裸跑窗口"（无 acceptance / retry / 通知 / budget / staleness）不可接受
- Enforcement 大量是 advisory（prompt + 事后 git-diff），不是机械护栏
- Subagent 自报"诚实记录"（assumptions / failed_attempts）≠ 审计 trail
- 5 个文件状态（progress / decisions / review-issues / checkpoint / blocked）会漂移；缺 concurrency model / atomic write / git baseline 锚定

## 7 Round-1 blocker 复审

| Round 1 blocker | Round 2 状态 |
|------------------|----------------|
| Stop-condition 模糊 | **partial** —— 决策表 + examples 帮忙但没消除歧义 |
| 通知缺失 | **mostly resolved** —— Tier 1/2/3 设计合理，但 OSC 9/BEL 是 best-effort |
| Retry 边界 | **partial** —— whitelist 好，但 prompt 弱；diff check 必须能检测 assertion 弱化 / 测试删除 / fixture 缩窄 |
| Decisions log schema | **partial** —— 缺 schema versioning / append-only 强制 / 损坏恢复 / 压缩 / 并发写锁 |
| /clear + resume | **partial** —— 层级好，但 progress.md 的"最高优先级"危险（它会过时） |
| Shared file ownership | **partial** —— manifest 好，但漏 semantic coupling / untracked / generated / package side effects / 全局缓存 |
| Phase 3 retry permissive | **mostly resolved** —— behavior/e2e fail must escalate 是对的；但要机械定义"localized fix" |

## NEW Blockers（round 2 新发现）

1. **v0.8.0 裸跑不可发** —— 自治执行没 acceptance/retry/通知/budget/staleness 不能 ship
2. **Enforcement 大部分是 advisory** —— prompt + post-hoc check 不是真护栏
3. **分期顺序错误** —— 通知 / fail-closed 验证 / acceptance / budget 是 safety primitive，不是 later enhancement
4. **Structured summary 是 self-attested evidence** —— 被审者自写记录 ≠ 审计
5. **Acceptance criteria types 缺非功能** —— 缺 security / performance / accessibility / compatibility / migration / cost / privacy / observability
6. **Budget hit 行为未定义** —— max elapsed/files/deps/tasks/retries 命中后必须 block + 用户选项，不能默默切模式
7. **File ownership 是反应式 + 不全** —— git diff 抓的是事后已跟踪文件；漏 install side effects / 生成物 / cache / 后台进程 / 网络调用 / untracked

## 主要 NEW 漏洞

- Contract 塞 frontmatter 太大 → 拆 `contract.json` 独立文件 + schema version
- 5 文件无 transactional / 无并发模型 / 无 atomic write / 无 git baseline
- 无 untracked-file policy / 无 dep side-effect policy / 无 network policy / 无 secrets policy / 无 data-loss boundary / 无 public API detector
- Staleness TTL 单维（时间）不够 —— 需触发式：base branch moved / lockfile changed / PRD changed / upstream API version / failing baseline tests
- 无 baseline test policy（区分预存在 fail vs 新引入 fail）
- 无 skipped-test 处理（必须 block，不能 warning）
- 无 review independence（codex 应收 contract + 完整 diff + test 结果 + issue log）
- 无 anti-gaming for summaries（事实必须 orchestrator 派生，不能 subagent 自报）
- 无 escalation UX（blocked.md 的用户选项需约束 + 安全默认）
- 无 abort safely flow / 无 nested autonomy 禁令 / 无 cost accounting / 无最终 audit summary / 无 contract version migration

## Specific Attacks

1. **Subagent honesty**：不可信。Orchestrator 必须从 git/test logs 派生 files_changed / commands_run / retries / churn / warnings。Subagent 的 `assumptions` 和 `failed_attempts` 仅 advisory
2. **Manifest enforcement**：用 sandbox / worktree 隔离。每 task 在干净 git worktree 跑；merge 前查 tracked + untracked diff。subagent 不直接动主 workspace
3. **Retry whitelist**：diff check 对测试要 semantic —— 标记改了的 assertion / snapshot / skipped tests / narrowed fixtures / deleted cases / changed acceptance commands / reduced coverage
4. **"Still the feature?" review**：高层 spec vs 低层 diff 是有损的。需 traceability：每 acceptance criterion 映射到 changed files/tests；每用户可见行为变化映射到 contract scope
5. **TTL=7**：默认可以，但不能是唯一守门。加 base branch movement / dep changes / contract edits / failing baseline / external API/library version 触发的失效
6. **Budget hit**：必须 block。写 blocked.md，通知，给选项（extend / reduce / split / abort / interactive）
7. **Acceptance types**：缺 security / performance / accessibility / compatibility / migration / cost / privacy / observability / docs / rollback

## 处置

1. **重排 5 ship → 4 ship**：v0.8.0 改成 schema + dry-run only（autonomy 执行关闭）；v0.8.1 = 完整安全栈一捆（首个真自治可发版）；v0.8.2 = 精修；v0.8.3 = 非功能扩展。
2. **加 worktree-per-task** 进 v0.8.1：作为 enforcement primitive。
3. **加 orchestrator 派生事实** 进 v0.8.1：subagent 返回降级为 narrative-only。
4. **拆 contract.json** 独立文件 + schema version：避免 frontmatter 脆。
5. **加 baseline test policy + 触发式 staleness + budget block + nested-autonomy 禁令** 进 v0.8.1。
6. **非功能 acceptance types** 留 v0.8.3。
7. **complex semantic test diff** 留 v0.8.2 起步用简单启发；并发写锁 / 单事件日志重构 留 YAGNI。

详 prd.md §3。
