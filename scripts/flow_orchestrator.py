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
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

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
    EVENT_AUTO_ENGAGED,
    EVENT_TASK_READY_TO_MERGE,
    EVENT_MERGE_STARTED,
    EVENT_MERGE_APPLIED,
    EVENT_TASK_COMPLETED,
    EVENT_POST_MERGE_VERIFICATION_STARTED,
    EVENT_POST_MERGE_VERIFICATION_COMPLETED,
    EVENT_POST_MERGE_VERIFY_FAILED,
    EVENT_AUTO_PREPARE_INTERRUPTED,
    AUTO_PREPARE_LOCK_FILENAME,
    AutoPrepareLock,
    detect_auto_prepare_state,
    write_auto_prepare_lock,
    consume_auto_prepare_lock,
    append_autonomy_event, _new_event_id,
    write_blocked, write_checkpoint,
    append_review_issue, ReviewIssueRecord,
    append_decision, DecisionRecord,
)
from flow_notification import Notifier  # type: ignore


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


# T19 Step 19.11: replaced v0.8.0 exit-2 stub with the end-to-end
# dispatch loop defined further down (after CrashRecoveryDispatcher
# and the helper functions it depends on). The stub historically
# returned exit 2; the new loop returns exit 0 (success / interactive
# fallback) or exit 3 (any block). T2 R11 ceiling check is preserved
# inside `build_plan()` which the new loop calls first.


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
# Slug allowlist: lowercase alphanumeric + `_-.` (the dot is needed for
# real project slugs like ``05-05-autonomous-mode-v0.8`` — without it the
# allowlist would reject this very project's own slug). The ``..`` denylist
# is enforced separately to block path-traversal even when individual
# characters are allowed.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_SLUG_DENYLIST = ("..",)
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
    # Path-traversal denylist (defense-in-depth — the regex already blocks
    # `/`, but double-dot path segments need an explicit guard since `.` is
    # in the allowed charset).
    for forbidden in _SLUG_DENYLIST:
        if forbidden in slug:
            raise ValueError(
                f"slug contains forbidden sequence {forbidden!r}; got {slug!r}"
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
    committed_changed = [ln for ln in name_only.splitlines() if ln]
    added_only = _git(
        ctx.worktree_path, "diff", "--name-only", "--diff-filter=A",
        f"{base}..HEAD",
    ).stdout
    committed_added = [ln for ln in added_only.splitlines() if ln]

    # Codex T11 round-1 [P1]: a subagent could leave forbidden /
    # out-of-scope files in the WORKING TREE (uncommitted staged,
    # uncommitted unstaged, or untracked) and bypass the manifest check
    # entirely — `base..HEAD` only sees committed changes. §1 row 4
    # ("untracked file added outside scope") is specifically about this
    # scenario. We merge `git status --porcelain` results so manifest
    # verification covers the full attack surface.
    #
    # `core.quotePath=false` keeps non-ASCII filenames unquoted so the
    # post-status text is literal repo-relative paths (no shell-escape
    # processing needed in the parser).
    # ``--untracked-files=all`` recursively lists every untracked FILE
    # rather than collapsing newly-created directories into a single
    # ``?? secrets/`` line. Row 4 detection ("untracked file outside
    # scope") needs file-level granularity — without this flag a rogue
    # ``secrets/key.pem`` would appear as the bare directory path,
    # leaking the actual filename out of `violations` and frustrating
    # forensics.
    #
    # Codex T11 round-2 [P1]: ``-z`` (NUL-terminated) is mandatory, NOT
    # cosmetic. Without it, porcelain v1 represents renames as ``R  new
    # -> old`` on a single line — which means a string-based ` -> `
    # split is the only way to recover paths. But that string match is
    # status-blind: a malicious subagent could create an UNTRACKED file
    # whose own filename contains ` -> ` (e.g.
    # ``secrets/key.pem -> src/foo.py``); the parser would record only
    # the half after the arrow, hiding the real forbidden path. ``-z``
    # disambiguates: renames produce TWO NUL-separated records (status+
    # new-path, then bare old-path). Only the explicit ``R``/``C``
    # status indicates rename — every other record's path is the entire
    # post-status text, ` -> ` or no ` -> `.
    porcelain = _git(
        ctx.worktree_path, "-c", "core.quotePath=false",
        "status", "--porcelain", "-z", "--untracked-files=all",
    ).stdout
    working_changed: set[str] = set()
    working_added: set[str] = set()
    records = porcelain.split("\x00")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if not rec or len(rec) < 4:
            # Trailing empty string after the final NUL is expected; skip.
            # Records shorter than ``XY space ...`` are malformed; skip.
            continue
        x = rec[0]
        # Porcelain v1 -z: 'XY' + space + path  (no ` -> ` separators
        # in-line; renames put oldpath in the NEXT record).
        path = rec[3:]
        if not path:
            continue
        working_changed.add(path)
        if x == "R" or x == "C":
            # Rename / copy: the path here is NEW; the next record holds
            # the OLD path. We treat the NEW path as a fresh write
            # (effectively row-4 territory) and consume the old-path
            # record so we don't mis-classify it as its own change.
            working_added.add(path)
            if i < len(records):
                i += 1
        elif x == "A" or rec[:2] == "??":
            working_added.add(path)

    # Merge committed + working-tree views; deduplicate while preserving
    # a stable sort for downstream determinism.
    changed_files = sorted(set(committed_changed) | working_changed)
    newly = sorted(set(committed_added) | working_added)

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


# ----------------------------------------------------------------------
# T11 — manifest violation enforcement (replaces v0.8.0 advisory dry-run).
#
# v0.8.0 `build_plan()` already computes `forbidden_hits / shared_hits /
# out_of_scope` for the **declared** writes block in `progress.md`. T11
# extends this from "advisory print at plan-time" to "enforce at runtime
# against ACTUAL changed files derived from git".
#
# Per design §1 row 3 (file outside scope.allowed → block) + row 4
# (untracked file outside scope → block) + the design-table separately
# from row 14 (`shared` artifacts serialize via wave queue, NOT block —
# T15 S1 territory; T11 surfaces them as advisory `shared_artifacts_touched`).
#
# Verifier identity: same primitives `_glob_match` + `SHARED_ARTIFACTS`
# already used by `build_plan()` — so the enforcement view and the
# advisory view share the same allowlist semantics. C2 frozenset-truth
# audit: every `SHARED_ARTIFACTS` entry must be reachable. Our matcher
# treats the path as a literal string equal-test (`path in
# SHARED_ARTIFACTS`); paths from `git diff --name-only` are repo-relative
# without `./` prefix, so `VERSION` matches `VERSION`. No normalization
# is needed (and would silently break case-sensitive lookups on Linux).
# ----------------------------------------------------------------------


@dataclass
class ManifestVerdict:
    """T11 verdict from `verify_manifest_against_facts`. Caller branches
    on `decision`; `block_row` (3 or 4) routes the blocked.md frontmatter.
    `shared_artifacts_touched` is advisory (NOT a block per §1 row 14
    semantics — wave-serialize logic lives in T15 S1).
    """
    decision: str                       # "pass" | "block"
    block_row: Optional[int] = None     # §1 row 3 or 4
    violations: list[str] = field(default_factory=list)
    shared_artifacts_touched: list[str] = field(default_factory=list)


def verify_manifest_against_facts(
    contract: Contract,
    manifest: TaskManifest,
    facts: TaskFacts,
) -> ManifestVerdict:
    """Verify the on-disk diff (`facts`) against the contract scope.

    Order of precedence (C-blindspot — forbidden wins over allowed even
    when both glob lists match the same path):
      1. ``contract.scope_forbidden`` glob hit → block row 3.
      2. ``path in SHARED_ARTIFACTS`` → advisory, skip allowed-check.
      3. ``contract.scope_allowed`` non-empty AND path does NOT match →
         block row 4 if path was newly added (untracked-style, §1 row 4),
         else row 3 (existing-file modification outside scope, §1 row 3).

    Empty-list semantics (F-blindspot fail-open closure):
      - ``contract.scope_allowed == []`` is treated as "no allowlist
        configured" → no row-3/row-4 block fires from out-of-scope (the
        forbidden + shared steps still run). This matches v0.8.0
        ``build_plan()`` semantics (line 172: ``if contract.scope_allowed
        and ...``) so the advisory and enforcement views agree. Real
        contracts always populate the list; an empty list is a contract
        author's explicit "scope unknown — allow anywhere" signal.
      - ``contract.scope_forbidden == []`` means "nothing forbidden"
        (vacuously, no glob can match). This is also v0.8.0 parity.

    `manifest` is accepted for symmetry with §11.4's signature contract
    (T15 will likely consume it; T11 only reads `contract` + `facts`).
    """
    verdict = ManifestVerdict(decision="pass")
    newly_set = set(facts.newly_added_files)

    for path in facts.changed_files:
        # Step 1 — forbidden globs always win (C-blindspot precedence).
        if _glob_match(contract.scope_forbidden, path):
            verdict.violations.append(path)
            verdict.decision = "block"
            verdict.block_row = 3
            continue
        # Step 2 — shared artifacts: advisory only, NOT a block.
        if path in SHARED_ARTIFACTS:
            verdict.shared_artifacts_touched.append(path)
            continue
        # Step 3 — out-of-scope (only when an allowlist is configured).
        if (
            contract.scope_allowed
            and not _glob_match(contract.scope_allowed, path)
        ):
            verdict.violations.append(path)
            verdict.decision = "block"
            row = 4 if path in newly_set else 3
            # Keep the lowest (= most severe) row already set. row 3 is
            # the catch-all "file outside scope"; row 4 is the narrower
            # "untracked added outside scope". A prior row 3 from a
            # forbidden hit must not be downgraded to row 4.
            if verdict.block_row is None or row < verdict.block_row:
                verdict.block_row = row
    return verdict


@dataclass
class DispatchOutcome:
    """T11 return type for `auto_dispatch_task`. Lets the orchestrator
    dispatch loop branch on block vs. ok without re-reading
    ``blocked.md`` from disk. ``ctx`` is always populated (worktree
    created); ``facts`` is also always populated post-T10 (dispatch
    return-path always reads the diff). ``blocked_md_path`` is set ONLY
    when ``status == "blocked"``.
    """
    status: str                        # "ok" | "blocked"
    ctx: WorktreeContext
    facts: TaskFacts
    block_type: Optional[str] = None
    block_row: Optional[int] = None
    blocked_md_path: Optional[Path] = None


def auto_dispatch_task(
    *, slug: str, task_idx: int, repo_root: Path,
    dispatch_fn,
    contract: Contract,
    manifest: TaskManifest,
    run_id: str,
    contract_path: Path,
    contract_hash: str,
    integration_target: str = "master",
    notifier: Optional["Notifier"] = None,  # type: ignore[name-defined]
) -> "DispatchOutcome":
    """T10 orchestration shell + T11 manifest enforcement.

    1. Create worktree (S6 dual-base: original == current at creation).
    2. Emit `auto_engaged` event into `decisions.jsonl` BEFORE invoking
       any subagent (§7 Q7.2 + §8.4 row `auto_engaged` 14-field schema).
    3. Invoke `dispatch_fn(ctx)` — opaque subagent boundary; the returned
       value is IGNORED per PRD §1.2 (subagent narrative is advisory).
    4. Read authoritative facts from disk via `derive_task_facts`.
    5. (T11) Verify `facts` against `manifest`/`contract` scope. Forbidden
       hit OR out-of-scope hit → write `blocked.md` (block_type =
       ``manifest_violation``, row 3 or 4) and return a blocked outcome.
       SHARED_ARTIFACTS hits are advisory (not a block; T15 S1 wave-
       serializes cross-task contention).

    Steps 6+ (acceptance gate / merge / cleanup) are T12–T15 territory.

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

    Non-empty validation (F-class fail-closed): ``run_id`` and
    ``contract_hash`` are security-critical fields journaled into
    ``decisions.jsonl`` for forensic recovery. Empty strings would pass
    the writer's key-presence check (which validates schema shape, not
    content) and produce a corrupt audit record. We reject them here so
    the orchestrator boundary fails loud.
    """
    # Codex round-1 [P2]: validate `contract` BEFORE create_task_worktree.
    # The type hint isn't runtime-enforced, so without this guard a None
    # or wrong-type `contract` would create the worktree on disk first,
    # then crash at `contract.contract_schema_version` below — leaving an
    # orphaned worktree and skipping the auto_engaged boundary marker
    # (which is the very Q7.2 invariant T10 promises).
    if not isinstance(contract, Contract):
        raise ValueError(
            f"auto_dispatch_task: contract must be a Contract instance; "
            f"got {type(contract).__name__}"
        )
    for field_name, field_value in (
        ("run_id", run_id),
        ("contract_hash", contract_hash),
    ):
        if not isinstance(field_value, str) or not field_value.strip():
            raise ValueError(
                f"auto_dispatch_task: {field_name} must be a non-empty "
                f"string; got {field_value!r}"
            )
    if not isinstance(contract_path, Path) or not str(contract_path).strip():
        raise ValueError(
            f"auto_dispatch_task: contract_path must be a non-empty "
            f"Path; got {contract_path!r}"
        )
    # T11: `manifest` is required for the post-dispatch verifier. It
    # supplies `manifest.id` for the auto_engaged event's `task_id`
    # field (§8.4 14-field schema) AND is reserved for T15 wave logic
    # (e.g., shared-artifact serialization keys off `manifest.id`).
    if not isinstance(manifest, TaskManifest):
        raise ValueError(
            f"auto_dispatch_task: manifest must be a TaskManifest; "
            f"got {type(manifest).__name__}"
        )
    task_dir = repo_root / ".flow" / "tasks" / slug
    # ── [P2 codex round-1] T5 §8.1 lock wire-up ───────────────────────
    # Production path was missing this; state-2 (R10) recovery was
    # therefore unreachable in real runs (only fixture tests could
    # synthesize the lock). Contract per T5 design §8.1:
    #
    #   1. write_auto_prepare_lock BEFORE worktree creation, so a crash
    #      between here and the auto_engaged event is recoverable via
    #      detect_auto_prepare_state → interrupted_dead_pid.
    #   2. consume_auto_prepare_lock AFTER auto_engaged is durably
    #      appended to decisions.jsonl — past this point state-3
    #      (auto_engaged_crashed) is the operative recovery class.
    #
    # K-class (no try/except swallow): a lock-write failure must
    # propagate. If the disk is wedged enough that we can't write the
    # lock, we MUST NOT proceed to create a worktree — that would leave
    # an orphan worktree with no recovery boundary marker on disk. The
    # whole point of T5 §8.1 is the lock is the durable boundary.
    #
    # P-class scope: the lock dataclass intentionally pins (run_id,
    # task_id, contract_hash) so the next run's detect_auto_prepare_state
    # can compare and either consume (matched) or block (mismatched).
    now_ts = _now_iso()
    lock = AutoPrepareLock(
        lock_version=1,
        slug=slug,
        run_id=run_id,
        task_id=manifest.id,
        contract_path=str(contract_path),
        contract_hash=contract_hash,
        contract_schema_version=contract.contract_schema_version,
        created_at=now_ts,
        pid=os.getpid(),
        host=socket.gethostname(),
        cwd=str(task_dir),
        target_branch=integration_target,
        intended_first_task_dispatch_at=now_ts,
    )
    write_auto_prepare_lock(task_dir, lock)
    # NOTE: any exception between here and consume_auto_prepare_lock
    # below MUST propagate without unlinking the lock. The next run's
    # CrashRecoveryDispatcher will see the orphaned lock + dead pid and
    # route to state 2 (auto_prepare_interrupted). DO NOT add a
    # try/except that calls consume_auto_prepare_lock on failure.

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
            "task_id": manifest.id,
            "worktree_id": ctx.worktree_id,
            "worktree_path": str(ctx.worktree_path),
            "original_base_commit": ctx.original_base_commit,
            "current_base_commit": ctx.current_base_commit,
            "lifecycle_state": ctx.lifecycle_state,
            "checkpoint_id": None,
            "contract_path": str(contract_path),
            "contract_hash": contract_hash,
            "contract_schema_version": contract.contract_schema_version,
        },
    )
    # [P2] Boundary marker durable on disk; consume the lock so state-3
    # (post-auto_engaged crash) is the operative recovery path beyond
    # this point. consume_auto_prepare_lock is os.replace + Y8 event;
    # K-class: errors propagate so we never leave an inconsistent
    # boundary (would manifest as orphan_lock_post_engaged on next run,
    # which is recoverable but warrants a loud failure here).
    consume_auto_prepare_lock(
        task_dir, slug=slug, run_id=run_id, task_id=manifest.id,
    )
    # Subagent boundary — opaque. Return value INTENTIONALLY discarded
    # (PRD §1.2: subagent narrative is advisory, never structured data).
    #
    # T21 / S5: propagate FLOW_AUTONOMY_PARENT_PID=<own pid> so that any
    # `flow orchestrator --auto-execute` invocation the subagent attempts
    # mechanically aborts via `_guard_against_nested_autonomy`. We pass a
    # fresh copy of the environment (not a reference to os.environ) so
    # downstream mutations cannot reach back into the parent process.
    subagent_env = os.environ.copy()
    subagent_env[AUTONOMY_PARENT_PID_ENV] = str(os.getpid())
    # F1 wire-up (codex round-1): WorktreeContext intentionally has NO
    # ``task_id`` field (the dataclass only carries worktree-level state;
    # task identity lives on the per-iteration manifest). The dispatch
    # shim's template needs ``{task_id}`` to invoke the subagent against
    # the right task — pass it explicitly as a kwarg so the shim doesn't
    # silently fall through ``getattr(ctx, "task_id", "")`` to "" and
    # interpolate ``--task `` (empty arg). Test fixtures that set
    # ``ctx.task_id`` directly still work (kwarg None → getattr fallback)
    # but production now goes through the canonical path.
    dispatch_fn(ctx, subagent_env=subagent_env, task_id=manifest.id)
    # Authoritative facts come from disk, not the dispatch return value.
    facts = derive_task_facts(ctx)

    # T11 — manifest verification on actual diff (§1 row 3 / row 4).
    verdict = verify_manifest_against_facts(contract, manifest, facts)
    if verdict.decision == "block":
        why = (
            f"manifest_violation (row {verdict.block_row}): "
            f"{verdict.violations}"
        )
        choices = [
            "abort_task",
            "switch_to_interactive",
            "extend_scope_with_rationale",
        ]
        resume = f"flow orchestrator --resume {slug}"
        if notifier is not None:
            # T16 path — Notifier handles Tier 1 (blocked.md) + Tier 2
            # (OSC 9 + BEL). Duck-typed: any object exposing
            # `fire_block(...)` returning the blocked.md path works.
            blocked_md_path = notifier.fire_block(
                block_type="manifest_violation",
                phase=2,
                task_id=manifest.id,
                issue_id="manifest_violation",
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
                frontmatter_extra={"block_row": verdict.block_row},
            )
        else:
            # Back-compat path for unit tests + callers that don't yet
            # construct a Notifier (Step 19.11 always passes one in
            # production). Tier 2 is skipped — only blocked.md lands.
            blocked_md_path = write_blocked(
                task_dir,
                phase=2,
                task=manifest.id,
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
            )
        return DispatchOutcome(
            status="blocked",
            ctx=ctx,
            facts=facts,
            block_type="manifest_violation",
            block_row=verdict.block_row,
            blocked_md_path=blocked_md_path,
        )
    return DispatchOutcome(status="ok", ctx=ctx, facts=facts)


# ----------------------------------------------------------------------
# T12 — gate harness: gates 1, 3, 5, 6 (baseline / manifest / acceptance /
# regression). Gates 2, 4, 7, 8 land in T13/T14/T15 and slot into
# `run_phase2` between the gates T12 owns.
#
# Design refs:
#   §3 gate 1 (baseline tests inside worktree per Q3.1)
#   §3 gate 3 (manifest verify — wires T11)
#   §3 gate 5 (acceptance criteria — wires T7+T8)
#   §3 gate 6 (final regression smoke)
#   §1 row 7 (baseline newly fail → block)
#   §1 row 8 (post-regression smoke fail — plan §12.8 maps to row 5 for
#     orchestrator routing parity with gate 5 BLOCK_ROW5; design alignment
#     is a T13 follow-up)
#
# Q3.1: baseline runs INSIDE the worktree (not the main checkout) so it
# catches state drift the worktree itself induced (lockfile diverged,
# submodule init missing). v0.8.1 cost: extra baseline run per worktree.
#
# Pitfall coverage:
#   D5 catch-all: every subprocess call has typed except handling for
#     `subprocess.TimeoutExpired` + `OSError` (spawn failures). Other
#     exceptions propagate to the caller — the harness is run inside the
#     orchestrator where unexpected exceptions are surfaced (no silent
#     swallow into "pass").
#   D6 status guard: gate5 maps EVERY EvalDecision branch explicitly so a
#     new variant added to the enum surfaces as an unknown-decision
#     `inconclusive` rather than silently routing to a default.
#   E shell=True: `gate1_baseline` and `gate6_regression` execute strings
#     supplied by the caller (which originate from contract.json — author-
#     trusted by definition). The trust boundary is the contract author;
#     untrusted user input MUST NOT reach these methods without contract-
#     level validation. Documented at the call sites.
#   F fail-closed: "no prior baseline" treats any current failure as
#     "newly broken" (block row 7), not silently passing.
#   G facts-from-disk: gate3 delegates to T11's verifier, which already
#     covers all 4 git disk layers (HEAD diff / staged / unstaged /
#     untracked). T12 does not short-circuit by reading only some.
#   subprocess timeout: `baseline_timeout_sec` / `regression_timeout_sec`
#     default to 600s (10 min). A hung subagent baseline must NOT hang
#     the harness forever; on timeout we return `inconclusive` so the
#     orchestrator can route to operator review rather than silently
#     pass or fail.
# ----------------------------------------------------------------------


# Default gate-1 / gate-6 subprocess timeout. Long enough for a typical
# project test suite, short enough that a runaway harness still hits the
# operator-review path in under 15 minutes. Override per call when a
# contract author knows their suite is slower.
_DEFAULT_GATE_TIMEOUT_SEC = 600

# Gate 4 codex CLI timeout. A hung codex CLI must NOT hang Phase 2 — the
# fix-pass for T13 added an explicit subprocess timeout; on expiry the
# gate routes to ``inconclusive`` (operator review) like every other
# subprocess D5-class catch-all in this module. Pinned at 600s to match
# `_DEFAULT_GATE_TIMEOUT_SEC`'s "long-but-bounded" budget for
# subprocess-driven gates.
_GATE4_CODEX_TIMEOUT_SEC = 600

# Codex verdict explicit allow-list. Anything else (missing field,
# typo, unknown future verdict) routes to ``inconclusive`` so the chain
# fails closed instead of fail-open silent pass on, e.g.,
# ``"INCONCLUSIVE"`` or ``"BLUE"``. Defined at module level so callers
# / reviewers can grep it without diving into the GateRunner class body.
ALLOWED_VERDICTS = ("GREEN", "YELLOW", "RED")


# ----------------------------------------------------------------------
# T13 — canonical issue id (S7) for cross-round churn detection.
#
# Design §6 S7: same issue across codex rounds must collide on a stable
# id so we can count appearances and halt on the CHURN_THRESHOLD-th hit
# (§3 line 141: do NOT consume more retry budget on churn — escalate).
#
# Whitespace-insensitive on the message so a re-worded but semantically
# identical complaint still collides. file/line_range/class are tightly
# scoped — different file or line range is a different issue.
# ----------------------------------------------------------------------


