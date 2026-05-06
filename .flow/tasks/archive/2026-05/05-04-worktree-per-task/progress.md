---
slug: worktree-per-task
status: done
phase: sediment
blocked_by: []
---

# progress.md — worktree-per-task

## Plan

(single, sub-agent A implements) — sub-project #4 of v0.4 audit-flow-issues.

Scope:
- `scripts/flow_task.py` — `cmd_create` adds isolation (worktree | branch | shared); add `cmd_status` (tree + deps); add `cmd_switch` (eval-friendly cd); `cmd_archive` cleans up worktree
- `templates/flow.config.yaml.template` — add `task_isolation: shared` default
- `templates/progress.md.template` — add YAML frontmatter (slug / status / phase / blocked_by)
- `tests/smoke/test_worktree_isolation.py` — 5 tests covering fallback, real worktree, status, switch, archive cleanup

Out of scope (公共件):
- install.sh / flow_install.py / flow.py (主 session)
- dependencies.json / capability defaults
- hook scripts / Skills / commands MD

## Execute Log

| 时间 (YYYY-MM-DD HH:MM) | Agent | Scope | Outcome |
|------|-------|-------|---------|
| 2026-05-04 06:30 | sub-agent A | read PRD + flow_task.py + paths/config helpers | clarified isolation semantics, frontmatter location |
| 2026-05-04 06:35 | sub-agent A | edit `templates/flow.config.yaml.template` | added `task_isolation: shared` block + comments explaining 3 modes |
| 2026-05-04 06:36 | sub-agent A | edit `templates/progress.md.template` | added YAML frontmatter with slug/status/phase/blocked_by |
| 2026-05-04 06:40 | sub-agent A | rewrite `scripts/flow_task.py` | new helpers _is_git_repo / _resolve_isolation_mode / _create_worktree / _remove_worktree / _read_location / _write_location / _parse_progress_frontmatter / _slug_of; updated cmd_create + cmd_archive; new cmd_status + cmd_switch; preserved P0-2 archive-pointer ordering fix |
| 2026-05-04 06:50 | sub-agent A | new `tests/smoke/test_worktree_isolation.py` | 5 unittest classes — NonGitFallback, GitWorktreeCreated, StatusOutput, SwitchOutput, ArchiveCleansWorktree |
| 2026-05-04 06:55 | sub-agent A | run my new tests + P0 regressions | 15/15 PASS (5 new + 10 existing P0) |
| 2026-05-04 07:00 | sub-agent A | sync deltas to worktree path (initial Edits landed in main repo by absolute path) | both main repo and worktree branch carry identical content |

## Verify Report

- **New tests (5)**: PASS
  - `NonGitFallback.test_create_falls_back_to_shared` — non-git + worktree mode → falls back, writes `.location=project_root`, prints WARN
  - `GitWorktreeCreated.test_worktree_created_and_location_recorded` — git + worktree mode → real `../<repo>-flow-<slug>` dir + branch `flow/<slug>` + `.location` records abs path
  - `StatusOutput.test_status_lists_tasks_and_dependencies` — 3 tasks (base, child blocked_by base, orphan); base listed before child; status/phase rendered as `[active/research]`
  - `SwitchOutput.test_switch_emits_cd` — output starts with `cd ` + path is real dir + flips `.current-task` pointer
  - `ArchiveCleansWorktree.test_archive_removes_worktree` — archive removes managed worktree dir + moves task to archive/YYYY-MM/
- **P0 regression (10)**: PASS — `tests.smoke.test_p0_fixes` all green; archive-pointer ordering (P0-2) intact
- **Lint / typecheck**: stdlib-only, no new deps; ruff/pyflakes-clean (visual review)
- **Credential grep**: no secrets in any file changed
- **Skipped**: tests requiring `git` are decorated with `@unittest.skipUnless(_has_git(), ...)` — both ran on this host because git is available

## Sediment Notes

- **No new ADR** — the design (worktree-per-task as opt-in default `shared`) was already fixed in PRD §子项目 #4. This sub-agent only implements.
- **Pattern candidate (do not promote yet)**: The `.location` file pattern (one-line abs path inside task dir) is reusable — any future "external resource pinned to a task" can use the same idiom. Defer to v0.4 wrap-up.
- **Pitfall captured**: `git worktree add` fails when target path already exists (we treat that as `worktree creation failed → fall back to shared + WARN` rather than overwrite). Prevention: `_create_worktree` checks `wt_path.exists()` first.
- **Pitfall captured**: when running edits from a sub-agent worktree, absolute-path Edit calls write to the main repo, NOT the worktree branch — even when the worktree is the cwd. Mitigation: cp the diffs back. Long-term: relative paths or explicit worktree root resolution. Worth noting in v0.4 sub-agent dispatch SKILL.md.
- **Frontmatter parsing**: built a tolerant mini-parser for progress.md frontmatter (handles list-style `blocked_by:` block + inline `[]`). Reused style of `common/config._parse_simple_yaml` but made it stricter on indentation rules.

## Retro (optional)

- Worked: TDD-shape — wrote 5 tests upfront from the spec then implemented to satisfy each. Saved at least one regression (archive cleanup ordering — initially I wrote the worktree-remove AFTER `shutil.move`, then realized `_read_location` would fail because task_dir no longer exists; reordered).
- Didn't work: Initial Edit calls all landed in the main repo because I used absolute paths. The worktree path discrepancy bit me. Next time: resolve REPO_ROOT from `Path(__file__)` or pass cwd explicitly to subagent prompt.
- Framework feedback: `flow_task.py` is now ~400 LOC — close to the 350 threshold mentioned in PRD §#4 "需拆文件". Suggest main session split into `flow_task/{create,archive,status,switch}.py` after #4 lands.
