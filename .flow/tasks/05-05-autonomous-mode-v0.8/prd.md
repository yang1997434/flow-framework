# v0.8 Autonomous Mode

> Created: 2026-05-05
> Slug: autonomous-mode-v0.8
> Type: backend (framework / orchestration)
> Complexity: complex

## Goal

让 `/flow:continue` 在 Phase 1 用户对齐之后，对一个 feature/PR 的 Phase 2/3/4 工作端到端自治执行。codex（跨模型）在关键节点当 gatekeeper；用户只在 spec/scope 变更、未预讨论的高反转成本岔路、不可逆外部动作三种 stop condition 上被叫回。目标：把"持续 work 直到目标实现"做出来，同时不牺牲安全与可审计性。

## What I already know

- 痛点来自 v0.7 开发周期：用户在每个 phase 边界基本是橡皮图章式确认。
- 现有 v0.7 基础：wave-dispatch（波内并行 subagent）、worktree skill、capability registry、`gstack:codex` consult/review/challenge。
- 硬约束：Claude 不能调 `/clear`；跨对话连续性靠文件；用户在 Linux CLI + Ghostty，常远程 SSH 进 server。
- 用户偏好：基于 SSH/CLI/headless，PushNotification（系统托盘）不可靠。
- Codex 第一轮 review 标 7 个 blocker + 19 个 silent-degeneration mode + missing controls 清单，已吸收进设计。
- Codex 第二轮 review 给 RED：原 5 ship 分期把"危险行为"排在"安全机制"之前；enforcement 大量是 advisory；subagent 自报 ≠ 审计；多文件状态有漂移风险。已据此重排 4 ship，并加 worktree-per-task 隔离 + orchestrator 派生事实模型。

## Design

### §1 Architecture

#### 1.1 Phase 1 末尾产出 Autonomy Contract

存放：`.flow/tasks/<slug>/contract.json`（**不**塞进 progress.md frontmatter — codex round 2 指出 nested schema 在 YAML frontmatter 会脆）。带 `contract_schema_version` 防版本漂移。

字段：

- `contract_schema_version`: int
- `autonomy_mode`: `auto | interactive`
- `created_at` + `staleness`:
  - `ttl_days` (default 7)
  - **触发式失效**：base branch moved / lockfile changed / prd.md edited / dependencies version changed / baseline tests fail
- `scope.allowed` / `scope.forbidden`: 文件 globs
- `known_forks`: `[{q, decision, rationale}]` —— 不在此列的 fork 自动 escalate
- `escalation_triggers`: 3 条 stop conditions 决策表，每条带 `examples_yes` + `examples_no`
- `irreversible_actions` enum: `push_main, release_tag, schema_migration, lockfile_major_change, public_docs_change, delete_local_work, overwrite_checkpoint, public_api_change`
- `budget`: `max_task_count, max_files_changed, max_new_deps, max_retry_per_task, max_elapsed_min`
- `acceptance_criteria`: `[{description, type ∈ {unit,integration,e2e,smoke,behavior,regression}, command}]`
- `notification.command`: Tier 3 opt-in shell（v0.8.0/0.8.1 schema 接受但运行时忽略；v0.8.2 起真正执行）
- `afk_timeout_min`: 超时分钟数
- `afk_on_timeout`: `abort | wait`（用户在 contract 里预先选超时后是安全 abort 还是继续等）

Per-task block（在 progress.md `### Tasks` 现有 schema 上扩展）：`risk_tier (low|med|high)` + `retry_budget` override。

Schema 解析失败 / 已知字段值无效 → **fail-closed** → 退回 interactive。未知字段（forward-compat）→ 接受 + 警告（不 fail-closed，避免老版本读 v0.8.X 写的 contract 直接挂）。

#### 1.2 Phase 2 orchestrator（v0.7 wave-dispatch 推广到 all-task）

**Worktree-per-task 隔离**（codex round 2 标的核心 enforcement primitive）：

- 每个 task subagent 在自己的干净 git worktree 里跑（扩展 `superpowers:using-git-worktrees`）
- Orchestrator 在合并回主分支前，检查 worktree 的 tracked + **untracked** diff
- Manifest 违规靠机械检查抓，不靠 prompt

**Orchestrator 拥有事实，subagent 拥有叙事：**

- Subagent 返回 = 仅叙事：`{decisions, rationale, alternatives, assumptions(advisory), failed_attempts(advisory)}`
- Orchestrator 从 worktree git diff + test logs + command logs **派生**事实：`{files_changed, files_touched_untracked, commands_run, retries, tests_run, tests_passed, tests_skipped, churn_signature}`
- Subagent 自报的事实字段**不可信**作为审计 trail

**Per-task 流程：**

