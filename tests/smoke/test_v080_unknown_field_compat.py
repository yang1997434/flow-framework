"""T3 smoke: v0.8.0 reader forward-compat on v0.8.1 contract.

Design refs:
- design/v0.8.1-execution-semantics.md line 319 — required smoke test
- design forward-compat semantics: Q2.2 + Q5.1 + R8 + PRD §1.1
  unknown-field-warning rule.

Y9 fix (codex round-3 plan review): test runs the **actual v0.8.0 tag
binary** (`scripts/flow.py contract --validate`) via `git worktree add`,
NOT the live (mutated) parser in this worktree. This isolates the
forward-compat assertion from T1/T2 schema changes — otherwise the test
would be checking the new code against the new contract, which is
tautological.

Pass condition: v0.8.0's `flow contract --validate` exits 0 on a
v0.8.1-shaped contract (with `max_codex_rounds_per_task`,
`notification.throttle_min`/`tier2_enabled`, `idempotent_cmd_allowlist`,
`post_merge_regression_optional`, criterion-level `method`,
`timeout_sec`, `idempotent`, `post_merge_skip`). v0.8.0 may either
silently accept these or list them as unknowns — both are acceptable per
the design's warn-and-keep rule.

Fail condition: v0.8.0 crashes (Traceback) or returns non-zero. That
would mean v0.8.0's forward-compat promise is broken; per plan T3 step
3.2 escalate to user — do NOT patch the test.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _v080_tag_available() -> bool:
    """True iff `v0.8.0` resolves to a commit in the local repo.

    Codex T3 round 1 [P2]: shallow CI clones often skip tags; if the test
    just `git worktree add ...v0.8.0`s without checking, a missing tag turns
    the suite red even though no production code regressed. We'd rather
    skip with a clear reason than confuse a forward-compat regression with
    "tags not fetched".
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "v0.8.0^{commit}"],
            cwd=str(REPO_ROOT),
            capture_output=True,
        )
        return result.returncode == 0
    except (OSError, FileNotFoundError):
        return False


class TestV080ReaderHandlesV081Contract(unittest.TestCase):
    """Run the v0.8.0 tag binary against a v0.8.1 contract."""

    def setUp(self):
        if not _v080_tag_available():
            self.skipTest(
                "v0.8.0 tag not present in local repo (shallow clone? tags "
                "not fetched?). Run `git fetch --tags` to enable this smoke. "
                "Skip is environment-fragility, not a code regression."
            )
        self.tmp = Path(tempfile.mkdtemp(prefix="t3_v080_compat_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _v080_worktree(self) -> Path:
        """Create an ephemeral detached worktree at v0.8.0; return its path.

        Uses the worktree's own git repo as the source — `git worktree add
        --detach <path> v0.8.0` checks out the tag without disturbing this
        worktree's branch. Cleaned up via addCleanup so a test failure does
        not leak worktrees.
        """
        wt = self.tmp / "v080"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt), "v0.8.0"],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt)],
                cwd=str(REPO_ROOT),
                capture_output=True,
            )
        )
        return wt

    def test_v080_reader_accepts_v081_fields(self):
        v080 = self._v080_worktree()

        # cwd for the validate call. v0.8.0's `_resolve_slug_dir` walks UP
        # from cwd looking for `.flow/`. We create `.flow/tasks/demo/` inside
        # `cwd_dir` so it resolves at the first parent (= cwd_dir itself).
        cwd_dir = self.tmp / "project"
        slug_dir = cwd_dir / ".flow" / "tasks" / "demo"
        slug_dir.mkdir(parents=True)

        # v0.8.1-shape contract: deliberately includes every additive field
        # T1 introduced — top-level (`idempotent_cmd_allowlist`,
        # `post_merge_regression_optional`, `budget.max_codex_rounds_per_task`,
        # `notification.throttle_min`, `notification.tier2_enabled`) and
        # criterion-level (`method`, `timeout_sec`, `idempotent` object,
        # `post_merge_skip`). v0.8.0 must not crash on any of these.
        contract = {
            "contract_schema_version": 1,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-06T00:00:00Z",
            "budget": {"max_codex_rounds_per_task": 3},
            "notification": {"throttle_min": 5, "tier2_enabled": True},
            "idempotent_cmd_allowlist": ["pytest"],
            "post_merge_regression_optional": False,
            "acceptance_criteria": [
                {
                    "description": "smoke",
                    "type": "smoke",
                    "method": "cmd",
                    "command": "true",
                    "timeout_sec": 30,
                    "idempotent": {
                        "value": True,
                        "rationale": "noop",
                        "timeout_sec": 30,
                        "side_effect_class": "pure",
                    },
                    "post_merge_skip": False,
                }
            ],
        }
        (slug_dir / "contract.json").write_text(json.dumps(contract))

        result = subprocess.run(
            [
                sys.executable,
                str(v080 / "scripts" / "flow.py"),
                "contract",
                "--validate",
                "demo",
            ],
            cwd=str(cwd_dir),
            capture_output=True,
            text=True,
        )

        # Forward-compat hard requirement: no crash, exit 0.
        self.assertEqual(
            result.returncode,
            0,
            f"v0.8.0 reader crashed/rejected v0.8.1 contract.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}",
        )
        # Belt-and-braces: even if returncode were 0 from a swallowed
        # exception, no Python traceback should appear in either stream.
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Traceback", result.stdout)


if __name__ == "__main__":
    unittest.main()
