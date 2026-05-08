codex
I’ll treat this as a design-review pass rather than implementation. I’m going to check shell edge cases against the proposed recognizers and focus on concrete ways the hook can pass or block incorrectly.
codex
I’ve identified one important class that is not in the draft examples: shell quoting can split the literal tokens themselves, so a regex looking for contiguous `git commit` can be skipped before bashlex ever runs. I’m doing a quick local shell sanity check on a few of those forms so the repros are precise.
exec
/bin/bash -lc "bash -lc 'git(){ printf \"GIT:%s\\n\" \""'$*"; }; G=git C=commit; $G $C -m x'"'" in /data/Claude/flow-framework
exec
/bin/bash -lc "bash -lc 'git(){ printf \"GIT:%s\\n\" \""'$*"; }; g'"\\it com\\mit -m x'" in /data/Claude/flow-framework
 succeeded in 0ms:
GIT:commit -m x

 succeeded in 0ms:
GIT:commit -m x

exec
/bin/bash -lc "bash -lc 'git(){ printf \"GIT:%s\\n\" \""'$*"; }; gi""t com""mit -m x'"'" in /data/Claude/flow-framework
 succeeded in 0ms:
GIT:commit -m x

exec
/bin/bash -lc 'bash -lc $'"'git(){ printf \"GIT:%s\\\\n\" \""'$*"; }; git '"\\\\\\ncommit -m x'" in /data/Claude/flow-framework
 succeeded in 0ms:
GIT:commit -m x

codex
The main pattern is clear now: the proposed prescreen is fast but not conservative enough, so several valid Bash spellings of `git commit` never reach the AST logic. I’m also checking the marker section for cases where “write-tree equals reviewed state” may be weaker than it sounds.
ERROR: Reconnecting... 1/5
exec
/bin/bash -lc "rg --files -g 'bashlex*' -g 'SKILL.md' -g '*.py' | head -100" in /data/Claude/flow-framework
 succeeded in 0ms:
claude/hooks/pre-compact.py
claude/hooks/post-tool-bash.py
claude/hooks/user-prompt-submit.py
claude/hooks/pre-tool-task.py
claude/hooks/post-tool-edit.py
claude/hooks/stop.py
claude/hooks/session-start.py
claude/skills/flow/flow-phase2-execute/SKILL.md
claude/skills/flow/flow-phase1-plan/SKILL.md
claude/skills/flow/flow-wave-runner/SKILL.md
claude/skills/flow/flow-phase4-sediment/SKILL.md
claude/skills/flow/flow-phase3-finish/SKILL.md
claude/skills/flow/flow-wave-planner/SKILL.md
claude/skills/flow/flow-orchestrator/SKILL.md
tests/unit/test_notification_throttle_semantics.py
tests/unit/test_idempotent_in_flight_resume.py
tests/unit/test_context_estimator_1m_resolution.py
tests/unit/test_merge_queue_serialization.py
tests/smoke/test_e2e_v0_8_2_p0.py
tests/smoke/test_install_logic.py
tests/smoke/test_wave_planner.py
tests/smoke/test_v05_precompact_hook.py
tests/smoke/test_v080_unknown_field_compat.py
tests/smoke/test_paused_clock.py
tests/smoke/test_orchestrator_dryrun.py
tests/smoke/test_atomic_merge_crash_blocks.py
tests/smoke/test_phase2_retry_loop.py
tests/smoke/test_subagent_dispatch_count.py
tests/smoke/test_v07_e2e.py
tests/smoke/test_v05_e2e.py
tests/smoke/test_capability.py
tests/smoke/test_phase_determination.py
tests/smoke/test_semantic_retry_whitelist_violations.py
tests/smoke/test_acceptance.py
tests/smoke/test_reviewer_blindspot_mount.py
tests/smoke/test_dispatch_template.py
tests/smoke/test_v0_8_0_e2e.py
tests/smoke/test_v05_safe_io.py
tests/smoke/test_acceptance_retry_matrix.py
tests/smoke/test_orchestrator_rejects_too_new_schema.py
tests/smoke/test_phase3_behavior_e2e_no_retry.py
tests/smoke/test_v05_hint_outbox.py
tests/smoke/test_dual_counter_invariants.py
tests/smoke/test_flow_task_cli.py
tests/smoke/test_v05_sessionstart_compact.py
tests/smoke/test_nested_autonomy_aborts.py
tests/smoke/test_flow_sediment.py
tests/smoke/test_budget_counter.py
tests/smoke/test_auto_prepare_lock_crash_blocks.py
tests/smoke/test_orchestrator_worktree.py
tests/smoke/test_post_merge_verify_failed_blocks.py
tests/smoke/test_doctor_capability_clis.py
tests/smoke/test_no_silent_mode_switch.py
tests/smoke/test_flow_acceptance_cli.py
tests/smoke/test_subagent_dispatch_shim.py
tests/smoke/test_v05_context_estimator.py
tests/smoke/test_post_auto_engaged_crash_blocks.py
tests/smoke/test_v05_mechanical.py
tests/smoke/test_autosave.py
tests/smoke/test_worktree_isolation.py
tests/smoke/test_p0_fixes.py
tests/smoke/test_criterion_timeout_blocks.py
tests/smoke/test_doctor_contract.py
tests/smoke/test_wave_runner_helpers.py
tests/smoke/test_v05_postool_integration.py
tests/smoke/test_skill_diff.py
tests/smoke/test_glob_overlap.py
tests/smoke/test_state_writer.py
tests/smoke/test_afk_signals.py
tests/smoke/test_afk_timeout.py
tests/smoke/test_exit_codes_module.py
tests/smoke/test_contract.py
tests/smoke/test_p1_hardening.py
tests/smoke/test_v05_postool_nudge.py
scripts/flow_waves.py
scripts/flow_notification.py
scripts/flow_doctor.py
scripts/flow_autosave.py
scripts/flow_save.py
scripts/flow_install.py
scripts/flow_staleness.py
scripts/flow.py
scripts/flow_selftest.py
scripts/flow_orchestrator.py
scripts/flow_triage.py
scripts/flow_conflict.py
scripts/flow_capability.py
scripts/flow_promote.py
scripts/flow_state_writer.py
scripts/flow_skill_diff.py
scripts/flow_task.py
scripts/flow_wave_planner.py
scripts/flow_sediment.py
scripts/flow_contract.py
scripts/flow_subagent_dispatch.py
scripts/dispatch_template.py
scripts/flow_acceptance.py
scripts/flow_acceptance_cli.py
scripts/flow_wave_runner.py
scripts/flow_init.py

