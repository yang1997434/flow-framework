#!/usr/bin/env python3
"""flow selftest — functional verification that the install actually works.

`flow doctor` checks STATIC state (files exist, JSON parses).
`flow selftest` checks DYNAMIC behavior:

  1. Each hook script accepts realistic stdin → exits 0 → emits valid JSON or empty
  2. `flow init` in a fresh temp dir produces the expected layout
  3. `flow task create` + `archive` round-trips correctly
  4. `claude plugin list` reports the required plugins (install actually applied)
  5. Delegates to `flow doctor` for static state recap

Exit code:
  0 = all passed (warnings allowed)
  1 = at least one functional check failed
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "claude" / "hooks"
DEPS_FILE = REPO_ROOT / "dependencies.json"

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"

failures: list[str] = []
warnings: list[str] = []


def section(title: str) -> None:
    print(f"\n>> {title}")


def ok(label: str, detail: str = "") -> None:
    suffix = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"   {GREEN}✓{RESET} {label}{suffix}")


def warn(label: str, detail: str = "") -> None:
    suffix = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"   {YELLOW}⚠{RESET} {label}{suffix}")
    warnings.append(label)


def fail(label: str, detail: str = "") -> None:
    suffix = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"   {RED}✗{RESET} {label}{suffix}")
    failures.append(label)


# --- 1. Hook dry-fire ---------------------------------------------------------

HOOK_FIXTURES: dict[str, dict] = {
    "session-start.py": {
        "stdin": {"event": "startup", "cwd": "/tmp"},
        "timeout": 10,
    },
    "user-prompt-submit.py": {
        "stdin": {"prompt": "hello world", "cwd": "/tmp"},
        "timeout": 5,
    },
    "pre-tool-task.py": {
        "stdin": {
            "tool_name": "Task",
            "tool_input": {"prompt": "implement a simple feature"},
            "cwd": "/tmp",
        },
        "timeout": 10,
    },
    "post-tool-bash.py": {
        "stdin": {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},  # not 'git commit' — should noop quickly
            "cwd": "/tmp",
        },
        "timeout": 15,
    },
    "post-tool-edit.py": {
        "stdin": {
            "tool_name": "Edit",
            # Non-existent path — hook bails early but exits 0 (fail-closed).
            "tool_input": {"file_path": "/tmp/flow-selftest-nonexistent.txt"},
            "cwd": "/tmp",
        },
        "timeout": 10,
    },
    "pre-compact.py": {
        # Hook expects cwd + transcript_path; minimal stdin → no active task
        # found → noop and exit 0 (fail-closed).
        "stdin": {"cwd": "/tmp"},
        "timeout": 10,
    },
    "stop.py": {
        "stdin": {"event": "stop", "cwd": "/tmp"},
        "timeout": 15,
    },
}


def check_hooks() -> None:
    section("Hook dry-fire (each hook receives synthetic stdin)")
    for name, fixture in HOOK_FIXTURES.items():
        script = HOOKS_DIR / name
        if not script.is_file():
            fail(name, "script missing")
            continue
        stdin_text = json.dumps(fixture["stdin"])
        try:
            result = subprocess.run(
                [sys.executable, str(script)],
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=fixture["timeout"],
            )
        except subprocess.TimeoutExpired:
            fail(name, f"timed out after {fixture['timeout']}s")
            continue
        if result.returncode != 0:
            fail(name, f"exit {result.returncode}: {(result.stderr or '').strip()[:120]}")
            continue
        # Output may be empty (noop) or valid JSON
        out = (result.stdout or "").strip()
        if out:
            try:
                json.loads(out)
            except json.JSONDecodeError as e:
                fail(name, f"non-JSON output: {str(e)[:80]}")
                continue
            ok(name, f"emitted JSON ({len(out)} bytes)")
        else:
            ok(name, "exit 0, no-op (expected)")


# --- 2. flow init in tmp ------------------------------------------------------

def check_flow_init() -> None:
    section("flow init in fresh temp project")
    with tempfile.TemporaryDirectory(prefix="flow-selftest-init-") as tmp:
        tmpdir = Path(tmp).resolve()
        try:
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "flow_init.py")],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            fail("flow init", "timed out")
            return
        if result.returncode != 0:
            fail("flow init", f"exit {result.returncode}: {(result.stderr or '').strip()[:120]}")
            return

        flow_dir = tmpdir / ".flow"
        expected = ["tasks", "tasks/archive", "ADRs", "patterns", "pitfalls", "workspace", ".runtime"]
        missing = [d for d in expected if not (flow_dir / d).is_dir()]
        if missing:
            fail("flow init", f"missing dirs: {', '.join(missing)}")
            return
        if not (flow_dir / "config.yaml").is_file():
            fail("flow init", "config.yaml missing")
            return
        ok("flow init", f"created {len(expected)} dirs + config.yaml")


# --- 3. task create + archive round-trip --------------------------------------

def check_task_roundtrip() -> None:
    section("flow task create + archive round-trip")
    with tempfile.TemporaryDirectory(prefix="flow-selftest-task-") as tmp:
        tmpdir = Path(tmp).resolve()

        env = os.environ.copy()
        env["PWD"] = str(tmpdir)

        def run(args: list[str]) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / "flow_task.py"), *args],
                cwd=tmpdir, env=env, capture_output=True, text=True, timeout=10,
            )

        # init first
        init = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "flow_init.py")],
            cwd=tmpdir, capture_output=True, text=True, timeout=15,
        )
        if init.returncode != 0:
            fail("task roundtrip", "flow init failed during setup")
            return

        # create
        r1 = run(["create", "Hello selftest", "--slug", "selftest-hello"])
        if r1.returncode != 0:
            fail("task create", (r1.stderr or "").strip()[:120])
            return
        if not list((tmpdir / ".flow" / "tasks").glob("*-selftest-hello")):
            fail("task create", "task dir not produced")
            return

        # archive
        r2 = run(["archive", "selftest-hello"])
        if r2.returncode != 0:
            fail("task archive", (r2.stderr or "").strip()[:120])
            return

        archives = list((tmpdir / ".flow" / "tasks" / "archive").rglob("*-selftest-hello"))
        if not archives:
            fail("task archive", "task not moved to archive/")
            return

        ok("task create + archive", f"round-trip OK ({archives[0].relative_to(tmpdir)})")


# --- 4. claude plugin list --------------------------------------------------

def check_plugins_actually_listed() -> None:
    section("claude plugin list (verifies install actually applied)")
    if not shutil.which("claude"):
        warn("claude CLI", "not in PATH — skipping plugin verification")
        return

    try:
        result = subprocess.run(
            ["claude", "plugin", "list"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        fail("claude plugin list", "timed out after 15s")
        return
    if result.returncode != 0:
        fail("claude plugin list", f"exit {result.returncode}")
        return

    output = result.stdout
    deps = json.loads(DEPS_FILE.read_text(encoding="utf-8"))
    required = deps["plugins"]["required"]

    for plugin in required:
        spec = f"{plugin['name']}@{plugin['marketplace']}"
        if spec in output or plugin["name"] in output:
            ok(spec, "installed")
        else:
            fail(spec, "REQUIRED plugin not listed by claude — install incomplete")


# --- 5. rendered prompt files have no leftover placeholders ----------------

def check_rendered_prompts() -> None:
    section("Rendered prompts (no leftover {{capability:X}} / {{model:Y}} / {{REPO_ROOT}} or stale ~/projects/flow-framework)")
    user_claude = Path.home() / ".claude"
    targets = [user_claude / "commands" / "flow", user_claude / "skills" / "flow"]

    found_any = False
    for root in targets:
        if not root.exists():
            warn(str(root.relative_to(Path.home())), "not present — run `flow install`")
            continue
        if root.is_symlink():
            fail(str(root.relative_to(Path.home())), "is a SYMLINK; install must render to real files (see Issue: symlink write-through)")
            continue
        for f in root.rglob("*"):
            if not f.is_file() or f.suffix not in (".md", ".yaml", ".yml"):
                continue
            found_any = True
            text = f.read_text(encoding="utf-8", errors="ignore")
            if "{{capability:" in text or "{{model:" in text:
                fail(str(f.relative_to(Path.home())), "leftover capability/model placeholder")
            elif "{{REPO_ROOT}}" in text:
                fail(str(f.relative_to(Path.home())), "leftover {{REPO_ROOT}} placeholder — render didn't substitute")
            elif "projects/flow-framework" in text:
                fail(str(f.relative_to(Path.home())), "stale ~/projects/flow-framework path — source file should use {{REPO_ROOT}}")
            else:
                ok(str(f.relative_to(Path.home())))
    if not found_any:
        warn("rendered prompts", "no files found — install hasn't rendered yet")


# --- 6. delegate to doctor for static recap ----------------------------------

def check_doctor() -> None:
    section("Doctor static recap")
    doctor = REPO_ROOT / "scripts" / "flow_doctor.py"
    if not doctor.is_file():
        warn("flow doctor", "not present")
        return
    try:
        result = subprocess.run(
            [sys.executable, str(doctor)],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        fail("flow doctor", "timed out")
        return
    code = result.returncode
    out = result.stdout or ""
    if code == 2:
        fail("flow doctor", "hook isolation FAILED (Issue #415 risk)")
        return
    if code == 1:
        fail("flow doctor", "required deps missing")
        return
    # exit 0 — distinguish all-clean vs has-warnings
    if "All checks passed" in out:
        ok("flow doctor", "all static checks pass")
    elif "warnings" in out.lower() or "no flow hooks found" in out:
        warn("flow doctor", "passes but with warnings (e.g., hooks not yet installed)")
    else:
        ok("flow doctor", "exit 0")


# --- main --------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Flow Framework self-test")
    parser.add_argument(
        "scope",
        nargs="?",
        choices=["hooks", "init", "task", "plugins", "rendered", "doctor", "all"],
        default="all",
    )
    args = parser.parse_args()

    print(f">> Flow Framework Self-Test")
    print(f"   source: {REPO_ROOT}")

    runners = {
        "hooks":    check_hooks,
        "init":     check_flow_init,
        "task":     check_task_roundtrip,
        "plugins":  check_plugins_actually_listed,
        "rendered": check_rendered_prompts,
        "doctor":   check_doctor,
    }
    if args.scope == "all":
        for fn in runners.values():
            fn()
    else:
        runners[args.scope]()

    print()
    if failures:
        print(f"{RED}>> Self-test FAILED: {len(failures)} failure(s){RESET}")
        for f in failures:
            print(f"   {RED}✗{RESET} {f}")
        sys.exit(1)
    if warnings:
        print(f"{YELLOW}>> Self-test passed with {len(warnings)} warning(s){RESET}")
        sys.exit(0)
    print(f"{GREEN}>> Self-test PASSED — install is functional.{RESET}")
    sys.exit(0)


if __name__ == "__main__":
    main()
