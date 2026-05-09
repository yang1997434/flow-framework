# Codex review R1 — v0.8.3 P0.2 implementer diff

## What you're reviewing

Staged diff in worktree `/data/Claude/flow-framework/.claude/worktrees/agent-a400e5af5e2336336`,
based on master commit `e1d3d67`. 8 files changed, +1033/-12.

Run `git -C /data/Claude/flow-framework/.claude/worktrees/agent-a400e5af5e2336336 diff --cached`
to see the full diff. The PRD is at
`<that worktree>/.flow/tasks/05-08-v0.8.3-p0.2-brief-sentinel-fullset/prd.md`
(committed in e1d3d67) — it's the authoritative spec, plan-pass GREEN at R3.

## In-scope (review these)

1. **Wire correctness end-to-end**: does `prompt_prefix` actually flow
   from `auto_dispatch_task` Round 1 / `_dispatch_implementer_fresh_worktree`
   Round 2+ through the new `flow_subagent_dispatch.invoke()` to a
   readable file at `<repo_root>/.flow/.runtime/<slug>+<task_id>+r<N>/dispatch_prefix.txt`
   that the rendered cmd_str references?

2. **Silent-failure / silent-drop class** (the bug class that produced
   the original gap):
   - `**_kw` removal complete? Any other silent-swallow paths?
   - `string.Formatter().parse()`-based fail-closed truly catches
     `# {prompt_prefix_file}`, `{{prompt_prefix_file}}`, escaped/literal
     forms? (PRD AC has 4 sub-assertions; verify implementation matches.)
   - Type validation (`prompt_prefix` non-str → raise BEFORE side-effect)?
   - Layout assertion correctly catches verify-worktree shape vs
     implementer-worktree shape?

3. **Round 1 wire-up correctness**:
   - `_cmd_auto_execute` builds prefix at the right point — AFTER
     `_task_already_completed` skip + `CrashRecoveryDispatcher.classify()`
     proceed, immediately before `auto_dispatch_task` call?
   - `auto_dispatch_task` accepts and forwards `prompt_prefix=""` default
     — backwards compat preserved (existing tests not broken)?
   - `dispatch_fn` call site at line ~900 now passes `prompt_prefix` +
     `round_num=1`?

4. **Manifest_violation safety**: the file is at
   `<repo_root>/.flow/.runtime/...` — confirm `derive_task_facts`
   does NOT see this path (it's outside the worktree). Confirm
   `verify_manifest_against_facts` does not block the task.

5. **Test adequacy**:
   - 10 unit + 2 integration tests present per PRD?
   - Test names match PRD AC list?
   - Integration tests exercise REAL `auto_dispatch_task` /
     `_dispatch_implementer_fresh_worktree` (not just fakes)?
   - Byte-for-byte content test verifies UTF-8 / no BOM / no CRLF
     mutation?

6. **Doc completeness**:
   - SKILL.md transport section clear about `cat {prompt_prefix_file}`
     requirement (operator must really pipe content into prompt)?
   - `claude/capabilities/defaults.json` placeholder doc updated?
   - CHANGELOG breaking-change warning + migration example present?
   - `dispatch-shim-silent-kw-drop.md` pitfall has all required
     frontmatter (date, severity, status, trigger_paths)?

7. **Adversarial probes** (the "200 IQ autistic developer" angle):
   - Anything in the diff that re-opens a silent-failure bypass?
   - Race condition in mkdir + write?
   - File path injection through slug or task_id?
   - Concurrent Round 1 + Round 2 of same task collision?

## Out-of-scope (DO NOT review)

- `K_CLASS_SENTINEL_PROHIBITION` text content — pinned by P0.0 tests
  in `test_dispatch_template.py`; not P0.2 scope.
- Other brief-generation sites — PRD plan-pass already classified them
  N/A and approved scope C "wire-up only".
- Fresh-worktree-per-round mechanism itself — P0.1 shipped, GREEN.
- Test framework / CI / lint / style preferences.
- Hypothetical operator templates (none in production; capability
  default was `{}` before P0.2).
- 18-class blindspot self-checks at the diff level (this is a wire-up
  PR — apply only D-class swallowed-exception and B-class state-machine
  checks where dispatch state matters).

## Threat model

- **Adversary**: future implementer subagent that wants to bypass the
  K_CLASS sentinel guard. Attack surface: the new file-based transport.
  Does this diff introduce a way to weaken delivery?
- **Adversary class**: silent-failure (the original bug). Look for new
  silent-swallow / silent-truncation / silent-drop introduced by this
  change.
- **NOT in threat model**: malicious operators (own the box), malicious
  fs actors (worktree is task-private), supply-chain.

## Project context

- Single-user owner-operated framework. Capability default was `{}`
  empty → no real users to break with breaking change.
- v0.8.3 P0.0 added K_CLASS guard text; P0.1 added fresh-per-round.
  This PR P0.2 is the missing wire-up between them.
- Tests use real tmp git for integration (precedent from P0.1 R2 GREEN).
- Recent pitfalls relevant: `edit-absolute-path-resolves-master.md`,
  this PR adds `dispatch-shim-silent-kw-drop.md`.
- Mandatory opus gate (state-machine + dispatch boundary).

## Review verdict format

**Verdict**: GREEN | YELLOW | RED
- GREEN = approved for merge; can write `.review-passed.json` marker
- YELLOW = fixable defects; list each with file:line + suggested fix
  - **D-class swallowed exceptions**: explicit check (per project memory:
    Codex review必抓 swallowed exception)
  - **B-class state-machine**: any new path silently advances or fails
    to advance counters?
- RED = structural issue; explain root cause

**P0 (must-fix before merge)**:
**P1 (nice-to-have)**:
**Adversarial probes that landed**:
**Test coverage gaps**:

Be terse. ~400 words max. No prose preamble.
