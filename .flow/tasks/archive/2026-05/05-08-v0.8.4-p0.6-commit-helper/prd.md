# v0.8.4 P0.6: commit-helper utility — marker writer + commit wrapper

> Created: 2026-05-08
> Slug: v0.8.4-p0.6-commit-helper
> Type: backend (dotfiles hook tooling)
> Complexity: simple

## Goal

把今天踩了两次的 marker-write + commit 摩擦工具化。新增
`~/.claude/hooks/_commit_helper.py`：CLI 多子命令，封装 marker 写入 +
跨 repo commit 调用，让主 session / reviewer agent / 自动化脚本一行命令
搞定 review-passed-commit 流程。

## What I already know

**今天踩的具体痛点**:
1. Reviewer agent 第一次写 marker 路径对（`~/.claude/hooks/.review-passed.json`）
   ；第二次写错（`/data/Claude/flow-framework/.review-passed.json`） →
   commit 仍然 block，要重派 agent 修。
2. 主 session commit 三步走：cd worktree → git commit -F file → 出错就回头。
3. Heredoc commit message 被 ParsingError hook 挡（git words 触发） →
   每次必须先 Write 到 /tmp/msg.txt → 再 -F file。
4. `git -C` 被 P0.4 hook 主动 block；compound `cd && git commit` 被 hook 挡。
5. Reviewer agent 调 `_marker_writer.py --help` 在 main 仓 CWD 运作，但跨
   worktree 的 tree_sha 需要在 worktree CWD 算 — 容易混淆。

**现有资产**:
- `~/.claude/hooks/_marker_writer.py` (94 行) — `write_marker()` 函数已存在，
  从 CWD git state 算 tree_sha + 写 `~/.claude/hooks/.review-passed.json`
- `~/.claude/hooks/pre-commit-review.py` (408 行) — hook 主体，验证
  marker 与 staged tree 匹配
- `tests/hooks/test_pre_commit_review.py` (316 行, 25+ tests) — hook 测试范式
  （shell out to hook script，验证各种 argv 组合 BLOCK / PASS）

## Requirements

1. 单文件 `~/.claude/hooks/_commit_helper.py`，多子命令 CLI
2. 复用 `_marker_writer.py::write_marker`（不复制实现）
3. 跨 repo 安全：`--repo /path` 显式声明 target；subprocess `cwd=` 跑
   git，不用 `git -C`（避开 P0.4 hook block）
4. Commit message 必走文件（`-F file` 或 stdin → tmpfile）
5. Fail-loud：marker 写失败 / commit 失败均显式 exit !=0 + actionable msg

## Acceptance Criteria

**CLI 设计**:
- [ ] 三个子命令：
  - `mark [--repo /path]` — 在 target repo 算 tree_sha 写 marker；CWD 默认；输出 tree_sha 到 stdout
  - `commit --repo /path (-F msg-file | --message-stdin)` — 在 target repo 跑 `git commit -F <msg>`；不写 marker
  - `mark-commit --repo /path (-F msg-file | --message-stdin)` — atomic mark + commit (for trusted automation)

**实现细节**:
- [ ] `--repo` 默认 `os.getcwd()`；显式 `--repo /path` 用 `subprocess.run(cwd=path)`；不调 `git -C`
- [ ] marker 复用 `_marker_writer.write_marker()`（必要时改 signature 接受 `cwd` 参数；保持向后兼容）
- [ ] commit 子命令：`subprocess.run(["git", "commit", "-F", msg_file], cwd=repo, check=True)`
- [ ] `--message-stdin` 模式：读 stdin → 写 `tempfile.NamedTemporaryFile(prefix="commit-msg-", suffix=".txt", delete=False)` → 用该文件 `-F`；commit 后 unlink
- [ ] 所有错误用 `print(..., file=sys.stderr) + sys.exit(N)`；exit codes：0 OK，1 git error，2 usage error，3 marker error
- [ ] CLI argparse；`--help` 显示完整范例
- [ ] Shebang `#!/usr/bin/env python3`，chmod 755（与 `_marker_writer.py` 一致）

**Tests**（新增 8 个，落 `tests/hooks/test_commit_helper.py` parallel to test_pre_commit_review.py）:
- [ ] `test_mark_writes_marker_in_cwd_repo` — `mark` 默认 CWD 行为
- [ ] `test_mark_writes_marker_in_named_repo` — `mark --repo /path`
- [ ] `test_mark_outputs_tree_sha_to_stdout`
- [ ] `test_commit_uses_subprocess_cwd_not_git_dash_C` — verify argv 不含 `-C` flag
- [ ] `test_commit_with_msg_file` — `commit --repo /path -F /tmp/msg.txt`
- [ ] `test_commit_with_stdin_message` — `commit --repo /path --message-stdin` + stdin pipe
- [ ] `test_mark_commit_atomic` — `mark-commit` 一次落
- [ ] `test_commit_fails_loud_on_git_error` — repo 没 staged → exit !=0 + stderr msg
- [ ] `test_commit_unlinks_tmpfile_on_stdin_path`

