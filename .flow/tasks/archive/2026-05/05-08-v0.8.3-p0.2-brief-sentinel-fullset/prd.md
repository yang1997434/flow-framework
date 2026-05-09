# v0.8.3 P0.2: dispatch shim wire-up — prompt_prefix transport via file placeholder

> Created: 2026-05-08
> Slug: v0.8.3-p0.2-brief-sentinel-fullset *(slug 名沿用，但 scope 已收窄到 wire-up — 见 Decision)*
> Type: backend
> Complexity: moderate

## Goal

让 P0.0 写好的 K_CLASS sentinel-prohibition prefix（以及未来任何
`prompt_prefix`）真正进入 dispatched subagent 的 prompt — 当前在
dispatch shim 边界被 `**_kw` 静默吞掉。修复后 P0.0 (K_CLASS guard) +
P0.1 (fresh-per-round redispatch) 的 dispatch-time 价值才真正兑现。

## What I already know（recon 已 grep-verified）

**Brief 生成端（P0.0/P0.1 工作产物）**：
- `scripts/dispatch_template.py:58–88` — `K_CLASS_SENTINEL_PROHIBITION`
  4-clause 常量（commit `52a580c`，CI test 钉住）。
- `scripts/dispatch_template.py:128` `build_implementer_prompt(...)` —
  自动 prepend K_CLASS（除非 `is_doc_only=True`）。
- `scripts/flow_orchestrator.py:5160` `_phase2_dispatch` Round 2+ 的
  `prefix = build_implementer_prompt(...)` 调用 — 正确产出 prefix 字符串。
- `scripts/flow_orchestrator.py:5409` `_dispatch_implementer_fresh_worktree`
  → `_invoke_subagent_dispatch(ctx, ..., prompt_prefix=prompt_prefix)`
  → `mod.invoke(ctx, **kw)` — 正确把 prefix 透传到 invoke。

**Dispatch 边界（gap 所在）**：
- `scripts/flow_subagent_dispatch.py:153` `def invoke(ctx, *,
  subagent_env=None, task_id=None, **_kw)` — 接受任何 kwarg 但
  template 只 substitute `{slug,task_id,worktree,worktree_quoted}`，
  无 `{prompt_prefix*}` 占位符 → `prompt_prefix` 落入 `**_kw` **静默丢弃**。
- `scripts/flow_orchestrator.py:900` `auto_dispatch_task` 内的
  `dispatch_fn(ctx, subagent_env=subagent_env, task_id=manifest.id)` —
  Round 1 路径**完全不传** `prompt_prefix`。

**结论**：K_CLASS guard 在 Round 1 + Round 2+ 两条路径都从未抵达
dispatched subagent 的 prompt；现有 test 监控的是 fake `_impl(prompt_prefix)`
（`tests/smoke/test_e2e_v0_8_2_p0.py:415` 等），覆盖不到 shim → subprocess
端到端的 wire。

**其他 brief 站点**（recon classification）：
- `build_reviewer_prompt` — N/A（reviewer 读 diff 不 touch sentinels；
  J-class 设计原则与 K-class 隔离）
- `_render_task_brief` — N/A（pure content helper，最终经
  `build_implementer_prompt` 添 guard）
- `cmd_render_prompts` / `check_rendered_prompts` /
  `_dispatch_method` — N/A（非 subagent brief 生成器）
- 结论：原"brief 全集化"任务只有一处真 gap（dispatch wire-up）。

## Requirements

1. `prompt_prefix` 经文件载体抵达 subagent，可 forensic（落盘 evidence）。
2. 现有 operator template 不带 `{prompt_prefix_file}` 占位符且 prefix 非
   空时 → fail-closed (raise RuntimeError)；防 silent-drop 复发。
3. Round 1 路径也补上：`auto_dispatch_task` → `dispatch_fn` 必须传 prefix。
4. Backwards compat：`prompt_prefix == ""` / 缺省 → 行为不变（现有 tests
   不需大改）。

