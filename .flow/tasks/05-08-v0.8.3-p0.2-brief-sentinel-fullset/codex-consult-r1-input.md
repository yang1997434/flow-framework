# Codex consult — v0.8.3 P0.2 plan-pass review

## Task framing

You are doing a **plan-pass review** (not a code review — there is no code yet).
The goal is to check the PRD/plan for design defects BEFORE implementation, so
the implementer doesn't need a costly second review round. Be strict on:

- Silent failures / silent drops in proposed wire-up
- Backwards compatibility holes
- Test coverage gaps relative to acceptance criteria
- State-machine / contract invariants the plan accidentally weakens
- Concurrency / re-entrancy issues with the proposed file path
- Forensic / evidence preservation (this project values worktree-as-evidence)

## In-scope (review these)

1. **Wire-up correctness**: does the proposed `prompt_prefix` flow
   (orchestrator → invoke → file → operator template substitution → claude
   subprocess) actually deliver the K_CLASS guard to the dispatched
   subagent's prompt? Identify any leak point.

2. **Fail-closed completeness**: the plan says "raise RuntimeError when
   prompt_prefix non-empty AND template lacks `{prompt_prefix_file}`
   placeholder". Are there other silent-drop modes the plan missed?
   - empty file
   - mkdir race
   - encoding edge case
   - operator template includes the placeholder but escapes/quotes wrong
   - SKILL not actually consuming the file content

3. **Round-1 wire-up**: `auto_dispatch_task` adds optional `prompt_prefix`
   param; `_cmd_auto_execute` builds prefix via `_render_task_brief` +
   `build_implementer_prompt`. Issues?
   - Is `_render_task_brief` callable at this point in `_cmd_auto_execute`'s
     control flow (does it have task_dir + criteria available)?
   - Does `is_first_pass=True, is_doc_only=False` correctly characterize
     a Round-1 dispatch?
   - Test fixtures for `auto_dispatch_task` use fake dispatch_fn — does
     adding optional param break them?

4. **Backwards compat**: optional param `prompt_prefix=""` defaults preserve
   old test paths. Audit: any existing call that would silently start passing
   non-empty prefix and trip fail-closed unexpectedly?

5. **Test plan adequacy**: 5 unit + 2 integration. Missing categories?
   Specifically:
   - End-to-end test that the file content actually matches the prefix
     orchestrator computed (no truncation / encoding loss)
   - Negative test: prefix non-empty + custom template w/o placeholder →
     raises with actionable message
   - Race / re-entrancy: two parallel tasks each writing to their own
     `<worktree>/.flow/.dispatch_prefix.txt` — file path uniqueness check

6. **Forensic preservation**: the file lives at
   `<worktree>/.flow/.dispatch_prefix.txt`. After Round 2+ creates a fresh
   worktree, does the new worktree get its own file? Are old-round files
   captured in `state.failed_rounds` for sediment?

7. **Operator template breaking change**: PRD says capability default is
   `{}` empty so no production users break. Verify by reading the recon
   data — did we miss any documented template in repo (`.flow/`, docs,
   examples) that would break?

## Out-of-scope (DO NOT review)

- The K_CLASS_SENTINEL_PROHIBITION text content — it's pinned by tests
  in `test_dispatch_template.py`, P0.0 territory, NOT P0.2 scope.
- Other brief-generation sites (reviewer prompt, render_task_brief) —
  recon already classified these N/A and the user (single user, owner)
  approved scope C "wire-up only".
- The fresh-worktree-per-round mechanism itself — that's P0.1 shipped.
- Test framework / CI choices.
- General code style / lint preferences.
- Migration tooling for hypothetical operator templates (none exist in
  prod per recon — capability default is `{}`).

## Threat model

- Attacker = a future implementer subagent that wants to bypass the
  K_CLASS sentinel guard. Does the wire-up create a new way to weaken
  or bypass the prefix delivery?
- Adversary class = silent-failure (the actual class that produced this
  whole bug). Look for new silent-drop / silent-truncation / silent-
  swallow paths introduced by the file-based transport.
- NOT in threat model: malicious operators (they own the box already);
  malicious file-system actors (worktree is task-private); supply-chain
  attacks on dependencies.

## Project context (key memory excerpts)

- This is a single-user, owner-operated framework (`flow-framework`).
  No external operators consuming the dispatch shim — capability
  default is `{}` empty. Breaking changes to operator template format
  cost zero real users; CHANGELOG warning sufficient.
- v0.8.3 P0.0 added K_CLASS_SENTINEL_PROHIBITION (4-clause text);
  P0.1 added `_dispatch_implementer_fresh_worktree` for Round 2+ retry
  in fresh worktree carrying redacted reviewer feedback in
  `prompt_prefix`. Both passed mandatory opus gate via codex review.
- Tests use real tmp git dirs for integration (3 mini + 2 prod-adapter
  in P0.1 are the precedent).
- Recent pitfalls relevant: `edit-absolute-path-resolves-master.md`
  (worktree paths must use worktree prefix, not master); will add
  `dispatch-shim-silent-kw-drop.md` as P0.2 sediment.

## Review verdict format (please use)

**Verdict**: GREEN | YELLOW | RED
- GREEN = plan is sound, proceed to implementation as-is
- YELLOW = plan has fixable defects; list them with file:line specificity
  if proposing code, otherwise list as `[ACs to add]` / `[ACs to revise]`
- RED = plan has structural issue requiring rethink; explain root issue

**P0 (must-fix before implementation)**:
- (each with WHY: silent-failure class | wire-up gap | invariant break |
  test gap | etc.)

**P1 (nice-to-have additions)**:

**Adversarial probe** (the "200 IQ autistic developer" angle):
- A creative bypass / edge-case the plan didn't cover
- Each: scenario + concrete repro + suggested AC

**Acceptance criteria deltas** (added / removed / refined):

Output should be terse and structured. No prose preambles. ~400 words max.
