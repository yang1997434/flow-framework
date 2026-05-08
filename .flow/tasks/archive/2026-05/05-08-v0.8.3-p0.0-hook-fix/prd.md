# v0.8.3 P0.0 hook block-after-PASS root cause fix

> Created: 2026-05-08
> Slug: v0.8.3-p0.0-hook-fix
> Type: backend
> Complexity: complex

## Goal

修复 `~/.claude/hooks/pre-commit-review.sh` 的双向 bug：

- **false-negative**：`<no-op> && git commit ...` 绕过 review gate（regex 只匹配前缀，不识别复合命令）。
- **false-positive**：`python3 <<EOF ... EOF` 等 multi-line 命令体内若任何一行匹配 `git commit` 模式即被拦（`grep -E '^...'` 按行匹配，不识别 shell 语法上下文）。

通过把 hook 升级为 bashlex AST + content-hash marker 双闭合，并硬化 `K_CLASS_SENTINEL_PROHIBITION` 文案补 noop-prefix 模式，解决 v0.8.2 T6.3 暴露的 K-class 红线绕过 + 日常 heredoc 误拦。

## What I already know

- pitfall `hook-blocks-after-reviewer-pass.md` 已完成完整诊断（codex round 4 给出 Option A–G 推荐排序）。
- v0.8.2.1 已在 `dispatch_template.K_CLASS_SENTINEL_PROHIBITION` 加了 "不允许 touch sentinel" 条款；本 task 在其基础上补 noop-prefix 条款（P0.2 预热）。
- Hook 源码：`~/.claude/hooks/pre-commit-review.sh` (symlink → `/home/yangpeng/claude-linux-config/claude/hooks/pre-commit-review.sh`)。
- Marker：`~/.claude/hooks/.review-passed`。
- 当前 hook 启动 budget：< 100ms cold；hook timeout 总额 10s。
- bashlex 是纯 Python，可 vendor。

## Requirements

