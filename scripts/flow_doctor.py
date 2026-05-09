#!/usr/bin/env python3
"""flow doctor — environment consistency diagnostic.

Reads dependencies.json + ~/.claude/plugins/installed_plugins.json
+ ~/.claude/settings.json and reports a capability matrix.

Usage:
  flow doctor                      — run all checks
  flow doctor --suggest-writes <slug>  — advisory writes: suggestions for a task

Exit code:
  0 = all required deps satisfied
  1 = at least one required dep missing
  2 = settings.json hook isolation violated (Issue #415 risk)
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from common.exit_codes import USAGE_ERROR  # v0.8.4 P3

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


def _safe_print_str(s: object, max_len: int = 200) -> str:
    """[Codex round-1 P2 R-class] Escape disk-derived strings before
    printing to terminal. Strips ANSI escape sequences and other control
    bytes that a malicious worktree name / slug / contract field could
    inject (e.g. `\\x1b[31m...` to recolor doctor output, or BEL/OSC
    sequences). Truncates to `max_len`.

    Same pattern as the T16 OSC 9 sanitizer applied to notification
    payloads — extends it to the doctor output layer.
    """
    if not isinstance(s, str):
        return repr(s)[:max_len]
    safe = "".join(
        ch for ch in s
        if (0x20 <= ord(ch) < 0x7f) or ch == "\t"
    )
    return safe[:max_len]


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


def check_external_skills(deps: dict) -> int:
    """Diagnose external skill bundles (loose-skill installs not in the
    marketplace+plugin system). Reports presence of install_path and
    availability of each required CLI declared on the entry.

    Returns count of REQUIRED entries missing.
    """
    external = deps.get("external_skills", {})
    if not external:
        return 0  # silent — nothing declared
    section("External skills (loose-skill bundles)")
    missing_required = 0
    for tier_name, required in (("required", True), ("optional", False)):
        for entry in external.get(tier_name, []):
            name = entry["name"]
            install_path = Path(entry["install_path"]).expanduser()
            present = install_path.is_dir()

            cli_missing = [c for c in entry.get("requires_cli", []) if not shutil.which(c)]
            caps = ", ".join(entry.get("capabilities", []))
            tier_label = "" if required else " (optional)"

            if present and not cli_missing:
                ok(f"{name}{tier_label}", f"path: {install_path} · capabilities: {caps}")
            elif present and cli_missing:
                msg = f"path present but missing CLI: {', '.join(cli_missing)}"
                if required:
                    fail(f"{name}", msg)
                    missing_required += 1
                else:
                    warn(f"{name}{tier_label}", msg)
            else:
                hint = f"not at {install_path} — `flow install install-external-skills` (or see dependencies.json)"
                if required:
                    fail(f"{name}", hint)
                    missing_required += 1
                else:
                    warn(f"{name}{tier_label}", hint)
    return missing_required


def _is_flow_command(cmd: str, repo_marker: str) -> bool:
    """Heuristic — a hook command belongs to flow if its path is under
    REPO_ROOT or it explicitly mentions the framework name."""
    return repo_marker in cmd or "flow-framework" in cmd


def _entry_owners(entry: dict, repo_marker: str) -> tuple[list[str], list[str]]:
    """Split an entry's `hooks[].command` strings into (flow_cmds, non_flow_cmds)."""
    commands = [h.get("command", "") for h in entry.get("hooks", [])]
    flow = [c for c in commands if _is_flow_command(c, repo_marker)]
    non_flow = [c for c in commands if not _is_flow_command(c, repo_marker)]
    return flow, non_flow


