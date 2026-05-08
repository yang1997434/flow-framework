# v0.8.2.1 patch: rc=2→rc=5 + Flow exit-code constants module

> Created: 2026-05-08
> Slug: v0.8.2.1-rc-park-fix
> Type: backend
> Complexity: moderate

## Goal

修复 v0.8.2 引入的 exit-code 语义冲突：`_cmd_auto_execute` 在 AFK
idle park 时返回 `rc=2`，与 Flow 内部 6 个 CLI（flow_doctor /
flow_promote / flow_capability / flow_autosave / flow.py /
flow_ralph.sh）历史上 `rc=2 = argparse/usage error` 的肌肉记忆冲突。
按 codex post-ship 反思的 Flow 全局 exit-code registry
（0 success / 1 generic / 2 usage / 3 blocked / 4 nested abort /
**5 recoverable parked**），把 AFK park 路径迁到 `rc=5`，并新建
`scripts/common/exit_codes.py` 常量模块作为后续 single source of truth。

走 patch release（tag `v0.8.2.1`），**不** force-move `v0.8.2`。

## What I already know

经 codex round-1 plan-pass 校正，rc=2 = AFK park 全部 site（path-aware）：

**A. 源码 + docstring（`scripts/flow_orchestrator.py`）**：
1. **L4949-4956** — `_run_retry_loop` docstring：currently 说 `afk_idle_park` 是
   "RECOVERABLE — no snapshot, no notifier, **rc=0**"。**v0.8.2 T6.2 真 bug**：
   T6.2 把 rc 改成 2 但 docstring 没跟上。本 patch 必须改成 `rc=5`。
2. **L4980-4986** — `_run_retry_loop` 内 inline 注释：
   "RECOVERABLE: no snapshot, **rc=2** [translated by `_phase2_dispatch`]"
3. **L5226-5234** — `_phase2_dispatch` docstring "Return codes" 列表里
   `2 = Phase 2 PARKED`
4. **L5354-5362** — `_phase2_dispatch` 函数体 `if outcome == "afk_idle_park":
   return 2` + 上方 8 行注释 "rc=2 (DISTINCT from rc=0 pass)"
5. **L5418-5435** — `_cmd_auto_execute` docstring "Exit codes" 列表里
   `2 = AFK idle park`
6. **L5571-5582** — `_cmd_auto_execute` 函数体 `if rc == 2: ... return 2`

**B. 操作员文档**：
7. `claude/skills/flow/flow-phase2-execute/SKILL.md` **L168, L171-178** —
   "rc=2 is recoverable park" 叙述句 + "Exit codes" 表

**C. 测试硬钉点（`tests/smoke/test_phase2_retry_loop.py`）—— 必须**全量迁移**，
不是补充：**
8. **L745** — class 名 `TestT62Phase2DispatchParkReturnsRc2` 改为
   `TestT821Phase2DispatchParkReturnsRc5`（或类似）
9. **L848-849** — `assertEqual(rc, 2, "wait-mode park must return rc=2 ...")`
10. **L878** — `def _fake_phase2(**_kw): return 2` 测试 fake
11. **L1013-1015** — `assertEqual(return_codes, [2], "_cmd_auto_execute must return rc=2 on park...")`
12. **L1107** — `_patch(fo, "_phase2_dispatch", lambda **_kw: 2)`
13. **L1016+** 附近 method 名 `test_cmd_auto_execute_logs_park_message_on_rc2`
    改为 `..._rc5`

**D. 公开发布契约（已发，新 patch 必须显式 supersede）**：
14. `CHANGELOG.md` **L37-39** v0.8.2 节："Exit codes: ... `2` = AFK idle park
    (recoverable...)"。本 patch 在 CHANGELOG 顶部加 v0.8.2.1 节时，必须显式
    "v0.8.2 published rc=2 for AFK park; v0.8.2.1 corrects this to rc=5
    — update wrappers/monitors that branch on rc=2"。

