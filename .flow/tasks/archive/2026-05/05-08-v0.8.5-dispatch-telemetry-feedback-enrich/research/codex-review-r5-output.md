**Verdict: GREEN**

P0: none  
P1: none  
P2: none

I3-A is correct. `_collect_unstaged()` now uses bare `git diff --stat` and bare `git diff -U0 --no-color`, which is working tree vs index, not working tree vs HEAD: [diff_summary.py](/data/Claude/flow-framework/.claude/worktrees/agent-aa426c11070d2f2f5/scripts/common/diff_summary.py:259). The staged-only test genuinely covers the double-count bug: it stages `f.py`, makes no further WT edit, asserts staged sees it and unstaged does not: [test_diff_summary_no_double_count_v085.py](/data/Claude/flow-framework/.claude/worktrees/agent-aa426c11070d2f2f5/tests/unit/test_diff_summary_no_double_count_v085.py:65). Full-summary staged-only once is also covered at line 138. Committed/staged/untracked collectors are otherwise untouched, so R2 I3’s main branches remain intact.

I6’s mock-boundary claim mostly holds. The test only patches `_invoke_subagent_dispatch` and `_run_shell_with_pgkill`: [test_v085_production_path.py](/data/Claude/flow-framework/.claude/worktrees/agent-aa426c11070d2f2f5/tests/smoke/test_v085_production_path.py:421). It calls `_phase2_dispatch` without `deps_factory`, so it reaches production `dispatch_with_retry`, `_prod_impl`, `_prod_review`, real Round 2 `git worktree add`, real `derive_task_facts`, real prompt assembly, real `_build_prev_round_diff_summary`, and real `GateRunner.run_phase2`.

Sharp nuance: `_run_shell_with_pgkill` is shared by gate1/gate4/gate6, so the mock intercepts baseline/smoke too, not only the codex CLI. The gate methods still run. Also the Round 1 RED payload is malformed for production issue parsing because it lacks `file`, `line_range`, `class`, `message`; gate4 therefore returns `inconclusive`, which `_prod_review` maps to retry `"fail"`. That does not break I6, but do not claim this test validates the parsed RED-issue path.

The test does create the intended disk states: committed `a.py`, staged `b.py`, unstaged modification to `a.py`, and untracked `c.py`: [test_v085_production_path.py](/data/Claude/flow-framework/.claude/worktrees/agent-aa426c11070d2f2f5/tests/smoke/test_v085_production_path.py:340). The Round 2 prompt is captured through `_invoke_subagent_dispatch`’s `prompt_prefix`, not by direct prompt-builder invocation: line 370.

No host worktree pollution concern. The test creates a temp repo, and `create_task_worktree()` places linked worktrees under that temp repo’s `.claude/worktrees`: [flow_orchestrator.py](/data/Claude/flow-framework/.claude/worktrees/agent-aa426c11070d2f2f5/scripts/flow_orchestrator.py:458). `TemporaryDirectory` is enough cleanup in normal CI.

I agree the duplicate hunk-block issue is cosmetic, not P2 must-fix. `_render()` iterates `file_stats`, so same-path multi-source stats can repeat the merged hunk block: [diff_summary.py](/data/Claude/flow-framework/.claude/worktrees/agent-aa426c11070d2f2f5/scripts/common/diff_summary.py:410). It can waste line budget, but the stat block remains correct, no code content leaks, and no changed file is falsely represented. Deferring to v0.8.6 is defensible.

I1/I2/I4 still look correct: codex telemetry is emitted from `GateRunner.gate4_codex_review` with a real monotonic bracket; outcome normalization is enforced before JSONL write; Round N+1 enrichment prefers `current_round_ctx` and uses its true base commit, with no `HEAD~1` fallback.

I could not rerun the targeted tests here: this sandbox has no writable temp dir, so `TemporaryDirectory()` fails before test bodies execute. Static review is clean.