def check_hook_isolation() -> int:
    """Check that flow hooks are isolated per Issue #415.

    Two violation classes are detected:
      A. Intra-entry: a single matcher entry's `hooks` list mixes flow and
         non-flow commands.
      B. Cross-entry sibling: multiple entries under the same (event, matcher)
         key — flow + non-flow co-resident under the same matcher group still
         executes together and triggers the bug.

    Returns 0 if isolated, 1 if no flow hooks found / settings missing,
    2 if any class A or B violation was detected.
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

    # --- Pass A: intra-entry mixing ---
    for event_name, entries in hooks.items():
        for entry in entries:
            flow_cmds, non_flow_cmds = _entry_owners(entry, repo_marker)
            if not flow_cmds:
                continue
            flow_entries_seen += 1
            if non_flow_cmds:
                fail(
                    f"{event_name} matcher entry",
                    f"flow hook shares with: {', '.join(non_flow_cmds[:2])}",
                )
                violations += 1
            else:
                ok(f"{event_name}", f"isolated · {len(flow_cmds)} flow command(s)")

    # --- Pass B: cross-entry siblings under the same matcher ---
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for event_name, entries in hooks.items():
        for entry in entries:
            matcher = entry.get("matcher", "")
            grouped[(event_name, matcher)].append(entry)

    for (event_name, matcher), entries in grouped.items():
        if len(entries) < 2:
            continue
        has_flow = any(_entry_owners(e, repo_marker)[0] for e in entries)
        has_non_flow = any(_entry_owners(e, repo_marker)[1] for e in entries)
        if has_flow and has_non_flow:
            disp = matcher if matcher else "<empty=all tools>"
            non_flow_cmds = [
                c[:60]
                for e in entries
                for c in _entry_owners(e, repo_marker)[1]
            ]
            hint = (
                f"non-flow neighbour(s): {', '.join(non_flow_cmds[:2])}. "
                f"Per Issue #415, move them to a different matcher (e.g. "
                f"split 'Bash' into 'Bash|Write' for one) or drop them if unused."
            )
            fail(
                f"{event_name}[{disp}] sibling entries",
                f"{len(entries)} entries share matcher; flow + non-flow co-resident — {hint}",
            )
            violations += 1

    if not flow_entries_seen:
        warn("settings.json", "no flow hooks found — run `flow install`")
        return 1
    if violations:
        return USAGE_ERROR
    return 0


def check_user_local_overrides(deps: dict) -> None:
    """Heuristic — point user to flow.config.local.yaml for capability overrides."""
    section("User overrides")
    local_cfg = Path.cwd() / ".flow" / "config.local.yaml"
    if local_cfg.is_file():
        ok(f"{local_cfg.relative_to(Path.cwd())}", "present (capability overrides may apply)")
    else:
        line("·", DIM, ".flow/config.local.yaml", "(none — using built-in defaults)")


def _context_mode_plugin_enabled() -> bool:
    """Read ~/.claude/settings.json safely and return True iff
    enabledPlugins["context-mode@context-mode"] is truthy.

    context-mode ships as a Claude Code plugin (no `context-mode` CLI on PATH,
    no `~/.context-mode/content/` directory until first hook fire), so we
    treat the settings.json entry as the authoritative "installed" signal.
    """
    settings = Path.home() / ".claude" / "settings.json"
    if not settings.is_file():
        return False
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return False
    return bool(plugins.get("context-mode@context-mode"))


def check_context_mode_running() -> None:
    """Non-blocking heuristic: is context-mode (Layer 1 raw persistence) live?

    Three signals (any one is "good enough" — context-mode self-installs its
    own state on first hook fire):
      1. enabledPlugins["context-mode@context-mode"] in ~/.claude/settings.json
         (authoritative — context-mode is a Claude Code plugin).
      2. The `context-mode` CLI is on PATH.
      3. `~/.context-mode/content/` directory exists (content store created).

    This is a non-blocking warning, not a fail. If Layer 1 is missing, flow's
    Layer 2 still works (we just won't have raw transcripts to feed a future
    LLM distill).
    """
    section("Context-mode (Layer 1 raw persistence)")
    plugin_enabled = _context_mode_plugin_enabled()
    cli_present = shutil.which("context-mode") is not None
    content_dir = Path.home() / ".context-mode" / "content"
    content_present = content_dir.is_dir()

    if plugin_enabled:
        ok("context-mode", "Claude Code plugin installed (enabledPlugins)")
    elif cli_present and content_present:
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


def _is_dependency_available(name: str) -> bool:
    """Check whether a capability dependency is available.

    `requires_cli` in capability entries has mixed semantics — some values
    name actual PATH binaries (e.g. `codex`), others name skill bundles
    installed under `~/.claude/skills/<name>/` (e.g. `gstack`). Check both
    locations: skill bundle dir first (cheap), then PATH binary fallback.
    """
    skill_bundle = Path.home() / ".claude" / "skills" / name
    if skill_bundle.is_dir():
        return True
    return shutil.which(name) is not None


def check_capability_clis() -> None:
    """Check that each capability's `requires_cli` is available.

    Walks the capability registry, collects every entry that declares
    `requires_cli`, and warns if the named dependency is missing. This is a
    warning, not a failure — capabilities marked `skip_if_not_available: true`
    degrade gracefully at render time, but the user benefits from knowing
    which capabilities will silently no-op so they're not surprised when,
    e.g., Phase 3's `quality_health` step does nothing.

    Pre-existing in v0.5: `cross_model_*` capabilities had `requires_cli` but
    nothing consumed it. v0.6.1 closes that gap (issue #10).
    """
    section("Capability CLI requirements")
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        from flow_capability import load_registry  # noqa: E402
    except ImportError:
        warn("capability registry", "could not import flow_capability — skipping check")
        return

    reg = load_registry()
    missing: dict[str, list[str]] = defaultdict(list)
    checked = 0
    for cap_name, cap in reg.capabilities.items():
        cli = cap.get("requires_cli")
        if not cli:
            continue
        clis = cli if isinstance(cli, list) else [cli]
        for c in clis:
            checked += 1
            if not _is_dependency_available(c):
                missing[c].append(cap_name)

    if not missing:
        ok("all capability deps available", f"{checked} requires_cli entries checked")
        return

    for dep, caps in sorted(missing.items()):
        preview = ", ".join(caps[:3]) + ("..." if len(caps) > 3 else "")
        warn(
            f"{dep} not available (PATH binary or ~/.claude/skills/{dep}/)",
            f"affects {len(caps)} capability/ies: {preview}",
        )


def _find_project_root_from_cwd(start: Path | None = None) -> Path | None:
    """Walk up from cwd looking for a .flow/ directory. Returns None if not
    found (caller should treat as 'no contract checks to run')."""
    here = (start or Path.cwd()).resolve()
    for p in [here, *here.parents]:
        if (p / ".flow").is_dir():
            return p
    return None


def check_contract_integrity() -> tuple[bool, list[str]]:
    """For every .flow/tasks/<slug>/ in the *user's project* (cwd-walked),
    if progress.md sets autonomy_mode, check contract.json exists, parses,
    and schema_version is known.
    """
    project_root = _find_project_root_from_cwd()
    if project_root is None:
        return True, []  # Not in a Flow project — nothing to check.

    _scripts = str(REPO_ROOT / "scripts")
    _common = str(REPO_ROOT / "scripts" / "common")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)
    if _common not in sys.path:
        sys.path.insert(0, _common)
    from flow_contract import parse_contract, ContractError, CONTRACT_SCHEMA_VERSION  # noqa: E402
    from progress_meta import read_progress_meta  # noqa: E402

    issues: list[str] = []
    tasks_dir = project_root / ".flow" / "tasks"
    if not tasks_dir.is_dir():
        return True, issues
    for slug_dir in sorted(tasks_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name == "archive":
            continue
        progress = slug_dir / "progress.md"
        if not progress.is_file():
            continue
        meta = read_progress_meta(progress)
        contract_path = slug_dir / (meta.contract_path or "contract.json")
        if meta.autonomy_mode == "interactive" and not contract_path.is_file():
            continue  # No contract is fine for interactive.
        if not contract_path.is_file():
            issues.append(f"{slug_dir.name}: autonomy_mode={meta.autonomy_mode} "
                          f"but {contract_path.name} missing")
            continue
        try:
            c = parse_contract(contract_path)
        except ContractError as e:
            issues.append(f"{slug_dir.name}: contract parse failed: {e}")
            continue
        if c.contract_schema_version > CONTRACT_SCHEMA_VERSION:
            issues.append(
                f"{slug_dir.name}: contract_schema_version "
                f"{c.contract_schema_version} > flow {CONTRACT_SCHEMA_VERSION}"
            )
    return (not issues), issues


def check_wave_plans() -> int:
    """v0.7: check progress.md files for writes: hygiene."""
    section("Wave-dispatch plans")
    _scripts = str(REPO_ROOT / "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)
    from flow_wave_planner import parse_plan_tasks, load_shared_artifacts  # noqa: E402

    _common = str(REPO_ROOT / "scripts" / "common")
    if _common not in sys.path:
        sys.path.insert(0, _common)
    from glob_overlap import is_broad_glob, validate_glob, GlobError, globs_overlap  # noqa: E402

    tasks_dir = REPO_ROOT / ".flow" / "tasks"
    if not tasks_dir.is_dir():
        ok("no .flow/tasks/ — skipping")
        return 0

    issues = 0
    shared = load_shared_artifacts()

    for slug_dir in sorted(tasks_dir.iterdir()):
        if not slug_dir.is_dir():
            continue
        progress = slug_dir / "progress.md"
        if not progress.is_file():
            continue
        try:
            tasks = parse_plan_tasks(progress.read_text(encoding="utf-8"))
        except Exception as e:
            warn(f"{slug_dir.name}: malformed `### Tasks` block — {e}")
            issues += 1
            continue
        if not tasks:
            continue  # legacy single-task plan, fine

        for t in tasks:
            if t.writes is None:
                warn(f"{slug_dir.name}/{t.id}: missing `writes:` (will be strict serial)")
                continue
            for g in t.writes:
                try:
                    validate_glob(g)
                except GlobError as e:
                    fail(f"{slug_dir.name}/{t.id}: invalid writes glob {g!r} — {e}")
                    issues += 1
            if shared and globs_overlap(t.writes, shared):
                warn(f"{slug_dir.name}/{t.id}: writes overlaps SHARED_ARTIFACTS — wave will be forced serial")

    if issues:
        return 1
    ok("all `### Tasks` blocks pass hygiene")
    return 0


def check_staleness() -> int:
    """T20 (v0.8.1) — task workspace staleness diagnostic.

    For every active task with a worktree on disk, run all 5 Y2 triggers
    via `StalenessChecker.check_all(include_baseline=True)` and surface
    the verdict. v0.8.1 ships this as the **only** staleness surface —
    `_cmd_auto_execute` / `auto_dispatch_task` integration is DEFERRED
    to v0.8.2 per route-A re-scope (codex round-4 R4 + round-5 R3).

    Active task discovery (v0.8.1 minimal): a task is "active" if
    `.flow/tasks/<slug>/contract.json` exists AND there is a matching
    `.claude/worktrees/<slug>+t*+<shortsha>` directory. Tasks without
    either are silently skipped (interactive-only / archived).

    Returns 0 always (warnings only — staleness is informational; the
    operator decides whether to abort `--auto-execute`). R-class
    (frontmatter injection): the `repr()` form is used when echoing
    user-controlled file paths into stdout to avoid raw control
    characters from filenames affecting terminal state.
    """
    section("Staleness (v0.8.1 doctor-only — orchestrator integration deferred to v0.8.2)")

    project_root = _find_project_root_from_cwd()
    if project_root is None:
        ok("no Flow project at cwd — skipping")
        return 0
    tasks_dir = project_root / ".flow" / "tasks"
    if not tasks_dir.is_dir():
        ok("no .flow/tasks/ — skipping")
        return 0

    _scripts = str(REPO_ROOT / "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)
    try:
        from flow_staleness import StalenessChecker  # noqa: E402
    except (ImportError, ModuleNotFoundError) as e:
        warn(f"could not import StalenessChecker: {type(e).__name__}: {e}")
        return 0

    worktrees_root = project_root / ".claude" / "worktrees"
    active_count = 0
    stale_count = 0

    for slug_dir in sorted(tasks_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name == "archive":
            continue
        contract_path = slug_dir / "contract.json"
        if not contract_path.is_file():
            continue
        # Find matching worktree directory: <slug>+t*+<shortsha>.
        slug = slug_dir.name
        candidates: list[Path] = []
        if worktrees_root.is_dir():
            for child in worktrees_root.iterdir():
                if not child.is_dir():
                    continue
                if child.name.startswith(f"{slug}+t"):
                    candidates.append(child)
        if not candidates:
            continue  # no live worktree — interactive task, skip
        # Pick the most recently modified worktree as the "current"
        # active one. Multiple worktrees per slug is uncommon; doctor
        # informs about the most recent.
        try:
            wt = max(candidates, key=lambda p: p.stat().st_mtime)
        except (OSError, PermissionError):
            continue

        # Read contract for integration_target + baseline_command. Also
        # pull `original_base_commit` if persisted (v0.8.1 may not have
        # it stored — fall back to current rev as a best-effort).
        try:
            contract_data = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            warn(
                f"{_safe_print_str(slug)}: contract.json unreadable — "
                f"{type(e).__name__}: {e}"
            )
            continue

        # L-class guard (code-review [85]): defend every `.get()` chain
        # below with isinstance checks. A corrupt contract — top-level
        # array/string/number, or `"baseline"` field that is itself a
        # non-dict — would otherwise raise AttributeError mid-doctor and
        # crash the run before `check_contract_integrity()` + summary.
        # Same defensive pattern flow_staleness.py uses internally;
        # this extends it to the doctor wire-up that reads the contract.
        if not isinstance(contract_data, dict):
            warn(
                f"{_safe_print_str(slug)}: "
                f"contract.json top-level is not a dict; skipping"
            )
            continue

        integration_target = contract_data.get("integration_target")
        if not isinstance(integration_target, str) or not integration_target:
            integration_target = "master"

        baseline_command = contract_data.get("baseline_command")
        if not isinstance(baseline_command, str):
            legacy = contract_data.get("baseline")
            baseline_command = (
                legacy.get("command") if isinstance(legacy, dict) else ""
            )
            if not isinstance(baseline_command, str):
                baseline_command = ""

        # Resolve original_base_commit: prefer the value stored in the
        # task dir (T10 records it via auto_engaged event); fall back
        # to current HEAD of integration_target so trigger 1 reports
        # "no change" rather than firing falsely.
        original_base = ""
        decisions = slug_dir / "decisions.jsonl"
        if decisions.is_file():
            try:
                for line_text in decisions.read_text(encoding="utf-8").splitlines():
                    if not line_text.strip():
                        continue
                    try:
                        rec = json.loads(line_text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    obc = rec.get("original_base_commit")
                    if isinstance(obc, str) and obc:
                        original_base = obc
                        break
            except (OSError, PermissionError):
                pass
        if not original_base:
            try:
                proc = subprocess.run(
                    ["git", "-C", str(project_root), "rev-parse",
                     integration_target],
                    capture_output=True, text=True, check=False,
                )
                if proc.returncode == 0:
                    original_base = proc.stdout.strip()
            except (OSError, FileNotFoundError):
                pass

        class _Ctx:
            pass
        ctx = _Ctx()
        ctx.integration_target = integration_target
        ctx.original_base_commit = original_base
        ctx.worktree_path = wt

        active_count += 1
        checker = StalenessChecker(
            repo_root=project_root, ctx=ctx, task_dir=slug_dir,
            baseline_snapshot={
                # v0.8.1 doctor-only: no persisted snapshot yet
                # (orchestrator capture deferred to v0.8.2). Doctor
                # reports against an empty snapshot — triggers 2/3/4
                # only fire if the file is present (treated as added)
                # which is the conservative default for "you have not
                # captured a baseline yet".
                "lockfiles": {},
                "prd_mtime": 0.0,
                "dep_versions": {},
            },
        )
        verdict = checker.check_all(
            include_baseline=bool(baseline_command),
            baseline_command=baseline_command,
            baseline_was_passing=True,
            baseline_timeout_sec=300,
        )
        # R-class: every disk-derived string (slug, wt.name,
        # integration_target, contract field strings) is run through
        # `_safe_print_str` before terminal output to strip ANSI / BEL
        # / control bytes a malicious worktree dir name or slug could
        # carry.
        slug_safe = _safe_print_str(slug)
        wt_name_safe = _safe_print_str(wt.name)
        if verdict.stale:
            stale_count += 1
            warn(
                f"{slug_safe}: STALE — triggers: "
                f"{', '.join(verdict.triggered)}",
                detail=f"worktree: {wt_name_safe}",
            )
            for trig, det in verdict.details.items():
                if not isinstance(det, dict):
                    continue
                # If detector skipped (empty-snapshot path), surface
                # that explicitly so the operator knows v0.8.1
                # doctor-only mode is the reason there's no signal.
                if "skipped" in det:
                    print(
                        f"      {trig}: skipped — "
                        f"{_safe_print_str(det.get('skipped', ''))}"
                    )
                    continue
                # [P3 codex round-3]: when one trigger fires stale but
                # another non-stale trigger returned `{"reason": ...}`
                # (e.g. baseline timed out / spawn failed / no command
                # configured), surface the reason instead of falling
                # through to the typed renderer below — without this,
                # `baseline_fail` w/ reason would mis-render as
                # "exit_code=?". Round-2 fix-pass made the aggregator
                # propagate `reason` on the not-stale path, but doctor
                # only consumed `skipped`; this finishes the chain.
                if "reason" in det:
                    print(
                        f"      {trig}: not stale — "
                        f"{_safe_print_str(det.get('reason', ''))}"
                    )
                    continue
                if trig == "base_branch":
                    target_safe = _safe_print_str(
                        det.get("integration_target", "?")
                    )
                    print(f"      base_branch: "
                          f"{det.get('from_commit', '?')[:7]} → "
                          f"{det.get('to_commit', '?')[:7]} "
                          f"({target_safe})")
                elif trig in ("lockfile", "dep_version"):
                    changed = det.get("changed", [])
                    print(
                        f"      {trig}: "
                        f"{', '.join(_safe_print_str(x) for x in changed)}"
                    )
                elif trig == "prd_mtime":
                    print(f"      prd_mtime: snapshot="
                          f"{det.get('snapshot_mtime', 0):.0f} "
                          f"current={det.get('current_mtime', 0):.0f}")
                elif trig == "baseline_fail":
                    print(f"      baseline_fail: exit_code="
                          f"{det.get('exit_code', '?')}")
        else:
            ok(
                f"{slug_safe}: clean",
                detail=f"worktree: {wt_name_safe}",
            )
            # Surface any "skipped" trigger details even on the clean
            # branch so the operator can tell whether triggers 2/3/4
            # actually ran or skipped due to the v0.8.1 empty-snapshot
            # mode (codex round-1 P2 visibility).
            #
            # [P3 codex round-3]: also surface `reason` details. The
            # round-2 fix-pass made check_all propagate both `skipped`
            # and `reason` keys through the aggregator, but doctor's
            # render loop only consumed `skipped`, so reasons like
            # "baseline timed out (inconclusive)", "baseline spawn
            # failed", "no baseline_command configured", and
            # "could not import _run_shell_with_pgkill helper" stayed
            # invisible — the aggregator propagation was dead code.
            # Both keys go through `_safe_print_str` because the
            # `reason` string may carry subprocess output (R-class).
            for trig, det in verdict.details.items():
                if not isinstance(det, dict):
                    continue
                if "skipped" in det:
                    print(
                        f"      {trig}: skipped — "
                        f"{_safe_print_str(det.get('skipped', ''))}"
                    )
                elif "reason" in det:
                    print(
                        f"      {trig}: not stale — "
                        f"{_safe_print_str(det.get('reason', ''))}"
                    )

    if active_count == 0:
        ok("no active task worktrees — nothing to check")
    elif stale_count == 0:
        ok(f"all {active_count} active task(s) clean")
    return 0


def check_wave_caches() -> int:
    """v0.7: detect stale wave-decomposition caches."""
    section("Wave caches")
    tasks_dir = REPO_ROOT / ".flow" / "tasks"
    if not tasks_dir.is_dir():
        return 0
    stale = 0
    _scripts = str(REPO_ROOT / "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)
    from flow_wave_planner import PLANNER_VERSION  # noqa: E402

    for cache_file in tasks_dir.glob("*/wave-decomposition.json"):
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            warn(f"{cache_file.parent.name}: malformed cache, will recompute")
            stale += 1
            continue
        cached_v = data.get("planner_version", "0.0.0")
        if cached_v != PLANNER_VERSION:
            warn(f"{cache_file.parent.name}: cache planner_version={cached_v} differs from current {PLANNER_VERSION}")
            stale += 1
    if stale == 0:
        ok("no stale wave caches")
    return 0


def cli_suggest_writes(slug: str) -> int:
    """Heuristic suggestions for missing `writes:` fields based on task description."""
    progress_path = REPO_ROOT / ".flow" / "tasks" / slug / "progress.md"
    if not progress_path.is_file():
        print(f"  no progress.md for {slug}")
        return 1

    _scripts = str(REPO_ROOT / "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)
    from flow_wave_planner import parse_plan_tasks  # noqa: E402

    tasks = parse_plan_tasks(progress_path.read_text(encoding="utf-8"))

    print(f"  {slug}: advisory writes: suggestions (NOT auto-written)")
    print(f"  Read prd.md and progress.md context manually before adding any.")
    for t in tasks:
        if t.writes is not None:
            continue  # already declared
        # Heuristic: extract file-path-looking strings from description
        matches = re.findall(
            r"([a-zA-Z0-9_./\-]+\.(?:py|md|json|sh|ts|tsx|js|jsx|yaml|yml))",
            t.description,
        )
        if matches:
            print(f"    {t.id}: suggested writes (review!): {matches[:5]}")
        else:
            print(f"    {t.id}: no obvious paths in description — author must declare manually")
    return 0


def main():
    args = sys.argv[1:]

    # --suggest-writes <slug> mode — advisory only, no full doctor run
    if len(args) >= 2 and args[0] == "--suggest-writes":
        slug = args[1]
        sys.exit(cli_suggest_writes(slug))

    if not DEPS_FILE.is_file():
        print(f"{RED}ERROR: dependencies.json not found at {DEPS_FILE}{RESET}", file=sys.stderr)
        sys.exit(1)

    deps = json.loads(DEPS_FILE.read_text(encoding="utf-8"))

    print(f">> Flow Framework Doctor")
    print(f"   source: {REPO_ROOT}")

    missing_cmds = check_system_commands(deps)
    missing_plugins = check_plugins(deps)
    missing_external = check_external_skills(deps)
    iso_status = check_hook_isolation()
    check_user_local_overrides(deps)
    check_context_mode_running()
    check_capability_clis()
    wave_plan_issues = check_wave_plans()
    check_wave_caches()
    # T20 (v0.8.1) — staleness diagnostic; non-fatal (informational).
    check_staleness()

    contract_ok, contract_errs = check_contract_integrity()
    section("Contract integrity")
    if contract_ok:
        ok("OK — all task contracts pass integrity checks")
    else:
        for e in contract_errs:
            warn(e)
        # Contract issues are non-fatal in v0.8.0 (autonomy execution disabled
        # anyway). Mark as warning only — don't bump the overall failure code
        # unless caller wants strict mode (v0.8.1+ may tighten).

    total_missing = missing_cmds + missing_plugins + missing_external
    print()
    if total_missing == 0 and iso_status == 0 and wave_plan_issues == 0:
        print(f"{GREEN}>> All checks passed.{RESET}")
        sys.exit(0)
    if iso_status == 2:
        print(f"{RED}>> Hook isolation FAILED (Issue #415 risk).{RESET}")
        sys.exit(USAGE_ERROR)
    if total_missing > 0:
        print(f"{RED}>> {total_missing} required dep(s) missing.{RESET}")
        sys.exit(1)
    print(f"{YELLOW}>> Some optional checks emitted warnings.{RESET}")
    sys.exit(0)


if __name__ == "__main__":
    main()
