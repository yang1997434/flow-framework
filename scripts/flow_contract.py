"""flow_contract — contract.json schema, parser, validator, and CLI.

Contract is the canonical source for autonomy configuration of a Flow task.
Stored at .flow/tasks/<slug>/contract.json. The Phase 1 brainstorm produces
it; Phase 2/3 read it; Phase 4 carries forward unresolved warnings.

v0.8.0 ships parsing + validation + CLI. Orchestrator reads contracts but
refuses to autonomously dispatch (use v0.8.1+ for that).

v0.8.1 (T1) extends the schema additively (schema version stays 1):
- budget.max_codex_rounds_per_task (default 3) — Q2.2
- notification.throttle_min (default 5) + tier2_enabled (default True) — R9
- idempotent_cmd_allowlist (default 8 entries) — R8
- post_merge_regression_optional (default False) — S3
- acceptance_criteria[].method (orthogonal to type) — R5
- acceptance_criteria[].timeout_sec (per-method default) — R7
- acceptance_criteria[].idempotent (object: value/rationale/timeout_sec/
    side_effect_class) — R8
- acceptance_criteria[].post_merge_skip (regression cross-field check) — Y1+S3

All defaults applied at parse time; missing fields never surface as None.
Forward-compat preserved: unknown top-level fields still warn-and-keep.

================================================================
CONTRIBUTOR NOTE — schema parsing rule (T1 v0.8.1, codex round-6)
================================================================
RULE: schema parsing MUST NOT use `dict.get(key)` (or `obj.get(key) or X`,
or `is None` checks) for fields where explicit `null`, empty string, wrong
type, or other falsy values have semantic meaning. Six rounds of /codex
review on T1 (commits be52061..51dc4d3) found the SAME bug class repeatedly:
absent → default vs explicit-null → reject was being conflated, silently
rescuing malformed contracts.

Use the `require_field` / `optional_field` helpers + the `non_empty_str` /
`bool_value` / `positive_int` / etc. validators defined below. They split
"absent → default" (the only path that returns the default unvalidated) from
"explicit value → validate" (which rejects null/wrong-type/falsy per the
field's contract). Codex consult session: 019dfd48-634c-7720-922d-15313dcc96c7.

T2-T22 implementers: any new contract field you add MUST go through these
helpers, otherwise the next codex round WILL find a null-bypass and we
re-do this fix yet again. If you need a validator we don't have yet, add
it next to the others — keep the same `(key, value) -> value | raises`
shape.
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
VALID_CRITERION_METHODS = ("cmd", "file_exists", "json_query", "http")
# T1 M1: per-method required-field map. Enforced after method validation so a
# criterion like {method: "cmd"} without `command` fails-closed at parse time
# rather than blowing up later in T6/T7 with a less actionable error.
REQUIRED_FIELD_BY_METHOD = {
    "cmd": "command",
    "file_exists": "path",
    "http": "url",
    "json_query": "json_query",
}
VALID_SIDE_EFFECT_CLASSES = ("pure", "read_only", "cache_only", "reversible")
KNOWN_IRREVERSIBLE = (
    "push_main", "release_tag", "schema_migration",
    "lockfile_major_change", "public_docs_change",
    "delete_local_work", "overwrite_checkpoint", "public_api_change",
)

# T1 R7: per-method default criterion timeouts. type=e2e overrides cmd default.
DEFAULT_TIMEOUT_BY_METHOD = {
    "file_exists": 30,
    "json_query": 30,
    "cmd": 600,
    "http": 60,
}
DEFAULT_TIMEOUT_E2E = 1800  # type=e2e overrides method-based default

# T1 R8: built-in idempotent command allowlist. Binaries whose canonical
# usage is read-only / pure verification (test runners, type checkers,
# linters, flow's own validation tools).
DEFAULT_IDEMPOTENT_CMD_ALLOWLIST = [
    "pytest", "mypy", "eslint", "tsc", "cargo check", "go test",
    "flow doctor", "flow contract --validate",
]

# T1 R9: notification defaults. throttle_min=0 means "no throttle, every
# event fires" (NOT "disabled"); tier2_enabled is the kill switch.
DEFAULT_NOTIFICATION = {
    "command": None,
    "throttle_min": 5,
    "tier2_enabled": True,
}


class ContractError(ValueError):
    """Raised when contract.json is malformed or has invalid known-field values.

    Per fail-closed policy, callers should treat this as 'revert to interactive
    mode' rather than ignoring the contract.
    """


# ----------------------------------------------------------------------
# Schema validation helpers (T1 v0.8.1, codex round-6 path B).
#
# RULE: schema parsing MUST NOT use `.get()` for fields where explicit `null`,
# empty string, wrong type, or falsy has semantic meaning. Use these helpers
# instead — they distinguish "absent → default" from "explicit value →
# validate". Codex review will keep finding bypasses if you use `.get()`.
# See module docstring + commits be52061..51dc4d3 for the bug history.
# ----------------------------------------------------------------------


def require_field(obj: dict, key: str, validate):
    """Field must be present. Raises ContractError if missing.

    Delegates value validation to `validate(key, value)`.
    """
    if key not in obj:
        raise ContractError(f"{key} missing")
    return validate(key, obj[key])


def optional_field(obj: dict, key: str, validate, default):
    """If `key` not in `obj`, return `default` (no validation).

    Otherwise validate the explicit value — `null`, `""`, wrong type are all
    routed to the validator, which should reject them per the fail-closed
    posture (the default applies ONLY to the absent case).
    """
    if key not in obj:
        return default
    return validate(key, obj[key])


def non_empty_str(key: str, value):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{key} must be a non-empty string, got {value!r}")
    return value


def nullable_str(key: str, value):
    """Accepts a non-empty string OR explicit `null` (both meaningful).

    Used for fields like `notification.command` where `null` legitimately
    means "no command configured" (same semantics as absent).
    """
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ContractError(
            f"{key} must be a non-empty string or null, got {value!r}"
        )
    return value


def bool_value(key: str, value):
    if not isinstance(value, bool):
        raise ContractError(f"{key} must be a boolean, got {value!r}")
    return value


def positive_int(key: str, value):
    # bool is a subclass of int — exclude explicitly.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(
            f"{key} must be a positive integer (>= 1), got {value!r}"
        )
    return value


def non_negative_int(key: str, value):
    """For fields like throttle_min where 0 is meaningful (no throttle)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(
            f"{key} must be a non-negative integer, got {value!r}"
        )
    return value