1. **Hook 7-step 路径（D''''+SoleRoot+WrapperDetect, R5 final）**：
   - Step 1: `\bgit\b` regex pre-screen — no match → PASS。
   - Step 2: `len(command) > 1MB` → BLOCK。
   - Step 3: `bashlex.parse(cmd)` 任何 exception → BLOCK fail-closed。
   - Step 4: Sole-root: `len(trees)==1 AND trees[0].kind=='command'`，否则 BLOCK。
   - Step 5: Strict white-list:
     - 5a: `node.assignments` 必须为空（用 `node.assignments` attr 不是 parts filter）
     - 5b: 任何 word.parts 含 `commandsubstitution` / `processsubstitution` → BLOCK
     - 5c: argv-position resolution（详见 ADR R5 patch）
     - 5d: argv[2:] 严格匹配 `{-m TEXT / --message=TEXT / -F PATH / --file=PATH / --amend / --no-edit / --no-gpg-sign / --allow-empty-message}` 组合
   - Step 6: Marker JSON v=1: `{schema_version, repo_id, head_oid, tree_sha, ts}` — 全等 + 双 TTL + atomic unlink。
   - Step 7: K_CLASS_SENTINEL_PROHIBITION 4-条款文案（详见 ADR）。
2. **Marker schema v=1**：JSON `{schema_version:1, repo_id, head_oid, tree_sha, ts}`；schema_version != 1 → BLOCK；single-use unlink。
3. **Marker writer helper**：reviewer 流程调用；原子 `os.replace(tmp, marker)` 写入；含 `git rev-parse --git-common-dir | Path.resolve()` + `git rev-parse HEAD` + `git write-tree`。
4. **`--amend` 豁免**：在 white-list 内，作为合法 `argv[2:]` 选项之一（不再走单独 regex/path）。
5. **bashlex vendored**：0.18 pinned commit hash 入 `~/.claude/hooks/_vendor/bashlex/`，含 LICENSE 文件 + commit hash 文件 + hook self-test (`python -S` isolated import)。
6. **Wrapper detection (R5 caveat)**：argv[0]≠'git' 时扫描 argv 内任何 word 含 'git'+'commit' 字串 → BLOCK（含 `command/env/eval/bash -c/python -c/nice`）。接受 caveat #1（false positive on benign echo/ls 含字串）。
7. **K_CLASS_SENTINEL_PROHIBITION 硬化**：替换为 R5 final 4-条款（wrap/hide/non-plain/marker-mutate/git-config-bypass），含 hook-maintenance 例外条款。
8. **pytest 套**（≥9 case）：
   - case 1: `git commit -m foo` + valid marker → PASS
   - case 2: `git commit -m foo` + 无 marker → BLOCK
   - case 3: `cat <<'EOF'\n...git commit...\nEOF` (no actual commit) → fail-closed BLOCK
   - case 4: `touch && git commit` (compound) → sole-root BLOCK
   - case 5: `command git commit -m foo` → wrapper BLOCK
   - case 6: `git commit --amend` + valid marker → PASS
   - case 7: marker schema_version=99 → BLOCK
   - case 8: `git -c alias.ci=commit ci -m foo` → BLOCK (`-c` reject)
   - case 9: `git commit -m "$(git add x)"` → cmd-sub BLOCK
   - case 10: HEAD changed since marker → BLOCK
   - case 11: tree changed since marker → BLOCK
   - case 12: env-prefix `PATH=. git commit` → BLOCK (assignments)
   - case 13: marker valid PASS → marker unlinked (single-use 验证)
9. **Spike**：✅ 已完成 → `research/spike-bashlex-perf.md` + `research/bashlex-probe.py`。
10. **5-round codex consult**：✅ 已完成 → `research/codex-consult-r{1..5}-{prompt,response}.md`。Final verdict Y (closed all 9 R3 issues, 1 acceptable caveat)。
11. **CHANGELOG**：v0.8.3 entry 含双向 bug 闭合 + sole-root + 5-round codex consult acknowledgement。
12. **Pitfall 沉淀**：`hook-blocks-after-reviewer-pass.md` last_verified + status=resolved + commit ref + 链接到 5 round codex consult artifacts。

## Acceptance Criteria

- [ ] **Hook 7-step 路径行为矩阵**：≥9 个 pytest case 全 PASS（见 Requirements #8）。
- [ ] **Pre-screen 0-import 路径**：命令无 `git` word 时不 import bashlex；mock/stat 验证启动 < 20ms。
- [ ] **Sole-root simple command 强制**：list/pipeline/subshell/compound/background → BLOCK，单测覆盖。
- [ ] **Wrapper detection**：`command/env/eval/bash -c/python -c` 含 git+commit 字串 → BLOCK，单测覆盖。
- [ ] **Substitution rejection**：`git commit -m "$(...)"` / `<(...)` → BLOCK，单测覆盖。
- [ ] **`-c` rejection**：任何 `git -c X` (含 alias 注入) → BLOCK。
- [ ] **assignments rejection**：`PATH=. / GIT_INDEX_FILE=. git commit` → BLOCK (用 node.assignments)。
- [ ] **Marker schema v=1 强制**：不识别版本号一律 BLOCK。
- [ ] **HEAD oid binding**：reviewer 后 branch switch / rebase → BLOCK。
- [ ] **tree_sha binding**：staged 内容变 → BLOCK。
- [ ] **Single-use marker**：通过校验后 marker 立即 unlink。
- [ ] **Vendored bashlex import**：`python -S -c "sys.path.insert(0, '_vendor'); import bashlex"` 在隔离环境成功。
- [ ] **Spike 数据**：`research/spike-bashlex-perf.md` ✅ 已完成
- [ ] **bashlex probe 数据**：`research/bashlex-probe.py` ✅ 已完成
- [ ] **5-round codex consult artifacts**：`research/codex-consult-r{1..5}-{prompt,response}.{txt,md}` ✅ 已完成
- [ ] **K_CLASS 文案**：4-条款 + hook-maintenance 例外，diff 在 dispatch_template.py。
- [ ] **CHANGELOG**：v0.8.3 entry 写入。
- [ ] **Pitfall metadata**：last_verified + status=resolved + 修复 commit ref + 链接到 codex consult artifacts。
- [ ] **Mandatory opus gate**：✅ Phase 1 5-round codex consult GREEN-equivalent (R5 Y)；Phase 2 实施后 codex review GREEN（state-machine + K-class 红线改动必带）。
- [ ] **Suite 全绿**：基线 944 + 新增 ≥9 个 hook test case → ≥953 PASS。

## Definition of Done

- Tests added/updated where appropriate
- Lint / typecheck / CI green
- Docs/notes updated if behavior changes
- Credential grep self-check passes
- Phase 4 sediment notes filled in (even if "no new ADR/pattern")

## Out of Scope

- **v0.8.3 P0.1** — Round 2+ implementer re-dispatch（worktree state inheritance + cross-round prompt-prefix 传递）。
- **v0.8.3 P0.2** — Subagent brief sentinel-path 全集化（仅本 task 内 K_CLASS 文案补一条 noop-prefix；全集化在 P0.2 系统处理）。
- **v0.8.3 P3** — 5 个内部 CLI literal→constant refactor。
- **v0.8.3 P1** — `build_implementer_prompt` 加 "Before declaring done" checklist + 18-class 标题挂载到 implementer。

## Research References

- `.flow/pitfalls/hook-blocks-after-reviewer-pass.md` — 完整诊断 + Option A–G 列表 + codex round 4 推荐。
- `.flow/pitfalls/subagent-misread-brief-do-not-add-modules.md` — K-class 文案硬化的 category-not-list 教训。
- `research/spike-bashlex-perf.md` — bashlex 0.18 perf + 兼容性 spike，揭示 `<<'EOF'` PARSE_ERROR + 97ms cold import，推翻原 D 一刀切方案，提出 D'+A。✅ 已完成
- `research/spike-bashlex-perf.py` — spike 复现脚本（保留供未来 bashlex 升级 / Python 升级时复测）。

## Decision (ADR-lite)

**Context**: pitfall `hook-blocks-after-reviewer-pass.md` 诊断 hook 双向 bug。原 codex round 4 (在 pitfall 内) 推 Option D = B+F。Phase 1 内做了 5 轮 cross-model codex consult（session `019e078a-...`），每轮迭代设计：
- R1 RED — 原 D 一刀切 AST，`<<'EOF'` 解析全崩 + 6+ critical bypasses
- R2 RED — D'+A (regex 预筛 + AST + first-line fallback) 仍漏 wrapper / env / pathspec 等
- R3 RED — D''+W (white-list) 但 `find_commit_invocation` 逻辑漏 wrapper, env-attr 写错, missing HEAD oid 等 9 个 in-scope 真问题
- R4 RED — D'''' (sole-root + 严格白名单) 仍漏 wrapper（argv[0]≠git fallback 让 `command git commit` PASS）
- **R5 Y** — D''''+SoleRoot+WrapperDetect (Round 5 patch): 9/9 issues closed，仅 1 个 acceptable caveat

R3 也有大量 OUT-OF-SCOPE adversarial findings（token 拆分、brace expansion、ANSI quote、user 配 alias 等），明确不纳入威胁模型 — hook 仅防 LLM accidental bypass，不防 user-shell-level adversary。

**Decision**: 最终方案 **D''''+SoleRoot+WrapperDetect**，7-step 路径：