**附属事实**：
- 未发现外部 CI / wrapper 把 rc=2 当 boolean error（codex audit）
- Flow 内 5 个 CLI 的 rc=2 = USAGE_ERROR 必须保留：`scripts/flow_autosave.py:236`、
  `scripts/flow_doctor.py:265`、`scripts/flow_promote.py:281`、
  `scripts/flow.py:85`、`scripts/flow_ralph.sh:187`。Path-aware grep 只看
  AFK park 路径，**不**做 repo-wide `return 2` 屠杀。
- `scripts/flow_orchestrator.py` **L33-36** sys.path 同时插入 `scripts/` 和
  `scripts/common/`，所以 `from common.exit_codes import` 与 `from exit_codes import`
  都能 resolve。**硬约束 patch 内统一用 `from common.exit_codes import ...`**，
  其他风格视为不合格。
- `scripts/common/afk_monitor.py` **L194** docstring 说 "caller stays parked"
  纯语义，**不** 涉及 rc 翻译；本 patch 不动该模块，且 reviewer 要警示
  "不要把 afk_monitor 改成 rc 逻辑"（保持纯化的 outcome 抽象）
- Suite 基线：834 smoke + 105 unit = 939 PASS（v0.8.2 SHIP 时）

## Requirements

1. **新建** `scripts/common/exit_codes.py`，plain module-level 常量
   （加 `Final[int]` 类型标注以避免 `True == 1` 隐式 bool 比较陷阱）：
   ```python
   from typing import Final

   PASS: Final[int] = 0
   GENERIC_FAIL: Final[int] = 1
   USAGE_ERROR: Final[int] = 2
   BLOCKED: Final[int] = 3
   NESTED_ABORT: Final[int] = 4
   PARKED_RECOVERABLE: Final[int] = 5
   ```
   - 模块**纯定义、零 side effect、零 import 除 `typing`**
   - 提供唯一 import 入口，**禁止**在 orchestrator + AFK-park 测试块
     写 AFK-park rc 字面量（`test_exit_codes_module.py` 本身需要断言
     `0..5` 字面量值，属于合法例外）
   - 必须在 import 风格上统一为 `from common.exit_codes import ...`
     （拒绝 `from exit_codes import ...` 即便 sys.path 让它能 resolve）

2. **迁移** `scripts/flow_orchestrator.py`（6 处源码）：
   - L4953 `_run_retry_loop` docstring："rc=0" → "`rc=5` (`PARKED_RECOVERABLE`)"
     —— 这是 v0.8.2 T6.2 漏改的 stale docstring，本 patch 顺手修
   - L4980-4986 inline 注释："rc=2" → "rc=5"
   - L5226-5234 `_phase2_dispatch` docstring "Return codes" 表：`2` → `5`
   - L5354-5362 `_phase2_dispatch` 体内 `return 2` → `return PARKED_RECOVERABLE`
     + 上方 8 行注释 rc=2 → rc=5
   - L5418-5435 `_cmd_auto_execute` docstring "Exit codes" 表：`2 = AFK idle park`
     → `5 = AFK idle park (recoverable)`，并说明 `2 = USAGE_ERROR` 保留给
     argparse error
   - L5571-5582 `_cmd_auto_execute` 体内 `if rc == 2:` → `if rc == PARKED_RECOVERABLE:`，
     `return 2` → `return PARKED_RECOVERABLE`
   - 顶部加 `from common.exit_codes import PARKED_RECOVERABLE`
     （已有 `from common import ...` 风格，对齐）

3. **迁移测试**（`tests/smoke/test_phase2_retry_loop.py` —— **全量迁移，不是补充**）：
   - L745 类名 `TestT62Phase2DispatchParkReturnsRc2` → `TestT821Phase2DispatchParkReturnsRc5`
   - L848-849 `assertEqual(rc, 2, "...rc=2...")` → `assertEqual(rc, PARKED_RECOVERABLE, "...rc=5...")`
   - L878 fake `return 2` → `return PARKED_RECOVERABLE`
   - L1013-1015 `assertEqual(return_codes, [2], "...rc=2...")` → `[PARKED_RECOVERABLE]`
   - L1107 `lambda **_kw: 2` → `lambda **_kw: PARKED_RECOVERABLE`
   - L1016+ 方法名 `test_cmd_auto_execute_logs_park_message_on_rc2` → `..._on_rc5`
   - 测试文件顶部加 `from common.exit_codes import PARKED_RECOVERABLE`

