"""Config loading for Flow Framework."""
from __future__ import annotations

import re
from pathlib import Path

from .paths import get_flow_dir


def load_config(project_root: Path | None = None) -> dict:
    """Load .flow/config.yaml as dict. Minimal YAML parser (no PyYAML dep).

    Only handles: top-level keys, nested 1 level, lists of strings/numbers.
    For complex configs, the caller should re-parse with proper YAML.
    """
    flow = get_flow_dir(project_root)
    cfg_path = flow / "config.yaml"
    if not cfg_path.is_file():
        return {}

    return _parse_simple_yaml(cfg_path.read_text(encoding="utf-8"))


def _parse_simple_yaml(text: str) -> dict:
    """Minimal YAML parser for our config format. Not general-purpose."""
    result: dict = {}
    stack = [result]
    indent_stack = [-1]

    for line in text.splitlines():
        # Strip comments
        line = re.sub(r"#.*$", "", line).rstrip()
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        # Pop stack to current indent level
        while indent_stack and indent <= indent_stack[-1]:
            stack.pop()
            indent_stack.pop()

        if ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()

            if not val:
                # Nested
                new_dict: dict = {}
                if stack:
                    stack[-1][key] = new_dict
                stack.append(new_dict)
                indent_stack.append(indent)
            elif val.startswith("[") and val.endswith("]"):
                # Inline list
                items = [x.strip().strip('"').strip("'") for x in val[1:-1].split(",") if x.strip()]
                if stack:
                    stack[-1][key] = items
            else:
                # Simple value
                val = val.strip('"').strip("'")
                if val.lower() in ("true", "false"):
                    val_typed = val.lower() == "true"
                elif val.isdigit():
                    val_typed = int(val)
                else:
                    try:
                        val_typed = float(val)
                    except ValueError:
                        val_typed = val
                if stack:
                    stack[-1][key] = val_typed
        elif stripped.startswith("-"):
            # List item under a key (not handled deeply, just collect strings)
            item = stripped[1:].strip().strip('"').strip("'")
            if stack and isinstance(stack[-1], dict):
                # Find last key whose value should be a list
                # This minimal parser doesn't fully support — caller should YAML-parse for arrays
                pass

    return result


def get_machine_id_from_local() -> str | None:
    """Read machine_id from ~/.flow/credentials.local."""
    from .paths import get_global_flow_home
    cred = get_global_flow_home() / "credentials.local"
    if not cred.is_file():
        return None
    for line in cred.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("machine_id:"):
            val = line.split(":", 1)[1].strip().strip('"').strip("'")
            if val and not val.startswith("<"):
                return val
    return None