## Acceptance Criteria

**Wire-up — file location（codex P0#1 修正）**：
- [ ] **Prefix file 写到 `<repo_root>/.flow/.runtime/<slug>+<task_id>+r<round>/dispatch_prefix.txt`，
      NOT 写到 worktree 内**。理由：worktree 内任何 untracked 文件会被
      `derive_task_facts` 抓到 → `verify_manifest_against_facts` 直接 row 4
      `manifest_violation` block 整个 task。`<repo_root>/.flow/.runtime/` 已被
      `.gitignore`（须验证；若否则 PRD 加一项 gitignore 补丁），且
      worktree 的 `git diff` / `git status` 范围不达 repo_root 上层。
- [ ] Path 由 `flow_subagent_dispatch.invoke()` 内部计算（不暴露给 caller）：
      `runtime_dir = repo_root / ".flow" / ".runtime" / f"{slug}+{task_id}+r{round_num}"`；
      `repo_root` 从 `ctx.worktree_path` 反推（worktree 是 `<repo_root>/.claude/worktrees/<name>` 模式）。
- [ ] **Layout assertion (codex R2 P1#1)**：`invoke()` 内 verify
      `worktree_path.parent.name == "worktrees"` AND `worktree_path.parent.parent.name == ".claude"`；
      不匹配（如未来 verify worktree `<repo>/.claude/worktrees/verify/...` 误用）→
      raise `RuntimeError` with actionable msg；防 silent-misroute 到错误 repo_root。
- [ ] 写 round-discriminated 路径，避免 Round 1 + Round 2+ 互覆。
- [ ] 父目录 `mkdir(parents=True, exist_ok=True)`；UTF-8 编码；写完 fsync 不必要。

**Wire-up — invoke 签名 / fail-closed（codex P0#2/#3/#4 修正）**：
- [ ] `flow_subagent_dispatch.invoke()` 签名：
      ```python
      def invoke(ctx, *, subagent_env=None, task_id=None,
                 prompt_prefix: str = "", round_num: int = 1) -> None:
      ```
      **NO `**_kw`** — codex P0#2: `**_kw` 是 silent-drop class 的复发风险，
      未知 kwarg 显式 raise `TypeError`。
- [ ] `prompt_prefix` 类型校验：非 `str` (含 None / bytes) → 在文件 / subprocess
      副作用之前 raise `TypeError`（codex P0#4）。
- [ ] **Fail-closed 用 `string.Formatter().parse()` 而不是 substring**（codex P0#3）：
      - `prompt_prefix != ""` AND `{prompt_prefix_file}` 不是 template 中真实
        format field（被注释 `# {prompt_prefix_file}`、shell-escaped `\{...\}`、
        双花括号 `{{...}}`、字符串里出现等都不算）→ raise `RuntimeError`。
      - 实现：`fields = {field for _, field, _, _ in string.Formatter().parse(template) if field is not None}`；
        `if "prompt_prefix_file" not in fields: raise`。
- [ ] `prompt_prefix == ""` / 没传 → 无文件副作用 + template 可不含占位符
      （backwards compat）。

**Wire-up — Round 1 path（auto_dispatch_task）**：
- [ ] `auto_dispatch_task` 加可选参数 `prompt_prefix: str = ""`，透传到 `dispatch_fn`。
- [ ] `_cmd_auto_execute` 内 build prefix **必须在 `_task_already_completed`
      跳过 + `CrashRecoveryDispatcher.classify()` 决定 proceed 之后、立即在
      `auto_dispatch_task` 调用前**（codex R2 P1#2 — 避免在 task 已完成 /
      pre-lock 失败 / fail-closed-interactive 路径上做 prefix-build 副作用）：
      ```python
      brief = _render_task_brief(task_dir=task_dir, criteria=criteria)
      prefix = build_implementer_prompt(task_brief=brief,
                                         is_first_pass=True, is_doc_only=False)
      outcome = auto_dispatch_task(..., prompt_prefix=prefix)
      ```
- [ ] 传入 `round_num=1` 用于路径 discriminator。

**Tests**（新增 9 个 — codex P0/P1/adversarial 全覆盖）：
- [ ] Unit `test_subagent_dispatch_shim.py`:
  - [ ] `test_invoke_writes_prefix_file_at_repo_root_runtime` — 验证文件落到
        `<repo_root>/.flow/.runtime/<slug>+<task_id>+r<round>/dispatch_prefix.txt`
        而不是 worktree 内
  - [ ] `test_invoke_substitutes_prefix_file_placeholder` — Formatter().parse() 真 substitute；
        rendered `cmd_str` 含 quoted absolute path
  - [ ] `test_invoke_raises_when_prefix_nonempty_template_lacks_placeholder` —
        substring 出现但非真 format field（注释、`{{...}}`、字符串字面量）
        都必须 raise（4 个子断言，codex adversarial）
  - [ ] `test_invoke_raises_on_unknown_kwargs` — 任何未声明 kwarg → TypeError（codex P0#2）
  - [ ] `test_invoke_raises_on_non_str_prefix` — None / bytes / int prefix → TypeError before side-effect（codex P0#4）
  - [ ] `test_invoke_no_file_when_prefix_empty` — backwards-compat
  - [ ] `test_invoke_round_discriminator_in_path` — 同 slug + task_id, round_num=1 vs 2 → 不同 path
  - [ ] `test_invoke_prefix_file_byte_for_byte` — 写入文件内容 == 输入 prefix
        UTF-8 byte sequence；assert 无 BOM、无 CRLF 混入、无 trailing newline 篡改（codex R2 AC delta#1）
  - [ ] `test_invoke_path_contains_dot_runtime` — assert path str 含 `/.flow/.runtime/`
        子串（catch 未来漏点 typo；codex R2 AC delta#2）
  - [ ] `test_invoke_raises_on_unexpected_worktree_layout` — worktree_path
        位于 `<repo>/.claude/wt/<n>` 或 `<repo>/.claude/worktrees/verify/<n>` →
        layout assertion raise（codex R2 P1#1）

- [ ] Integration `test_v083_p02_dispatch_wireup.py`（real tmp git）:
  - [ ] `test_round1_auto_dispatch_passes_prefix_through` — `_cmd_auto_execute`
        走 Round 1，**断言 outcome 不是 `manifest_violation`**（codex AC delta）；
        验证 `<repo_root>/.flow/.runtime/<slug>+<task_id>+r1/dispatch_prefix.txt`
        存在 + 内容含 `K_CLASS_SENTINEL_PROHIBITION`；验证 file 路径**不在**
        `TaskFacts.changed_files / newly_added_files` 中（codex AC delta）
  - [ ] `test_round2_fresh_worktree_passes_prefix_through` —
        `_dispatch_implementer_fresh_worktree` 走完，验证 `+r2` 路径文件存在 + 内容
        含 reviewer feedback 拼接

**Doc**：
- [ ] `claude/skills/flow/flow-phase2-execute/SKILL.md` § "Implementer prompt
      — K-class sentinel prohibition" 加段：transport 通过
      `{prompt_prefix_file}` 占位符 + 仓库 runtime dir 文件载体；operator
      template 范例必须**真把内容拼进 prompt**（codex adversarial）：
      ```
      claude -p "$(cat {prompt_prefix_file})

      flow:flow-phase2-execute --slug {slug} --task {task_id} --worktree {worktree_quoted}"
      ```
      警告：仅在 template 里 mention `{prompt_prefix_file}` 而不真 cat 进 prompt
      会让 K_CLASS guard 静默失效（fail-closed 只能保证 path 出现在 cmd 里，
      operator 责任是真用它）。
- [ ] `flow_subagent_dispatch.py` `_resolve_cmd_template` docstring + `RuntimeError`
      文案：注明 `{prompt_prefix_file}` 已经 `shlex.quote()`-wrapped，operator
      不要再加 shell quote（codex P1）。
- [ ] **`claude/capabilities/defaults.json`** 内 `autonomy_orchestrator` 说明文档
      / placeholder list 更新（codex P1#1）；example template 用新形式。
- [ ] CHANGELOG `v0.8.3 P0.2` 条目（breaking change 警告 + 迁移示例）。

**回归**：
- [ ] `tests/smoke/test_subagent_dispatch_shim.py` 既有用例全 PASS（不传 prefix 路径）。
- [ ] `tests/smoke/test_dispatch_template.py` 全 PASS（K_CLASS 内容 invariant 不变）。
- [ ] `tests/smoke/test_v083_p01_implementer_redispatch.py` 全 PASS。
- [ ] 全套 990+12 = 1002 PASS（5 unit + 4 加固 + 1 path-typo guard + 2 integration）。

## Definition of Done

- 全套 1002 PASS（969 baseline + 21 P0.1 + 12 新 P0.2）
- mypy clean
- codex review GREEN（mandatory opus gate — state-machine + dispatch boundary 改动）
- pitfall 沉淀：「dispatch shim **_kw silent-drop 模式」加入 pitfalls/
- CHANGELOG 加 breaking change 警告
- Phase 4 sediment 填 ADR

## Out of Scope

- 其他 brief 生成站点的 4-clause 全集化（recon 已确认 N/A —
  reviewer / render_task_brief / install / selftest / acceptance 都不涉及 sentinel）
- subagent SKILL.md 内的 prefix-reading 逻辑（operator template 通过
  shell `$(cat ...)` 在 CLI prompt 边界注入；SKILL 自身无需感知文件）
- `<repo_root>/.flow/.runtime/<...>/dispatch_prefix.txt` 主动清理（保留为 forensic；
  task end 时整 `.flow/.runtime/<slug>+...` 目录可独立清理 — 后续 task 范围）
- P0.7 parallel speculation
- v0.8.2 carried 的 P3 CLI literal→constant refactor

## Research References

- 本次 recon 输出（写入 conversation；不另存独立 research/ 文件）

## Decision (ADR-lite)

**Context**: P0.0 加了 K_CLASS_SENTINEL_PROHIBITION 4-clause 到
`build_implementer_prompt`，P0.1 加了 fresh-worktree-per-round redispatch
传 `prompt_prefix`。两个工作都假设 prefix 会到 subagent，但 dispatch
shim `flow_subagent_dispatch.invoke` 的 cmd template 只认
`{slug,task_id,worktree,worktree_quoted}`，`prompt_prefix` 落入 `**_kw`
被静默吞。Round 1 `auto_dispatch_task` 更是根本不传 prompt_prefix。
当前 K_CLASS guard 在 dispatch 边界完全是 dead code。

**Decision**: 文件载体 + 新占位符 `{prompt_prefix_file}` + fail-closed
（强化版，吸纳 codex R1）。
- **Transport path**: `<repo_root>/.flow/.runtime/<slug>+<task_id>+r<round>/dispatch_prefix.txt`
  （**NOT** in worktree — worktree 内文件会触发 manifest_violation row 4；
  repo_root/.flow/ 是 gitignored 且超出 worktree 范围，安全 + forensic）。
- **Template**: 加 `{prompt_prefix_file}` 占位符（shlex.quote 包装）；
  fail-closed 用 `string.Formatter().parse()` 校验是真 format field（不是
  注释 / 双花括号 / 字符串字面量）。
- **Invoke 签名**: 显式 `prompt_prefix: str = ""`，**移除 `**_kw`**（codex
  P0#2 — 防 silent-drop 类复发；未知 kwarg → TypeError）；prompt_prefix
  类型校验（非 str → TypeError before side-effect）。
- **Round 1**: `auto_dispatch_task` 加 optional `prompt_prefix` 参数；
  `_cmd_auto_execute` build prefix 后透传。
- 拒绝方案：env var (3 票否决 — 不可 forensic + 难 fail-closed)，
  argv 直传 (K_CLASS prefix ~50 行 → length / escape 风险)，
  worktree 内路径 (codex R1 P0#1 — manifest_violation 自爆)。

**Consequences**:
- Short-term cost: operator template 必须加 `{prompt_prefix_file}`
  占位符（breaking change，但现产部署 capability default 是 `{}` 空，
  零真实用户被打到；CHANGELOG 醒目标注）。`auto_dispatch_task` 加
  可选参数（默认 ""）→ 老 test 不破。
- Long-term benefit: K_CLASS guard 真正生效；后续任何
  `prompt_prefix` 增改都自动经此 wire；fail-closed 防新 silent-drop。
- Reversibility: 高 — invoke 内部 + 一处 orchestrator wire-up；
  rollback 只需 revert commit。

**Revisit triggers**:
- 出现 multi-prefix 需求（implementer + auditor 各一份 prefix）→
  `{prompt_prefix_file}` 升级为 list/dict
- subagent 需要在 prompt 之外读其他 dispatch metadata（runtime hints
  / tracing IDs）→ 从单文件升级为 `<worktree>/.flow/dispatch_meta/` 目录
- env-var 方案需求重新出现（如 CI cache）→ 重看 trade-off

## Technical Notes

**Files to modify**：
- `scripts/flow_subagent_dispatch.py` — `invoke` 签名 + 文件写入 +
  template substitution + fail-closed check + docstring/error msg
- `scripts/flow_orchestrator.py` — `auto_dispatch_task` 加
  `prompt_prefix` 参数 + `_cmd_auto_execute` build prefix + 传入
- `claude/skills/flow/flow-phase2-execute/SKILL.md` — 加 transport 段 + operator template 范例
- `tests/smoke/test_subagent_dispatch_shim.py` — 5 个新 unit
- `tests/smoke/test_v083_p02_dispatch_wireup.py` — 新文件，2 个 integration
- `CHANGELOG.md` — P0.2 条目 + breaking change 警告

**Files to inspect (no edit)**：
- `scripts/dispatch_template.py` — K_CLASS 文本 / `build_implementer_prompt` 调用约定
- `scripts/flow_orchestrator.py:5160` `_phase2_dispatch` — Round 2+ 已 build prefix（无需改）
- `tests/smoke/test_e2e_v0_8_2_p0.py:415` — 现有 fake `_impl(prompt_prefix)` 断言路径

**Constraints**:
- Mandatory opus gate（state-machine + dispatch boundary）
- `dispatch_template.py` K_CLASS 文本是 invariant — test 钉死，不动
- `auto_dispatch_task` 现有 test 必须不破（默认 `prompt_prefix=""` 路径）
- Fail-closed message 必须可执行（告诉 operator 如何在 template 里加占位符）

**Pre-implement 验证（codex R1 派生）— 已完成**:
- ✅ `.gitignore:21` 已含 `.flow/.runtime/` — 用此路径 zero gitignore 改动
- ✅ `_cmd_auto_execute` line 5906-5914: `task_dir`, `repo_root`, `criteria`
      均已在 scope；prefix build 插在 `auto_dispatch_task` 调用前
- ✅ Worktree path 约定：`<repo_root>/.claude/worktrees/<id>/`
      （`create_task_worktree:456`）；`worktree_path.parents[2] = repo_root`
      stable；`flow_subagent_dispatch.invoke` 内可安全反推

**Related ADRs / pitfalls**:
- P0.0 K_CLASS prohibition 设计
- P0.1 fresh-worktree-per-round redispatch
- 本次将新增 pitfall: `dispatch-shim-silent-kw-drop.md`
  - trigger_paths: `scripts/flow_subagent_dispatch.py`,
    `scripts/flow_orchestrator.py` 中调 dispatch_fn 的位置
  - 教训: shim 接受 `**_kw` 但 template 不引用即静默丢弃；新增 kwarg
    必须同时加 placeholder + fail-closed assertion

- credentials_ref: N/A
