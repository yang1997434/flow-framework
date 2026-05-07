"""flow_orchestrator — v0.8.0 dry-run orchestrator + v0.8.1 auto-dispatch shell.

Reads contract.json + progress.md `### Tasks` block. Builds per-task file
ownership manifest (allowed_writes = scope.allowed ∩ task.writes; flags
files outside scope as out_of_scope). Prints plan in human-readable form.

v0.8.0 explicitly REFUSES `--auto-execute`. v0.8.1 (T10) lands the
worktree-per-task creation primitive + orchestrator-derives-facts
infrastructure: `WorktreeContext`, `create_task_worktree`, `TaskFacts`,
`derive_task_facts`, `auto_dispatch_task`. T11–T15 wire the
manifest/gates/merge layers on top. v0.8.0 `_cmd_dry_run` is untouched.
"""
from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Reuse the wave-planner's parser since the `### Tasks` block format is shared.
# parse_plan_tasks takes raw markdown text (NOT a path) and returns list[Task].
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "common"))
from flow_wave_planner import parse_plan_tasks  # type: ignore
from flow_contract import (  # type: ignore
    parse_contract, ContractError, Contract, CONTRACT_SCHEMA_VERSION,
)
from progress_meta import read_progress_meta, ProgressMeta  # type: ignore
from flow_state_writer import (  # type: ignore
    EVENT_AUTO_ENGAGED, append_autonomy_event, _new_event_id,
)


# Files implicitly shared across tasks — copied from flow_wave_planner; v0.8.0
# uses the same denylist for manifest building.
SHARED_ARTIFACTS = {
    "VERSION", "CHANGELOG.md", "README.md", "package.json", "package-lock.json",
    "requirements.txt", "pyproject.toml", "Cargo.toml", "Cargo.lock",
}


@dataclass
class TaskManifest:
    id: str
    writes_declared: list[str]
    allowed_writes: list[str]
    out_of_scope: list[str]
    forbidden_hits: list[str]
    shared_hits: list[str]
    risk_tier: str = "med"


@dataclass
class OrchestratorPlan:
    slug: str
    autonomy_mode: str
    contract: Optional[Contract]
    manifests: list[TaskManifest] = field(default_factory=list)
    fallback_reason: Optional[str] = None


def _resolve_slug_dir(slug: str) -> Path:
    here = Path.cwd().resolve()
    for parent in [here, *here.parents]:
        cand = parent / ".flow" / "tasks" / slug
        if cand.is_dir():
            return cand
    raise SystemExit(f"ERROR: .flow/tasks/{slug}/ not found from {here}")


def _glob_match(globs: list[str], path: str) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs)


