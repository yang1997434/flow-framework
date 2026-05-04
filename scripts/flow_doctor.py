#!/usr/bin/env python3
"""flow doctor — environment consistency diagnostic.

Reads dependencies.json + ~/.claude/plugins/installed_plugins.json
+ ~/.claude/settings.json and reports a capability matrix.

Exit code:
  0 = all required deps satisfied
  1 = at least one required dep missing
  2 = settings.json hook isolation violated (Issue #415 risk)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPS_FILE = REPO_ROOT / "dependencies.json"
USER_SETTINGS = Path.home() / ".claude" / "settings.json"
INSTALLED_PLUGINS = Path.home() / ".claude" / "plugins" / "installed_plugins.json"

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


def line(symbol: str, color: str, label: str, detail: str = "") -> None:
    suffix = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"   {color}{symbol}{RESET} {label}{suffix}")


def ok(label: str, detail: str = "") -> None:
    line("✓", GREEN, label, detail)


def warn(label: str, detail: str = "") -> None:
    line("⚠", YELLOW, label, detail)


def fail(label: str, detail: str = "") -> None:
    line("✗", RED, label, detail)


def section(title: str) -> None:
    print(f"\n>> {title}")


def check_system_commands(deps: dict) -> int:
    section("System commands")
    missing_required = 0
    for entry in deps["system_commands"]["required"]:
        if shutil.which(entry["name"]):
            ok(entry["name"])
        else:
            fail(entry["name"], entry.get("hint", ""))
            missing_required += 1
    for entry in deps["system_commands"].get("optional", []):
        name = entry["name"]
        cap = entry.get("capability", "?")
        if shutil.which(name):
            ok(f"{name}", f"optional · capability: {cap}")
        else:
            warn(f"{name}", f"optional · capability: {cap} disabled")
    return missing_required


def check_plugins(deps: dict) -> int:
    section("Plugins")
    missing_required = 0

    installed = {}
    if INSTALLED_PLUGINS.is_file():
        try:
            data = json.loads(INSTALLED_PLUGINS.read_text(encoding="utf-8"))
            installed = data.get("plugins", {})
        except json.JSONDecodeError:
            warn("installed_plugins.json", "could not parse — Claude Code may not be configured yet")
            return 1

    for tier_name, required in (("required", True), ("optional", False)):
        for p in deps["plugins"].get(tier_name, []):
            spec = f"{p['name']}@{p['marketplace']}"
            entries = installed.get(spec, [])
            if entries:
                version = entries[0].get("version", "?")
                ok(f"{spec}", f"v{version}")
            else:
                if required:
                    fail(f"{spec}", "REQUIRED — run `flow install` to install")
                    missing_required += 1
                else:
                    warn(f"{spec}", "optional — capabilities disabled")
    return missing_required


def check_hook_isolation() -> int:
    """Check that flow hooks are in their own matcher entries (Issue #415 risk).

    Returns 0 if isolated, 1 if a flow hook shares a matcher entry with another command.
    """
    section("Hook isolation (Issue #415 mitigation)")

    if not USER_SETTINGS.is_file():
        warn("~/.claude/settings.json", "not found — hooks not installed")
        return 1

    try:
        settings = json.loads(USER_SETTINGS.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        fail("~/.claude/settings.json", "is not valid JSON")
        return 1

    hooks = settings.get("hooks", {})
    if not hooks:
        warn("settings.json", "no hooks configured")
        return 1

    repo_marker = str(REPO_ROOT)
    violations = 0
    flow_entries_seen = 0
    for event_name, entries in hooks.items():
        for entry in entries:
            commands = [h.get("command", "") for h in entry.get("hooks", [])]
            flow_cmds = [c for c in commands if repo_marker in c or "flow-framework" in c]
            non_flow_cmds = [c for c in commands if c not in flow_cmds]
            if flow_cmds:
                flow_entries_seen += 1
                if non_flow_cmds:
                    fail(
                        f"{event_name} matcher entry",
                        f"flow hook shares with: {', '.join(non_flow_cmds[:2])}",
                    )
                    violations += 1
                else:
                    ok(f"{event_name}", f"isolated · {len(flow_cmds)} flow command(s)")
    if not flow_entries_seen:
        warn("settings.json", "no flow hooks found — run `flow install`")
        return 1
    if violations:
        return 2
    return 0


def check_user_local_overrides(deps: dict) -> None:
    """Heuristic — point user to flow.config.local.yaml for capability overrides."""
    section("User overrides")
    local_cfg = Path.cwd() / ".flow" / "config.local.yaml"
    if local_cfg.is_file():
        ok(f"{local_cfg.relative_to(Path.cwd())}", "present (capability overrides may apply)")
    else:
        line("·", DIM, ".flow/config.local.yaml", "(none — using built-in defaults)")


def check_context_mode_running() -> None:
    """Non-blocking heuristic: is context-mode (Layer 1 raw persistence) live?

    Two signals (either is "good enough" — context-mode self-installs its
    own state on first run):
      1. The `context-mode` CLI is on PATH.
      2. `~/.context-mode/content/` directory exists (content store created).

    This is a non-blocking warning, not a fail. If Layer 1 is missing, flow's
    Layer 2 still works (we just won't have raw transcripts to feed a future
    LLM distill).
    """
    section("Context-mode (Layer 1 raw persistence)")
    cli_present = shutil.which("context-mode") is not None
    content_dir = Path.home() / ".context-mode" / "content"
    content_present = content_dir.is_dir()

    if cli_present and content_present:
        ok("context-mode", "CLI on PATH + content store present")
    elif cli_present:
        warn("context-mode", "CLI present but ~/.context-mode/content/ not yet created")
    elif content_present:
        warn("context-mode", "content store present but CLI missing — install incomplete")
    else:
        warn(
            "context-mode",
            "not detected — flow Layer-2 still works, raw transcripts won't be captured",
        )


def main():
    if not DEPS_FILE.is_file():
        print(f"{RED}ERROR: dependencies.json not found at {DEPS_FILE}{RESET}", file=sys.stderr)
        sys.exit(1)

    deps = json.loads(DEPS_FILE.read_text(encoding="utf-8"))

    print(f">> Flow Framework Doctor")
    print(f"   source: {REPO_ROOT}")

    missing_cmds = check_system_commands(deps)
    missing_plugins = check_plugins(deps)
    iso_status = check_hook_isolation()
    check_user_local_overrides(deps)
    check_context_mode_running()

    print()
    if missing_cmds == 0 and missing_plugins == 0 and iso_status == 0:
        print(f"{GREEN}>> All checks passed.{RESET}")
        sys.exit(0)
    if iso_status == 2:
        print(f"{RED}>> Hook isolation FAILED (Issue #415 risk).{RESET}")
        sys.exit(2)
    if missing_cmds + missing_plugins > 0:
        print(f"{RED}>> {missing_cmds + missing_plugins} required dep(s) missing.{RESET}")
        sys.exit(1)
    print(f"{YELLOW}>> Some optional checks emitted warnings.{RESET}")
    sys.exit(0)


if __name__ == "__main__":
    main()
