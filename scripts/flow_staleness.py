#!/usr/bin/env python3
"""flow staleness — verify memories cite paths that still exist + are not stale.

Usage:
  flow_staleness.py [--scope project|vault|all] [--json] [--recent N]

  --scope: which memory tier to check (default: project)
  --json: output JSON (for hook consumption)
  --recent N: flag as stale if cited path was modified in last N commits AND
              memory file hasn't been touched since (default: 5)

A memory entry is "stale" if any of:
  1. Cited path no longer exists
  2. Cited symbol/function no longer matches (heuristic — checks if name still appears in cited file)
  3. Cited path was modified in last N commits AND memory file is older than that change
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.paths import get_flow_dir


# Match path-like strings (heuristic) referenced in memory files
PATH_PATTERN = re.compile(
    r"`([./\w-]+\.(?:py|ts|tsx|js|jsx|md|yaml|yml|json|sh|sql|rs|go|java|cpp|c|h))`"
)


@dataclass
class StaleFinding:
    memory_file: str
    cited_path: str
    reason: str  # "missing" | "modified-after-memory" | "symbol-not-found"
    detail: str = ""


def get_file_last_modified(path: Path) -> datetime | None:
    """Return path's mtime as UTC datetime, or None if missing."""
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def git_log_recent(path: Path, n: int, repo_root: Path) -> list[tuple[str, datetime]]:
    """Return [(sha, commit_time), ...] for last n commits touching path."""
    try:
        result = subprocess.run(
            ["git", "log", f"-{n}", "--format=%H|%cI", "--", str(path)],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    commits = []
    for line in result.stdout.strip().splitlines():
        if "|" in line:
            sha, ct = line.split("|", 1)
            try:
                commits.append((sha, datetime.fromisoformat(ct)))
            except ValueError:
                continue
    return commits


def find_repo_root(start: Path) -> Path | None:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".git").is_dir():
            return cur
        cur = cur.parent
    return None


def scan_memory_file(memory: Path, project_root: Path, recent_n: int) -> list[StaleFinding]:
    """Scan a single memory file for stale references. Return findings."""
    findings: list[StaleFinding] = []

    text = memory.read_text(encoding="utf-8", errors="replace")
    paths = set(PATH_PATTERN.findall(text))
    if not paths:
        return findings

    memory_mtime = get_file_last_modified(memory)
    repo_root = find_repo_root(project_root) or project_root

    for cited in paths:
        # Resolve cited path
        candidates = [
            project_root / cited,
            repo_root / cited,
            Path(cited).expanduser() if Path(cited).is_absolute() else None,
        ]
        existing = next((c for c in candidates if c and c.is_file()), None)

        if existing is None:
            findings.append(StaleFinding(
                memory_file=str(memory),
                cited_path=cited,
                reason="missing",
                detail=f"Path does not exist (checked {len(candidates) - candidates.count(None)} candidates)",
            ))
            continue

        # Check if path was modified after memory was written
        commits = git_log_recent(existing, recent_n, repo_root)
        if commits and memory_mtime:
            most_recent_change = max(c[1] for c in commits)
            if most_recent_change > memory_mtime:
                findings.append(StaleFinding(
                    memory_file=str(memory),
                    cited_path=cited,
                    reason="modified-after-memory",
                    detail=(
                        f"File modified at {most_recent_change.isoformat()}, "
                        f"memory written at {memory_mtime.isoformat()}. "
                        f"Recent commit: {commits[0][0][:8]}"
                    ),
                ))

    return findings


def collect_targets(scope: str, flow: Path) -> list[Path]:
    targets = []
    if scope in ("project", "all") and flow.is_dir():
        for sub in ("ADRs", "patterns", "pitfalls"):
            d = flow / sub
            if d.is_dir():
                targets += list(d.glob("*.md"))
    if scope in ("vault", "all"):
        vault = Path.home() / "data" / "knowledge-base"
        if vault.is_dir():
            for sub in ("patterns", "pitfalls", "ADRs"):
                d = vault / sub
                if d.is_dir():
                    targets += list(d.glob("*.md"))
    return targets