def build_plan(slug: str) -> OrchestratorPlan:
    slug_dir = _resolve_slug_dir(slug)
    contract_path = slug_dir / "contract.json"
    progress_path = slug_dir / "progress.md"

    meta = read_progress_meta(progress_path)
    plan = OrchestratorPlan(slug=slug, autonomy_mode=meta.autonomy_mode,
                            contract=None)

    if not contract_path.is_file():
        plan.autonomy_mode = "interactive"
        plan.fallback_reason = "contract.json not found — falling back to interactive"
        return plan

    # T2 R11 (codex P1 fix): PRE-PARSE raw-JSON ceiling check.
    # The post-parse_contract ceiling check below is bypassed when a v=999
    # contract ALSO uses future-incompatible field values (e.g. a new
    # `autonomy_mode` value, a new criterion `method`, a new `type`) that
    # this v0.8.1 parser doesn't know — parse_contract raises ContractError,
    # the except branch sets fallback_reason, and we silently degrade to
    # interactive. R11 mandates fail-closed even when the contract is
    # otherwise unparseable: a too-new schema means the runtime CANNOT know
    # the field semantics, so degrading is unsafe (we'd be guessing).
    #
    # Rule: only ceiling-check if the version is a clean int > known max.
    # Every other shape error (missing/wrong-type/negative/bool subclass/
    # malformed JSON) is deferred to parse_contract's existing rich error
    # paths so error messages stay consistent with the documented schema.
    try:
        raw = json.loads(contract_path.read_text())
    except (OSError, json.JSONDecodeError):
        raw = None  # parse_contract will surface the proper error
    if isinstance(raw, dict):
        raw_v = raw.get("contract_schema_version")
        if (
            isinstance(raw_v, int)
            and not isinstance(raw_v, bool)
            and raw_v > CONTRACT_SCHEMA_VERSION
        ):
            raise SystemExit(
                f"ERROR: contract.json declares contract_schema_version="
                f"{raw_v} but this flow runtime knows up to "
                f"{CONTRACT_SCHEMA_VERSION}. Upgrade flow OR downgrade "
                f"the contract."
            )

    try:
        contract = parse_contract(contract_path)
    except ContractError as e:
        plan.autonomy_mode = "interactive"
        plan.fallback_reason = f"contract parse failed ({e}) — falling back to interactive"
        return plan

    # T2 R11: post-parse defense-in-depth ceiling check. Kept (not removed)
    # so that any future code path that calls parse_contract directly and
    # then trusts the result still catches a too-new schema if (somehow)
    # the pre-parse check above is bypassed. Redundant on the happy path
    # but cheap and self-documenting. If parse_contract starts being more
    # permissive about version typing, this guard stays in force.
    if contract.contract_schema_version > CONTRACT_SCHEMA_VERSION:
        raise SystemExit(
            f"ERROR: contract.json declares contract_schema_version="
            f"{contract.contract_schema_version} but this flow runtime "
            f"knows up to {CONTRACT_SCHEMA_VERSION}. Upgrade flow OR "
            f"downgrade the contract."
        )

    plan.contract = contract
    plan.autonomy_mode = contract.autonomy_mode

    # Parse tasks block from progress.md (reuse wave-planner parser).
    # parse_plan_tasks takes the raw markdown text, not a path.
    try:
        tasks = parse_plan_tasks(progress_path.read_text())
    except Exception:
        tasks = []

    for t in tasks:
        writes = list(t.writes or [])
        out_of_scope: list[str] = []
        allowed: list[str] = []
        forbidden_hits: list[str] = []
        shared_hits: list[str] = []
        for w in writes:
            if _glob_match(contract.scope_forbidden, w):
                forbidden_hits.append(w)
                continue
            if w in SHARED_ARTIFACTS:
                shared_hits.append(w)
                continue
            if contract.scope_allowed and not _glob_match(contract.scope_allowed, w):
                out_of_scope.append(w)
                continue
            allowed.append(w)
        plan.manifests.append(TaskManifest(
            id=t.id,
            writes_declared=writes,
            allowed_writes=allowed,
            out_of_scope=out_of_scope,
            forbidden_hits=forbidden_hits,
            shared_hits=shared_hits,
        ))

    return plan


def print_plan(plan: OrchestratorPlan) -> None:
    print(f"flow orchestrator (dry-run) — slug: {plan.slug}")
    print(f"  autonomy_mode: {plan.autonomy_mode}")
    if plan.fallback_reason:
        print(f"  fallback: {plan.fallback_reason}")
    if plan.contract:
        c = plan.contract
        print(f"  contract: schema_version={c.contract_schema_version} "
              f"created_at={c.created_at}")
        if c.unknown_fields:
            print(f"  contract.unknown_fields: {c.unknown_fields}")
        if c.acceptance_criteria:
            print(f"  acceptance_criteria: {len(c.acceptance_criteria)}")
        else:
            print("  acceptance_criteria: <none> "
                  "(Phase 3 will fall back to legacy gate)")
    print()
    if plan.manifests:
        print("  Task manifests:")
        for m in plan.manifests:
            print(f"  - {m.id} (risk={m.risk_tier})")
            print(f"      allowed_writes: {m.allowed_writes}")
            if m.out_of_scope:
                print(f"      out_of_scope: {m.out_of_scope}  (would escalate)")
            if m.forbidden_hits:
                print(f"      forbidden_hits: {m.forbidden_hits}  (would escalate)")
            if m.shared_hits:
                print(f"      shared_hits: {m.shared_hits}  (would serialize)")
    else:
        print("  Task manifests: <no `### Tasks` block in progress.md>")


def _cmd_dry_run(slug: str) -> int:
    plan = build_plan(slug)
    print_plan(plan)
    return 0


def _cmd_auto_execute(slug: str) -> int:
    # T2 R11: parse + ceiling-check the contract FIRST so a too-new schema
    # fails closed with a precise version-mismatch message rather than the
    # generic "v0.8.0 disabled" stub. build_plan() does the parse + ceiling
    # enforcement and raises SystemExit on a too-new schema; if it returns
    # we know the contract is parseable and ≤ runtime version, then we
    # still refuse v0.8.0 dispatch with the long-standing not-yet-implemented msg.
    # Parse + R11 ceiling check; raises SystemExit on too-new schema before we hit the v0.8.0 stub.
    build_plan(slug)
    print(
        "ERROR: v0.8.0 does not support autonomous dispatch. "
        "The schema and dry-run preview are stable; the safety stack "
        "(worktree-per-task isolation, acceptance gate, notification, "
        "budget enforcement) ships in v0.8.1.\n"
        "Use `--dry-run` to preview the plan, then upgrade to v0.8.1+ "
        "before enabling auto execution.",
        file=sys.stderr,
    )
    return 2