def canonical_issue_id(
    file_path: str, line_range: str,
    issue_class: str, issue_message: str,
) -> str:
    """Return a 12-char hex id derived from sha256 of pipe-joined fields
    with the message normalized (lowercased, whitespace-collapsed). Same
    issue across codex rounds yields the same id; different file / class
    / line range yields a different id.
    """
    norm_msg = " ".join(issue_message.lower().split())
    raw = f"{file_path}|{line_range}|{issue_class}|{norm_msg}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ----------------------------------------------------------------------
# T13 — semantic-diff retry-whitelist violation detection.
#
# Design §3 line 141: after a Phase 2 retry, compare BEFORE-retry diff
# vs AFTER-retry diff. If the retry suppressed verification (deleted
# tests, added skip decorators, narrowed fixtures, suppressed CI flags),
# escalate without consuming retry budget — the retry was an evasion,
# not a real fix.
#
# Helper is pure: no I/O. Caller (gate 4 / retry orchestrator) supplies
# already-collected file lists + diff strings. G-class watch: when the
# caller sources these from disk, it MUST pull from a single coherent
# snapshot (e.g. one ``git status --porcelain -z`` invocation per
# before/after side, not stitched from multiple layers).
# H-class watch: caller is responsible for splitting porcelain output
# correctly (rename arrows, NUL boundaries) BEFORE handing the lists
# to this helper. The helper trusts its inputs as already-cleaned.
# ----------------------------------------------------------------------


# CI / Makefile / package.json flag patterns that suppress test-failure
# signal. Matched as plain substrings against the diff text. Case-
# sensitive — these flags are literal CLI strings.
_SUPPRESS_FLAG_PATTERNS = (
    "--no-fail-fast", "--ignore", "--skip", "-k 'not ", "skipif",
)

# Decorator patterns used to disable tests in pytest / unittest.
_SKIP_DECORATOR_PATTERNS = ("@unittest.skip", "@pytest.mark.skip", "@skip")


@dataclass
class SemanticViolations:
    """Result of `detect_semantic_violations`.

    `escalate` is True iff at least one violation pattern fired; the
    `violations` list names which patterns hit (for blocked.md /
    decisions.jsonl forensics).
    """
    escalate: bool
    violations: list[str]


def detect_semantic_violations(
    *,
    before_files: list[str], after_files: list[str],
    before_diff: str, after_diff: str,
) -> SemanticViolations:
    """Detect 4 retry-whitelist violation patterns (§3 line 141).

    Patterns:
      1. ``test_file_deleted`` — a path under ``tests/`` ending in
         ``.py`` was present BEFORE and absent AFTER.
      2. ``test_skipped`` — a skip decorator (`@unittest.skip`,
         `@pytest.mark.skip`, `@skip`) appears in the AFTER diff but
         NOT in the BEFORE diff.
      3. ``flag_suppression`` — a verification-suppressing CLI flag
         (`--no-fail-fast`, `--ignore`, `--skip`, `-k 'not `, `skipif`)
         appears AFTER but not BEFORE.
      4. ``fixture_narrowing`` — a path containing "fixture" or "data"
         present in both lists shrank by more than 2× AND the post-
         diff is small (< 1024 bytes), indicating substantive content
         removal rather than incidental edits.

    Returns ``escalate=True`` iff at least one pattern fired, with the
    detected pattern names in ``violations``.
    """
    violations: list[str] = []

    # 1. Test files deleted.
    before_tests = {
        f for f in before_files
        if f.startswith("tests/") and f.endswith(".py")
    }
    after_tests = {
        f for f in after_files
        if f.startswith("tests/") and f.endswith(".py")
    }
    if before_tests - after_tests:
        violations.append("test_file_deleted")

    # 2. Skip decorator newly introduced. We loop and break to record
    # the violation only once even if multiple decorator forms appear.
    for decorator in _SKIP_DECORATOR_PATTERNS:
        if decorator not in before_diff and decorator in after_diff:
            violations.append("test_skipped")
            break

    # 3. Verification-suppressing CLI flag newly introduced.
    for flag in _SUPPRESS_FLAG_PATTERNS:
        if flag not in before_diff and flag in after_diff:
            violations.append("flag_suppression")
            break

    # 4. Fixture narrowing. Heuristic: a path with "fixture" or "data"
    # in its name present in both before/after lists, where the diff
    # bytes shrank more than 2× and the post-diff is < 1024 bytes
    # (cap prevents false positives on large refactors that legitimately
    # modify large fixtures).
    common_fixtures = [
        f for f in (set(before_files) & set(after_files))
        if "fixture" in f or "data" in f
    ]
    if common_fixtures:
        if len(before_diff) > 2 * len(after_diff) and len(after_diff) < 1024:
            violations.append("fixture_narrowing")

    return SemanticViolations(
        escalate=bool(violations),
        violations=violations,
    )

# Codex T12 round-1 [P2]: bare ``subprocess.run(..., shell=True,
# timeout=...)`` only kills the SHELL on timeout — child processes (test
# servers, file watchers, ``&``-backgrounded jobs spawned by the test
# command) keep running, leaking resources and continuing to mutate the
# worktree after the gate has returned ``inconclusive``. T7's
# ``_run_cmd`` already solved this with ``Popen(start_new_session=True)``
# + ``os.killpg`` on the entire process group; we duplicate the pattern
# here. SIGTERM grace window matches T7's constant.
_PROCESS_GROUP_KILL_GRACE_SEC = 2.0


@dataclass
class _ShellRunResult:
    """Compact result type for `_run_shell_with_pgkill` — mirrors the
    subset of `subprocess.CompletedProcess` we use, plus an explicit
    timeout flag so callers don't have to interpret exception state.
    """
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False
    spawn_error: Optional[str] = None


def _run_shell_with_pgkill(
    command: str,
    *,
    cwd: Path,
    timeout_sec: int,
) -> _ShellRunResult:
    """Run ``command`` via ``shell=True`` in its OWN process group,
    capturing stdout/stderr. On timeout, SIGTERM the whole group, drain
    briefly, then SIGKILL anything still alive — same pattern as T7's
    ``AcceptanceRunner._run_cmd`` to address codex T12 round-1 [P2].

    Returns a ``_ShellRunResult``; never raises ``TimeoutExpired`` (the
    flag is on the result instead). ``OSError`` during spawn surfaces in
    ``spawn_error`` with ``returncode=None``.

    Trust boundary (E-class): caller is responsible for ensuring
    ``command`` originates from a contract-author-trusted source.
    ``shell=True`` is intentional — gate test commands are author-
    composed shell strings (``pytest tests/ && bash extras.sh``-style).
    """
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # POSIX: own session ⇒ own process group. Lets us killpg the
            # entire descendant tree on timeout.
            start_new_session=True,
        )
    except OSError as e:
        return _ShellRunResult(
            returncode=None, stdout="", stderr="",
            spawn_error=f"{type(e).__name__}: {e}",
        )

    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
        return _ShellRunResult(
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
        )
    except subprocess.TimeoutExpired:
        # Two-stage kill of the WHOLE group, not just the shell.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # Brief grace for graceful shutdown.
        deadline = time.monotonic() + _PROCESS_GROUP_KILL_GRACE_SEC
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)  # probe — raises when group is gone
            except ProcessLookupError:
                break
            time.sleep(0.05)
        # Belt-and-suspenders SIGKILL regardless. The criterion already
        # failed; the tree must be dead before we return.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Reap the shell so we don't leave a zombie. Group is dead by now;
        # short bounded wait.
        try:
            stdout, stderr = proc.communicate(
                timeout=_PROCESS_GROUP_KILL_GRACE_SEC,
            )
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return _ShellRunResult(
            returncode=None,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=True,
        )


@dataclass
class GateResult:
    """Per-gate outcome consumed by `run_phase2`.

    `status`:
      - "pass": gate satisfied; chain continues.
      - "fail": gate failed; chain halts. `details["block_row"]` carries
        the §1 routing row (3, 4, 5, 6, 7) so the caller can write the
        appropriate blocked.md frontmatter.
      - "inconclusive": gate could not produce a verdict (subprocess
        timeout / spawn failure). Chain halts. Caller routes to operator
        review (T9 owns the resume path).

    `escalate`: Y1 — set True when the gate's failure must surface the
    `{abort, interactive, split}` menu (gate 5 e2e fails, etc.). Caller's
    blocked.md writer reads this flag.
    """
    status: str
    escalate: bool = False
    details: dict = field(default_factory=dict)


@dataclass
class BaselineRecord:
    """v0.8.1 naive baseline: a flat list of test ids known to fail at task
    start. Per-test diffing (compare current fails vs prior fails) is
    deferred — gate1 currently uses returncode equality alone. This means
    any non-zero baseline returncode blocks if no prior baseline OR if any
    prior baseline existed but suite-level pass status flipped from green
    to red. Acceptable v0.8.1 trade-off; T19+ may upgrade.
    """
    failing: list[str]


@dataclass
class Phase2Verdict:
    """Aggregate verdict from `GateRunner.run_phase2`.

    `status`: "pass" | "blocked".
    `halted_at_gate`: the method name of the gate that halted the chain
      (e.g. "gate3_manifest"); None on full pass.
    `gate_result`: the GateResult that halted; None on full pass.
    """
    status: str
    halted_at_gate: Optional[str] = None
    gate_result: Optional[GateResult] = None


