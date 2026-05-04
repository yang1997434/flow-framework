# flow-test-task

> Created: 2026-05-04
> Slug: flow-test-task
> Type: backend  <!-- 后端/UI/数据/文档/部署/调研 -->
> Complexity: simple  <!-- trivial/simple/moderate/complex -->

## Goal

End-to-end dogfood validation of the v0.5 + v0.5.1 + v0.5.2 chain — exercise `/flow:start → /flow:pause → simulated compact → /flow:resume → /flow:finish` with REAL slash-command invocations against the installed prompt files (`~/.claude/commands/flow/`), not just unit tests.

## What I already know

- v0.5.0 + v0.5.1 + v0.5.2 already shipped. 135/135 smoke tests pass.
- Layer 1 framework testing already proved doctor / selftest / runtime artifacts. Found + fixed test isolation leak as v0.5.2.
- Layer 3's value-add: tests installed-prompt rendering, model's interpretation of Step 6-8 prompt instructions, real hook subprocess firing, full lifecycle UX.

## Requirements

- Skip interactive brainstorm interview — this is a test, requirements are predetermined.
- Trigger Lv1 trickle (post-tool-bash + post-tool-edit) by doing real work in this task dir.
- Invoke `/flow:pause` via the Skill tool — verify Step 6-8 actually run, intent.md gets written.
- Simulate compact via subprocess invocation of session-start.py with `compact` matcher — verify resume block injected.
- Invoke `/flow:resume` via Skill tool — verify reads checkpoint, surfaces Next Action.
- Invoke `/flow:finish` via Skill tool — verify archive + sediment.

## Acceptance Criteria

- [ ] Lv1 trickle: `progress.md ## Files Touched` and/or `## Commits` populated by hooks
- [ ] `/flow:pause` produces `<task>/.checkpoint/intent.md` with valid frontmatter
- [ ] Cascade hint appears in `~/.flow/.runtime/hints/`
- [ ] Nudge state has `acknowledged_via: "manual_pause"` after pause
- [ ] Simulated compact (subprocess) emits `<flow-resumed-from-compact>` block with intent body
- [ ] `/flow:resume` correctly surfaces Next Action from intent.md
- [ ] `/flow:finish` archives task to `.flow/tasks/archive/2026-05/`

## Definition of Done

- All acceptance criteria checked
- Verify Report written into progress.md before /flow:finish
- Sediment Notes capture any new pitfalls / observations
- No regressions to runtime state for other tasks

## Out of Scope

- Full /flow:start interactive brainstorm (skipped on purpose for test)
- Performance benchmarking
- Stress / concurrency testing

## Research References

- Spec: `docs/specs/2026-05-04-auto-resume-design.md`
- Plan: `docs/plans/2026-05-04-auto-resume-v0.5.0.md`
- Persistent memory: `feedback_subagent_driven_workflow.md`, `pitfall_checkpoint_paths_mkdir.md`

## Decision (ADR-lite)

N/A — this is a test, not a design task.

## Technical Notes

- Files to inspect: `~/.claude/commands/flow/{pause,resume,finish}.md` (installed renders), `claude/commands/flow/{pause,resume,finish}.md` (source), `claude/hooks/{pre-compact,post-tool-bash,post-tool-edit,session-start}.py`
- Constraints: don't push to origin during this test; if findings produce a v0.5.3 patch, batch all into one commit at the end
