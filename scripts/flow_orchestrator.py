"""flow_orchestrator — v0.8.0 dry-run orchestrator.

Reads contract.json + progress.md `### Tasks` block. Builds per-task file
ownership manifest (allowed_writes = scope.allowed ∩ task.writes; flags
files outside scope as out_of_scope). Prints plan in human-readable form.

v0.8.0 explicitly REFUSES `--auto-execute`. v0.8.1+ extends this script
(or a sibling) to dispatch worktree-isolated subagents.
"""
from __future__ import annotations

import argparse
import fnmatch
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

    try:
        contract = parse_contract(contract_path)
    except ContractError as e:
        plan.autonomy_mode = "interactive"
        plan.fallback_reason = f"contract parse failed ({e}) — falling back to interactive"
        return plan

    # T2 R11: hard-reject contracts whose schema_version exceeds what this
    # runtime knows. v0.8.0 only warned via the validator and let the
    # orchestrator proceed; codex round-3 R11 caught the silent-accept gap.
    # Fail-closed at the orchestrator boundary covers BOTH --dry-run and
    # --auto-execute (the caller routes through build_plan in both paths).
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
