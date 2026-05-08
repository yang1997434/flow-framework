---
id: claude-review-blindspots
title: Claude review 链路 4 大盲点（codex 抓到的）
discovered_at: 2026-05-06
discovered_in: feat/v0.8.1-safety-stack T1 (8 commits) + T2 (4 commits) + T3-T7 (扩展 D2-D5) + T8 (扩展 C2 + D6) + T9 (扩展 E shell=True bypass + F identity fail-open) + T10 (扩展 E "validation 规则 vs 真实数据" 自查项) + T11 (新增 G "facts from disk 要穷举层" + H "外部工具输出解析 ambiguity") + T12 (新增 I "复用前 task 解决模式自查" + J "fix 链式引入新 paper cut")
trigger_paths:
  - "scripts/flow_*.py"
  - "**/contract*.py"
  - "**/orchestrat*.py"
severity: high
recurrence_risk: high — applies to every T3-T22 task in v0.8.1
---

# Claude review 链路 4 大盲点

T1 + T2 累计 7 轮 codex review。Claude 链路（spec review + pr-review-toolkit code review）GREEN/APPROVE 之后 codex 仍然抓到 8+ 个真问题。把 codex 独家发现的归类，给 T3-T22 reviewer 一份自查 checklist，**降低 codex review 反复成本**。

---

## A. Python falsy/truthy 陷阱

**症状**：
- `x = c.get("key") or default` — falsy 值 `""` `0` `False` `[]` `{}` 都被当 missing
- `c.get("key") is None` — 漏 falsy strings/bools，"" 0 False 不拒
- `dict(raw.get("parent") or {})` — `parent: null` / `false` 被静默兜底
- `if "key" in c` 是对的，`.get()` + 值检查永远是错的（除非 null 真的合法）

**Reviewer 自查**：
- [ ] grep `\.get(` 每个调用点，问：null/""/0/False 应该如何处理？跟 absent 一样吗？
- [ ] grep ` or ` 在赋值/条件里，问：左边能否 falsy？
- [ ] grep `is None`，问：是否应该改成 `not in obj`？

**修复 pattern**：用 `flow_contract.py` 的 `require_field` / `optional_field` + validators。详见 `.flow/pitfalls/schema-parsing-get-vs-in.md`。

---

## B. 设计文档跨引用语义耦合

**症状**：
- T2 实现按 plan 步骤逐条勾选，spec review 给 GREEN，但 design doc §6 R8 写"e2e 永远不接受 idempotent override"——只看 plan 看不到这条
- 跨字段约束（X 类型 + Y 字段值 → 必须拒绝/要求）在 design 表格里，plan 只引用不重复

**Reviewer 自查**：
- [ ] 改某个字段时，在 design doc 里 grep 该字段名 + 类型枚举值，看有没有跨字段约束行（"e2e cannot ..."、"X must imply Y"）
- [ ] 任何 enum 字段（`type`、`method`、`autonomy_mode` 等），列出所有值 × 所有相关字段的笛卡儿积，问每个组合 design 怎么说
- [ ] design doc 的 §3 / §6 / §7 等"semantics"章节是必读的，不是参考的

**修复 pattern**：parser 在校验单字段后再做跨字段 invariant 检查（T1 已有 `post_merge_skip + regression` 这类）。

---

## C. 架构级顺序 / 可达性

**症状**：
- T2 把 ceiling check 加在 `parse_contract()` 之后；plan + design 都说"必须在 dispatch path 里 fail-closed"。Claude review 验证"check 存在"，但没问"check 在 parse 失败后还能 fire 吗"
- "fail-fast 防线" 被 "fail-soft 异常 handler" 短路
- **C2 (T8 round-1 [P2])**：分支链中的 catch-all 早返回（`if type == "regression": return BLOCK_ROW5`）位于一个**含同样类型的 frozenset 检查**（`if type in PHASE3_NEVER_LOCAL_TYPES: return ESCALATE`，含 `regression`）之前 → frozenset 中的 regression 永远到不了，常量在"撒谎"。Claude spec/code review 看到分支顺序但没逐 cell 算"哪个分支拦住哪个 (type, phase, status) 组合"。

**Reviewer 自查**：
- [ ] 任何"MUST happen in path X"的 gate / check，找代码里所有可能抛异常的前置步骤。问：每个异常处理器里这个 gate 还能 fire 吗？
- [ ] 画一张简化的控制流图：入口 → 各异常 handler → 出口。每条出口路径都过 gate 吗？
- [ ] grep 所有 `except` 块。每个 catch 后是否绕过了某个原本应该 fire 的 gate？
- [ ] 如果 spec 用 "MUST" / "fail-closed" / "hard reject" 措辞，警觉：fail-soft fallback 路径是反义词
- [ ] **若代码里有命名集合 / frozenset / whitelist（如 `PHASE3_NEVER_LOCAL_TYPES`）**：grep 该常量的每个成员，对每个成员手算"该 type 在每个 phase × status 组合下到底命中哪条分支"。常量含某成员但分支顺序让该成员永远到不了 = 常量在撒谎，未来读者很可能基于常量假设修代码而引入 regression。

**修复 pattern**：把 gate 提到所有异常处理之前（pre-parse / pre-validation），或在异常 handler 里补 gate 调用，或重构异常类型让 gate-relevant 异常往上传播而不被 swallow。**对于含 frozenset 的分支链：要么 frozenset 检查置于所有"含该成员的 catch-all"之前；要么 frozenset 不含该成员（但要在测试矩阵里 pin 每个成员的实际路由）。**

---

## D. Bypass 通过 fallback path

C 的具体子集，单独列因为最隐蔽。已发现 4 个变种（D1-D4），都是 codex 在 T2-T3 抓到的：

### D1. 后置 gate 被前置失败短路（T2 round-1 P1）

**症状**：v0.8.1 ceiling check 加在 parse 之后，parse 因为未来字段（如新 `autonomy_mode` 值）失败 → fallback_reason 设了 → degrade to interactive → ceiling 永远没机会跑

**Reviewer 自查**：
- [ ] 任何带 fallback_reason / degrade / soft-error 路径的 gate，**专门**写一个 "future-incompatible payload" 测试：让 parse/validation 必失败，看 gate 是否仍然命中
- [ ] 测试矩阵：当前合法 ✓ + 当前非法 ✓（已覆盖）+ **未来合法-当前非法** ✗（最易漏）

**修复**：fail-closed gate 必须在 fail-soft 处理之前。或者用 dedicated 异常 subclass + 区分捕获。

### D2. try/except 吞 OSError 当成"条件不成立"（T3 round-2 P2）

**症状**：precondition 检查里 `try: ...; except (OSError, FileNotFoundError): return False`。本意是"工具不可用 = 条件不成立"，结果把"工具坏了"和"条件本就不成立"混淆 → silent skip。

**Reviewer 自查**：
- [ ] 任何 `except` 后 `return False` / `return None` / `pass` 的写法，问：异常代表"条件不成立"还是"环境坏了"？这两种处理应该不一样
- [ ] precondition gates 不应该 swallow 任何 exception；让它们 propagate 到测试 reporter / CLI 错误显示

**修复**：只 catch 你明确知道含义的异常类型；其他全部 propagate。或者改用不抛异常的 API（`subprocess.run` 不带 `check=True`）然后只看返回值。

### D3. 子进程 rc != 0 当作"事情不成立"（T3 round-3 P2）

**症状**：`subprocess.run(["git", "rev-parse", "--verify", "--quiet", "tag"]); return rc == 0`。但 `rev-parse` 在很多场景都会 rc != 0：tag 不存在 / repo 损坏 / safe.directory 报警 / dubious-ownership / 权限错误 / 非 git 目录。把所有这些当成"tag 不存在"是谎言。

