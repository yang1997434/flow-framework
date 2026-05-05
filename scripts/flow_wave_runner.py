"""flow wave runner — runtime helpers.

Most of the wave runtime logic lives in claude/skills/flow/flow-wave-runner/SKILL.md
(controller follows it during execution). This module contains the
deterministic pieces that benefit from Python: git operations, subset checks,
and waiver log appending.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
_common = str(REPO_ROOT / "scripts" / "common")
if _common not in sys.path:
    sys.path.insert(0, _common)

from glob_overlap import _matches  # reuse the regex matcher  # noqa: E402


def diff_names_between_shas(repo_dir: str | Path, pre_sha: str, post_sha: str) -> list[str]:
    """Return the list of file paths changed between pre_sha and post_sha.

    Per-task scope: this is the spec's per-task verifier, NOT cumulative
    against wave base. See spec §Wave runtime > Time slot 2.
    """
    result = subprocess.check_output(
        ["git", "diff", "--name-only", f"{pre_sha}..{post_sha}"],
        cwd=str(repo_dir),
        text=True,
    )
    return [line.strip() for line in result.splitlines() if line.strip()]


def verify_subset_of_writes(actual: list[str], declared: list[str]) -> tuple[bool, list[str]]:
    """Strict subset check: every actual file must match some declared glob.

    Returns (ok, violations). violations is empty when ok is True.
    """
    violations = []
    for f in actual:
        matched = any(_matches(d, f) for d in declared)
        if not matched:
            violations.append(f)
    return (len(violations) == 0, violations)


def append_waiver(waiver_log_path: Path, *, task_id: str, state: str, rationale: str) -> None:
    """Append a waiver entry to .flow/tasks/<slug>/wave-decisions.log."""
    import datetime
    waiver_log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    line = f"[{timestamp}] WAIVE task={task_id} state={state} rationale={rationale!r}\n"
    with waiver_log_path.open("a", encoding="utf-8") as f:
        f.write(line)


def cli_diff_names(args) -> int:
    repo = args.repo or REPO_ROOT
    files = diff_names_between_shas(repo, args.pre, args.post)
    print("\n".join(files))
    return 0


def cli_verify_subset(args) -> int:
    actual = args.actual.split() if args.actual else []
    declared = args.declared.split(",") if args.declared else []
    ok, violations = verify_subset_of_writes(actual, declared)
    if not ok:
        print(f"VIOLATIONS: {violations}", file=sys.stderr)
        return 1
    return 0


def cli_waive(args) -> int:
    log_path = REPO_ROOT / ".flow" / "tasks" / args.task_slug / "wave-decisions.log"
    append_waiver(log_path, task_id=args.task_id, state=args.state, rationale=args.rationale)
    return 0


def main():
    ap = argparse.ArgumentParser(description="flow wave runner helpers")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_dn = sub.add_parser("diff-names")
    p_dn.add_argument("--pre", required=True)
    p_dn.add_argument("--post", required=True)
    p_dn.add_argument("--repo", default=None)
    p_dn.set_defaults(func=cli_diff_names)

    p_vs = sub.add_parser("verify-subset")
    p_vs.add_argument("--actual", required=True, help="space-separated actual paths")
    p_vs.add_argument("--declared", required=True, help="comma-separated declared globs")
    p_vs.set_defaults(func=cli_verify_subset)

    p_wv = sub.add_parser("waive")
    p_wv.add_argument("--task-slug", required=True)
    p_wv.add_argument("--task-id", required=True)
    p_wv.add_argument("--state", required=True)
    p_wv.add_argument("--rationale", required=True)
    p_wv.set_defaults(func=cli_waive)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