class GateRunner:
    """Phase 2 in-worktree gate harness. T12 wires gates 1, 3, 5, 6;
    T13/T14/T15 wire 2, 4, 7, 8 by extending `run_phase2`.

    Each gate method is pure-by-side-effect-on-disk (subprocess + log
    files in `task_dir/logs/...`); none of them mutate `self`. The chain
    in `run_phase2` is sequential — first non-pass result halts.
    """

    def __init__(
        self,
        *,
        ctx: WorktreeContext,
        contract: Contract,
        task_dir: Path,
        run_id: str,
        task_id: str,
        prior_baseline: Optional[BaselineRecord] = None,
    ):
        self.ctx = ctx
        self.contract = contract
        self.task_dir = task_dir
        self.run_id = run_id
        self.task_id = task_id
        self.prior_baseline = prior_baseline

    # ------------------------------------------------------------------
    # Gate 1 — baseline tests inside the worktree (Q3.1).
    # ------------------------------------------------------------------

    def gate1_baseline(
        self, *, test_command: str,
        timeout_sec: int = _DEFAULT_GATE_TIMEOUT_SEC,
    ) -> GateResult:
        """Run `test_command` inside `ctx.worktree_path`. Exit code 0
        passes; non-zero blocks on §1 row 7 (newly broken baseline).

        E-class (shell=True trust boundary): `test_command` is passed to
        a shell. The caller is responsible for ensuring it originates
        from a trusted source (contract.json `baseline_command` field
        authored by the contract author). Untrusted user input MUST NOT
        reach this method.

        D5 catch-all: subprocess timeouts and spawn failures route to
        `inconclusive` (not silent pass). Operator review handles them.

        F fail-closed: when there's no prior baseline OR the prior was
        clean, any current failure blocks. Naive equality on returncode
        — full per-test diffing is deferred (see BaselineRecord).
        """
        result = _run_shell_with_pgkill(
            test_command,
            cwd=self.ctx.worktree_path,
            timeout_sec=timeout_sec,
        )
        if result.spawn_error is not None:
            return GateResult(
                status="inconclusive",
                details={
                    "gate": "gate1_baseline",
                    "reason": "spawn_failed",
                    "error": result.spawn_error,
                },
            )
        if result.timed_out:
            return GateResult(
                status="inconclusive",
                details={
                    "gate": "gate1_baseline",
                    "reason": "timeout",
                    "timeout_sec": timeout_sec,
                    "stderr_tail": result.stderr[-2000:],
                },
            )

        if result.returncode == 0:
            return GateResult(
                status="pass",
                details={"pre_existing_fails": []},
            )
        # Newly failing relative to integration target. v0.8.1 cost:
        # naive returncode equality (no per-test diffing yet).
        return GateResult(
            status="fail",
            details={
                "block_row": 7,
                "stderr_tail": result.stderr[-2000:],
                "returncode": result.returncode,
            },
        )

    # ------------------------------------------------------------------
    # Gate 3 — manifest verification (wires T11).
    # ------------------------------------------------------------------

    def gate3_manifest(
        self, *, manifest: TaskManifest, facts: TaskFacts,
    ) -> GateResult:
        """Delegate to T11's `verify_manifest_against_facts`. T11 already
        covers all 4 git disk layers (HEAD diff / staged / unstaged /
        untracked) via `derive_task_facts`; T12 must NOT short-circuit by
        reading a subset.

        Translates `ManifestVerdict` → `GateResult`:
          - decision=pass  → status=pass, details["shared"] populated
          - decision=block → status=fail, block_row + violations preserved
        """
        verdict = verify_manifest_against_facts(
            self.contract, manifest, facts,
        )
        if verdict.decision == "pass":
            return GateResult(
                status="pass",
                details={
                    "shared_artifacts_touched":
                        list(verdict.shared_artifacts_touched),
                },
            )
        return GateResult(
            status="fail",
            details={
                "block_row": verdict.block_row,
                "violations": list(verdict.violations),
            },
        )

    # ------------------------------------------------------------------
    # Gate 4 — per-task codex review (T13).
    #
    # Wraps the codex CLI; parses GREEN / YELLOW / RED verdict + issues.
    # On RED, persists each issue into review-issues.jsonl via the T6
    # `append_review_issue` helper using the S7 canonical issue id, then
    # checks churn: an issue id appearing CHURN_THRESHOLD+ times across
    # the task's history triggers `escalate=True` (per design §3 line
    # 141 — escalation does NOT consume more retry budget; the next-step
    # review-rejection rationale path owns operator override).
    #
    # Pitfall coverage:
    #   A get/in:    `output.get("verdict", "INCONCLUSIVE")` is safe
    #                because the entire output is treated as advisory
    #                metadata; falsy/missing → INCONCLUSIVE → fail-closed.
    #                Each issue field uses ``key in issue`` semantics
    #                (via the F fail-closed guard below) so absent vs
    #                explicit-empty are distinguished.
    #   D5 catch-all: codex CLI rc != 0 OR JSON parse failure → routes
    #                to ``inconclusive`` (operator review) rather than
    #                silent pass.
    #   E shell=True: ``codex_command`` is treated as TRUSTED INTERNAL.
    #                Tests inject ``echo '...'`` shell strings; production
    #                callers wire a fixed CLI invocation. Caller MUST NOT
    #                interpolate user-controlled data into this string.
    #   F fail-closed: missing issue fields (``file`` / ``line_range`` /
    #                ``class`` / ``message``) → return inconclusive,
    #                NOT silent substitution of empty strings (which
    #                would collide all malformed issues onto the same
    #                canonical id and short-circuit churn detection).
    # ------------------------------------------------------------------

    # Same id appearing CHURN_THRESHOLD+ times across a task's review
    # history → escalate without consuming retry budget. 3 matches the
    # design's "rounds 1-3 then escalate" cadence.
    CHURN_THRESHOLD = 3

    # Required keys on each codex-emitted issue. Missing any → inconclusive
    # (F fail-closed). We do NOT default to empty string because that
    # collides every malformed issue onto the same canonical id, which
    # would silently mask churn detection on real issues.
    _REQUIRED_ISSUE_KEYS = ("file", "line_range", "class", "message")

    def _count_issue_id_in_history(self, issue_id: str) -> int:
        """Count appearances of ``issue_id`` in this task's
        review-issues.jsonl. Missing file → 0; malformed lines skipped
        (T6 writer is the canonical producer; manual edits are best-effort).

        Codex round-1 [P2] fix-3 (G-class disk-state): the JSONL is
        scoped to the worktree task dir, but a worktree slug can host
        SEVERAL tasks (T11, T12, ...) that share the same dir layout.
        If a previous task logged the same canonical id, this task's
        count starts non-zero — could hit CHURN_THRESHOLD without 3
        actual codex rounds for THIS task. Filter on ``rec["task"] ==
        self.task_id`` so the count is per-task, matching the §3
        "after 3 rounds [for this task], escalate" semantics.
        """
        path = self.task_dir / "review-issues.jsonl"
        if not path.is_file():
            return 0
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                rec.get("id") == issue_id
                and rec.get("task") == self.task_id
            ):
                count += 1
        return count

    def gate4_codex_review(
        self, *,
        codex_command: str = "codex review --diff-only --json",
        codex_timeout_sec: Optional[int] = None,
    ) -> GateResult:
        """Run codex CLI, parse verdict, persist RED issues, detect churn.

        Returns a `GateResult`:
          - GREEN verdict        → status=pass.
          - YELLOW verdict       → status=pass (advisory issues persisted).
          - RED verdict          → status=fail; details include `block_row`
                                    is NOT set here (caller routes via
                                    `escalate` flag — churn → escalate).
          - codex CLI rc != 0    → status=inconclusive (D5).
          - codex CLI timeout    → status=inconclusive (D5; fix-pass P2-2).
          - non-JSON output      → status=inconclusive (F fail-closed).
          - missing/unknown verdict → status=inconclusive (F fail-closed;
                                    fix-pass P1-2 — explicit allow-list,
                                    no fail-open silent pass on, e.g.,
                                    ``"INCONCLUSIVE"`` or ``"BLUE"``).
          - issue missing fields → status=inconclusive (F fail-closed).

        Fix-pass invariant (P1-1): on ANY validation failure during issue
        parsing, NOTHING is appended to ``review-issues.jsonl``. We
        first validate the entire batch + collect canonical ids, then
        only on full-batch success do we append. A partial-write would
        poison churn counts on subsequent rounds.

        ``codex_timeout_sec`` defaults to ``_GATE4_CODEX_TIMEOUT_SEC``
        when None; tests inject smaller values to exercise the timeout
        path without sleeping for the full default.
        """
        # E-class trust boundary: codex_command is INTERNAL — never build
        # this string from external / codex-emitted / user-controlled data.
        #
        # Pitfall I (reuse prior helper): codex round-1 [P1] vindicated
        # the recurring T7/T12 pattern. Original T13 used
        # ``subprocess.run(shell=True, timeout=...)`` with the
        # justification that "codex CLI is a single binary, no expected
        # child-process tree". WRONG — with ``shell=True`` the SHELL is
        # the parent and Python's timeout-kill only signals the shell.
        # Children (codex's own subprocesses, the test stubs' ``sleep``,
        # any fork the codex CLI does) become orphans. Now routed
        # through the same ``_run_shell_with_pgkill`` helper that gate 1
        # / gate 6 use so the WHOLE process group dies on timeout. The
        # helper's ``_ShellRunResult`` is shaped to match this gate's
        # branching needs — see field handling below.
        if codex_timeout_sec is None:
            codex_timeout_sec = _GATE4_CODEX_TIMEOUT_SEC
        result = _run_shell_with_pgkill(
            codex_command,
            cwd=self.ctx.worktree_path,
            timeout_sec=codex_timeout_sec,
        )
        if result.spawn_error is not None:
            # Spawn-time OSError (e.g. fork failure) — fail-closed
            # inconclusive. Codex round-1 cited this as a recurring
            # I-class amnesia: gate 1 / gate 6 already do this.
            return GateResult(
                status="inconclusive",
                details={
                    "gate": "gate4_codex_review",
                    "reason": "spawn_failed",
                    "error": result.spawn_error,
                },
            )
        if result.timed_out:
            # D5 catch-all: a hung codex CLI must NOT hang Phase 2.
            # ``_run_shell_with_pgkill`` already SIGTERM/SIGKILLed the
            # entire process group; we just route the verdict.
            return GateResult(
                status="inconclusive",
                details={
                    "gate": "gate4_codex_review",
                    "reason": "codex_timeout",
                    "timeout_sec": codex_timeout_sec,
                    "stderr_tail": result.stderr[-1000:],
                },
            )

        if result.returncode != 0:
            return GateResult(
                status="inconclusive",
                details={
                    "gate": "gate4_codex_review",
                    "reason": "codex_cli_failed",
                    "error": "codex CLI failed",
                    "returncode": result.returncode,
                    "stderr_tail": result.stderr[-1000:],
                },
            )
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            return GateResult(
                status="inconclusive",
                details={
                    "gate": "gate4_codex_review",
                    "reason": "codex_output_not_json",
                    "error": "codex output not JSON",
                    "stdout_tail": result.stdout[-1000:],
                },
            )
        if not isinstance(output, dict):
            # F fail-closed: a list/string/null at the top level isn't
            # the documented shape — treat as inconclusive.
            return GateResult(
                status="inconclusive",
                details={
                    "gate": "gate4_codex_review",
                    "reason": "codex_output_not_object",
                    "stdout_tail": result.stdout[-1000:],
                },
            )

        # Fix-pass P1-2: explicit verdict allow-list. Missing field /
        # typo / unknown future value MUST route to inconclusive — not
        # fall-through to ``status="pass"`` (that's the exact fail-open
        # pattern T9/T10 had P1s for). The check happens BEFORE issue
        # parsing because an unknown verdict shouldn't even attempt to
        # walk issues.
        verdict = output.get("verdict")
        if verdict not in ALLOWED_VERDICTS:
            return GateResult(
                status="inconclusive",
                details={
                    "gate": "gate4_codex_review",
                    "reason": "unknown_verdict",
                    "verdict": verdict,
                    "stdout_tail": result.stdout[-1000:],
                },
            )

        if verdict == "GREEN":
            return GateResult(
                status="pass", details={"verdict": "GREEN"},
            )

        # YELLOW + RED both can carry issues; persist them all with S7
        # canonical ids so churn detection sees the full history.
        issues = output.get("issues", [])
        if not isinstance(issues, list):
            return GateResult(
                status="inconclusive",
                details={
                    "gate": "gate4_codex_review",
                    "reason": "issues_not_list",
                    "stdout_tail": result.stdout[-1000:],
                },
            )

        # Fix-pass P1-1: TWO-PASS validate-then-append. Validate the
        # ENTIRE batch first (collect canonical ids + persist payloads
        # in memory). Only on full-batch success do we append rows to
        # ``review-issues.jsonl``. A first-pass crash mid-batch would
        # leave issues 0..k-1 on disk → next round's churn count is
        # inflated by those orphans, producing false-positive escalates.
        #
        # Pass 1: validate every issue, collect (id, severity, message)
        # tuples in a staging list. Any malformed issue → return
        # inconclusive WITHOUT writing.
        issue_ids: list[str] = []
        staged: list[tuple[str, str, str]] = []  # (issue_id, severity, msg)
        for idx, issue in enumerate(issues):
            if not isinstance(issue, dict):
                return GateResult(
                    status="inconclusive",
                    details={
                        "gate": "gate4_codex_review",
                        "reason": "issue_not_object",
                        "stdout_tail": result.stdout[-1000:],
                    },
                )
            # F fail-closed: every required key MUST be present. We do
            # not silently substitute "" for missing fields — that would
            # collide every malformed issue onto the same canonical id
            # and mask real churn. Use ``key in issue`` (A-class).
            missing = [k for k in self._REQUIRED_ISSUE_KEYS if k not in issue]
            if missing:
                return GateResult(
                    status="inconclusive",
                    details={
                        "gate": "gate4_codex_review",
                        "reason": "issue_missing_required_field",
                        "missing": missing,
                        "stdout_tail": result.stdout[-1000:],
                    },
                )
            # Codex round-1 [P1] fix-2 (D5/F deeper): presence-check is
            # not enough. JSON ``null`` / int / list survives ``key in
            # dict`` but breaks the canonical id pipeline silently:
            #   - ``message=None`` → AttributeError on later
            #     ``.lower()`` calls in S7 normalization → uncaught
            #     exception (NOT fail-closed inconclusive).
            #   - ``file=42`` (or any non-string) → str()-ified into
            #     the SHA hash with garbage → wrong canonical id →
            #     churn detection silently unreliable.
            # Both modes route to inconclusive instead.
            for k in self._REQUIRED_ISSUE_KEYS:
                if not isinstance(issue[k], str):
                    return GateResult(
                        status="inconclusive",
                        details={
                            "gate": "gate4_codex_review",
                            "reason": "malformed_issue_non_string_field",
                            "field": k,
                            "type": type(issue[k]).__name__,
                            "idx": idx,
                            "stdout_tail": result.stdout[-1000:],
                        },
                    )
            issue_id = canonical_issue_id(
                issue["file"], issue["line_range"],
                issue["class"], issue["message"],
            )
            issue_ids.append(issue_id)
            # Severity is optional. When PRESENT but non-string (e.g.
            # ``"severity": 5``) → fail-closed inconclusive (codex
            # round-1 fix-2 — same D5/F class as the required-key
            # type check above; non-string severity must NOT silently
            # demote to "med" because that masks a malformed payload).
            # When PRESENT-and-string-but-not-in-enum → demote to
            # "med" (T6 enum default; preserves prior behavior for
            # forward-compat unknown-but-string severities).
            if "severity" in issue:
                if not isinstance(issue["severity"], str):
                    return GateResult(
                        status="inconclusive",
                        details={
                            "gate": "gate4_codex_review",
                            "reason": "malformed_issue_non_string_field",
                            "field": "severity",
                            "type": type(issue["severity"]).__name__,
                            "idx": idx,
                            "stdout_tail": result.stdout[-1000:],
                        },
                    )
                severity = issue["severity"]
                if severity not in ("critical", "high", "med", "low", "info"):
                    severity = "med"
            else:
                severity = "med"
            staged.append((issue_id, severity, issue["message"]))

        # Codex round-1 [P2] fix-4: per-round dedupe. If a single
        # codex response contains N entries that normalize to the
        # same canonical id (e.g. wording variants on the same line),
        # we MUST write only one row — otherwise round 1 alone
        # crosses CHURN_THRESHOLD=3 and triggers churn from a single
        # response. Cross-round churn is the documented behavior
        # (§3 line 141: "after 3 rounds, escalate"), per-round
        # duplication is not.
        seen_ids: set[str] = set()
        unique_staged: list[tuple[str, str, str]] = []
        unique_issue_ids: list[str] = []
        for tup in staged:
            iid = tup[0]
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            unique_staged.append(tup)
            unique_issue_ids.append(iid)

        # Pass 2: every issue validated → persist (deduped). We freeze
        # the timestamp once for the whole batch so all rows from a
        # single codex round share the same ts (audit-log clarity; no
        # microsecond drift between adjacent rows). Any per-row I/O
        # failure here legitimately leaves a partial write on disk —
        # that's a disk-state failure (G-class), not a parse-time
        # poisoning, and is surfaced through the normal exception path.
        ts = datetime.datetime.now(datetime.UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ",
        )
        for issue_id, severity, message in unique_staged:
            append_review_issue(self.task_dir, ReviewIssueRecord(
                id=issue_id,
                ts=ts,
                task=self.task_id,
                severity=severity,
                reviewer="codex",
                description=message,
                disposition="open",
            ))

        # Churn detection — issue ids that have appeared
        # CHURN_THRESHOLD+ times across this task's review history.
        # Counts include the rows we JUST appended (deduped to one
        # per round), so the threshold is the inclusive Nth round
        # (matches the §3 line 141 spec "after 3 rounds, escalate").
        # ``issue_ids`` carries every original (pre-dedupe) id so the
        # caller still sees the full count of issues codex reported,
        # but churn fires only on cross-round repetition.
        churn_ids = [
            iid for iid in unique_issue_ids
            if self._count_issue_id_in_history(iid) >= self.CHURN_THRESHOLD
        ]
        existing_details: dict = {
            "verdict": verdict,
            "issue_count": len(issues),
            "issue_ids": issue_ids,
        }
        if churn_ids:
            # Add `churn` key ONLY when non-empty so the GREEN /
            # no-churn paths' `assertNotIn("churn", ...)` checks keep
            # working (J-class watch — fixed-shape returns are easy to
            # break by adding fields unconditionally).
            existing_details["churn"] = churn_ids
        return GateResult(
            status="fail" if verdict == "RED" else "pass",
            escalate=bool(churn_ids),
            details=existing_details,
        )

    # ------------------------------------------------------------------
    # Gate 5 — acceptance criteria (wires T7 run_one + T8 evaluate).
    # ------------------------------------------------------------------

    def gate5_acceptance(
        self,
        *,
        criteria: list,  # list[AcceptanceCriterion]; runtime-typed
        attempt_id: str,
        retry_idx: int,
    ) -> GateResult:
        """Iterate `criteria` in declared order. For each:
          1. `AcceptanceRunner.run_one` executes the criterion.
          2. `evaluate_criterion(phase=2)` produces an `EvalDecision`.

        First non-PASS decision halts and returns a `GateResult`.

        D6 status guard — explicit branch for every EvalDecision value:
          PASS                    → continue to next criterion
          LOCAL_FIX_ALLOWED       → fail / escalate=False / block_row=5
                                    (T15 retry-loop reads decision tag)
          BLOCK_ROW5              → fail / escalate=False / block_row=5
          BLOCKED_ESCALATE_ROW6   → fail / escalate=True  / block_row=6
          INCONCLUSIVE            → inconclusive / escalate=False
                                    (T9 owns the resume path)

        A new EvalDecision variant added later without updating this
        mapping falls into the explicit ``else`` branch which routes to
        ``inconclusive`` so the regression is surfaced (not silently
        treated as ``pass``).
        """
        # Lazy import — flow_acceptance pulls in heavier modules and we
        # don't want to import-couple the orchestrator's dry-run path.
        from flow_acceptance import (  # type: ignore
            AcceptanceRunner, EvalDecision,
        )

        runner = AcceptanceRunner(
            worktree_root=self.ctx.worktree_path,
            log_dir=self.task_dir / "logs" / "acceptance",
            slug=self.ctx.slug,
            task_id=self.task_id,
            run_id=self.run_id,
            worktree_id=self.ctx.worktree_id,
        )

        for idx, crit in enumerate(criteria):
            result = runner.run_one(
                crit,
                criterion_idx=idx,
                attempt_id=attempt_id,
                retry_idx=retry_idx,
                task_dir=self.task_dir,
            )
            decision = runner.evaluate_criterion(
                crit, phase=2, runner_result=result,
            )

            if decision == EvalDecision.PASS:
                continue

            base_details = {
                "halted_at_idx": idx,
                "criterion_description": crit.description,
                "decision": decision.value,
                "run_status": result.status,
            }

            if decision == EvalDecision.LOCAL_FIX_ALLOWED:
                # T15 retry-loop reads the `decision` tag and may attempt
                # a local fix + retry. Block row 5 keeps the routing
                # parity with the operator-review fallback if the retry
                # budget is exhausted.
                return GateResult(
                    status="fail",
                    escalate=False,
                    details={**base_details, "block_row": 5},
                )
            if decision == EvalDecision.BLOCK_ROW5:
                return GateResult(
                    status="fail",
                    escalate=False,
                    details={**base_details, "block_row": 5},
                )
            if decision == EvalDecision.BLOCKED_ESCALATE_ROW6:
                return GateResult(
                    status="fail",
                    escalate=True,
                    details={**base_details, "block_row": 6},
                )
            if decision == EvalDecision.INCONCLUSIVE:
                return GateResult(
                    status="inconclusive",
                    escalate=False,
                    details=base_details,
                )
            # D6 defense-in-depth: an EvalDecision variant added later
            # without an explicit branch above lands here. Don't silently
            # pass; surface as inconclusive so operator review catches it.
            return GateResult(
                status="inconclusive",
                escalate=False,
                details={
                    **base_details,
                    "reason": "unknown_eval_decision",
                },
            )

        return GateResult(
            status="pass",
            details={"criteria_count": len(criteria)},
        )

    # ------------------------------------------------------------------
    # Gate 6 — final regression smoke.
    # ------------------------------------------------------------------

    def gate6_regression(
        self, *, smoke_command: str,
        timeout_sec: int = _DEFAULT_GATE_TIMEOUT_SEC,
    ) -> GateResult:
        """Run `smoke_command` inside `ctx.worktree_path`. Mirrors gate 1
        in shape but lands LATER in the chain (post-acceptance). Failure
        blocks on §1 row 5 (regular block, no escalate menu) per plan
        §12.8; design §1 row 8 alignment is a T13 follow-up.

        Same trust boundary + timeout semantics as gate 1.
        """
        result = _run_shell_with_pgkill(
            smoke_command,
            cwd=self.ctx.worktree_path,
            timeout_sec=timeout_sec,
        )
        if result.spawn_error is not None:
            return GateResult(
                status="inconclusive",
                details={
                    "gate": "gate6_regression",
                    "reason": "spawn_failed",
                    "error": result.spawn_error,
                },
            )
        if result.timed_out:
            return GateResult(
                status="inconclusive",
                details={
                    "gate": "gate6_regression",
                    "reason": "timeout",
                    "timeout_sec": timeout_sec,
                    "stderr_tail": result.stderr[-2000:],
                },
            )

        if result.returncode == 0:
            return GateResult(status="pass")
        return GateResult(
            status="fail",
            details={
                "block_row": 5,
                "stderr_tail": result.stderr[-2000:],
                "returncode": result.returncode,
            },
        )

    # ------------------------------------------------------------------
    # Phase 2 chain — gates 1 → 3 → 4 → 5 → 6 (T13 inserts gate 4
    # between 3 and 5; T15 inserts gate 2 before gate 1 + gates 7/8
    # after gate 6).
    # ------------------------------------------------------------------

    def run_phase2(
        self,
        *,
        manifest: TaskManifest,
        facts: TaskFacts,
        criteria: list,  # list[AcceptanceCriterion]
        attempt_id: str,
        retry_idx: int,
        baseline_command: str,
        smoke_command: str,
        codex_command: str = "codex review --diff-only --json",
        baseline_timeout_sec: int = _DEFAULT_GATE_TIMEOUT_SEC,
        smoke_timeout_sec: int = _DEFAULT_GATE_TIMEOUT_SEC,
    ) -> Phase2Verdict:
        """Chain the five Phase 2 gates in declared order:
        ``1 → 3 → 4 → 5 → 6``.

        Halt condition: first non-pass result OR any ``escalate=True``
        from a passing gate. Returns a `Phase2Verdict` with
        `halted_at_gate` naming the gate. Caller branches on
        `gate_result.escalate` for blocked.md routing.

        ``inconclusive`` from any gate also halts (it's "could not
        produce a verdict" — operator review owns the resolution).

        Fix-pass P2-1: ``escalate`` is honored even when ``status ==
        "pass"``. Gate 4 may return ``status=pass + escalate=True`` on
        YELLOW + churn (design §3 line 141: "churn → escalate
        regardless of verdict; do NOT consume more retry budget"). The
        escalate signal is generic — applied uniformly to all gates so
        a future gate that reuses the pattern doesn't need a custom
        halt branch.

        Codex round-1 [P2]: re-derive facts AFTER gate 1 baseline. The
        baseline command runs INSIDE the worktree (Q3.1) and may write
        files (cache, build artifacts, accidental source mutation). The
        ``facts`` argument was captured BEFORE baseline ran, so passing
        it directly to gate 3 would let any baseline-introduced manifest
        violation slip through. Refresh the snapshot now that gate 1 has
        confirmed the suite is green.

        T13: ``codex_command`` is forwarded to gate 4 (per-task codex
        review). It defaults to the production CLI invocation; tests
        inject deterministic ``echo '...'`` shell strings. Trust
        boundary same as ``baseline_command`` — caller-supplied,
        contract-author-trusted.
        """
        r = self.gate1_baseline(
            test_command=baseline_command,
            timeout_sec=baseline_timeout_sec,
        )
        if r.status != "pass" or r.escalate:
            return Phase2Verdict(
                status="blocked",
                halted_at_gate="gate1_baseline",
                gate_result=r,
            )

        # Re-derive facts so gate 3 sees any disk state the baseline
        # command produced. T11's `derive_task_facts` already covers
        # committed + staged + unstaged + untracked layers (G-class).
        #
        # Codex round-2 [P2] D5 catch-all: a baseline command CAN
        # corrupt the worktree (delete ``.git``, cause submodule
        # divergence, exhaust inodes mid-write). Each git invocation
        # inside ``derive_task_facts`` runs with ``check=True``, so any
        # corruption surfaces as ``CalledProcessError`` (or ``OSError``
        # on FS-level failures, ``ValueError`` on unparseable output).
        # We catch the family and route to ``inconclusive`` so the
        # orchestrator surfaces operator review instead of crashing
        # — exactly the role the round-1 refresh promised.
        try:
            post_baseline_facts = derive_task_facts(self.ctx)
        except (
            subprocess.CalledProcessError,
            OSError,
            ValueError,
        ) as e:
            # Codex round-3 [P2]: this halt is BEFORE gate 3 runs, not
            # AT it. Label the verdict with a dedicated phase name so
            # `Phase2Verdict.halted_at_gate` is structurally accurate
            # for downstream routing + audit logs.
            # Codex round-3 [P3]: ``str(CalledProcessError)`` only
            # produces the "Command ... returned non-zero" message —
            # the actual git stderr (missing .git / inode exhaustion /
            # submodule divergence) is the actionable clue. Capture it
            # explicitly when the exception type carries it.
            error_msg = f"{type(e).__name__}: {e}"
            stderr_payload = getattr(e, "stderr", None)
            if isinstance(stderr_payload, bytes):
                stderr_tail = stderr_payload.decode(
                    "utf-8", errors="replace"
                )
            elif isinstance(stderr_payload, str):
                stderr_tail = stderr_payload
            else:
                stderr_tail = ""
            return Phase2Verdict(
                status="blocked",
                halted_at_gate="post_baseline_fact_refresh",
                gate_result=GateResult(
                    status="inconclusive",
                    details={
                        "gate": "post_baseline_fact_refresh",
                        "reason": "post_baseline_fact_refresh_failed",
                        "error": error_msg,
                        "stderr_tail": stderr_tail[-2000:],
                    },
                ),
            )

        r = self.gate3_manifest(manifest=manifest, facts=post_baseline_facts)
        if r.status != "pass" or r.escalate:
            return Phase2Verdict(
                status="blocked",
                halted_at_gate="gate3_manifest",
                gate_result=r,
            )

        r = self.gate4_codex_review(codex_command=codex_command)
        if r.status != "pass" or r.escalate:
            return Phase2Verdict(
                status="blocked",
                halted_at_gate="gate4_codex_review",
                gate_result=r,
            )

        r = self.gate5_acceptance(
            criteria=criteria,
            attempt_id=attempt_id,
            retry_idx=retry_idx,
        )
        if r.status != "pass" or r.escalate:
            return Phase2Verdict(
                status="blocked",
                halted_at_gate="gate5_acceptance",
                gate_result=r,
            )

        r = self.gate6_regression(
            smoke_command=smoke_command,
            timeout_sec=smoke_timeout_sec,
        )
        if r.status != "pass" or r.escalate:
            return Phase2Verdict(
                status="blocked",
                halted_at_gate="gate6_regression",
                gate_result=r,
            )

        return Phase2Verdict(status="pass")


# ----------------------------------------------------------------------
# T14 — R3 9-step transactional sequence steps 1-7 (Gate 7 local merge).
#
# Per design §6 line 242 transition table + Y5 gap-by-gap crash semantics.
# T14 owns steps 1-7 + mid-merge crash detection. T15 owns steps 8 + 9a/9b
# (gate 8 + completion paths). T19 (Group G) owns dispatch-side recovery
# routing — `detect_mid_merge_crash` is the contract it consumes.
#
# Subprocess discipline (T13 K-class lesson): the 3 git subprocess calls
# below run in list-form (no shell=True) so E-class injection is not a
# concern. They do still need timeout protection — `git merge` can hang
# on lock contention, GPG prompts, or pre-commit hooks; `git rev-parse`
# can hang on a corrupt repo. A small list-form sibling helper
# `_run_argv_with_pgkill` mirrors `_run_shell_with_pgkill`'s timeout +
# process-group-kill semantics for argv invocations. Reuse over reinvent
# (I-class).
# ----------------------------------------------------------------------


# Module-level timeouts — kept as named constants so reviewers can grep
# them. Both are conservative defaults; tests inject smaller values via
# the `_GIT_*_TIMEOUT_SEC` patches if needed.
_GIT_HEAD_QUERY_TIMEOUT_SEC = 30
_GIT_MERGE_TIMEOUT_SEC = 60

# Code-review round-1 [P1-3]: argv allowlist guard. ``merge_strategy`` is
# spliced directly into the ``git merge <strategy> <branch>`` argv. Only
# ``--ff-only`` is in scope for v0.8.1; ``--no-ff`` is reserved for
# v0.8.2. Any other value (e.g. ``--squash``, ``--strategy=ours``) MUST
# fail loud at the merge_task entry point — BEFORE any disk side-effect
# (event append / checkpoint write / progress.md status) — so a future
# caller passing a user-derived string gets an immediate rejection
# instead of a silent strategy substitution.
_ALLOWED_MERGE_STRATEGIES: tuple[str, ...] = ("--ff-only", "--no-ff")


