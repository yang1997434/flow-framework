---
slug: v0.8.3-p0.0-hook-fix
status: active   # active | paused | blocked | done
phase: implement    # triage | research | implement | check | verify | sediment
# blocked_by: list of task slugs this task depends on. Used by `flow task status`
# to draw the dependency graph (parent slugs must finish first). Default: empty.
# Example:
#   blocked_by:
#     - capability-registry-and-model-roles
#     - prereq-installer-and-doctor
blocked_by: []
---

# progress.md — v0.8.3-p0.0-hook-fix

## Plan

**Phase 1 done — 2026-05-08 (after 5-round codex consult)**

- Triage: complex / backend / hook infrastructure
- Brainstorm: 2 user-confirmed decisions（parser D 优先 G fallback / scope 全包 / hash write-tree / vendor bashlex / marker JSON v1）
- Spike (research/spike-bashlex-perf.md, .py): bashlex 0.18 perf + 兼容性测得
  - 🔴 `<<'EOF'` quoted heredoc 3/3 PARSE_ERROR
  - 🟡 cold import 97ms 占 hook budget 97%
  - 🟢 200KB unquoted heredoc 解析 44ms
- bashlex AST probe (research/bashlex-probe.py): 24 案例验证 dequote / assignments / substitution 行为
- 5-round codex consult (`019e078a-...`)：
  - R1 RED — 6+ critical bypasses on naive D
  - R2 RED — D'+A 漏 wrapper / env / pathspec 等
  - R3 RED — D''+W (white-list) 9 个 in-scope 真问题
  - R4 RED — D''''+SoleRoot 漏 wrapper fallback
  - **R5 Y** — D''''+SoleRoot+WrapperDetect closes 9/9, 1 caveat acceptable
- ADR 最终决议 (PRD ADR-lite)：D''''+SoleRoot+WrapperDetect 7-step 路径
  1. `\bgit\b` pre-screen → no match PASS
  2. 1MB size cap
  3. bashlex parse fail-closed
  4. Sole-root simple command
  5. Strict white-list (assignments/substitution/argv-position/args)
  6. Marker JSON v=1 含 head_oid + tree_sha + repo_id, single-use unlink
  7. K_CLASS brief 4-条款 + hook-maintenance 例外
- Mode: single, main session implements（hook + test + 文案为强耦合区，不并行）

### Phase 2 step plan (post 5-round codex consult, R5 Y verdict)

| 步骤 | scope | 依赖 |
|------|-------|------|
| S1 | Vendor bashlex 0.18: clone GitHub master，pin commit hash（写 `_vendor/bashlex/COMMIT_HASH`），copy bashlex/ 源目录 + LICENSE 入 `~/.claude/hooks/_vendor/bashlex/` | — |
| S2 | Hook self-test：`~/.claude/hooks/_vendor/_selftest.py`，`python -S` 隔离 import bashlex + 解析 5 个代表命令 | S1 |
| S3 | 重写 `pre-commit-review.sh` → Python `pre-commit-review.py`，含完整 7-step 路径。`.sh` 改为 thin shim 调 `.py` | S1, S2 |
| S4 | Marker writer helper `~/.claude/hooks/_marker_writer.py`：原子 `os.replace`，写完整 schema | S3 |
| S5 | pytest 套 `tests/hooks/test_pre_commit_review.py`：≥13 case 矩阵（见 PRD Acceptance） | S3, S4 |
| S6 | 硬化 `scripts/dispatch_template.py::K_CLASS_SENTINEL_PROHIBITION` 文案为 R5 final 4-条款 | — |
| S7 | CHANGELOG.md v0.8.3 entry | S3, S6 |
| S8 | Pitfall `hook-blocks-after-reviewer-pass.md` metadata 更新 | S7 |
| S9 | Mandatory opus gate (Phase 3 边界)：codex review on full diff (S1–S8) 必须 GREEN | S1–S8 |

执行模式：sequential，不并行（hook + test + 文案为强耦合区，单 session 顺序更易跟踪）。

**跨 repo 边界**（Phase 1 末发现）：
- `~/.claude/hooks/pre-commit-review.sh` symlink → `/home/yangpeng/claude-linux-config/claude/hooks/pre-commit-review.sh`，**dotfiles 在独立 git repo**
- S1, S2, S3, S4 改动落在 `claude-linux-config` repo（hook + vendor + marker writer）
- S5, S6, S7, S8 改动落在 `flow-framework` repo（tests + dispatch_template + CHANGELOG + pitfall）
- 提交流程：单 session 实施完所有 9 步 → 两次 reviewer + commit（先 dotfiles，再 flow-framework）
- progress.md 在 flow-framework repo，最终记录两个 repo 的 commit ref

Phase 1 codex consult artifact summary：
- Session: `019e078a-61da-73a2-a8a8-8274ebc6436f` (5 rounds resumed)
- Files: `research/codex-consult-r{1..5}-{prompt,response}.{txt,md}` + `research/spike-bashlex-perf.{md,py}` + `research/bashlex-probe.py`
- Final verdict: **R5 Y** — 9/9 R3 issues closed, 1 acceptable caveat (非 git 命令 argv 文本含 git+commit 字串 false positive)

## Execute Log

<!-- TEMPLATE: 未填写。Phase 2 渐进 append。每个 sub-agent / 主 session 完成一段工作时追加一行。 -->

<!-- 表格示例（首行为表头，自动生效）：
| 时间 (YYYY-MM-DD HH:MM) | Agent | Scope | Outcome |
|------|-------|-------|---------|
-->

## Verify Report

<!-- TEMPLATE: 未填写。Phase 3 末写。各项必须有具体值（pass / fail / 跳过原因），不能留 pending。 -->

## Sediment Notes

<!-- TEMPLATE: 未填写。Phase 4 末写。强制写一段——即使"no new sediment"也要明确写。 -->

## Retro (optional)

<!-- TEMPLATE: 自由格式回顾——什么 worked / didn't / 框架反馈。可省略。 -->

## Files Touched

_Updated 2026-05-08 08:40 (last 20 unique edits)_:

- `/tmp/dt-c.py`
- `/tmp/dotfiles-commit-msg.txt`
- `/tmp/codex-review-v083-p00-r2.txt`
- `tests/hooks/test_pre_commit_review.py`
- `/home/yangpeng/claude-linux-config/claude/hooks/pre-commit-review.py`
- `/tmp/codex-review-v083-p00.txt`
- `/tmp/hook-quick-test.py`
- `tests/smoke/test_dispatch_template.py`
- `scripts/dispatch_template.py`
- `.flow/pitfalls/hook-blocks-after-reviewer-pass.md`
- `CHANGELOG.md`
- `/home/yangpeng/claude-linux-config/claude/hooks/pre-commit-review.sh`
- `/home/yangpeng/claude-linux-config/claude/hooks/_marker_writer.py`
- `/home/yangpeng/claude-linux-config/claude/hooks/_vendor/_selftest.py`
- `.flow/tasks/05-08-v0.8.3-p0.0-hook-fix/progress.md`
- `/tmp/codex-consult-v083-p00-r5.txt`
- `/tmp/codex-consult-v083-p00-r4.txt`
- `/tmp/codex-consult-v083-p00-r3.txt`
- `/tmp/bashlex-probe.py`
- `/tmp/codex-consult-v083-p00-r2.txt`