4. **新增 import smoke 测试**（`tests/smoke/test_exit_codes_module.py`，新建）：
   - 断言 `from common.exit_codes import PARKED_RECOVERABLE` 在 repo-root
     执行环境 + test 环境都能 import
   - 断言 6 个常量数值（`PASS=0` ... `PARKED_RECOVERABLE=5`）
   - 断言模块**没有任何 side effect**：`importlib.reload(common.exit_codes)`
     不报错且属性不变
   - **不**测试 `import exit_codes`（裸 import）能否 resolve；该 import 风格
     被禁止，把"它能 resolve"写成测试等于把 footgun 写进 contract

5. **更新 SKILL.md**（`claude/skills/flow/flow-phase2-execute/SKILL.md`）：
   - L168 叙述句 "rc=2 is recoverable park" → "rc=5 is recoverable park"
   - L171-178 Exit codes 表：`2` 节改为 `5`，并新增一行说明 `2 = USAGE_ERROR`
     （指向 5 个内部 CLI 的历史用法）
   - 不修改 `.claude/worktrees/feat+v0.8.2-p0-core/...` 镜像

6. **新增 SKILL.md 测试**（扩展或新建 smoke 测试）：
   - 用**归一化匹配**（去 backtick / 去 bold marker / 折叠空格）后断言
     SKILL.md 含 `5 = AFK idle park`（容忍 `` `5` = **AFK idle park** ``
     等 Markdown 变体）
   - 同样归一化后断言**不含** `rc=2 is recoverable park`、`2 = AFK idle park`
   - 这是固化文档契约 anti-drift 的硬钉

7. **Release 流程**（codex 校正：rc 改动 IS observable，不能"对外无影响"）：
   - tag `v0.8.2.1`（注解 tag），**不**force-move `v0.8.2`
   - `VERSION` 文件更新到 `0.8.2.1`
   - `CHANGELOG.md` 加 `## [0.8.2.1] - 2026-05-08` 节，**必须显式包含**：
     - "**Observable change**: AFK idle park exit code corrected from
       `2` (published in v0.8.2) to `5`. Wrappers/monitors that branch
       on rc=2 must be updated."
     - 链接到 `scripts/common/exit_codes.py` 全局 registry
   - GitHub release notes 复用 CHANGELOG 内容，**不**用"无外部影响"措辞
   - SemVer 选择 patch 版本号（v0.8.2.1）的合理性记录在 ADR `Decision`：
     v0.8.2 仅发布 24 小时，外部尚无生产部署，patch 修正"刚发布的错误"
     比 minor bump 更准确传达 intent（修 v0.8.2 引入的 contract bug，
     不是新功能）。如果外部已 ramp 用 rc=2 的 wrapper，转 minor 重新评估。

## Acceptance Criteria

- [ ] `scripts/common/exit_codes.py` 存在；6 个 `Final[int]` 常量数值正确；
      模块**仅** import `typing`，零 side effect
- [ ] `scripts/flow_orchestrator.py` AFK park 路径全部 6 处迁移完成；
      `from common.exit_codes import PARKED_RECOVERABLE` 已加文件顶部
- [ ] **5 处文档**全部反映 rc=5 契约：`_run_retry_loop` docstring（L4953）+
      inline 注释（L4980-4986）+ `_phase2_dispatch` docstring（L5226-5234）+
      `_cmd_auto_execute` docstring（L5418-5435）+ SKILL.md（L168, L171-178）
- [ ] **path-aware grep AC**：`grep -nE 'return 2|rc == 2|rc=2' scripts/flow_orchestrator.py`
      在 AFK park 路径区间（L4945-5600）结果为 0；其他路径 USAGE_ERROR
      用法保留（5 个 CLI 文件 `flow_autosave.py` / `flow_doctor.py` /
      `flow_promote.py` / `flow.py` / `flow_ralph.sh` 不动）
- [ ] **测试全量迁移**：`tests/smoke/test_phase2_retry_loop.py` 中
      `Rc2` / `rc=2` / `return 2` / fake `: 2` 在 AFK park 测试块结果为 0；
      class 名 `TestT821..Rc5` + method `..._rc5` 完成重命名；
      `from common.exit_codes import PARKED_RECOVERABLE` 已加