def _run_argv_with_pgkill(
    argv: list[str],
    *,
    cwd: Path,
    timeout_sec: int,
) -> _ShellRunResult:
    """List-form sibling of :func:`_run_shell_with_pgkill`. Same
    timeout + process-group-kill semantics; argv goes through ``Popen``
    without ``shell=True`` so shell metachars are NOT a concern (E-class
    safe by construction).

    The pgkill mechanics still matter: ``git`` subprocesses can spawn
    children (hooks, GPG agent prompts, alternate-object readers); on
    timeout we must SIGTERM/SIGKILL the entire group, not just the
    direct child, otherwise children orphan to PID 1 (T7/T12/T13
    pgkill recurrence).
    """
    try:
        proc = subprocess.Popen(
            argv,
            shell=False,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as e:
        return _ShellRunResult(
            returncode=None, stdout="", stderr="",
            spawn_error=f"{type(e).__name__}: {e}",
        )

    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
        return _ShellRunResult(
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + _PROCESS_GROUP_KILL_GRACE_SEC
        while time.monotonic() < deadline:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(
                timeout=_PROCESS_GROUP_KILL_GRACE_SEC,
            )
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return _ShellRunResult(
            returncode=None,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=True,
        )


def _now_iso() -> str:
    """ISO 8601 UTC timestamp with `Z` suffix. Stable across the module."""
    return datetime.datetime.now(datetime.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )


def _now_iso_micro() -> str:
    """ISO 8601 UTC timestamp with microsecond precision and `Z` suffix.

    Used for checkpoint filenames where second-precision collisions are
    possible (TOCTOU between MergeRunner pre_merge and Gate8 9a
    post_merge writes for fast tasks — both call ``write_checkpoint``,
    which derives the filename from the timestamp; same-second writes
    raise ``FileExistsError``). Microsecond precision disambiguates the
    filenames without requiring a retry loop.

    The `:` characters in HH:MM:SS still need to be sanitized by
    ``write_checkpoint`` (as for ``_now_iso``); the `.` separator
    introduced by ``%f`` is filesystem-safe on all targets we support.
    """
    return datetime.datetime.now(datetime.UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ",
    )


def _summarize_porcelain_z(porcelain_z: str) -> str:
    """Render a debug-readable file list from ``git status --porcelain -z
    --untracked-files=all`` output.

    H-class: porcelain ``-z`` is NUL-delimited, status-aware. Renames /
    copies emit TWO records (status+new-path, then bare old-path); we
    must consume the old-path record so it is not mis-classified. Reuses
    the parsing pattern from ``derive_task_facts`` (T11 round-2 P1).

    The output is bounded for use in ``block_reason`` strings — callers
    further trim with ``[:N]``. Format: ``"XY path[, XY path]..."``.
    """
    records = porcelain_z.split("\x00")
    parts: list[str] = []
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if not rec or len(rec) < 4:
            # Trailing NUL artifact / malformed; skip.
            continue
        xy = rec[:2]
        path = rec[3:]
        if not path:
            continue
        parts.append(f"{xy} {path}")
        if rec[0] in ("R", "C") and i < len(records):
            # Consume the old-path record (no status prefix).
            i += 1
    return ", ".join(parts)


@dataclass
class MergeResult:
    """Return type for :meth:`MergeRunner.merge_task`.

    `status`:
      - "merged": git merge applied + `merge_applied` event emitted.
      - "blocked": merge refused or failed; `block_reason` populated.
        Caller writes blocked.md per §1 routing (T15 + T19 own blocked.md
        emission for mid-merge gaps; T14 only returns the verdict).
    """
    status: str
    target_commit_pre_merge: Optional[str] = None
    target_commit_post_merge: Optional[str] = None
    block_reason: Optional[str] = None


class MergeRunner:
    """R3 9-step transactional sequence — steps 1-7.

    Step 1 (gates 1-6 pass) is the caller's contract: ``merge_task`` MUST
    only be invoked after :meth:`GateRunner.run_phase2` returned
    ``status == "pass"``. T14 does NOT re-verify; that is the wave-runner /
    dispatcher's responsibility.

    Steps 2-7 are this method's atomic-write sequence:
      2. ``decisions.jsonl`` append ``task_ready_to_merge``
      3. ``checkpoints/<ts>.md`` write (``phase=pre_merge``)
      4. ``progress.md`` task status → ``merging``
      5. ``decisions.jsonl`` append ``merge_started``
      6. ``git merge`` into ``integration_target`` (R9 HEAD safety check
         BEFORE the merge call: refuse if repo HEAD is not on
         ``integration_target``).
      7. ``decisions.jsonl`` append ``merge_applied``
    """

    def __init__(
        self, *,
        ctx: WorktreeContext, contract: Contract, task_dir: Path,
        run_id: str, task_id: str,
    ):
        self.ctx = ctx
        self.contract = contract
        self.task_dir = task_dir
        self.run_id = run_id
        self.task_id = task_id

    # ------------------------------------------------------------------
    # Public surface.
    # ------------------------------------------------------------------

    def merge_task(
        self, *, facts: TaskFacts, merge_strategy: str,
    ) -> MergeResult:
        """Execute steps 2-7 of the R3 transactional sequence."""
        # Code-review round-1 [P1-3]: allowlist guard. Run BEFORE any
        # disk side-effect so an unsupported strategy never produces
        # half-written events / checkpoints. v0.8.1 only ``--ff-only``
        # is exercised; allowlist documents the contract and fails
        # loud on accidental expansion.
        if merge_strategy not in _ALLOWED_MERGE_STRATEGIES:
            raise ValueError(
                f"unsupported merge_strategy: {merge_strategy!r}; "
                f"allowed: {_ALLOWED_MERGE_STRATEGIES}"
            )
        # Step 2 — task_ready_to_merge.
        append_autonomy_event(
            self.task_dir, EVENT_TASK_READY_TO_MERGE,
            {
                "event_id": _new_event_id(),
                "ts": _now_iso(),
                "slug": self.ctx.slug,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "worktree_id": self.ctx.worktree_id,
                "worktree_path": str(self.ctx.worktree_path),
                "original_base_commit": self.ctx.original_base_commit,
                "current_base_commit": self.ctx.current_base_commit,
                "lifecycle_state": "merging",
                "diff_hash": facts.diff_hash,
                "target_commit_pre_merge": facts.target_commit_pre_merge,
            },
        )
        # Step 3 — pre_merge checkpoint. Use microsecond precision so
        # a fast task whose Gate8 9a post_merge checkpoint lands in the
        # same wall-clock second as this pre_merge write does NOT
        # collide on the derived filename (TOCTOU; Codex round-1 P1).
        ts = _now_iso_micro()
        write_checkpoint(
            self.task_dir, ts=ts,
            body=(
                f"phase: pre_merge\n"
                f"worktree_id: {self.ctx.worktree_id}\n"
                f"worktree_path: {self.ctx.worktree_path}\n"
                f"original_base_commit: {self.ctx.original_base_commit}\n"
                f"current_base_commit: {self.ctx.current_base_commit}\n"
                f"lifecycle_state: merging\n"
                f"diff_hash: {facts.diff_hash}\n"
                f"target_commit_pre_merge: {facts.target_commit_pre_merge}\n"
            ),
            git_hash=facts.target_commit_pre_merge,
        )
        # Step 4 — progress.md status → merging. Naïve writer; T20 owns
        # the lint + schema enforcement.
        self._update_task_status("merging")
        # Steps 5-7.
        return self._continue_merge(
            facts=facts, merge_strategy=merge_strategy,
        )

    # ------------------------------------------------------------------
    # Internal — steps 5-7 + helpers.
    # ------------------------------------------------------------------

    def _continue_merge(
        self, *, facts: TaskFacts, merge_strategy: str,
    ) -> MergeResult:
        """Steps 5-7. Split out so step 4's status update is the gap-
        boundary commit point — a crash between step 4 and step 5 is
        observable by readers as ``task_ready_to_merge`` without a
        following ``merge_started`` (Y5).

        Code-review round-1 [P1-1]: the R9 HEAD safety pre-check runs
        BEFORE the ``merge_started`` event is emitted. Reason: R9 block
        + emitted ``merge_started`` would leave a phantom gap signature
        (``merge_started`` without ``merge_applied``) that
        :func:`detect_mid_merge_crash` reports as ``mid_merge_crash`` —
        but no git ran, so the recommended ``replay_merge_from_diff_hash``
        is misleading. Emit ``merge_started`` only after the HEAD
        invariant is proven.
        """
        # R9 HEAD pre-check (BEFORE merge_started event — see docstring).
        repo_root = self._derive_repo_root()

        # R9 HEAD assertion: refuse to merge unless repo_root HEAD is
        # already on integration_target. `git merge` would otherwise
        # silently merge into whatever the user last checked out — a
        # destructive footgun (their feature branch absorbs our task
        # branch). NO auto-checkout: that mutates user state without
        # consent. Block reason names the wrong HEAD + expected target
        # so the user can recover manually.
        head_query = _run_argv_with_pgkill(
            ["git", "-C", str(repo_root),
             "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            timeout_sec=_GIT_HEAD_QUERY_TIMEOUT_SEC,
        )
        if head_query.timed_out:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason="git rev-parse --abbrev-ref HEAD timed out",
            )
        if head_query.spawn_error is not None:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"git rev-parse spawn failed: {head_query.spawn_error}"
                ),
            )
        if head_query.returncode != 0:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"git rev-parse --abbrev-ref HEAD returned "
                    f"rc={head_query.returncode}: "
                    f"{head_query.stderr[-500:]}"
                ),
            )
        head_ref = head_query.stdout.strip()
        if head_ref != self.ctx.integration_target:
            # R9 safety: refuse to merge into the wrong branch.
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"refusing to merge into HEAD={head_ref!r}: "
                    f"expected integration_target="
                    f"{self.ctx.integration_target!r}; "
                    f"checkout integration_target manually and resume"
                ),
            )

        # Codex round-1 [P1]: Gate 7 silently dropped uncommitted /
        # untracked content from the integration merge. T11's manifest
        # verifier (which DOES inspect the working tree) only blocks
        # out-of-scope writes; it does NOT require the subagent to
        # commit. Acceptance gates run inside the worktree so they see
        # uncommitted edits as "working". But ``git merge ctx.branch``
        # only integrates COMMITTED commits — leaving the dirty content
        # silently dropped while ``merge_applied`` reports success.
        # Three pre-checks below close the bypass:
        #   Check #1 — task worktree must be clean (no staged /
        #              unstaged / untracked).
        #   Check #2 — task branch HEAD must equal
        #              facts.target_commit_pre_merge (TOCTOU defense:
        #              another commit landing between fact derivation
        #              and gate 7 invalidates earlier gate verdicts).
        #   Check #3 — task worktree symbolic HEAD must point at
        #              ``ctx.branch`` (codex round-2 [P1]: subagent can
        #              ``git checkout --detach`` or switch branches; w/o
        #              this, step 6 would merge the original branch ref
        #              which still points at the pre-task base).
        # All fail-closed BEFORE merge_started — no phantom gap.
        # (TOCTOU window between Check #3 and step 6 remains; v0.8.1
        # single-process orchestrator accepts this. Step 6 also merges
        # the verified SHA — not the branch ref — as belt-and-suspenders.)

        # Check #1 — task worktree clean.
        status = _run_argv_with_pgkill(
            ["git", "-C", str(self.ctx.worktree_path),
             "status", "--porcelain", "-z", "--untracked-files=all"],
            cwd=self.ctx.worktree_path,
            timeout_sec=_GIT_HEAD_QUERY_TIMEOUT_SEC,
        )
        if status.timed_out:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason="task worktree status check timed out",
            )
        if status.spawn_error is not None:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"task worktree status spawn failed: "
                    f"{status.spawn_error}"
                ),
            )
        if status.returncode != 0:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"task worktree status check failed: "
                    f"rc={status.returncode}: {status.stderr[-500:]}"
                ),
            )
        if status.stdout != "":
            # Parse with the porcelain -z helper (T11 lesson: NUL-delim,
            # not space-split). Distinguish "untracked only" vs "any
            # uncommitted" so the block_reason matches the failure mode
            # for forensics + tests.
            summary = _summarize_porcelain_z(status.stdout)
            has_tracked_change = any(
                rec and len(rec) >= 4 and rec[:2] != "??"
                for rec in status.stdout.split("\x00")
            )
            if has_tracked_change:
                preface = (
                    "task worktree has uncommitted or untracked content; "
                    "subagent must commit all in-scope changes before merge"
                )
            else:
                preface = (
                    "task worktree has untracked file(s); subagent must "
                    "commit or remove them before merge"
                )
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=f"{preface}. Dirty entries: {summary[:500]}",
            )

        # Check #2 — task branch HEAD matches facts.target_commit_pre_merge.
        branch_head = _run_argv_with_pgkill(
            ["git", "-C", str(self.ctx.worktree_path),
             "rev-parse", "HEAD"],
            cwd=self.ctx.worktree_path,
            timeout_sec=_GIT_HEAD_QUERY_TIMEOUT_SEC,
        )
        if branch_head.timed_out:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason="task branch rev-parse HEAD timed out",
            )
        if branch_head.spawn_error is not None:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"task branch rev-parse spawn failed: "
                    f"{branch_head.spawn_error}"
                ),
            )
        if branch_head.returncode != 0:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"task branch rev-parse failed: "
                    f"rc={branch_head.returncode}: "
                    f"{branch_head.stderr[-500:]}"
                ),
            )
        actual_head = branch_head.stdout.strip()
        if actual_head != facts.target_commit_pre_merge:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"task branch HEAD drifted: facts recorded "
                    f"{facts.target_commit_pre_merge[:12]} but worktree "
                    f"HEAD is now {actual_head[:12]}; re-run gates "
                    f"against the new HEAD"
                ),
            )

        # Check #3 — symbolic HEAD must point at ``ctx.branch``. Catches
        # subagent running ``git checkout --detach`` or switching to
        # another branch. Without this, Check #2 still passes (worktree
        # HEAD is whatever subagent committed) but step 6 would merge
        # the ORIGINAL ``ctx.branch`` ref, which may still point at the
        # pre-task base — silently dropping the gated commit.
        symref = _run_argv_with_pgkill(
            ["git", "-C", str(self.ctx.worktree_path),
             "symbolic-ref", "--short", "HEAD"],
            cwd=self.ctx.worktree_path,
            timeout_sec=_GIT_HEAD_QUERY_TIMEOUT_SEC,
        )
        if symref.timed_out:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason="task worktree symbolic-ref HEAD timed out",
            )
        if symref.spawn_error is not None:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"task worktree symbolic-ref spawn failed: "
                    f"{symref.spawn_error}"
                ),
            )
        if symref.returncode != 0:
            # symbolic-ref returns non-zero when HEAD is detached
            # (or any other unreadable state). Fail-closed.
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"task worktree HEAD is detached "
                    f"(symbolic-ref rc={symref.returncode}); subagent "
                    f"must keep HEAD attached to {self.ctx.branch!r}"
                ),
            )
        actual_branch = symref.stdout.strip()
        if not actual_branch:
            # F-class fail-closed: empty stdout despite rc=0 is
            # malformed git output we refuse to trust.
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    "task worktree symbolic-ref returned empty branch "
                    "name (unexpected)"
                ),
            )
        if actual_branch != self.ctx.branch:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"task worktree on wrong branch: HEAD points to "
                    f"{actual_branch!r}, expected {self.ctx.branch!r}"
                ),
            )

        # Step 5 — merge_started. Emitted ONLY after R9 HEAD pre-check +
        # Check #1 (clean worktree) + Check #2 (branch HEAD == facts) +
        # Check #3 (symbolic HEAD == ctx.branch) all pass; otherwise we'd
        # write a phantom gap that detect_mid_merge_crash would mistake
        # for a real mid_merge_crash (see code-review P1-1 fix).
        # ``branch_at_merge`` records the verified symbolic ref for
        # forensic clarity.
        append_autonomy_event(
            self.task_dir, EVENT_MERGE_STARTED,
            {
                "event_id": _new_event_id(),
                "ts": _now_iso(),
                "slug": self.ctx.slug,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "worktree_id": self.ctx.worktree_id,
                "worktree_path": str(self.ctx.worktree_path),
                "integration_target": self.ctx.integration_target,
                "target_commit_pre_merge": facts.target_commit_pre_merge,
                "branch_at_merge": actual_branch,
            },
        )

        # Step 6 — git merge. Codex round-2 [P1]: merge the EXACT verified
        # SHA, not ``ctx.branch``. Checks #2 + #3 lock down branch state,
        # but merging the SHA directly eliminates the entire class of
        # "branch ref might not point at what we think" issues.
        merge_run = _run_argv_with_pgkill(
            ["git", "-C", str(repo_root),
             "merge", merge_strategy, facts.target_commit_pre_merge],
            cwd=repo_root,
            timeout_sec=_GIT_MERGE_TIMEOUT_SEC,
        )
        if merge_run.timed_out:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason="git merge timed out",
            )
        if merge_run.spawn_error is not None:
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"git merge spawn failed: {merge_run.spawn_error}"
                ),
            )
        if merge_run.returncode != 0:
            # Conflict / non-ff push / hook reject / etc. — surface to
            # caller as blocked. T19 routes to the R3 mid-merge crash
            # reconcile menu via blocked.md.
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"git merge failed: rc={merge_run.returncode}: "
                    f"{merge_run.stderr[-500:]}"
                ),
            )

        post_query = _run_argv_with_pgkill(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            cwd=repo_root,
            timeout_sec=_GIT_HEAD_QUERY_TIMEOUT_SEC,
        )
        if (
            post_query.timed_out
            or post_query.spawn_error is not None
            or post_query.returncode != 0
        ):
            # Forensic edge case: merge applied, but we can't read the
            # new HEAD. Surface the gap so T19 picks it up via
            # detect_mid_merge_crash (we never wrote merge_applied).
            reason = (
                "post-merge git rev-parse HEAD timed out"
                if post_query.timed_out
                else "post-merge git rev-parse spawn failed"
                if post_query.spawn_error is not None
                else f"post-merge git rev-parse rc={post_query.returncode}: "
                     f"{post_query.stderr[-500:]}"
            )
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=reason,
            )
        target_commit_post_merge = post_query.stdout.strip()
        if not target_commit_post_merge or len(target_commit_post_merge) < 7:
            # F-class fail-closed (mirrors create_task_worktree precedent).
            return MergeResult(
                status="blocked",
                target_commit_pre_merge=facts.target_commit_pre_merge,
                block_reason=(
                    f"post-merge git rev-parse returned unusable output: "
                    f"{target_commit_post_merge!r}"
                ),
            )

        # Step 7 — merge_applied.
        append_autonomy_event(
            self.task_dir, EVENT_MERGE_APPLIED,
            {
                "event_id": _new_event_id(),
                "ts": _now_iso(),
                "slug": self.ctx.slug,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "worktree_id": self.ctx.worktree_id,
                "target_commit_post_merge": target_commit_post_merge,
                "merge_strategy": merge_strategy,
            },
        )
        return MergeResult(
            status="merged",
            target_commit_pre_merge=facts.target_commit_pre_merge,
            target_commit_post_merge=target_commit_post_merge,
        )

    def _derive_repo_root(self) -> Path:
        """Resolve the parent git repo from ``ctx.worktree_path``.

        v0.8.1 worktrees live at
        ``<repo_root>/.claude/worktrees/<worktree_id>/`` so the parent
        repo is ``worktree_path.parents[2]`` (parents: 0=worktrees,
        1=.claude, 2=repo_root).
        """
        return self.ctx.worktree_path.parents[2]

    def _update_task_status(self, status: str) -> None:
        """Naïve `progress.md` task-status writer.

        Maintains a single line of the form
        ``<!-- T14 task_status: <status> -->`` near the top of progress.md
        (after the H1 title). T14 sets ``merging``; T15 sets
        ``completed`` / ``failed`` / ``blocked_post_merge`` etc.

        T20 owns lint + schema enforcement (status enum from §8.3.1) and
        will replace this writer with a structured table. Until then, a
        clearly marked HTML comment is the smallest disk side-effect that
        downstream tools can grep for without conflicting with the
        existing free-form markdown body.

        If progress.md doesn't exist (e.g., test fixture), this is a
        no-op — the merge sequence is still observable via decisions.jsonl
        + checkpoints/, which are the authoritative state per §6.
        """
        progress_path = self.task_dir / "progress.md"
        marker_prefix = f"<!-- task_status[{self.task_id}]: "
        new_line = f"{marker_prefix}{status} -->"
        if not progress_path.is_file():
            # Fresh fixture — append a one-line stub. Safe-create.
            try:
                progress_path.parent.mkdir(parents=True, exist_ok=True)
                progress_path.write_text(new_line + "\n", encoding="utf-8")
            except OSError:
                # Don't crash the merge sequence on a progress.md write
                # failure; decisions.jsonl + checkpoints are the
                # authoritative state. Surface to stderr for visibility.
                print(
                    f"WARN: failed to write task_status marker to "
                    f"{progress_path}",
                    file=sys.stderr,
                )
            return
        try:
            text = progress_path.read_text(encoding="utf-8")
        except OSError:
            print(
                f"WARN: failed to read {progress_path} for task_status "
                f"update", file=sys.stderr,
            )
            return
        lines = text.splitlines(keepends=False)
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(marker_prefix):
                lines[i] = new_line
                replaced = True
                break
        if not replaced:
            # Insert after the first non-empty line (typically `# title`).
            insert_idx = 0
            for i, line in enumerate(lines):
                if line.strip():
                    insert_idx = i + 1
                    break
            lines.insert(insert_idx, new_line)
        new_text = "\n".join(lines)
        if not new_text.endswith("\n"):
            new_text += "\n"
        try:
            progress_path.write_text(new_text, encoding="utf-8")
        except OSError:
            print(
                f"WARN: failed to write task_status marker to "
                f"{progress_path}", file=sys.stderr,
            )


# ----------------------------------------------------------------------
# T15 — Gate 8 + 9a/9b transactional sub-steps (post-merge verify) +
# MergeQueue serialization (S1 wave block) + S3 skip rule.
#
# Step 8 of R3 runs in an EPHEMERAL verification worktree created from
# the integration target's post-merge SHA (§4 Y4). Acceptance criteria
# (Phase 3) + a final regression smoke run inside that fresh checkout
# — verifying the merge as observers will see it, not as the task
# worktree saw it (which is now removed on PASS).
#
# Pitfall coverage (per .flow/pitfalls/claude-review-blindspots.md):
#   - I/K: every subprocess goes through `_run_argv_with_pgkill` /
#     `_run_shell_with_pgkill` — no plain `subprocess.run`, no
#     `shell=True` shortcut, no "fixed CLI doesn't need pgkill"
#     justification (T13 was caught EXACTLY on that).
#   - N: verification worktree is created from the post-merge SHA
#     directly (`git worktree add --detach <SHA>`); NEVER a ref. The
#     path scheme uses the 7-char shortsha so two reruns at different
#     post-merge commits produce distinct paths.
#   - G2: verification worktree HEAD == post-merge SHA by
#     construction. Acceptance reads the same on-disk bytes a future
#     observer of the integration branch would see, not whatever the
#     task worktree happened to leave behind (worktree clean
#     guarantees from T14 cover the merge entry; G2 here covers the
#     merge exit).
#   - L: every JSON read uses string-equality comparison only — no
#     substring scanning of `event` / `run_id` values.
#   - M: `MergeQueue.can_proceed` filters by `run_id` so an unrelated
#     run's `post_merge_verify_failed` cannot halt the current run.
#   - 9a/9b atomicity: the rename-aside on FAIL handles a pre-
#     existing `verify/aborted/<id>+failed/` by appending a unique
#     timestamp suffix (overwriting would erase prior diagnostic
#     evidence; raising would convert a recoverable observation gap
#     into a crash mid-9b).
# ----------------------------------------------------------------------


@dataclass
class Gate8Result:
    """Return type for :meth:`Gate8VerificationRunner.verify`.

    `status`:
      - "completed": gate 8 PASS → 9a executed (BOTH worktrees cleaned
        up, `task_completed` event emitted, `phase=post_merge`
        checkpoint written, progress.md status → completed).
      - "blocked_post_merge": gate 8 FAIL → 9b executed (verification
        worktree preserved at verify/aborted/, blocked.md written,
        merge stays intact, progress.md status → blocked_post_merge).
    """
    status: str
    verification_worktree_id: Optional[str] = None
    blocked_md_path: Optional[Path] = None
    criteria_results: list = field(default_factory=list)


