# Changelog

## [0.8.3.1] - 2026-05-08 (hotfix: AFK park rc=5 test time-bomb)

### Fixed

- **`tests/smoke/test_phase2_retry_loop.py:805-822`** — `test_phase2_dispatch_park_returns_rc5_no_merge`
  was a time-bomb: hardcoded `start = datetime(2026, 5, 8, 0, 0, 0)`
  combined with `hard_cap_seconds=99_999.0` (~27.7h) meant the test
  PASSed only when run within ~27.7h of that hardcoded date. Once real
  `now_iso_utc` (used inside production `_phase2_dispatch`) drifted
  past the hard cap, `AfkMonitor.evaluate` produced `afk_hard_cap`
  (terminal rc=3) instead of `afk_idle_park` (recoverable rc=5) —
  the test asserted rc=5 and consistently failed forever after.
  v0.8.3 shipped with this red unnoticed (the ship-time test run was
  inside the 27.7h window). Fix: derive `start` from
  `datetime.now(timezone.utc)` + raise `hard_cap_seconds` to
  `99_999_999.0` (~3 years) — production code unchanged. Other
  `99_999.0` uses (lines 545, 585) are paired with `_make_now_fn` mock
  clocks and are safe.

### Production code

No change. Audit confirmed wait-mode AFK behaviour in
`_phase2_dispatch` is correct — the bug was test-only.

## [0.8.3 P0.2] - 2026-05-08 (dispatch shim wire-up — prompt_prefix file transport)

### Fixed (P0.2 — dispatch shim silent-drop class)

- **Closes the K-class sentinel guard wire-gap**: `build_implementer_prompt`
  has prepended the K-class sentinel prohibition to every first-pass
  code dispatch since v0.8.2 T4 / v0.8.3 P0.0, but the prefix was
  silently swallowed at the dispatch shim boundary
  (`flow_subagent_dispatch.invoke` accepted `prompt_prefix` via
  `**_kw` while the operator template only knew
  `{slug,task_id,worktree,worktree_quoted}`). Round 1 never even
  passed it. The guard was effectively dead code in production.
- **File-based transport** (`<repo_root>/.flow/.runtime/<slug>+<task_id>+r<round>/dispatch_prefix.txt`):
  the orchestrator writes the prompt prefix to disk; operator templates
  reference it via the new `{prompt_prefix_file}` placeholder
  (already `shlex.quote()`-wrapped). Path lives outside the worktree
  to avoid a `manifest_violation` row 4 block (`derive_task_facts`
  only enumerates files inside the worktree). Already gitignored
  at `.gitignore:21` (`.flow/.runtime/`).
- **`invoke()` signature overhaul**: explicit `prompt_prefix: str = ""`
  + `round_num: int = 1` kwargs; `**_kw` REMOVED so any unknown
  kwarg raises `TypeError` (kills the silent-drop class — adding a
  new parameter without wiring it now fails loud).