# ---------------------------------------------------------------------------
# T20 (v0.8.1) — task-workspace staleness (5 explicit Y2 triggers)
#
# Distinct domain from the memory-file scanner above:
#   - scan_memory_file / StaleFinding (top of file): does memory cite paths
#     that still exist? — kept for backward compat with `flow staleness`
#     subcommand which `flow.py` still routes to this module's main().
#   - StalenessChecker / StalenessVerdict (this section): is the task
#     workspace still aligned with the ground-truth state captured at task
#     start? — Y2 design §1 row 8.
#
# Both coexist because they share the verb "stale" but neither replaces
# the other. v0.8.1 ships StalenessChecker via `flow doctor` only (Step
# 20.8); the orchestrator-side caller is **DEFERRED to v0.8.2** per the
# route-A re-scope (codex round-4 R4 + round-5 R3).
#
# Pitfall guards used here:
#   - D5 (typed except): every disk-touching helper isolates expected
#     errors (OSError / FileNotFoundError / PermissionError) and returns
#     a benign sentinel; un-anticipated errors propagate.
#   - L (type-check vs presence): snapshot dicts are compared with
#     `isinstance(value, str)` before hash-equality so a corrupt snapshot
#     entry can't silently match a real hash.
#   - K + I (helper reuse for subprocess): trigger 5 (baseline_now_fails)
#     does NOT call `subprocess.run(shell=True, timeout=...)` directly —
#     it imports `_run_shell_with_pgkill` from `flow_orchestrator` (T12
#     helper) so SIGKILL on timeout takes the whole process group, not
#     just the shell.
#   - S (wire-up gap): T20 wires StalenessChecker into `flow_doctor.main`
#     ONLY. Adding a caller into `auto_dispatch_task` / `_cmd_auto_execute`
#     is explicitly v0.8.2 scope — see plan rows 7220, 7232, 7520-7524.
# ---------------------------------------------------------------------------


@dataclass
class StalenessVerdict:
    """Aggregate verdict from `StalenessChecker.check_all`.

    `stale` is True when ANY of the 5 triggers fired. `triggered` is the
    ordered list of trigger names that fired (subset of
    {base_branch, lockfile, prd_mtime, dep_version, baseline_fail}).
    `details` carries per-trigger forensic info (e.g. base-branch from/to
    commits, list of changed lockfiles, baseline command exit code +
    stderr tail) for surfaces (doctor / future blocked.md frontmatter)
    that need to explain WHY the workspace is stale.
    """
    stale: bool
    triggered: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