exec
/bin/bash -lc 'find . -maxdepth 4 -type d -name bashlex -print' in /data/Claude/flow-framework
 succeeded in 0ms:
codex
**A. Pre-Screen Regex Bypasses**

1. **Critical**: Git global options bypass prescreen.
Repro: `git -c user.name=x commit -m bypass` has no literal `git commit`.
Fix: Prescreen on command-position `git`, not adjacent `git commit`; let parser decide subcommand after git options.

2. **Critical**: Shell quoting/escaping splits literal tokens.
Repro: `g\it com\mit -m bypass` or `gi""t com""mit -m bypass`.
Fix: Do not rely on raw contiguous token text; parse/dequote shell words before matching.

3. **High**: Expansion constructs commit at runtime.
Repro: `G=git C=commit; $G $C -m bypass` or `git ${C} -m bypass`.
Fix: Block or require review for unresolved expansions in command name / git subcommand position; otherwise accept as known limitation.

**B. AST Traversal Completeness**

1. **Critical**: `parts[1] == commit` misses valid git global-option forms.
Repro: `git -C repo commit -m x`, `git --git-dir=.git commit -m x`.
Should: **BLOCK**.
Fix: Skip git global options and their operands before identifying the subcommand.

2. **High**: Only “top-level” command nodes misses executed nested commands.
Repro: `(git commit -m x)`, `false || git commit -m x`, `echo msg | git commit -F -`, `git commit -m x &`.
Should: **BLOCK**.
Fix: Recursively walk executable command nodes in lists, pipelines, subshells, background jobs, command/process substitutions.

3. **High**: Command identity normalization is incomplete.
Repro: `/usr/bin/git commit -m x`, `PATH=. git commit -m x`, `\git commit -m x`.
Should: **BLOCK**.
Fix: Skip leading assignments, normalize quote removal/escapes, and compare `basename(argv0) == git`.

**C. ParsingError Fallback Safety**

1. **Critical**: Quoted heredoc can force parse failure, then hide real commit after delimiter.
Repro:
```bash
cat <<'EOF'
x
EOF
git commit -m bypass
```
Fix: On parse error after prescreen hit, fail closed or implement heredoc-aware stripping before fallback.

2. **High**: Fallback reintroduces leading-command bypass.
Repro:
```bash
cat <<'EOF'
x
EOF
touch /tmp/x && git commit -m bypass
```
Fix: Never use first-line-only PASS as a safety fallback.

3. **Medium**: Non-shell subprocess commits remain out of scope.
Repro:
```bash
python3 <<'EOF'
import subprocess; subprocess.run(["git","commit","-m","x"])
EOF
```
Fix: Accept as known limitation unless policy expands beyond Bash AST detection.

