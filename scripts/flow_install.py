#!/usr/bin/env python3
"""flow install — declarative installer for the Flow Framework.

Reads dependencies.json and applies the requested install actions:

  flow_install.py check-system          # verify required system commands exist
  flow_install.py register-marketplaces # claude plugin marketplace add ...
  flow_install.py install-plugins       # claude plugin install plugin@marketplace
  flow_install.py install-hooks         # backup + merge settings.json
  flow_install.py all                   # do everything in order

All subcommands accept --dry-run to print actions without executing.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPS_FILE = REPO_ROOT / "dependencies.json"
HOOKS_TEMPLATE = REPO_ROOT / "claude" / "hooks" / "settings.template.json"
USER_SETTINGS = Path.home() / ".claude" / "settings.json"
USER_CLAUDE_DIR = Path.home() / ".claude"

# Prompt sources to render (relative to REPO_ROOT) → installed location
RENDER_TARGETS = [
    ("claude/commands/flow", USER_CLAUDE_DIR / "commands" / "flow"),
    ("claude/skills/flow",   USER_CLAUDE_DIR / "skills" / "flow"),
]

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


def load_deps() -> dict:
    if not DEPS_FILE.is_file():
        die(f"dependencies.json not found at {DEPS_FILE}")
    return json.loads(DEPS_FILE.read_text(encoding="utf-8"))


def die(msg: str, code: int = 1) -> None:
    print(f"{RED}ERROR: {msg}{RESET}", file=sys.stderr)
    sys.exit(code)


def info(msg: str) -> None:
    print(f"   {msg}")


def _format(label: str, detail: str = "") -> str:
    return f"{label}  {DIM}{detail}{RESET}" if detail else label


def ok(label: str, detail: str = "") -> None:
    print(f"   {GREEN}✓{RESET} {_format(label, detail)}")


def warn(label: str, detail: str = "") -> None:
    print(f"   {YELLOW}⚠{RESET}  {_format(label, detail)}")


def fail(label: str, detail: str = "") -> None:
    print(f"   {RED}✗{RESET} {_format(label, detail)}")


def run(cmd: list[str], dry_run: bool, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess | None:
    if dry_run:
        info(f"{DIM}[dry-run]{RESET} {' '.join(cmd)}")
        return None
    return subprocess.run(cmd, check=check, capture_output=True, text=True, env=env)


def _https_only_env() -> dict[str, str]:
    """Return os.environ + GIT_CONFIG_* vars that force git to rewrite
    git@github.com: → https://github.com/ for the subprocess only.

    Some `claude plugin marketplace add` invocations end up shelling to
    git with an SSH-form URL even when the user passed `owner/repo`
    shortcut. On machines without SSH keys (or strict known_hosts), the
    clone fails. GIT_CONFIG_COUNT/KEY/VALUE injects an `insteadOf` rule
    scoped to this subprocess — git 2.31+ supports it. Zero side effects
    on working installs (rewrite is a no-op when URL already HTTPS)."""
    env = os.environ.copy()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "url.https://github.com/.insteadOf"
    env["GIT_CONFIG_VALUE_0"] = "git@github.com:"
    return env


def cmd_check_system(args) -> int:
    deps = load_deps()
    print(f">> Check system commands")
    failed_required = 0
    for entry in deps["system_commands"]["required"]:
        name = entry["name"]
        if shutil.which(name):
            ok(f"{name}")
        else:
            fail(f"{name}  — {entry.get('hint', 'install required')}")
            failed_required += 1
    for entry in deps["system_commands"].get("optional", []):
        name = entry["name"]
        if shutil.which(name):
            ok(f"{name} (optional)")
        else:
            cap = entry.get("capability", "?")
            warn(f"{name} (optional, used by {cap}) — {entry.get('hint', '')}")

    if failed_required:
        die(f"{failed_required} required command(s) missing")
    return 0


def cmd_register_marketplaces(args) -> int:
    deps = load_deps()
    print(f">> Register marketplaces (via `claude plugin marketplace add`)")

    # Get already-known marketplaces to skip duplicates
    known: set[str] = set()
    if not args.dry_run:
        try:
            r = subprocess.run(
                ["claude", "plugin", "marketplace", "list"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    parts = line.split()
                    if parts:
                        known.add(parts[0].strip())
        except Exception:
            pass  # best-effort dedupe

    for mp in deps["marketplaces"]:
        name = mp["name"]
        source = mp["source"]
        if name in known:
            ok(f"{name}  ({DIM}already registered{RESET})")
            continue
        result = run(
            ["claude", "plugin", "marketplace", "add", source],
            dry_run=args.dry_run, check=False, env=_https_only_env(),
        )
        if args.dry_run:
            continue
        if result and result.returncode == 0:
            ok(f"{name} ← {source}")
        else:
            stderr = (result.stderr if result else "").strip()
            fail(f"{name} ← {source}  : {stderr or 'unknown error'}")
    return 0


def cmd_install_plugins(args) -> int:
    deps = load_deps()
    print(f">> Install plugins (via `claude plugin install`)")

    installed: set[str] = set()
    if not args.dry_run:
        try:
            r = subprocess.run(
                ["claude", "plugin", "list"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                # Each line typically like "plugin-name@marketplace  version"
                for line in r.stdout.splitlines():
                    tok = line.split()
                    if tok:
                        installed.add(tok[0].strip())
        except Exception:
            pass

    failed = 0
    for tier_name, required in (("required", True), ("optional", False)):
        for p in deps["plugins"].get(tier_name, []):
            spec = f"{p['name']}@{p['marketplace']}"
            if spec in installed:
                ok(f"{spec}  ({DIM}already installed{RESET})")
                continue
            result = run(
                ["claude", "plugin", "install", spec],
                dry_run=args.dry_run, check=False, env=_https_only_env(),
            )
            if args.dry_run:
                continue
            if result and result.returncode == 0:
                ok(f"{spec}")
            else:
                stderr = (result.stderr if result else "").strip()
                if required:
                    fail(f"{spec}  : {stderr or 'unknown error'}")
                    failed += 1
                else:
                    warn(f"{spec}  : {stderr or 'unknown error'}")
    if failed:
        die(f"{failed} required plugin(s) failed to install")
    return 0


def cmd_install_hooks(args) -> int:
    print(f">> Install hooks (merge into ~/.claude/settings.json)")

    if not HOOKS_TEMPLATE.is_file():
        die(f"hook template not found at {HOOKS_TEMPLATE}")

    template_text = HOOKS_TEMPLATE.read_text(encoding="utf-8")
    rendered_text = template_text.replace("{{REPO_ROOT}}", str(REPO_ROOT))
    rendered = json.loads(rendered_text)

    if args.dry_run:
        info(f"{DIM}[dry-run]{RESET} would merge {len(rendered.get('hooks', {}))} hook events into {USER_SETTINGS}")
        return 0

    USER_SETTINGS.parent.mkdir(parents=True, exist_ok=True)

    if USER_SETTINGS.is_file():
        try:
            existing = json.loads(USER_SETTINGS.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            die(
                f"~/.claude/settings.json is not valid JSON ({e}). "
                f"Fix it or move it aside before re-running install."
            )
        backup = USER_SETTINGS.with_suffix(
            f".json.flow-bak.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        backup.write_text(USER_SETTINGS.read_text(encoding="utf-8"), encoding="utf-8")
        ok(f"backed up settings.json → {backup.name}")
    else:
        existing = {}

    merged = merge_hooks(existing, rendered)
    USER_SETTINGS.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    ok(f"settings.json updated with {sum(len(v) for v in rendered.get('hooks', {}).values())} hook entries")
    return 0


FLOW_OWNED_MARKERS = (
    "flow-framework",
    "claude/hooks/session-start.py",
    "claude/hooks/user-prompt-submit.py",
    "claude/hooks/pre-tool-task.py",
    "claude/hooks/post-tool-bash.py",
    "claude/hooks/post-tool-edit.py",
    "claude/hooks/pre-compact.py",
    "claude/hooks/stop.py",
)


def _entry_is_flow_owned(entry: dict) -> bool:
    """Detect whether a hook entry was installed by flow.

    Identification is by command-string substring (path-independent), so
    re-installing flow from a different REPO_ROOT cleanly replaces older
    entries instead of accumulating duplicates.
    """
    for h in entry.get("hooks", []):
        cmd = h.get("command", "") or ""
        if any(marker in cmd for marker in FLOW_OWNED_MARKERS):
            return True
    return False


def merge_hooks(existing: dict, new: dict) -> dict:
    """Merge `new` hooks into `existing` settings.

    Each flow hook lives in its OWN matcher entry to satisfy context-mode
    Issue #415 requirement (sibling hooks must not share matcher groups).

    Strategy: drop any pre-existing flow-owned entries (path-independent
    marker match) and append the fresh `new` entries. User-installed hooks
    that flow doesn't own are preserved untouched. This keeps re-install
    idempotent across REPO_ROOT moves.
    """
    merged = dict(existing)
    new_hooks = new.get("hooks", {})
    if not new_hooks:
        return merged

    out_hooks = dict(existing.get("hooks", {}))

    for event_name, new_entries in new_hooks.items():
        existing_entries = out_hooks.get(event_name, [])
        # Strip any prior flow entries (independent of REPO_ROOT)
        kept = [e for e in existing_entries if not _entry_is_flow_owned(e)]
        out_hooks[event_name] = kept + list(new_entries)

    merged["hooks"] = out_hooks
    return merged


def _resolves_into_source(dst: Path, src_abs: Path) -> Path | None:
    """If `dst` (existing or not) ultimately resolves to a path inside `src_abs`,
    return the resolved location. Otherwise return None.

    Both args must be absolute — Path.resolve() on a relative `dst` would
    anchor it to cwd and silently mis-target. Walks parent dirs via
    .resolve() so a missing dst still gets meaningful resolution based on
    the deepest existing ancestor.
    """
    if not dst.is_absolute() or not src_abs.is_absolute():
        raise ValueError(f"_resolves_into_source requires absolute paths: dst={dst} src={src_abs}")
    try:
        resolved = dst.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved == src_abs or src_abs in resolved.parents:
        return resolved
    return None


def cmd_render_prompts(args) -> int:
    """Render every .md / .yaml prompt in RENDER_TARGETS through the capability
    registry, writing the rendered output to the user's ~/.claude/ tree.

    Safety: refuses to write any file whose resolved destination falls inside
    the source repo (would clobber the templates). Catches three cases:
      1. dst_root itself is a symlink into REPO_ROOT
      2. an intermediate dir under dst_root is a symlink into REPO_ROOT
      3. dst_file itself is a symlink into REPO_ROOT
    install.sh removes legacy top-level symlinks before invoking this.
    """
    print(f">> Render prompt templates → ~/.claude/")

    repo_abs = REPO_ROOT.resolve()

    # --- Safety: reject symlink-into-source dst roots (early, with clearer msg)
    for src_rel, dst_root in RENDER_TARGETS:
        if dst_root.is_symlink():
            resolved = _resolves_into_source(dst_root, repo_abs)
            if resolved is not None:
                die(
                    f"{dst_root} is a symlink to {resolved}, which would clobber the "
                    f"source templates. Run `rm {dst_root}` first (install.sh handles this)."
                )

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from flow_capability import load_registry, render

    registry = load_registry()
    total_files = 0
    total_errors = 0

    for src_rel, dst_root in RENDER_TARGETS:
        src_root = REPO_ROOT / src_rel
        if not src_root.is_dir():
            warn(f"{src_rel}", "source dir missing")
            continue

        for src_file in src_root.rglob("*"):
            if not src_file.is_file():
                continue
            if src_file.suffix not in (".md", ".yaml", ".yml", ".json"):
                continue
            rel_path = src_file.relative_to(src_root)
            dst_file = dst_root / rel_path

            # Per-file safety: refuse if dst_file resolves into REPO_ROOT
            # (covers nested-dir symlinks and per-file symlinks).
            danger = _resolves_into_source(dst_file, repo_abs)
            if danger is not None:
                die(
                    f"{dst_file} resolves to {danger} inside the source repo. "
                    f"Some symlink under {dst_root} points back into the templates; "
                    f"remove it before re-running install."
                )

            text = src_file.read_text(encoding="utf-8")
            # Substitute {{REPO_ROOT}} BEFORE delegating to flow_capability.render(),
            # which only handles {{capability:...}} and {{model:...}} placeholders.
            # Slash command prompts (e.g. flow/pause.md Steps 6-8) embed the repo
            # path into Python `sys.path.insert` lines so helpers under scripts/
            # can be imported at runtime; without this substitution the literal
            # token would land in ~/.claude/commands/... and the import would fail.
            text = text.replace("{{REPO_ROOT}}", str(repo_abs))
            rendered, errors = render(text, registry)

            if errors:
                total_errors += len(errors)
                fail(f"{src_rel}/{rel_path}", "; ".join(errors[:2]))
                continue

            if args.dry_run:
                info(f"{DIM}[dry-run]{RESET} would write {dst_file}")
            else:
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                dst_file.write_text(rendered, encoding="utf-8")
                total_files += 1

    if args.dry_run:
        return 0
    if total_errors:
        die(f"{total_errors} unresolved placeholder(s); render aborted")
    ok(f"rendered {total_files} files into {USER_CLAUDE_DIR}/{{commands,skills}}/flow/")
    return 0


def cmd_install_external_skills(args) -> int:
    """Install loose-skill bundles declared under deps['external_skills'].

    These are NOT marketplace plugins — they install by cloning a git repo
    into ~/.claude/skills/<name>/ and (optionally) running a build command.
    Required CLIs (e.g. bun) are checked first and treated as a hard skip
    if missing. Idempotent: skip if install_path already exists.
    """
    deps = load_deps()
    print(f">> Install external skill bundles")

    external = deps.get("external_skills", {})
    if not external:
        ok("external_skills", f"({DIM}none declared{RESET})")
        return 0

    failed = 0
    for tier_name, required in (("required", True), ("optional", False)):
        for entry in external.get(tier_name, []):
            name = entry["name"]
            install_path = Path(entry["install_path"]).expanduser()

            if install_path.is_dir():
                ok(f"{name}", f"({DIM}already installed at {install_path}{RESET})")
                continue

            install_cmd = entry.get("install", "")
            if not install_cmd:
                msg = "no install command specified in dependencies.json"
                if required:
                    fail(f"{name}", msg)
                    failed += 1
                else:
                    warn(f"{name}", msg)
                continue

            # Verify required CLIs before attempting install.
            cli_missing = [c for c in entry.get("requires_cli", []) if not shutil.which(c)]
            if cli_missing:
                msg = f"missing CLI: {', '.join(cli_missing)}"
                if required:
                    fail(f"{name}", msg)
                    failed += 1
                else:
                    warn(f"{name} (optional)", f"skipping — {msg}")
                continue

            print(f"   {DIM}running:{RESET} {install_cmd}")
            if args.dry_run:
                ok(f"{name}", f"{DIM}(dry-run){RESET}")
                continue

            try:
                result = subprocess.run(
                    install_cmd, shell=True, capture_output=True, text=True, timeout=900,
                )
            except subprocess.TimeoutExpired:
                if required:
                    fail(f"{name}", "install timed out (>15min)")
                    failed += 1
                else:
                    warn(f"{name} (optional)", "install timed out")
                continue

            if result.returncode == 0:
                ok(f"{name}", f"installed at {install_path}")
            else:
                stderr = (result.stderr or "").strip().splitlines()
                detail = stderr[-1] if stderr else f"exit {result.returncode}"
                if required:
                    fail(f"{name}", f"install failed: {detail[:200]}")
                    failed += 1
                else:
                    warn(f"{name} (optional)", f"install failed: {detail[:200]}")

    if failed:
        die(f"{failed} required external skill(s) failed to install")
    return 0


def cmd_all(args) -> int:
    print(f">> Flow Framework full install")
    print(f"   source: {REPO_ROOT}")
    print(f"   target: ~/.claude/")
    print()
    cmd_check_system(args)
    print()
    cmd_register_marketplaces(args)
    print()
    cmd_install_plugins(args)
    print()
    cmd_install_external_skills(args)
    print()
    cmd_install_hooks(args)
    print()
    cmd_render_prompts(args)
    print()
    print(f">> Install complete. Run `flow doctor` to verify.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Flow Framework installer")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="Print actions without executing")

    sub.add_parser("check-system", parents=[common]).set_defaults(func=cmd_check_system)
    sub.add_parser("register-marketplaces", parents=[common]).set_defaults(func=cmd_register_marketplaces)
    sub.add_parser("install-plugins", parents=[common]).set_defaults(func=cmd_install_plugins)
    sub.add_parser("install-external-skills", parents=[common]).set_defaults(func=cmd_install_external_skills)
    sub.add_parser("install-hooks", parents=[common]).set_defaults(func=cmd_install_hooks)
    sub.add_parser("render-prompts", parents=[common]).set_defaults(func=cmd_render_prompts)
    sub.add_parser("all", parents=[common]).set_defaults(func=cmd_all)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