class StalenessChecker:
    """5 explicit Y2 trigger checks (PRD §1.1 + design §1 row 8).

    Trigger 1 (base_branch) needs a `WorktreeContext` so we can compare
    `original_base_commit` to the current rev of `integration_target`.
    Triggers 2-4 work off a pre-captured snapshot dict (lockfile hashes,
    prd.md mtime, dep-file hashes) — in v0.8.1 those snapshots are NOT
    captured by the orchestrator (doctor-only scope) so the doctor
    compares snapshot=current = no trigger by definition unless caller
    passes their own pre-saved snapshot. Trigger 5 re-runs the baseline
    command in the worktree — only fires when `baseline_was_passing` is
    True and current run returns non-zero.

    The class is stateless apart from constructor args; no mutation, no
    caching. Callers can construct freely.
    """

    # NOTE: kept as tuple (immutable) so external callers can't mutate
    # the canonical list at runtime.
    LOCKFILES: tuple[str, ...] = (
        "package-lock.json",
        "Cargo.lock",
        "requirements.txt",
        "poetry.lock",
        "go.sum",
        "Gemfile.lock",
    )

    DEP_FILES: tuple[str, ...] = (
        "package.json",
        "pyproject.toml",
        "Cargo.toml",
        "go.mod",
    )

    def __init__(
        self,
        *,
        repo_root: Path,
        ctx: Any,                      # WorktreeContext-shaped (duck-typed)
        task_dir: Path,
        baseline_snapshot: Optional[dict] = None,
    ):
        self.repo_root = repo_root
        self.ctx = ctx
        self.task_dir = task_dir
        # L-class (codex round-1 P3): reject corrupt persistence input
        # before dict() coerces a str/list into something silently wrong.
        # None / empty-dict remain valid (doctor passes empty in v0.8.1
        # while orchestrator wire-up is deferred to v0.8.2).
        if baseline_snapshot is not None and not isinstance(baseline_snapshot, dict):
            raise TypeError(
                "baseline_snapshot must be dict or None; got "
                f"{type(baseline_snapshot).__name__}"
            )
        # Defensive copy — snapshot is read-only inside the checker.
        self.baseline_snapshot = dict(baseline_snapshot or {})

    # ------------------------------------------------------------------
    # Trigger 1 — base branch moved (cheapest; rev-parse one ref).
    # ------------------------------------------------------------------
    @staticmethod
    def check_base_branch_moved(
        *, repo_root: Path, ctx: Any,
    ) -> tuple[bool, dict]:
        """True iff `git rev-parse <integration_target>` ≠ `original_base_commit`.

        D2/D3 typed except: a failed rev-parse (rc != 0) is reported as
        "could not resolve" — NOT silently treated as "no change". The
        caller (doctor) surfaces this distinct branch to the user.
        """
        target = getattr(ctx, "integration_target", None)
        original = getattr(ctx, "original_base_commit", None)
        if not isinstance(target, str) or not target:
            return (False, {"reason": "ctx missing integration_target"})
        if not isinstance(original, str) or not original:
            return (False, {"reason": "ctx missing original_base_commit"})
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_root), "rev-parse", target],
                capture_output=True, text=True, check=False,
            )
        except (OSError, FileNotFoundError) as e:
            return (False, {
                "reason": "rev-parse spawn failed",
                "error": f"{type(e).__name__}: {e}",
            })
        if proc.returncode != 0:
            return (False, {
                "reason": "rev-parse non-zero returncode",
                "returncode": proc.returncode,
                "stderr_tail": (proc.stderr or "")[-200:],
            })
        current = (proc.stdout or "").strip()
        if not current:
            return (False, {"reason": "rev-parse empty stdout"})
        if current == original:
            return (False, {})
        return (True, {
            "from_commit": original,
            "to_commit": current,
            "integration_target": target,
        })

    # ------------------------------------------------------------------
    # Trigger 2 — lockfile content changed (sha256 vs snapshot).
    # ------------------------------------------------------------------
    @classmethod
    def snapshot_lockfiles(cls, repo_root: Path) -> dict[str, str]:
        """Capture {lockfile_name: sha256_hex} for present LOCKFILES.

        Future-facing helper (v0.8.2 will call it at task-start); v0.8.1
        only invokes it from tests + when the doctor wants a live diff.
        D5 typed except: missing files are simply skipped; permission
        errors / corrupt blocks propagate so the operator sees them.
        """
        out: dict[str, str] = {}
        for name in cls.LOCKFILES:
            p = repo_root / name
            if not p.is_file():
                continue
            try:
                blob = p.read_bytes()
            except (OSError, PermissionError):
                # Skip unreadable file — better to under-detect than to
                # crash the whole snapshot. Doctor will still flag it via
                # other paths if it's truly broken.
                continue
            out[name] = hashlib.sha256(blob).hexdigest()
        return out

    @classmethod
    def check_lockfile_changed(
        cls, repo_root: Path, snapshot: dict,
    ) -> tuple[bool, dict]:
        """Compare current lockfile hashes against `snapshot` dict.

        [Codex round-1 P2] If `snapshot` is empty / missing, we cannot
        distinguish "newly added" from "always existed" — every present
        lockfile would otherwise be reported as "added" and the v0.8.1
        doctor (which currently passes empty snapshots because task-start
        snapshot capture is v0.8.2 orchestrator scope) would false-
        positive on every active task. Skip with explicit detail rather
        than spam.

        L-class type-check: when a baseline IS present, snapshot value
        MUST be a str before we compare — a corrupt entry (e.g. None /
        int / list) does NOT silently equal a hex hash and trigger a
        false negative. Non-str entries are treated as "no recorded
        hash" → a present current file with no recorded hash counts as
        "added".
        """
        if not snapshot:
            return (False, {
                "skipped": (
                    "no task-start snapshot available "
                    "(v0.8.2 orchestrator wire-up deferred)"
                ),
            })
        current = cls.snapshot_lockfiles(repo_root)
        changed: list[str] = []
        for name, recorded in snapshot.items():
            if not isinstance(recorded, str) or not recorded:
                # No usable recorded hash — treat as missing baseline.
                if name in current:
                    changed.append(name)
                continue
            if current.get(name) != recorded:
                changed.append(name)
        added = [name for name in current if name not in snapshot]
        if not changed and not added:
            return (False, {})
        # De-dupe while preserving order: changed first, then truly new.
        seen = set()
        diff: list[str] = []
        for name in changed + added:
            if name not in seen:
                diff.append(name)
                seen.add(name)
        return (True, {"changed": diff})

    # ------------------------------------------------------------------
    # Trigger 3 — prd.md edited (mtime advance).
    # ------------------------------------------------------------------
    @staticmethod
    def check_prd_edited(
        *, prd_path: Path, snapshot_mtime: float,
    ) -> tuple[bool, dict]:
        """True iff prd.md mtime advanced past `snapshot_mtime`.

        [Codex round-1 P2] When `snapshot_mtime <= 0.0` we have no
        baseline (v0.8.1 doctor-only mode passes 0.0 because task-start
        snapshot capture is v0.8.2 orchestrator scope). Any existing
        prd.md would otherwise satisfy `current_mtime > 0.0` and
        false-positive every active task. Skip with explicit detail.

        D5 typed except: missing prd.md is reported with `reason`
        instead of trigger-firing — a deleted prd.md is a different
        anomaly the doctor surfaces separately (and `<= snapshot` would
        false-positive when stat() raises).
        """
        if not isinstance(prd_path, Path):
            return (False, {"reason": "prd_path not a Path"})
        # L-class: snapshot must be numeric — anything else means "no
        # recorded mtime", treat as zero (then skip below).
        if not isinstance(snapshot_mtime, (int, float)):
            snapshot_mtime = 0.0
        if snapshot_mtime <= 0.0:
            return (False, {
                "skipped": (
                    "no task-start snapshot available "
                    "(v0.8.2 orchestrator wire-up deferred)"
                ),
            })
        try:
            if not prd_path.is_file():
                return (False, {"reason": "prd.md missing"})
            current_mtime = prd_path.stat().st_mtime
        except (OSError, PermissionError) as e:
            return (False, {
                "reason": "prd stat failed",
                "error": f"{type(e).__name__}: {e}",
            })
        if current_mtime <= snapshot_mtime:
            return (False, {})
        return (True, {
            "snapshot_mtime": snapshot_mtime,
            "current_mtime": current_mtime,
        })

    # ------------------------------------------------------------------
    # Trigger 4 — dep-file content changed (byte-level hash; semver-aware
    # diffing deferred to v0.8.2 per plan row 7423).
    # ------------------------------------------------------------------
    @classmethod
    def snapshot_dep_versions(cls, repo_root: Path) -> dict[str, str]:
        """Capture {dep_file_name: sha256_hex} for present DEP_FILES.

        v0.8.1 hashes the whole file (any byte-level change counts).
        Future v0.8.2 may parse {package_name: version} per-file and
        diff individual entries — explicitly out of scope here.
        """
        out: dict[str, str] = {}
        for name in cls.DEP_FILES:
            p = repo_root / name
            if not p.is_file():
                continue
            try:
                blob = p.read_bytes()
            except (OSError, PermissionError):
                continue
            out[name] = hashlib.sha256(blob).hexdigest()
        return out

    @classmethod
    def check_dep_versions_changed(
        cls, repo_root: Path, snapshot: dict,
    ) -> tuple[bool, dict]:
        """Same shape as `check_lockfile_changed` but for DEP_FILES.

        [Codex round-1 P2] Same empty-snapshot skip semantics as
        `check_lockfile_changed`: in v0.8.1 doctor-only mode the
        snapshot is empty, so every present dep-file would otherwise
        report "added" and false-positive every active task.
        """
        if not snapshot:
            return (False, {
                "skipped": (
                    "no task-start snapshot available "
                    "(v0.8.2 orchestrator wire-up deferred)"
                ),
            })
        current = cls.snapshot_dep_versions(repo_root)
        changed: list[str] = []
        for name, recorded in snapshot.items():
            if not isinstance(recorded, str) or not recorded:
                if name in current:
                    changed.append(name)
                continue
            if current.get(name) != recorded:
                changed.append(name)
        added = [name for name in current if name not in snapshot]
        if not changed and not added:
            return (False, {})
        seen = set()
        diff: list[str] = []
        for name in changed + added:
            if name not in seen:
                diff.append(name)
                seen.add(name)
        return (True, {"changed": diff})

    # ------------------------------------------------------------------
    # Trigger 5 — baseline now fails (re-run baseline_command).
    # ------------------------------------------------------------------
    @staticmethod
    def check_baseline_now_fails(
        *,
        worktree_root: Path,
        baseline_command: str,
        baseline_was_passing: bool,
        timeout_sec: int = 600,
    ) -> tuple[bool, dict]:
        """Re-run `baseline_command` inside `worktree_root` and report
        non-zero exit as a fresh trigger ONLY when the baseline was
        passing at task start.

        Pitfall I + K + E: this method does NOT use `subprocess.run(
        shell=True, timeout=...)` — that pattern only kills the shell,
        not the test process tree, on timeout (T7/T12/T13 lesson). We
        import the project's `_run_shell_with_pgkill` helper (T12) which
        wraps `Popen(start_new_session=True)` + `os.killpg` for whole-
        group SIGKILL.

        D5 typed except: spawn failure / timeout route to NON-trigger
        with explicit reason (not trigger), because we cannot prove
        "baseline now fails" if we couldn't re-run it. The doctor surface
        prints the reason so the operator notices.
        """
        if not baseline_was_passing:
            return (False, {
                "reason": "baseline was failing at task start",
            })
        if not isinstance(baseline_command, str) or not baseline_command.strip():
            return (False, {"reason": "no baseline_command configured"})
        if not isinstance(worktree_root, Path) or not worktree_root.is_dir():
            return (False, {
                "reason": "worktree_root missing or not a directory",
            })
        # Late import [codex round-1 P3 doc-fix]: keeps the legacy
        # `flow staleness` CLI lightweight. The v0.7 memory-files
        # scanner doesn't need `flow_orchestrator`'s subprocess helpers
        # — only trigger 5 (baseline_now_fails) does, and that's a
        # doctor-only path. Hoisting would force every `flow staleness`
        # invocation to load the full orchestrator module + its
        # imports. (No circular dependency exists between
        # flow_orchestrator and flow_staleness — `flow_orchestrator`
        # does not import `flow_staleness`.)
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from flow_orchestrator import _run_shell_with_pgkill  # type: ignore
        except (ImportError, ModuleNotFoundError) as e:
            return (False, {
                "reason": "could not import _run_shell_with_pgkill helper",
                "error": f"{type(e).__name__}: {e}",
            })
        try:
            result = _run_shell_with_pgkill(
                baseline_command,
                cwd=worktree_root,
                timeout_sec=timeout_sec,
            )
        except (OSError, PermissionError) as e:
            return (False, {
                "reason": "baseline subprocess raised",
                "error": f"{type(e).__name__}: {e}",
            })
        if result.spawn_error is not None:
            return (False, {
                "reason": "baseline spawn failed",
                "error": result.spawn_error,
            })
        if result.timed_out:
            # Not a clean trigger — the operator can't tell whether this
            # is a real regression or a hang. Surface the timeout
            # reason; do not lie that the baseline "now fails".
            return (False, {
                "reason": "baseline timed out (inconclusive)",
                "timeout_sec": timeout_sec,
            })
        rc = result.returncode
        if rc == 0:
            return (False, {})
        # Real fail. Capture stderr tail (last 500 chars) for forensics.
        # J-class: include stderr only when non-zero — avoid "always
        # attach empty stderr" noise that hides the real signal.
        details: dict = {"exit_code": rc}
        if result.stderr:
            details["stderr_tail"] = result.stderr[-500:]
        return (True, details)

    # ------------------------------------------------------------------
    # Aggregator: run triggers 1-4 (cheap) + optionally 5 (expensive).
    # ------------------------------------------------------------------
    def check_all(
        self,
        *,
        include_baseline: bool = False,
        baseline_command: str = "",
        baseline_was_passing: bool = True,
        baseline_timeout_sec: int = 600,
    ) -> StalenessVerdict:
        """v0.8.1 sole production entry point — `flow doctor` calls this
        with `include_baseline=True`. The orchestrator-side caller is
        DEFERRED to v0.8.2 (plan rows 7220 / 7232 / 7520-7524).

        Each trigger is independent — a single fire flips `stale=True`
        but later triggers still run so the verdict reports ALL active
        triggers (operator wants the full picture, not "first wins").
        """
        verdict = StalenessVerdict(stale=False)

        # Trigger 1 — base branch moved.
        r1, d1 = self.check_base_branch_moved(
            repo_root=self.repo_root, ctx=self.ctx,
        )
        if r1:
            verdict.stale = True
            verdict.triggered.append("base_branch")
            verdict.details["base_branch"] = d1

        # Trigger 2 — lockfile changed.
        snap_lock = self.baseline_snapshot.get("lockfiles", {}) or {}
        if not isinstance(snap_lock, dict):
            snap_lock = {}
        r2, d2 = self.check_lockfile_changed(self.repo_root, snap_lock)
        if r2:
            verdict.stale = True
            verdict.triggered.append("lockfile")
            verdict.details["lockfile"] = d2

        # Trigger 3 — prd.md edited.
        prd = self.task_dir / "prd.md"
        snap_mtime = self.baseline_snapshot.get("prd_mtime", 0.0)
        if not isinstance(snap_mtime, (int, float)):
            snap_mtime = 0.0
        r3, d3 = self.check_prd_edited(
            prd_path=prd, snapshot_mtime=float(snap_mtime),
        )
        if r3:
            verdict.stale = True
            verdict.triggered.append("prd_mtime")
            verdict.details["prd_mtime"] = d3

        # Trigger 4 — dep-file version changed.
        snap_dep = self.baseline_snapshot.get("dep_versions", {}) or {}
        if not isinstance(snap_dep, dict):
            snap_dep = {}
        r4, d4 = self.check_dep_versions_changed(self.repo_root, snap_dep)
        if r4:
            verdict.stale = True
            verdict.triggered.append("dep_version")
            verdict.details["dep_version"] = d4

        # Trigger 5 — baseline now fails (opt-in only).
        if include_baseline:
            worktree_root = getattr(self.ctx, "worktree_path", None)
            if not isinstance(worktree_root, Path):
                # Cannot run baseline without a worktree path — record
                # the gap so the doctor surfaces it.
                verdict.details.setdefault(
                    "baseline_fail_skipped",
                    {"reason": "ctx missing worktree_path"},
                )
            else:
                r5, d5 = self.check_baseline_now_fails(
                    worktree_root=worktree_root,
                    baseline_command=baseline_command,
                    baseline_was_passing=baseline_was_passing,
                    timeout_sec=baseline_timeout_sec,
                )
                if r5:
                    verdict.stale = True
                    verdict.triggered.append("baseline_fail")
                    verdict.details["baseline_fail"] = d5

        return verdict