**D. Marker Semantics**

1. **Critical**: `git commit -a` / pathspec commits can include unstaged work not represented by `git write-tree`.
Repro: review staged tree, then edit tracked file unstaged, run `git commit -am bypass`.
Fix: Block `-a/--all`, `--include`, `--only`, and pathspec commit forms unless computing the actual would-be commit tree.

2. **High**: Marker must be single-use and explicit.
Repro: Multi-use marker allows repeated `git commit --allow-empty -m x` while tree SHA stays unchanged.
Fix: Consume marker on successful validation; document single-use.

3. **High**: Marker is global and lacks repo/worktree identity.
Repro: Review repo A with tree SHA X, commit repo B with same tree SHA X.
Fix: Include canonical git dir/worktree identity in marker and validate against target repo, including `git -C` / `--git-dir`.

**E. Vendoring + Supply Chain**

1. **Medium**: Vendoring from GitHub master is not reproducible.
Repro: Reinstall later gets different parser code.
Fix: Pin commit hash, record source URL/checksum/license, vendor reviewed snapshot only.

2. **Medium**: `sys.path` insertion path may be wrong.
Repro: If source is `_vendor/bashlex/__init__.py`, inserting `_vendor/bashlex` makes `import bashlex` look for `_vendor/bashlex/bashlex`.
Fix: Insert `_vendor`, then `import bashlex`; test in clean `python -S` environment.

3. **Low**: Transitive/import assumptions need hermetic test.
Repro: Spike works from clone because extra files are on path; installed hook lacks parser/yacc support files.
Fix: Add hook selftest that imports vendored bashlex and parses representative commands with no external site-packages.

**F. Performance**

1. **Medium**: Large commands remain unbounded.
Repro: 1MB heredoc or generated shell payload causes parse latency/memory spike.
Fix: Add input size cap and timeout; fail closed with clear reason when exceeded.

2. **Medium**: `git write-tree` cost is repo-dependent.
Repro: Large monorepo/index or slow filesystem makes marker validation exceed expected 50ms.
Fix: Timeout `write-tree`; fail closed on timeout/error.

3. **Low**: Broader prescreen will import Python for more `git` commands.
Repro: `git status` now triggers parser if prescreen becomes conservative.
Fix: Accept cost or add cheap shell lexer prefilter; correctness should win.

**G. K-Class Brief Language**

1. **Medium**: “禁止 `<noop> && git commit`” is too narrow and teaches the bypass shape.
Repro: Agent uses `printf x >/tmp/y && git commit -m x` or `git -c x=y commit`.
Fix: Phrase as “do not hide, wrap, indirectly invoke, or combine `git commit` to bypass review.”

2. **Low**: “noop” may over-restrict legitimate directory setup.
Repro: Agent avoids valid `cd repo && git status` or misclassifies `cd repo && git commit`.
Fix: Say commits must follow review flow and be issued plainly in the intended repo; non-commit setup commands are fine.

3. **High**: Marker prohibition must cover all mutations, not just `touch`.
Repro: `printf '{"schema_version":1,...}' > ~/.claude/hooks/.review-passed`.
Fix: Ban create/modify/delete/chmod/mtime edits to marker and hook/vendor files.

**Verdict: RED**

Prescreen and parse-error fallback both have critical bypasses. Address those before implementation.
tokens used
17,360
**A. Pre-Screen Regex Bypasses**

1. **Critical**: Git global options bypass prescreen.
Repro: `git -c user.name=x commit -m bypass` has no literal `git commit`.
Fix: Prescreen on command-position `git`, not adjacent `git commit`; let parser decide subcommand after git options.

2. **Critical**: Shell quoting/escaping splits literal tokens.
Repro: `g\it com\mit -m bypass` or `gi""t com""mit -m bypass`.
Fix: Do not rely on raw contiguous token text; parse/dequote shell words before matching.

3. **High**: Expansion constructs commit at runtime.
Repro: `G=git C=commit; $G $C -m bypass` or `git ${C} -m bypass`.
Fix: Block or require review for unresolved expansions in command name / git subcommand position; otherwise accept as known limitation.

**B. AST Traversal Completeness**

1. **Critical**: `parts[1] == commit` misses valid git global-option forms.
Repro: `git -C repo commit -m x`, `git --git-dir=.git commit -m x`.
Should: **BLOCK**.
Fix: Skip git global options and their operands before identifying the subcommand.