1. Orchestrator 打 risk_tier，构 file ownership manifest（`scope.allowed` ∩ `task.writes` + 共享文件 denylist）
2. 起 worktree → 派 subagent
3. Subagent 工作；返回叙事 summary
4. Orchestrator 检查 worktree diff（tracked + untracked）
   - manifest 外的 file → escalate
   - scope 外的 untracked → escalate
5. Per-task codex review；fail → 进 retry whitelist 流程
6. **Retry whitelist** 双层：
   - Subagent prompt（advisory，主控）
   - 事后 semantic diff check（起步用简单启发：跳过的测试数 / 删除的测试文件 / 测试命令变更 / fixture 缩窄；复杂语义诊断留 v0.8.2+）
7. **Churn detection**：相同 (file, issue_id) 跨轮反复 → 在 N 用尽前 escalate
8. 用尽 retry 仍未解决 → escalate

每个 codex 多轮讨论 → 独立 subagent；consult prompt 强制带 adversarial framing 防共识盲点。User-sovereignty escape：若讨论收敛到违反 contract 的路径 → stop（不只看共识）。

#### 1.3 Phase 3 verify gate

按 `acceptance_criteria` 逐条跑：

- `unit` → 测试套件
- `integration` → 集成测试
- `e2e` → playwright / 真 CLI / 真 API（brainstorm 阶段决定方式）
- `behavior` → CLI 输出 / API 返回比对 spec
- `regression` → 现有 smoke 套件全过

**所有 criterion pass + codex review pass + 无 unresolved warnings** → 解锁 release-stop。任一 fail → escalate。

Phase 3 retry **比 Phase 2 严格**：仅允许 localized fix；behavior/e2e fail **必须** escalate（codex 强调 Phase 3 失败常暗示集成 / 设计错配）。

Phase 3 codex review 多审一维："**这真的还是用户在 Phase 1 要的那个 feature 吗**" —— 防 scope drift / 静默 reinterpretation。

**Baseline test policy：**
- Task 起跑前记录 baseline 测试 pass/fail
- 区分：**预存在 fail**（不动，作 warning carry-forward）vs **新引入 fail**（block）
- 跳过 / 不可执行的 acceptance command **必须 block**，不能降级为 warning

#### 1.4 Phase 4 sediment

仍自治，但**禁止擦除** unresolved warnings / skipped tests / deviations —— 必须显式 carry-forward 到 sediment 记录或 follow-up backlog。

#### 1.5 Persistent state layer

`.flow/tasks/<slug>/` 下文件：

- `contract.json` —— schema-versioned canonical contract
- `progress.md` —— 小 status pointer + tasks block（**不塞**完整 contract）
- `decisions.jsonl` —— append-only schema'd: `{id, ts, phase, task, decision, reason, alternatives, files_affected, review_status, supersedes}`
- `review-issues.jsonl` —— per-issue 显式 disposition: `{fixed, rejected_with_rationale, superseded, escalated}`
- `checkpoints/<ts>.md` —— atomic via temp-then-rename + git commit hash 锚定
- `blocked.md` —— transient，resume 时清掉

**Concurrency model**：仅 orchestrator 写规范 jsonl 日志；subagent 写自己 worktree 的 journal，orchestrator 合入时 derive。

**Conflict resolution**：`contract.json`（versioned, atomic）> `decisions.jsonl` tail > `checkpoint` > `progress.md` status。无效 frontmatter / corrupt log → fail-closed → interactive。

**Write atomicity**：所有规范写入用 temp-then-rename；每个 checkpoint 锚定一个 git commit hash 以便复现。

#### 1.6 Notification 协议（3 层）

**Tier 1（always）—— 单一事实源**：写 `blocked.md`：`{phase, task, why_blocked, required_choice, safe_resume_command, ts}`。100% 可靠永不丢，跟通道无关。

**Tier 2（always）—— 终端通道**：
- 主线程 stdout 输出 OSC 9 escape：`\033]9;Flow stopped on <slug>: <reason>\007` —— Ghostty/kitty/iTerm 自动转 desktop notification；通过 SSH 透传回本地终端
- 同时打 BEL（`\a`）—— 最低保底

**Tier 3（opt-in pluggable）—— 跨设备 push**：
- contract 里配 `notification.command: "<shell>"`
- Flow 把 blocked.md 内容通过 stdin 传给该命令
- 推荐 recipe（doc 提供，不内置）：ntfy.sh / Telegram bot / msmtp / notify-send / 自定义脚本

**正交关注点：**
- AFK timeout：`afk_timeout_min` 超时未响应 → 按 `afk_on_timeout` 字段执行 `abort` 或 `wait`
- 节流：同 task 同 issue 5 min 内不重复

#### 1.7 /clear + resume

仅在主线程真满（罕见，跨多 feature）或 stop-condition 暂停时需要。Resume 协议：