- [ ] 新 import smoke 测试（`tests/smoke/test_exit_codes_module.py`）覆盖：
      6 常量数值、`from common.exit_codes import` 风格、模块无 side effect
- [ ] **import 风格 grep AC**：`grep -rE 'from[[:space:]]+exit_codes[[:space:]]+import|^[[:space:]]*import[[:space:]]+exit_codes\b' scripts/ tests/`
      结果为 0（禁止裸 import 含缩进；canonical = `from common.exit_codes import ...`）
- [ ] 新 SKILL.md 文档契约测试断言含 `5 = AFK idle park`、不含
      `rc=2 is recoverable park`
- [ ] 既有 939 PASS 全部不退化；新增测试 ≥ +3 cases
- [ ] **CHANGELOG supersession**：`CHANGELOG.md` 顶部 v0.8.2.1 节显式含
      "Observable change: rc=2 → rc=5 for AFK park, update wrappers" 措辞
- [ ] **Mandatory codex review gate (opus)** 输出 0 P1
- [ ] tag `v0.8.2.1` 已创建并 push；GitHub release 已发；
      **`v0.8.2` tag 指向不变**（`git rev-parse v0.8.2` == `24bdecc`）
- [ ] `VERSION` = `0.8.2.1`

## Definition of Done

- 新测试 + 既有测试 all green
- Lint / typecheck pass
- pre-commit hook 自然通过（无 K-class 违规、无 --no-verify）
- Phase 4 sediment：进 progress.md + 是否需要 promote 到 vault 评估
- 无 credentials grep 命中

## Out of Scope

- 5 个内部 flow CLI 的 rc=2 用法（codex 校正：实际 5 个不是 6 个，
  `flow_capability.py` 无 rc=2）：`flow_autosave.py:236`、`flow_doctor.py:265`、
  `flow_promote.py:281`、`flow.py:85`、`flow_ralph.sh:187`。其语义 = USAGE_ERROR
  = 2，与 registry 一致；代码字面量替换为 import 常量是 v0.8.3 P3 backlog
- `scripts/common/afk_monitor.py` 完全不动；reviewer brief 必须警示
  "保持纯 outcome 抽象，禁止把 monitor 改成 rc 翻译"（pitfall: cascade trigger）
- `.claude/worktrees/feat+v0.8.2-p0-core/` 镜像（worktree 已 FF merge，
  cleanup 在另一 backlog 项）
- v0.8.3 P0.0 hook fix（独立任务）
- v0.8.3 P0.1 round 2+ implementer re-dispatch（独立任务）
- 任何对 `_phase2_dispatch` / `_run_retry_loop` 状态机除 rc 字面量
  + docstring 以外的逻辑改动

## Research References

- session breakpoint: `~/.claude/projects/-data-Claude-flow-framework/memory/session_latest.md`
  §"rc=2 ecosystem audit — DONE" + §"v0.8.2.1 patch release scope"
- codex round-2 反思（Flow 全局 exit-code registry）

## Decision (ADR-lite)

**Context**: v0.8.2 把 `_cmd_auto_execute` AFK idle park 设为 `rc=2`
（distinct from `rc=0 pass`），目的是阻止 caller 把 park 当 success
误判进 gate-7 merge。post-ship audit + codex round-1 plan-pass 同时发现：
(a) Flow 内 5 个 CLI 历史上 rc=2 = "argparse/usage error"，与 v0.8.2
新语义碰撞；(b) `_run_retry_loop` docstring（L4953）还说 afk_idle_park
"rc=0"——v0.8.2 T6.2 漏改的 stale doc bug；(c) CHANGELOG.md L37 已公布
v0.8.2 的 rc=2 契约——任何写 v0.8.2 wrapper 的人会读到这条信息。新
registry 给 park 独立槽位 `5 = PARKED_RECOVERABLE`，同时纠正 (b) (c)。