class Gate8VerificationRunner:
    """R3 step 8 + sub-steps 9a / 9b — post-merge verification.

    See §3 gate 8 (R4) + §4 Y4 (verification worktree path/lifecycle)
    + §1 row 18 (`blocked_post_merge` semantics: merge stays intact;
    user choice set ``{keep_and_fix_interactive, revert_merge,
    split_followup, abort_run}``).

    The constructor accepts an optional ``notifier`` so the
    orchestrator dispatch loop (T19 Step 19.11) can inject a single
    Notifier shared across recovery dispatcher / dispatch / gate
    runner / merger / gate 8. T15 itself only needs the back-compat
    direct-write_blocked path — the Notifier branch is exercised by
    T16+ smoke once Tier 2 lands.
    """

    def __init__(
        self, *,
        ctx: WorktreeContext, contract: Contract, task_dir: Path,
        run_id: str, task_id: str, target_commit_post_merge: str,
        notifier: Optional[object] = None,
    ):
        if not isinstance(target_commit_post_merge, str) or not target_commit_post_merge:
            # F-class fail-closed: missing post-merge SHA is non-recoverable
            # — the verification worktree path scheme depends on it.
            raise ValueError(
                f"target_commit_post_merge required; got "
                f"{target_commit_post_merge!r}"
            )
        self.ctx = ctx
        self.contract = contract
        self.task_dir = task_dir
        self.run_id = run_id
        self.task_id = task_id
        self.target_commit_post_merge = target_commit_post_merge
        self.notifier = notifier
        self.verification_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Path scheme + worktree creation.
    # ------------------------------------------------------------------

    def _verification_path(self) -> Path:
        """Path scheme per §4 Y4:
        ``<repo_root>/.claude/worktrees/verify/<run_id>+t<idx>+<sha[:7]>/``.

        Uses ``ctx.task_idx`` (the same int that drives the task
        worktree's `t<n>` segment) so observers can correlate the
        verification path to the task by visual inspection.
        """
        short = self.target_commit_post_merge[:7]
        name = f"{self.run_id}+t{self.ctx.task_idx}+{short}"
        return (
            self._repo_root() / WORKTREE_ROOT / "verify" / name
        )

    def _repo_root(self) -> Path:
        """Resolve the parent git repo from ``ctx.worktree_path``.

        Same convention as :meth:`MergeRunner._derive_repo_root`: the
        task worktree lives at
        ``<repo_root>/.claude/worktrees/<worktree_id>/`` so the
        repo is ``worktree_path.parents[2]``.
        """
        return self.ctx.worktree_path.parents[2]

    def _create_verification_worktree(self) -> Path:
        """Create the ephemeral verification worktree at
        :meth:`_verification_path` from the post-merge SHA.

        N-class hardening: ``git worktree add --detach <SHA>`` —
        SHA-based, NEVER a ref name. Even if the integration branch
        ref drifts mid-run, the verification worktree HEAD is pinned
        to the SHA gate 7 actually merged.

        Failure modes (typed-except per D5):
          - ``OSError`` creating the parent directory → re-raised as
            ``RuntimeError`` so the gate 8 entry sees a clear failure
            (callers can route to operator review).
          - ``git worktree add`` non-zero rc / timeout → same.

        On success, sets ``self.verification_id`` and returns the
        path. C-class: this MUST run cleanly BEFORE the
        ``post_merge_verification_started`` event is emitted (the
        plan template + T14 4-pre-check pattern); on failure no event
        is emitted.
        """
        path = self._verification_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(
                f"failed to create verify/ parent dir for {path}: "
                f"{type(e).__name__}: {e}"
            ) from e
        repo_root = self._repo_root()
        # I/K-class: route through `_run_argv_with_pgkill`. `_git()` is a
        # thin `subprocess.run` without timeout/pgkill — using it here
        # would orphan a slow `git worktree add` on hook timeout. Reuse
        # the established helper, no exception, no justification comment.
        result = _run_argv_with_pgkill(
            ["git", "-C", str(repo_root),
             "worktree", "add", "--detach",
             str(path), self.target_commit_post_merge],
            cwd=repo_root,
            timeout_sec=_GIT_HEAD_QUERY_TIMEOUT_SEC,
        )
        if result.spawn_error is not None or result.timed_out or result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed for verification path {path}: "
                f"rc={result.returncode}, timed_out={result.timed_out}, "
                f"spawn_error={result.spawn_error}, "
                f"stderr={result.stderr[-500:]}"
            )
        self.verification_id = path.name
        return path

    # ------------------------------------------------------------------
    # Public surface — verify() drives gate 8 + 9a/9b.
    # ------------------------------------------------------------------

    def verify(
        self, *,
        criteria: list,                 # list[AcceptanceCriterion]
        regression_command: str,
    ) -> Gate8Result:
        """Run gate 8 (Phase 3 acceptance + regression smoke) inside an
        ephemeral verification worktree. Routes to 9a (PASS) or 9b
        (FAIL).

        Per-criterion ``post_merge_skip=True`` excludes from gate 8
        (S3 skip rule; T1 enforces the cross-field validation that
        ``type=regression`` cannot set this flag unless
        ``contract.post_merge_regression_optional=true``).

        9-step ordering (§6 R3): the
        ``post_merge_verification_started`` event is emitted ONLY
        after :meth:`_create_verification_worktree` returns cleanly.
        Failure of worktree creation propagates as ``RuntimeError``
        WITHOUT emitting any event — observers see no started/
        completed pair, no orphan progress entry.
        """
        vpath = self._create_verification_worktree()
        # 8-1: started event (post-creation per C-class ordering).
        append_autonomy_event(
            self.task_dir, EVENT_POST_MERGE_VERIFICATION_STARTED,
            {
                "event_id": _new_event_id(),
                "ts": _now_iso(),
                "slug": self.ctx.slug,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "verification_worktree_id": self.verification_id,
                "verification_worktree_path": str(vpath),
                "target_commit_post_merge": self.target_commit_post_merge,
            },
        )

        # S3 skip rule: per-criterion `post_merge_skip` excludes
        # from gate 8 (T1's parser already rejects regression+skip
        # cross-field unless explicitly opted in via
        # `contract.post_merge_regression_optional=true`).
        #
        # Codex round-1 P2: build (orig_idx, crit) pairs BEFORE
        # filtering so ``criterion_idx`` and ``results[*].idx`` track
        # the ORIGINAL contract index. A naïve ``enumerate(effective)``
        # collapses indices after a leading skip, mismatching audit
        # trail vs contract definition.
        indexed = [
            (i, c) for i, c in enumerate(criteria)
            if not getattr(c, "post_merge_skip", False)
        ]

        # Phase 3 acceptance — lazy-import keeps the dry-run path
        # decoupled from heavier acceptance modules (matches T12/T13
        # pattern in `GateRunner`).
        from flow_acceptance import (  # type: ignore
            AcceptanceRunner, EvalDecision,
        )

        runner = AcceptanceRunner(
            worktree_root=vpath,
            log_dir=self.task_dir / "logs" / "post_merge",
            slug=self.ctx.slug,
            task_id=self.task_id,
            run_id=self.run_id,
            worktree_id=self.verification_id,
        )

        # Codex round-1 P2: attempt_id MUST be task-scoped, not just
        # run-scoped. ``AcceptanceRunner.find_resume_point`` filters
        # ``acceptance-progress.jsonl`` solely by ``attempt_id``; a
        # multi-task run would otherwise have all tasks' gate-8 rows
        # share the same ``post_merge_<run_id>`` key and cross-pollute
        # on resume.
        post_merge_attempt_id = (
            f"post_merge_{self.run_id}_{self.task_id}"
        )

        results: list = []
        gate_passed = True
        for orig_idx, crit in indexed:
            rr = runner.run_one(
                crit, criterion_idx=orig_idx,
                attempt_id=post_merge_attempt_id, retry_idx=0,
                task_dir=self.task_dir,
            )
            decision = runner.evaluate_criterion(
                crit, phase=3, runner_result=rr,
            )
            results.append({
                "idx": orig_idx,
                "status": getattr(rr, "status", "inconclusive"),
                "decision": decision.value,
            })
            if decision != EvalDecision.PASS:
                gate_passed = False
                break

        # Final regression smoke — runs ONLY if all per-criterion
        # checks passed. I/K-class: route through
        # `_run_shell_with_pgkill` (T13 was caught on the exact
        # `subprocess.run(shell=True)` shortcut here).
        if gate_passed:
            smoke = _run_shell_with_pgkill(
                regression_command,
                cwd=vpath,
                timeout_sec=_DEFAULT_GATE_TIMEOUT_SEC,
            )
            if smoke.spawn_error is not None or smoke.timed_out or smoke.returncode != 0:
                gate_passed = False
                results.append({
                    "regression_smoke": "fail",
                    "returncode": smoke.returncode,
                    "timed_out": smoke.timed_out,
                    "spawn_error": smoke.spawn_error,
                    "stderr_tail": smoke.stderr[-500:],
                })

        # 8-2: completed event (always — regardless of pass/fail, so
        # the `started`/`completed` pair is honored). The 9b path
        # later emits a separate `post_merge_verify_failed` event
        # carrying user-choice set + blocked.md path.
        append_autonomy_event(
            self.task_dir, EVENT_POST_MERGE_VERIFICATION_COMPLETED,
            {
                "event_id": _new_event_id(),
                "ts": _now_iso(),
                "slug": self.ctx.slug,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "verification_worktree_id": self.verification_id,
                "status": "pass" if gate_passed else "fail",
                "criteria_results": results,
            },
        )

        if gate_passed:
            return self._complete_9a(results)
        return self._fail_9b(results)

    # ------------------------------------------------------------------
    # 9a — PASS path (task_completed + checkpoint + cleanup BOTH worktrees).
    # ------------------------------------------------------------------

    def _complete_9a(self, results: list) -> Gate8Result:
        """9a sub-steps:

          9a.1: emit ``task_completed`` event with the final
                cumulative diff hash (computed from the task worktree
                BEFORE cleanup — once removed we can't read it back).
          9a.2: write ``phase=post_merge`` checkpoint.
          9a.3: progress.md status → ``merged``.
          9a.4: cleanup task worktree (remove + branch -D).
          9a.5: cleanup verification worktree.

        Steps 9a.4/9a.5 are irreversible — only run on a TRULY clean
        PASS (all criteria + regression smoke green). Helper choice:
        every git invocation routes through ``_run_argv_with_pgkill``
        (I/K-class).
        """
        repo_root = self._repo_root()

        # 9a.1: compute cumulative final_diff_hash from the TASK
        # worktree BEFORE cleanup. The task worktree's HEAD reflects
        # the post-subagent state; original_base_commit anchors the
        # diff range. Same hashing convention as T11's
        # `derive_task_facts.diff_hash`.
        diff_proc = _run_argv_with_pgkill(
            ["git", "-C", str(self.ctx.worktree_path),
             "diff", "--unified=0",
             f"{self.ctx.original_base_commit}..HEAD"],
            cwd=self.ctx.worktree_path,
            timeout_sec=_GIT_HEAD_QUERY_TIMEOUT_SEC,
        )
        if diff_proc.spawn_error is not None or diff_proc.timed_out or diff_proc.returncode != 0:
            # Don't crash the 9a path on a diff read — task_completed
            # is more important than a perfect hash. Surface the gap
            # in the event payload via a sentinel hash so audit can
            # tell the read failed.
            final_diff_hash = ""
        else:
            final_diff_hash = hashlib.sha256(
                diff_proc.stdout.encode("utf-8"),
            ).hexdigest()

        append_autonomy_event(
            self.task_dir, EVENT_TASK_COMPLETED,
            {
                "event_id": _new_event_id(),
                "ts": _now_iso(),
                "slug": self.ctx.slug,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "worktree_id": self.ctx.worktree_id,
                "final_diff_hash": final_diff_hash,
                "target_commit_post_merge": self.target_commit_post_merge,
            },
        )

        # 9a.2: phase=post_merge checkpoint. Use microsecond precision
        # to avoid a same-second collision with MergeRunner's pre_merge
        # checkpoint on a fast task (Codex round-1 P1 TOCTOU).
        write_checkpoint(
            self.task_dir, ts=_now_iso_micro(),
            body=(
                f"phase: post_merge\n"
                f"worktree_id: {self.ctx.worktree_id}\n"
                f"final_diff_hash: {final_diff_hash}\n"
                f"target_commit_post_merge: {self.target_commit_post_merge}\n"
            ),
            git_hash=self.target_commit_post_merge,
        )

        # 9a.3: progress.md status → merged (canonical PASS terminal
        # status per design §4/§3/§7; T20 owns lint/enum). Note:
        # Gate8Result.status="completed" is an internal enum returned
        # to MergeRunner; only progress.md uses the "merged" canonical.
        self._update_task_status("merged")

        # 9a.4: cleanup task worktree. `check=False`-equivalent — WARN
        # on failure but don't crash; the merge is already in integration
        # target, verification PASSED, so a stale worktree dir is
        # cosmetic, not correctness. Symmetric WARN with 9b.
        rm_task = _run_argv_with_pgkill(
            ["git", "-C", str(repo_root),
             "worktree", "remove", "--force",
             str(self.ctx.worktree_path)],
            cwd=repo_root,
            timeout_sec=_GIT_HEAD_QUERY_TIMEOUT_SEC,
        )
        if rm_task.timed_out or rm_task.spawn_error is not None or rm_task.returncode != 0:
            print(
                f"WARN: 9a cleanup task worktree remove failed "
                f"({self.ctx.worktree_path}): "
                f"{rm_task.stderr[-500:] or rm_task.spawn_error or 'timeout'}",
                file=sys.stderr,
            )
        rm_branch = _run_argv_with_pgkill(
            ["git", "-C", str(repo_root),
             "branch", "-D", self.ctx.branch],
            cwd=repo_root,
            timeout_sec=_GIT_HEAD_QUERY_TIMEOUT_SEC,
        )
        if rm_branch.timed_out or rm_branch.spawn_error is not None or rm_branch.returncode != 0:
            print(
                f"WARN: 9a cleanup branch -D failed "
                f"({self.ctx.branch}): "
                f"{rm_branch.stderr[-500:] or rm_branch.spawn_error or 'timeout'}",
                file=sys.stderr,
            )

        # 9a.5: cleanup verification worktree.
        rm_verify = _run_argv_with_pgkill(
            ["git", "-C", str(repo_root),
             "worktree", "remove", "--force",
             str(self._verification_path())],
            cwd=repo_root,
            timeout_sec=_GIT_HEAD_QUERY_TIMEOUT_SEC,
        )
        if rm_verify.timed_out or rm_verify.spawn_error is not None or rm_verify.returncode != 0:
            print(
                f"WARN: 9a cleanup verification worktree remove failed "
                f"({self._verification_path()}): "
                f"{rm_verify.stderr[-500:] or rm_verify.spawn_error or 'timeout'}",
                file=sys.stderr,
            )

        return Gate8Result(
            status="completed",
            verification_worktree_id=self.verification_id,
            criteria_results=results,
        )

    # ------------------------------------------------------------------
    # 9b — FAIL path (post_merge_verify_failed + blocked.md + preserve).
    # ------------------------------------------------------------------

    def _fail_9b(self, results: list) -> Gate8Result:
        """9b sub-steps:

          9b.1: emit ``post_merge_verify_failed`` event.
          9b.2: write ``blocked.md`` with §1 row 18 user-choice set
                ``{keep_and_fix_interactive, revert_merge,
                split_followup, abort_run}``. Routes through
                ``Notifier`` when present; otherwise direct
                ``write_blocked`` call (back-compat for unit tests
                that don't construct a Notifier).
          9b.3: progress.md status → ``blocked_post_merge``.
          9b.4: preserve verification worktree at
                ``verify/aborted/<id>+failed/`` for post-mortem.

        Merge STAYS intact — NO auto-revert (§1 row 18). Operator
        chooses one of the four resolution paths via blocked.md.
        """
        blocked_md_path = self.task_dir / "blocked.md"
        choices = [
            "keep_and_fix_interactive", "revert_merge",
            "split_followup", "abort_run",
        ]
        # Cause/effect string: structured enough for grep, human-
        # readable enough for blocked.md. T19's recovery dispatcher
        # reads `block_type` (separate frontmatter field) for routing,
        # not `why_blocked`.
        why = (
            f"post_merge_verify_failed: {len(results)} criteria checked"
        )
        resume = f"flow orchestrator --resume {self.ctx.slug}"

        # 9b.1: event emission.
        append_autonomy_event(
            self.task_dir, EVENT_POST_MERGE_VERIFY_FAILED,
            {
                "event_id": _new_event_id(),
                "ts": _now_iso(),
                "slug": self.ctx.slug,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "verification_worktree_id": self.verification_id,
                "blocked_md_path": str(blocked_md_path),
                "user_choices": choices,
            },
        )

        # 9b.2: blocked.md. Notifier path (T16+) wires Tier 2 (OSC 9
        # + BEL); back-compat path writes Tier 1 only. Same pattern
        # as `auto_dispatch_task` (T11): no defensive wrapping —
        # Notifier exceptions surface to the caller (T19's recovery
        # dispatcher will fail loud rather than silently producing
        # half-written blocked.md state).
        if self.notifier is not None:
            self.notifier.fire_block(  # type: ignore[attr-defined]
                block_type="post_merge_verify_failed",
                phase=3,
                task_id=self.task_id,
                issue_id="post_merge_verify_failed",
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
            )
        else:
            write_blocked(
                self.task_dir, phase=3, task=self.task_id,
                why_blocked=why, required_choice=choices,
                safe_resume_command=resume,
                block_type="post_merge_verify_failed",
            )

        # 9b.3: progress.md → blocked_post_merge (T20 enforces enum).
        self._update_task_status("blocked_post_merge")

        # 9b.4: preserve verification worktree at verify/aborted/.
        # Atomicity note: `vpath.rename(aborted)` is atomic on the
        # same filesystem, but fails if `aborted` already exists. A
        # rerun after operator chose abort_run on a previous failure
        # will hit this. We append a microsecond timestamp suffix to
        # produce a unique target — overwriting would erase prior
        # diagnostic state; raising would convert recoverable
        # observation into a crash mid-9b.
        vpath = self._verification_path()
        aborted_root = vpath.parent / "aborted"
        try:
            aborted_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(
                f"WARN: failed to create {aborted_root} "
                f"({type(e).__name__}: {e}); verification worktree "
                f"left in place at {vpath}",
                file=sys.stderr,
            )
            return Gate8Result(
                status="blocked_post_merge",
                verification_worktree_id=self.verification_id,
                blocked_md_path=blocked_md_path,
                criteria_results=results,
            )
        target = aborted_root / f"{vpath.name}+failed"
        if target.exists():
            # Unique-suffix collision avoidance.
            ts_suffix = datetime.datetime.now(datetime.UTC).strftime(
                "%Y%m%dT%H%M%S%fZ",
            )
            target = aborted_root / f"{vpath.name}+failed+{ts_suffix}"
        # Use `git worktree move` so the registry at
        # `.git/worktrees/<id>/gitdir` follows the directory — operator
        # post-mortem (`git status`/`git log` inside the preserved dir)
        # would break with a plain filesystem rename.
        repo_root = self._repo_root()
        move = _run_argv_with_pgkill(
            ["git", "-C", str(repo_root),
             "worktree", "move", str(vpath), str(target)],
            cwd=repo_root,
            timeout_sec=_GIT_HEAD_QUERY_TIMEOUT_SEC,
        )
        if move.timed_out or move.spawn_error is not None or move.returncode != 0:
            # Codex round-1 P2: do NOT fall back to ``Path.rename``.
            # That fallback was the original review-pass-1 motivation
            # to switch to ``git worktree move``: a plain rename leaves
            # ``.git/worktrees/<id>/gitdir`` pointing at the OLD path,
            # so the preserved dir is no longer git-usable for post-
            # mortem (operator's ``git status``/``git log`` inside the
            # preserved dir errors out — exactly the broken-worktree
            # state we documented in the deferred T19 recovery design).
            # Leave the verification worktree at its ORIGINAL path:
            # still git-usable, operator can inspect / repair manually,
            # T19's recovery dispatcher routes from the
            # ``post_merge_verify_failed`` event already emitted at 9b.1.
            print(
                f"WARN: git worktree move failed "
                f"({vpath} -> {target}): "
                f"{move.stderr[-500:] or move.spawn_error or 'timeout'}. "
                f"Verification worktree left at original path: {vpath}. "
                f"Operator must inspect / repair manually before next run.",
                file=sys.stderr,
            )

        return Gate8Result(
            status="blocked_post_merge",
            verification_worktree_id=self.verification_id,
            blocked_md_path=blocked_md_path,
            criteria_results=results,
        )

    # ------------------------------------------------------------------
    # progress.md status writer — delegates to the same naïve table
    # writer MergeRunner uses, but Gate8 owns its own Path so it can
    # be invoked without instantiating a MergeRunner. T20 owns
    # status-enum lint + structured-table replacement.
    # ------------------------------------------------------------------

    def _update_task_status(self, status: str) -> None:
        progress_path = self.task_dir / "progress.md"
        marker_prefix = f"<!-- task_status[{self.task_id}]: "
        new_line = f"{marker_prefix}{status} -->"
        if not progress_path.is_file():
            try:
                progress_path.parent.mkdir(parents=True, exist_ok=True)
                progress_path.write_text(new_line + "\n", encoding="utf-8")
            except OSError:
                print(
                    f"WARN: failed to write task_status marker to "
                    f"{progress_path}",
                    file=sys.stderr,
                )
            return
        try:
            text = progress_path.read_text(encoding="utf-8")
        except OSError:
            print(
                f"WARN: failed to read {progress_path} for task_status "
                f"update", file=sys.stderr,
            )
            return
        lines = text.splitlines(keepends=False)
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(marker_prefix):
                lines[i] = new_line
                replaced = True
                break
        if not replaced:
            insert_idx = 0
            for i, line in enumerate(lines):
                if line.strip():
                    insert_idx = i + 1
                    break
            lines.insert(insert_idx, new_line)
        new_text = "\n".join(lines)
        if not new_text.endswith("\n"):
            new_text += "\n"
        try:
            progress_path.write_text(new_text, encoding="utf-8")
        except OSError:
            print(
                f"WARN: failed to write task_status marker to "
                f"{progress_path}", file=sys.stderr,
            )


class MergeQueue:
    """S1 wave-block (§3 / §6 R3): while ANY task in the current run
    has an unresolved ``post_merge_verify_failed`` event, every
    later task MUST halt at ``pending`` (NOT enter gate 7).

    Resolution is detected by T19's recovery dispatcher writing a
    ``post_merge_resolved`` event (introduced in T19); for T15 the
    queue checks only for the FAILED event's presence — operator
    intervention via blocked.md is the unblocking path until T19
    lands.

    Pitfall coverage:
      - M (cross-task pollution): filter by ``run_id``. An older /
        unrelated run's failure cannot halt the current run.
      - L (type vs presence): compare ``rec.get("event")`` by string
        equality only — never substring scan / lower() / split.
      - A (.get falsy): missing fields skip; we never silently treat
        absent as match.
      - D5 (typed except on JSON parse): ``json.JSONDecodeError`` is
        caught; any other exception propagates.
    """

    def __init__(self, *, task_dir: Path, run_id: str):
        self.task_dir = task_dir
        self.run_id = run_id

    def can_proceed(self, *, task_id: str) -> bool:
        path = self.task_dir / "decisions.jsonl"
        if not path.is_file():
            return True
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            # Read failure: fail-CLOSED. We can't prove the queue is
            # clean → halt. Better a stuck queue (visible) than a
            # silent advance past a verify failure (invisible).
            return False
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # Malformed row: skip. Don't crash the queue check on
                # one bad line; corrupted journal is T19's recovery
                # surface.
                continue
            if not isinstance(rec, dict):
                continue
            # M-class: filter by run_id BEFORE checking event kind so
            # an unrelated run's failure never halts us.
            if rec.get("run_id") != self.run_id:
                continue
            # L-class: string-equality, no substring scan.
            if rec.get("event") == EVENT_POST_MERGE_VERIFY_FAILED:
                return False
        return True


def detect_mid_merge_crash(
    task_dir: Path, *, run_id: str, task_id: str,
) -> dict:
    """R3 mid-merge gap detection (Y5 gap-by-gap state machine).

    Tail-scans ``task_dir/decisions.jsonl`` for autonomy events scoped
    to ``(run_id, task_id)`` and returns one of:

      - ``{"state": "merge_completed"}`` — ``merge_applied`` paired with
        either ``task_completed`` or ``post_merge_verify_failed``.
      - ``{"state": "mid_merge_crash", "block_type":
        "atomic_merge_crashed", "choices": [...]}`` — ``merge_started``
        without a paired ``merge_applied``. R3 reconcile choices.
      - ``{"state": "mid_gate8_crash", "block_type":
        "post_merge_verify_in_progress_crash", "choices": [...]}`` —
        ``merge_applied`` without a paired terminal verify event.
      - ``{"state": "none"}`` — no merge events for this (run, task).

    ``mid_merge_crash`` semantic note (code-review round-1 [P1-2]):
    after the P1-1 reorder (R9 HEAD pre-check before
    ``merge_started``), several distinct ``MergeRunner._continue_merge``
    failure paths can still produce the gap signature
    (``merge_started`` without ``merge_applied``):

      * **timeout during git merge** — pgkill fired; on-disk repo state
        is undefined. Genuine mid-merge crash.
      * **spawn_error invoking git merge** — Popen failed; no git ran
        at all. Phantom gap; ``replay_merge_from_diff_hash`` is a no-op.
      * **rc != 0 from git merge** — conflict, hook reject, etc. Replay
        will recur the same error. Conservative-correct (operator must
        unblock manually before retry).
      * **post-merge ``rev-parse HEAD`` failure** — merge HAS APPLIED
        but we couldn't read the new HEAD. Reported here as
        ``mid_merge_crash`` (a slight misclassification — semantically
        ``mid_gate8_crash`` would be closer). Acceptable because
        ``replay_merge_from_diff_hash`` is idempotent: the second
        attempt's ``git merge --abort`` + re-merge resolves to a no-op
        when source == target after the first merge.

    All four paths funnel to the same R3 reconcile choices:
    ``{replay_merge_from_diff_hash, abort_and_revert_partial,
    switch_to_interactive}``. Replay is idempotent in every path
    above, so a false-positive ``mid_merge_crash`` cannot corrupt repo
    state — at worst it surfaces a redundant prompt to the user.

    v0.8.2 may emit a separate ``merge_aborted_pre_apply`` event from
    the spawn_error / post-rev-parse-fail paths to allow finer-grained
    reporting; for v0.8.1 the conservative classification + idempotent
    replay is sufficient.

    Pitfalls covered:
      - **L** (presence vs type): every JSON access uses ``.get(...)``
        with explicit None handling; equality comparisons use the
        constants defined in :mod:`flow_state_writer` and never call
        string methods on the loaded values.
      - **M** (cross-task pollution): ``decisions.jsonl`` is shared at
        the slug task dir level. Filter by ``(run_id, task_id)`` BEFORE
        any kind inspection so events from sibling tasks / runs don't
        skew the verdict.
      - **D2** (typed except): ``json.JSONDecodeError`` is caught
        explicitly so a single garbled line doesn't poison the whole
        scan; ``OSError`` from a missing file is handled by the
        ``is_file()`` guard.
    """
    path = task_dir / "decisions.jsonl"
    events: list[dict] = []
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return {"state": "none"}
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            # Scope filter (M-class): events for a different (run, task)
            # MUST NOT influence our verdict, even if they live in the
            # same shared file.
            if (
                rec.get("run_id") == run_id
                and rec.get("task_id") == task_id
            ):
                events.append(rec)

    # Build kind set with explicit None filtering — `e.get("event")` may
    # return None if the row is a v0.8.0 DecisionRecord (which has no
    # `event` field). L-class: never call string ops on the value, only
    # equality compare with the EVENT_* constants.
    kinds: set[str] = set()
    for e in events:
        ev = e.get("event")
        if isinstance(ev, str):
            kinds.add(ev)

    if EVENT_MERGE_STARTED in kinds and EVENT_MERGE_APPLIED not in kinds:
        return {
            "state": "mid_merge_crash",
            "block_type": "atomic_merge_crashed",
            "choices": [
                "replay_merge_from_diff_hash",
                "abort_and_revert_partial",
                "switch_to_interactive",
            ],
        }
    if (
        EVENT_MERGE_APPLIED in kinds
        and EVENT_TASK_COMPLETED not in kinds
        and EVENT_POST_MERGE_VERIFY_FAILED not in kinds
    ):
        return {
            "state": "mid_gate8_crash",
            "block_type": "post_merge_verify_in_progress_crash",
            "choices": [
                "rerun_post_merge_verify",
                "abort_and_revert_partial",
                "switch_to_interactive",
            ],
        }
    if EVENT_MERGE_APPLIED in kinds:
        return {"state": "merge_completed"}
    return {"state": "none"}


