codex
I’m checking one marker detail against the current repo: whether `git rev-parse --git-common-dir` is stable from subdirectories, because the marker design depends on canonical repo identity from the hook CWD.
exec
/bin/bash -lc 'git rev-parse --git-common-dir && realpath "$(git rev-parse --git-common-dir)"' in /data/Claude/flow-framework
exec
/bin/bash -lc 'git rev-parse --git-common-dir && realpath "$(git rev-parse --git-common-dir)"' in /data/Claude/flow-framework/scripts
 succeeded in 0ms:
.git
/data/Claude/flow-framework/.git

 succeeded in 0ms:
../.git
/data/Claude/flow-framework/.git

codex
**A. Critique Issues**

- Your wrapper conclusion is wrong. Your own bashlex data shows `command git commit`, `env git commit`, `eval "git commit"`, and `bash -c "git commit"` parse as argv0 `command` / `env` / `eval` / `bash`, not as a nested `git` command. Current `find_commit_invocation()` returns `None`, so the hook would PASS unless you add explicit wrapper blocking.

- Your env-assignment conclusion is implementation-dependent and currently wrong in pseudocode. You say bashlex stores assignments in `node.assignments`, but the code only scans `node.parts` for `kind == "assignment"`. As written, `GIT_INDEX_FILE=/tmp/idx git commit -m x` is likely missed.

- Inline alias bypass is not pre-existing user config and is still in scope.
  Repro: `git -c alias.ci=commit ci -m x`
  Current finder sees argv `[git, -c, alias.ci=commit, ci, -m, x]`; no exact subcommand `commit`, so PASS.

Everything else in your critique about static dequoting is fair: bashlex 0.18 does dequote `git""`, `g\it`, `gi""t`, `\git`.

**B. In-Scope Bypasses**

- High: compound command can mutate index after marker validation.
  Repro: `git add new.py && git commit -m x`
  Narrowing: require the entire Bash input to be exactly one simple root command: `git commit ...`; reject lists, `&&`, `;`, subshells, background, pipelines.

- High: wrappers still pass.
  Repro: `command git commit -m x`, `env -u GIT_INDEX_FILE git commit -m x`, `bash -c "git commit -m x"`
  Narrowing: block wrapper/evaluator/interpreter commands when raw command contains both `git` and `commit`, or require sole-root `git commit`.

- High: inline git alias via `-c` bypasses literal-subcommand detection.
  Repro: `git -c alias.ci=commit ci -m x`
  Narrowing: reject any `git -c ...` form, not only `git -c ... commit`.

- High: command substitution can mutate index before `git commit` executes.
  Repro: `git commit -m "$(git add new.py; echo msg)"`
  Narrowing: reject command/process substitution inside any approved commit argv word.

- High: first-match return allows later unsafe commits.
  Repro: `git commit -m ok && git commit -a -m unsafe`
  Narrowing: collect all commit-like invocations; reject if count is not exactly one. Sole-root command rule also fixes this.

**C. Pseudocode Bugs**

- `assigns` collection is wrong: read `node.assignments`, not only `node.parts`.

- `/usr/bin/git commit -m x` is misclassified as `__plain__` because flavor ignores `argv[0]`.
  Fix: `__plain__` only when `argv[0] == "git"` exactly.

- `find_commit_invocation()` returns first match, not all matches.
  Fix: collect matches plus AST context; reject non-root or multiple matches.

- `__nonplain__` detection only catches exact word `commit`, missing `git -c alias.ci=commit ci`.
  Fix: reject all git global options before deciding whether a commit was found, especially `-c`.

- Marker check should validate `ts` as well as mtime.
  Fix: reject if either payload `ts` or file mtime is stale/skewed.

- Marker should bind at least `HEAD` and probably worktree gitdir/top-level, not only common-dir + tree.
  Repro: reviewed tree reused after branch/worktree switch.
  Fix: include `git rev-parse HEAD`, `--show-toplevel`, and `--git-dir` canonicalized.

- Avoid shelling through `xargs realpath`.
  Repro: repo path with spaces.
  Fix: use subprocess args and Python `Path.resolve()`.

**Verdict: RED**

White-list direction is right, but current design still allows common LLM-shaped bypasses: `git add && git commit`, wrappers, command substitution, and inline `git -c alias.*`. The main fix is simple but structural: only allow a single root simple command whose argv is exactly plain `git commit ...`, with no assignments, wrappers, compound syntax, substitutions, global git options, or extra commit invocations.
tokens used
