---
name: worktree-fork-before-prd-commit
date: 2026-05-08
project: flow-framework
severity: medium
status: active
trigger_paths:
  - ".claude/worktrees/*"
  - ".flow/tasks/*/prd.md"
  - "scripts/flow_task.py"
last_verified: 2026-05-08
---

# worktree-fork-before-prd-commit

## Symptom

Subagent dispatched into worktree reports the PRD file does NOT exist. Repro:

> "The PRD file `.flow/tasks/05-08-v0.8.2-p0-core/prd.md` does NOT exist
> on disk; worked from the prompt's R3 specification + ADR-1 directly."
> — T3 subagent report, 2026-05-08

Yet the file IS present in the main repo's working tree (where the user
edited it) and was visible to the main session.

## Root cause

`git worktree add` creates a working tree from a specific commit (e.g.
`master@b4a99f4` in v0.8.2 case). Files that exist only as **uncommitted
changes** in the main repo's working tree are NOT visible in the worktree.

The `.flow/tasks/<slug>/prd.md` is created by `flow_task.py create` AND
edited by the main session during Phase 1 brainstorm — but is typically
not committed to master before the worktree is forked. Result: PRD lives
only in main repo's uncommitted working state; worktree sees nothing.

## Mitigation actually used in v0.8.2

Subagent briefs **inline the full spec** they need — R-section text, ADR-1
text, acceptance criteria. Subagent doesn't need to read PRD file because
the brief already contains everything.

This worked for T1-T5 + all fix rounds. T3 subagent flagged the missing
PRD as a curiosity but completed the task using inlined spec.

## Why this is still a pitfall worth recording

1. The mitigation places maintenance burden on the dispatcher (every brief
   must inline complete spec). Easy to forget, and missing pieces cause
   subagents to invent or hand-wave.
2. Subagents reading "PRD file path: .flow/tasks/.../prd.md" in the brief
   may waste tool calls trying to find the file.
3. Reviewer agents and codex review inside the worktree ALSO can't see the
   PRD — codex round 1 noted the same issue.

## Prevention / fix candidates (v0.8.3)

- **Option A**: `flow_task.py` and worktree-create wrapper auto-commit the
  task dir (`.flow/tasks/<slug>/`) to master before forking. Cheap, but
  pollutes master with in-progress task files.
- **Option B**: Worktree creation explicitly copies `.flow/tasks/<slug>/`
  from main repo working tree into the new worktree's working tree (no
  commit needed). Preserves main-repo-only-PRD model.
- **Option C**: Document the constraint and standardize "always inline
  full spec in subagent briefs". Cheapest, but error-prone.

**Recommendation**: Option B — auto-copy on worktree creation. Done in a
helper script that wraps `git worktree add`.

## Related

- v0.8.2 T3 subagent report (commit `52829a0`)
- v0.8.2 T6 codex round-1 also flagged: "PRD path named in the prompt is
  not present in this worktree"
- `feedback_release_workflow.md` memory: "worktree → subagent-driven →
  final review → tag → release" — the protocol assumes worktree has
  enough context, this pitfall is the gap