def non_empty_str_list(key: str, value):
    if not isinstance(value, list):
        raise ContractError(f"{key} must be a list of strings, got {value!r}")
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ContractError(
                f"{key}[{i}] must be a non-empty string, got {item!r}"
            )
    return list(value)


def dict_value(key: str, value):
    if not isinstance(value, dict):
        raise ContractError(f"{key} must be an object, got {value!r}")
    return value


@dataclass
class AcceptanceCriterion:
    description: str
    type: str          # unit | integration | e2e | smoke | behavior | regression
    method: str        # cmd | file_exists | json_query | http  (R5: orthogonal)
    command: Optional[str] = None
    path: Optional[str] = None
    url: Optional[str] = None
    json_query: Optional[str] = None
    # R7: parse_contract always overwrites this with a positive value via
    # _default_timeout_for_method or the explicit field. The 0 placeholder
    # exists only because dataclass requires a default for fields after this
    # one; reading 0 in production is a parser bug.
    timeout_sec: int = 0
    # R8 hardened: {value: bool, rationale: str, timeout_sec: int,
    #               side_effect_class: pure|read_only|cache_only|reversible}
    idempotent: Optional[dict] = None
    post_merge_skip: bool = False      # Y1 + S3


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
    # T1 R8: project-wide idempotent command allowlist (extendable per-task).
    idempotent_cmd_allowlist: list[str] = field(
        default_factory=lambda: list(DEFAULT_IDEMPOTENT_CMD_ALLOWLIST)
    )
    # T1 S3: opt-in to allow type=regression criteria with post_merge_skip=true.
    post_merge_regression_optional: bool = False
    # T1 R9: notification dict supersedes the v0.8.0 standalone field.
    # Shape: {command: str|None, throttle_min: int, tier2_enabled: bool}.
    # NOTE: the standalone top-level `notification_command` field was removed
    # in v0.8.1 (no shim). CHANGELOG breaking-change note added at T23 release.
    notification: dict = field(default_factory=lambda: dict(DEFAULT_NOTIFICATION))
    afk_timeout_min: Optional[int] = None
    afk_on_timeout: Optional[str] = None
    # v0.8.5 — dispatch observability + feedback enrichment switches.
    # PRD R5: both default to "on" (telemetry writes locally to task
    # dir; feedback enrichment adds Round 2+ structural diff map to
    # implementer prompt). The two switches are INDEPENDENT — disabling
    # one does NOT disable the other.
    # Shape: {"telemetry": "on"|"off", "feedback_enrichment": "on"|"off"}.
    dispatch: dict = field(default_factory=lambda: {
        "telemetry": "on",
        "feedback_enrichment": "on",
    })
    unknown_fields: list[str] = field(default_factory=list)


