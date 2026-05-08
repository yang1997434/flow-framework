---
name: flow-phase4-sediment
description: "Use when running Phase 4 of Flow framework — promote ADRs/patterns/pitfalls, auto-save journal, archive task. Invoked after Phase 3 commits done. Trigger: 'Phase 4', 'flow:sediment', 'sediment learnings'."
---

# Flow Phase 4 — Sediment

Capture learnings before moving on. Forced phase even if "nothing to sediment".

> **Safety**: before any destructive operation in this phase (archive task = `git mv`, branch deletion via branch_finish, force-push to release branch), invoke `{{capability:safety_guardrails}}`. See orchestrator §Cross-cutting capabilities.

## Why this is its own phase

Human instinct after task = "done, move on". Folding sediment into Phase 3 = it gets skipped. Independent Phase 4 forces conscious thought.

## Step 1 — Decide what to do with the development branch

Before sediment writing, invoke `{{capability:branch_finish}}` for structured options:
- **Merge to base** (default for completed features)
- **Create PR** (requires team review)
- **Cleanup** (abandoned exploration)

This decision shapes downstream sediment scope (e.g. ADR-worthy decisions only when merging).

## Step 2 — Identify candidates

Review task journey. Ask:

1. **ADR worth keeping?**
   - Made a non-trivial design decision?
   - Reviewed multiple options?
   - Decision has reversibility cost?
   → Write to `.flow/ADRs/<slug>.md` from `templates/ADR-lite.md.template`

2. **Pattern emerged?**
   - A reusable approach this project will need again?
   - Code shape worth replicating?
   → Write to `.flow/patterns/<slug>.md` from `templates/pattern.md.template`

3. **Pitfall hit?**
   - Got bitten by something unexpected?
   - Same kind could bite next time?
   → Write to `.flow/pitfalls/<slug>.md` from `templates/pitfall.md.template`
   - **Critical**: fill `trigger_paths` field (so future tasks auto-load this)

4. **Retry-round wrong turns? (v0.8.3 P0.1)**
   - Phase 2's retry loop logs each round (implementer + reviewer) as a row
     in progress.md `## Execute Log`. After v0.8.3 P0.1, Round 2+ runs in a
     FRESH worktree per round (`<slug>+t<n>+r<N>+<shortsha>`); the per-round
     diff is recoverable from the worktree (until the task-end batch
     ExitWorktree clears them).
   - Sediment input for "走过的错路": read FAIL rounds via the Execute Log
     rows + `git diff <ctx.original_base_commit>..HEAD` inside each
     surviving FAIL worktree. Promote any RECURRING wrong design to a
     `.flow/pitfalls/` entry — same kind biting twice is the bar for
     promotion (one-off mistakes belong in the task's own progress.md, not
     a global pitfall).

## Step 3 — Promotion candidates

When promoting to vault (`~/data/knowledge-base/`), follow the frontmatter conventions in `~/.claude/rules/knowledge-base.md` (required fields: `title`, `date`, `type`, `tags`, `status`, optional `project`). Flow's templates already include these but vault writes from raw chat content must conform.

For each new ADR/pattern/pitfall, also evaluate cross-project reuse:

| Sign | Action |
|------|--------|
| Same pattern would help in OTHER projects | Mark as `vault candidate` in Sediment Notes |
| Same pitfall is library/tool issue (not project) | Promote NOW to vault `pitfalls/` |
| Same rule has been used 3+ times no exception | Promote to `~/.claude/rules/` |

Don't auto-promote — user decides via `/flow:promote`.

## Step 4 — Char cap check

Letta-anchored caps:

| Layer | Limit | Action if exceeded |
|-------|-------|-------------------|
| `.flow/ADRs/<slug>.md` | <500 lines | Split by topic |
| `.flow/patterns/<name>.md` | <300 lines | Split / extract sub-patterns |
| `.flow/pitfalls/<slug>.md` body | <800 chars | Tighten |
| `~/.claude/rules/<topic>.md` | <200 lines | Split topics or demote items |

## Step 5 — Write Sediment Notes

Append to progress.md `## Sediment Notes`:

```markdown
## Sediment Notes
- ADR promoted: .flow/ADRs/<slug>.md ([[<slug>]]) — <1-line why>
- Pattern promoted: .flow/patterns/<name>.md ([[<name>]]) — <when reuse>
- Pitfall captured: .flow/pitfalls/<slug>.md ([[<slug>]]) — <symptom>
- vault candidates (deferred): pattern <name> (used here, watch if reused)
- Auto-saved to: .flow/workspace/${USER}/journal.md + auto-memory
```

**If nothing to sediment**: write one line "no new ADR/pattern/pitfall — task was routine for this project". Even this conscious "nothing" is a recorded thought.

## Step 6 — Auto-save

**Critical step — this is the user's preserved-memory requirement.**

1. Append to `.flow/workspace/${USER}/journal.md`:
   ```markdown
   ## ${TASK_SLUG} — ${DATE}
   - Title: ${TASK_TITLE}
   - Type: ${TASK_TYPE} / ${COMPLEXITY}
   - Outcome: commit ${COMMIT_HASH}
   - Sediment: ${SEDIMENT_SUMMARY}
   - Auto-loaded pitfalls used: ${LOADED_PITFALLS_LIST}
   - Next-suggested: ${USER_HINT or "none"}
   ```

2. Invoke `{{capability:session_save}}` skill — write breakpoint to `~/.claude/projects/.../memory/`

3. (Optional) Update auto-memory MEMORY.md with pointer to new sediment if cross-project significance

If `{{REPO_ROOT}}/scripts/flow_save.py` exists, run it for automation:
```bash
python3 {{REPO_ROOT}}/scripts/flow_save.py --task "${CURRENT}"
```

## Step 7 — Archive

```bash
YEAR_MONTH=$(date +%Y-%m)
mkdir -p .flow/tasks/archive/${YEAR_MONTH}
mv "${CURRENT}" .flow/tasks/archive/${YEAR_MONTH}/
rm .flow/.current-task
```

## Step 8 — After ship — auto-generate changelog

If the task culminated in a ship (new commits to base branch + version bump), invoke `{{capability:changelog_gen}}` to generate a user-facing changelog entry from the commit history. Validate output before committing — the underlying changelog-generator skill may overwrite or append to existing CHANGELOG.md depending on its detection heuristics.

## Step 9 — Confirm to user

```
Task ${TASK_SLUG} archived.
Sediment: ${X} ADR / ${Y} pattern / ${Z} pitfall written.
Journal entry appended.
Cross-project candidates noted: ${LIST}.

Resume: /flow:start <next-task> when ready.
```

## Constraints

- **Never skip** — even "nothing to sediment" gets a line
- **Never promote without /flow:promote** — Phase 4 only writes Lv1, defers Lv2/3 to explicit command
- **Char caps enforced** — overflow forces split
- **Auto-save is non-negotiable** — user's "memory must persist" requirement
- **Don't delete archived tasks** — they're queryable via `task list-archive`