# ----------------------------------------------------------------------
# T10 — per-task worktree + dual-base + orchestrator-derives-facts.
#
# Per design §4 Q4.1: worktree id = `<slug>+t<n>+<shortsha>`. The shortsha
# is derived from the integration_target HEAD at create-time and is STABLE
# across retries even if a later rebase moves `current_base_commit` — the
# branch name keeps reflecting the original creation point.
#
# Per design §4 S6: each worktree records BOTH `original_base_commit` (set
# once at creation) AND `current_base_commit` (rewritten on rebase). At
# creation the two are equal; they only diverge after a rebase. T10 only
# wires the recording side; rebase-driven mutation lives in later tasks.
#
# Per PRD §1.2: the orchestrator NEVER trusts a subagent's structured
# self-report. The subagent returns ONLY a free-form narrative. Facts come
# from disk: `git diff <current_base_commit>..HEAD`, `git rev-parse HEAD`,
# acceptance log paths. `derive_task_facts` is the read primitive.
#
# Per design §7 Q7.2 + §8.4 row `auto_engaged` (R6+Y3, 14 fields): the
# `auto_engaged` event is emitted BEFORE first subagent dispatch — even if
# dispatch crashes immediately, the boundary marker remains on disk so §6
# R10 lock-state recovery can distinguish "never started" from "started
# and crashed". `auto_dispatch_task` is the orchestration shell.
# ----------------------------------------------------------------------


WORKTREE_ROOT = Path(".claude/worktrees")  # relative to repo root

# Slug must be filesystem-safe and stable across rerun/archival. We
# intentionally allowlist a narrow charset: lowercase + digits + `_` + `-`.
# `+` is reserved as the worktree-id separator. Path traversal segments
# (`..`, leading `/`) cannot pass this regex. Same regex is reused for
# `integration_target` to prevent shell-metachar smuggling into a
# branch-name argv slot (E-class blindspot: even with list-form argv, a
# refname like `master;rm -rf /` would be REJECTED by git, but we'd rather
# fail-loud at the orchestrator boundary with a clear error than rely on
# git's own validation surfacing through CalledProcessError).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# Branch / refname allowlist (looser than slug — must allow `/` for
# `release/v0.8.1` and the like). Disallows shell metachars + `..` (git
# refname rule) + leading `-` (would be parsed as a flag).
_REF_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_./-]*$")


@dataclass
class WorktreeContext:
    """11-field worktree context per plan §10.1 schema. Mutating
    `current_base_commit` is the only post-creation mutation envisaged
    (S6 dual-base) — every other field is set once at create-time.
    """
    slug: str
    task_idx: int                      # zero-based — the t<n> in branch name
    worktree_id: str                   # = `<slug>+t<n>+<shortsha>`
    worktree_path: Path
    branch: str                        # same as worktree_id
    integration_target: str            # parent branch (e.g., master)
    original_base_commit: str          # full sha at creation (immutable)
    current_base_commit: str           # full sha after any rebase (S6)
    base_shortsha: str                 # 7-char short of original
    lifecycle_state: str               # active|merging|merged|aborted|blocked
    created_at: str                    # ISO 8601


@dataclass
class TaskFacts:
    """Authoritative per-task facts derived FROM DISK (PRD §1.2). Subagent
    narrative is advisory only and never populates these fields. Each
    field has a single, distinct disk source — see `derive_task_facts`.
    """
    changed_files: list[str]
    diff_hash: str                      # sha256 hex over the unified diff
    target_commit_pre_merge: str        # worktree HEAD after subagent
    newly_added_files: list[str]


def _git(
    repo_root: Path, *args: str, check: bool = True,
) -> subprocess.CompletedProcess:
    """Subprocess wrapper for `git -C <repo_root> ...`.

    LIST-FORM argv (NOT shell=True). E-class pitfall (shell=True + string
    match = bypass) does not apply because we never assemble a command
    string — every argument is its own argv element. Even if a caller
    smuggles shell metachars into a slug, git itself receives them as a
    single argv slot and either rejects them (refname rules) or treats
    them literally (path argument). The `_SLUG_RE` / `_REF_RE` allowlists
    above provide an additional fail-loud layer at the orchestrator
    boundary so we surface user-facing errors with clear messages
    instead of relying on git's own argv validation.
    """
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=check, capture_output=True, text=True,
    )