def _infer_method(c: dict, idx: int) -> str:
    """v0.8.0 contracts wrote `command` only (no `method`). Infer for compat.

    M2: idx is included in the error message so it matches sibling validators.
    """
    if "command" in c:
        return "cmd"
    if "path" in c:
        return "file_exists"
    if "url" in c:
        return "http"
    if "json_query" in c:
        return "json_query"
    raise ContractError(
        f"acceptance_criteria[{idx}] missing method (and no command/path/url/"
        f"json_query to infer from)"
    )


def _validate_idempotent_object(idem: dict, idx: int) -> dict:
    """R8: idempotent override must include value/rationale/timeout_sec/
    side_effect_class. Raises ContractError on shape violation."""
    if not isinstance(idem, dict):
        raise ContractError(
            f"acceptance_criteria[{idx}].idempotent must be an object"
        )
    required = ("value", "rationale", "timeout_sec", "side_effect_class")
    missing = [k for k in required if k not in idem]
    if missing:
        raise ContractError(
            f"acceptance_criteria[{idx}].idempotent missing keys: "
            f"{', '.join(missing)}"
        )
    if not isinstance(idem["value"], bool):
        raise ContractError(
            f"acceptance_criteria[{idx}].idempotent.value must be bool"
        )
    if not isinstance(idem["rationale"], str) or not idem["rationale"].strip():
        raise ContractError(
            f"acceptance_criteria[{idx}].idempotent.rationale must be non-empty "
            f"string"
        )
    # L1: timeout_sec must be a positive int — zero/negative verification
    # timeouts are nonsense and match the constraint at the criterion level.
    its = idem["timeout_sec"]
    if isinstance(its, bool) or not isinstance(its, int) or its <= 0:
        raise ContractError(
            f"acceptance_criteria[{idx}].idempotent.timeout_sec must be "
            f"positive int"
        )
    sec = idem["side_effect_class"]
    if sec not in VALID_SIDE_EFFECT_CLASSES:
        raise ContractError(
            f"acceptance_criteria[{idx}].idempotent.side_effect_class must be "
            f"one of {VALID_SIDE_EFFECT_CLASSES}, got {sec!r}"
        )
    return dict(idem)


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
        # T1 v0.8.1 additive top-level fields:
        "idempotent_cmd_allowlist", "post_merge_regression_optional",
        # v0.8.5 — dispatch observability + feedback enrichment switches.
        "dispatch",
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

    # T1 S3: top-level opt-in for regression suite skip.
    # Routed through optional_field — explicit null/non-bool reject;
    # absent → default False.
    pmro = optional_field(raw, "post_merge_regression_optional", bool_value, False)

    ac_raw = raw.get("acceptance_criteria", [])
    if not isinstance(ac_raw, list):
        raise ContractError("acceptance_criteria must be an array")
    crits: list[AcceptanceCriterion] = []
    for idx, c in enumerate(ac_raw):
        if not isinstance(c, dict):
            raise ContractError("acceptance_criteria items must be objects")
        for k in ("description", "type"):
            if k not in c:
                raise ContractError(f"acceptance_criterion missing {k}")
        if c["type"] not in VALID_CRITERION_TYPES:
            raise ContractError(
                f"acceptance_criterion.type must be one of "
                f"{VALID_CRITERION_TYPES}, got {c['type']!r}"
            )

        # R5: method orthogonal to type. Backward compat: infer from fields
        # ONLY when `method` key is absent (v0.8.0 contracts had `command`
        # only). C1 fail-closed: an explicit falsy/invalid value (`""`, `0`,
        # `False`, `None`, etc.) must NOT fall through to inference — that
        # silently rescues a typo'd contract. Only a missing key triggers
        # v0.8.0-compat inference.
        if "method" in c:
            method = c["method"]
            if method not in VALID_CRITERION_METHODS:
                raise ContractError(
                    f"acceptance_criteria[{idx}].method must be one of "
                    f"{VALID_CRITERION_METHODS}, got {method!r}"
                )
        else:
            method = _infer_method(c, idx)
            # _infer_method always returns a value in VALID_CRITERION_METHODS
            # (or raises) so no post-check needed here.

        # M1: per-method required-field check — routed through the
        # require_field helper so that `cmd` with `command: ""`, `command: 0`,
        # `command: false`, or `command: null` ALL reject at parse time
        # (rather than parsing as command=None and exploding later in T6/T7
        # with a less actionable error).
        required_field_name = REQUIRED_FIELD_BY_METHOD[method]
        try:
            require_field(c, required_field_name, non_empty_str)
        except ContractError as e:
            # Re-raise with the same wording the existing tests assert on
            # (preserves error-message stability for downstream log parsers).
            raise ContractError(
                f"acceptance_criteria[{idx}] method={method!r} requires "
                f"{required_field_name!r}"
            ) from e

        # R7: timeout default by method (or by type=e2e override).
        # optional_field: absent → method/type default; explicit value (incl.
        # null/non-int/<=0) → routed through positive_int validator → reject.
        default_timeout = (
            DEFAULT_TIMEOUT_E2E if c["type"] == "e2e"
            else DEFAULT_TIMEOUT_BY_METHOD[method]
        )
        timeout_sec = optional_field(c, "timeout_sec", positive_int, default_timeout)

        # R8: idempotent override object validation (when present).
        # C2: e2e criteria CANNOT carry an idempotent override at all. Per
        # design v0.8.1-execution-semantics §6 R8 table row `e2e`: "always
        # non-idempotent | NO override accepted". T9 will always treat
        # in-flight e2e as block_in_flight regardless of any value here, so
        # accepting the field would let the contract silently lie about a
        # safety property the runtime refuses to honor. Reject at parse time.
        # Use key presence (`"idempotent" in c`), not `.get() is not None`,
        # so that an explicit `idempotent: null` is also rejected — design
        # §6 R8 forbids ANY override on e2e, value or null included.
        if "idempotent" in c and c["type"] == "e2e":
            raise ContractError(
                f"acceptance_criteria[{idx}] type=e2e cannot specify "
                f"idempotent override (T9 always blocks in-flight e2e "
                f"regardless; design §6 R8 forbids any e2e idempotent override)"
            )
        # C2-followup-2 (codex round-3): use key presence here too. An explicit
        # `idempotent: null` on a non-e2e criterion is a malformed override,
        # not "absent" — the schema requires the field, when present, to be
        # an object with required keys. Routed through optional_field with a
        # closure validator so the idx is captured and absent → None.
        def _validate_idem(_key, value, _idx=idx):
            return _validate_idempotent_object(value, _idx)
        idempotent = optional_field(c, "idempotent", _validate_idem, None)

        # Y1 + S3: post_merge_skip cross-field rule (regression type requires
        # contract-level opt-in). bool_value validates type — null/non-bool
        # reject; absent → default False.
        pms = optional_field(c, "post_merge_skip", bool_value, False)
        if pms and c["type"] == "regression" and not pmro:
            raise ContractError(
                "post_merge_skip illegal for type=regression unless "
                "post_merge_regression_optional set"
            )

        crits.append(AcceptanceCriterion(
            description=c["description"],
            type=c["type"],
            method=method,
            command=c.get("command"),
            path=c.get("path"),
            url=c.get("url"),
            json_query=c.get("json_query"),
            timeout_sec=timeout_sec,
            idempotent=idempotent,
            post_merge_skip=pms,
        ))

    afk = raw.get("afk_on_timeout")
    if afk is not None and afk not in VALID_AFK_ON_TIMEOUT:
        raise ContractError(
            f"afk_on_timeout must be one of {VALID_AFK_ON_TIMEOUT}, got {afk!r}"
        )

    # T1 R9: notification dict — defaults applied at parse time. throttle_min=0
    # is a sentinel for "no throttle, every event fires" (NOT "disabled").
    # tier2_enabled is the separate kill switch.
    notif_raw = raw.get("notification", {})
    if not isinstance(notif_raw, dict):
        raise ContractError("notification must be an object")
    # All three sub-fields routed through helpers — explicit null on
    # throttle_min/tier2_enabled rejects (typed int/bool); command IS
    # nullable (legitimate "no command" value, same as absent). Inlined
    # rather than going through optional_field so we can pass the dotted-path
    # key (`notification.foo`) for unambiguous error messages — short keys
    # like `command` collide with criterion-level fields otherwise.
    if "throttle_min" in notif_raw:
        throttle_min = non_negative_int(
            "notification.throttle_min", notif_raw["throttle_min"],
        )
    else:
        throttle_min = 5
    if "tier2_enabled" in notif_raw:
        tier2_enabled = bool_value(
            "notification.tier2_enabled", notif_raw["tier2_enabled"],
        )
    else:
        tier2_enabled = True
    if "command" in notif_raw:
        notification_command = nullable_str(
            "notification.command", notif_raw["command"],
        )
    else:
        notification_command = None
    notification = {
        "command": notification_command,
        "throttle_min": throttle_min,
        "tier2_enabled": tier2_enabled,
    }

    # T1 Q2.2: budget.max_codex_rounds_per_task default 3. N1: when explicitly
    # set, must be >= 1 — `0 rounds` is meaningless (it would mean "never call
    # codex", which is the wrong way to express that — disable codex hook
    # instead). throttle_min=0 IS meaningful (fire every event); they differ.
    # Codex round-6: parent budget routed through optional_field+dict_value
    # — explicit `budget: null` / `false` / `""` / `0` reject (mirrors
    # notification/scope handling); absent → empty dict, then
    # max_codex_rounds_per_task defaulted to 3 below.
    budget = dict(optional_field(raw, "budget", dict_value, {}))
    budget["max_codex_rounds_per_task"] = optional_field(
        budget, "max_codex_rounds_per_task", positive_int, 3,
    )

    # T1 R8: idempotent_cmd_allowlist default = built-in 8 entries. When user
    # overrides, must be a list of non-empty strings. optional_field routes
    # explicit null/non-list/empty-string-element through non_empty_str_list
    # → reject; absent → built-in default.
    idempotent_cmd_allowlist = optional_field(
        raw, "idempotent_cmd_allowlist", non_empty_str_list,
        list(DEFAULT_IDEMPOTENT_CMD_ALLOWLIST),
    )

    # v0.8.5 — dispatch observability + feedback enrichment switches.
    # PRD R5: independent on/off toggles, default both "on". Absent →
    # default applies. Explicit shape: ``{telemetry: on|off,
    # feedback_enrichment: on|off}``. Wrong type / unknown values
    # rejected per fail-closed posture (matches notification handling).
    dispatch_raw = optional_field(raw, "dispatch", dict_value, {})
    dispatch_cfg = {
        "telemetry": _validate_on_off(
            dispatch_raw, "telemetry", "on",
        ),
        "feedback_enrichment": _validate_on_off(
            dispatch_raw, "feedback_enrichment", "on",
        ),
    }

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
        budget=budget,
        acceptance_criteria=crits,
        idempotent_cmd_allowlist=idempotent_cmd_allowlist,
        post_merge_regression_optional=pmro,
        notification=notification,
        afk_timeout_min=raw.get("afk_timeout_min"),
        afk_on_timeout=afk,
        dispatch=dispatch_cfg,
        unknown_fields=unknown,
    )


def _validate_on_off(d: dict, key: str, default: str) -> str:
    """v0.8.5 — accept 'on' / 'off' strings; absent → default; bool
    True/False shorthand also accepted (translated to 'on'/'off').
    Anything else raises ContractError."""
    if key not in d:
        return default
    v = d[key]
    if isinstance(v, bool):
        return "on" if v else "off"
    if v == "on" or v == "off":
        return v
    raise ContractError(
        f"dispatch.{key} must be 'on' or 'off' (or boolean); got {v!r}"
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
            "max_codex_rounds_per_task": 3,
        },
        "acceptance_criteria": [],
        "notification": {"command": None, "throttle_min": 5, "tier2_enabled": True},
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