**整合验证**:
- [ ] 端到端：tmp git repo + stage 一个文件 + run `mark-commit` → commit 成功 + marker 已被 hook 单 use unlink（per K-class red-line）
- [ ] 已存 marker 错位时（如旧的 `<repo>/.review-passed.json`）helper 不会被误导（只信 `~/.claude/hooks/.review-passed.json`）

**Doc**:
- [ ] `~/.claude/hooks/_commit_helper.py` docstring 头：用法范例 + 与
  `_marker_writer.py` 的关系
- [ ] dotfiles `CHANGELOG.md` 加 v0.8.4 P0.6 entry
- [ ] 加一段到 `~/.claude/rules/code-delivery.md` 或新建 `code-commit.md`：
  reviewer agent 应该用 `_commit_helper.py mark --repo <path>` 而不是手写
  marker JSON（防今天的"agent 写错路径"复发）

## Definition of Done

- 全套 985+8 = 993 PASS（hook tests 加 8）
- helper 跑 `--help` 输出完整且 actionable
- mypy clean (跳过 — env 没装；与 P0.1/P0.2 同处理)
- codex review GREEN（轻 — 单 helper 不是 state-machine；single round 大概率过）
- pitfall 沉淀：「agent 手写 marker 路径易错」→ 推荐永远用 helper
- dotfiles CHANGELOG 加 entry

## Out of Scope

- 取代 reviewer agent 的"独立审查"语义 — helper 只封装 mark + commit，
  不做 review 决策；调 helper 之前 caller 必须确保已审过
- 修改 `pre-commit-review.py` hook 本身（marker 验证逻辑不动）
- `_marker_writer.py` 大改造（只加 optional `cwd` 参数；保持现有 `main()` 行为）
- v0.8.4 P0.7 / P3

## Decision (ADR-lite)

**Context**: 今天 P0.2 ship 中 reviewer agent 错路径写 marker 一次，
主 session 跨 worktree commit 摩擦数次。两个独立 friction 但同一根因：
没有官方 helper 把 marker + commit 工作流封成一个 right-shaped tool。

**Decision**: 新增 `~/.claude/hooks/_commit_helper.py`，多子命令 CLI；
`mark` / `commit` / `mark-commit`；`--repo` 显式 + subprocess `cwd=`；
不引入 `git -C`（P0.4 hook 主动 block）。复用 `_marker_writer.write_marker`
（小改造加 `cwd` 参数）。

**Consequences**:
- Short: 单文件 + 8 tests + 1 docstring update + 1 CHANGELOG entry，体量小
- Long: 减少跨 worktree commit / agent marker-write 的 tooling 摩擦；
  "agent 写错 marker"类失败显著降低
- Reversibility: 高 — pure additive；rollback 删文件即可

**Revisit triggers**:
- 多 reviewer / 多 marker 模型出现（如不同 reviewer 写不同 marker）→
  可能需重新设计 marker 路径 schema
- 出现 helper 应该自身做 review 的需求（不应该；helper 是 plumbing 不是 policy）

## Technical Notes

**Files to create**:
- `~/.claude/hooks/_commit_helper.py`（dotfiles 仓 `claude/hooks/_commit_helper.py`）
- `tests/hooks/test_commit_helper.py`（flow-framework 仓）

**Files to modify**:
- `~/.claude/hooks/_marker_writer.py` — `write_marker(cwd: Optional[Path]=None)` 加可选参数
- dotfiles `CHANGELOG.md` — v0.8.4 P0.6 entry
- `~/.claude/rules/code-delivery.md`（或新建 `code-commit.md`）—
  reviewer agent 改用 helper

**Constraints**:
- 跨 2 仓改动（dotfiles 主载体 + flow-framework tests + rule 也在 dotfiles）
- 仍走 mandatory pre-commit-review hook — 提交 helper 自身时也要走 marker
- Worktree 内文件 Edit 必用 worktree 前缀（沿用 P0.1 pitfall）

**Related**:
- `feedback_failclosed_template_wireup.md` — multi-layer pattern 不直接适用
  （helper 不做 template substitution）
- 沉淀 candidate pitfall: `agent-marker-write-path-mistake.md` —
  reviewer agent 不应手写 marker JSON，应永远调 helper
- credentials_ref: N/A
