"""flow_contract — contract.json schema, parser, validator, and CLI.

Contract is the canonical source for autonomy configuration of a Flow task.
Stored at .flow/tasks/<slug>/contract.json. The Phase 1 brainstorm produces
it; Phase 2/3 read it; Phase 4 carries forward unresolved warnings.

v0.8.0 ships parsing + validation + CLI. Orchestrator reads contracts but
refuses to autonomously dispatch (use v0.8.1+ for that).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


CONTRACT_SCHEMA_VERSION = 1

VALID_AUTONOMY_MODES = ("auto", "interactive")
VALID_RISK_TIERS = ("low", "med", "high")
VALID_AFK_ON_TIMEOUT = ("abort", "wait")
VALID_CRITERION_TYPES = (
    "unit", "integration", "e2e", "smoke", "behavior", "regression",
)
KNOWN_IRREVERSIBLE = (
    "push_main", "release_tag", "schema_migration",
    "lockfile_major_change", "public_docs_change",
    "delete_local_work", "overwrite_checkpoint", "public_api_change",
)


class ContractError(ValueError):
    """Raised when contract.json is malformed or has invalid known-field values.

    Per fail-closed policy, callers should treat this as 'revert to interactive
    mode' rather than ignoring the contract.
    """


@dataclass
class AcceptanceCriterion:
    description: str
    type: str
    command: str


@dataclass
class Contract:
    contract_schema_version: int
    autonomy_mode: str
    created_at: str
    staleness_ttl_days: int = 7
    scope_allowed: list[str] = field(default_factory=list)
    scope_forbidden: list[str] = field(default_factory=list)
    known_forks: list[dict] = field(default_factory=list)
    escalation_triggers: list[dict] = field(default_factory=list)
    irreversible_actions: list[str] = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    notification_command: Optional[str] = None
    afk_timeout_min: Optional[int] = None
    afk_on_timeout: Optional[str] = None
    unknown_fields: list[str] = field(default_factory=list)


def parse_contract(path: Path) -> Contract:
    """Parse contract.json. Fail-closed on missing required fields or invalid
    values for *known* fields. Unknown fields are accepted with a warning
    list (forward-compat: an old reader should not crash on a new writer).
    """
    if not path.is_file():
        raise ContractError(f"contract.json not found: {path}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ContractError(f"contract.json is not valid JSON: {e}")
    if not isinstance(raw, dict):
        raise ContractError("contract.json must be a JSON object")

    known = {
        "contract_schema_version", "autonomy_mode", "created_at",
        "staleness_ttl_days", "scope", "known_forks", "escalation_triggers",
        "irreversible_actions", "budget", "acceptance_criteria",
        "notification", "afk_timeout_min", "afk_on_timeout",
    }
    unknown = sorted(set(raw.keys()) - known)

    # Required fields
    for k in ("contract_schema_version", "autonomy_mode", "created_at"):
        if k not in raw:
            raise ContractError(f"contract.json missing required field: {k}")

    # Validate known field values
    csv = raw["contract_schema_version"]
    if not isinstance(csv, int) or csv < 1:
        raise ContractError(f"contract_schema_version must be int >= 1, got {csv!r}")

    am = raw["autonomy_mode"]
    if am not in VALID_AUTONOMY_MODES:
        raise ContractError(
            f"autonomy_mode must be one of {VALID_AUTONOMY_MODES}, got {am!r}"
        )

    ca = raw["created_at"]
    if not isinstance(ca, str) or not ca:
        raise ContractError("created_at must be non-empty ISO 8601 string")

    scope = raw.get("scope", {})
    if not isinstance(scope, dict):
        raise ContractError("scope must be an object")

    ac_raw = raw.get("acceptance_criteria", [])
    if not isinstance(ac_raw, list):
        raise ContractError("acceptance_criteria must be an array")
    crits = []
    for c in ac_raw:
        if not isinstance(c, dict):
            raise ContractError("acceptance_criteria items must be objects")
        for k in ("description", "type", "command"):
            if k not in c:
                raise ContractError(f"acceptance_criterion missing {k}")
        if c["type"] not in VALID_CRITERION_TYPES:
            raise ContractError(
                f"acceptance_criterion.type must be one of "
                f"{VALID_CRITERION_TYPES}, got {c['type']!r}"
            )
        crits.append(AcceptanceCriterion(
            description=c["description"], type=c["type"], command=c["command"],
        ))

    afk = raw.get("afk_on_timeout")
    if afk is not None and afk not in VALID_AFK_ON_TIMEOUT:
        raise ContractError(
            f"afk_on_timeout must be one of {VALID_AFK_ON_TIMEOUT}, got {afk!r}"
        )

    notif = raw.get("notification", {})
    if not isinstance(notif, dict):
        raise ContractError("notification must be an object")

    return Contract(
        contract_schema_version=csv,
        autonomy_mode=am,
        created_at=ca,
        staleness_ttl_days=int(raw.get("staleness_ttl_days", 7)),
        scope_allowed=list(scope.get("allowed", [])),      # T2: add isinstance list check
        scope_forbidden=list(scope.get("forbidden", [])),  # T2: add isinstance list check
        known_forks=list(raw.get("known_forks") or []),            # T2: harden
        escalation_triggers=list(raw.get("escalation_triggers") or []),  # T2: harden
        irreversible_actions=list(raw.get("irreversible_actions") or []),  # T2: harden
        budget=dict(raw.get("budget") or {}),                      # T2: harden
        acceptance_criteria=crits,
        notification_command=notif.get("command"),
        afk_timeout_min=raw.get("afk_timeout_min"),
        afk_on_timeout=afk,
        unknown_fields=unknown,
    )


def validate_contract(path: Path) -> tuple[bool, list[str]]:
    """Validate contract.json end-to-end. Returns (ok, errors).

    Layered on parse_contract: parse errors → ok=False with one error.
    Cross-field rules and version-compat checks happen here. Warnings are
    prefixed `[warn]` and do NOT flip ok to False.
    """
    errors: list[str] = []
    try:
        c = parse_contract(path)
    except ContractError as e:
        return False, [str(e)]

    if c.contract_schema_version > CONTRACT_SCHEMA_VERSION:
        errors.append(
            f"contract_schema_version {c.contract_schema_version} is newer than "
            f"this flow ({CONTRACT_SCHEMA_VERSION}). Upgrade flow or downgrade "
            f"the contract."
        )

    if c.autonomy_mode == "auto" and not c.acceptance_criteria:
        # Warn rather than fail in v0.8.0 — Phase 3 falls back to legacy gate.
        # Treated as warning by CLI (non-zero hint, not failure exit).
        errors.append(
            "[warn] autonomy_mode=auto but no acceptance_criteria — Phase 3 "
            "will fall back to the legacy test+codex gate. Add criteria to "
            "unlock the v0.8.1+ verification gate."
        )

    return (not any(e for e in errors if not e.startswith("[warn]"))), errors


def _resolve_slug_dir(slug: str) -> Path:
    """Resolve .flow/tasks/<slug>/ from cwd. Walks up from current working
    directory looking for a `.flow/` directory (mirrors common pattern
    used by flow_wave_planner._project_root).
    """
    here = Path.cwd().resolve()
    for parent in [here, *here.parents]:
        if (parent / ".flow").is_dir():
            return parent / ".flow" / "tasks" / slug
    raise SystemExit(f"ERROR: .flow/ directory not found from {here}")


def _cmd_validate(args: argparse.Namespace) -> int:
    slug_dir = _resolve_slug_dir(args.slug)
    contract_path = slug_dir / "contract.json"
    ok, errors = validate_contract(contract_path)
    warnings = [e for e in errors if e.startswith("[warn]")]
    hard_errors = [e for e in errors if not e.startswith("[warn]")]
    for e in hard_errors:
        print(f"ERROR: {e}", file=sys.stderr)
    for w in warnings:
        print(w)
    if ok:
        print(f"OK: contract.json for {args.slug} is valid"
              + (f" ({len(warnings)} warning(s))" if warnings else ""))
        return 0
    return 1


def _cmd_init(args: argparse.Namespace) -> int:
    slug_dir = _resolve_slug_dir(args.slug)
    contract_path = slug_dir / "contract.json"
    if contract_path.exists() and not args.force:
        print(f"ERROR: {contract_path} already exists. Use --force to overwrite.",
              file=sys.stderr)
        return 1
    template = _build_template(args.slug, slug_dir)
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {contract_path}")
    print("Next: edit the file or update progress.md frontmatter, then "
          "run `flow contract --validate <slug>`.")
    return 0


def _build_template(slug: str, slug_dir: Path) -> dict:
    """Build a minimal valid contract template. v0.8.0: no auto-infer yet —
    we ship a documented skeleton; v0.8.1+ adds prd.md/research/ inference.
    """
    import datetime
    return {
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "autonomy_mode": "interactive",
        "created_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "staleness_ttl_days": 7,
        "scope": {
            "allowed": ["<file glob>"],
            "forbidden": [".env", "secrets/**"],
        },
        "known_forks": [],
        "escalation_triggers": [],
        "irreversible_actions": list(KNOWN_IRREVERSIBLE),
        "budget": {
            "max_task_count": 20,
            "max_files_changed": 50,
            "max_new_deps": 0,
            "max_retry_per_task": 2,
            "max_elapsed_min": 240,
        },
        "acceptance_criteria": [],
        "notification": {"command": None},
        "afk_timeout_min": 240,
        "afk_on_timeout": "wait",
    }


def main(argv: Optional[list[str]] = None) -> int:
    args = argv or sys.argv[1:]
    if not args:
        print("Usage: flow contract --validate <slug>  |  --init <slug> [--force]",
              file=sys.stderr)
        return 1

    head = args[0]
    if head == "--validate":
        if len(args) < 2:
            print("ERROR: flow contract --validate <slug>", file=sys.stderr)
            return 1
        return _cmd_validate(argparse.Namespace(slug=args[1]))
    if head == "--init":
        ns = argparse.Namespace(slug=None, force=False)
        for a in args[1:]:
            if a == "--force":
                ns.force = True
            elif not a.startswith("-"):
                ns.slug = a
        if not ns.slug:
            print("ERROR: flow contract --init <slug>", file=sys.stderr)
            return 1
        return _cmd_init(ns)
    print(f"Unknown subcommand: {head}", file=sys.stderr)
    print("Usage: flow contract --validate <slug>  |  --init <slug> [--force]",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