def create_task_worktree(
    *, repo_root: Path, slug: str, task_idx: int, integration_target: str,
) -> WorktreeContext:
    """Create a worktree at `.claude/worktrees/<slug>+t<n>+<shortsha>/`.

    §4 Q4.1: name uses shortsha from `integration_target` HEAD at
    create-time — stable for the worktree lifetime; retries reuse the
    same name (collision-free across rerun + archival).
    §4 S6: at creation `original_base_commit == current_base_commit`;
    they only diverge after a later rebase.

    Validation:
      - `slug` must match `_SLUG_RE` (lowercase alphanumeric + `_-`).
      - `integration_target` must match `_REF_RE` (git refname-shaped).
      - `task_idx` must be a non-negative int.
    Validation runs BEFORE any disk side effect — invalid input raises
    ValueError without creating directories or invoking git.

    Errors:
      - subprocess.CalledProcessError: `git worktree add` failed (path
        already exists, branch exists, repo locked, etc.). Caller decides
        recovery; we do NOT swallow.
      - OSError: filesystem error creating the parent directory. Same.
    """
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ValueError(
            f"slug must match {_SLUG_RE.pattern!r}; got {slug!r}"
        )
    if not isinstance(integration_target, str) or not _REF_RE.match(integration_target):
        raise ValueError(
            f"integration_target must match {_REF_RE.pattern!r}; "
            f"got {integration_target!r}"
        )
    if not isinstance(task_idx, int) or isinstance(task_idx, bool) or task_idx < 0:
        # `bool` is a subclass of int — exclude it explicitly so
        # `task_idx=True` doesn't smuggle `1` past validation.
        raise ValueError(
            f"task_idx must be a non-negative int; got {task_idx!r}"
        )

    head = _git(repo_root, "rev-parse", integration_target).stdout.strip()
    if not head or len(head) < 7:
        # F-class fail-closed: if rev-parse returns an empty / truncated
        # sha (very rare — would mean git produced output we can't trust),
        # do NOT silently fall through to a default. Raise so the caller
        # surfaces the unexpected git state.
        raise ValueError(
            f"git rev-parse {integration_target} returned unusable "
            f"output: {head!r}"
        )
    shortsha = head[:7]
    worktree_id = f"{slug}+t{task_idx}+{shortsha}"
    worktree_path = repo_root / WORKTREE_ROOT / worktree_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    # `git worktree add -b <branch> <path> <start-point>` — list-form argv.
    _git(
        repo_root, "worktree", "add", "-b", worktree_id,
        str(worktree_path), integration_target,
    )
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return WorktreeContext(
        slug=slug,
        task_idx=task_idx,
        worktree_id=worktree_id,
        worktree_path=worktree_path,
        branch=worktree_id,
        integration_target=integration_target,
        original_base_commit=head,
        current_base_commit=head,
        base_shortsha=shortsha,
        lifecycle_state="active",
        created_at=now,
    )


def derive_task_facts(ctx: WorktreeContext) -> TaskFacts:
    """Read authoritative facts from disk (PRD §1.2 — subagent narrative
    is NOT trusted; every field comes from git).

    Diff is computed against `current_base_commit` rather than
    `original_base_commit`: the S6 dual-base distinction matters here —
    the branch name shortsha is pinned to ORIGINAL (so retries hit the
    same on-disk path) but the comparison must reflect the actual
    starting point of THIS attempt (which may have been rebased).

    All git invocations run with `check=True` (via `_git`); a
    `subprocess.CalledProcessError` propagates to the caller. Per
    F-class fail-closed: empty diff stdout is a meaningful "no changes"
    answer — represented by empty lists / hash-of-empty-string — and is
    NOT conflated with "git command failed" (which raises).
    """
    base = ctx.current_base_commit
    diff = _git(
        ctx.worktree_path, "diff", "--unified=0", f"{base}..HEAD",
    ).stdout
    diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    name_only = _git(
        ctx.worktree_path, "diff", "--name-only", f"{base}..HEAD",
    ).stdout
    # `splitlines` on empty string returns []; on `"a\nb\n"` returns
    # `["a", "b"]`. NOT using `.strip().splitlines()` after
    # `.split("\n")` — the strip() above existing implementation in plan
    # would map `"a\n\nb"` to `"a\n\nb".splitlines() == ["a","","b"]`
    # which still leaks an empty entry. Filter explicitly.
    changed_files = [ln for ln in name_only.splitlines() if ln]
    added_only = _git(
        ctx.worktree_path, "diff", "--name-only", "--diff-filter=A",
        f"{base}..HEAD",
    ).stdout
    newly = [ln for ln in added_only.splitlines() if ln]
    head = _git(ctx.worktree_path, "rev-parse", "HEAD").stdout.strip()
    if not head or len(head) < 7:
        raise ValueError(
            f"git rev-parse HEAD in {ctx.worktree_path} returned "
            f"unusable output: {head!r}"
        )
    return TaskFacts(
        changed_files=changed_files,
        diff_hash=diff_hash,
        target_commit_pre_merge=head,
        newly_added_files=newly,
    )