**Reviewer 自查**（任何 subprocess-based check）：
- [ ] 该子命令的 rc 含义是否唯一？查 man page / `--help`，列举 rc != 0 的所有原因
- [ ] 如果 rc != 0 含义模糊，换一个含义清晰的子命令。例：`git tag --list <name>`（rc=0 时 stdout 空 = 不存在 / 非空 = 存在；rc != 0 = git 自己挂了）
- [ ] 配合 `check=True` 让"工具自身挂了"propagate，区别于"工具说事情不成立"

**修复**：选 rc 语义干净的子命令；配合 `check=True` 强制 git 故障 raise。

### D4. silent default fallback when validation 错（推测但还没遇到）

**症状**（前瞻）：`try: x = parse(value); except ValidationError: x = DEFAULT`。把"输入坏了"当成"用 default"，跟 D1 同源但发生在更细的位置。

**Reviewer 自查**：grep 任何 `except.*ValueError` / `except.*Error.*: return DEFAULT` / fallback 风格，问 default 应不应该轮到。

---

D 类共同教训：**`except` 块里所有"return 一个看似合理的值"都是嫌疑犯**。"看似合理"= 让上层逻辑当作"事情不成立"，但 exception 本身可能 carry"工具/环境/前置条件"等其它语义。每个 except 块都要明确分辨"这真是我要 catch 的语义"还是"我把别的也吞了"。

### D5. 每个 executor 的 typed except-tuple 都是不完整的（T7 R5 [P2]）

**症状**：T7 `_run_cmd` / `_run_file_exists` / `_run_http` 都列了精心设计的 except-tuple（subprocess.TimeoutExpired / OSError / urllib.URLError / json.JSONDecodeError 等），但 codex 在 5 轮 review 里持续找到**没列出的**异常源头：
- `Path.resolve("a\x00b")` 抛 `ValueError`（embedded NUL）
- `subprocess.Popen("echo \x00")` 抛 `ValueError`
- `urllib.parse.urlsplit("http://[::1")` 抛 `ValueError`
- `urllib.urlopen` 抛 `http.client.HTTPException`（不被 URLError 包装）
- `Path.resolve()` 在 symlink loop 抛 `RuntimeError` 或 `OSError`

每个都让 executor 的 typed except 漏掉，异常逃到 `run_one` —— 但 `run_one` 已经 emit 了 `started` event。结果：孤儿 progress entry，没有 paired `completed`。

**根因**：你不可能枚举出所有"奇怪输入会让标准库抛什么异常"。typed except-tuple 必然不全。

**修法（T7 最终采用）**：在 orchestration 层（不是 executor 层）加 catch-all：

```python
# in run_one
try:
    result = self._dispatch_method(criterion)
except Exception as e:  # NOT BaseException — 别吞 KeyboardInterrupt/SystemExit
    result = RunResult(
        status="inconclusive",
        error_msg=f"executor raised unexpected {type(e).__name__}: {e}",
    )
# 后续仍然 emit completed event — paired 不破
```

这是**唯一**能堵死整类的方法。一行 catch-all > 5 轮 codex 加 except 项。

**Reviewer 自查**：
- [ ] 任何带"先 emit started → dispatch → emit completed/timeout"模式的代码，dispatch 那一行**必须**被 catch-all 包住
- [ ] catch-all 是 `Exception` 不是 `BaseException`（用户中断必须能传到顶）
- [ ] catch-all 里 routes to 一个 sane terminal status（inconclusive / fail，不是 crash）
- [ ] 测试至少要有一条"夹一个谁都料不到的输入"的路径（embedded NUL / 控制字符 / symlink loop / 超大 input），验证 paired event 仍然在
- [ ] 别试图在每个 executor 里 catch 所有可能 — 那是 whack-a-mole；做 catch-all 的 orchestration 防御层

**适用范围扩展**：任何"emit before / dispatch / emit after"的模式（事件源头、生命周期 hooks、metrics 包装器、tracing span 等），dispatch 失败必须保证 emit-after 仍然 fire（或显式取消 emit-before），否则一定有孤儿状态。

### D6. 重排分支以修 C-blindspot 时的 status 范围越界（T8 round-2 [P2]）

**症状**：T8 round-1 把 `if phase == 3 and type in PHASE3_NEVER_LOCAL_TYPES:` 提到 `regression` catch-all 之前，**修对了 fail/timeout 的路由**，但同一句对**所有未被前面分支 short-circuit 的 status** 也生效——包括 `interrupted`。原 catch-all docstring 明说 `interrupted → BLOCK_ROW5`。新分支静默把 Phase 3 behavior+regression 的 `interrupted` 路由从 `BLOCK_ROW5` 改成 `BLOCKED_ESCALATE_ROW6`，不在原意图范围内。

**根因**：修 C-blindspot 时，开发者只想着"让某个特定 (type, phase) 走对路径"，没追问"被这条新分支拦下的所有 (type, phase, status) 组合**全部**应该走这里吗？" status 维度被忽略。

**修法**：分支条件加 status 守卫，跟 spec/design 文档明示的 escalate 触发条件对齐：

```python
if (phase == 3
        and criterion.type in PHASE3_NEVER_LOCAL_TYPES
        and status in ("fail", "timed_out")):  # ← 加守卫
    return EvalDecision.BLOCKED_ESCALATE_ROW6
```

**Reviewer 自查（针对每次"为修 C-blindspot 重排分支"的 PR）**：
- [ ] 列出**新分支**实际拦下的 (type, phase, status, escalate, ...) 组合全集；逐一对照 spec / design：每个组合都应该走这条分支吗？
- [ ] 特别关注 status 这个常被遗漏的维度——`pass` 和 `inconclusive` 通常在前面早返回，但还有 `fail` `timed_out` `interrupted` 三种。文档常只描述前两种；第三种很容易"搭便车"。
- [ ] 测试矩阵里有没有给这个新分支拦下的**每个 status** 加 cell？只测 `fail` 不够。
- [ ] 反向验证：若 spec 说"X→escalate"，grep "其他 status 规则"，确认它们没被覆盖。

**适用范围扩展**：任何"按多维条件 (type × phase × status × ...) 路由"的状态机，重排分支时必须**同时**审视所有维度。frozenset / whitelist 只描述一个维度；其他维度（status / role / phase / ...）需要单独的守卫子句或单独的早返回。

---

## E. shell=True 命令 + 字符串前缀匹配 = 复合命令绕过（T9 round-1 [P1]）

**症状**：
- T9 `resolve_in_flight_idempotency` 用 `command.startswith(allowed + " ")` 给 `cmd` allowlist 做前缀匹配，命中后路由 `auto_rerun`。
- 但底层 executor `_run_cmd` 用 `shell=True` 跑命令字符串。
- `pytest tests; ./deploy.sh` 完美前缀匹配 `pytest` 但实际跑两条命令——crash 后 resume 会重跑 `./deploy.sh` 的副作用。
- Claude spec + code review 双 GREEN，因为：spec review 看的是"前缀匹配"是否符合 allowlist 语义；code review 看的是 `startswith` 实现是否正确。**没人问"被允许的命令实际执行模式（subprocess.run shell=True）会不会让前缀匹配的语义被绕过"**。