# ----------------------------------------------------------------------
# T19 — CrashRecoveryDispatcher: 5-state pre-dispatch recovery gate.
#
# Per design §6 + §7 + §8.1 + §4 Y4 + §1 row 18 + Q7.2.
#
# State table (consumed by _cmd_auto_execute Step 19.11):
#
#   0. clean → proceed (no boundary marker, no auto-engaged for run/task,
#      and progress.md does not declare `autonomy_mode: auto`).
#   1. pre_lock_crash → fail_closed_interactive — LEGAL silent fallback;
#      only when progress.md declares `autonomy_mode: auto` but no lock,
#      no auto_engaged event for this run/task. User never opted in to
#      this attempt (boundary never crossed). §7 line 312.
#   2. auto_prepare_interrupted → BLOCK (R10). T5's
#      `detect_auto_prepare_state` returns `interrupted_dead_pid` /
#      `interrupted_contract_changed` / `interrupted_host_mismatch` /
#      `interrupted_lock_corrupt` / `interrupted_journal_corrupt` —
#      all share `block_type=auto_prepare_interrupted`.
#   3. auto_engaged_crashed → BLOCK (Q7.2 + §6/§7 contradiction-fix).
#      auto_engaged event present, no terminal event (task_completed /
#      post_merge_verify_failed / aborted_*) and no mid-merge case 4.
#      NEVER silent fallback to interactive — explicit user choice
#      required. Hard rule per §7 line 312.
#   4. mid_merge_crash / mid_gate8_crash → BLOCK (R3). T14's
#      `detect_mid_merge_crash` returns `mid_merge_crash` /
#      `mid_gate8_crash` — different choice sets per design.
#   5. verification_worktree_orphaned → BLOCK (§4 Y4 + §6 line 250).
#      Live `verify/` worktree on disk, no matching
#      `post_merge_verification_completed`. T15 owns lifecycle (create /
#      cleanup-on-pass / preserve-on-fail); T19 owns ORPHAN recovery —
#      orchestrator died WHILE gate 8 was still running.
#
# Pitfall coverage actively defended:
#   K (plausible-justification): every classify branch is explicit; we
#     never wrap detect_auto_prepare_state / detect_mid_merge_crash in
#     try/except (let exceptions propagate to operator review).
#   G/G2 (disk-state authoritative): each state reads decisions.jsonl
#     fresh on every classify(); no in-memory cache.
#   D5 (typed except): jsonl parse uses `except json.JSONDecodeError
#     continue` — never broad except.
#   N (disk-vs-ref identity): mid-merge case uses
#     `target_commit_pre_merge` SHA (not branch ref); verify orphan case
#     keys off `verification_worktree_path` (string path, not ref).
#   F (fail-closed for safety): state 2/3/4/5 ALL block. Only state 1 is
#     a legal silent fallback (user never opted in this attempt).
#   R (frontmatter injection): all user-controlled strings reach
#     blocked.md only via Notifier.fire_block, which delegates to
#     write_blocked's _reject_frontmatter_line_separators validator.
# ----------------------------------------------------------------------


@dataclass
class RecoveryVerdict:
    """Verdict from CrashRecoveryDispatcher.classify(). Caller branches
    on `action`; `state` and `block_type` are operator-facing labels.
    """
    state: str
    action: str                                        # proceed | fail_closed_interactive | block
    block_type: Optional[str] = None
    choices: list = field(default_factory=list)
    blocked_md_path: Optional[Path] = None
    details: dict = field(default_factory=dict)


# Terminal event kinds for state-3 (post-auto_engaged crash) detection.
# `aborted_*` lives in the `decision` field (v0.8.0 DecisionRecord
# convention), not the `event` field — checked separately.
TERMINAL_TASK_EVENTS: tuple[str, ...] = (
    EVENT_TASK_COMPLETED,
    EVENT_POST_MERGE_VERIFY_FAILED,
)


class CrashRecoveryDispatcher:
    """5-state pre-dispatch recovery classifier.

    Single entry point at orchestrator startup: ``classify()`` returns a
    ``RecoveryVerdict`` the caller (``_cmd_auto_execute``) consumes to
    halt-vs-proceed. All blocked verdicts have already written
    blocked.md (via the injected ``notifier``); the caller only needs
    to print the path and exit 3.

    Constructor inputs are all keyword-only — positional arg confusion
    has caused real recovery bugs in prior iterations (K-class trap:
    "I'll just trust the param order").
    """

    def __init__(
        self,
        *,
        task_dir: Path,
        slug: str,
        run_id: str,
        task_id: str,
        current_contract_hash: str,
        repo_root: Path,
        notifier: Optional[object] = None,
    ):
        self.task_dir = task_dir
        self.slug = slug
        self.run_id = run_id
        self.task_id = task_id
        self.current_contract_hash = current_contract_hash
        self.repo_root = repo_root
        self.notifier = notifier

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self) -> RecoveryVerdict:
        """Single classify pass. Order matters — see the state table in
        the module-level T19 docstring. Detection is fail-loud: any
        unexpected exception from T5 / T14 detectors propagates so
        operator review surfaces (K-class: NEVER wrap in try/except just
        because the call site looks scary).
        """
        # ── State 2: lock+dead-pid (T5 boundary marker) ────────────────
        # detect_auto_prepare_state may raise (e.g. unknown OSError from
        # _is_pid_alive). We let that propagate per K-class: silent
        # swallow would mis-classify a real interrupted run as `no_run`.
        lock_state = detect_auto_prepare_state(
            self.task_dir,
            run_id=self.run_id,
            task_id=self.task_id,
            current_contract_hash=self.current_contract_hash,
        )
        ls = lock_state.get("state")
        # All `interrupted_*` synthetic states share block_type=
        # `auto_prepare_interrupted` per T5 design. Route them all here.
        if ls in (
            "interrupted_dead_pid",
            "interrupted_contract_changed",
            "interrupted_host_mismatch",
            "interrupted_lock_corrupt",
            "interrupted_journal_corrupt",
        ):
            return self._block_auto_prepare_interrupted(lock_state)

        # ── orphan_lock_post_engaged: T5 §8.1 "consume + warning" ──────
        # T19 review round 1 [Y2]: detect_auto_prepare_state returns this
        # synthetic state when (lock present AND auto_engaged event also
        # present) — the previous run reached engagement but exited
        # without unlinking the lock. T5 §8.1 contract is explicit:
        # consume the lock + emit warning, NEVER block. Without consume,
        # the stale lock survives the run; the next run sees it again
        # and either re-warns indefinitely or, worse, escalates spuriously
        # if pid/contract/host bookkeeping drifts. Real safety boundary
        # is owned by state 2 (interrupted_*) — those are blocks. Falling
        # through to subsequent state checks is correct: orphan-lock
        # alone does not determine recovery action.
        if ls == "orphan_lock_post_engaged":
            consume = self._consume_stale_lock(lock_state)
            if not consume.get("consumed"):
                # [P2] codex round-1: failed to remove the stale lock
                # (PermissionError / read-only fs / etc.). Fail-closed
                # to state-2 block instead of silently proceeding —
                # otherwise the lock survives indefinitely, every
                # subsequent run silently re-warns + falls through, and
                # any genuine pid/contract/host drift would never be
                # caught (the orphan branch always wins over real
                # interrupted_* in detect_auto_prepare_state).
                synthetic = dict(lock_state)
                synthetic["state"] = "interrupted_dead_pid"
                synthetic["consume_failure_reason"] = consume.get("reason")
                synthetic["consume_failure_exception_type"] = (
                    consume.get("exception_type")
                )
                return self._block_auto_prepare_interrupted(synthetic)
            # fall through — state 4/5/3/1/0 detection still runs.

        # ── State 4: mid-merge / mid-gate-8 crash (T14 detector) ────────
        merge_state = detect_mid_merge_crash(
            self.task_dir, run_id=self.run_id, task_id=self.task_id,
        )
        ms = merge_state.get("state")
        if ms == "mid_merge_crash":
            return self._block_mid_merge_crash(merge_state)
        if ms == "mid_gate8_crash":
            # mid_gate8 may ALSO be a verify-worktree orphan if the
            # verify worktree is still live on disk — check that first
            # so the more specific case wins.
            orphan = self._classify_verification_orphan()
            if orphan is not None:
                return orphan
            return self._block_mid_gate8_crash(merge_state)

        # ── State 5: verification-worktree orphan (Y4) ─────────────────
        # Independent check — meaningful AFTER merge_applied event but
        # the underlying detector (worktree-on-disk + missing-completed)
        # is correct in any state.
        orphan = self._classify_verification_orphan()
        if orphan is not None:
            return orphan

        # ── State 3: post-auto_engaged crash ───────────────────────────
        # auto_engaged event present for run/task with no terminal
        # event AND no mid-merge case caught above.
        if self._has_post_auto_engaged_crash():
            return self._block_post_auto_engaged_crash()

        # ── State 1: pre-lock crash (legal silent fallback) ─────────────
        # progress.md declares autonomy_mode=auto but no boundary marker
        # was ever written. User never opted in to THIS attempt.
        if ls == "no_run" and self._was_auto_intended():
            return RecoveryVerdict(
                state="pre_lock_crash",
                action="fail_closed_interactive",
                details={
                    "reason": (
                        "no auto run boundary written; user never "
                        "opted in this attempt"
                    ),
                },
            )

        # ── State 0: clean → proceed ───────────────────────────────────
        return RecoveryVerdict(state="clean", action="proceed")

    # ------------------------------------------------------------------
    # State 1 helper — progress.md autonomy_mode read.
    # ------------------------------------------------------------------

    def _was_auto_intended(self) -> bool:
        """Read progress.md frontmatter ``autonomy_mode``. Returns True
        only on explicit ``autonomy_mode: auto``. Missing file / parse
        failure → False (fail-closed: do NOT silently classify a
        recoverable state as state 1 just because the pointer file is
        broken — let state 0 / clean handle it).
        """
        progress_path = self.task_dir / "progress.md"
        if not progress_path.is_file():
            return False
        try:
            meta = read_progress_meta(progress_path)
        except Exception:
            # K-class boundary: read_progress_meta is documented as
            # fail-soft (returns default ProgressMeta on parse error),
            # but if it ever raises (corrupt file, IO error), DO NOT
            # silently treat as "auto intended" — that would route to
            # state 1 fail-closed-interactive, which is itself silent.
            # Return False → state 0 clean → proceed; the orchestrator
            # boundary marker write (T5) will subsequently fail with a
            # clearer error.
            return False
        return meta.autonomy_mode == "auto"

    # ------------------------------------------------------------------
    # orphan_lock_post_engaged helper — T5 §8.1 "consume + warning".
    # ------------------------------------------------------------------

    def _consume_stale_lock(self, lock_state: dict) -> dict:
        """T19 round 1 [Y2] + codex round-1 [P2]: T5 §8.1
        consume_with_warning contract, fail-closed on non-race unlink
        failure.

        Returns ``{"consumed": True, ...}`` on success or race-safe
        FileNotFoundError; returns
        ``{"consumed": False, "reason": str, "exception_type": str}``
        on any other ``OSError`` (PermissionError / IsADirectoryError /
        read-only fs / etc.). Caller MUST handle ``consumed=False`` by
        escalating to a state-2 block — silently proceeding with a
        stale lock still on disk is the F-class silent-pass that codex
        round-1 flagged: a stale lock that we can't remove is
        indistinguishable from a real interrupted run from the next
        attempt's perspective, so the safe posture is to block visibly
        rather than ship an invisible pass.

        K-class safety: only ``FileNotFoundError`` (race-safe) and
        ``OSError`` (catchable failure) are matched. Broader except
        would mask real bugs (D5 reverse: silent except defeats
        fail-loud propagation that the rest of the dispatcher depends
        on).
        """
        # Prefer disk-derived path for forensic accuracy; fall back to
        # the conventional location so `lock_path is None` (e.g. exotic
        # fixture) still resolves to the real file we want to unlink.
        ls_path = lock_state.get("lock_path")
        lock_path = (
            Path(ls_path) if isinstance(ls_path, str) and ls_path
            else self.task_dir / AUTO_PREPARE_LOCK_FILENAME
        )
        try:
            lock_path.unlink()
        except FileNotFoundError:
            # Race-safe: another process already cleaned up. Treat as
            # consumed so caller proceeds (the boundary marker is gone
            # from disk regardless of who removed it).
            return {"consumed": True, "note": "lock already gone"}
        except OSError as e:
            # [P2] codex round-1: do NOT silently print + proceed.
            # Caller will fail-closed to state-2 block.
            return {
                "consumed": False,
                "reason": str(e),
                "exception_type": type(e).__name__,
            }
        print(
            f"WARN: consumed orphan auto_prepare.lock from prior run "
            f"(slug={self.slug}, task={self.task_id}); T5 §8.1 contract "
            f"— previous run reached engagement but exited without "
            f"unlocking; recovery proceeds.",
            file=sys.stderr,
        )
        return {"consumed": True}

    # ------------------------------------------------------------------
    # State 2 helper — auto_prepare_interrupted block.
    # ------------------------------------------------------------------

    def _block_auto_prepare_interrupted(
        self, lock_state: dict,
    ) -> RecoveryVerdict:
        """R10 + Y8 partner: write blocked.md (block_type=
        auto_prepare_interrupted) + emit
        ``EVENT_AUTO_PREPARE_INTERRUPTED`` so audit trail records the
        recovery decision.
        """
        choices = [
            "resume_auto_from_prepare",
            "abort_task",
            "switch_to_interactive",
        ]
        # R-class defense: lock_state values come from disk JSON and
        # could in theory carry frontmatter-injection chars. The state
        # name itself is from a fixed allowlist (we route by string
        # equality earlier), but we still sanitize the why_blocked
        # composition by passing the state name through isinstance(str).
        ls_label = lock_state.get("state")
        if not isinstance(ls_label, str):
            ls_label = "unknown"
        why = (
            f"auto preparation interrupted ({ls_label}); "
            f"manual recovery required"
        )
        resume = f"flow orchestrator --resume-auto-prepare {self.slug}"
        blocked_md_path = self.task_dir / "blocked.md"
        if self.notifier is not None:
            blocked_md_path = self.notifier.fire_block(  # type: ignore[attr-defined]
                block_type="auto_prepare_interrupted",
                phase=2,
                task_id=self.task_id,
                issue_id="auto_prepare_interrupted",
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
            )
        else:
            blocked_md_path = write_blocked(
                self.task_dir,
                phase=2,
                task=self.task_id,
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
                block_type="auto_prepare_interrupted",
            )
        # Y8 audit event. lock_path may be missing on synthetic states
        # like interrupted_journal_corrupt (no lock at all). Fall back
        # to the conventional path so the event still validates.
        lock_path = lock_state.get("lock_path") or str(
            self.task_dir / AUTO_PREPARE_LOCK_FILENAME,
        )
        append_autonomy_event(
            self.task_dir,
            EVENT_AUTO_PREPARE_INTERRUPTED,
            {
                "event_id": _new_event_id(),
                "ts": _now_iso(),
                "slug": self.slug,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "lock_path": lock_path,
                "blocked_md_path": str(blocked_md_path),
            },
        )
        return RecoveryVerdict(
            state="auto_prepare_interrupted",
            action="block",
            block_type="auto_prepare_interrupted",
            choices=choices,
            blocked_md_path=blocked_md_path,
            details={"lock_state": ls_label},
        )

    # ------------------------------------------------------------------
    # State 3 helpers — post-auto_engaged crash detection + block.
    # ------------------------------------------------------------------

    def _scan_decisions_jsonl(self) -> list:
        """Read + filter `decisions.jsonl` for records matching this
        run/task. D5 typed except: ONLY json.JSONDecodeError on parse
        skip; OSError on read missing file is handled by is_file()
        guard above; any other exception propagates.
        """
        records: list = []
        path = self.task_dir / "decisions.jsonl"
        if not path.is_file():
            return records
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # D5: skip malformed lines for state-3 / orphan checks.
                # T5 has its own `interrupted_journal_corrupt` path that
                # routes BEFORE we get here; here a malformed line is a
                # forensic artifact, not a recovery signal.
                continue
            if not isinstance(rec, dict):
                continue
            if (
                rec.get("run_id") == self.run_id
                and rec.get("task_id") == self.task_id
            ):
                records.append(rec)
        return records

    def _has_post_auto_engaged_crash(self) -> bool:
        """auto_engaged event present + no terminal event for this
        (run, task). Terminal kinds: TASK_COMPLETED /
        POST_MERGE_VERIFY_FAILED / aborted_* (decision field).
        """
        records = self._scan_decisions_jsonl()
        # Build the `event` set explicitly with isinstance check (L-class:
        # never assume the value is a string just because v0.8.0 producers
        # always wrote one).
        event_kinds: set = set()
        decisions: list = []
        for rec in records:
            ev = rec.get("event")
            if isinstance(ev, str):
                event_kinds.add(ev)
            dec = rec.get("decision")
            if isinstance(dec, str):
                decisions.append(dec)
        if EVENT_AUTO_ENGAGED not in event_kinds:
            return False
        for terminal in TERMINAL_TASK_EVENTS:
            if terminal in event_kinds:
                return False
        # aborted_* lives in the v0.8.0 `decision` field, not `event`.
        if any(d.startswith("aborted_") for d in decisions):
            return False
        return True

    def _block_post_auto_engaged_crash(self) -> RecoveryVerdict:
        """Q7.2 + §6/§7 contradiction-fix: BLOCK + user choice. NEVER
        silent fallback to interactive. Hard rule per §7 line 312.
        """
        choices = [
            "resume_from_last_safe_state",
            "abort_task",
            "switch_to_interactive",
        ]
        why = (
            "auto run interrupted post-engagement; "
            "explicit user choice required"
        )
        resume = f"flow orchestrator --resume {self.slug}"
        blocked_md_path = self.task_dir / "blocked.md"
        if self.notifier is not None:
            blocked_md_path = self.notifier.fire_block(  # type: ignore[attr-defined]
                block_type="auto_engaged_crashed",
                phase=2,
                task_id=self.task_id,
                issue_id="auto_engaged_crashed",
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
            )
        else:
            blocked_md_path = write_blocked(
                self.task_dir,
                phase=2,
                task=self.task_id,
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
                block_type="auto_engaged_crashed",
            )
        return RecoveryVerdict(
            state="auto_engaged_crashed",
            action="block",
            block_type="auto_engaged_crashed",
            choices=choices,
            blocked_md_path=blocked_md_path,
        )

    # ------------------------------------------------------------------
    # State 4 helpers — mid-merge + mid-gate8 block.
    # ------------------------------------------------------------------

    def _block_mid_merge_crash(self, merge_state: dict) -> RecoveryVerdict:
        choices = list(merge_state.get("choices") or [])
        why = (
            "git merge transaction crashed between "
            "merge_started and merge_applied"
        )
        resume = f"flow orchestrator --resume {self.slug}"
        blocked_md_path = self.task_dir / "blocked.md"
        if self.notifier is not None:
            blocked_md_path = self.notifier.fire_block(  # type: ignore[attr-defined]
                block_type="atomic_merge_crashed",
                phase=2,
                task_id=self.task_id,
                issue_id="atomic_merge_crashed",
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
            )
        else:
            blocked_md_path = write_blocked(
                self.task_dir,
                phase=2,
                task=self.task_id,
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
                block_type="atomic_merge_crashed",
            )
        return RecoveryVerdict(
            state="mid_merge_crash",
            action="block",
            block_type="atomic_merge_crashed",
            choices=choices,
            blocked_md_path=blocked_md_path,
        )

    def _block_mid_gate8_crash(self, merge_state: dict) -> RecoveryVerdict:
        choices = list(merge_state.get("choices") or [
            "rerun_post_merge_verify",
            "abort_and_revert_partial",
            "switch_to_interactive",
        ])
        why = (
            "post-merge verification was running when orchestrator "
            "crashed; user must choose rerun / revert / interactive"
        )
        resume = f"flow orchestrator --resume {self.slug}"
        blocked_md_path = self.task_dir / "blocked.md"
        if self.notifier is not None:
            blocked_md_path = self.notifier.fire_block(  # type: ignore[attr-defined]
                block_type="post_merge_verify_in_progress_crash",
                phase=3,
                task_id=self.task_id,
                issue_id="post_merge_verify_in_progress_crash",
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
            )
        else:
            blocked_md_path = write_blocked(
                self.task_dir,
                phase=3,
                task=self.task_id,
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
                block_type="post_merge_verify_in_progress_crash",
            )
        return RecoveryVerdict(
            state="mid_gate8_crash",
            action="block",
            block_type="post_merge_verify_in_progress_crash",
            choices=choices,
            blocked_md_path=blocked_md_path,
        )

    # ------------------------------------------------------------------
    # State 5 helper — verification-worktree orphan.
    # ------------------------------------------------------------------

    def _classify_verification_orphan(self) -> Optional[RecoveryVerdict]:
        """§4 Y4 + §6 line 250: live verify worktree path + no matching
        post_merge_verification_completed event for this (run, task).
        Returns RecoveryVerdict on orphan, None when no orphan is
        detected (state 5 didn't fire).

        Orchestrator pid liveness is already covered by state 2 (T5's
        lock-based check). Here we trust that if we got past state 2
        without a lock, the orchestrator that emitted the
        post_merge_verification_started event is dead — otherwise it
        would be holding the lock or have written the completed event.
        """
        records = self._scan_decisions_jsonl()
        started: list = []
        completed_ids: set = set()
        for rec in records:
            ev = rec.get("event")
            if ev == EVENT_POST_MERGE_VERIFICATION_STARTED:
                started.append(rec)
            elif ev == EVENT_POST_MERGE_VERIFICATION_COMPLETED:
                # [P2 codex round-1] Pair completed events to their
                # started event by `verification_worktree_id`. Previous
                # logic used `not started or completed:` which masked
                # an orphaned LATER verify pass whenever ANY earlier
                # verify pass had completed in the same journal. That
                # made retry / re-verify scenarios silently bypass
                # state 5.
                #
                # L-class type-check: only accept str ids before set
                # membership; a non-string id can't be paired and is
                # treated as malformed (the started event with a
                # malformed id will return None via the path check
                # below, which is the conservative outcome — never
                # silently mis-classify as orphan).
                wid = rec.get("verification_worktree_id")
                if isinstance(wid, str) and wid:
                    completed_ids.add(wid)
        if not started:
            # Never started for this (run, task) — not an orphan.
            return None

        # Latest started event is the operative one; older starteds are
        # superseded if they happen to exist (they shouldn't on a clean
        # 9-step sequence, but the scan is forensic-minded).
        latest = started[-1]
        latest_id = latest.get("verification_worktree_id")
        if not isinstance(latest_id, str) or not latest_id:
            # Malformed event — no id to pair. Conservative: return
            # None (don't mis-classify as orphan); corrupt event will
            # surface elsewhere.
            return None
        if latest_id in completed_ids:
            # Latest started has matching completed → not orphaned.
            return None
        path_str = latest.get("verification_worktree_path")
        if not isinstance(path_str, str) or not path_str:
            # Malformed event — no path to check. Return None (don't
            # silently mis-classify as orphan); the corrupt event will
            # surface elsewhere.
            return None
        verify_path = Path(path_str)
        if not verify_path.is_dir():
            # Worktree already cleaned up — not an orphan.
            return None

        # Live worktree on disk + no completion event → orphan.
        choices = [
            "rerun_post_merge_verify",
            "accept_merge_anyway",
            "revert_merge",
            "switch_to_interactive",
        ]
        # R-class defense: verify_path is read from disk JSON and could
        # carry control chars. We pass via isinstance(str) above; the
        # composed `why` then flows through `write_blocked`, which
        # T19 round-1 hardened to run `_reject_frontmatter_line_separators`
        # on `why_blocked` itself (Y1 fix: previously only `block_type`
        # and `frontmatter_extra` str values went through the helper, so
        # a CR in `verification_worktree_path` would have forged a
        # frontmatter row. Now any forged char raises ValueError before
        # the disk write). Truncate to a reasonable length for
        # blocked.md readability.
        short_path = path_str if len(path_str) <= 200 else (
            path_str[:200] + "…"
        )
        why = (
            f"verification worktree {short_path} alive but no "
            f"post_merge_verification_completed event for "
            f"run={self.run_id} task={self.task_id}"
        )
        resume = f"flow orchestrator --resume {self.slug}"
        blocked_md_path = self.task_dir / "blocked.md"
        if self.notifier is not None:
            blocked_md_path = self.notifier.fire_block(  # type: ignore[attr-defined]
                block_type="verification_worktree_orphaned",
                phase=3,
                task_id=self.task_id,
                issue_id="verification_worktree_orphaned",
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
            )
        else:
            blocked_md_path = write_blocked(
                self.task_dir,
                phase=3,
                task=self.task_id,
                why_blocked=why,
                required_choice=choices,
                safe_resume_command=resume,
                block_type="verification_worktree_orphaned",
            )
        return RecoveryVerdict(
            state="verification_worktree_orphaned",
            action="block",
            block_type="verification_worktree_orphaned",
            choices=choices,
            blocked_md_path=blocked_md_path,
            details={"verification_worktree_path": path_str},
        )