def auto_dispatch_task(
    *, slug: str, task_idx: int, repo_root: Path,
    dispatch_fn,
    contract: Optional[Contract] = None,
    run_id: str = "",
    contract_path: Optional[Path] = None,
    contract_hash: str = "",
    integration_target: str = "master",
) -> TaskFacts:
    """T10 orchestration shell — minimal scaffolding for one auto task.

    1. Create worktree (S6 dual-base: original == current at creation).
    2. Emit `auto_engaged` event into `decisions.jsonl` BEFORE invoking
       any subagent (§7 Q7.2 + §8.4 row `auto_engaged` 14-field schema).
    3. Invoke `dispatch_fn(ctx)` — opaque subagent boundary; the returned
       value is IGNORED per PRD §1.2 (subagent narrative is advisory).
    4. Read authoritative facts from disk via `derive_task_facts`.

    Steps 5+ (manifest verify / acceptance gate / merge / cleanup) are
    T11–T15 territory. T10 deliberately keeps this shell minimal so the
    boundary contract (event-before-dispatch, facts-from-disk) is testable
    without dragging in the full pipeline.

    Required parameters for the `auto_engaged` event payload:
      - `contract`: parsed Contract — used for `contract_schema_version`.
      - `run_id`: caller-supplied run identifier.
      - `contract_path`: pathlib path to the contract file.
      - `contract_hash`: caller-supplied hash (sha256 hex of contract bytes).

    These are passed through verbatim to the event payload. Validation of
    the 14-field schema is delegated to `append_autonomy_event` (which
    fails closed via `EVENT_REQUIRED_FIELDS`). If any required field is
    missing here, the event-write step raises before dispatch — by
    design (Q7.2 demands a real boundary marker, not a partial one).

    `task_dir` is `<repo_root>/.flow/tasks/<slug>` and must exist; we do
    NOT create it here because by the time auto_dispatch_task runs the
    flow harness has already provisioned the slug directory (Phase 1/2
    ordering).
    """
    task_dir = repo_root / ".flow" / "tasks" / slug
    ctx = create_task_worktree(
        repo_root=repo_root,
        slug=slug,
        task_idx=task_idx,
        integration_target=integration_target,
    )
    # §8.4 row `auto_engaged` requires 14 fields. Any missing key here
    # causes `append_autonomy_event` to raise with the missing-field list
    # — fail-loud BEFORE dispatch, which is the Q7.2 contract.
    append_autonomy_event(
        task_dir,
        EVENT_AUTO_ENGAGED,
        {
            "event_id": _new_event_id(),
            "ts": ctx.created_at,
            "slug": slug,
            "run_id": run_id,
            "task_id": f"T{task_idx}",
            "worktree_id": ctx.worktree_id,
            "worktree_path": str(ctx.worktree_path),
            "original_base_commit": ctx.original_base_commit,
            "current_base_commit": ctx.current_base_commit,
            "lifecycle_state": ctx.lifecycle_state,
            "checkpoint_id": None,
            "contract_path": str(contract_path) if contract_path else "",
            "contract_hash": contract_hash,
            "contract_schema_version": (
                contract.contract_schema_version if contract else CONTRACT_SCHEMA_VERSION
            ),
        },
    )
    # Subagent boundary — opaque. Return value INTENTIONALLY discarded
    # (PRD §1.2: subagent narrative is advisory, never structured data).
    dispatch_fn(ctx)
    # Authoritative facts come from disk, not the dispatch return value.
    return derive_task_facts(ctx)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="flow orchestrator")
    parser.add_argument("--dry-run", metavar="SLUG", help="Print plan + manifest")
    parser.add_argument("--auto-execute", metavar="SLUG",
                        help="(disabled in v0.8.0) attempt autonomous run")
    args = parser.parse_args(argv)

    if args.dry_run:
        return _cmd_dry_run(args.dry_run)
    if args.auto_execute:
        return _cmd_auto_execute(args.auto_execute)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