2. **High**: Only “top-level” command nodes misses executed nested commands.
Repro: `(git commit -m x)`, `false || git commit -m x`, `echo msg | git commit -F -`, `git commit -m x &`.
Should: **BLOCK**.
Fix: Recursively walk executable command nodes in lists, pipelines, subshells, background jobs, command/process substitutions.

3. **High**: Command identity normalization is incomplete.
Repro: `/usr/bin/git commit -m x`, `PATH=. git commit -m x`, `\git commit -m x`.
Should: **BLOCK**.
Fix: Skip leading assignments, normalize quote removal/escapes, and compare `basename(argv0) == git`.

**C. ParsingError Fallback Safety**

1. **Critical**: Quoted heredoc can force parse failure, then hide real commit after delimiter.
Repro:
```bash
cat <<'EOF'
x
EOF
git commit -m bypass
```
Fix: On parse error after prescreen hit, fail closed or implement heredoc-aware stripping before fallback.

2. **High**: Fallback reintroduces leading-command bypass.
Repro:
```bash
cat <<'EOF'
x
EOF
touch /tmp/x && git commit -m bypass
```
Fix: Never use first-line-only PASS as a safety fallback.

3. **Medium**: Non-shell subprocess commits remain out of scope.
Repro:
```bash
python3 <<'EOF'
import subprocess; subprocess.run(["git","commit","-m","x"])
EOF
```
Fix: Accept as known limitation unless policy expands beyond Bash AST detection.

**D. Marker Semantics**

1. **Critical**: `git commit -a` / pathspec commits can include unstaged work not represented by `git write-tree`.
Repro: review staged tree, then edit tracked file unstaged, run `git commit -am bypass`.
Fix: Block `-a/--all`, `--include`, `--only`, and pathspec commit forms unless computing the actual would-be commit tree.

2. **High**: Marker must be single-use and explicit.
Repro: Multi-use marker allows repeated `git commit --allow-empty -m x` while tree SHA stays unchanged.
Fix: Consume marker on successful validation; document single-use.

3. **High**: Marker is global and lacks repo/worktree identity.
Repro: Review repo A with tree SHA X, commit repo B with same tree SHA X.
Fix: Include canonical git dir/worktree identity in marker and validate against target repo, including `git -C` / `--git-dir`.

**E. Vendoring + Supply Chain**

1. **Medium**: Vendoring from GitHub master is not reproducible.
Repro: Reinstall later gets different parser code.
Fix: Pin commit hash, record source URL/checksum/license, vendor reviewed snapshot only.

2. **Medium**: `sys.path` insertion path may be wrong.
Repro: If source is `_vendor/bashlex/__init__.py`, inserting `_vendor/bashlex` makes `import bashlex` look for `_vendor/bashlex/bashlex`.
Fix: Insert `_vendor`, then `import bashlex`; test in clean `python -S` environment.

3. **Low**: Transitive/import assumptions need hermetic test.
Repro: Spike works from clone because extra files are on path; installed hook lacks parser/yacc support files.
Fix: Add hook selftest that imports vendored bashlex and parses representative commands with no external site-packages.

**F. Performance**

1. **Medium**: Large commands remain unbounded.
Repro: 1MB heredoc or generated shell payload causes parse latency/memory spike.
Fix: Add input size cap and timeout; fail closed with clear reason when exceeded.

2. **Medium**: `git write-tree` cost is repo-dependent.
Repro: Large monorepo/index or slow filesystem makes marker validation exceed expected 50ms.
Fix: Timeout `write-tree`; fail closed on timeout/error.

3. **Low**: Broader prescreen will import Python for more `git` commands.
Repro: `git status` now triggers parser if prescreen becomes conservative.
Fix: Accept cost or add cheap shell lexer prefilter; correctness should win.

**G. K-Class Brief Language**

1. **Medium**: “禁止 `<noop> && git commit`” is too narrow and teaches the bypass shape.
Repro: Agent uses `printf x >/tmp/y && git commit -m x` or `git -c x=y commit`.
Fix: Phrase as “do not hide, wrap, indirectly invoke, or combine `git commit` to bypass review.”

2. **Low**: “noop” may over-restrict legitimate directory setup.
Repro: Agent avoids valid `cd repo && git status` or misclassifies `cd repo && git commit`.
Fix: Say commits must follow review flow and be issued plainly in the intended repo; non-commit setup commands are fine.

3. **High**: Marker prohibition must cover all mutations, not just `touch`.
Repro: `printf '{"schema_version":1,...}' > ~/.claude/hooks/.review-passed`.
Fix: Ban create/modify/delete/chmod/mtime edits to marker and hook/vendor files.

**Verdict: RED**

Prescreen and parse-error fallback both have critical bypasses. Address those before implementation.