1. 读 `contract.json`（含 schema version 校验）
2. 读 latest checkpoint（含 git commit hash 校验）
3. 读 `review-issues.jsonl`（open dispositions）
4. 读 `decisions.jsonl` tail
5. 校验 git 状态 vs checkpoint 记录的 files_changed → mismatch → 警告 + 恢复提示
6. 跑触发式 staleness 检查
7. 续跑

### §2 Surface area

#### 2.1 `.flow/tasks/<slug>/` 新文件

- `contract.json`、`decisions.jsonl`、`review-issues.jsonl`、`checkpoints/<ts>.md`、`blocked.md`（transient）

#### 2.2 progress.md frontmatter（小 pointer）

```yaml
contract_path: contract.json
contract_schema_version: 1
autonomy_mode: auto | interactive
last_checkpoint: <ts>
```

#### 2.3 新 CLI

- `flow contract --validate <slug>` —— schema + 完整性
- `flow contract --init <slug>` —— Phase 1 末交互生成（从 prd.md 自动 infer 大部分；用户手填字段 ≤ 5）
- `flow blocked --list / --show <slug> / --resolve <slug> --choice <X>`
- `flow notify-test`
- `flow audit <slug>` —— 自治 run 结束输出"发生了什么"
- `flow doctor` 扩展：contract 完整性、decisions log 完整性、checkpoint 原子性、review-issue closure rate、untracked-file leak detection

#### 2.4 新 capability（`claude/capabilities/defaults.json`）

- `autonomy_orchestrator`
- `acceptance_verify`

#### 2.5 Skill 改造（不新增更多 skill）

- Phase 1 SKILL —— 末尾追加 contract builder 步骤
- Phase 2 SKILL —— 增 `autonomy.mode=auto` orchestrator 分支
- Phase 3 SKILL —— 增 acceptance_criteria gate
- wave-planner / wave-runner —— 支持 risk_tier + file ownership manifest
- `using-git-worktrees` —— 扩展 per-task disposable worktree 模式（或加 helper）

#### 2.6 Backward compat（v0.6/0.7 用户零回归）

- 缺 `contract.json` → 默认 interactive
- 缺 per-task `risk_tier` → 默认 med
- 缺 `acceptance_criteria` → Phase 3 退回原 test+codex review gate（无 E2E）
- 缺 `autonomy_orchestrator` capability → `/flow:continue` 走交互模式
- `contract_schema_version` 不匹配 → fail-closed（不自动迁移；要求用户介入）

### §3 Phasing（4 ship，安全 bundle）

#### v0.8.0 — Foundation，**autonomy execution 关闭**

- `contract.json` schema + parser + validator + fail-closed
- `contract_schema_version`
- `flow contract --validate / --init` CLI
- `decisions.jsonl` + `review-issues.jsonl` + `checkpoints/` schemas（仅写路径，orchestrator 还没 read 路径）
- **Dry-run** orchestrator：读 contract 打印 task plan + manifest，**不真派发**
- progress.md frontmatter pointer 字段
- Backward compat：缺 contract → interactive（v0.7 行为不变）
- **`autonomy_mode: auto` 字段会被接受但 orchestrator 拒绝执行**（提示需要 v0.8.1+）

After v0.8.0：能写 contract、能验证。**没有自治执行**。

#### v0.8.1 — 安全栈一捆（**首个真自治可发版**）

- worktree-per-task 隔离（扩展 `using-git-worktrees`）
- Orchestrator 派生事实（解析 git diff + test logs + command logs）
- Subagent 返回降级为 narrative-only
- `acceptance_runner` 含全部 5 种 type executors
- Phase 3 gate：所有 criterion pass
- `review-issues.jsonl` 含 disposition 跟踪
- Notification Tier 1（blocked.md）+ Tier 2（OSC 9 + BEL）
- AFK timeout 强制
- Budget enforcement：hit budget → block + 用户选项（extend / reduce / split / abort / interactive）。**禁止静默切模式**
- Baseline test policy
- 触发式 staleness 基础（base branch + lockfile）

After v0.8.1：autonomy 可发布。所有"开 auto 必须有"的安全机制同步上线。

#### v0.8.2 — 精修 + UX

- Retry whitelist + 简单 semantic diff（跳过/删除测试检测、fixture narrowing 检测）
- Churn detection
- `decisions.jsonl` read 路径（resume + Phase 3 用）
- Tier 3 notification + recipe doc + `flow notify-test`
- Nested-autonomy 禁用（subagent 不能再起子自治流）
- Abort-safely flow（用户拒绝路径时 revert/preserve partial）
- 最终 audit summary（`flow audit <slug>`）
- Review independence：codex 收完整 contract + full diff + test results + issue log（不只 summary）

#### v0.8.3 — 非功能 + 收尾