1. Pre-screen `\bgit\b` regex — no match → PASS
2. Size cap 1MB — over → BLOCK
3. bashlex.parse — exception → BLOCK (fail-closed)
4. **Sole-root simple command** — trees 必须是 1 个 kind=='command' 节点（拒绝 list/pipeline/subshell/compound/background）
5. argv 严格白名单：
   - 5a: `node.assignments` 必须空（拒 env-prefix）
   - 5b: word.parts 不含 `commandsubstitution` / `processsubstitution`
   - 5c: argv-position 解析 — argv[0]≠'git' 时检测 wrapper（任何 argv word 含 'git'+'commit' 字串 → BLOCK）；argv[0]=='git' 时拒 `git -c X` (alias 注入)
   - 5d: argv[2:] 严格匹配 `{-m TEXT, --message=TEXT, -F PATH, --file=PATH, --amend, --no-edit, --no-gpg-sign, --allow-empty-message}` 组合
6. Marker JSON v=1: `{schema_version, repo_id, head_oid, tree_sha, ts}` — 5 项全等 + mtime+ts 双 TTL + atomic unlink (single-use)
7. K_CLASS_SENTINEL_PROHIBITION 改写为 4-条款 + 例外（hook maintenance 显式 user 授权）

被拒绝的方案：
- D / D' / D'' / D''' / D'''' 各版本（详见 R1–R4 codex consult 反馈，已 sediment 到 `research/codex-consult-r{1..5}-{prompt,response}.md`）
- pure Option G (first-line + hash)：拦不住 `touch && git commit`
- pure Option D AST：`<<'EOF'` 全崩
- white-list without sole-root：漏 `git add && git commit` compound + `git commit -m "$(...)"` substitution + multiple-commits

**Consequences**:
- Short-term cost: vendor bashlex 0.18 (228KB pinned commit hash) + 改写 hook (~300 行 Python) + 7-9 pytest case + Phase 1 已完成（5 轮 codex + spike + bashlex probe）。
- Long-term benefit: 双向 bug 同时闭合；K-class 红线 LLM-side 真正强制；marker 加 head_oid + repo_id 防 branch switch / repo cross-use；schema_version 可演进。
- Reversibility: 中等 — 若 sole-root 过严，可放宽为 "白名单 list 节点" 但失去 `git add && commit` 防御。
- Accepted caveats:
  1. **非 git 命令 argv 文本同时含 'git' 和 'commit' 字串会被 BLOCK** (e.g. `echo "git commit and push"`、`ls /tmp/git-commit-logs`)。罕见，user 拆 Bash call 即可。R5 接受。
  2. `<<'EOF'` heredoc + `git commit` 在 body 内会触发 fail-closed（即使 body 内不真正执行 git commit）。罕见。
  3. User 配 `alias.ci=commit` + 用 `git ci` → BLOCK（hook 不读 user git config）。User-config 责任。
  4. Adversarial token splitting (`G=gi; T=t; ...`)、brace expansion、ANSI quotes — 出 LLM accidental scope，hook 不防。
- Risks:
  - bashlex 0.18 GitHub master 仅 ~150 行 yacc 维护；vendor 时 pin commit hash + 加 hook self-test (`python -S` isolated import)。
  - K-class brief 文案 (Step 7) 必须包含 hook maintenance 例外，否则本 task 自身违 brief。
  - R5 caveat #1 false-positive 不能在 hook 端动态判定意图，只能靠 user 调整命令形式。

**Revisit triggers**:
- Hook 因新 git subcommand / shell 语法版本误拦合法命令。
- bashlex 0.18 引入安全 CVE 或暴露 parse 崩溃。
- 出现 R5 caveat #1 高频 false-positive（如 user 频繁 `echo "git commit"` 类用法）。
- v1.0 阶段 hook 设计被整体重构（改为 Claude Code native hook API）。

**Codex consult sessions**:
- All rounds resumed in single session: `019e078a-61da-73a2-a8a8-8274ebc6436f`
- Total tokens: ~80K (R1 17K + R2 21K + R3 42K + R4 ?? + R5 47K stack)
- Final verdict: Y (YELLOW with caveat #1) — equivalent to plan-pass GREEN under accepted caveat

## Technical Notes

- **Files to inspect / modify**:
  - `/home/yangpeng/claude-linux-config/claude/hooks/pre-commit-review.sh`（symlink 目标，主修改点）
  - `~/.claude/hooks/.review-passed`（marker，schema 升级）
  - `~/.claude/hooks/_vendor/bashlex/`（新建 vendored 目录）
  - `claude/dispatch_template.py`（`K_CLASS_SENTINEL_PROHIBITION` 字符串硬化）
  - `tests/hooks/test_pre_commit_review.py`（新建测试套）
  - `CHANGELOG.md`（v0.8.3 entry）
  - `.flow/pitfalls/hook-blocks-after-reviewer-pass.md`（status + last_verified 更新）
- **Constraints**:
  - Hook 启动 < 100ms cold（vendored import 不能拖时间）
  - 单次 AST 解析 budget < 500ms（spike 阈值，hook 总 budget 10s 的 5%）
  - 保留 `--amend` 豁免 + 30 分钟 marker TTL
  - `git write-tree` 必须在 git work tree 内执行；否则 hook 应优雅降级（marker 失效 → BLOCK，不崩溃）
- **Related ADRs / pitfalls**:
  - `.flow/pitfalls/hook-blocks-after-reviewer-pass.md`（本 task 闭合）
  - `.flow/pitfalls/subagent-misread-brief-do-not-add-modules.md`（K-class 文案硬化的姊妹经验）
- **credentials_ref**: N/A