**Decision**: rc=2 → rc=5；新建 `scripts/common/exit_codes.py` 常量模块
（plain `Final[int]` constants，拒绝 IntEnum / helper class）。本 patch
narrow scope：只动 AFK park 路径；5 个 USAGE_ERROR CLI 不动。
SemVer 选 patch（v0.8.2.1）：v0.8.2 仅发布 24h，外部无生产 ramp，
"修刚发布的 contract bug" 比 minor bump 更准确传达 intent。

被拒绝：
- broader audit（所有 CLI 同时 import 替换）：churn 大，本 patch 溢出
- IntEnum class：import 路径变长，无类型强约束需求
- 保留 rc=2，仅文档化警示：codex round-2 反思认为 distinct rc 是
  D-class mechanical fix，文档化是 K-class，强度不够
- minor bump v0.9：传达 "新功能" 错误信号（实际是 contract bug fix）

**Consequences**:
- Short-term cost: ~60-90 min（codex round-1 plan-pass 把 site count
  从 5 升到 13，含 6 docstring/comment + 5 test 硬钉点 + 1 类名 + 1 method 名 +
  CHANGELOG supersession）
- Long-term benefit: AFK park rc 与 USAGE_ERROR rc=2 不再语义碰撞；
  exit_codes.py 成为后续 rc 改动 single source of truth；docstring
  drift（L4953）顺手修正
- Reversibility: high — 新 module additive；rc=5 → rc=2 回退是 trivial
  revert；patch tag 隔离影响不污染 v0.8.2 tag。但**rc=2 已经在 v0.8.2
  CHANGELOG 公布**，所以 v0.8.2.1 的语义变化是 "observable"，
  reversibility 视外部 wrapper ramp 情况

**Revisit triggers**:
- 外部用户脚本把 rc=2 当 AFK park 报告（需 maintainer 给迁移指引）
- 新 caller 出现需要把 rc=5 vs 其他 non-pass rc 做策略分支
  （触发 helper / IntEnum 升级讨论）
- 第二次出现"docstring 漏改 rc"（cascade evidence → 触发自动 grep CI 规则）

## Technical Notes

- Files to inspect (read-only first, before subagent dispatch)：
  - `scripts/flow_orchestrator.py` L33-36（sys.path 双插入说明）, L4945-4995, L5210-5240, L5340-5380, L5410-5445, L5560-5595
  - `claude/skills/flow/flow-phase2-execute/SKILL.md` L165-185
  - `tests/smoke/test_phase2_retry_loop.py` L740-760, L840-885, L1005-1025, L1100-1115
  - `CHANGELOG.md` L37-39 (v0.8.2 节，要 supersede)
  - `scripts/common/afk_monitor.py` L188-205（**只读，理解 monitor 纯 outcome 抽象**）
- Files to create:
  - `scripts/common/exit_codes.py`（新；`Final[int]` 常量；零 side effect）
  - `tests/smoke/test_exit_codes_module.py`（新；6 常量值 + import 风格 + 无 side effect 三组断言）
- Constraints:
  - **Mandatory opus gate**：state-machine + rc value 改动必走 codex review
    （session_latest 反思固化的规则）
  - **K-class 红线**：禁 `--no-verify`；hook 若 block 走根因排查（pitfall #1
    Option E first-line-only 临时未实现，本 patch 不打算修 hook 自身）
  - **Tag 不可 force-move**：`v0.8.2` 必须保持指向 `24bdecc`
  - **Import 风格强约束**：所有 `exit_codes` import 必须用 `from common.exit_codes import ...`，
    禁止 `from exit_codes import ...`（即便 sys.path 让它能 resolve）
  - **afk_monitor.py 不动**：保持 outcome 抽象纯净，不引入 rc 翻译；
    reviewer brief 必须显式警示这一点
  - **path-aware grep**：移除 rc=2 时只 scope 到 orchestrator AFK park 路径
    （L4945-5600）；5 个 USAGE_ERROR CLI 文件保留 rc=2 字面量
- Related ADRs / pitfalls:
  - `.flow/pitfalls/hook-blocks-after-reviewer-pass.md`
  - `.flow/tasks/05-08-v0.8.2-p0-core/prd.md` ADR-1（budget counter）
  - codex round-1 plan-pass session: `019e0724-0b49-7a23-92b0-1a5226c0d8d0`
- credentials_ref: 无；本 patch 不接触任何 credentials