def main():
    parser = argparse.ArgumentParser(description="Stale-memory check")
    parser.add_argument("--scope", choices=["project", "vault", "all"], default="project")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--recent", type=int, default=5, help="Flag if path modified in last N commits")
    args = parser.parse_args()

    flow = get_flow_dir()
    project_root = flow.parent if flow.is_dir() else Path.cwd()

    targets = collect_targets(args.scope, flow)

    if not targets:
        if args.json:
            print(json.dumps({"findings": [], "checked_files": 0}))
        else:
            print("(no memory files to check)")
        return

    all_findings: list[StaleFinding] = []
    for memory in targets:
        all_findings.extend(scan_memory_file(memory, project_root, args.recent))

    if args.json:
        print(json.dumps({
            "findings": [asdict(f) for f in all_findings],
            "checked_files": len(targets),
        }, ensure_ascii=False))
        return

    # Human output
    print(f"Checked {len(targets)} memory file(s) in scope: {args.scope}")
    if not all_findings:
        print("All references resolved. No stale memory.")
        return

    by_reason: dict[str, list[StaleFinding]] = {}
    for f in all_findings:
        by_reason.setdefault(f.reason, []).append(f)

    print(f"\n{len(all_findings)} stale finding(s):")
    for reason, items in by_reason.items():
        print(f"\n[{reason}]")
        for f in items:
            print(f"  {f.memory_file}")
            print(f"    cites: {f.cited_path}")
            if f.detail:
                print(f"    detail: {f.detail}")

    print("\nNext: review and update memory files, or mark obsolete with `status: obsolete` frontmatter.")
    sys.exit(1 if all_findings else 0)


if __name__ == "__main__":
    main()
