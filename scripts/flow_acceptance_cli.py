#!/usr/bin/env python3
"""flow acceptance — CLI subcommand wired by ``scripts/flow.py`` for the
v0.8.1 Phase 3 SKILL change (T22 Step 22.4b).

Usage:
    flow acceptance --run <slug> [--phase {2,3}]

Resolves the contract under ``.flow/tasks/<slug>/contract.json``,
dispatches each ``acceptance_criteria`` entry through
``flow_acceptance.AcceptanceRunner.run_one`` + ``evaluate_criterion``,
and exits 0 if every criterion evaluates to ``PASS``, 1 on the first
non-PASS criterion (with a FAIL diagnostic on stderr).

Empty ``acceptance_criteria`` returns 0 — the legacy test+codex gate
(SKILL.md Step 1+) is responsible for verification in that case.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _resolve_slug_dir(slug: str) -> Path:
    """Match ``flow_orchestrator._resolve_slug_dir`` semantics — search
    upward from cwd for ``.flow/tasks/<slug>/``. R-class: the slug
    string flows into a filesystem path, but ``.flow/tasks/...`` is
    rooted at a known parent and ``Path.is_dir`` won't traverse outside
    of the candidate. We do reject obvious traversal early.
    """
    if not slug or "/" in slug or "\\" in slug or slug in (".", ".."):
        raise SystemExit(f"ERROR: invalid slug {slug!r}")
    here = Path.cwd().resolve()
    for parent in [here, *here.parents]:
        cand = parent / ".flow" / "tasks" / slug
        if cand.is_dir():
            return cand
    raise SystemExit(f"ERROR: .flow/tasks/{slug}/ not found from {here}")


def _cmd_acceptance(args: argparse.Namespace) -> int:
    # Lazy import — keeps the CLI fast for `--help` and isolates the
    # heavy contract/runner deps to actual run paths.
    from flow_acceptance import AcceptanceRunner, EvalDecision
    from flow_contract import (
        CONTRACT_SCHEMA_VERSION,
        ContractError,
        parse_contract,
    )

    slug = args.run
    task_dir = _resolve_slug_dir(slug)
    contract_path = task_dir / "contract.json"
    if not contract_path.is_file():
        print(
            f"ERROR: {contract_path} missing — nothing to verify.",
            file=sys.stderr,
        )
        return 1

    # L-class (codex round-1 F5): R11 schema-version ceiling MUST fire on
    # this CLI path too, mirroring ``flow_orchestrator.build_plan`` lines
    # 118-148. parse_contract alone does NOT enforce a ceiling — a
    # contract_schema_version=999 contract can roundtrip parse fields
    # this v0.8.1 runtime knows about and execute under v1 semantics
    # (silently). Pre-parse raw-JSON check is the canonical pattern: it
    # also fires when the future schema makes parse itself fail (a v=999
    # contract with a future-only field would otherwise degrade through
    # the ContractError path below). Same Rule as build_plan: only
    # ceiling-check on a clean int > known max; everything else falls
    # through to parse_contract's error paths.
    try:
        raw = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    if isinstance(raw, dict):
        raw_v = raw.get("contract_schema_version")
        if (
            isinstance(raw_v, int)
            and not isinstance(raw_v, bool)
            and raw_v > CONTRACT_SCHEMA_VERSION
        ):
            print(
                f"ERROR: contract.json declares contract_schema_version="
                f"{raw_v} but this flow runtime knows up to "
                f"{CONTRACT_SCHEMA_VERSION}. Upgrade flow OR downgrade "
                f"the contract.",
                file=sys.stderr,
            )
            return 1

    try:
        contract = parse_contract(contract_path)
    except ContractError as e:
        print(f"ERROR: contract parse failed: {e}", file=sys.stderr)
        return 1

    # Defense-in-depth: parsed-side ceiling check (mirrors build_plan
    # lines 157-169). Pre-parse check above already covers happy path;
    # this catches any future code that bypasses the raw-JSON guard.
    if contract.contract_schema_version > CONTRACT_SCHEMA_VERSION:
        print(
            f"ERROR: contract.json declares contract_schema_version="
            f"{contract.contract_schema_version} but this flow runtime "
            f"knows up to {CONTRACT_SCHEMA_VERSION}. Upgrade flow OR "
            f"downgrade the contract.",
            file=sys.stderr,
        )
        return 1

    criteria = list(contract.acceptance_criteria or [])
    if not criteria:
        # Empty criteria is legal; the SKILL Step 0.5 routes to the
        # legacy test+codex gate when this path returns 0.
        print(
            "INFO: contract has no acceptance_criteria — nothing to run.",
            file=sys.stderr,
        )
        return 0

    # task_dir = .flow/tasks/<slug>  →  parents[2] = repo root.
    repo_root = task_dir.parents[2]
    log_dir = task_dir / "logs" / "acceptance"
    run_id = f"cli+{int(time.time())}"
    runner = AcceptanceRunner(
        worktree_root=repo_root,
        log_dir=log_dir,
        slug=slug,
        task_id="phase3",
        run_id=run_id,
        worktree_id="cli",
    )
    for idx, crit in enumerate(criteria):
        rr = runner.run_one(
            crit,
            criterion_idx=idx,
            attempt_id=run_id,
            retry_idx=0,
            task_dir=task_dir,
        )
        decision = runner.evaluate_criterion(
            crit, phase=args.phase, runner_result=rr,
        )
        if decision != EvalDecision.PASS:
            print(
                f"FAIL: criterion {idx} "
                f"({crit.description!r}) → decision={decision.value} "
                f"status={rr.status}",
                file=sys.stderr,
            )
            return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flow acceptance",
        description=(
            "Run acceptance_criteria from contract.json. v0.8.1+ "
            "wired by Phase 3 SKILL Step 0.5."
        ),
    )
    parser.add_argument(
        "--run",
        metavar="SLUG",
        required=True,
        help="task slug under .flow/tasks/<slug>/",
    )
    parser.add_argument(
        "--phase",
        type=int,
        choices=[2, 3],
        default=3,
        help=(
            "Phase 2 (during dispatch) or Phase 3 (post-merge verify; "
            "default)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _cmd_acceptance(args)


if __name__ == "__main__":
    sys.exit(main())