# ----------------------------------------------------------------------
# T19 — _cmd_auto_execute end-to-end dispatch loop helpers.
#
# Per Step 19.11 design: replaces v0.8.0 exit-2 stub with the full
# loop (recovery → dispatch → gates 1-6 → merge → gate 8). Single
# Notifier created at entry threaded through every component.
# ----------------------------------------------------------------------


def _resolve_gate_commands(contract: Contract) -> dict:
    """Source baseline / smoke / codex / merge_strategy commands.

    Honors a forward-compat ``gates`` dict on the contract raw (added
    in v0.8.2 per PRD §3 Future); falls back to v0.8.0 repo-convention
    defaults. v0.8.1 schema validator silently accepts the field as
    forward-compat — see flow_contract.parse_contract `unknown_fields`.
    """
    raw = getattr(contract, "_raw_gates", None) or {}
    return {
        "baseline": raw.get("baseline_command", "bash tests/smoke/run.sh"),
        "smoke": raw.get("smoke_command", "bash tests/smoke/run.sh"),
        "codex": raw.get("codex_command", "codex review --diff-only --json"),
        "merge_strategy": raw.get("merge_strategy", "--ff-only"),
    }


def _resolve_integration_target(contract: Contract) -> str:
    """Forward-compat field. v0.8.1 default = "master". v0.8.2 may
    promote to required-validated; the schema validator silently
    accepts the field today.
    """
    return getattr(contract, "integration_target", None) or "master"


def _task_already_completed(
    task_dir: Path, *, run_id: str, task_id: str,
) -> bool:
    """[P1] T19 codex round-1: scan ``decisions.jsonl`` for a terminal
    ``task_completed`` event matching this ``(run_id, task_id)`` pair.

    Why: ``_resolve_or_create_run_id`` reuses the most-recent run_id from
    decisions.jsonl, which is correct — we don't want to mint a fresh
    run_id every time the operator pokes ``--auto-execute``. But
    ``CrashRecoveryDispatcher.classify`` only routes CRASH states; a
    journal that already contains a clean
    ``auto_engaged + merge_applied + post_merge_verification_completed
    + task_completed`` sequence is not a crash, so classify returns
    ``clean / proceed``. Without this skip, the manifest loop then
    re-dispatches the already-merged task — re-creating the worktree,
    re-running the subagent, re-merging.

    Scope (P-class): filter strictly by ``(run_id, task_id)`` BEFORE
    matching the event field. A task_completed event for a different
    run or a different task in the same journal MUST NOT mark this
    pair as completed.

    Type-check (L-class): isinstance(str) on ``event`` field before
    equality, mirroring the dispatcher's defensive read pattern.

    Failure semantics (D5): only ``json.JSONDecodeError`` is caught
    on per-line parse; other exceptions propagate. Missing journal
    file → return False (fail-open here is correct: a missing journal
    means no record of completion, so the dispatcher still needs to
    run; if there was a real prior run, state-1 / state-2 / state-3
    paths in the dispatcher will catch it before this skip is even
    relevant).

    [P2] T19 codex round-2 (fail-closed on journal corruption):
    if ANY line in decisions.jsonl is malformed (JSONDecodeError) OR
    decodes to a non-dict (e.g. array literal), return False
    immediately so the CrashRecoveryDispatcher takes over. T5's
    ``detect_auto_prepare_state`` raises ``JournalCorruptError`` on
    malformed lines (flow_state_writer.py JournalCorruptError →
    ``interrupted_journal_corrupt`` state-2 block); silently skipping
    malformed lines here would let a journal with a valid
    task_completed plus a later truncated line bypass that stricter
    fail-closed safety boundary. We also do NOT early-return on the
    first task_completed match — full journal scan is required to
    detect any later malformed line. This trades a tiny bit of CPU
    for the guarantee that any corruption forces dispatcher routing.

    NOTE: ``post_merge_verify_failed`` and ``aborted_*`` are NOT
    "completed" — those land in CrashRecoveryDispatcher's state-3
    path (terminal-but-failed). This helper only matches the explicit
    success-completion event.
    """
    path = task_dir / "decisions.jsonl"
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Journal unreadable → don't claim completed. The dispatcher's
        # state checks will surface the real issue (e.g.
        # interrupted_journal_corrupt).
        return False

    completed = False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # [P2 codex round-2] fail-closed on journal corruption:
            # any malformed line forces the dispatcher to handle this
            # task. T5's interrupted_journal_corrupt path is the
            # safety boundary; do NOT silently skip.
            return False
        if not isinstance(rec, dict):
            # [P2 codex round-2] same fail-closed posture for non-dict
            # records (e.g. an array literal sneaks in): force
            # dispatcher routing.
            return False
        if rec.get("run_id") != run_id or rec.get("task_id") != task_id:
            continue
        ev = rec.get("event")
        if isinstance(ev, str) and ev == EVENT_TASK_COMPLETED:
            completed = True
            # NOTE: do NOT early-return; finish scanning to ensure no
            # malformed lines lurk later in the journal. See P2 above.
    return completed