- **Type validation before side effects** (codex P0#4): non-`str`
  `prompt_prefix` (None, bytes, int, list, dict) raises `TypeError`
  before the runtime dir / file is created.
- **Fail-closed via `string.Formatter().parse()`** (codex P0#3): the
  placeholder check inspects real format-field positions, not
  substring matches. Commented-out tokens, doubled-brace escapes
  `{{...}}`, and string literals do NOT count. Non-empty
  `prompt_prefix` + missing placeholder → `RuntimeError` BEFORE
  any subprocess runs.
- **R2 fix — shell-comment fail-closed extension** (codex R1 caught
  during R2 review): even when `{prompt_prefix_file}` is a real
  format field, a shell-comment placement (`true # {prompt_prefix_file}`)
  silently drops the K-class guard at the subprocess layer (the
  shell parses `#` as a line comment). New per-line regex
  `(?:^|\s)#[^\"']*$` rejects this form before substitution. Scope-
  honest: covers shell-comment placement; does NOT cover
  inner-subprocess string-literal embedding (e.g.
  `python -c 'x="{prompt_prefix_file}"'`) — that is documented as a
  known operator-responsibility bypass in SKILL.md transport
  section + the `dispatch-shim-silent-kw-drop` pitfall.
- **R2 fix — task_id required when prompt_prefix non-empty** (codex
  R1 P1#2): empty `task_id` with non-empty `prompt_prefix` would
  route ALL tasks to `<repo>/.flow/.runtime/<slug>++r1/`, causing
  per-task evidence collisions across same-slug tasks. Now raises
  `RuntimeError` with actionable message; the `'NOTASK'` fallback
  identifier is removed.
- **R3 fix — bare-form enforcement closes Formatter conversion/spec
  bypass** (codex R2 P0): `Formatter().parse()` returns the same
  `field_name` for `{prompt_prefix_file}`, `{prompt_prefix_file!s}`,
  and `{prompt_prefix_file:>10}`, AND cannot distinguish `{x:}`
  from `{x}` (both → `format_spec=''`). The literal-token shell-
  comment scanner matches the bare form only, so a template like
  `true # {prompt_prefix_file!s}` would pass the field check, evade
  the comment scanner, and be silently dropped at runtime by the
  shell. Two-layered fix: (a) `Formatter().parse()` walk rejects
  any non-empty `format_spec` or non-`None` `conversion`; (b)
  raw-template regex `\{prompt_prefix_file[^}]` rejects the empty-
  spec `{x:}` form that parse() can't see. Collapses the variant
  family to the bare spelling so the downstream literal scanner
  stays sound.
- **R3 fix — internal docstring honesty** (codex R2 P1): the
  `_template_field_names` docstring used to claim string literals
  "do NOT count" — contradicting the R2 doc honesty rewrite that
  acknowledges string-literal-inside-subprocess as a documented
  operator-responsibility bypass. Docstring now matches SKILL.md /
  pitfall scope.
- **Worktree layout assertion** (codex R2 P1#1): `invoke()` derives
  `repo_root` by reversing `<repo_root>/.claude/worktrees/<id>/`;
  unexpected layouts (`<repo>/.claude/wt/<id>`,
  `<repo>/.claude/worktrees/verify/<id>`) raise `RuntimeError` with
  an actionable message rather than misroute the prefix file.
- **Round 1 wire-up**: `auto_dispatch_task` accepts an optional
  `prompt_prefix=""` kwarg and forwards it to `dispatch_fn` with
  `round_num=1`. `_cmd_auto_execute` builds the prefix immediately
  before the dispatch call AFTER the task-already-completed skip
  + recovery dispatcher returns `proceed` (codex R2 P1#2 — no
  prefix-build side effect on skipped/aborted paths).
- **Round 2+ wire-up**: `_dispatch_implementer_fresh_worktree` now
  passes `round_num=N` to the shim so each retry round writes its
  prefix to `+r<N>` (no clobber of round-1's `+r1` evidence).
- **Byte-for-byte file fidelity** (codex R2 AC delta #1): the prefix
  file is written via `write_bytes(prompt_prefix.encode("utf-8"))` —
  no BOM, no CRLF translation, no trailing newline added.

### Tests (P0.2 — 20 new after R3 fix; was 17 after R2, 12 after R1)

- `tests/smoke/test_subagent_dispatch_shim.py` — 16 new units:
  prefix file path location, placeholder substitution, fail-closed
  on missing placeholder (4 sub-assertions: literal absent,
  comment-style, `{{...}}`-escaped, name typo), shell-comment
  placement (R2 fix — 3 sub-assertions: single-line, multi-line,
  tab-prefixed), known string-literal-bypass (R2 fix — pinned as
  documented bypass; MUST NOT raise), empty-task_id with prefix
  (R2 fix — fail-closed required), bare-form variants rejected
  (R3 fix — 3 tests: `{x!s}`/`{x!r}`/`{x!a}` conversion forms,
  `{x:}` empty + `{x:>10}` non-empty spec, combined shell-comment
  + conversion `# {x!s}`), unknown kwargs rejected,
  non-str prefix rejected, empty-prefix backwards-compat,
  round-discriminator path, byte-for-byte fidelity, path-typo guard,
  worktree layout assertion.
- `tests/smoke/test_v083_p02_dispatch_wireup.py` — 4 integration
  tests against a real tmp git repo: Round 1 via
  `auto_dispatch_task` (asserts NOT `manifest_violation`, prefix
  file content embeds K-class text, file path NOT in
  `TaskFacts.changed_files`/`newly_added_files`); Round 2 via
  `_dispatch_implementer_fresh_worktree` (asserts `+r2` path +
  reviewer-feedback content present); R2 P1#1 happy-path through
  `_cmd_auto_execute` itself (proves the prefix-build site is
  reached + forwards correctly); R2 P1#1 skip path (proves no
  prefix file is written when `_task_already_completed` returns
  True — codex R2 P1#2 ordering invariant).
- Total smoke 880 (up from baseline 860; net +20 P0.2 — no regressions).

### Breaking change (operator)

- **Operator dispatch templates with non-empty prefix MUST add the
  `{prompt_prefix_file}` placeholder** AND actually `cat` its body
  into the prompt sent to the subagent. The default capability
  config ships `dispatch_cmd` absent (operator supplies via
  `FLOW_SUBAGENT_DISPATCH_CMD` env var), so zero real users
  affected by the absence of a default — but anyone with an
  existing template will hit the fail-closed `RuntimeError` on
  next dispatch until they update.
- Recommended template:
  ```
  claude -p "$(cat {prompt_prefix_file})

  flow:flow-phase2-execute --slug {slug} --task {task_id} --worktree {worktree_quoted}"
  ```
- The `{prompt_prefix_file}` value is already `shlex.quote()`-wrapped;
  do NOT add additional shell quoting around it.
- Empty-prefix path is still backwards-compatible: callers that
  never pass `prompt_prefix=` (or pass `""`) work with the legacy
  4-placeholder template; no runtime dir is created.

### Pitfall captured

- `.flow/pitfalls/dispatch-shim-silent-kw-drop.md` — any shim that
  accepts `**_kw` while the downstream consumer (template, CLI
  contract, etc.) doesn't reference the kwarg is a silent-drop
  class. Adding a new kwarg requires both a placeholder/contract
  update AND a fail-closed assertion that the downstream really
  uses it. Trigger paths: `scripts/flow_subagent_dispatch.py` +
  any `dispatch_fn` call site in `scripts/flow_orchestrator.py`.

## [0.8.3] - 2026-05-08 (P0.0 hook fix + P0.1 fresh-per-round + P0.4 + P0.5)

### Added (P0.1 — fresh-worktree-per-round implementer redispatch)

- **Closes v0.8.2 T18 deferred stub**: `_prod_impl` previously returned
  empty deltas on Round 2+ in the prod path, so reviewer feedback never
  entered implementation. Round 2+ now genuinely re-dispatches the
  implementer subagent.
- **Fresh worktree per round** (`<slug>+t<n>+r<N>+<shortsha>`): Round 2+
  starts from `integration_target` base — no inheritance of Round 1's
  working-tree files. Round 1 keeps legacy naming (`<slug>+t<n>+<shortsha>`)
  for backward compat. Codex round-1 review G2 + G3 closed: worktree id
  collisions and double `auto_engaged` events impossible by design.
- **Helper `_dispatch_implementer_fresh_worktree`**: bypasses
  `auto_dispatch_task` on retry rounds (skips lock + auto_engaged event
  + manifest verify). Calls `create_task_worktree(round_num=N)` +
  `_invoke_subagent_dispatch` + `derive_task_facts` directly.
- **`InfraFailureError`** for worktree-create / subagent crash on retry
  rounds → routes to `phase2_infra_failure` block (rc=3) WITHOUT bumping
  retry counters. Codex round-1 D §1: J-class progress invariant
  preserved — infra noise never smuggled into review-driven counters.
- **Winner ctx propagation**: `_phase2_dispatch` signature extended to
  `-> (rc, winner_ctx, winner_facts)`. `_cmd_auto_execute` uses winner
  pair to construct `MergeRunner` + `Gate8VerificationRunner`. Round 1
  PASS aliases the seeded ctx; Round 2+ PASS uses the fresh helper-
  produced ctx. Hard invariant: rc==0 ⇒ winner pair non-None.
- **`RoundRecord(worktree_id, path, branch, round_num)`** lightweight
  frozen dataclass tracks FAIL rounds on `state.failed_rounds` (in-
  memory only — no journal mirror in P0.1; documented contract).
  Phase 4 sediment skill updated to reference recovery via Execute Log
  rows + per-worktree diff.
- **`WorktreeContext.round_num`** first-class field (default 1) so
  cleanup / journal queries don't string-parse worktree id.
- **Two-phase commit on round 2+ state mutation**: helper computes new
  ctx + facts FIRST; only on success does `_prod_impl` append prev
  `RoundRecord` and swap `current_round_*`. Mid-helper raise leaves
  state coherent (codex round-1 P0 §2).
- **Round-cap default 3 → 2**: each retry round is materially more
  expensive (real subagent dispatch + fresh worktree); contract
  override path preserved for callers that want longer convergence.
- **Task brief renderer (`_render_task_brief`)**: replaces
  `task_brief=""` bug at the prefix-build site (`build_implementer_prompt`
  was getting an empty brief — Round 2+ subagents would have had no
  task context). Reads `task_dir/prd.md`, falls back to a bullet-
  rendered acceptance-criteria list.
- **Tests**: 21 new tests across 3 files — 16 unit in
  `test_v083_p01_implementer_redispatch.py` + 3 mini-integration in
  `test_fresh_worktree_per_round.py` + 2 production-adapter
  integration in `test_v083_p01_prod_adapter_integration.py` (codex
  review round 2 J §3+§4 follow-up: real `_phase2_dispatch` Round 2+
  PASS path + InfraFailureError → phase2_infra_failure block). Total
  suite 990 PASS (969 + 21 new).

### Fixed (P0.4 — sediment follow-up)

- **Hook `git -C /path commit` bypass** discovered in P0.0 sediment notes:
  argv[1]='-C' caused 5c branch to PASS without checking that `commit`
  appears later in argv. Forms newly BLOCKed (4 new pytest cases):
  - `git -C /path commit -m foo`
  - `git --git-dir=.git commit -m foo`
  - `git --work-tree=. commit -m foo`
  - `git -c k=v commit -m foo` (already caught by `-c` check; now also by
    earlier `commit in argv[2:]` check)
- **Dotfiles `.gitignore` (P0.5)**: added `claude/hooks/_vendor/**/__pycache__/`
  + `*.pyc` patterns; removed 11 stray `.pyc` files accidentally committed in
  v0.8.3 P0.0 dotfiles commit `7326607`. Also gitignores
  `.review-passed.json` (new marker file) + `pre-commit-review.sh.bak.*`.

### Fixed

- **Pre-commit review hook double-direction bug** (pitfall
  `hook-blocks-after-reviewer-pass.md`):
  - **false-negative**: `<noop> && git commit ...` (e.g. `touch /tmp/x && git
    commit`) no longer bypasses the hook. v0.8.2 T6.3 incident closed.
  - **false-positive**: heredoc bodies / multi-line commands containing the
    text `git commit` no longer trigger BLOCK on legitimate work. v0.8.2
    /flow:pause incident closed.

### Added

- `~/.claude/hooks/pre-commit-review.py` — full Python rewrite implementing
  the **D''''+SoleRoot+WrapperDetect** 7-step path (vendored bashlex AST
  analysis + sole-root simple-command requirement + strict argv white-list +
  content-hash + HEAD-bound marker).
- `~/.claude/hooks/_marker_writer.py` — atomic marker writer helper called
  by reviewer flow.
- `~/.claude/hooks/_vendor/bashlex/` — vendored bashlex 0.18 (frozen at
  GitHub commit `3f6b76c`, ~228KB) + `_selftest.py` for `python -S`
  isolated import smoke test.
- `tests/hooks/test_pre_commit_review.py` — 18-case acceptance matrix
  (sole-root / wrapper / substitution / `-c` injection / env-prefix /
  HEAD binding / single-use unlink / `--amend` exempt / etc.).

### Changed

- Marker schema **v=1**: now JSON `{schema_version, repo_id, head_oid,
  tree_sha, ts}` at `~/.claude/hooks/.review-passed.json`. The legacy
  empty `.review-passed` file is treated as absent. Marker is bound to
  current repo + HEAD + index — branch switch / `git add -p` / repo
  cross-use all invalidate it. Single-use (atomic unlink on PASS).
- `K_CLASS_SENTINEL_PROHIBITION` (`scripts/dispatch_template.py`): expanded
  from 5-line "do not touch marker" to 4-clause prohibition (wrap/non-plain/
  marker-mutate/git-config-bypass) + hook-maintenance exception. Forensic
  anchor preserved.

### Process

- Phase 1 used **5 rounds of cross-model codex consult** (~80K tokens,
  session `019e078a-...`). Final verdict: Y (YELLOW with one acceptable
  caveat — non-git commands whose argv text contains both `git` and
  `commit` substrings will be BLOCKed; rare and recoverable). Full
  artifacts in `.flow/tasks/05-08-v0.8.3-p0.0-hook-fix/research/`.
- Threat model explicitly limited to **LLM accidental bypass**, not
  adversarial users with shell access (the user owns `~/.claude/hooks/`).

## [0.8.2.1] - 2026-05-08

### Fixed
- **Observable change**: AFK idle park exit code corrected from `2`
  (published in v0.8.2) to `5`. Wrappers/monitors that branch on
  `rc=2` from `_cmd_auto_execute` must be updated. The change
  resolves a semantic collision with Flow's internal `rc=2 = USAGE_ERROR`
  convention used by 5 internal CLIs.
- Stale `_run_retry_loop` docstring (line ~4953) that still claimed
  AFK idle park returns rc=0 (a v0.8.2 T6.2 doc-drift bug) — now
  correctly says rc=5.

### Added
- `scripts/common/exit_codes.py` — Flow global exit-code registry
  (single source of truth):
  `0=PASS / 1=GENERIC_FAIL / 2=USAGE_ERROR / 3=BLOCKED /
   4=NESTED_ABORT / 5=PARKED_RECOVERABLE`.

### Notes
- `v0.8.2` tag remains pinned at commit `24bdecc` (NOT force-moved).
- 5 existing CLI files (`flow.py`, `flow_doctor.py`, `flow_promote.py`,
  `flow_autosave.py`, `flow_ralph.sh`) still use rc=2 = USAGE_ERROR;
  this is consistent with the new registry. Their literal-to-constant
  refactor is deferred to v0.8.3 P3 backlog.

## v0.8.2 — 2026-05-08

**Safety stack live.** v0.8.1 schema-only T17/T18 placeholders are now
real runtime enforcement; Phase 2 dispatch converts from fail-fast to a
retry-on-non-pass loop with independent caps; dispatch templates
auto-prepend the K-class sentinel prohibition.

- **Budget enforcement (R2)**: 5 frozen counters — `tokens_in`,
  `tokens_out`, `cost_usd`, `active_wallclock_minutes`,
  `subagent_dispatches`. `cost_usd` carries `model_id` +
  `pricing_version`; all token counters use 80%/100% trip wire (matching
  the estimator's ±20% coarseness). Hard hit writes a unified
  `HardStopSnapshot` (schema `v1`).
- **Paused-clock**: first-class pause-interval records (not single
  accumulated value) for crash-resume safety; shared between AFK monitor
  and `active_wallclock_minutes` budget.
- **AFK timeout (R1)**: `wait` mode default (parks recoverably);
  `abort` mode terminal; 24h hard cap overrides `wait`. 3 mechanical
  activity signals — file mtime / cmd issuance / subagent heartbeat.
- **Phase 2 retry-loop (R3)**: `dispatch_with_retry` replaces fail-fast
  in `_cmd_auto_execute`. Two independent round caps —
  `max_dispatch_retry_rounds=3` (implementer fail) and
  `max_codex_review_rounds=2` (codex `rejected_with_rationale`).
  5 dual-counter invariants enforced: round-RWR consumes review round
  not retry; budget hits override review verdicts; all terminals share
  one snapshot shape; no path leaves both counters static.
- **Dispatch hardening (R4)**: `scripts/dispatch_template.py` exposes
  `build_implementer_prompt` / `build_reviewer_prompt`. Implementer
  prompts auto-prepend the verbatim K-class sentinel prohibition (saves
  v0.8.1's 2-incident drift). Reviewer prompts mount the 18-class
  blindspot summary inline + reference; reviewer findings to implementer
  are stripped of class-letter triggers via extended
  `redact_blindspot_index` (covers `A.`, `A:`, `A)`, `A — ` em-dash, and
  `Class A` variants).
- **Exit codes**: `0` = pass / interactive fallback; `2` = AFK idle
  park (recoverable, `/flow:resume` to continue, no `blocked.md`, no
  snapshot, no merge); `3` = block raised OR Phase 2 terminal
  hard-stop (any of `budget_hit`, `retry_cap`, `codex_review_cap`,
  `afk_aborted`, `afk_hard_cap`); `4` = `aborted_nested`.
- **Suite**: 822 → 939 (+117 cases). Smoke 834 + unit 105.
- **Cross-model review**: 3 codex review rounds; round-1 caught 5
  issues, round-2 caught 3 issues introduced by fixes, round-3 PASS
  with 2 P2 doc drifts (closed in T6.3).
- **Sediment**: 3 new pitfalls (`hook-blocks-after-reviewer-pass`,
  `worktree-fork-before-prd-commit`,
  `subagent-misread-brief-do-not-add-modules`). 1 K-class process
  violation recorded (T6.3 `--no-verify` bypass after 2x reviewer PASS;
  hook block-after-PASS root cause unknown — v0.8.3 P0 investigation).

**Known caveats (v0.8.3 carry-over)**:

- **Round 2+ implementer re-dispatch is a no-op in production** — the
  retry loop currently provides budget/AFK/round-cap enforcement with
  unified snapshots, but the prod `_prod_impl` returns `{}` on round 2+,
  so retry rounds have no real "fix opportunity". v0.8.3 P0 work covers
  worktree state inheritance + cross-round mutation + reviewer-feedback
  prompt-prefix transfer.
- **Hook block-after-PASS** investigation; **worktree pre-PRD-commit**
  auto-fix; **subagent brief language hardening** (concrete may /
  may-not lists).

## v0.8.1 — 2026-05-07

**Autonomy enabled.** The execution refusal in v0.8.0 is replaced with a
hardened 8-gate safety stack:

- **Contract schema**: 6 new fields (`max_codex_rounds_per_task`,
  `notification.throttle_min`, `notification.tier2_enabled`,
  `idempotent_cmd_allowlist`, `post_merge_regression_optional`,
  criterion-level `idempotent` / `timeout_sec` / `post_merge_skip`).
  Schema version stays at `1` (additive); v0.8.0 readers ignore new
  fields with warning.
- **8-gate runner**: baseline / subagent / manifest verify / codex
  review / acceptance criteria / regression smoke / local merge /
  post-merge verify (in ephemeral verification worktree).
- **Atomic merge**: 9-step transactional sequence with explicit gap-
  by-gap crash recovery.
- **Notification**: 3-tier with throttle + Tier 2 disable + OSC 9
  auto-detect + archive on resume.
- **Crash recovery**: 5-state classifier (pre-lock / lock+dead-pid /
  auto_engaged / mid-merge / verification-orphan).
- **Staleness**: 5 explicit triggers (base branch / lockfile / prd
  mtime / dep version / baseline fail) wired into `flow doctor`.
- **Nested-autonomy**: `FLOW_AUTONOMY_PARENT_PID` env-var mechanical
  guard at orchestrator entry.
- **context_estimator 1M-mode fix**: `_resolve_limit` priority chain
  (`FLOW_CONTEXT_LIMIT` env > rung 2a model-specific
  `ANTHROPIC_DEFAULT_<BASE>_MODEL` ending `[1m]` > rung 2b plan-level
  heuristic — *any* `ANTHROPIC_DEFAULT_*_MODEL` ending `[1m]` upgrades
  all models, since 1M is an Anthropic plan-level paid add-on, not
  per-model > `MODEL_LIMITS` table > 200k default). Fixes 5x context-
  percent inflation on 1M-mode sessions whose transcripts record bare
  `claude-opus-4-7`, including the common case where the user only
  aliased one base (e.g. sonnet) but runs others under the same plan.
  - Round-2 [P2] guard: rung 2b only applies when the matching alias
    is *absent*, not merely when it exists without a `[1m]` suffix —
    so an explicit non-1M alias (e.g.
    `ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7`) is no longer
    silently upgraded by an unrelated sibling alias.
  24 unit cases in `tests/unit/test_context_estimator_1m_resolution.py`.

**Validation**: full suite 822 cases passing (717 smoke + 105 unit);
`flow doctor` clean; `flow_selftest.py` ALL CHECKS PASSED; 3 contract
fixtures (`docs/fixtures/v081-{minimal,typical,advanced}.json`)
validate; v0.8.0 forward-compat smoke green.

**Deferred to v0.8.2**: AFK detector loop (T17), budget runtime
enforcement (T18), Phase 2 retry loop, staleness checks inside the
dispatch loop, Tier 3 notification command execution. All four parse
forward-compat in v0.8.1 contracts so migration is monotonic.

No backward-incompatible changes. v0.6/0.7 plans without contracts
continue to run interactively.

## v0.8.0 — 2026-05-06

### Major: Autonomous mode foundation (execution gated to v0.8.1)

The `contract.json` schema lands. Tasks can now declare autonomy scope,
budget, irreversible actions, acceptance criteria, and stop-condition
decision tables. Dry-run orchestrator builds per-task file ownership
manifests and previews what v0.8.1 *would* execute.

**New CLI:**
- `flow contract --init <slug>` — generate template
- `flow contract --validate <slug>` — schema + integrity check
- `flow orchestrator --dry-run <slug>` — print plan + manifests
- `flow orchestrator --auto-execute <slug>` — refused with explanatory error

**New schema:**
- `.flow/tasks/<slug>/contract.json` (versioned: `contract_schema_version: 1`)
- `progress.md` frontmatter pointer: `contract_path`, `contract_schema_version`,
  `autonomy_mode`, `last_checkpoint`
- State writers (no reads yet): `decisions.jsonl`, `review-issues.jsonl`,
  `checkpoints/<ts>.md`, `blocked.md`

**New capabilities:**
- `autonomy_orchestrator` (placeholder; activated in v0.8.1)
- `acceptance_verify` (placeholder; activated in v0.8.1)

**`flow doctor` extension:** contract.json existence + schema-version check
for every task whose `progress.md` declares `autonomy_mode`.

### Backwards compatibility

Pure additive. Missing contract → interactive (v0.7 behavior unchanged).
Forward-compat: unknown contract fields accepted with warning.

### Migration

See `docs/v0.8-migration.md`.

## v0.7.1 — 2026-05-05

### Fixed

- **`flow waves` + `flow_wave_runner` user-data path resolution**
  (`scripts/flow_wave_planner.py`, `scripts/flow_wave_runner.py`):
  six callsites resolved against the framework `REPO_ROOT`
  (= `Path(__file__).parent.parent`, i.e. flow-framework's own checkout)
  instead of the user's project:
  - `_progress_md_for_slug()` and `_cache_path_for_slug()` (planner)
  - the two `_get_base_commit()` calls in `cli_cache_check` / `cli_write_cache` (planner)
  - the `--repo` default in `cli_diff_names` (runner) — per-task git-diff verifier
  - the waiver log path in `cli_waive` (runner) — `wave-decisions.log` writer

  Symptoms: `flow waves --preview <slug>` invoked from any project other than
  flow-framework itself failed with `ERROR: progress.md not found for <slug>`;
  `cli_write_cache` and `cli_waive` would write artifacts under the framework
  directory rather than the project's `.flow/tasks/<slug>/`; `cli_diff_names`
  would diff the framework's git history when `--repo` was omitted.

  Fix: introduce `_project_root()` in `flow_wave_planner.py` that walks
  `Path.cwd()` up looking for a `.flow/` directory (mirrors
  `common.paths.get_project_root()` behavior), then route the six user-data
  path/git lookups through it. `flow_wave_runner.py` imports the helper from
  the planner module to stay DRY. SHARED_ARTIFACTS and `sys.path` setup
  continue to use framework `REPO_ROOT` (correctly — they refer to
  framework-shipped assets).

### Tests

- New `TestProjectRoot` in `tests/smoke/test_wave_planner.py`: 5 cases
  covering project-dir cwd, nested-subdir cwd, fallback when no `.flow`
  ancestor exists (skipped automatically if the OS tempdir itself sits under
  a `.flow` chain), and explicit assertions that progress.md / cache paths
  do **not** leak into the framework directory. Uses `addCleanup` to
  restore cwd and `shutil.rmtree` the tmpdir.
- New `TestWaveRunnerCLIPathResolution` in
  `tests/smoke/test_wave_runner_helpers.py`: 2 cases covering the two
  runner CLI fixes — `cli_diff_names` defaults to project root when
  `--repo` is omitted (drives a real throwaway git repo), and `cli_waive`
  writes its log under the project (not the framework).
- Suite total grows 18 → 23 (planner) and 6 → 8 (runner); full smoke
  247 → 249 passing; `flow_selftest.py` PASSED.

### Migration

Pure bugfix — no schema, capability, or CLI surface changes. Re-run
`flow install render-prompts` is **not** required (no rendered prompts
touched). After upgrading, `flow waves --preview <slug>` works from any
project containing a `.flow/` directory.

## v0.7.0 — 2026-05-05

### Major: Dependency-aware parallel subagent dispatch

Phase 2 now supports `wave-dispatch` mode: implementer subagents that touch disjoint files run in parallel within a "wave"; cross-wave runs sequential. Conservative defaults (cap=3, mechanical disjointness primary, LLM concept-veto, post-hoc git-diff verification, shared-artifact denylist).

**New skills**:
- `flow:flow-wave-planner` — decompose plan into waves, cache reproducibly
- `flow:flow-wave-runner` — paired implementer/spec-reviewer dispatch + sequential merge + code-quality reviewer

**New capabilities**: `wave_planning`, `wave_dispatch`.

**New CLI**:
- `flow waves --preview <slug>` / `--show <slug>` / `--invalidate <slug>` — wave decomposition inspection
- `flow doctor` — extended with writes hygiene, broad-glob, SHARED_ARTIFACTS overlap, stale cache
- `flow doctor --suggest-writes <slug>` — advisory `writes:` suggestions for legacy plans

**Plan schema**: optional `### Tasks` YAML block in progress.md with per-task `writes:` glob (and `reads:` hint). Plans without `### Tasks` block fall back to all-serial (zero regression).

**Capability fallback**: missing `wave_planning` or `wave_dispatch` → automatic fallback to existing v0.6 dispatch path (zero regression for users without v0.7).

### Backwards compatibility

- All v0.6.x plans run unchanged
- Capability registry baseline preserved (no removals)
- No behavior changes for existing flow doctor / flow task / flow promote subcommands

### Spec

Full design: `docs/superpowers/specs/2026-05-05-v0.7-parallel-dispatch-design.md` (3 rounds of codex consult absorbed: round-1 architecture, round-2 bug fixes including LLM-veto-only / per-task pre-post diff / SHARED_ARTIFACTS glob-overlap / failed_blocking-non-waivable, round-3 verification fixes including per-task pre-post sha verifier and contiguous-prefix planner).

### Out of scope (deferred to v0.8+)

- Cross-phase parallel
- Subagent-to-subagent direct communication within a wave
- Auto-learning from historical plan executions

## v0.6.1 (2026-05-05)

Patch addressing the two Minor follow-ups from v0.6.0 final code review
(closes #9, #10). Both were "documentation / observability gaps that
don't break anything but defer surprise to runtime." Folded into a single
patch since they're small and complementary.

### Fixed

- **#9 — `safety_guardrails` cross-phase discoverability**: each phase
  SKILL.md (`flow-phase{1,2,3,4}-*/SKILL.md`) now begins with a one-line
  blockquote referencing `{{capability:safety_guardrails}}` and pointing
  to the orchestrator's `§Cross-cutting capabilities` section. Previously
  the safety reminder was only in `flow-orchestrator/SKILL.md`, which is
  not loaded once a phase skill takes over. Now the reminder is in active
  context regardless of which phase Claude is executing. Per-phase
  destructive-op examples tailored to that phase's typical operations
  (Phase 2: `git reset --hard` / migrations / `kubectl delete`; Phase 3:
  `git branch -D` / force-push / `git clean -fd`; etc).

- **#10 — capability `requires_cli` is now consumed by `flow doctor`**:
  new `check_capability_clis()` walks the registry and warns if any
  capability's declared `requires_cli` dependency is missing. Mixed-
  semantics aware: `requires_cli` may name either a PATH binary
  (e.g. `codex`) OR a Claude skill bundle under `~/.claude/skills/<name>/`
  (e.g. `gstack`). New helper `_is_dependency_available()` checks both
  locations. Pre-existing in v0.5: `cross_model_*` capabilities had
  `requires_cli` set but nothing consumed it; v0.6.0 added 11 more
  entries with the same pattern. v0.6.1 closes the loop — `flow doctor`
  now warns "gstack not available — affects 11 capability/ies: …" if the
  user hasn't installed gstack, surfacing what would silently no-op.

### Added — tests

- `tests/smoke/test_doctor_capability_clis.py` — 4 tests covering
  `_is_dependency_available()` (skill bundle / PATH binary / missing) and
  `check_capability_clis()` (no-crash + warning emission when missing).
  Suite total grows 15 → 19.

### Migration

Pure additive — no schema changes, no removed capabilities. Re-run
`flow install render-prompts` after upgrade to refresh phase SKILL.md
files with the new safety blockquote.

## v0.6.0 (2026-05-05)

Capability registry expansion — wires 19 new capabilities from gstack /
superpowers / pr-review-toolkit / planning-with-files / code-review into
Flow's per-phase orchestration. Capability count grows 14 → 33. Phase 1
gains hat-shifted brainstorming (Engineer / DX / Security perspectives)
that replaces gstack:plan-*-review's batched output with one-question-
at-a-time UX consistent with `superpowers:brainstorming`.

### Added — Phase 1 (2 capabilities + hat-shift)

- `multi_step_plan` → `planning-with-files:plan` (B/C-size tasks)
- `dev_setup` → `gstack:setup-deploy` (deploy task initialization)
- Phase 1 SKILL.md hat-shifted brainstorming continuation
  (Engineer / DX / Security hats; user picks 0-N; same one-question-
  at-a-time rhythm as base brainstorm)

### Added — Phase 2 (5 capabilities)

- `subagent_discipline` → `superpowers:subagent-driven-development`
  (pairs with parallel_dispatch — discipline + orchestration)
- `execute_plan_discipline` → `superpowers:executing-plans`
  (closes loop with multi_step_plan)
- `systematic_debug` → `superpowers:systematic-debugging`
  (4-phase root-cause discipline; first-line debug)
- `deep_investigate` → `gstack:investigate`
  (escalation when systematic_debug insufficient)
- `land_and_deploy` → `gstack:land-and-deploy`
  (alt to deploy_chain; one-shot for small confident changes)

### Added — Phase 3 (8 capabilities)

- **`verify_completion`** → `superpowers:verification-before-completion`
  **MANDATORY at Phase 3 entry — closes a security-class gap where
  Flow previously allowed self-reported success without actual
  verification. Non-skippable.**
- `code_review_small` → `code-review:code-review`
  (5 Sonnet parallel + Haiku confidence; diff < 200 lines)
- `code_review_large` → `pr-review-toolkit:review-pr`
  (6-specialist agent panel; diff ≥ 200 lines)
- `review_request_etiquette` →
  `superpowers:requesting-code-review,superpowers:receiving-code-review`
  (request scope discipline + verify-before-agreeing chain)
- `pre_land_review` → `gstack:review`
  (SQL safety / LLM trust / conditional side effects)
- `quality_health` → `gstack:health`
  (composite 0-10 quality score; Phase 3 entry gate)
- `perf_baseline` → `gstack:benchmark`
  (Web Vitals + resource size regression; perf-sensitive tasks)
- `post_deploy_qa` → `gstack:qa`
  (active deployed-site QA; complements canary's passive monitoring)

### Added — Phase 4 (2 capabilities)

- `branch_finish` → `superpowers:finishing-a-development-branch`
  (structured merge / PR / cleanup decision)
- `changelog_gen` → `gstack:changelog-generator`
  (auto-generate user-facing changelog from commit history)

### Added — Cross-cutting (2 capabilities)

- `safety_guardrails` → `gstack:careful`
  (destructive command warnings — orchestrator invokes before
  rm -rf / DROP TABLE / force-push / kubectl delete / migrations.
  Hook-based auto-fire deferred to v0.7)
- `weekly_retro` → `gstack:retro`
  (cross-task weekly review; user-triggered or `/loop weekly`)

### Added — defensive infrastructure

- `scripts/flow_capability.py`: `load_registry()` strips `_`-prefixed
  keys from capabilities and model_roles dicts (defends against marker-key
  AttributeError in `flow_skill_diff.analyze_plugin` and noise pollution
  in `cmd_list` output).
- `tests/smoke/test_capability.py`: new `test_v06_additions_are_well_formed`
  asserts each v0.6.0 capability has dict shape + `default` (str) +
  `description` fields.

### Out of scope (rejected during design)

- `plan_ceo_critique` (gstack:plan-ceo-review) — user opted out
- `autoplan` (gstack:autoplan) — bundles all 4 plan-*-review
- `plan_eng_critique` / `plan_devex_critique` — replaced by hat-shift
- `release_docs` / `project_learnings` / `security_audit` /
  `silent-failure-hunter` — deferred to v0.7

### Migration

Pure additive — no existing capability removed or renamed. Project-level
overrides in `.flow/config.local.yaml` continue to work. Re-run
`flow install render-prompts` after upgrade to substitute new
`{{capability:X}}` placeholders into `~/.claude/{commands,skills}/flow/`.

### Tests

`tests/smoke/test_capability.py` REQUIRED_CAPS extended 13 → 33 entries.
Suite total grows 14 → 15 tests (new: `test_v06_additions_are_well_formed`).
All pass + flow_selftest.py PASSED.

## v0.5.9 (2026-05-05)

Cosmetic / UX cleanup. Fresh `flow init` no longer leaves the project in a
state where `git status` perpetually flags `?? .flow/workspace/` and
`?? .flow/config.yaml` as untracked. The `.gitignore` rules were already
correct (`.flow/workspace/*` + `!.flow/workspace/.gitkeep`), but `flow init`
never told the user to stage the un-ignored placeholder + project config —
so every adopter saw the same noisy untracked entries forever.

### Fixed

- **`.gitignore` duplicates** (this repo): removed the orphan "defensive"
  block (`.flow/.runtime/` + `.flow/config.local.yaml`) that duplicated
  the canonical "Flow Framework" block lower down.
- **Self-bootstrapping noise** (this repo): `.flow/workspace/.gitkeep`
  and `.flow/config.yaml` are now committed, so the workspace dir
  materializes after clone and project-level config is reproducible.

### Added

- **`flow init` next-step hint** (`scripts/flow_init.py`): on completion,
  print the exact `git add` command for the un-ignored placeholders that
  `flow init` just created. No auto-staging — the user is in control.

## v0.5.8 (2026-05-05)

Phase determination now respects user's authoritative `phase:` declaration in
progress.md frontmatter as an upper bound on section-based advancement. Fixes
the brainstorm-stuck-at-phase3 bug where logging brainstorm milestones
(sub-agent dispatches, decision lock entries) into `## Execute Log` during
phase 1 caused `<flow-state>` to mis-report the task as `phase3-finish` even
though the user was still iterating on prd.md and the architecture wasn't
locked.

### Fixed

- **Phase-state spurious advancement** (`claude/hooks/user-prompt-submit.py`):
  `determine_phase` now combines section-based heuristic with the frontmatter
  `phase:` field as an upper-bound cap. If the user explicitly sets
  `phase: triage` or `phase: research`, the hook returns `phase1-plan`
  regardless of how much content lives in `## Execute Log`. The frontmatter
  value is the authoritative declaration of "where I am"; `/flow:continue` is
  responsible for advancing it. Section heuristic still wins in the inverse
  direction (stale frontmatter `implement` with empty Plan → phase1-plan from
  sections), so a forgotten frontmatter advance can't promote past actual
  artifact reality.

### Added

- New helpers in `claude/hooks/user-prompt-submit.py`: `parse_frontmatter_phase`
  (extracts and maps the YAML `phase:` field; defensive against malformed YAML),
  `min_phase` (returns the earlier of two canonical phases per `PHASE_ORDER`).
  Constants: `PHASE_ORDER`, `PHASE_FRONTMATTER_MAP`.
- Test coverage in `tests/smoke/test_phase_determination.py`: 21 new tests
  across `FrontmatterPhaseParse`, `MinPhase`, `FrontmatterPhaseCap` classes —
  including the actual user-reported regression case (frontmatter triage +
  Plan/Execute filled with brainstorm artifacts → must return phase1-plan)
  and the cross-model codex review finding (sections-all-filled → must reach
  done even when frontmatter caps at `sediment`).

### Cross-model review notes

Per cross-model codex review on this fix, the section heuristic is now allowed
to short-circuit the frontmatter cap when `section_phase == "done"`. Why:
`PHASE_FRONTMATTER_MAP` has no key for `done` (the frontmatter enum tops out at
`sediment`), so without this short-circuit, a fully-completed task with
`phase: sediment` and all sections filled would forever report `phase4-sediment`
instead of `done`. Two regression tests added.

## v0.5.7 (2026-05-05)

Stabilization release. Fixes the Sonnet alias env-var routing that was
silently downgrading sub-agent dispatch (or forcing operators to fall
back to haiku — research-depth-inadequate), and the phase-state
"false-done" bug where automated autosave breadcrumbs in
`progress.md ## Sediment Notes` made `<flow-state>` mis-report any task
as `done` from Phase 1 onward. Adds an explicit Sonnet → Opus fallback
chain to the sub-agent dispatch protocol so operators don't silently
downgrade. Wires the new `external_skills` dependency section into
`flow install` + `flow doctor` (gstack is now properly diagnosed and
installable). Per cross-model codex review, the breadcrumb-filtering
regex now consumes the full line so migrated tasks with old breadcrumbs
in Sediment Notes don't falsely advance past Phase 4.

### Fixed

- **Agent tool model dispatch**: `defaults.json` `model_roles.*.default`
  now ships **aliases** (`sonnet`/`opus`/`haiku`), not full model IDs.
  The Agent tool's `model` parameter is enum-restricted to those three
  aliases — full IDs were rejected with `InputValidationError`. Added
  per-role `fallback` field documenting the retry alias on dispatch
  failure.
- **Phase-state false `done`** (`claude/hooks/user-prompt-submit.py`):
  `is_section_filled` now filters the autosave breadcrumb pattern
  (defense-in-depth); `determine_phase` now requires **sequential**
  filling (Plan → Execute → Verify → Sediment), so a stray write to a
  later section can't skip earlier empty sections.
- **Sediment Notes pollution source** (`scripts/flow_autosave.py`):
  the `distill queued` breadcrumb is re-routed from
  `progress.md ## Sediment Notes` to
  `~/.flow/.runtime/autosave-log-<cwd>.md` (out-of-band of any
  phase-determining file).
- **Breadcrumb regex consumes full line** (codex review P1): old regex
  anchored on the prefix only, leaving residual ` (trigger=...) — note`
  text that still counted as section content. Now matches up to the
  newline so migrated old progress.md files don't falsely advance phase.

### Added

- `external_skills` section in `dependencies.json` for loose-skill
  bundles distributed outside the marketplace+plugin system. First
  entry: **gstack** (`~/.claude/skills/gstack/`) with `requires_cli`,
  `capabilities`, and a documented `install` command.
- `flow install install-external-skills` — new subcommand that clones
  + builds bundles declared under `external_skills`, idempotent on
  existing installs, fails-closed on missing required CLIs.
- `flow doctor` — new section reporting external skills presence +
  missing CLIs; entries count toward total-missing for exit code.
- `claude/commands/flow/start.md` + `claude/skills/flow/flow-phase1-plan/SKILL.md`:
  explicit **Sub-agent dispatch protocol** — primary `sonnet` alias,
  fallback `opus`, never haiku for research depth.
- `tests/smoke/test_phase_determination.py` — 17 unit tests covering
  `is_section_filled` across all observed breadcrumb variants,
  `determine_phase` sequential AND-chain, and three regression tests
  for the false-`done` scenarios.
- `.flow/pitfalls/{agent-sonnet-alias-stale, phase-state-triple-bug,
  context-mode-mcp-flake, flow-protocol-needs-fallback-chain}.md` — 4
  pitfall captures so future tasks reference these failure modes.

### Changed

- `claude/hooks/stop.py` docstring updated to reflect breadcrumb
  destination change (was: progress.md Sediment Notes; now: runtime log).
- `claude/capabilities/defaults.json` `_comment` updated to reflect
  alias-based dispatch and the role of `ANTHROPIC_DEFAULT_*_MODEL`
  env vars in concrete-id resolution (1M-context variants
  recommended).

### Tests

- Suite: 80 → 97 (+17 in `test_phase_determination.py`).

## v0.5.6 (2026-05-04)

Two new CLI surfaces resolving GitHub issues #4 and #5. Both were
deferred from v0.5.3 as feature additions.

### Added

- **`flow task phase <name>`** (#4) — advance the current task's phase
  field via CLI instead of hand-editing frontmatter. Validates against
  the enum (triage|research|implement|check|verify|sediment), atomic-writes
  via `safe_io.locked_text_rmw`, appends an Execute Log entry, and
  records a `phase_transition` event in `history.jsonl` when
  `.checkpoint/` exists. Accepts `--slug` for non-active tasks.
- **`flow sediment <type> <slug>`** (#5) — render a pitfall/pattern/ADR
  from `templates/`, write to `.flow/{pitfalls,patterns,ADRs}/`, and
  link from the active task's progress.md `## Sediment Notes`. ADRs
  auto-number (`0001-`, `0002-`, ...) by scanning existing files;
  explicit `0042-` prefix in slug is respected. Per-type flags:
  `--severity` (pitfall), `--tier` (pattern). Eliminates Phase 4's
  manual boilerplate (template select → substitute → write → link).

### Tests

- Suite: 143 → 155 (+12 across `test_flow_task_cli.py` extension and
  new `test_flow_sediment.py`).

## v0.5.5 (2026-05-04)

Patch release — fix #7: `dependencies.json` referenced a nonexistent
`andrejkarpathy/karpathy-skills` GitHub repo. Discovered by v0.5.4's
cleaner SSH→HTTPS error path, which removed the SSH failure that had
been masking the underlying 404.

### Fixed

- **#7 nonexistent karpathy-skills repo** — changed marketplace source
  from `andrejkarpathy/karpathy-skills` (404) to `forrestchang/andrej-karpathy-skills`
  (the actual fork). Local marketplace identifier `karpathy-skills` is
  unchanged so existing installs aren't disturbed.

## v0.5.4 (2026-05-04)

Patch release — single-fix for the SSH-only marketplace clone failure
flagged in PR #1's "Notes for reviewer" and reproduced on this machine
during v0.5.3 selftest.

### Fixed

- **`flow install` SSH→HTTPS fallback** — `cmd_register_marketplaces` and
  `cmd_install_plugins` now inject `GIT_CONFIG_COUNT/KEY/VALUE` env vars
  into their `claude plugin` subprocess calls, telling git (2.31+) to
  rewrite `git@github.com:` to `https://github.com/` for the duration
  of that subprocess only. Eliminates the "SSH host key not in
  known_hosts" failure path on machines without SSH keys configured.
  Zero impact on installs that already worked (the rewrite is a no-op
  when the URL is already HTTPS).

## v0.5.3 (2026-05-04)

Patch release with 6 fixes — 2 local L3-dogfood findings + PR #1 (path
placeholder fix from yang1997434) + 3 GitHub issues from other-machine
smoke testing.

### Fixed

- **PR #1** (path placeholders) — replaced 11 hardcoded `~/projects/flow-framework/`
  occurrences across 5 source files with `{{REPO_ROOT}}/` so the renderer
  in `flow_install.py` substitutes the real install path on every machine.
  Plus 2 regression checks in `flow_selftest.py`.
- **L-1 progress.md write race** — `post-tool-bash.append_commit_to_progress`
  and `post-tool-edit.upsert_files_section` both did unsync'd RMW on
  `progress.md`; concurrent fires could lose one section's update. Fix:
  added `safe_io.locked_text_rmw()` helper using fcntl.LOCK_EX; both
  hooks routed through it. New unit test verifies 8 threads × 25
  appends produce 200 distinct entries with no loss.
- **L-2 `/flow:pause` Step 6 task_path bug** — prompt did
  `Path(".flow/tasks") / Path(.current-task content)` but `.current-task`
  already contains the full relative path. Fixed to read directly.
- **Issue #6 `flow doctor` context-mode false-negative** — added a third
  positive signal: read `~/.claude/settings.json` `enabledPlugins` and
  match `context-mode@context-mode`. Doctor no longer warns on green
  installs.
- **Issue #3 `flow task archive` slug + finish ordering** — `archive`
  now strips `^\d{2}-\d{2}-` prefix so both dated and bare-slug forms
  work. `finish` no longer clears `.current-task` (leaves it for
  `archive`). The natural copy-paste from `flow task list` now works.
- **Issue #2 `/flow:codex-review` non-git fallback** — detects whether
  cwd is a git repo and falls back to `codex exec --skip-git-repo-check -`
  with a content-built prompt when not. Restores the "any project" promise.

### Tests

- Suite: 135 → 143 (+4 from `LockedTextRmw` test class, +4 from new
  `test_flow_task_cli.py` covering Issue #3 archive slug + finish
  ordering).
- Selftest now rejects rendered files containing `{{REPO_ROOT}}` or
  `projects/flow-framework` — regression guard from PR #1.

## v0.5.2 (2026-05-04)

Patch release fixing a test-isolation bug.

### Fixed

- **Hook integration tests leaked state into real `~/.flow/.runtime/`** — 3 of
  the 4 v0.5 tests (`test_v05_precompact_hook.py`, `test_v05_e2e.py`,
  `test_v05_sessionstart_compact.py`) invoked hook scripts via
  `subprocess.run` without an `env=` override, so the hook scripts read
  `FLOW_HOME` from the parent process and wrote test-task nudge-state
  files (`nudge-state-01-01-demo.json`, `nudge-state-01-01-e2e.json`) into
  the user's actual runtime dir. Discovered during v0.5.1 framework
  validation. Fix: pin `FLOW_HOME` to a per-test tempdir in each
  `subprocess.run` `env=` arg, mirroring `test_v05_postool_integration.py`'s
  `_isolated_runtime_env` pattern.

### Tests

- All 3 fixed tests now create a `runtime` tempdir in setUp and pass it via
  `env=` to subprocess hook invocations. tearDown cleans up.
- Suite total still 135 (no test added; isolation is internal).

## v0.5.1 (2026-05-04)

Patch release covering two bugs found during v0.5.0 final validation
that were deferred to a follow-up.

### Fixed

- **`credential_grep` `(?i)` regex prefix** — GNU `grep -E` treats `(?i)` as
  a literal optional group, breaking the intended case-insensitive scan.
  The `-i` flag (already passed) provides case-insensitivity correctly.
  Local dev environments with `ugrep` aliased as `grep` masked the bug.
- **Selftest dry-fire fixtures** for `pre-compact.py` and `post-tool-edit.py`
  — these v0.5 hooks were validated by `flow doctor` isolation check +
  live invocation, but absent from `flow_selftest.py::HOOK_FIXTURES`.

### Tests

- Restored the credential-leak integration test case in
  `test_v05_postool_integration.py` that v0.5.0's polish work had to
  substitute due to the credential_grep regex bug.
- Smoke suite total: 134 → 135.
- Selftest hook dry-fire count: 5 → 7.

## v0.5.0 (2026-05-04)

Foundation for **auto-resume on context pressure**. Manual flow hardening
+ infra for v0.6.0 autopilot. Spec at `docs/specs/2026-05-04-auto-resume-design.md`.

### Highlights

- **PreCompact hook** — writes mechanical snapshot before Claude Code auto-compacts.
- **Per-task `.checkpoint/`** — `intent.md` + `mechanical.json` + `history.jsonl`
  capture in-flight state. `.gitignored` by default.
- **Atomic writes + fcntl.flock** — all v0.5+ state files go through `safe_io.py`.
  Concurrent appends to `history.jsonl` proven race-free under 8 threads.
- **Append-only hint outbox** — replaces single-file cascade hint that codex
  pre-merge review flagged as lossy.
- **Context-pressure nudge** — PostToolUse hook estimates context % from
  `transcript_path`, suggests `/flow:pause` once per compact cycle when ≥50%.
  Best-effort: model relays text, user sees in conversation.
- **Enhanced `/flow:pause`** — writes intent.md snapshot + cascade hint.
- **Enhanced `/flow:resume`** — reads checkpoint, surfaces Next Action,
  warns on staleness.
- **SessionStart on `compact`** — restores intent + mechanical context after
  auto-compact, model awaits user signal (no auto-execute).

### Added

- `scripts/common/safe_io.py` — atomic_write_text / atomic_write_json /
  append_jsonl_locked
- `scripts/common/hint_outbox.py` — write_hint / list_pending / mark_processed
- `scripts/common/context_estimator.py` — estimate_context_pct
- `scripts/common/checkpoint_paths.py` — per-task path helpers
- `scripts/common/mechanical.py` — build_payload (mechanical.json schema)
- `scripts/common/nudge.py` — maybe_nudge_text / acknowledge / rotate_window
- `claude/hooks/pre-compact.py` — PreCompact hook
- `tests/smoke/test_v05_*.py` — 8 new test modules including
  `test_v05_postool_integration.py` (subprocess-driven hook integration).
  Suite total: 73 → 133 cases.

### Changed

- `claude/hooks/post-tool-bash.py` — adds nudge + throttled mechanical update
- `claude/hooks/post-tool-edit.py` — adds nudge + throttled mechanical update
- `claude/hooks/session-start.py` — compact-matcher branch reads checkpoint
- `claude/commands/flow/pause.md` — Steps 6-8 (intent.md + hint + ack)
- `claude/commands/flow/resume.md` — Step 0 (personal /resume hint) + 1.5
- `scripts/flow_install.py` — `pre-compact.py` added to FLOW_OWNED_MARKERS
- `scripts/flow_init.py` — propagates `.checkpoint/` to project `.gitignore`
- `claude/hooks/settings.template.json` — PreCompact entry

### Not yet shipped (deferred to v0.6.0)

- `/flow:start --autopilot` and autopilot state machine
- R5 sanity check via external evidence (downgrade-only)
- Hard budgets (tool calls / files / time)
- Destructive-command denylist
- Explicit `done_when` checklist replacing completion-promise

These are designed in the spec but require dogfooding v0.5.0 first.

## v0.4.0 (2026-05-04)

Major v0.4 release. Addresses 9 sub-projects identified in the v0.3.1 audit
(`05-04-audit-flow-issues`). Production goal: **断点能记 / 长期记忆能沉淀 / 踩坑能沉淀**.

### Highlights

- **Capability registry** — prompt files no longer hard-code skill names. 13 capabilities
  + 5 model roles, all overridable via `flow.config.local.yaml`. Skill churn → 1 yaml line.
- **Real-automation install** — `install.sh` declarative (`dependencies.json`-driven):
  registers marketplaces, installs plugins via `claude plugin install`, merges hooks into
  `~/.claude/settings.json` with isolated matcher entries (Issue #415 mitigation), runs
  `flow selftest` to prove it actually works.
- **Worktree-per-task (default `shared`, opt-in `worktree`)** — task isolation that doesn't
  poison the shared tree on cross-task switches. `flow status` / `flow switch` UX added.
- **Three-tier autosave** — Lv1 trickle (git commit / file touch append) / Lv2 phase-boundary
  / Lv3 distill-on-pause-or-stop with cooldown + heartbeat. Layer-1 raw persistence delegated
  to context-mode plugin (hard dep).
- **Ralph bash-loop** — Phase 2 alt mode for autonomous PRD-checklist runs. `scripts/flow_ralph.sh`
  reimplements the Anthropic ralph-wiggum pattern in bash to avoid Stop-hook collisions.
- **Skill diff hook** — SessionStart compares installed plugins to last snapshot, scores
  capability overlap (Szymkiewicz–Simpson), surfaces "consider replacing X with Y" suggestions.
- **Rule integration** — flow phases explicitly reference `~/.claude/rules/code-delivery.md`,
  `code-review.md`, `knowledge-base.md`; new `behavioral_guidelines` capability invokes
  Karpathy's guidelines at the implement boundary.

### Added

- `dependencies.json` — declarative manifest (system commands / marketplaces / plugins)
- `claude/capabilities/defaults.json` — built-in capability + model_role mapping
- `scripts/flow_capability.py` — resolver + template renderer (dotted access supported)
- `scripts/flow_install.py` — install orchestrator (5 subcommands + dry-run)
- `scripts/flow_doctor.py` — environment diagnostic incl. hook-isolation + context-mode check
- `scripts/flow_selftest.py` — functional verification (hooks dry-fire / init / task-roundtrip /
  plugins / rendered prompts / doctor recap)
- `scripts/flow_skill_diff.py` — capability overlap analysis with per-(spec, version) cache
- `scripts/flow_autosave.py` — Lv1/Lv2/Lv3 orchestrator (queue-based; LLM distill deferred to
  next interactive session, never invoked from hook context)
- `scripts/flow_ralph.sh` — Phase 2 ralph-loop wrapper (375 LOC, dry-run + fake-mode for tests)
- `claude/hooks/post-tool-edit.py` — Lv1 file-touch tracking with debounce
- `claude/hooks/settings.template.json` — replaces old `.snippet`; templated `{{REPO_ROOT}}`
- `tests/smoke/` — 73 unittest cases + 12 ralph bash-test cases (was: empty in v0.3.1)

### Changed

- `claude/skills/flow/*/SKILL.md` and `claude/commands/flow/*.md` — every concrete skill name /
  model name replaced with `{{capability:X}}` / `{{model:Y}}` placeholder; rendered at install
- `install.sh` — slim orchestrator over `flow_install.py`; auto-removes legacy symlinks before
  rendering; runs selftest at end and fails install if non-functional
- `claude/hooks/stop.py` — removed raw save (delegated to context-mode); now triggers Lv3 distill
  with 5-min cooldown
- `claude/hooks/post-tool-bash.py` — added Lv1 git-commit append (hash-uniqueness debounce);
  preserved credential grep
- `claude/hooks/session-start.py` — runs `flow_skill_diff.py diff --quiet` (best-effort, 8s
  timeout) and surfaces pending suggestions in injected context
- `scripts/flow_task.py` — worktree creation + `.location` file + `cmd_status` (tree view) +
  `cmd_switch` (eval-friendly cd output) + worktree cleanup on archive
- `templates/flow.config.yaml.template` — new fields: `task_isolation`, `phase2_mode`, `autosave`
- `templates/progress.md.template` — added YAML frontmatter (`slug` / `status` / `phase` /
  `blocked_by`) for `cmd_status` to read

### Fixed (P0 from audit)

- `claude/hooks/pre-tool-task.py:62` — removed `or True` debug leftover
- `scripts/flow_task.py:cmd_archive` — capture `was_current` BEFORE `shutil.move` (was: any
  archive cleared `.current-task` pointer regardless of which task was archived)
- `scripts/flow_promote.py:rewrite_frontmatter_for_promotion` — extracted as pure function;
  `strip()` instead of `rstrip()` to avoid blank-line accumulation across re-promotions

### Removed

- `claude/hooks/settings.json.snippet` — superseded by `settings.template.json`

### Pitfalls captured this cycle

1. **render write-through symlink** — first install attempt wrote rendered output back into
   source templates because `~/.claude/skills/flow/` was a legacy symlink to repo. Fixed by
   adding fail-loud detection in `flow_install.py render-prompts` + auto-unlink in `install.sh`.
2. **Sub-agent model selection** — dispatching with `model: sonnet` failed silently when that
   model wasn't accessible in the local environment. Now: don't specify model unless certain.
3. **`shutil.move` then query stale state** — pattern repeats across the codebase; archive
   logic in `flow_task.py` was the surface case but worth promoting to vault.

### Acknowledgments

v0.4 absorbed two external plugins as dependencies:
- [`mksglu/context-mode`](https://github.com/mksglu/context-mode) (Elastic 2.0) — Layer-1 raw
  session persistence + tool output sandboxing
- Anthropic's `ralph-wiggum` pattern — reimplemented in bash to avoid Stop-hook collisions

## v0.3.1-alpha (2026-05-04 later)

Sandbox dogfood test exposed bugs + completed three stubs.

### Bug fixes from dogfood test

- **Phase detection in `user-prompt-submit.py`** — was matching template placeholder
  text ("main session", "promoted", etc.) and reporting fresh task as "done".
  Rewrote `determine_phase` to extract section-by-section and check for non-comment,
  non-template content. Verified across 5 progressive states (empty → done).
- **`progress.md.template`** — simplified to use clear `<!-- TEMPLATE: 未填写 -->`
  markers in each section. Phase detector now reliably distinguishes filled vs
  template state.
- **`flow_conflict.py` regex** — original directive matcher required polarity at
  start of sentence (after `^` / `.\s+` / list bullet). Fixed to use `\b` word
  boundary anywhere; added stop-word filter for subject overlap; lowered
  threshold from 0.5 to 0.4 (with min-based denominator for sensitivity).

### Stubs completed (P1 + P2)

- **`flow_staleness.py` (P1)** — full implementation:
  - Scans memory files for cited path patterns (`.py`, `.ts`, `.md`, etc.)
  - Verifies path existence (project-relative, repo-relative, absolute)
  - Cross-references with `git log -N` to detect "modified after memory written"
  - Outputs human-readable findings or JSON for hook consumption
  - Exits non-zero on stale findings (CI-integrable)
- **`flow_promote.py` (P2)** — criteria validation:
  - Counts archived task mentions, vault MOC mentions, active project mentions
  - Cap warnings (Letta-anchored: vault pattern <300 lines / rules <200 lines)
  - `--check-only` mode prints metrics without promoting
  - `--force` to override criteria; `--confirm-rule` required for Lv3 rules tier
  - Credential grep self-check before write
  - Updates source frontmatter with `status: promoted` + target path + date
- **`pre-tool-task.py` jsonl injection hook (P2)** — Trellis-style:
  - Triggers on `Task` / `Agent` tool calls
  - Reads active task's `implement.jsonl` or `check.jsonl` (heuristic by prompt content)
  - Loads referenced spec files, injects into sub-agent prompt as `additionalContext`
  - 50 KB total / 10 KB per-file caps to avoid context bloat
  - Best-effort: silent exit on missing jsonl

### Stubs completed (P3)

- **`flow_conflict.py`** — heuristic conflict detection:
  - Extracts directives (always/never/must/should + Chinese equivalents) from rules
  - Pairs across files where polarity is opposite + subject overlap ≥ 0.4
  - Outputs suspect pairs for human/Claude review (not auto-resolution)
  - JSON output for hook consumption; scope flag (project/vault/global/all)

### Added

- `flow conflict` subcommand in CLI dispatcher
- `LICENSE` (MIT)
- `docs/USAGE.md` — step-by-step install / first-time setup / daily workflow / troubleshooting / cross-machine sync

### Smoke test results (dogfood task: token-counter CLI)

End-to-end run of all 4 phases on `/tmp/flow-test-real`:
- ✅ flow init → skeleton + .gitignore + ~/.flow/credentials.local
- ✅ flow task create → prd.md + progress.md from templates
- ✅ Phase 1-4 simulation (manual fill of prd.md, then implement, verify, sediment)
- ✅ flow save → journal entry with title / machine_id / status / commits
- ✅ flow task archive → moved to archive/2026-05/
- ✅ All 5 hooks: silent on no-op, correct output on triggers
- ✅ All scripts: idempotent, exit codes correct

### Still deferred

- Triage uses heuristic, not actual Haiku call (slash command guides the model)
- Auto-save does not auto-update `~/.claude/projects/.../memory/MEMORY.md` (call `/save` skill manually)
- Conflict detection is heuristic (false positives expected); LLM-based verification deferred

## v0.3.0-alpha (2026-05-04)

Initial extraction from vault into installable repo.

### Added

- Repo skeleton + bilingual README + install/uninstall scripts
- All 4 design docs mirrored from vault (`编码框架.md`, `Skills-Phase映射.md`, `框架对比.md`, `调研方法论.md`)
- 9 templates (prd / progress / pitfall / server / topology / ADR / pattern / config / config.local + gitignore snippet)
- 8 slash commands under `claude/commands/flow/` (start / continue / finish / resume / pitfall / promote / codex-review / pause)
- 5 skills under `claude/skills/flow/` (orchestrator + 4 phase skills)
- 4 hooks under `claude/hooks/` (session-start / user-prompt-submit / post-tool-bash / stop)
- Python helpers: `flow_init.py`, `flow_task.py`, `flow_save.py`, `flow_triage.py`, `flow.py` (CLI dispatcher)
- Common utilities: `paths.py`, `config.py`, `git.py`

### Status

- Foundation laid; not yet validated on real coding project
- Auto-checks (`flow_staleness.py`, `flow_conflict.py`, `flow_promote.py`) stubbed
- pre-tool-task.py jsonl injection hook stubbed (Trellis-style spec injection — not yet implemented)

### Known limitations

- Triage uses heuristic, not actual Haiku call (slash command guides the model to do classification)
- Auto-save writes to journal but does not auto-update `~/.claude/projects/.../memory/MEMORY.md` pointers (manual step)
- No staleness verification on session start (planned v0.3.1)
- No conflict pre-flight on rule load (planned v0.3.1)

## Predecessor: vault v0.2.1 (2026-05-04 earlier)

The design that this repo implements. See `docs/编码框架.md` CHANGELOG section for design history.
