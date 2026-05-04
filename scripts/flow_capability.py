#!/usr/bin/env python3
"""flow capability — resolver and template renderer.

Resolution chain (later overrides earlier):
  1. Built-in defaults     — claude/capabilities/defaults.json (this repo)
  2. User global override  — ~/.flow/config.yaml (capabilities + model_roles)
  3. Project override      — <project>/.flow/config.yaml (capabilities + model_roles)
  4. Project-local override — <project>/.flow/config.local.yaml

Template substitution:
  {{capability:NAME}}  → resolved skill identifier (e.g. "superpowers:brainstorming")
  {{capability:NAME.args.mode}}  → access nested arg field (e.g. "consult" for codex)
  {{capability:NAME.follow_with}}  → access optional follow-up skill
  {{model:ROLE}}       → concrete model id (e.g. "claude-sonnet-4-6")

CLI:
  flow_capability.py resolve <name>        # print resolved capability dict (JSON)
  flow_capability.py resolve-model <role>  # print resolved model id
  flow_capability.py render <file>         # print rendered text to stdout
  flow_capability.py render <file> -o out  # write rendered to <out>
  flow_capability.py list                  # list all known capabilities + roles
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_FILE = REPO_ROOT / "claude" / "capabilities" / "defaults.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from common.config import _parse_simple_yaml  # noqa: E402
from common.paths import get_flow_dir, get_global_flow_home  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{\{(capability|model):([a-z_][a-z0-9_]*)(?:\.([a-z_]+(?:\.[a-z_]+)*))?\}\}")


class CapabilityRegistry:
    """Holds the merged resolution chain."""

    def __init__(self, capabilities: dict, model_roles: dict, *, sources: list[str]):
        self.capabilities = capabilities
        self.model_roles = model_roles
        self.sources = sources

    def resolve_capability(self, name: str) -> dict:
        if name not in self.capabilities:
            raise KeyError(f"unknown capability: {name!r} (known: {sorted(self.capabilities)})")
        return self.capabilities[name]

    def resolve_model(self, role: str) -> str:
        if role not in self.model_roles:
            raise KeyError(f"unknown model role: {role!r} (known: {sorted(self.model_roles)})")
        entry = self.model_roles[role]
        if isinstance(entry, str):
            return entry
        return entry["default"]


def _load_json(p: Path) -> dict:
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _load_yaml_or_json(p: Path) -> dict:
    if not p.is_file():
        return {}
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".json":
        return json.loads(text)
    return _parse_simple_yaml(text)


def _deep_merge(base: dict, override: dict) -> dict:
    """Override wins. Lists and scalars are replaced (not merged)."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_registry(project_root: Path | None = None) -> CapabilityRegistry:
    """Build the capability registry from the resolution chain."""
    sources: list[str] = []

    base = _load_json(DEFAULTS_FILE)
    capabilities = dict(base.get("capabilities", {}))
    model_roles = dict(base.get("model_roles", {}))
    sources.append(str(DEFAULTS_FILE))

    # User global ~/.flow/config.yaml (capabilities + model_roles sections)
    global_cfg = get_global_flow_home() / "config.yaml"
    if global_cfg.is_file():
        cfg = _load_yaml_or_json(global_cfg)
        capabilities = _deep_merge(capabilities, cfg.get("capabilities", {}))
        model_roles = _deep_merge(model_roles, cfg.get("model_roles", {}))
        sources.append(str(global_cfg))

    # Project .flow/config.yaml
    if project_root is not None or get_flow_dir().is_dir():
        proj_cfg = get_flow_dir(project_root) / "config.yaml"
        if proj_cfg.is_file():
            cfg = _load_yaml_or_json(proj_cfg)
            capabilities = _deep_merge(capabilities, cfg.get("capabilities", {}))
            model_roles = _deep_merge(model_roles, cfg.get("model_roles", {}))
            sources.append(str(proj_cfg))

        # Project .flow/config.local.yaml (highest priority)
        local_cfg = get_flow_dir(project_root) / "config.local.yaml"
        if local_cfg.is_file():
            cfg = _load_yaml_or_json(local_cfg)
            capabilities = _deep_merge(capabilities, cfg.get("capabilities", {}))
            model_roles = _deep_merge(model_roles, cfg.get("model_roles", {}))
            sources.append(str(local_cfg))

    return CapabilityRegistry(capabilities, model_roles, sources=sources)


def _resolve_path(obj: dict, dotted: str | None):
    """Walk a dotted access path through a dict. Returns None if any segment missing."""
    if not dotted:
        return None
    cur = obj
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur


def render(text: str, registry: CapabilityRegistry) -> tuple[str, list[str]]:
    """Substitute {{capability:X}} and {{model:Y}} placeholders.

    Returns (rendered_text, errors). errors is a list of unresolved placeholders.
    On error, the placeholder is left in place so callers can spot it.
    """
    errors: list[str] = []

    def sub(match: re.Match) -> str:
        kind, name, dotted = match.group(1), match.group(2), match.group(3)
        try:
            if kind == "capability":
                cap = registry.resolve_capability(name)
                if dotted is None:
                    return cap.get("default", "")
                val = _resolve_path(cap, dotted)
                if val is None:
                    errors.append(f"capability '{name}' has no path '{dotted}'")
                    return match.group(0)
                if isinstance(val, dict):
                    errors.append(f"capability '{name}.{dotted}' resolves to dict, expected scalar")
                    return match.group(0)
                return str(val)
            if kind == "model":
                return registry.resolve_model(name)
        except KeyError as e:
            errors.append(str(e))
            return match.group(0)
        return match.group(0)

    return PLACEHOLDER_RE.sub(sub, text), errors


# --- CLI -----------------------------------------------------------------------

def cmd_resolve(args) -> int:
    reg = load_registry()
    try:
        cap = reg.resolve_capability(args.name)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps(cap, indent=2, ensure_ascii=False))
    return 0


def cmd_resolve_model(args) -> int:
    reg = load_registry()
    try:
        print(reg.resolve_model(args.role))
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


def cmd_render(args) -> int:
    reg = load_registry()
    text = Path(args.input).read_text(encoding="utf-8")
    rendered, errors = render(text, reg)
    if errors:
        for e in errors:
            print(f"WARNING: {e}", file=sys.stderr)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0 if not errors else 2


def cmd_list(args) -> int:
    reg = load_registry()
    print("# capabilities")
    for k in sorted(reg.capabilities):
        v = reg.capabilities[k]
        default = v.get("default", "(no default)") if isinstance(v, dict) else v
        print(f"  {k:30s}  → {default}")
    print("\n# model_roles")
    for k in sorted(reg.model_roles):
        print(f"  {k:30s}  → {reg.resolve_model(k)}")
    print(f"\n# resolution chain (later overrides earlier)")
    for s in reg.sources:
        print(f"  - {s}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Flow capability resolver/renderer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_res = sub.add_parser("resolve")
    p_res.add_argument("name")
    p_res.set_defaults(func=cmd_resolve)

    p_rm = sub.add_parser("resolve-model")
    p_rm.add_argument("role")
    p_rm.set_defaults(func=cmd_resolve_model)

    p_render = sub.add_parser("render")
    p_render.add_argument("input")
    p_render.add_argument("-o", "--output")
    p_render.set_defaults(func=cmd_render)

    p_list = sub.add_parser("list")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