def _resolve_or_create_run_id(task_dir: Path) -> str:
    """Resolve the current auto-run id from `decisions.jsonl` or mint
    a fresh one. The run boundary writer (T6) appends a `run_started`
    decision when an auto run begins; until that lands we MUST mint
    locally so the dispatch loop has a stable id to thread through
    every component's keyword args.

    Strategy: scan `decisions.jsonl` for the most-recent record with a
    `run_id` field. If present, reuse; else mint `auto-<utc-iso-z>`
    (collision-resistant for a single-orchestrator-per-task contract).
    """
    path = task_dir / "decisions.jsonl"
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for raw_line in reversed(text.splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            rid = rec.get("run_id")
            if isinstance(rid, str) and rid:
                return rid
    # Mint fresh: ISO ts + a small uuid suffix so two orchestrators on
    # the same machine that start in the same second don't collide.
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"auto-{ts}-{_new_event_id()[:6]}"


def _load_prior_baseline(task_dir: Path, task_id: str) -> Optional[BaselineRecord]:
    """Read any prior `BaselineRecord` from `acceptance-progress.jsonl`
    (T4 writer). Returns None if no prior record exists for this task —
    F-class fail-closed treats that as "any current failure is newly
    broken" per gate 1's design.

    v0.8.1: BaselineRecord is just a `failing: list[str]` carrier. We
    materialize it from the most recent record whose `task_id` matches
    and whose record-kind is `baseline`. If the schema lacks `task_id`
    or `kind`, we conservatively return None.
    """
    path = task_dir / "acceptance-progress.jsonl"
    if not path.is_file():
        return None
    failing: Optional[list] = None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if (
            rec.get("task_id") == task_id
            and rec.get("kind") == "baseline"
        ):
            f = rec.get("failing")
            if isinstance(f, list):
                failing = list(f)
                break
    if failing is None:
        return None
    return BaselineRecord(failing=failing)


def _resolve_afk_monitor(contract: Contract, start_iso: str):
    """v0.8.2 T2 — instantiate AfkMonitor from a Contract.

    Reads ``afk_on_timeout`` (default ``"wait"``) and
    ``afk_timeout_min`` (default 30 min if absent) from the parsed
    contract; constructs an ``AfkMonitor`` with a fresh
    ``PausedClock`` rooted at ``start_iso``.

    Imported lazily so this module's import surface stays unchanged
    for callers that do not need AFK (orchestrator dry-run, doctor,
    etc.). T3 will call this from the Phase 2 retry loop alongside
    budget enforcement; T2 ships it dormant (helper exists, not yet
    consumed inside the live dispatch loop).

    I-class guard: each call constructs a NEW monitor — there is no
    cross-task state leakage. The clock starts at the supplied
    ``start_iso`` (caller passes a fresh per-task timestamp).
    """
    from common.afk_monitor import (  # type: ignore
        DEFAULT_IDLE_SECONDS_THRESHOLD,
        AfkMonitor,
    )
    mode = contract.afk_on_timeout if contract.afk_on_timeout else "wait"
    if contract.afk_timeout_min is not None:
        idle_seconds = float(contract.afk_timeout_min) * 60.0
    else:
        idle_seconds = DEFAULT_IDLE_SECONDS_THRESHOLD
    return AfkMonitor(
        start_iso=start_iso,
        mode=mode,
        idle_seconds_threshold=idle_seconds,
    )


# ────────────────────────────────────────────────────────────────────────
# v0.8.2 T3 — Phase 2 retry-on-non-pass loop + dual-counter invariants
# ────────────────────────────────────────────────────────────────────────
#
# Wraps the implementer + codex review pair in a retry loop with TWO
# independent round caps:
#
#   - max_dispatch_retry_rounds (default 3): caps implementer-retry
#     loops. Advanced ONLY by review verdict ``fail``.
#   - max_codex_review_rounds (default 2): caps codex review rounds.
#     Advanced ONLY by review verdict ``rejected_with_rationale`` (RWR).
#
# Five terminal outcomes (PRD §R3 + ADR-1 invariant 4 — single
# HardStopSnapshot shape across all reasons):
#
#   - "pass"           — review verdict GREEN; no snapshot.
#   - "budget_hit"     — any of 5 T1 budget counters tripped 100%.
#   - "retry_cap"      — dispatch_retry_rounds >= max.
#   - "review_cap"     — codex_review_rounds >= max.
#   - "afk_hard_cap"   — T2 AFK 24h hard cap or abort-mode timeout.
#
# Five dual-counter invariants (PRD §R2):
#   1. dispatch_retry_rounds caps implementer loops only.
#   2. codex_review_rounds is independent (RWR consumes it, NOT retry).
#   3. Budget counters cap EVERYTHING (round counters can't outpace).
#   4. All terminal paths share the HardStopSnapshot v1 shape.
#   5. Every loop iteration advances EXACTLY ONE counter or terminates
#      (J-class chained-paper-cut guard — no path is allowed to leave
#      both counters static while continuing).
#
# Determinism: ``now_iso_fn`` is injected. No real wallclock reads, no
# ``time.sleep`` in the loop. Tests inject deterministic step-by-step
# clocks; production callers pass ``afk_monitor.now_iso_utc``.
#
# I-class guard: counters are PER-DISPATCH-SESSION. ``RetrySessionState``
# defaults dispatch_retry_rounds + codex_review_rounds to 0 on
# construction. There is NO module-level state. Each dispatch builds a
# fresh state.


_BLINDSPOT_INDEX_LINE_RE = re.compile(
    # Lines that look like a blindspot-class trigger header:
    #   "A. ..." | "Class A: ..." | "[A] ..." | "(A) ..."
    # Letters A-T (the 18-class registry). Match at line start.
    r"^\s*(?:"
    r"[A-T]\.\s"           # "A. ..."
    r"|Class\s+[A-T]\s*:"  # "Class A: ..."
    r"|\[[A-T]\]\s"        # "[A] ..."
    r"|\([A-T]\)\s"        # "(A) ..."
    r")",
    re.MULTILINE,
)


def redact_blindspot_index(reviewer_findings: str) -> str:
    """Strip 18-class blindspot trigger lines; preserve specific findings.

    The reviewer feedback is included as a prompt prefix when an
    implementer round retries. Per PRD §R3 "reviewer transparency rule":
    we transfer the SPECIFIC findings (line refs / file refs / exact
    issues) but NOT the trigger checklist headers (which would let the
    implementer cargo-cult the categorisation rather than fix the
    issue).

    Lines matching ``A. ``, ``Class A:``, ``[A] ``, ``(A) `` (letters
    A-T) are dropped. Everything else passes through verbatim.
    """
    if not reviewer_findings:
        return reviewer_findings
    # Split on \n and filter; this preserves trailing newline behaviour
    # by joining + re-adding a trailing newline only if the input had
    # one. Avoids corner-case "" input -> "\n" output.
    lines = reviewer_findings.splitlines()
    kept = [ln for ln in lines if not _BLINDSPOT_INDEX_LINE_RE.match(ln + "\n")]
    out = "\n".join(kept)
    if reviewer_findings.endswith("\n"):
        out += "\n"
    return out


@dataclass(frozen=True)
class RetryConfig:
    """Per-dispatch-session retry caps.

    Both caps are validated >= 1 on construction; 0 / negative is a
    config bug (would cause immediate cap-hit on iteration 1, masking
    any actual progress).
    """
    max_dispatch_retry_rounds: int = 3
    max_codex_review_rounds: int = 2

    def __post_init__(self) -> None:
        if self.max_dispatch_retry_rounds < 1:
            raise ValueError(
                f"max_dispatch_retry_rounds must be >= 1, got "
                f"{self.max_dispatch_retry_rounds!r}"
            )
        if self.max_codex_review_rounds < 1:
            raise ValueError(
                f"max_codex_review_rounds must be >= 1, got "
                f"{self.max_codex_review_rounds!r}"
            )


@dataclass
class RetrySessionState:
    """Per-dispatch-session state.

    I-class guard: a fresh instance is required per dispatch. Counters
    do NOT persist across dispatches even within the same task.
    """
    task_slug: str
    dispatch_retry_rounds: int = 0
    codex_review_rounds: int = 0
    progress_path: Optional[Path] = None
    # Cached reviewer feedback from the previous round; consumed by the
    # next implementer dispatch as a prompt prefix (transparency rule).
    last_reviewer_feedback: Optional[str] = None


@dataclass
class RetryDeps:
    """Injection seam for the retry loop.

    Both callables are passed the current state + the redacted
    reviewer feedback (None on round 1). The implementer returns a dict
    of counter-deltas (keys: any of the 5 T1 counter names; ``model_id``
    + ``pricing_version`` for cost_usd). The reviewer returns one of
    ``"pass"`` / ``"fail"`` / ``"rejected_with_rationale"``; any other
    value raises ValueError (PRD R3 invariant 5 — no silent re-loop).
    """
    run_implementer_round: Callable[..., dict]
    run_codex_review: Callable[..., str]


_VALID_REVIEW_OUTCOMES = frozenset({"pass", "fail", "rejected_with_rationale"})


def _build_budget_snapshot(
    counter,  # BudgetCounter
    task_slug: str,
    now_iso: str,
) -> "HardStopSnapshot":
    """Build a HardStopSnapshot for a tripped budget counter.

    cost_usd carries pricing metadata in ``extra``; other counters
    leave ``extra`` empty. Reuses the v1 schema from T1 — no schema
    bump.
    """
    from common.snapshot import HardStopSnapshot  # type: ignore
    extra: dict = {}
    if counter.name == "cost_usd":
        if counter.model_id is not None:
            extra["model_id"] = counter.model_id
        if counter.pricing_version is not None:
            extra["pricing_version"] = counter.pricing_version
    return HardStopSnapshot(
        reason="budget_hit",
        counter_name=counter.name,
        value=float(counter.value),
        limit=float(counter.limit),
        hit_at_iso=now_iso,
        estimated=bool(counter.estimated),
        extra=extra,
        task_slug=task_slug,
    )


def _build_round_cap_snapshot(
    reason: str,
    task_slug: str,
    now_iso: str,
    *,
    extra: Optional[dict] = None,
) -> "HardStopSnapshot":
    """Build a HardStopSnapshot for a retry-cap or review-cap event.

    ``reason`` must be one of "retry_cap" | "codex_review_cap" (the
    valid HardStopSnapshot enum values for round-cap reasons).
    """
    from common.snapshot import HardStopSnapshot  # type: ignore
    return HardStopSnapshot(
        reason=reason,
        counter_name=None,
        value=None,
        limit=None,
        hit_at_iso=now_iso,
        estimated=False,
        extra=dict(extra or {}),
        task_slug=task_slug,
    )


def _progress_log_round(
    progress_path: Optional[Path],
    round_num: int,
    agent_role: str,
    counter_delta_dict: dict,
) -> None:
    """Append one row to the ``## Execute Log`` table in progress.md.

    Idempotent on missing-file / missing-section: returns silently
    without crashing. Rows are appended (G-class atomic via tmp +
    os.replace).
    """
    if progress_path is None:
        return
    try:
        text = progress_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return
    # Locate the ## Execute Log section. If absent, no-op.
    marker = "## Execute Log"
    if marker not in text:
        return
    # Format counter deltas compactly for the table cell.
    pairs = sorted(
        (k, v) for k, v in counter_delta_dict.items()
        if isinstance(v, (int, float))
    )
    delta_str = ", ".join(f"{k}={v}" for k, v in pairs) or "-"
    row = f"| {round_num} | {agent_role} | {delta_str} |\n"
    new_text = text.rstrip("\n") + "\n" + row
    tmp = progress_path.parent / (progress_path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, progress_path)


def _apply_impl_deltas_to_budget(
    budget: dict, deltas: dict, *, estimated: bool = False,
) -> None:
    """Apply impl-round counter deltas to the T1 budget dict.

    Keys recognised: any of the 5 counter names. ``model_id`` +
    ``pricing_version`` (for cost_usd) are ALSO recognised but consumed
    rather than treated as counter names.
    """
    model_id = deltas.get("model_id")
    pricing_version = deltas.get("pricing_version")
    for name, counter in budget.items():
        amt = deltas.get(name)
        if amt is None:
            continue
        if name == "cost_usd":
            counter.add(
                float(amt),
                estimated=estimated,
                model_id=model_id,
                pricing_version=pricing_version,
            )
        else:
            counter.add(float(amt), estimated=estimated)


def _first_tripped_counter(budget: dict):
    """Return the first counter that is_hit, or None."""
    for counter in budget.values():
        if counter.is_hit():
            return counter
    return None


def dispatch_with_retry(
    *,
    state: RetrySessionState,
    config: RetryConfig,
    budget: dict,
    afk,  # AfkMonitor
    deps: RetryDeps,
    now_iso_fn: Callable[[], str],
) -> Tuple[str, Optional["HardStopSnapshot"]]:
    """Phase 2 retry-on-non-pass loop entrypoint.

    Returns ``(outcome, snapshot)`` where:

      - outcome ∈ {"pass", "budget_hit", "retry_cap", "review_cap",
        "afk_hard_cap"}
      - snapshot is a ``HardStopSnapshot`` for any non-pass outcome,
        or ``None`` for "pass".

    State machine (B-class):

      [start] -> pre-tick (AFK + budget + round caps)
              -> implementer round (advances 0 counters; updates budget)
              -> review round
                   pass -> [terminate(pass)]
                   fail -> dispatch_retry_rounds += 1; loop
                   rejected_with_rationale -> codex_review_rounds += 1; loop
                   <other> -> ValueError (J-class guard)

    Pause/resume bracketing (B-class): codex review wait time does NOT
    tick AFK — we ``afk.pause("codex_review")`` before calling
    ``run_codex_review`` and ``afk.resume`` after.
    """
    while True:
        # ── Pre-tick: AFK first (it can override "wait" mode) ─────
        now = now_iso_fn()
        afk_state = afk.evaluate(now)
        if afk_state == "hard_cap":
            snap = afk.to_snapshot(state.task_slug, now, reason="hard_cap")
            return "afk_hard_cap", snap
        if afk_state == "timeout":
            # mode='abort' -> snapshot; mode='wait' -> None (parked).
            # In production wait-mode parking, the caller stays in this
            # function awaiting the next activity tick. For the retry-
            # loop deterministic path: return abort snapshot if we got
            # one; otherwise treat as no-op continue.
            snap = afk.to_snapshot(
                state.task_slug, now, reason="timeout"
            )
            if snap is not None:
                return "afk_hard_cap", snap

        # ── Pre-tick: budget gates (any tripped -> terminal) ──────
        tripped = _first_tripped_counter(budget)
        if tripped is not None:
            now = now_iso_fn()
            tripped.mark_hit(now)
            snap = _build_budget_snapshot(tripped, state.task_slug, now)
            return "budget_hit", snap

        # ── Pre-tick: round caps ──────────────────────────────────
        if state.dispatch_retry_rounds >= config.max_dispatch_retry_rounds:
            now = now_iso_fn()
            snap = _build_round_cap_snapshot(
                "retry_cap", state.task_slug, now,
                extra={"max": config.max_dispatch_retry_rounds},
            )
            return "retry_cap", snap
        if state.codex_review_rounds >= config.max_codex_review_rounds:
            now = now_iso_fn()
            snap = _build_round_cap_snapshot(
                "codex_review_cap", state.task_slug, now,
                extra={"max": config.max_codex_review_rounds},
            )
            return "review_cap", snap

        # ── Implementer round ─────────────────────────────────────
        # Round number = retry_rounds + 1 on round 1; visual.
        round_num = state.dispatch_retry_rounds + 1
        prefix = redact_blindspot_index(
            state.last_reviewer_feedback or ""
        )
        impl_deltas = deps.run_implementer_round(
            state=state, prompt_prefix=prefix,
        )
        # T1 budget bookkeeping: register the dispatch + apply deltas.
        try:
            from common import budget_counter as _bc  # type: ignore
            _bc.register_dispatch(budget)
        except Exception:
            # If budget shape is unusual (test fakes), skip silently.
            pass
        _apply_impl_deltas_to_budget(budget, impl_deltas or {})
        # AFK heartbeat after each implementer round.
        afk.note_subagent_heartbeat(now_iso_fn())
        _progress_log_round(
            state.progress_path, round_num,
            "implementer", impl_deltas or {},
        )

        # ── Recheck budget after impl deltas applied ──────────────
        tripped = _first_tripped_counter(budget)
        if tripped is not None:
            now = now_iso_fn()
            tripped.mark_hit(now)
            snap = _build_budget_snapshot(tripped, state.task_slug, now)
            return "budget_hit", snap

        # ── Review round (paused-clock bracket around the wait) ──
        afk.pause("codex_review", now_iso_fn())
        try:
            review_outcome = deps.run_codex_review(
                state=state, impl_deltas=impl_deltas,
            )
        finally:
            afk.resume(now_iso_fn())
        _progress_log_round(
            state.progress_path, round_num,
            f"reviewer({review_outcome})",
            {},
        )

        # ── State transition (J-class invariant 5) ────────────────
        if review_outcome == "pass":
            return "pass", None
        if review_outcome == "fail":
            state.dispatch_retry_rounds += 1
            # Cache the feedback for the next implementer round.
            # Fakes don't return findings; production wiring will.
            state.last_reviewer_feedback = state.last_reviewer_feedback or ""
            continue
        if review_outcome == "rejected_with_rationale":
            state.codex_review_rounds += 1
            continue
        # No path is allowed to silently re-loop with both counters
        # static. Any unrecognised value is a config / wiring bug —
        # raise loudly (J-class).
        raise ValueError(
            f"unexpected review_outcome: {review_outcome!r} "
            f"(allowed: {sorted(_VALID_REVIEW_OUTCOMES)})"
        )


def _invoke_subagent_dispatch(ctx: WorktreeContext, **kw) -> None:
    """Subagent dispatch shim. T22 owns the SKILL implementation that
    actually invokes Claude CLI via the configured capability.

    T19 defines the import boundary so the dispatch loop is testable
    with a fake_dispatch fixture. Production callable resolved lazily
    from `claude/capabilities/defaults.json::autonomy_orchestrator`.

    K-class trap defended: ImportError MUST raise RuntimeError with a
    clear pointer (NOT silently no-op). Silent no-op would let the
    dispatch loop reach `derive_task_facts` immediately after worktree
    creation, see an empty diff, and route every task to a phantom
    pass — invisible in CI until production. Loud RuntimeError is the
    correct fail-closed posture.
    """
    from importlib import import_module
    try:
        mod = import_module("flow_subagent_dispatch")  # T22 module
    except ImportError as e:
        raise RuntimeError(
            "subagent dispatch capability not wired — set "
            "FLOW_SUBAGENT_DISPATCH_CMD env var or implement "
            "scripts/flow_subagent_dispatch.py via T22 SKILL"
        ) from e
    mod.invoke(ctx, **kw)


# ────────────────────────────────────────────────────────────────────────
# v0.8.2 T3.1 — production wire-up of Phase 2 dispatch through the retry
# loop (`dispatch_with_retry`). T3 shipped the loop as a tested
# abstraction; T3.1 routes the live `_cmd_auto_execute` entrypoint
# through it so the legacy fail-fast `GateRunner.run_phase2` call is no
# longer reachable from production.
#
# Design (D-class, J-class):
#
#   * `_phase2_dispatch` is the SOLE Phase 2 entrypoint from
#     `_cmd_auto_execute`. It builds fresh-per-invocation budget +
#     AfkMonitor + RetrySessionState (I-class: no cross-task counter
#     leak), constructs `RetryDeps` either from the prod adapter or
#     from the test seam (`deps_factory`), and translates the 5 retry
#     outcomes to the existing return contract: pass→rc=0, all others
#     →rc=3 with HardStopSnapshot persisted to disk.
#
#   * Snapshot path: `.flow/tasks/<slug>/hard-stop.json` (overwrite;
#     latest wins). Atomic write via `common.snapshot.write` (G-class).
#
#   * Backwards-compat: contract has no `phase2.retry` block in the
#     v0.8.1 schema, so we always use safe defaults (3 dispatch retry
#     rounds, 2 codex review rounds). v0.8.2+ may extend Contract to
#     surface these — until then, the constants live with `RetryConfig`.
#
#   * Test seam: `deps_factory` is the documented hook for
#     `tests/smoke/test_phase2_retry_loop.py::test_cmd_auto_execute_uses_retry_loop`.
#     If None, we build prod deps from the gate runner. Tests inject a
#     callable returning a `(RetryDeps, sentinel_dict)` to spy the
#     retry-loop wiring without touching `dispatch_with_retry` itself.
#
# I-class: every call to `_phase2_dispatch` constructs a NEW
# RetrySessionState + budget + AfkMonitor. No module-level retain of
# any of these.
#
# J-class: state.task_slug, contract, task_dir, run_id, task_id all
# flow through to both `dispatch_with_retry` and the prod review
# adapter without losing fields.

def _phase2_dispatch(
    *,
    slug: str,
    task_dir: Path,
    contract: Contract,
    manifest,
    facts,
    ctx,
    criteria: list,
    gate_cmds: dict,
    run_id: str,
    task_id: str,
    notifier: "Notifier",
    deps_factory: Optional[Callable[..., RetryDeps]] = None,
) -> int:
    """Phase 2 production wire-up. Returns rc (0 = pass, 3 = blocked).

    On non-pass terminals, writes the HardStopSnapshot to
    ``.flow/tasks/<slug>/hard-stop.json`` (atomic) and fires a
    structured block via ``notifier`` before returning 3.
    """
    from common import snapshot as _snap  # type: ignore
    from common.afk_monitor import now_iso_utc  # type: ignore
    from common import budget_counter as _bc  # type: ignore

    # ── Fresh-per-invocation state (I-class) ────────────────────────
    start_iso = now_iso_utc()
    afk = _resolve_afk_monitor(contract, start_iso=start_iso)
    # Read budget limits from contract.budget; missing keys default to
    # 0.0 (a 0-limit counter never trips because BudgetCounter checks
    # value >= limit only when limit > 0; T1 invariant).
    raw_budget = contract.budget or {}
    limits = {
        "tokens_in": float(raw_budget.get("tokens_in", 0.0)),
        "tokens_out": float(raw_budget.get("tokens_out", 0.0)),
        "cost_usd": float(raw_budget.get("cost_usd", 0.0)),
        "active_wallclock_minutes": float(
            raw_budget.get("active_wallclock_minutes", 0.0)
        ),
        "subagent_dispatches": float(
            raw_budget.get("subagent_dispatches", 0.0)
        ),
    }
    budget = _bc.make_default_set(limits)

    config = RetryConfig(
        max_dispatch_retry_rounds=3,
        max_codex_review_rounds=2,
    )
    state = RetrySessionState(
        task_slug=slug,
        progress_path=task_dir / "progress.md",
    )

    # ── Build deps (prod default or test-injected) ──────────────────
    if deps_factory is not None:
        deps = deps_factory(
            slug=slug,
            task_dir=task_dir,
            contract=contract,
            manifest=manifest,
            facts=facts,
            ctx=ctx,
            criteria=criteria,
            gate_cmds=gate_cmds,
            run_id=run_id,
            task_id=task_id,
        )
    else:
        # Prod adapter: implementer round 1 already ran via
        # auto_dispatch_task before _phase2_dispatch was called, so the
        # adapter returns empty deltas on round 1. v0.8.2 T18 will
        # extend this to re-dispatch on retry rounds 2+.
        gate_runner = GateRunner(
            ctx=ctx,
            contract=contract,
            task_dir=task_dir,
            run_id=run_id,
            task_id=task_id,
            prior_baseline=_load_prior_baseline(task_dir, task_id),
        )

        def _prod_impl(*, state, prompt_prefix, **_kw):
            # Round 1: auto_dispatch_task already produced facts; no
            # additional impl work needed. T18 wires real re-dispatch.
            del prompt_prefix
            return {}

        attempt_id = f"{run_id}+{task_id}"

        def _prod_review(*, state, impl_deltas, **_kw):
            del impl_deltas
            verdict = gate_runner.run_phase2(
                manifest=manifest,
                facts=facts,
                criteria=criteria,
                attempt_id=attempt_id,
                retry_idx=state.dispatch_retry_rounds,
                baseline_command=gate_cmds["baseline"],
                codex_command=gate_cmds["codex"],
                smoke_command=gate_cmds["smoke"],
            )
            if verdict.status == "pass":
                return "pass"
            # Cache reviewer feedback so the next implementer round
            # gets it as prompt prefix (transparency rule).
            gr = verdict.gate_result
            details = (gr.details if gr is not None else None) or {}
            state.last_reviewer_feedback = (
                f"phase 2 halted at {verdict.halted_at_gate}: "
                f"{details}"
            )
            return "fail"

        deps = RetryDeps(
            run_implementer_round=_prod_impl,
            run_codex_review=_prod_review,
        )

    # ── Drive the retry loop ────────────────────────────────────────
    outcome, snap = dispatch_with_retry(
        state=state,
        config=config,
        budget=budget,
        afk=afk,
        deps=deps,
        now_iso_fn=now_iso_utc,
    )

    if outcome == "pass":
        return 0

    # ── Non-pass terminal: persist snapshot + fire block ───────────
    if snap is not None:
        snap_path = task_dir / "hard-stop.json"
        try:
            _snap.write(snap, snap_path)
        except OSError:
            # G-class: a write failure here is non-fatal — the block
            # notifier still fires below; operator can still triage.
            pass

    block_type = {
        "budget_hit": "phase2_budget_hit",
        "retry_cap": "phase2_retry_cap",
        "review_cap": "phase2_review_cap",
        "afk_hard_cap": "phase2_afk_hard_cap",
    }.get(outcome, "phase2_halted")
    issue_id = (
        snap.counter_name
        if (snap is not None and snap.counter_name)
        else outcome
    )
    why = f"phase 2 retry-loop terminal: {outcome}"
    if snap is not None and snap.value is not None and snap.limit is not None:
        why += f" ({snap.counter_name}={snap.value}/{snap.limit})"
    notifier.fire_block(
        block_type=block_type,
        phase=2,
        task_id=task_id,
        issue_id=str(issue_id),
        why_blocked=why,
        required_choice=["abort_task", "switch_to_interactive"],
        safe_resume_command=f"flow orchestrator --resume {slug}",
    )
    return 3


def _cmd_auto_execute(slug: str) -> int:
    """T19 Step 19.11 — end-to-end dispatch loop. Replaces v0.8.0 exit-2
    stub. Per manifest:

      1. CrashRecoveryDispatcher.classify() (recovery gate; T19)
      2. auto_dispatch_task() (T10/T11) → DispatchOutcome{ctx, facts}
      3. GateRunner.run_phase2() — gates 1→3→4→5→6 (T12+T13)
      4. MergeRunner.merge_task() — gate 7 + R3 steps 1-7 (T14)
      5. Gate8VerificationRunner.verify() — gate 8 + 9a/9b (T15)
      6. MergeQueue.can_proceed() — S1 wave block (T15)

    Exit codes:
      0 = all manifests merged + verified, OR contract missing →
          interactive fallback, OR pre-lock recovery → fail-closed
          interactive (legal silent — user never opted in this attempt).
      3 = block raised at any phase (recovery / dispatch / gate 1-6 /
          merge / gate 8 / wave halt). Distinct from exit 4 (S5
          nested-autonomy aborted_nested) and exit 2 (v0.8.0 stub —
          no longer reachable on auto-execute path).

    Staleness gate (T20) is OUT-OF-LOOP for v0.8.1 — operator runs
    `flow doctor <slug>` manually before invoking --auto-execute.
    v0.8.2 promotes to gate-0 in the dispatch loop.

    AFK + Budget + retry-loop wired in v0.8.2 T1/T2/T3 + T3.1: Phase 2
    flows through `dispatch_with_retry` via `_phase2_dispatch`. T18
    extends the prod implementer-round adapter to actually re-dispatch
    on retry rounds (today: round-1 facts only, retries reuse them).
    """
    # Parse + R11 ceiling check via build_plan; raises SystemExit on
    # too-new schema before we touch the dispatch loop.
    plan = build_plan(slug)
    if plan.contract is None:
        # v0.8.0 fallback: contract missing/invalid → interactive.
        # Print to stderr so the test fixture can grep for the reason.
        print(
            f"INFO: {plan.fallback_reason} — running interactive instead.",
            file=sys.stderr,
        )
        return 0

    task_dir = _resolve_slug_dir(slug)
    # task_dir = .flow/tasks/<slug>  →  parents[2] = repo root
    repo_root = task_dir.parents[2]
    contract_path = task_dir / "contract.json"
    contract_hash = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    run_id = _resolve_or_create_run_id(task_dir)
    gate_cmds = _resolve_gate_commands(plan.contract)
    integration_target = _resolve_integration_target(plan.contract)
    criteria = list(plan.contract.acceptance_criteria or [])
    notifier = Notifier(
        contract=plan.contract, slug=slug, task_dir=task_dir,
    )

    for task_idx, manifest in enumerate(plan.manifests):
        task_id = manifest.id

        # ── [P1 codex round-1] skip already-completed tasks ────────────
        # CrashRecoveryDispatcher only classifies CRASH states; a journal
        # with a clean task_completed event for this (run_id, task_id)
        # is NOT a crash → classify returns clean / proceed. Without
        # this gate, rerunning ``--auto-execute`` against a
        # half-finished plan re-dispatches and re-merges already-merged
        # tasks. Per-task completion is the resume-correctness boundary;
        # we keep it OUTSIDE the dispatcher so the dispatcher's job
        # (crash-state classification) stays focused (separation of
        # concerns — sticking it inside classify() would mean future
        # readers conflate "no crash" with "no work to do").
        if _task_already_completed(
            task_dir, run_id=run_id, task_id=task_id,
        ):
            print(
                f"INFO: task {task_id} already completed for run "
                f"{run_id}; skipping.",
                file=sys.stderr,
            )
            continue

        # ── Recovery gate (T19) ────────────────────────────────────────
        dispatcher = CrashRecoveryDispatcher(
            task_dir=task_dir,
            slug=slug,
            run_id=run_id,
            task_id=task_id,
            current_contract_hash=contract_hash,
            repo_root=repo_root,
            notifier=notifier,
        )
        v = dispatcher.classify()
        if v.action == "block":
            print(
                f"BLOCKED: {v.block_type} → see {v.blocked_md_path}",
                file=sys.stderr,
            )
            return 3
        if v.action == "fail_closed_interactive":
            # Pre-lock crash: user never opted in to this attempt.
            # Distinct from post-auto_engaged crash (which MUST block).
            reason = v.details.get("reason") if v.details else ""
            print(
                f"WARN: {reason} — interactive fallback",
                file=sys.stderr,
            )
            return 0
        # v.action == "proceed"

        # ── T10 / T11: dispatch subagent + verify manifest ─────────────
        outcome = auto_dispatch_task(
            slug=slug,
            task_idx=task_idx,
            repo_root=repo_root,
            dispatch_fn=_invoke_subagent_dispatch,  # T22 SKILL impl
            contract=plan.contract,
            manifest=manifest,
            run_id=run_id,
            contract_path=contract_path,
            contract_hash=contract_hash,
            integration_target=integration_target,
            notifier=notifier,
        )
        if outcome.status == "blocked":
            print(
                f"BLOCKED: {outcome.block_type} → see "
                f"{outcome.blocked_md_path}",
                file=sys.stderr,
            )
            return 3
        ctx, facts = outcome.ctx, outcome.facts

        # ── T20 staleness — DEFERRED v0.8.2 ─────────────────────────────
        # Codex round-4 R4: staleness gate position should be BEFORE
        # auto_dispatch_task (else worktree+auto_engaged+subagent run
        # against stale state). v0.8.1 punts to out-of-band check via
        # `flow doctor <slug>`; user runs manually before invoking
        # --auto-execute. v0.8.2 adds StalenessChecker.check_all here.

        # ── T12 + T13 + T3.1: Phase 2 retry-loop (gates 1→3→4→5→6) ─────
        # v0.8.2 T3.1: replaces fail-fast `GateRunner.run_phase2` call
        # with retry-loop wire-up via `_phase2_dispatch`. Round caps
        # default to (3, 2) until contract surfaces phase2.retry config.
        # Non-pass outcomes write HardStopSnapshot to
        # `.flow/tasks/<slug>/hard-stop.json` and fire a structured block.
        rc = _phase2_dispatch(
            slug=slug,
            task_dir=task_dir,
            contract=plan.contract,
            manifest=manifest,
            facts=facts,
            ctx=ctx,
            criteria=criteria,
            gate_cmds=gate_cmds,
            run_id=run_id,
            task_id=task_id,
            notifier=notifier,
        )
        if rc != 0:
            return rc

        # ── T14: gate 7 atomic merge (R3 9-step sequence steps 1-7) ─────
        merger = MergeRunner(
            ctx=ctx,
            contract=plan.contract,
            task_dir=task_dir,
            run_id=run_id,
            task_id=task_id,
        )
        mr = merger.merge_task(
            facts=facts, merge_strategy=gate_cmds["merge_strategy"],
        )
        if mr.status != "merged":
            # MergeRunner returns blocked status on git-merge fail (clean
            # return; mid-merge crash is caught by state 4 on next
            # startup). Here the merge failed but didn't crash.
            notifier.fire_block(
                block_type="atomic_merge_failed",
                phase=2,
                task_id=task_id,
                issue_id="atomic_merge_failed",
                why_blocked=mr.block_reason or "git merge failed",
                required_choice=[
                    "replay_merge_from_diff_hash",
                    "abort_and_revert_partial",
                    "switch_to_interactive",
                ],
                safe_resume_command=f"flow orchestrator --resume {slug}",
            )
            return 3

        # ── T15: gate 8 post-merge verify (9a PASS / 9b FAIL) ───────────
        gate8 = Gate8VerificationRunner(
            ctx=ctx,
            contract=plan.contract,
            task_dir=task_dir,
            run_id=run_id,
            task_id=task_id,
            target_commit_post_merge=mr.target_commit_post_merge,
            notifier=notifier,
        )
        g8 = gate8.verify(
            criteria=criteria,
            regression_command=gate_cmds["smoke"],
        )
        if g8.status != "completed":
            # 9b path already wrote blocked.md via the injected notifier.
            print(
                f"BLOCKED: post_merge_verify_failed → see "
                f"{g8.blocked_md_path}",
                file=sys.stderr,
            )
            return 3

        # ── T15 S1: serialization gate before next task ─────────────────
        if not MergeQueue(
            task_dir=task_dir, run_id=run_id,
        ).can_proceed(task_id=task_id):
            # Earlier task in this run is unresolved blocked_post_merge.
            # MergeQueue is the wave-level halt; the originating block
            # was already emitted, no notifier fire here.
            print(
                f"WAVE_HALTED: prior task in run {run_id} is "
                f"unresolved blocked_post_merge",
                file=sys.stderr,
            )
            return 3

    # All manifests merged + verified.
    return 0


# ── T21: nested-autonomy mechanical guard (§7 S5) ──────────────────────
# A subagent dispatched by `auto_dispatch_task` inherits the env var set
# by the parent orchestrator. If that subagent in turn invokes `flow
# orchestrator --auto-execute`, the env var is the mechanical signal that
# we are in a nested autonomy attempt — which §1 row 16 forbids.
#
# The check is mechanical (env var presence) rather than parent-process
# tree walking — robust against process-group changes, container
# boundaries, sudo, etc. Plan §21 makes this the spec, NOT a degradation:
# K-class trap defense. We do NOT decode the value as int, validate
# format, or correlate against actual ancestor PIDs — any non-empty
# string triggers abort. F-class fail-closed: even slug-not-found returns
# exit 4 (security-positive default; no info leak about slug existence).
#
# S-class wire-up requirement: the guard MUST run inside `main()` BEFORE
# routing to `_cmd_auto_execute`. Putting it inside `_cmd_auto_execute`
# would let any future caller of `_cmd_auto_execute` (e.g. a programmatic
# resume path) bypass the guard.
AUTONOMY_PARENT_PID_ENV = "FLOW_AUTONOMY_PARENT_PID"

# [P3 stderr parity] codex round-1: use the SAME generic stderr message
# for both slug-exists and slug-missing paths. Earlier versions printed
# distinct messages (one named the slug as "not found", the other named
# the decisions.jsonl path) — that lets an adversary enumerate slugs via
# stderr text even though the exit code (4) is parity. The detail
# (parent_pid, slug, task_dir) belongs in the decisions.jsonl audit trail
# when the slug resolves; operators read that journal, not stderr.
NESTED_ABORT_STDERR = (
    "ERROR: aborted_nested - orchestrator was invoked from a subagent "
    "context. Nested autonomy is forbidden (see safety stack plan, "
    "section 1 row 16)."
)


def _guard_against_nested_autonomy(slug: str) -> Optional[int]:
    """Return exit code 4 when the env var indicates a nested attempt;
    return ``None`` to let the top-level orchestrator proceed.

    Side effects on guard fire:
      * stderr always prints the same generic ``NESTED_ABORT_STDERR``
        message (no slug / parent_pid leak).
      * Decision record `aborted_nested` appended to
        ``decisions.jsonl`` when the slug resolves; ``parent_pid`` is
        ``repr()``-escaped before interpolation (R-class
        defense-in-depth: control chars become readable ``\\n``/``\\r``).

    The slug-not-found branch is intentionally treated as "still abort
    with 4" rather than "report missing slug" — see plan 7670-7677.
    Reporting "not found" would leak slug-existence to a sub-process
    that may be probing.
    """
    parent_pid = os.environ.get(AUTONOMY_PARENT_PID_ENV)
    if not parent_pid:
        return None

    # [P3 stderr parity] Generic message first; identical for both
    # branches below. parent_pid + slug context live in decisions.jsonl
    # when task_dir resolves.
    print(NESTED_ABORT_STDERR, file=sys.stderr)

    try:
        task_dir = _resolve_slug_dir(slug)
    except SystemExit:
        # Slug-not-found: still abort (no info leak). No task_dir to
        # write a decision into. stderr already printed above; we
        # deliberately emit nothing more here to keep parity with the
        # slug-exists path.
        return 4
    # [P3 R-class] parent_pid comes from an env var (user-controlled);
    # repr() converts \n, \r, \t, ANSI ESC, OSC, BEL etc. into readable
    # backslash escapes. Strip the surrounding quotes so the reason
    # field reads naturally. json.dumps inside append_jsonl_locked is
    # the primary defense for the JSONL file format; this is
    # defense-in-depth on input so any *future* downstream consumer that
    # renders ``reason`` directly (terminal, HTML diff, log tail) is
    # also safe.
    parent_pid_safe = repr(parent_pid)[1:-1]
    append_decision(task_dir, DecisionRecord(
        id=f"aborted-nested-{_new_event_id()}",
        ts=_now_iso(),
        phase=2,
        task=slug,
        decision="aborted_nested",
        reason=(
            f"--auto-execute called with "
            f"{AUTONOMY_PARENT_PID_ENV}={parent_pid_safe}; "
            f"nested autonomy is forbidden (§1 row 16)"
        ),
    ))
    return 4


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="flow orchestrator")
    # [P2] codex round-1: --dry-run and --auto-execute are mutually
    # exclusive. T21 changed the routing order in main() so that
    # `args.auto_execute` is checked BEFORE `args.dry_run` (the guard
    # has to run on the auto-execute path), which silently turned
    # `flow orchestrator --dry-run demo --auto-execute demo` into an
    # auto-execute call. argparse now raises SystemExit(2) with a
    # helpful "not allowed with" message when both flags are supplied.
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", metavar="SLUG",
                     help="Print plan + manifest")
    grp.add_argument("--auto-execute", metavar="SLUG",
                     help="(disabled in v0.8.0) attempt autonomous run")
    args = parser.parse_args(argv)

    if args.auto_execute:
        # T21 / S5 — mechanical nested-autonomy guard runs BEFORE any
        # other logic on the --auto-execute path. Putting this inside
        # `_cmd_auto_execute` would risk a future caller bypassing it
        # (S-class wire-up gap); main() is the single entry point.
        guard = _guard_against_nested_autonomy(args.auto_execute)
        if guard is not None:
            return guard
        return _cmd_auto_execute(args.auto_execute)
    if args.dry_run:
        return _cmd_dry_run(args.dry_run)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