**Reviewer 自查（每次代码涉及"字符串模式 + 命令执行 + 安全决策"时）**：
- [ ] 命令到底怎么执行？`shell=True` / 直接 fork+exec / 解析 token 后 exec？答 1：考虑 shell metachar bypass。答 2/3：考虑 token 注入。
- [ ] 如果是 `shell=True`：grep `;` `&&` `||` `|` `<` `>` `` ` `` `$(` `$\\` `\n` 这些 metachar 在测试 fixture 里是否出现过？没出现 → 测试矩阵漏掉了"复合命令"这个 attack surface。
- [ ] 字符串前缀/包含匹配做安全决策时，**永远问**：能不能在被允许的前缀后面附加非允许的内容，结合执行语义产生绕过？
- [ ] Allowlist 类决策：默认拒绝（block），允许是显式 opt-in，不是反过来。
- [ ] **Validation 规则要先用项目自己的真实数据自测**（T10 教训）：implementer 主动加 `_SLUG_RE = ^[a-z0-9][a-z0-9_-]*$`，但项目自己的 slug `05-05-autonomous-mode-v0.8` 含 `.`，连 `python -c "import re; print(re.match(r'^[a-z0-9][a-z0-9_-]*$', '05-05-autonomous-mode-v0.8'))"` 都没跑过。规则一上线就会拒绝合法输入。**reviewer 必查**：grep 项目里所有出现该规则要校验的字段（task slug / repo / branch 名）的真实样本，对比规则。规则 vs 数据不匹配 = bug。

**修法**：在 allowlist 命中后再加一道 metachar guard，命中 metachar 退化到 block + 提供"per-criterion override + rationale"作为 operator opt-in 通道：

```python
_SHELL_METACHARS = ";&|<>()`$\\\n\t\r\v\f"
if command_matches_allowlist:
    if any(c in _SHELL_METACHARS for c in command):
        return block_in_flight(reason="contains shell control chars")
    return auto_rerun
```

**适用范围扩展**：所有"基于命令字符串的安全决策" + `shell=True`/`os.system`/`eval`/`exec` 类 API 的组合。如果安全决策依赖前缀/子串匹配，metachar guard 必须显式加。

---

## F. Identity check fail-open（T9 round-1 [P1] + round-2 [P2]）

**症状**：
- T9 `resume_attempt` 用 `criteria[in_flight_idx]` 查找 in-flight criterion，**没验**事件里记录的 `criterion_hash`。
- 如果 contract 在 crash 和 resume 之间被改了（删/加/重排），同 idx 上现在的 criterion 可能跟当时跑的完全不是一个东西（method 从 cmd 变成 file_exists）。
- 错的 criterion 走 R8 分类规则，可能 auto_rerun 一个新 criterion，而真正的不安全 cmd 永远不被 block。
- Round-1 修：加 `recorded_hash != current_hash → block`。
- Round-2 又抓：`if recorded_hash:` 在 falsy（空字符串、None、缺字段）时 fall-through，**重新打开了 round-1 关闭的洞**——malformed JSONL 也会绕过。

**根因（一类共通）**：identity / 身份验证 check 的"找不到验证依据"分支，必须 fail-closed（block / reject），不是 fail-open（skip / pass）。

**Reviewer 自查**：
- [ ] 任何 `if hash/token/sig: verify` 模式 → 问 falsy 分支干啥？skip = fail-open（危险）；block = fail-closed（正确）。
- [ ] backward compat 论证（"老 schema 没这字段所以放过"）→ 警觉：这就是 fail-open。要么显式枚举可信旧 schema，要么 fail-closed + 迁移计划。
- [ ] 测试矩阵：identity check 一定要有"验证依据缺失"的 cell 测 fail-closed。
- [ ] 测试 fixture 默认值 `default = field or "fallback"` → 检查 `""` 是否被静默替换。空字符串和 None 在测试里要能区分。

**修法**：

```python
recorded = event.get("criterion_hash")
if not recorded or not isinstance(recorded, str):  # ← 不放过任何 falsy
    return block_in_flight(reason="lacks usable criterion_hash")
if recorded != current_hash:
    return block_in_flight(reason="identity changed")
# 只有两道都过才 fall-through
```

**适用范围扩展**：所有 hash / signature / nonce / token 校验。"找不到/无效"绝对走 reject 路径。这是密码学世界的基本规矩，状态机里也一样。

---

## G. "Facts from disk" 的 disk state 必须穷举（T11 round-1 [P1]）

**症状**：T11 verifier 信 `derive_task_facts(ctx)` 返回的 facts 来防 manifest 违规。`derive_task_facts` 用 `git diff base..HEAD` 派生 changed_files / newly_added_files。看起来很 PRD §1.2 ("facts come from disk, not subagent narrative")。

**但 disk 不是单一 state**。Git 的 disk 状态至少 4 个层：
1. **HEAD commit history** (`git log` / `git diff base..HEAD`)
2. **Index / staging area** (`git diff --cached`)
3. **Working tree, tracked changes** (`git diff` no args)
4. **Working tree, untracked files** (`git status` 或 `git ls-files --others`)

T11 verifier 只看 #1。subagent 把 forbidden 文件留在 #2/#3/#4 任何一层都能绕过。讽刺：design §1 row 4 整个就是 "untracked file outside scope"，但实现不看 untracked。

**Reviewer 自查（任何 "facts derived from disk" 模式）**：
- [ ] disk 包含哪些 state 层？逐一列出，不要只想"主"那个。
- [ ] 攻击者只需要把 evidence 留在**任何一个**未被检查的层就能绕过。
- [ ] 对 git：facts 必须同时覆盖 HEAD diff + working tree (staged + unstaged + untracked)。
- [ ] 对 file system：facts 不能只看 directory listing — 还有 hidden files / symlinks / extended attributes。
- [ ] 对 process state：facts 不能只看 stdout — stderr + exit code + signals + 子进程留下的副作用都是 disk 层。

**修法**：
- Git: 用 `git status --porcelain --untracked-files=all -c core.quotePath=false` 拿全谱，merge 入 facts。
- File system: 用 `find -type f` 而不是 `os.listdir`。
- Process: capture stdout + stderr + returncode + 监控副作用文件。

**适用范围扩展**：所有 "trust the source of truth" 论证都要追问"哪个 source of truth"。一句"facts come from disk"是不够的——必须明示"facts come from {disk state #1, #2, #3, #4}"。Subagent / 攻击者 will find any unchecked layer.

---

## H. 解析外部工具输出时的 string-parsing ambiguity（T11 round-2 [P1]）

**症状**：T11 round-1 fix 用 `git status --porcelain` 返回的文本，用 string split 区分 rename 行 (`R  newpath -> oldpath`) vs 普通行。代码：
```python
path = rest.split(" -> ", 1)[1] if " -> " in rest else rest
```
但 `git status --porcelain` 默认用 newline + space 分隔字段，没有结构化的 record boundary。攻击者可以创建 untracked 文件名为 `secrets/key.pem -> ok-decoy.py`，git 报告：
```
?? secrets/key.pem -> ok-decoy.py
```
parser 看到 ` -> ` → split → 截断只取 `ok-decoy.py`（in scope），forbidden prefix 完全消失。

**根因**：用 string-based heuristic 区分语义（"是不是 rename"），而 git 实际只在 metadata 层（status code）声明了这个事实。string parser 是 **status-blind** —— 看不到 git 想告诉你的"this is a rename"信号，反而被 user-controlled filename 内容污染。

**Reviewer 自查（任何"解析外部工具输出"代码）**：
- [ ] 工具有没有提供 unambiguous 输出格式？(porcelain `-z` / JSON / NUL-delimited)。如有，**必须用** unambiguous 格式，不要用人类友好的 default。
- [ ] 字段分隔符（` -> `, `,`, ` `）是不是可能出现在 user-controlled 内容里？filename / commit message / branch name 都可以含任意 ASCII。
- [ ] 用 metadata（status code / type marker）区分 record 类型，而不是 inline 文本 heuristic。
- [ ] 测试一定要 cover "filename / message / arg 含 separator" 的情况。

**修法**：
- Git: `git status --porcelain -z` (NUL-delimited records, renames 占两个 records)；同样 `git ls-files -z`、`git diff -z`。
- 解析输出: `output.split('\x00')`，rename detect 看 status code (`R`/`C`) 而不是 inline 字符串。
- 通用: 优先选 stable / structured 输出（JSON / `--format=...` 自定义），最后才考虑 default human-readable。

**适用范围扩展**：所有 CLI tool 输出解析（git / docker / kubectl / aws-cli / ...）。**不要相信 default human-readable format 的字段分隔**——它对人友好，对 parser 是攻击面。每次 grep/sed/awk 之前问：有没有 `-z` / `--json` / `--format` 替代？

---

## I. 重新踩同一坑（T12 round-1 [P2] #2）

**症状**：T12 的 `gate1_baseline` / `gate6_regression` 都用 `subprocess.run(shell=True, timeout=...)`，这是 T7 早就修过的同一个 process-group leak bug。T7 的 `_run_cmd` 用 `Popen(start_new_session=True)` + `os.killpg` 解决，T12 完全没复用，又自己跌一遍。

**根因**：implementer 写新模块时只看 plan + 当前 review prompt，不会自动 grep 项目里"已有的相似代码 + 教训"。每个 task 的 codex 教训沉淀进 pitfall 文件，但**主动复用模式**没固化成自查流程。

**Reviewer 自查（T13+ 必加）**：
- [ ] 当看到 `subprocess.run(..., shell=True, timeout=...)` / `subprocess.Popen` / `os.fork` 等 process-spawn 调用：项目里有没有已经存在的 process-spawn helper？（grep `_run_cmd` / `start_new_session` / `killpg`）
- [ ] 当看到 git 输出解析：项目里有没有已经存在的 git output parser？（grep `--porcelain` / `-z` / `git diff`）
- [ ] 当看到 path-traversal validation：grep `_SLUG_RE` / `_REF_RE` / `denylist`。
- [ ] 任何"似乎应该有 helper 的"低层操作 → 先 grep，没找到才自己写。

**修法**：implementer prompt 加一个固定段落："此 task 涉及 X / Y / Z 操作，请先 grep `scripts/` 找 existing helpers，**复用 + 引用** 而不是重写"。

**适用范围扩展**：每次跨 task 的"同形状"操作都应该先 grep，避免重复劳动 + 重新踩坑。Pitfall 文件本身就是这种"踩过的坑"目录，但需要主动调用而不是被动等 codex 抓。

---

## J. Fix 链式引入新 paper cut（T12 round-2/3 [P2/P3]）

**症状**：T12 codex 4 轮收敛过程：
- Round-1 修两个 [P2] (stale facts + pgkill)
- Round-2 [P2]：round-1 的 fix 引入新 D5 gap (`derive_task_facts` 抛 CalledProcessError 让 orchestrator crash)
- Round-3 [P2/P3]：round-2 的 fix 标签错（halted_at_gate=gate3_manifest 但 gate3 还没跑）+ str(CalledProcessError) 丢 stderr forensic info
- Round-4 GREEN

每次 fix 引入下一轮要堵的 paper cut。codex 总能再发现"刚加的 try/except 缺 forensic info" / "刚加的标签语义不匹配" 这类紧贴前一 fix 的边角问题。

**根因**：implementer / reviewer 修 main bug 时容易陷入"局部正确"——看新加的 5 行代码内自洽，没看大图：
- 新加的 except 分支返回的 verdict 是否符合上层 `Phase2Verdict` 契约？
- 新加的 catch 是否丢失了原 exception 的 forensic info？
- 新加的标签字符串是否和现有 audit log 解析逻辑一致？

**Reviewer 自查（每次 fix-pass 时必查）**：
- [ ] 看新加的 except / catch 分支：返回的对象的所有字段值都符合"如果调用方不知道这是 fix 路径"的语义吗？
- [ ] exception object 上的 forensic data（`.stderr` / `.stdout` / `.cause` / `.__notes__`）有没有被显式提取？`str(e)` 是不够的——它只输出 exception class 默认的 short message。
- [ ] 新加的字符串字段值（`"gate"`、`"reason"` 等）是否跟现有 audit log 消费者匹配？grep 引用点。
- [ ] 任何"返回一个新 enum/状态值"的 fix → 上层 dispatch 是否处理这个新值？还是被默认 fallback 吞？

**修法**：fix 完一轮立刻在心里跑一遍"如果这是 production 的 incident，audit log + forensic 够不够定位根因？"如果不够，缺什么。这能在 round-1 就把 round-2/3 的内容一起改完。

**适用范围扩展**：任何防御性 fix-pass。坑修一半 = 引入下一坑，codex 会继续抓。**真 GREEN = 整个 fix 路径的 verdict / forensic / labeling 都和 happy-path 一致**，不是"main bug 修了"。

---

## K. Plausible justification trap — implementer 自圆其说 + spec accept ≠ safety（T13 round-1 [P1]）

**症状**：T13 `gate4_codex_review` 用 `subprocess.run(shell=True, timeout=...)`，没用项目里已经有的 `_run_shell_with_pgkill`（gate 1 + gate 6 都用了）。implementer 在源码里写了说服性的 justification 注释：

```python
# Pitfall I: gate 1 / gate 6 use _run_shell_with_pgkill for tests
# spawning child trees. gate 4 is different — fixed CLI invocation
# owned by gate 4 itself, with no expected child-process tree.
```

spec-review 看了这段 justification 觉得合理，标 GREEN。**codex round-1 直接 vindicate T7/T12 教训**：`shell=True` 就是有 shell parent，shell 的子进程（即 codex 本体或测试里的 `sleep`）不会跟 shell 一起被 SIGKILL，会 orphan 到 PID 1。

**根因**：I-class 的升级版。
- I-class 原版：implementer **忘了**复用现有 helper，被动靠 reviewer 抓
- K-class 升级：implementer **主动论证不复用是合理的**，论证看上去自洽，spec/code review 也接受
- 这种"看起来想清楚了"的偏离 比"忘了复用"更危险，因为后续 reviewer 会被前面的 reasoning 锚定

**Reviewer 自查（K-class 必查）**：
- [ ] 任何"为什么这里不用现有 helper"的注释 → 立刻 grep `_run_shell_with_pgkill` / 类似 helper 实际行为，不要凭注释信任
- [ ] 任何"X 是特殊情况，不需要 Y"的 reasoning → 反向验证：if Y, would it hurt? if no harm, why not Y?
- [ ] 任何 "fixed CLI" / "single binary" / "no child tree" 类 reasoning，且周围有 `subprocess` / `Popen` / `shell=True` → 默认不信，要求 codex 二次审
- [ ] 看 implementer 注释里"复用 X 的 pattern"和"不复用 X 因为..."的比例 — 后者多于前者就是危险信号

**修法**：implementer prompt 增加一段强约束：

> 任何偏离已有 helper / pattern 的选择 — 即使你写了 justification — **不要** 在 self-review 阶段批准自己。把 justification 写出来留给 codex 审；codex 不同意时优先承认错误，不要辩。

spec-review prompt 增加：

> 看到任何"为什么不用 X"的 justification，treat it as 高风险 — 立刻 grep 现有 helper 行为，并用 codex review 兜底。spec review 自己的 GREEN 不能取消 codex 的必要性。

**与 I-class 的关系**：I 是 base class（"忘了复用"）；K 是危险变体（"自论证不复用"）。两者修法相同（grep + codex），但 K 多一条：**spec/code review accept 是 K-class 的 enabler，不是 mitigation**。

**适用范围扩展**：所有"established pattern + plausible reason to deviate"的场景。最常见在：
- subprocess / process management（T7/T12/T13 三次踩同坑）
- error-handling 的 fallback default（D 类 family）
- 安全 boundary 的 trust assumption（E/F 类）

---

## L. Type-check vs presence-check — `key in dict` 不等于值合法（T13 round-1 [P1]）

**症状**：T13 `gate4_codex_review` 用 `key in dict` 验证 codex JSON 输出有 `_REQUIRED_ISSUE_KEYS`，但当 codex 返回 `{"file": null, "message": null, ...}` 时：
- `"message" in issue` → True（key 存在）
- `issue["message"].lower()` → AttributeError（None 没有 .lower()）
- 或 `f"{issue['file']}|..."` → 静默 stringify None / 数字 进 hash，产生错误的 canonical id

**根因**：F-class（fail-closed on missing fields）的浅层版本只检查 key 存在，没检查 value 类型。JSON null / 数字 / list 都能通过 `key in dict` 但在 string-only 上下文中要么 crash 要么静默错。

**Reviewer 自查**：
- [ ] 任何 `if key in dict` / `dict.get(key, default)` 后接 `dict[key].lower()` / `dict[key].split()` / 字符串拼接 → 必须先 `isinstance(dict[key], str)`
- [ ] JSON-loaded data 的所有 string-typed 字段访问 → 加 type 守门
- [ ] `f"{...}"` / `str(...)` 在 hash / id / 路径 这种"必须确定来源"的上下文 → 必须先 type-check，禁止 silent stringify
- [ ] schema 文档说 "file: string" 不等于 runtime 保证 — runtime 必须自己验

**修法**：建立 fail-closed 验证模式：

```python
for k in _REQUIRED_KEYS:
    if k not in d:
        return inconclusive(reason="missing_field", field=k)
    if not isinstance(d[k], str):
        return inconclusive(reason="non_string_field",
                            field=k, type=type(d[k]).__name__)
```

应用范围：所有从外部（JSON / YAML / subprocess output / network）读取后立刻在字符串上下文使用的字段。

---

## N. Disk identity vs ref identity — branch ref 可 stale 或被改写（T14 round-2 [P1]）

**症状**：T14 `MergeRunner._continue_merge` 写好 R9 + Check #1 (worktree clean) + Check #2 (HEAD == facts) 三个前置 check 后，spec/code review 双 GREEN。但 codex round-2 立刻指出：

> A subagent can move the worktree off ctx.branch, causing Gate 7 to merge a different ref than the one that passed gates.

攻击场景：
1. worktree 启动时在 ctx.branch @ A
2. subagent 执行 `git checkout --detach`，HEAD 变 detached @ A
3. subagent 在 detached 上 commit B
4. derive_task_facts 记 `target_commit_pre_merge = B`（HEAD 处的 sha）
5. gates 全对 B 跑（worktree 在 B）→ 全 PASS
6. T14 Check #1（worktree 干净）✅
7. T14 Check #2（HEAD == facts.B）✅
8. T14 step 6 `git merge --ff-only ctx.branch` — 但 ctx.branch ref 还指 A → "Already up to date"
9. `merge_applied` 写入。B 留 detached limbo 永远不被合并。Gate 7 完全 bypass。

**根因**：把 "branch identity" 等同于 "branch ref 指向哪里"。在 git 中，ref 和当前 HEAD 是两个独立的概念：
- HEAD 可以 detached（不指任何 ref）
- HEAD 可以指 ref-A 但 ref-A 的 sha 不等于实际 commit
- ref 可以被 `git update-ref` 任意改写
- 多个 worktree 可以共享同一个 git database，所有 ref 操作都会反映

T14 的 fix 双重防御：
- **Fix #1 — symbolic-ref check**：`git -C worktree symbolic-ref --short HEAD` 必须 rc=0 且 stdout == ctx.branch。这一步抓 "detached" + "切到别的 branch"两种情况。
- **Fix #2 — merge SHA 不 ref**：step 6 改成 `git merge --ff-only <facts.target_commit_pre_merge>`。把"merge 哪个 commit"和"branch 是否健康"解耦——即使 ref 被外部改写，我们 merge 的还是 gate 验过的 SHA。

**Reviewer 自查（N-class 必查）**：
- [ ] 任何 `git merge <branch>` / `git rebase <branch>` / `git cherry-pick <branch>` 操作 → 应该改成对应的 SHA（用 fact-derivation 时记录的）
- [ ] 任何依赖 "worktree 还在原 branch" 的 invariant → 必须显式 `git symbolic-ref --short HEAD` 验证
- [ ] 任何 long-running 操作中间 ref 可能被改写 → 操作开头记 SHA，操作结束 verify 还是同一 SHA 或终止
- [ ] **特别**：work tree 模型下，subagent 在 worktree 里 `git checkout` 不影响 repo_root 的 HEAD，所以 R9 (`repo_root HEAD == integration_target`) 不能保护这个攻击面，**必须额外加 worktree 自己的 symbolic-ref check**

**修法**：
- 任何 ref-based 操作 → 默认改 SHA-based。例外：用户能看的输出（log message、commit message body）。
- 所有 worktree-local 操作 → 加 symbolic-ref check 作为前置。
- 把 "branch 概念" 拆成"sha"和"symbolic-ref"两个独立 check，两个都验。

**适用范围扩展**：所有"通过 ref 名查 commit"的操作。常见在：
- merge / rebase / cherry-pick
- git log / git show（一般是只读，但 audit 输出可能依赖）
- worktree 切换 / checkout
- 提交签名验证（验 sha 不是 ref）

T14 揭示这个 attack class 不是新概念（git 用户都知道 ref 可改写），但**在 multi-worktree + long-running orchestrator 上下文里，是一个全新 attack surface**：subagent 在它的 worktree 里搞乱 ref 状态，orchestrator 在 repo_root 里继续按"我们记的 facts"操作 — 中间状态不一致就是 bypass。

---

## G2. G-class extension to merge time (T14 round-1 [P1])

**症状**：T14 `gate7_local_merge` 之前的所有 gate（gate 1/3/4/5/6）都跑完，T11 manifest 验过 working-tree（确认 in-scope），T8 acceptance 在 worktree 内跑过（看到 uncommitted 文件 work），但 T14 `git merge ctx.branch` 只合并 commit 历史的部分 → **未 commit 的内容被静默丢出 integration target**。Codex round-1 抓出。

**根因**：T11 G-class 教训说"facts 必须从 working tree 取，不只 commit history"，T14 也确实用了 facts，但 T14 的关键 op 是 `git merge` —— merge 的输入是**已 commit 的 history**，跟 facts 无关。working tree 的 dirty 状态再次被忽略，但这次是在 op input 而不是 fact source。

**Reviewer 自查（merge-time G2 必查）**：
- [ ] 任何 disk-mutating op (merge / rebase / commit / push)：op 的输入是不是隐含 "已 committed" 的假设？working tree 还有 uncommitted 部分会怎样？
- [ ] 任何 "进 op 之前" 都应有 worktree clean 验证（`git status --porcelain -z` 必须为空），否则有 silent drop 风险
- [ ] subagent 在 worktree 里跑测试 / 执行 acceptance → 测试通过的 invariant 是基于"它看到的 working tree 状态"，但 merge 的 invariant 是基于"branch 的 commit 状态" — 两个状态可能不同，必须在 merge 入口对齐

**修法**：merge 类 op 入口加 4-pre-check 模板：
1. 上层 (parent) HEAD 是预期的 integration target（R9）
2. 任务 worktree 干净（`status --porcelain -z` 空 + `--untracked-files=all`）
3. 任务 worktree HEAD == facts.target_commit_pre_merge
4. 任务 worktree symbolic HEAD == ctx.branch（N-class）

四个全过才 merge。任何一个 fail → blocked，no events emitted。

**与 G-class 关系**：G 是"取事实时要看 working-tree"；G2 是"用事实做 disk op 时也要再看一遍 working-tree"。两阶段都要查，因为 fact-derivation 和 op-execution 之间会有窗口。

---

## M. Shared state files cross-task 污染（T13 round-1 [P2]）

**症状**：T13 `_count_issue_id_in_history` 读 `review-issues.jsonl`（在 slug task dir 共享），统计 canonical id 出现次数判断 churn。但 jsonl 里有所有 task 的 issue（T11/T12/T13 都写）。如果两个 task 的 codex 在同一文件同一行抓出概念上不同但 canonical 化后碰撞的 issue（或同一 task 类）→ 当前 task 还没跑过 3 轮就触发 churn escalate。

**根因**：在共享 state 文件里"按 id 找"是不够的，必须"按 (id, current_scope) 找"。每个 jsonl row 自带 `task` 字段就是为了这种 scope 隔离，但 helper 写的时候忘了用。

**Reviewer 自查**：
- [ ] 任何 helper 读 `*.jsonl` 文件做 count / aggregation → row 上有 `task` / `run_id` / `attempt_id` 字段时，必须按当前 scope 过滤
- [ ] grep 文件里所有 row 字段 vs helper filter 条件，缺哪个 scope 字段
- [ ] "this file is shared at slug level" / "this file is shared at run level" 类注释附近的 helper → scope 隔离是 default 假设，不是 special case

**修法**：

```python
if rec.get("id") == target_id and rec.get("task") == self.task_id:
    count += 1
```

更宽适用：任何 jsonl helper 默认按"caller's natural scope"过滤，需要跨 scope 时显式开关。

---

## O. Same-pid TOCTOU within-second (T15 round-1 [P1])

**症状**：T15 `_complete_9a` 用 `_now_iso()` 给 post_merge checkpoint 取时间戳，但 MergeRunner 的 pre_merge checkpoint 也用 `_now_iso()`。两个 checkpoint 都是相同的 task_dir/checkpoints/<ts>.md 文件名格式。`_now_iso()` 是秒级精度。当 gate 8 acceptance 跑得快（小 task / mock 测试），两个 checkpoint 可能落在同秒 → `write_checkpoint` 抛 `FileExistsError`。但 `task_completed` event 已经写入 → recovery 把 task 当 completed 处理，但 progress.md update + 双 worktree cleanup 全没跑。**fast pipeline 必中**。

**根因**：TOCTOU 通常被理解为"op 之间的时间窗口"，但 same-pid 在 same second 内多次调用 `_now_iso()` 也是 TOCTOU 类——只是窗口短到秒级精度内。秒级 ts 在快速 pipeline 中**保证**碰撞。

**Reviewer 自查（O-class 必查）**：
- [ ] 任何用 ts-as-filename 的 op：所有写入点用同一个 ts 精度 helper（`_now_iso_micro()` 或类似），不要混用秒级和微秒级
- [ ] same-pid 顺序写多个 ts-key 文件：检查两个调用的最大间隔（CPU op 时间通常 <1ms）→ 必须用 µs 或更细粒度
- [ ] 所有 `*.jsonl` event 的 `ts` 字段也应该是 µs（保证多 event 在 same op 内有序排列）
- [ ] `write_checkpoint` / `write_blocked` / 类似的 path-from-ts 助手：检查它们的 filename 派生有没有 collision-handling（retry-on-collision OR 用 µs ts）

**修法**：
- 引入 `_now_iso_micro()` (`%Y-%m-%dT%H:%M:%S.%fZ` UTC) 全局替换 path-from-ts 场景
- 保留 `_now_iso()` 用于 event payload 的 ts 字段（display 友好），但 path-from-ts 必须 µs
- 如果用 retry-on-collision，要有 max_retries + 在 retries 耗尽时 WARN+inconclusive

**适用范围扩展**：所有"ts → filename" / "ts → unique key"模式。常见在：
- checkpoint files
- blocked.md path（如果用 ts）
- jsonl event_id（如果用 ts 派生）
- log file rotation
- worktree id（如果用 ts 派生）

T15 揭示这个 attack class 不是新概念但**在 fast-pipeline 多写入 同 task_dir** 上下文里第一次显式触发。

---

## P. JSONL scope key 必须够细（T15 round-1 [P2]）

**症状**：T15 `Gate8VerificationRunner.verify` 用 `attempt_id=f"post_merge_{run_id}"` 作 acceptance jsonl 的 scope key。`AcceptanceRunner.find_resume_point()` 按 attempt_id filter。但同一 run 中多个 task 都用同 attempt_id → 跨 task 污染：T7 的 acceptance 行被 T8 resume 当成自己的 → resume 跳过本应该跑的 criterion。

**根因**：jsonl scope 的设计是"按 attempt_id 唯一"，但代码生成的 attempt_id 不够唯一（run-scoped 不是 task-scoped）。M-class 类似但更细：M 是"读 jsonl 时按 task 过滤"，P 是"写 jsonl 时 attempt_id 必须 unique 到 task"。

**Reviewer 自查（P-class 必查）**：
- [ ] 任何 jsonl `attempt_id` / `run_id` / `worktree_id` / `event_id` 等 scope key 的生成：必须问 "这个 key 在多少粒度上 unique？" — run-level / task-level / criterion-level
- [ ] 比对消费者的 filter 粒度：如果消费者按 task 过滤但 key 只 run-unique → 漏过滤
- [ ] 跨 task / 跨 run 的 jsonl 文件：每个 row 必须自带足够的 scope 标识（task_id + run_id 双字段）
- [ ] 测试：写一个跨 task 的 fixture，验证 task A 的 row 不会被 task B 的 resume 错读

**修法**：
- attempt_id format: `{phase}_{run_id}_{task_id}` 或 `{phase}_{run_id}_{worktree_id}`
- 每个 jsonl row 至少 3 个 scope 字段：`run_id` + `task_id` + 操作 unit (attempt_id / criterion_idx / event_id)
- 消费者 (`find_resume_point` 类) 必须按全部相关字段 filter，不能只按一个

**与 M-class 关系**：M = 读时按 task 过滤；P = 写时 unique 到 task。两个一起用才能完全防 cross-task。

---

## Q. 过滤 + enumerate = 索引漂移（T15 round-1 [P2]）

**症状**：T15 `Gate8VerificationRunner.verify` 用 `effective = [c for c in criteria if not c.post_merge_skip]` 过滤掉 skip 的 criterion，然后 `for idx, crit in enumerate(effective)` 遍历。`idx` 是 filtered 列表的位置，不是 contract 中的原始 idx。当 contract criterion 0 是 skip、criterion 1 跑：`enumerate(effective)` 给 criterion 1 的 idx=0。审计 jsonl row "criterion_idx=0" → 工程师/recovery 看不出这是哪个 criterion，audit 错位。

**根因**：直觉的"过滤后再遍历"丢了原始位置信息。常见在 `filter` / list comprehension + enumerate 组合。

**Reviewer 自查（Q-class 必查）**：
- [ ] 任何"过滤 + enumerate"模式：必须改成 `[(orig_idx, item) for orig_idx, item in enumerate(items) if predicate(item)]`，保留 (orig_idx, item) tuple
- [ ] 任何 audit log 写入 idx 字段：grep 派生路径，确认是 contract 原始 idx 不是 post-filter idx
- [ ] resume 逻辑用 idx 找记录：必须是 contract 原始 idx
- [ ] 测试：fixture 中第一个 criterion skip，验证后续 criterion 在 audit log 中 idx 对应 contract 原始位置

**修法**：

```python
# Wrong (loses original idx):
effective = [c for c in criteria if not c.skip]
for idx, crit in enumerate(effective):
    audit(idx=idx, ...)  # idx is post-filter, NOT contract idx

# Right (preserves original idx):
indexed = [(i, c) for i, c in enumerate(criteria) if not c.skip]
for orig_idx, crit in indexed:
    audit(idx=orig_idx, ...)  # original contract idx
```

**适用范围扩展**：所有"先 filter 再 iterate" 且 idx 是 audit/recovery 关键的场景。

---

## R. Frontmatter / OSC injection — separator class 半截 + encoding 透传（T16 round-3/4/5）

**症状**：写入操作员可读文件（blocked.md frontmatter / 终端 OSC 9 escape sequence）时，user-controlled string 字段被插入到 YAML frontmatter 行或终端控制序列里。如果 validation 只 reject 部分 line-break-class chars 或 encoding 把 raw bytes 透传，攻击者可以：
- 在 frontmatter 注入伪造行（`block_type="x\rts: forged"` → YAML parser 看到两行）
- 关闭 OSC 9 转义序列后注入新的（`why_blocked="pwn\x07\x1b]0;HACKED\x07"` → 设置终端窗口标题）

T16 codex round 1-4 各抓出一个未覆盖到的字符类：
- round 1: OSC 9 body 完全无 sanitize → 控制字符直插
- round 3: `\r` (block_type) / Unicode separators (U+2028 / U+2029 / NEL / NUL) / `ensure_ascii=False` 让原始 UTF-8 字节透传
- round 4: 还漏 5 个 Python `splitlines()` 边界字符（`\v \f \x1c \x1d \x1e`）

**根因**：
1. **半截 separator class** —— docstring 写"defends Python `splitlines()` boundary"但实际只列了 `\n \r`，漏 vertical tab / form feed / FS / GS / RS / NEL / Unicode line/paragraph separator
2. **Encoding 透传** —— `json.dumps(..., ensure_ascii=False)` 在 frontmatter context 下让 multi-byte UTF-8 line-separator 字节原样输出
3. **Source ASCII 不洁** —— Python source 文件含 raw U+2028/U+2029 字节，源文件本身被 `splitlines()` 工具误读
4. **多个 callers 各写一份不一致 reject 集** —— `block_type` 验证和 `frontmatter_extra` 验证不同步，`block_type` 漏 `\r` 是 pre-existing bug

**Reviewer 自查（R-class 必查）**：
- [ ] 任何"用户字段插入 YAML frontmatter / 终端 escape / shell command"路径必须走 separator-class validation
- [ ] separator class 必须覆盖 Python `str.splitlines()` 完整边界集 11 chars：`\n \r \x00 \x0b \x0c \x1c \x1d \x1e \x85    `
- [ ] 多个验证 callers 必须复用同一个 helper（不能 caller A 拒 `\n`，caller B 拒 `\n\r`）
- [ ] String → output 的 encoding 步骤：考虑用 `ensure_ascii=True` / 显式转义 / allowlist 截断（defense-in-depth）
- [ ] Source files 自己也要保持 ASCII clean（separator literals 用 Python escape `" "` 不要 raw bytes）
- [ ] 测试：把每个 separator char 单独跑一遍 reject 测试，不要只测 `\n` 当代表

**修法**（v0.8.1 reference impl）：

```python
# 1. Module-level constant — single source of truth for the separator class
_FRONTMATTER_LINE_SEPARATORS = (
    "\n", "\r",
    "\x00",                                     # NUL — YAML stream terminator
    "\x0b", "\x0c",                             # VT, FF — splitlines() boundary
    "\x1c", "\x1d", "\x1e",                    # FS, GS, RS — splitlines() boundary
    "\x85",                                     # NEL — YAML 1.2 line-break
    " ", " ",                         # LSEP, PSEP — splitlines() boundary
)

# 2. Shared helper — every validator calls this
def _reject_frontmatter_line_separators(value: str, *, field_name: str) -> None:
    for sep in _FRONTMATTER_LINE_SEPARATORS:
        if sep in value:
            raise ValueError(
                f"{field_name} must not contain line-separator chars; "
                f"frontmatter injection guard"
            )

# 3. Defense-in-depth at encoding step
lines.append(f"{k}: {json.dumps(v, ensure_ascii=True)}")  # not False

# 4. Output truncation for terminal escapes
def _sanitize_osc_text(body: str, max_len: int = 200) -> str:
    safe = "".join(ch for ch in body if 0x20 <= ord(ch) < 0x7f or ch == "\t")
    return safe[:max_len]
```

**适用范围**：
- blocked.md / progress.md / decisions.jsonl frontmatter 写入
- 终端 OSC 9/0/1/2 escape sequences (Notifier Tier 2)
- 任何"YAML / JSON-in-frontmatter / 控制序列"凡 user-controlled string 字段插入

**T16 5-round 收敛痛感**：
- 每轮 fix 引入下一轮 paper cut（半截 reject → 部分 separator 漏 → encoding 透传 → source 脏）
- **真"GREEN"必须迭代到 codex 0 finding** — 4 轮 RED 后才到 GREEN，妥协 YELLOW ship 是技术债
- **新攻击面族在 review 第一遍很难穷举** — 写"frontmatter injection 防御"时只列了 `\n` 也通过了 spec/code review；codex 是唯一独立查 splitlines() spec + Unicode 行分隔标准的来源

---

## S. Wire-up gap — helper 存在但 production 未调用 = safety boundary unreachable（T19 codex round-1 [P2]）

**症状**：safety helpers (`write_auto_prepare_lock` / `consume_auto_prepare_lock` / `detect_X_state` 等) 在 T1-T15 各自被实现 + 单测通过，但 production 路径（`_cmd_auto_execute` / `auto_dispatch_task` 等）从未调用它们。结果：依赖该 helper 的 recovery / classification 路径在生产 unreachable，相应 safety boundary 形同虚设。

T19 暴露的具体例：
- T5 ships `write_auto_prepare_lock` (`flow_state_writer.py:818`) + `consume_auto_prepare_lock` (line 839)
- T19 ships state-2 `auto_prepare_interrupted` 检测（dispatcher 的 R10 路径），但 R10 依赖 lock 在 disk 上才能 fire
- production `auto_dispatch_task` 从来没调用 `write_auto_prepare_lock` / `consume_auto_prepare_lock`
- → state-2 R10 整个 unreachable in production；codex 抓出"crash after worktree create + before auto_engaged 留 orphan worktree 永远孤立"

**根因**：
1. 单元测试只验"helper 行为正确"（例如：lock 写入 → state machine 返回正确状态）；不验"production 调用 helper"
2. 集成测试不存在或不全（v0.8.1 至 T19 才首次有 multi-component integration smoke test）
3. plan 给 happy-path 骨架，但 plan **不能**自动检测"helper has caller"的语义关联
4. spec/code review 看的是 task 内部 + 现有 callers，看不到"应该有 caller 但没"

**Reviewer 自查（S-class 必查）**：
- [ ] 任何 `write_X` / `consume_X` / `detect_X_state` helper：grep `production_module` 找到调用点；如果调用点只在 test 文件，flag "wire-up gap"
- [ ] 任何 recovery / classification path：列出依赖的 helper / state；逐项验证 production 路径写入对应 state（lock / event / journal entry）
- [ ] 任何 `Optional[Notifier] = None` / `dispatch_fn=...` injection 入口：grep production caller 是否真的传 non-None；不传 = path unreachable
- [ ] 任何 `if X is not None: X.do_thing()` 守卫：production caller 是否能让 X 是 None？如果 X 永远是 None，整个 if-branch unreachable
- [ ] 任何 `block_type=...` 或 `state=...` 字面值：grep `==` 或 `in (...)` 验证有 consumer 检查这个值；只有 producer 没 consumer = dead emit

**典型 wire-up gaps**（T19 抓的 + 推测的）：
| Helper | Production caller | T19 当前 |
|--------|-------------------|---------|
| `write_auto_prepare_lock` | `auto_dispatch_task` 中 worktree 创建之前 | ✓ T19 round-1 fix-pass 加上 |
| `consume_auto_prepare_lock` | `auto_dispatch_task` 中 auto_engaged 后 | ✓ T19 round-1 fix-pass 加上 |
| `_invoke_subagent_dispatch` | T22 SKILL 实现 | ⚠ deferred — RuntimeError raise 防 silent skip |
| `Notifier.fire_terminal` | AFK abort / Budget block | ⚠ T17/T18 deferred to v0.8.2 |

**修法**：
- 任何新 task 实现 helper，**必须** 在 ship 前 grep `production caller` 验证调用链 alive
- 任何 task 拓展 production loop，**必须** 列出"本 task 依赖的所有 helper"，逐个验证 production 调用点
- ship-required smoke 必须 include integration scenarios（不只 unit-level helper test）

**与其他 class 区别**：
- **K-class** = implementer 自圆其说跳过 check（plausible justification）；S-class = no caller exists at all（不是跳过，是从未 wire 起来）
- **G-class** = facts 只看部分 disk state；S-class = facts 没看 because no producer wrote it
- **F-class** = fail-open default；S-class = the path that fails open is itself unreachable

T19 wire-up gap 的修复路径标准化为：**helper exists 之后必须立刻 grep + wire production caller，不能等到 integration task**。

---

## 工作流改进建议

把 A-D 写进每个 task 派发的 prompt（implementer 视角）和 reviewer prompt（pre-flight 视角），proactively 防止 codex review 再发现这类 bug。

T3-T22 的 reviewer prompt 模板里加一句：

> 在标准 spec/code review 之外，按 `.flow/pitfalls/claude-review-blindspots.md` 4 类盲点（A 落 Python falsy 陷阱，B 设计跨引用，C 架构顺序，D fallback bypass）做主动自查 grep。

---

## 历史代价 / 收益

- T1: 6 轮 codex（A 类大头 5 轮 + B 类 1 轮）—— 最终触发 path B helper 提取
- T2: 1 轮 codex 抓到 [P1]（C+D1 类）
- T3: 3 轮 codex 全是 D 类 fallback bypass 子家族（D1 → D2 → D3）
  - 教训：D 类有多个 subtype，T1+T2 总结的盲点只覆盖 D1。T3 的 3 轮揭示 D2 (try/except 吞 OSError) + D3 (subprocess rc 谎言) 是独立子家族
- T7: 5 轮（subprocess + HTTP + JSON + file ops 全新模块，D5 catch-all 教训）
- T8: 3 轮（C ordering + D6 status guard）
- T9: 3 轮（**2 个 [P1] 安全 bug** — E shell metachar + F identity 错配）
- T10: 2 轮（slug regex / contract validation）
- T11: 3 轮（**2 个 [P1]** — G working-tree bypass + H porcelain ` -> ` ambiguity）
- T12: 4 轮（J fix-introduces-fix 链式 + I pgkill 复用错过）
- T13: 2 轮（**spec-review GREEN 但 codex 抓 [P1]** — K plausible justification trap + L isinstance type-check + M cross-task pollution）
- T14: 3 轮（**spec+code 双 GREEN 但 codex 连续 2 轮抓 [P1]** — G2 merge-time bypass + N branch-identity bypass）
- T15: 2 轮（codex round-1 抓 [P1] + 3 [P2] — O same-pid TOCTOU + P jsonl scope 不够细 + Q filter+enumerate 漂移 + 重新引入 review-pass 1 修过的 broken-worktree fallback）
- 提取这份 checklist 后，预期 T16-T22：
  - subprocess / 子进程 / 外部工具 check 类的 task：尤其防 D3 + I + K（任何不复用 helper 的 justification = 红旗）
  - 任何 `except` 块：用 D2 + J 自查
  - 任何 JSON / 外部输入字段访问：F + L 双重检查（presence + type）
  - 任何共享 state file 读取：M 自查（按 task 过滤）
  - 任何 disk-mutating op (merge / rebase / commit) 入口：G2 自查（worktree 干净 + HEAD == facts + symbolic-ref == branch）
  - 任何 ref/branch 操作：N 自查（用 SHA 不用 ref + 验 symbolic-ref）
  - 如果某 task 仍 ≥ 3 轮，更新 checklist 补新子家族

## 相关

- `.flow/pitfalls/schema-parsing-get-vs-in.md` — A 类的具体修复方案
- `scripts/flow_contract.py` 顶部 docstring — A 类规则文档化
- Codex consult session: `019dfd48-634c-7720-922d-15313dcc96c7`（T1+T2 都用同一个）

---

## T-class — Codex Counter-Factual Anchoring（反假设锚定）

### 触发条件

Codex review 在**全新的、未发布的代码**上跑多轮 review 时，会基于"假设 parent revision 已部署给真实 operator"来生成 finding，导致：

1. 自相矛盾的 round-by-round 摆动（round-2 抱怨 X，round-3 反过来抱怨 not-X）
2. False-positive [P2] 基于不存在的"existing deployment"
3. Implementer 被诱导无限调整 placeholder / API contract，永远收敛不到 GREEN

### 案例：T22 worktree placeholder（v0.8.1 safety-stack）

- **round-0**（4c3408e）：`{worktree}` = raw → codex F4 [P1]：R-class 注入风险
- **round-1**（b3a937a）：`{worktree}` = `shlex.quote()` → codex round-2 [P2]：破坏 outer-quoted 模板（`--worktree "{worktree}"` 会被 shell 看作字面量带单引号）
- **round-2**（a95278c）：双 placeholder（`{worktree}` raw + `{worktree_quoted}` quoted，文档明示推荐用 quoted variant） → codex round-3 [P2]：raw default 破坏"已基于 b3a937a 部署的 operator"
- **真相**：v0.8.1 还没 ship（master 仍 `15c938b`），**不存在** "existing operator"。round-3 是反假设。

### 修法

- ship gate **只看 [P1]**，[P2] 是 should-fix 不是 must-fix
- round-N+1 与 round-N 自相矛盾时，**先看是否反假设**（finding 论据是否依赖"已有 deployment"，但代码还没发布）
- 反假设盲点案例：disagree-with-rationale 录入 progress.md，**不再 fix**
- Codex review 在 v0.X.0 / v0.X.1 第一次 ship 之前的 review pass 数控制在 ≤3 轮，多于 3 轮且只剩 [P2] 的反假设 finding 就 disagree 收手

### 与其他 class 的区别

- **K-class** = implementer 自圆其说接受 plausible justification；T-class = codex 自圆其说基于不存在的部署历史
- **B-class** = design 跨引用矛盾；T-class = review-rounds 之间矛盾（不同 round 的 codex 互相打架）
- T-class 的特征是 **codex 自身 round-by-round 矛盾**，而非 codex 与 implementer 矛盾

### 应用于派发模板

派 reviewer prompt 时增加一句：
> 如果你与上一轮 codex 的 finding 意见相反，请**显式标注**："我建议 X，与 round-N 的 not-X 立场相反，理由：..." —— 让 implementer 决定 disagree 还是再 fix。