- Acceptance criterion type 扩展：security / performance / accessibility / compatibility / migration / cost / privacy / observability / docs / rollback
- Public API surface detector（exported types / CLI flags / config schema / file layout / behavior changes）
- Dependency side-effects detector
- Cost/time accounting source（model + tool + external API）
- 触发式 staleness 扩展（PRD edited / upstream dependency version changes / baseline tests fail）

## Acceptance Criteria

- [ ] **v0.8.0 ship**：contract.json schema + validator + dry-run orchestrator。E2E smoke：写 contract → validate → dry-run 打印预期 task plan
- [ ] **v0.8.1 ship**：完整安全栈。E2E smoke：contract 含 `auto` + `acceptance_criteria` → 自治 Phase 2/3 run 完成；budget hit 触发 blocked.md；manifest 违规 escalate
- [ ] **v0.8.2 ship**：retry/churn + Tier 3 + audit summary
- [ ] **v0.8.3 ship**：扩展 criterion types + 非功能 detectors
- [ ] 每个 release，所有 v0.6/0.7 plans 不变正常跑（regression suite）
- [ ] 每个 ship `flow doctor` 干净
- [ ] **codex round-3 GREEN 才允许 ship v0.8.1**（首个 autonomy-enabling release）；exit policy：连续 2 次 RED 后必须 re-scope（不无限迭代）
- [ ] 文档同步：每个 ship 在 DoD 里包含 README + CHANGELOG 更新；v0.8.2 加 recipes doc；v0.8.0 加 migration guide

## Definition of Done

(每 ship 适用)
- 测试加 / 改（unit + smoke + e2e where relevant）
- Lint / typecheck / CI 全绿
- 行为变更同步更新 docs/notes
- Credential grep self-check 通过
- Phase 4 sediment notes 填写

## Out of Scope (YAGNI)

(codex round 2 + 我的判断)

- 复杂 push 通知集成 —— 仅 hook + recipe，不集成具体服务
- Decisions log 形式化验证 —— jsonschema 校验够，不上 SAT
- 多 agent voting —— codex consult 多轮已够
- 跨文件语义冲突自动检测 —— subagent reviewer + worktree diff 兜底
- 富 TUI dashboard —— CLI + blocked.md 够用
- 跨任务长期记忆图谱 —— per-task 状态已够
- 内置具体 push 服务（ntfy/Telegram/Pushover）—— 留给用户配 `notify_command`
- per-severity / per-phase 不同 retry N —— per-task `retry_budget` override 在 v0.8.1 已支持；不再细分到 severity / phase 维度
- 单事件日志 + 派生视图重构 —— 短期"orchestrator-as-sole-writer"足够

## Open Risks

1. **Phase 1 contract builder UX 是命门** —— 契约难写 → 用户跳过 → 退 interactive → 自治模式形同虚设。Mitigation：builder 必须从 prd.md/research/ 自动 infer 大部分；手填字段 ≤ 5
2. **`acceptance_criteria` practicality** —— 写不出可执行检查的 feature 不能进 auto。这是设计意图，但限制了适用范围
3. **N=2 起步** —— empirical；至少 3 周 dogfood 后再调
4. **Worktree-per-task 开销** —— 磁盘 + 启动时间。dev 机器可接受；CI 可能要调
5. **Subagent narrative honesty 上限** —— 即使 orchestrator 派生事实，subagent 的 `assumptions` / `failed_attempts` 仍是自报。视为 advisory，不在它上 gate

## Codex Reviews（research record）

- **Round 1**（原始 7 条决策）：7 blockers + 19 silent-degeneration mode + missing-controls 清单。已吸收
- **Round 2**（修订版 §1+§2+§3）：**RED** —— 分期把"危险"排在"安全"前；enforcement 大量 advisory；subagent 自报不算审计；多文件状态有漂移。已据此重排 4 ship + 加 worktree-per-task + orchestrator 派生事实
- **Round 3**（计划，v0.8.1 ship 前）：GREEN-or-iterate gate 才能发首个 autonomy-enabling release

## Research References

- `.flow/tasks/05-05-autonomous-mode-v0.8/research/codex-round-1-summary.md`
- `.flow/tasks/05-05-autonomous-mode-v0.8/research/codex-round-2-summary.md`
- `.flow/tasks/archive/2026-05/05-04-audit-flow-issues/` —— capability registry pattern 起源
- `.flow/tasks/archive/2026-05/05-04-worktree-per-task/` —— per-task worktree pattern 前身
- `claude/capabilities/defaults.json` —— capability registry baseline
- `scripts/flow_wave_planner.py` —— 现 wave-dispatch planner（v0.8.1 推广到 all-task）
- `scripts/flow_wave_runner.py` —— 现 wave-dispatch runner（同上）
- Codex session ID: `019dfb47-c6da-7e61-9ad6-788af8856ca7`（可 resume 做 round 3）
