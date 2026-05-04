# Auto-Resume v0.5.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship v0.5.0 of Flow Framework — foundation + manual flow hardening for surviving Claude Code auto-compact without losing in-flight intent. No autopilot (deferred to v0.6.0). Provides per-task `.checkpoint/` files, atomic-write / file-lock safety, append-only hint outbox, PreCompact hook, enriched `/flow:pause` and `/flow:resume`, best-effort context-pressure nudge.

**Architecture:** Three new utility modules under `scripts/common/` (atomic I/O, hint outbox queue, context-pressure estimator), one new hook (`pre-compact.py`), extensions to two existing hooks (`session-start.py`, `post-tool-bash.py`, `post-tool-edit.py`), enrichments to two existing slash commands (`pause.md`, `resume.md`), and one settings-template entry. Per-task ephemeral state lives at `<task>/.checkpoint/{intent.md, mechanical.json, history.jsonl}`. Cross-conversation cascade hint at `~/.flow/.runtime/hints/<ts>-<seq>.json` (append-only outbox).

**Tech Stack:** Python 3.10+ stdlib only (matching existing project constraint). `fcntl` for file locking (POSIX). `subprocess.Popen` for fire-and-forget. `os.replace` for atomic rename. Tests with stdlib `unittest`.

**Scope check:** Single subsystem (auto-resume infrastructure for Flow). All tasks land in one branch + one release. Spec at `docs/specs/2026-05-04-auto-resume-design.md`.

---

## File Structure

| Path | Action | Purpose |
|------|--------|---------|
| `scripts/common/safe_io.py` | Create | Atomic write + fcntl.flock helpers |
| `scripts/common/hint_outbox.py` | Create | Hint outbox queue (write/list/mark_processed) |
| `scripts/common/context_estimator.py` | Create | Model detection + context % estimation |
| `scripts/common/checkpoint_paths.py` | Create | Per-task `.checkpoint/` path resolution + dir bootstrap |
| `scripts/common/mechanical.py` | Create | Build mechanical.json payload from existing data sources |
| `claude/hooks/pre-compact.py` | Create | PreCompact hook — write mechanical.json, append history |
| `claude/hooks/post-tool-bash.py` | Modify | Add nudge injection + throttled mechanical.json update |
| `claude/hooks/post-tool-edit.py` | Modify | Add nudge injection + throttled mechanical.json update |
| `claude/hooks/session-start.py` | Modify | On `compact` matcher, inject resume context from .checkpoint/ |
| `claude/hooks/settings.template.json` | Modify | Add PreCompact entry (own matcher per Issue #415) |
| `claude/commands/flow/pause.md` | Modify | Add steps to write intent.md + outbox hint |
| `claude/commands/flow/resume.md` | Modify | Add reading of .checkpoint/ + staleness assessment |
| `scripts/flow_init.py` | Modify | Propagate `.checkpoint/` to project `.gitignore` |
| `scripts/flow_install.py` | Modify | Add new hook to `FLOW_OWNED_MARKERS` tuple |
| `.gitignore` | Modify | Add `.flow/tasks/*/.checkpoint/` for this very repo |
| `CHANGELOG.md` | Modify | v0.5.0 release notes |
| `VERSION` | Modify | Bump to 0.5.0 |
| `tests/smoke/test_v05_safe_io.py` | Create | Unit tests for atomic writes + locks |
| `tests/smoke/test_v05_hint_outbox.py` | Create | Unit tests for outbox queue |
| `tests/smoke/test_v05_context_estimator.py` | Create | Unit tests for context % estimation |
| `tests/smoke/test_v05_mechanical.py` | Create | Unit tests for mechanical payload builder |
| `tests/smoke/test_v05_precompact_hook.py` | Create | Hook integration tests with mock stdin |
| `tests/smoke/test_v05_sessionstart_compact.py` | Create | SessionStart compact-matcher injection tests |
| `tests/smoke/test_v05_postool_nudge.py` | Create | Nudge injection logic tests |

---

## Phase A — Foundational Utilities (no inter-dependencies; can be parallelized)

### Task 1: `safe_io.py` — atomic write helpers

**Files:**
- Create: `scripts/common/safe_io.py`
- Test: `tests/smoke/test_v05_safe_io.py`

**Why this task:** Every state file written by v0.5+ must be atomic (no partial writes visible to a reader). Codex review identified single-file overwrites as a class of bugs. This module is the foundation.

- [ ] **Step 1: Write the failing test for `atomic_write_text`**

Create `tests/smoke/test_v05_safe_io.py`:

```python
#!/usr/bin/env python3
"""Smoke tests for v0.5 safe_io — atomic writes + fcntl.flock."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class AtomicWriteText(unittest.TestCase):
    def test_writes_file_with_content(self):
        from common.safe_io import atomic_write_text
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "out.txt"
            atomic_write_text(p, "hello\n")
            self.assertEqual(p.read_text(), "hello\n")

    def test_overwrite_is_atomic_no_temp_left_behind(self):
        from common.safe_io import atomic_write_text
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "out.txt"
            atomic_write_text(p, "v1\n")
            atomic_write_text(p, "v2\n")
            self.assertEqual(p.read_text(), "v2\n")
            tmp_files = [f for f in Path(tmp).iterdir() if f.name != "out.txt"]
            self.assertEqual(tmp_files, [], f"stray temp files: {tmp_files}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run test, confirm it fails (module missing)**

```bash
cd /data/Claude/flow-framework
python3 -m unittest tests.smoke.test_v05_safe_io.AtomicWriteText -v
```
Expected: `ModuleNotFoundError: No module named 'common.safe_io'`

- [ ] **Step 3: Implement `atomic_write_text` (+ stub for json + jsonl)**

Create `scripts/common/safe_io.py`:

```python
"""Atomic file writes + fcntl.flock helpers for v0.5+ state files.

All state files (intent.md, mechanical.json, autopilot-state.json,
nudge-state.json, hint files, history.jsonl) MUST go through these helpers.
Ad-hoc `open(path, 'w').write(...)` is banned in flow code paths that
write state observable across processes.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    """Write content to path atomically. Either old content or new content
    is observable; never a partial file. Uses POSIX rename semantics.

    Caller's responsibility: parent dir must exist.
    """
    path = Path(path)
    parent = path.parent
    # Temp file in same dir to guarantee same filesystem (rename is atomic
    # only within a filesystem boundary).
    tmp_fd, tmp_path = _mkstemp_in(parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)  # POSIX atomic rename
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any, indent: int = 2) -> None:
    """Atomic JSON write with stable indent + trailing newline."""
    text = json.dumps(obj, ensure_ascii=False, indent=indent) + "\n"
    atomic_write_text(path, text)


def append_jsonl_locked(path: Path, record: dict, timeout_s: float = 2.0) -> bool:
    """Append one JSON record as a single line, holding fcntl.flock LOCK_EX.

    Returns True on success, False if the lock could not be acquired within
    timeout_s. Caller should treat False as "audit gap, log to stderr,
    proceed". File is created if missing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return True


def _mkstemp_in(dir_: Path, prefix: str, suffix: str) -> tuple[int, str]:
    """Wrapper around tempfile.mkstemp pinned to a specific dir."""
    import tempfile
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(dir_))
    return fd, name
```

- [ ] **Step 4: Run text-write tests, confirm pass**

```bash
python3 -m unittest tests.smoke.test_v05_safe_io.AtomicWriteText -v
```
Expected: 2/2 PASS.

- [ ] **Step 5: Add tests for `atomic_write_json`**

Append to `tests/smoke/test_v05_safe_io.py`:

```python
class AtomicWriteJson(unittest.TestCase):
    def test_writes_valid_json(self):
        from common.safe_io import atomic_write_json
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "out.json"
            atomic_write_json(p, {"a": 1, "b": [1, 2, 3]})
            data = json.loads(p.read_text())
            self.assertEqual(data, {"a": 1, "b": [1, 2, 3]})

    def test_trailing_newline(self):
        from common.safe_io import atomic_write_json
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "out.json"
            atomic_write_json(p, {"x": 1})
            self.assertTrue(p.read_text().endswith("\n"))
```

- [ ] **Step 6: Run, confirm pass**

```bash
python3 -m unittest tests.smoke.test_v05_safe_io.AtomicWriteJson -v
```
Expected: 2/2 PASS.

- [ ] **Step 7: Add concurrent-append test for `append_jsonl_locked`**

Append to `tests/smoke/test_v05_safe_io.py`:

```python
class AppendJsonlLocked(unittest.TestCase):
    def test_simple_append(self):
        from common.safe_io import append_jsonl_locked
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "log.jsonl"
            self.assertTrue(append_jsonl_locked(p, {"a": 1}))
            self.assertTrue(append_jsonl_locked(p, {"a": 2}))
            lines = p.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0]), {"a": 1})
            self.assertEqual(json.loads(lines[1]), {"a": 2})

    def test_concurrent_appends_no_interleave(self):
        from common.safe_io import append_jsonl_locked
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "log.jsonl"
            N_THREADS = 8
            N_PER_THREAD = 25

            def worker(tid):
                for i in range(N_PER_THREAD):
                    append_jsonl_locked(p, {"tid": tid, "i": i})

            threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            lines = p.read_text().splitlines()
            self.assertEqual(len(lines), N_THREADS * N_PER_THREAD)
            for ln in lines:
                obj = json.loads(ln)
                self.assertIn("tid", obj)
                self.assertIn("i", obj)
```

- [ ] **Step 8: Run, confirm pass**

```bash
python3 -m unittest tests.smoke.test_v05_safe_io -v
```
Expected: 5/5 PASS, no JSON parse errors (no interleaving).

- [ ] **Step 9: Commit**

```bash
git add scripts/common/safe_io.py tests/smoke/test_v05_safe_io.py
git commit -m "feat(v0.5): add safe_io — atomic writes + fcntl.flock

- atomic_write_text: temp file + fsync + os.replace (POSIX-atomic rename)
- atomic_write_json: serialize + atomic_write_text
- append_jsonl_locked: fcntl.flock LOCK_EX with timeout, no interleaving
  under concurrent appends (verified with 8 threads × 25 lines test)

Foundation for v0.5 state files. Required by codex pre-merge review of
the auto-resume design — single-file overwrites without locking were
flagged as a class of bugs."
```

---

### Task 2: `hint_outbox.py` — append-only hint queue

**Files:**
- Create: `scripts/common/hint_outbox.py`
- Test: `tests/smoke/test_v05_hint_outbox.py`

**Why this task:** Replaces the original "single hint file" design (codex flagged as lossy under concurrent writes). One hint file per pause event. Consumer (personal `/save`) moves processed hints to a sibling `processed/` dir.

- [ ] **Step 1: Write the failing test**

Create `tests/smoke/test_v05_hint_outbox.py`:

```python
#!/usr/bin/env python3
"""Smoke tests for v0.5 hint_outbox — append-only hint queue."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class HintOutbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("FLOW_HOME")
        os.environ["FLOW_HOME"] = self._tmp.name
        # Re-import to pick up FLOW_HOME
        for m in list(sys.modules):
            if m.startswith("common.hint_outbox"):
                del sys.modules[m]

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("FLOW_HOME", None)
        else:
            os.environ["FLOW_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_write_creates_file_in_hints_dir(self):
        from common.hint_outbox import write_hint
        path = write_hint({"task_slug": "abc", "phase": "phase-2"})
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.name, "hints")
        data = json.loads(path.read_text())
        self.assertEqual(data["task_slug"], "abc")

    def test_list_pending_returns_only_unprocessed(self):
        from common.hint_outbox import write_hint, list_pending, mark_processed
        p1 = write_hint({"task_slug": "a"})
        p2 = write_hint({"task_slug": "b"})
        self.assertEqual(set(list_pending()), {p1, p2})
        mark_processed(p1)
        self.assertEqual(list_pending(), [p2])

    def test_two_hints_in_same_second_get_unique_filenames(self):
        from common.hint_outbox import write_hint
        p1 = write_hint({"x": 1})
        p2 = write_hint({"x": 2})
        self.assertNotEqual(p1.name, p2.name)

    def test_mark_processed_moves_into_processed_subdir(self):
        from common.hint_outbox import write_hint, mark_processed
        p = write_hint({"x": 1})
        mark_processed(p)
        self.assertFalse(p.exists())
        moved = (p.parent / "processed" / p.name)
        self.assertTrue(moved.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run, confirm fails**

```bash
python3 -m unittest tests.smoke.test_v05_hint_outbox -v
```
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `hint_outbox.py`**

Create `scripts/common/hint_outbox.py`:

```python
"""Append-only outbox for cascade hints from /flow:pause to L3 (personal /save).

Layout:
  ~/.flow/.runtime/hints/                     ← pending hint files
                  /hints/processed/           ← consumed hint files

Each hint is a separate JSON file (no shared single-file race). Filename:
  <ISO8601-with-seconds>-<seq>.json

Consumer (personal /save) calls list_pending(), processes each, then
mark_processed(path) — moves it under processed/.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .safe_io import atomic_write_json


def _runtime_dir() -> Path:
    home = os.environ.get("FLOW_HOME")
    base = Path(home) if home else Path.home() / ".flow"
    rt = base / ".runtime"
    rt.mkdir(parents=True, exist_ok=True)
    return rt


def _hints_dir() -> Path:
    d = _runtime_dir() / "hints"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _processed_dir() -> Path:
    d = _hints_dir() / "processed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_hint(payload: dict) -> Path:
    """Write a hint file with a unique filename. Returns the path."""
    payload = dict(payload)
    payload.setdefault("schema_version", 1)
    payload.setdefault("ts", datetime.now().astimezone().isoformat(timespec="seconds"))
    base = payload["ts"].replace(":", "").replace("+", "p").replace("-", "")
    seq = 0
    while True:
        fname = f"{base}-{seq:03d}.json"
        path = _hints_dir() / fname
        if not path.exists():
            atomic_write_json(path, payload)
            return path
        seq += 1


def list_pending() -> list[Path]:
    """Return all *.json files directly under hints/ (NOT processed/)."""
    d = _hints_dir()
    return sorted(p for p in d.glob("*.json") if p.is_file())


def mark_processed(hint_path: Path) -> None:
    """Move hint into processed/ subdir."""
    target = _processed_dir() / hint_path.name
    os.replace(hint_path, target)
```

- [ ] **Step 4: Run, confirm 4/4 pass**

```bash
python3 -m unittest tests.smoke.test_v05_hint_outbox -v
```
Expected: 4/4 PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/common/hint_outbox.py tests/smoke/test_v05_hint_outbox.py
git commit -m "feat(v0.5): add hint_outbox — append-only hint queue

write_hint / list_pending / mark_processed. One file per hint event,
filename keyed by ISO8601 timestamp + seq counter for sub-second collisions.
Replaces the original 'single hint file' design which codex flagged as
lossy under concurrent personal-save / autopilot-bail writes.

FLOW_HOME env var supported for test isolation."
```

---

### Task 3: `context_estimator.py` — coarse context % from transcript

**Files:**
- Create: `scripts/common/context_estimator.py`
- Test: `tests/smoke/test_v05_context_estimator.py`

**Why this task:** Hooks need a coarse "we're in the danger zone" signal. transcript_path file size ÷ 4 ≈ tokens, divided by model's context limit gives a percentage. Acknowledged-coarse: ±20% real-world error margin per spec.

- [ ] **Step 1: Write failing tests**

Create `tests/smoke/test_v05_context_estimator.py`:

```python
#!/usr/bin/env python3
"""Smoke tests for v0.5 context_estimator."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class EstimatePct(unittest.TestCase):
    def test_returns_none_low_for_missing_file(self):
        from common.context_estimator import estimate_context_pct
        pct, conf = estimate_context_pct("/nonexistent/path/transcript.jsonl")
        self.assertIsNone(pct)
        self.assertEqual(conf, "low")

    def test_returns_none_low_for_unreadable(self):
        from common.context_estimator import estimate_context_pct
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            p.write_text("")
            pct, conf = estimate_context_pct(p)
            # Empty file → 0% but confidence low (no model detected)
            self.assertEqual(pct, 0)
            self.assertEqual(conf, "low")

    def test_known_size_with_default_model_limit(self):
        from common.context_estimator import estimate_context_pct
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            # Write 100KB of fake JSONL with a model field
            line = json.dumps({"model": "claude-sonnet-4-6", "x": "a" * 200}) + "\n"
            with p.open("w") as f:
                while p.stat().st_size < 100_000:
                    f.write(line)
            pct, conf = estimate_context_pct(p)
            # 100KB / 4 = 25k tokens; sonnet limit 200k → ~12-13%
            self.assertIsNotNone(pct)
            self.assertGreaterEqual(pct, 10)
            self.assertLessEqual(pct, 20)
            self.assertIn(conf, ("medium", "high"))

    def test_opus_1m_uses_1m_limit(self):
        from common.context_estimator import estimate_context_pct
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            line = json.dumps({"model": "claude-opus-4-7[1m]", "x": "a" * 200}) + "\n"
            with p.open("w") as f:
                while p.stat().st_size < 100_000:
                    f.write(line)
            pct, _conf = estimate_context_pct(p)
            # 100KB / 4 = 25k tokens; 1M limit → 2-3%
            self.assertIsNotNone(pct)
            self.assertLessEqual(pct, 5)

    def test_unknown_model_falls_back_to_200k(self):
        from common.context_estimator import estimate_context_pct
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            line = json.dumps({"x": "no model field at all"}) + "\n"
            with p.open("w") as f:
                while p.stat().st_size < 100_000:
                    f.write(line)
            pct, conf = estimate_context_pct(p)
            self.assertEqual(conf, "low")  # no model → low confidence
            self.assertIsNotNone(pct)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m unittest tests.smoke.test_v05_context_estimator -v
```
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `context_estimator.py`**

Create `scripts/common/context_estimator.py`:

```python
"""Coarse context % estimator from a Claude Code transcript_path.

Hook input includes `transcript_path` — the JSONL file backing the active
conversation. We approximate token count as `file_size_bytes / 4` and
divide by the model's context limit. Confidence levels reflect ambiguity:

  high   — file >= 10 KB AND model identified
  medium — file readable AND model identified, but small
  low    — file unreadable OR model unknown OR estimator unsure

Caller MUST treat (None, 'low') as 'skip this trigger' (do not false-fire).
This is a coarse trigger for nudges, NOT a safety boundary — actual context
fill may diverge by ±20% due to JSON metadata, tool payload escaping, etc.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Tuple

# Model context window sizes (tokens). Update as new models ship.
MODEL_LIMITS: dict[str, int] = {
    "claude-opus-4-7": 200_000,
    "claude-opus-4-7[1m]": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}
DEFAULT_LIMIT = 200_000

# How many JSONL lines from the head of the file to scan for a model field.
MODEL_DETECT_HEAD_LINES = 20


def estimate_context_pct(transcript_path) -> Tuple[Optional[int], str]:
    """Return (pct, confidence). pct in [0, 100] or None on hard failure."""
    p = Path(transcript_path)
    if not p.is_file():
        return (None, "low")

    try:
        size_bytes = p.stat().st_size
    except OSError:
        return (None, "low")

    model = _detect_model(p)
    limit = MODEL_LIMITS.get(model, DEFAULT_LIMIT) if model else DEFAULT_LIMIT
    estimated_tokens = size_bytes / 4
    pct = min(100, max(0, round(estimated_tokens / limit * 100)))

    if model is None:
        confidence = "low"
    elif size_bytes >= 10_000:
        confidence = "high"
    else:
        confidence = "medium"

    return (pct, confidence)


def _detect_model(path: Path) -> Optional[str]:
    """Scan the head of the JSONL for a 'model' field. None if not found."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= MODEL_DETECT_HEAD_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = _extract_model_field(obj)
                if m:
                    return m
    except OSError:
        return None
    return None


def _extract_model_field(obj) -> Optional[str]:
    """Recursively look for a 'model' field in an object."""
    if isinstance(obj, dict):
        if "model" in obj and isinstance(obj["model"], str):
            return obj["model"]
        for v in obj.values():
            r = _extract_model_field(v)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _extract_model_field(item)
            if r:
                return r
    return None
```

- [ ] **Step 4: Run, confirm 5/5 pass**

```bash
python3 -m unittest tests.smoke.test_v05_context_estimator -v
```
Expected: 5/5 PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/common/context_estimator.py tests/smoke/test_v05_context_estimator.py
git commit -m "feat(v0.5): add context_estimator — coarse context % from transcript

Reads transcript_path file size, divides by 4 for rough token count,
divides by model's context limit (1M for Opus 4.7-1M, 200k otherwise).
Recursively scans first 20 lines for a 'model' field. Returns confidence
high/medium/low so callers can decide to skip on uncertainty.

Acknowledged-coarse: ±20% real-world error margin per spec. Used only
as a nudge trigger, NOT as a safety boundary."
```

---

### Task 4: `checkpoint_paths.py` — per-task `.checkpoint/` resolution

**Files:**
- Create: `scripts/common/checkpoint_paths.py`
- Test: covered indirectly by later tasks (no dedicated test file — pure path helper)

**Why this task:** Centralize path math. Avoid scattered `task_dir / ".checkpoint" / "intent.md"` literals throughout hooks.

- [ ] **Step 1: Implement (no test — too thin)**

Create `scripts/common/checkpoint_paths.py`:

```python
"""Per-task .checkpoint/ path resolution for v0.5+ state files."""
from __future__ import annotations

from pathlib import Path


def checkpoint_dir(task_dir: Path) -> Path:
    """Return <task>/.checkpoint/, creating it if missing."""
    d = Path(task_dir) / ".checkpoint"
    d.mkdir(parents=True, exist_ok=True)
    return d


def intent_path(task_dir: Path) -> Path:
    return checkpoint_dir(task_dir) / "intent.md"


def mechanical_path(task_dir: Path) -> Path:
    return checkpoint_dir(task_dir) / "mechanical.json"


def history_path(task_dir: Path) -> Path:
    return checkpoint_dir(task_dir) / "history.jsonl"


def autopilot_state_path(task_dir: Path) -> Path:
    """v0.6 only — return path even if file doesn't exist yet."""
    return checkpoint_dir(task_dir) / "autopilot-state.json"
```

- [ ] **Step 2: Sanity-import the module**

```bash
python3 -c "
import sys
sys.path.insert(0, 'scripts')
from common.checkpoint_paths import intent_path, mechanical_path, history_path
import tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as tmp:
    t = Path(tmp)
    assert intent_path(t).name == 'intent.md'
    assert mechanical_path(t).name == 'mechanical.json'
    assert history_path(t).name == 'history.jsonl'
    print('OK')
"
```
Expected: prints `OK`.

- [ ] **Step 3: Commit**

```bash
git add scripts/common/checkpoint_paths.py
git commit -m "feat(v0.5): add checkpoint_paths — per-task .checkpoint/ helpers

Centralizes <task>/.checkpoint/{intent.md, mechanical.json, history.jsonl,
autopilot-state.json} path math so hooks and slash commands don't
scatter literal strings."
```

---

### Task 5: `mechanical.py` — build mechanical.json payload

**Files:**
- Create: `scripts/common/mechanical.py`
- Test: `tests/smoke/test_v05_mechanical.py`

**Why this task:** Both PreCompact hook and PostToolUse extension need to write `mechanical.json`. Extract into one helper so they don't drift.

- [ ] **Step 1: Write failing test**

Create `tests/smoke/test_v05_mechanical.py`:

```python
#!/usr/bin/env python3
"""Smoke tests for v0.5 mechanical payload builder."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _has_git() -> bool:
    return shutil.which("git") is not None


@unittest.skipUnless(_has_git(), "git not available")
class BuildMechanicalPayload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-mech-"))
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "test")
        env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
        env.setdefault("GIT_COMMITTER_NAME", "test")
        env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
        (self.tmp / "README").write_text("hi\n")
        subprocess.run(["git", "-C", str(self.tmp), "add", "."], check=True,
                       env=env, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.tmp), "commit", "-q", "-m", "init"],
                       check=True, env=env, stdout=subprocess.DEVNULL)
        self.task_dir = self.tmp / ".flow" / "tasks" / "01-01-test"
        self.task_dir.mkdir(parents=True)
        (self.task_dir / "progress.md").write_text(
            "---\nphase: phase-2-execute\nstatus: active\n---\n", encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_payload_has_required_top_level_fields(self):
        from common.mechanical import build_payload
        payload = build_payload(
            project_root=self.tmp,
            task_dir=self.task_dir,
            trigger="precompact",
            transcript_path=None,
        )
        for k in ("schema_version", "ts", "trigger", "task_slug", "phase", "git",
                  "files_touched_recent", "context_pct_estimated",
                  "transcript_path_size_bytes"):
            self.assertIn(k, payload)
        self.assertEqual(payload["trigger"], "precompact")
        self.assertEqual(payload["task_slug"], "01-01-test")
        self.assertEqual(payload["git"]["branch"], "main")
        self.assertTrue(payload["git"]["head"])
        self.assertIsInstance(payload["git"]["recent_commits"], list)
        self.assertGreaterEqual(len(payload["git"]["recent_commits"]), 1)
        self.assertEqual(payload["phase"], "phase-2-execute")

    def test_phase_extracted_from_progress_frontmatter(self):
        from common.mechanical import build_payload
        (self.task_dir / "progress.md").write_text(
            "---\nphase: phase-3-finish\n---\n", encoding="utf-8"
        )
        payload = build_payload(
            project_root=self.tmp, task_dir=self.task_dir,
            trigger="post-tool", transcript_path=None,
        )
        self.assertEqual(payload["phase"], "phase-3-finish")

    def test_no_progress_md_returns_phase_unknown(self):
        from common.mechanical import build_payload
        (self.task_dir / "progress.md").unlink()
        payload = build_payload(
            project_root=self.tmp, task_dir=self.task_dir,
            trigger="post-tool", transcript_path=None,
        )
        self.assertEqual(payload["phase"], "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m unittest tests.smoke.test_v05_mechanical -v
```
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `mechanical.py`**

Create `scripts/common/mechanical.py`:

```python
"""Build the mechanical.json payload from existing data sources.

Used by PreCompact hook AND PostToolUse extension so the two paths produce
identical schemas. Zero LLM cost — pure data extraction.
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from .context_estimator import estimate_context_pct

SCHEMA_VERSION = 1
RECENT_COMMITS_LIMIT = 5
RECENT_FILES_LIMIT = 10

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def build_payload(
    project_root: Path,
    task_dir: Path,
    trigger: str,
    transcript_path: Optional[str | Path],
    recent_files: Optional[list[str]] = None,
) -> dict:
    """Compose mechanical state. `trigger` is e.g. 'precompact' or 'post-tool'."""
    pct, conf = estimate_context_pct(transcript_path) if transcript_path else (None, "low")
    transcript_size = 0
    if transcript_path:
        try:
            transcript_size = Path(transcript_path).stat().st_size
        except OSError:
            pass

    return {
        "schema_version": SCHEMA_VERSION,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "trigger": trigger,
        "task_slug": task_dir.name,
        "phase": _read_phase(task_dir),
        "git": _git_state(project_root),
        "files_touched_recent": (recent_files or [])[:RECENT_FILES_LIMIT],
        "context_pct_estimated": pct if pct is not None else 0,
        "transcript_path_size_bytes": transcript_size,
        "estimator_confidence": conf,
    }


def _read_phase(task_dir: Path) -> str:
    pmd = task_dir / "progress.md"
    if not pmd.is_file():
        return "unknown"
    text = pmd.read_text(encoding="utf-8", errors="replace")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return "unknown"
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            if k.strip() == "phase":
                return v.strip().strip('"').strip("'") or "unknown"
    return "unknown"


def _git_state(project_root: Path) -> dict:
    """Best-effort git state. Returns sane defaults on any failure."""
    out = {
        "branch": "unknown",
        "head": "unknown",
        "dirty_files": 0,
        "recent_commits": [],
    }
    if not (project_root / ".git").exists():
        return out
    try:
        out["branch"] = subprocess.check_output(
            ["git", "-C", str(project_root), "branch", "--show-current"],
            text=True, stderr=subprocess.DEVNULL, timeout=3,
        ).strip() or "unknown"
        out["head"] = subprocess.check_output(
            ["git", "-C", str(project_root), "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=3,
        ).strip() or "unknown"
        status = subprocess.check_output(
            ["git", "-C", str(project_root), "status", "--porcelain"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        out["dirty_files"] = sum(1 for line in status.splitlines() if line.strip())
        log = subprocess.check_output(
            ["git", "-C", str(project_root), "log",
             f"-{RECENT_COMMITS_LIMIT}", "--pretty=%h\t%s"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        for ln in log.splitlines():
            if "\t" in ln:
                h, s = ln.split("\t", 1)
                out["recent_commits"].append({"hash": h.strip(), "subject": s.strip()})
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return out
```

- [ ] **Step 4: Run, confirm 3/3 pass**

```bash
python3 -m unittest tests.smoke.test_v05_mechanical -v
```
Expected: 3/3 PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/common/mechanical.py tests/smoke/test_v05_mechanical.py
git commit -m "feat(v0.5): add mechanical — build mechanical.json payload

Single source of truth for the mechanical snapshot schema. Reads phase
from progress.md frontmatter, gathers git branch/head/dirty/recent_commits
via subprocess (sane defaults on any failure), uses context_estimator for
context % + confidence."
```

---

## Phase B — PreCompact Hook

### Task 6: `pre-compact.py` hook

**Files:**
- Create: `claude/hooks/pre-compact.py`
- Test: `tests/smoke/test_v05_precompact_hook.py`

**Why this task:** New hook event. Fires before Claude Code auto-compacts. Writes `mechanical.json` (always) and appends to `history.jsonl`. Never blocks compact. v0.5 does NOT inject any model instruction — that's deferred to v0.6.

- [ ] **Step 1: Write failing tests**

Create `tests/smoke/test_v05_precompact_hook.py`:

```python
#!/usr/bin/env python3
"""Smoke tests for v0.5 PreCompact hook."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / "claude" / "hooks" / "pre-compact.py"


def _has_git() -> bool:
    return shutil.which("git") is not None


@unittest.skipUnless(_has_git(), "git not available")
class PreCompactHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-pre-")).resolve()
        # Init project + git
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "test")
        env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
        env.setdefault("GIT_COMMITTER_NAME", "test")
        env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
        (self.tmp / "README").write_text("hi\n")
        subprocess.run(["git", "-C", str(self.tmp), "add", "."], check=True,
                       env=env, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.tmp), "commit", "-q", "-m", "init"],
                       check=True, env=env, stdout=subprocess.DEVNULL)
        # .flow + active task
        flow = self.tmp / ".flow"
        (flow / "tasks" / "01-01-demo").mkdir(parents=True)
        (flow / "tasks" / "01-01-demo" / "progress.md").write_text(
            "---\nphase: phase-2-execute\n---\n", encoding="utf-8"
        )
        (flow / ".current-task").write_text(
            str((flow / "tasks" / "01-01-demo").relative_to(self.tmp)), encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_hook(self, hook_input: dict) -> int:
        result = subprocess.run(
            ["python3", str(HOOK_PATH)],
            input=json.dumps(hook_input),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode

    def test_writes_mechanical_json_and_history_entry(self):
        rc = self._run_hook({"cwd": str(self.tmp), "transcript_path": ""})
        self.assertEqual(rc, 0)
        cp = self.tmp / ".flow" / "tasks" / "01-01-demo" / ".checkpoint"
        mech = cp / "mechanical.json"
        history = cp / "history.jsonl"
        self.assertTrue(mech.is_file(), "mechanical.json must be written")
        self.assertTrue(history.is_file(), "history.jsonl must be written")
        data = json.loads(mech.read_text())
        self.assertEqual(data["trigger"], "precompact")
        self.assertEqual(data["task_slug"], "01-01-demo")
        # history line
        events = [json.loads(ln) for ln in history.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "precompact")

    def test_no_active_task_exits_silently(self):
        (self.tmp / ".flow" / ".current-task").unlink()
        rc = self._run_hook({"cwd": str(self.tmp), "transcript_path": ""})
        self.assertEqual(rc, 0, "hook must never block on missing task")

    def test_no_flow_dir_exits_silently(self):
        outside = Path(tempfile.mkdtemp(prefix="not-flow-"))
        try:
            rc = self._run_hook({"cwd": str(outside), "transcript_path": ""})
            self.assertEqual(rc, 0)
        finally:
            shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m unittest tests.smoke.test_v05_precompact_hook -v
```
Expected: hook script not found / FileNotFoundError on `pre-compact.py`.

- [ ] **Step 3: Implement `pre-compact.py`**

Create `claude/hooks/pre-compact.py`:

```python
#!/usr/bin/env python3
"""PreCompact hook (v0.5) — write mechanical snapshot before Claude Code auto-compacts.

v0.5 behavior: write <task>/.checkpoint/mechanical.json (atomic) and append
one line to history.jsonl. Never block compact (always exit 0).

v0.6 will additionally fork the autopilot-checkpoint script when
autopilot-state.json exists and is active. That extension is NOT in this
file yet — see docs/specs/2026-05-04-auto-resume-design.md component K.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common.safe_io import atomic_write_json, append_jsonl_locked
from common.checkpoint_paths import mechanical_path, history_path
from common.mechanical import build_payload


def find_project_root(start: Path) -> Path | None:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".flow").is_dir():
            return cur
        cur = cur.parent
    return None


def find_active_task(project_root: Path) -> Path | None:
    flow = project_root / ".flow"
    ptr = flow / ".current-task"
    if not ptr.is_file():
        return None
    raw = ptr.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = project_root / p
    return p if p.is_dir() else None


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    cwd = Path(hook_input.get("cwd", os.getcwd())).resolve()
    transcript_path = hook_input.get("transcript_path") or None

    project_root = find_project_root(cwd)
    if project_root is None:
        return 0

    task_dir = find_active_task(project_root)
    if task_dir is None:
        return 0

    try:
        payload = build_payload(
            project_root=project_root,
            task_dir=task_dir,
            trigger="precompact",
            transcript_path=transcript_path,
        )
        atomic_write_json(mechanical_path(task_dir), payload)
        append_jsonl_locked(history_path(task_dir), {
            "schema_version": 1,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": "precompact",
            "ctx_pct": payload.get("context_pct_estimated", 0),
            "trigger_origin": "hook",
        })
    except Exception:
        # Fail-closed: never block compact. Audit gap acceptable.
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Make executable + run tests**

```bash
chmod +x claude/hooks/pre-compact.py
python3 -m unittest tests.smoke.test_v05_precompact_hook -v
```
Expected: 3/3 PASS.

- [ ] **Step 5: Commit**

```bash
git add claude/hooks/pre-compact.py tests/smoke/test_v05_precompact_hook.py
git commit -m "feat(v0.5): add PreCompact hook

Writes <task>/.checkpoint/mechanical.json atomically + appends a 'precompact'
event to history.jsonl. Never blocks compact (silent exit on any failure).

v0.5 hook is mechanical-only — no model instruction injection. v0.6 will
add autopilot subprocess fork (see spec component K)."
```

---

### Task 7: Wire PreCompact into `settings.template.json` + `flow_install.py`

**Files:**
- Modify: `claude/hooks/settings.template.json`
- Modify: `scripts/flow_install.py:233-241` (FLOW_OWNED_MARKERS)
- Test: existing `tests/smoke/test_install_logic.py` will catch hook count mismatches

- [ ] **Step 1: Read current settings.template.json**

```bash
cat claude/hooks/settings.template.json
```
Note the structure: top-level `hooks` object, with each event having a list of matcher entries. Need to add a new top-level entry `PreCompact`.

- [ ] **Step 2: Add PreCompact entry to settings.template.json**

In `claude/hooks/settings.template.json`, find the closing `}` of the `hooks` object. Insert before it (matching style of existing entries):

```json
    "PreCompact": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 {{REPO_ROOT}}/claude/hooks/pre-compact.py",
            "timeout": 5
          }
        ]
      }
    ]
```

(Add a comma after the previous event-list entry to keep JSON valid.)

- [ ] **Step 3: Validate JSON parses**

```bash
python3 -c "import json; json.load(open('claude/hooks/settings.template.json'))" && echo OK
```
Expected: `OK`.

- [ ] **Step 4: Add `pre-compact.py` to `FLOW_OWNED_MARKERS`**

Edit `scripts/flow_install.py`. Find the `FLOW_OWNED_MARKERS` tuple (around line 233):

```python
FLOW_OWNED_MARKERS = (
    "flow-framework",
    "claude/hooks/session-start.py",
    "claude/hooks/user-prompt-submit.py",
    "claude/hooks/pre-tool-task.py",
    "claude/hooks/post-tool-bash.py",
    "claude/hooks/post-tool-edit.py",
    "claude/hooks/stop.py",
)
```

Add `"claude/hooks/pre-compact.py",` before the closing `)`:

```python
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
```

- [ ] **Step 5: Update existing test that asserts command count**

In `tests/smoke/test_install_logic.py`, find the test
`test_render_with_repo_root` (around line 79):

```python
        self.assertEqual(len(all_commands), 9,
                         "expect 9 commands: 3x SessionStart + 1 UserPromptSubmit + 1 PreToolUse(Task) "
                         "+ 3 PostToolUse(Bash/Edit/Write) + 1 Stop")
```

Update to 10:

```python
        self.assertEqual(len(all_commands), 10,
                         "expect 10 commands: 3x SessionStart + 1 UserPromptSubmit + 1 PreToolUse(Task) "
                         "+ 3 PostToolUse(Bash/Edit/Write) + 1 Stop + 1 PreCompact")
```

- [ ] **Step 6: Run install_logic tests**

```bash
python3 -m unittest tests.smoke.test_install_logic -v
```
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add claude/hooks/settings.template.json scripts/flow_install.py tests/smoke/test_install_logic.py
git commit -m "feat(v0.5): install PreCompact hook via settings template

- settings.template.json: PreCompact entry with own matcher (Issue #415-clean)
- flow_install.py: pre-compact.py added to FLOW_OWNED_MARKERS so re-install
  cleanly replaces it instead of accumulating duplicates
- test_install_logic: bumped expected command count 9 → 10"
```

---

## Phase C — PostToolUse Hook Extensions

### Task 8: Shared nudge logic + post-tool-bash extension

**Files:**
- Create: `scripts/common/nudge.py`
- Modify: `claude/hooks/post-tool-bash.py`
- Test: `tests/smoke/test_v05_postool_nudge.py`

**Why this task:** Both `post-tool-bash.py` and `post-tool-edit.py` need the same nudge logic. Extract into a shared helper. nudge-state per-task (not per-cwd) keeps multi-task projects clean.

- [ ] **Step 1: Write failing test for nudge_helper**

Create `tests/smoke/test_v05_postool_nudge.py`:

```python
#!/usr/bin/env python3
"""Smoke tests for v0.5 nudge helper used by PostToolUse hooks."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class NudgeDecide(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("FLOW_HOME")
        os.environ["FLOW_HOME"] = self._tmp.name
        for m in list(sys.modules):
            if m.startswith("common.nudge"):
                del sys.modules[m]

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("FLOW_HOME", None)
        else:
            os.environ["FLOW_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_below_threshold_no_nudge(self):
        from common.nudge import maybe_nudge_text
        text = maybe_nudge_text(task_slug="t", pct=30, confidence="high",
                                 window_id="w1", min_seconds_between=60)
        self.assertIsNone(text)

    def test_low_confidence_skips_nudge(self):
        from common.nudge import maybe_nudge_text
        text = maybe_nudge_text(task_slug="t", pct=80, confidence="low",
                                 window_id="w1", min_seconds_between=60)
        self.assertIsNone(text)

    def test_at_threshold_emits_nudge_first_time(self):
        from common.nudge import maybe_nudge_text
        text = maybe_nudge_text(task_slug="t", pct=55, confidence="high",
                                 window_id="w1", min_seconds_between=60)
        self.assertIsNotNone(text)
        self.assertIn("55", text)
        self.assertIn("/flow:pause", text)

    def test_already_acknowledged_skips_in_same_window(self):
        from common.nudge import maybe_nudge_text, acknowledge
        # First nudge fires
        text = maybe_nudge_text(task_slug="t", pct=55, confidence="high",
                                 window_id="w1", min_seconds_between=60)
        self.assertIsNotNone(text)
        acknowledge(task_slug="t", via="manual_pause")
        # Second call same window — suppressed
        text2 = maybe_nudge_text(task_slug="t", pct=60, confidence="high",
                                  window_id="w1", min_seconds_between=0)
        self.assertIsNone(text2)

    def test_new_window_re_arms(self):
        from common.nudge import maybe_nudge_text, acknowledge
        maybe_nudge_text(task_slug="t", pct=55, confidence="high",
                         window_id="w1", min_seconds_between=60)
        acknowledge(task_slug="t", via="manual_pause")
        # New window after compact
        text = maybe_nudge_text(task_slug="t", pct=55, confidence="high",
                                 window_id="w2", min_seconds_between=0)
        self.assertIsNotNone(text)

    def test_min_seconds_between_throttles(self):
        from common.nudge import maybe_nudge_text
        t1 = maybe_nudge_text(task_slug="t", pct=55, confidence="high",
                               window_id="w1", min_seconds_between=300)
        self.assertIsNotNone(t1)
        # Same call within throttle window
        t2 = maybe_nudge_text(task_slug="t", pct=60, confidence="high",
                               window_id="w1", min_seconds_between=300)
        self.assertIsNone(t2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m unittest tests.smoke.test_v05_postool_nudge -v
```
Expected: ModuleNotFoundError on `common.nudge`.

- [ ] **Step 3: Implement `nudge.py`**

Create `scripts/common/nudge.py`:

```python
"""Context-pressure nudge helper for PostToolUse hooks (v0.5).

Decides whether to inject a 'consider /flow:pause' reminder into the model's
next turn. State per task slug (not per cwd) so multi-task projects don't
collide. State at ~/.flow/.runtime/nudge-state-<task_slug>.json.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from .safe_io import atomic_write_json

CTX_THRESHOLD_PCT = 50  # configurable later via flow.config.local.yaml


def _runtime_dir() -> Path:
    home = os.environ.get("FLOW_HOME")
    base = Path(home) if home else Path.home() / ".flow"
    rt = base / ".runtime"
    rt.mkdir(parents=True, exist_ok=True)
    return rt


def _state_path(task_slug: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_slug)
    return _runtime_dir() / f"nudge-state-{safe}.json"


def _read_state(task_slug: str) -> dict:
    p = _state_path(task_slug)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(task_slug: str, state: dict) -> None:
    state.setdefault("schema_version", 1)
    state.setdefault("task_slug", task_slug)
    atomic_write_json(_state_path(task_slug), state)


def maybe_nudge_text(
    task_slug: str,
    pct: Optional[int],
    confidence: str,
    window_id: str,
    min_seconds_between: int = 60,
) -> Optional[str]:
    """Decide whether a nudge should fire and return the additionalContext
    text, or None if not.

    Side effect: updates nudge-state with last_nudge_ts + last_nudge_ctx_pct
    when a nudge IS fired. acknowledge() is a separate call.
    """
    if pct is None or pct < CTX_THRESHOLD_PCT:
        return None
    if confidence == "low":
        return None

    state = _read_state(task_slug)
    now = datetime.now().astimezone()

    if state.get("current_window_id") == window_id and state.get("acknowledged"):
        return None

    last_ts = state.get("last_nudge_ts")
    if last_ts and state.get("current_window_id") == window_id:
        try:
            last = datetime.fromisoformat(last_ts)
            if (now - last).total_seconds() < min_seconds_between:
                return None
        except ValueError:
            pass

    text = (
        f"<flow-checkpoint-suggested priority=\"medium\" cycle=\"{window_id}\">\n"
        f"Context usage estimated at {pct}% (estimator confidence: {confidence}).\n"
        f"Best moment to checkpoint while model is still clear.\n\n"
        f"Tell the user verbatim before any other content (only once per session):\n"
        f"> 💾 上下文已到 {pct}%。建议 /flow:pause 存档，新 session 跑 /flow:resume 续上。\n\n"
        f"This is a soft hint — user may continue if they prefer. Do not interrupt\n"
        f"in-flight tool sequences; surface at the next natural pause.\n"
        f"</flow-checkpoint-suggested>"
    )

    _write_state(task_slug, {
        "current_window_id": window_id,
        "last_nudge_ts": now.isoformat(timespec="seconds"),
        "last_nudge_ctx_pct": pct,
        "acknowledged": False,
        "acknowledged_via": None,
    })
    return text


def acknowledge(task_slug: str, via: str) -> None:
    """Mark current nudge as acknowledged (e.g., user ran /flow:pause)."""
    state = _read_state(task_slug)
    state["acknowledged"] = True
    state["acknowledged_via"] = via
    state["acknowledged_ts"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_state(task_slug, state)


def derive_window_id(task_slug: str) -> str:
    """Produce a stable cycle id for the current window. Caller (SessionStart
    on `compact`) is expected to roll over by calling rotate_window."""
    state = _read_state(task_slug)
    return state.get("current_window_id") or f"cycle-{datetime.now().astimezone().isoformat(timespec='seconds')}"


def rotate_window(task_slug: str) -> str:
    """Force a new window_id (called by SessionStart on `compact` matcher)."""
    new_id = f"cycle-{datetime.now().astimezone().isoformat(timespec='seconds')}"
    _write_state(task_slug, {
        "current_window_id": new_id,
        "acknowledged": False,
        "acknowledged_via": None,
        "last_nudge_ts": None,
        "last_nudge_ctx_pct": None,
    })
    return new_id
```

- [ ] **Step 4: Run nudge tests, confirm 6/6 pass**

```bash
python3 -m unittest tests.smoke.test_v05_postool_nudge -v
```
Expected: 6/6 PASS.

- [ ] **Step 5: Extend `post-tool-bash.py`**

Edit `claude/hooks/post-tool-bash.py`. After the existing `bump_heartbeat(cwd)` call (around line 224), add the nudge + mechanical-update logic.

First, add the import block (near top, after existing imports):

```python
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from common.context_estimator import estimate_context_pct
from common.nudge import maybe_nudge_text, derive_window_id
from common.checkpoint_paths import mechanical_path, history_path
from common.mechanical import build_payload
from common.safe_io import atomic_write_json, append_jsonl_locked
```

Then in `main()`, replace this block:

```python
    # Heartbeat bump on every Bash invocation (cheap; bounded).
    bump_heartbeat(cwd)

    # Only the rest of the work is for git-commit events.
    if not is_git_commit_command(command):
        sys.exit(0)
```

with:

```python
    # Heartbeat bump on every Bash invocation (cheap; bounded).
    bump_heartbeat(cwd)

    # v0.5: context-pressure nudge + throttled mechanical update.
    transcript_path = hook_input.get("transcript_path")
    project_root = find_project_root(cwd)
    if project_root is not None:
        task_dir = find_active_task(project_root)
        if task_dir is not None and transcript_path:
            _maybe_nudge_and_update_mechanical(
                project_root=project_root,
                task_dir=task_dir,
                transcript_path=transcript_path,
            )

    # Only the rest of the work is for git-commit events.
    if not is_git_commit_command(command):
        sys.exit(0)
```

Then add this helper function above `main()`:

```python
def _maybe_nudge_and_update_mechanical(
    project_root: Path,
    task_dir: Path,
    transcript_path: str,
) -> None:
    """v0.5 PostToolUse extension: emit nudge if ctx >= threshold AND not
    acknowledged this window; throttle mechanical.json writes to once per 60s."""
    try:
        pct, conf = estimate_context_pct(transcript_path)
        if pct is None:
            return

        window_id = derive_window_id(task_dir.name)
        nudge_text = maybe_nudge_text(
            task_slug=task_dir.name,
            pct=pct,
            confidence=conf,
            window_id=window_id,
            min_seconds_between=60,
        )

        # Throttled mechanical update — only if last write > 60s ago
        mech = mechanical_path(task_dir)
        now_epoch = time.time()
        write_mech = True
        if mech.is_file():
            try:
                if now_epoch - mech.stat().st_mtime < 60:
                    write_mech = False
            except OSError:
                pass
        if write_mech:
            payload = build_payload(
                project_root=project_root,
                task_dir=task_dir,
                trigger="post-tool",
                transcript_path=transcript_path,
            )
            atomic_write_json(mech, payload)

        if nudge_text:
            # Append history event for nudge fired
            append_jsonl_locked(history_path(task_dir), {
                "schema_version": 1,
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "event": "nudge_emitted",
                "ctx_pct": pct,
                "estimator_confidence": conf,
                "window_id": window_id,
            })
            # Inject the nudge text via stdout JSON
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": nudge_text,
                }
            }, ensure_ascii=False), flush=True)
    except Exception:
        # Fail-closed; never break the hook chain.
        pass
```

Note: this new helper writes to stdout for nudge injection. The existing credential-warning path also writes to stdout. Confirm Claude Code merges multiple JSON outputs gracefully — if it doesn't, we'll need to combine them. Per Claude Code hook docs each hook script is allowed exactly one JSON output; if a credential warning AND a nudge both fire, we'd need to merge. Add this at the end of `_maybe_nudge_and_update_mechanical` for now: if a nudge is emitted and the rest of `main` would also emit a credential warning, we need to combine. **For v0.5 simplicity**: if a nudge is emitted, set a marker on the function so the rest of `main` knows not to emit additionally. We'll wire this in Step 6.

- [ ] **Step 6: Wire emit guard so credential-warning + nudge don't both stdout**

Refactor `_maybe_nudge_and_update_mechanical` to RETURN the nudge text instead of printing. Then in `main()`, collect both nudge text + credential warning text into a single combined block before printing.

In `_maybe_nudge_and_update_mechanical`, change the `if nudge_text:` block to:

```python
        if nudge_text:
            append_jsonl_locked(history_path(task_dir), {
                "schema_version": 1,
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "event": "nudge_emitted",
                "ctx_pct": pct,
                "estimator_confidence": conf,
                "window_id": window_id,
            })
            return nudge_text
        return None
    except Exception:
        return None
```

(And change the function signature to return `Optional[str]`.)

Then refactor the calling code in `main()` to collect both:

```python
    transcript_path = hook_input.get("transcript_path")
    nudge_text: Optional[str] = None
    project_root = find_project_root(cwd)
    if project_root is not None:
        task_dir = find_active_task(project_root)
        if task_dir is not None and transcript_path:
            nudge_text = _maybe_nudge_and_update_mechanical(
                project_root=project_root,
                task_dir=task_dir,
                transcript_path=transcript_path,
            )
```

And at the very end of `main()`, after the existing credential-grep logic, replace the final output block:

```python
    matches = credential_grep(project_root) if project_root else None
    if not matches and not nudge_text:
        sys.exit(0)

    parts = []
    if nudge_text:
        parts.append(nudge_text)
    if matches:
        parts.append(
            "<flow-credential-warning>\n"
            "POSSIBLE credential leak detected after git commit. Review:\n"
            f"{matches}\n\n"
            "If real credentials: rotate immediately, remove from history (git filter-repo), "
            "and move to ~/.flow/credentials.local. If false positive (e.g., template / docs example), "
            "rename the matched key to avoid future false alarms.\n"
            "</flow-credential-warning>"
        )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n\n".join(parts),
        }
    }
    print(json.dumps(output, ensure_ascii=False), flush=True)
```

Also add `from typing import Optional` import if not already present.

- [ ] **Step 7: Run smoke tests for post-tool-bash**

```bash
python3 -m unittest tests.smoke.test_p1_hardening.P1_4_GitCommitDetection -v
python3 -m unittest tests.smoke.test_v05_postool_nudge -v
```
Expected: existing 8 P1-4 tests still pass + 6/6 v05 nudge tests pass.

- [ ] **Step 8: Commit**

```bash
git add scripts/common/nudge.py claude/hooks/post-tool-bash.py tests/smoke/test_v05_postool_nudge.py
git commit -m "feat(v0.5): nudge helper + post-tool-bash extension

scripts/common/nudge.py:
- maybe_nudge_text(): decide+emit nudge based on ctx pct + confidence,
  per-task state, 60s min throttle, single-fire per window
- acknowledge(): mark current window's nudge as resolved
- rotate_window(): roll over after compact (called by SessionStart)

post-tool-bash.py:
- estimate ctx % from transcript_path
- emit nudge text if threshold crossed and not acknowledged this window
- throttled mechanical.json update (60s)
- merged additionalContext output: nudge + credential-warning combined
  into a single hookSpecificOutput (Claude Code only allows one JSON out)"
```

---

### Task 9: Mirror nudge logic into `post-tool-edit.py`

**Files:**
- Modify: `claude/hooks/post-tool-edit.py`

**Why this task:** Edit and Write tools also need to drive the nudge — file edits accumulate context fast in many sessions. Reuses the helper from Task 8.

- [ ] **Step 1: Read current post-tool-edit.py main()**

```bash
sed -n '210,275p' claude/hooks/post-tool-edit.py
```

- [ ] **Step 2: Add imports + helper at top of file**

After the existing `import` block, add:

```python
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from common.context_estimator import estimate_context_pct
from common.nudge import maybe_nudge_text, derive_window_id
from common.checkpoint_paths import mechanical_path, history_path
from common.mechanical import build_payload
from common.safe_io import atomic_write_json, append_jsonl_locked
from typing import Optional
```

Add the helper function (paste exactly the same `_maybe_nudge_and_update_mechanical`
defined in Task 8, Step 5 — repeated here for clarity since the engineer may be
reading tasks out of order):

```python
def _maybe_nudge_and_update_mechanical(
    project_root: Path,
    task_dir: Path,
    transcript_path: str,
) -> Optional[str]:
    """v0.5 PostToolUse extension. See post-tool-bash.py for prose."""
    try:
        pct, conf = estimate_context_pct(transcript_path)
        if pct is None:
            return None

        window_id = derive_window_id(task_dir.name)
        nudge_text = maybe_nudge_text(
            task_slug=task_dir.name, pct=pct, confidence=conf,
            window_id=window_id, min_seconds_between=60,
        )

        mech = mechanical_path(task_dir)
        now_epoch = time.time()
        write_mech = True
        if mech.is_file():
            try:
                if now_epoch - mech.stat().st_mtime < 60:
                    write_mech = False
            except OSError:
                pass
        if write_mech:
            payload = build_payload(
                project_root=project_root, task_dir=task_dir,
                trigger="post-tool", transcript_path=transcript_path,
            )
            atomic_write_json(mech, payload)

        if nudge_text:
            append_jsonl_locked(history_path(task_dir), {
                "schema_version": 1,
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "event": "nudge_emitted",
                "ctx_pct": pct,
                "estimator_confidence": conf,
                "window_id": window_id,
            })
            return nudge_text
        return None
    except Exception:
        return None
```

- [ ] **Step 3: Wire into `main()`**

In `main()`, after `bump_heartbeat(cwd)` and before the existing flush logic, insert:

```python
    transcript_path = hook_input.get("transcript_path")
    nudge_text: Optional[str] = None
    project_root_v05 = find_project_root(cwd)
    if project_root_v05 is not None:
        task_dir_v05 = find_active_task(project_root_v05)
        if task_dir_v05 is not None and transcript_path:
            nudge_text = _maybe_nudge_and_update_mechanical(
                project_root=project_root_v05,
                task_dir=task_dir_v05,
                transcript_path=transcript_path,
            )
```

At the end of `main()`, before the final `sys.exit(0)`, add:

```python
    if nudge_text:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": nudge_text,
            }
        }
        print(json.dumps(output, ensure_ascii=False), flush=True)
```

- [ ] **Step 4: Run all v05 + p1 hook tests**

```bash
python3 -m unittest tests.smoke.test_v05_postool_nudge tests.smoke.test_p1_hardening -v
```
Expected: all PASS (no regressions).

- [ ] **Step 5: Commit**

```bash
git add claude/hooks/post-tool-edit.py
git commit -m "feat(v0.5): post-tool-edit nudge + mechanical mirror

Mirrors the nudge + throttled mechanical-update logic from
post-tool-bash.py. Edit/Write tool calls also drive context %, so the
same helper applies. nudge-state is per-task so multiple tools sharing
the same active task don't double-fire."
```

---

## Phase D — SessionStart Compact Resume

### Task 10: SessionStart compact-matcher injection

**Files:**
- Modify: `claude/hooks/session-start.py`
- Test: `tests/smoke/test_v05_sessionstart_compact.py`

**Why this task:** When Claude Code finishes auto-compact, it fires `SessionStart` with matcher `compact`. We use that opportunity to re-inject the freshest checkpoint state so the model can resume work.

- [ ] **Step 1: Write failing test**

Create `tests/smoke/test_v05_sessionstart_compact.py`:

```python
#!/usr/bin/env python3
"""Smoke tests for v0.5 SessionStart compact-matcher resume injection."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / "claude" / "hooks" / "session-start.py"


class SessionStartCompact(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-ss-")).resolve()
        flow = self.tmp / ".flow"
        task = flow / "tasks" / "01-01-demo"
        task.mkdir(parents=True)
        (task / "prd.md").write_text("# Demo Task\n\nstuff\n", encoding="utf-8")
        (task / "progress.md").write_text(
            "---\nphase: phase-2-execute\n---\n", encoding="utf-8"
        )
        (flow / ".current-task").write_text(
            str(task.relative_to(self.tmp)), encoding="utf-8"
        )
        # Pre-existing checkpoint files
        cp = task / ".checkpoint"
        cp.mkdir()
        (cp / "intent.md").write_text(
            "---\nschema_version: 1\ntrigger: manual\nts: 2026-05-04T15:30:00+08:00\n"
            "context_pct_estimated: 50\ntask_slug: 01-01-demo\nphase: phase-2-execute\n"
            "supersedes: none\n---\n\n## Current Intent\nworking on it\n",
            encoding="utf-8",
        )
        (cp / "mechanical.json").write_text(json.dumps({
            "schema_version": 1,
            "ts": "2026-05-04T15:35:00+08:00",
            "trigger": "precompact",
            "task_slug": "01-01-demo",
            "phase": "phase-2-execute",
            "git": {"branch": "main", "head": "abc1234", "dirty_files": 0,
                    "recent_commits": [{"hash": "abc1234", "subject": "wip"}]},
            "files_touched_recent": ["foo.py", "bar.py"],
            "context_pct_estimated": 88,
            "transcript_path_size_bytes": 800000,
        }), encoding="utf-8")
        self.task = task

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_hook(self, matcher: str) -> dict:
        result = subprocess.run(
            ["python3", str(HOOK_PATH)],
            input=json.dumps({"cwd": str(self.tmp), "trigger": matcher}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_compact_matcher_injects_resume_block(self):
        out = self._run_hook("compact")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("flow-resumed-from-compact", ctx)
        self.assertIn("Last Intent", ctx)
        self.assertIn("Current Intent", ctx)  # body of intent.md is in
        self.assertIn("Latest Mechanical State", ctx)
        self.assertIn("abc1234", ctx)
        self.assertIn("MANUAL", ctx)  # Resume Mode

    def test_startup_matcher_does_not_inject_resume_block(self):
        out = self._run_hook("startup")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("flow-resumed-from-compact", ctx)

    def test_compact_with_no_checkpoint_falls_back(self):
        shutil.rmtree(self.task / ".checkpoint")
        out = self._run_hook("compact")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("flow-resumed-from-compact", ctx)
        # but should still have active task in standard quick-guide
        self.assertIn("Active Task", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run, confirm fail**

```bash
python3 -m unittest tests.smoke.test_v05_sessionstart_compact -v
```
Expected: FAIL on `flow-resumed-from-compact` not present.

- [ ] **Step 3: Extend `session-start.py`**

In `claude/hooks/session-start.py`, add imports near the top:

```python
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from common.checkpoint_paths import intent_path, mechanical_path, history_path
from common.safe_io import append_jsonl_locked
from common.nudge import rotate_window
from datetime import datetime, timedelta
```

Add helper function above `main()`:

```python
def build_compact_resume_block(task_dir: Path) -> str | None:
    """If .checkpoint/ exists, build the <flow-resumed-from-compact> block.
    Returns None if no checkpoint files present (fall back to startup behavior)."""
    intent = intent_path(task_dir)
    mech = mechanical_path(task_dir)
    if not intent.is_file() and not mech.is_file():
        return None

    parts = ["<flow-resumed-from-compact>"]

    if intent.is_file():
        text = intent.read_text(encoding="utf-8", errors="replace")
        # Truncate body to ~1500 tokens (roughly 6000 chars) if huge
        if len(text) > 6000:
            text = text[:6000] + "\n\n[... truncated, see full file at " + str(intent) + "]"
        parts.append("## Last Intent")
        parts.append(text.rstrip())

    intent_ts = None
    mech_ts = None
    if mech.is_file():
        try:
            data = json.loads(mech.read_text(encoding="utf-8"))
            mech_ts = data.get("ts")
            git_info = data.get("git", {})
            files = data.get("files_touched_recent", [])
            parts.append("\n## Latest Mechanical State")
            parts.append(f"- Snapshot ts: {mech_ts}")
            parts.append(f"- Branch: {git_info.get('branch', '?')} @ {git_info.get('head', '?')}")
            commits = git_info.get("recent_commits", [])
            if commits:
                parts.append("- Recent commits:")
                for c in commits[:5]:
                    parts.append(f"  - {c.get('hash', '?')} {c.get('subject', '')}")
            if files:
                parts.append(f"- Files touched recent: {', '.join(files[:10])}")
        except (json.JSONDecodeError, OSError):
            pass

    if intent.is_file():
        try:
            head = intent.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in head[:20]:
                if line.startswith("ts:"):
                    intent_ts = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass

    parts.append("\n## Resume Mode")
    parts.append("MANUAL — present the briefing above to the user, then await their direction.")
    parts.append("Do NOT auto-execute next actions.")

    if intent_ts and mech_ts:
        try:
            ti = datetime.fromisoformat(intent_ts)
            tm = datetime.fromisoformat(mech_ts)
            if tm - ti > timedelta(minutes=5):
                delta_min = round((tm - ti).total_seconds() / 60)
                parts.append("\n## Staleness")
                parts.append(
                    f"⚠️ Mechanical state is {delta_min} minutes newer than intent. "
                    f"Review commits + file edits before assuming intent is still fresh."
                )
        except ValueError:
            pass

    parts.append("</flow-resumed-from-compact>")
    return "\n".join(parts)
```

In `main()`, after the existing `if flow:` block (after pitfalls and skill_diff), add:

```python
    matcher = hook_input.get("trigger") or hook_input.get("hook_event_matcher") or ""
    if matcher == "compact" and flow:
        active = load_active_task(flow)
        if active and not active.get("stale"):
            task_dir = Path(active["path"])
            block = build_compact_resume_block(task_dir)
            if block:
                parts.append("\n" + block)
                # Append history event + roll over nudge window
                try:
                    append_jsonl_locked(history_path(task_dir), {
                        "schema_version": 1,
                        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "event": "resumed_from_compact",
                        "mode": "manual",
                    })
                    rotate_window(task_dir.name)
                except Exception:
                    pass
```

- [ ] **Step 4: Run tests**

```bash
python3 -m unittest tests.smoke.test_v05_sessionstart_compact -v
```
Expected: 3/3 PASS.

- [ ] **Step 5: Commit**

```bash
git add claude/hooks/session-start.py tests/smoke/test_v05_sessionstart_compact.py
git commit -m "feat(v0.5): SessionStart compact-matcher injects resume context

When matcher == 'compact' AND there is an active flow task with .checkpoint/:
- Read intent.md (full body, truncated to ~1500 tokens if huge)
- Read mechanical.json (branch/head/recent commits/files touched/ts)
- Compose <flow-resumed-from-compact> block
- Add staleness warning if mechanical.ts > intent.ts + 5 min
- Append 'resumed_from_compact' event to history.jsonl
- Roll over nudge window so a fresh nudge can fire in next cycle

Resume Mode is hard-coded to MANUAL in v0.5 (model awaits user signal).
v0.6 autopilot will set this to AUTOPILOT and tell model to continue
without waiting."
```

---

## Phase E — Slash Command Extensions

### Task 11: `/flow:pause` — write intent.md + outbox hint

**Files:**
- Modify: `claude/commands/flow/pause.md`

**Why this task:** Existing `/flow:pause` already writes journal + Execute Log. v0.5 adds the high-value `intent.md` write + cascade hint to L3.

- [ ] **Step 1: Read current pause.md**

```bash
cat claude/commands/flow/pause.md
```

- [ ] **Step 2: Add v0.5 steps to pause.md**

Edit `claude/commands/flow/pause.md`. After the existing "Step 5 — Confirm" section but BEFORE "Constraints", insert:

```markdown
## Step 6 — (v0.5) Write intent.md snapshot

This captures your current mental state at the highest fidelity possible.
Write a markdown body covering the following sections, total length ≤ 1000 tokens:

- **## Current Intent** — 200-300 words: what you're working on right now
- **## Next Action** — one concrete step: file path, function, exact command
- **## Mental Model** — your remaining plan, decision rationale, assumptions
- **## Blockers** — external waits / blockers; may be empty
- **## Dont-Forget** — small details easily lost (e.g. "codex review left 5 nits")

Then write atomically via the helper:

```python
import sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0, "{{REPO_ROOT}}/scripts")
from common.safe_io import atomic_write_text, append_jsonl_locked
from common.checkpoint_paths import intent_path, history_path

intent_body = """\
---
schema_version: 1
trigger: manual
ts: <ISO timestamp now>
context_pct_estimated: <best-guess from your awareness, or 0>
task_slug: <task slug>
phase: <current phase>
supersedes: <previous trigger and ts, or none>
---

<the body sections you wrote above>
"""
atomic_write_text(intent_path(Path("<task path>")), intent_body)
append_jsonl_locked(history_path(Path("<task path>")), {
    "schema_version": 1,
    "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
    "event": "checkpoint",
    "trigger": "manual",
    "intent_len_chars": len(intent_body),
})
```

## Step 7 — (v0.5) Write cascade hint for personal /save

Outbox the hint so the user's personal `/save` skill picks it up next time
they save the session globally.

```python
from common.hint_outbox import write_hint
write_hint({
    "task_slug": "<task slug>",
    "task_path": "<absolute task path>",
    "phase": "<current phase>",
    "last_action": "<one sentence: what you just did>",
    "next_action": "<one sentence: what's next>",
    "pause_trigger": "manual",
})
```

## Step 8 — (v0.5) Mark nudge acknowledged

If a nudge had been pending, this manual pause counts as acknowledgement.

```python
from common.nudge import acknowledge
acknowledge(task_slug="<task slug>", via="manual_pause")
```
```

- [ ] **Step 3: Smoke-test render**

The /flow:pause command file is consumed by the model, not by Python directly.
Confirm it parses as valid markdown with no broken code blocks:

```bash
python3 -c "
import re
text = open('claude/commands/flow/pause.md').read()
opens = len(re.findall(r'```[a-z]+', text))
closes = len(re.findall(r'^```$', text, re.M))
print(f'opens={opens} closes={closes}')
assert abs(opens - closes) <= 0, f'unbalanced code fences in pause.md'
print('OK')
"
```
Expected: prints `OK`.

- [ ] **Step 4: Commit**

```bash
git add claude/commands/flow/pause.md
git commit -m "feat(v0.5): /flow:pause writes intent.md + outbox hint

Steps 6-8 added after existing 5-step protocol:
- Step 6: model writes a structured intent.md snapshot (≤1000 tokens),
  atomic via safe_io.atomic_write_text, history.jsonl event appended
- Step 7: cascade hint to personal /save via hint_outbox.write_hint
- Step 8: mark current nudge window acknowledged via nudge.acknowledge

All file writes go through v0.5 safe_io / hint_outbox helpers — never
ad-hoc open().write()."
```

---

### Task 12: `/flow:resume` — read .checkpoint/ + staleness assessment

**Files:**
- Modify: `claude/commands/flow/resume.md`

- [ ] **Step 1: Read current resume.md**

```bash
cat claude/commands/flow/resume.md
```

- [ ] **Step 2: Insert new steps**

Edit `claude/commands/flow/resume.md`. Insert as Step 0 at the top of the protocol (before existing Step 1):

```markdown
## Step 0 — (v0.5) Personal /resume coordination

If the user has not yet run their personal `/resume` skill in this session,
suggest running it first for cross-conversation global state. After they do
(or if they say skip), continue with this command.

> "Have you run personal /resume yet? It loads MEMORY.md + session_latest.md.
> If not, run it first; then re-invoke /flow:resume for task-depth state.
> If you'd rather skip, say 'skip' and I'll proceed."
```

Then between existing Steps 1 and 2, insert:

```markdown
## Step 1.5 — (v0.5) Load checkpoint files

If `${CURRENT}/.checkpoint/intent.md` exists:
- Read it. Surface the **Next Action** and **Mental Model** sections to the user.
- Note its `trigger` field — `manual` is highest fidelity, `auto-checkpoint`
  was written by autopilot (v0.6+), `autopilot-bail` means autopilot exited
  with concern.

If `${CURRENT}/.checkpoint/mechanical.json` exists:
- Compare its `ts` against intent.md's `ts`.
- If mechanical is > 5 min newer than intent, surface a staleness notice:
  *"Intent was last updated N minutes ago. Mechanical state shows M commits
  + K files touched since then. Review carefully before assuming intent is
  still fresh."*
```

And modify the existing Step 4 wording to mention checkpoint reading:

> Step 4 — Determine current phase + next step

Replace with:

> Step 4 — Determine current phase + next step
>
> Combine intent.md's Next Action (Step 1.5) with progress.md state.
> If they agree → present concrete next step.
> If they conflict → ask the user which is authoritative.

- [ ] **Step 3: Smoke-test render**

```bash
python3 -c "
import re
text = open('claude/commands/flow/resume.md').read()
opens = len(re.findall(r'```[a-z]+', text))
closes = len(re.findall(r'^```$', text, re.M))
print(f'opens={opens} closes={closes}')
print('OK')
"
```

- [ ] **Step 4: Commit**

```bash
git add claude/commands/flow/resume.md
git commit -m "feat(v0.5): /flow:resume reads .checkpoint/ + staleness assessment

Step 0 (NEW): suggest personal /resume first for global state if not yet run.
Step 1.5 (NEW): load intent.md + mechanical.json from .checkpoint/, surface
                Next Action + Mental Model to user.
Step 4 (UPDATED): combine intent's Next Action with progress.md state;
                  surface conflict if they disagree.

Staleness warning fires when mechanical.ts > intent.ts + 5 min."
```

---

## Phase F — Install / Init Integration

### Task 13: `flow_init.py` — propagate `.checkpoint/` to project `.gitignore`

**Files:**
- Modify: `scripts/flow_init.py`

- [ ] **Step 1: Read flow_init.py**

```bash
cat scripts/flow_init.py
```

Identify the function that writes / updates `.gitignore` (likely called during init). If none exists, add one.

- [ ] **Step 2: Add .gitignore append helper**

In `scripts/flow_init.py`, add this helper near the top:

```python
GITIGNORE_FLOW_BLOCK = """\
# Flow Framework runtime + per-task checkpoint
.flow/.runtime/
.flow/.current-task
.flow/config.local.yaml
.flow/workspace/*
!.flow/workspace/.gitkeep
.flow/**/*.tmp
.flow/tasks/*/.checkpoint/
"""

def ensure_gitignore_block(project_root: Path) -> bool:
    """Idempotently add the flow .gitignore block to project .gitignore.
    Returns True if the file was modified, False if no change needed."""
    gi = project_root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    if ".flow/tasks/*/.checkpoint/" in existing:
        return False  # already there
    new_block = GITIGNORE_FLOW_BLOCK
    if existing and not existing.endswith("\n"):
        existing += "\n"
    gi.write_text(existing + "\n" + new_block, encoding="utf-8")
    return True
```

Wire into the existing `init` flow — find the function that finalizes the `.flow/`
directory and call `ensure_gitignore_block(project_root)` after its main writes.

- [ ] **Step 3: Sanity test**

```bash
cd /tmp && rm -rf test-flow-init && mkdir test-flow-init && cd test-flow-init
python3 -c "
import sys
sys.path.insert(0, '/data/Claude/flow-framework/scripts')
from flow_init import ensure_gitignore_block
from pathlib import Path
modified = ensure_gitignore_block(Path('.'))
print('modified:', modified)
print(open('.gitignore').read() if Path('.gitignore').is_file() else '(no gitignore)')
modified2 = ensure_gitignore_block(Path('.'))
print('second call modified:', modified2)
"
```
Expected: first call prints `modified: True` + the new gitignore block; second prints `modified: False`.

- [ ] **Step 4: Commit**

```bash
git add scripts/flow_init.py
git commit -m "feat(v0.5): flow init propagates .checkpoint/ to project .gitignore

ensure_gitignore_block() idempotently adds the v0.5+ runtime / checkpoint
exclusions to a project's .gitignore. Safe to call multiple times.

Existing flow_init wiring updated to call this on init."
```

---

### Task 14: This repo's own `.gitignore` + CHANGELOG + VERSION bump

**Files:**
- Modify: `.gitignore`
- Modify: `CHANGELOG.md`
- Modify: `VERSION`

- [ ] **Step 1: Update .gitignore**

Edit `.gitignore`. Find the existing flow runtime block. Add `.flow/tasks/*/.checkpoint/` to it (or append at end):

```
.flow/tasks/*/.checkpoint/
```

Confirm with:

```bash
grep -q "checkpoint" .gitignore && echo "OK" || echo "MISSING"
```

- [ ] **Step 2: Update CHANGELOG.md**

Edit `CHANGELOG.md`. Add v0.5.0 entry above the v0.4.0 entry:

```markdown
## v0.5.0 (2026-05-04)

Foundation for **auto-resume on context pressure**. Manual flow hardening
+ infra for v0.6.0 autopilot. Spec at `docs/specs/2026-05-04-auto-resume-design.md`.

### Highlights

- **PreCompact hook** — writes mechanical snapshot before Claude Code auto-compacts.
- **Per-task `.checkpoint/`** — `intent.md` + `mechanical.json` + `history.jsonl`
  capture in-flight state. `.gitignored` by default.
- **Atomic writes + fcntl.flock** — all v0.5+ state files go through `safe_io.py`.
  Concurrent appends to `history.jsonl` proven race-free under 8 threads.
- **Append-only hint outbox** — replaces single-file cascade hint that codex
  pre-merge review flagged as lossy.
- **Context-pressure nudge** — PostToolUse hook estimates context % from
  `transcript_path`, suggests `/flow:pause` once per compact cycle when ≥50%.
  Best-effort: model relays text, user sees in conversation.
- **Enhanced `/flow:pause`** — writes intent.md snapshot + cascade hint.
- **Enhanced `/flow:resume`** — reads checkpoint, surfaces Next Action,
  warns on staleness.
- **SessionStart on `compact`** — restores intent + mechanical context after
  auto-compact, model awaits user signal (no auto-execute).

### Added

- `scripts/common/safe_io.py` — atomic_write_text / atomic_write_json /
  append_jsonl_locked
- `scripts/common/hint_outbox.py` — write_hint / list_pending / mark_processed
- `scripts/common/context_estimator.py` — estimate_context_pct
- `scripts/common/checkpoint_paths.py` — per-task path helpers
- `scripts/common/mechanical.py` — build_payload (mechanical.json schema)
- `scripts/common/nudge.py` — maybe_nudge_text / acknowledge / rotate_window
- `claude/hooks/pre-compact.py` — PreCompact hook
- `tests/smoke/test_v05_*.py` — 25+ new test cases

### Changed

- `claude/hooks/post-tool-bash.py` — adds nudge + throttled mechanical update
- `claude/hooks/post-tool-edit.py` — adds nudge + throttled mechanical update
- `claude/hooks/session-start.py` — compact-matcher branch reads checkpoint
- `claude/commands/flow/pause.md` — Steps 6-8 (intent.md + hint + ack)
- `claude/commands/flow/resume.md` — Step 0 (personal /resume hint) + 1.5
- `scripts/flow_install.py` — `pre-compact.py` added to FLOW_OWNED_MARKERS
- `scripts/flow_init.py` — propagates `.checkpoint/` to project `.gitignore`
- `claude/hooks/settings.template.json` — PreCompact entry

### Not yet shipped (deferred to v0.6.0)

- `/flow:start --autopilot` and autopilot state machine
- R5 sanity check via external evidence (downgrade-only)
- Hard budgets (tool calls / files / time)
- Destructive-command denylist
- Explicit `done_when` checklist replacing completion-promise

These are designed in the spec but require dogfooding v0.5.0 first.

```

- [ ] **Step 3: Bump VERSION**

```bash
echo "0.5.0" > VERSION
cat VERSION
```
Expected: `0.5.0`.

- [ ] **Step 4: Commit**

```bash
git add .gitignore CHANGELOG.md VERSION
git commit -m "chore: bump VERSION 0.4.0 → 0.5.0 + CHANGELOG

VERSION: 0.5.0
.gitignore: add .flow/tasks/*/.checkpoint/
CHANGELOG: full v0.5.0 entry covering Highlights / Added / Changed +
            'Not yet shipped' for v0.6.0 deferred items"
```

---

## Phase G — End-to-End Integration Test

### Task 15: e2e test: pause → simulated compact → resume

**Files:**
- Create: `tests/smoke/test_v05_e2e.py`

**Why this task:** The unit tests cover modules in isolation. This proves the full chain works.

- [ ] **Step 1: Write e2e test**

Create `tests/smoke/test_v05_e2e.py`:

```python
#!/usr/bin/env python3
"""End-to-end test: simulate /flow:pause writes, then SessionStart on
`compact` matcher reads them and produces a resume block."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
HOOK_PATH = REPO_ROOT / "claude" / "hooks" / "session-start.py"


class E2EPauseCompactResume(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-e2e-")).resolve()
        # init project
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)
        flow = self.tmp / ".flow"
        task = flow / "tasks" / "01-01-e2e"
        task.mkdir(parents=True)
        (task / "prd.md").write_text("# E2E Task\n", encoding="utf-8")
        (task / "progress.md").write_text("---\nphase: phase-2-execute\n---\n", encoding="utf-8")
        (flow / ".current-task").write_text(
            str(task.relative_to(self.tmp)), encoding="utf-8"
        )
        self.task = task

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pause_writes_then_sessionstart_compact_reads(self):
        # Simulate /flow:pause Step 6: write intent.md
        from common.safe_io import atomic_write_text
        from common.checkpoint_paths import intent_path
        intent_body = (
            "---\n"
            "schema_version: 1\n"
            "trigger: manual\n"
            "ts: 2026-05-04T15:30:00+08:00\n"
            "context_pct_estimated: 50\n"
            "task_slug: 01-01-e2e\n"
            "phase: phase-2-execute\n"
            "supersedes: none\n"
            "---\n\n"
            "## Current Intent\nshipping v0.5\n\n"
            "## Next Action\nrun final smoke suite\n"
        )
        atomic_write_text(intent_path(self.task), intent_body)

        # Simulate /flow:pause Step 7: write hint (using FLOW_HOME isolation)
        with tempfile.TemporaryDirectory() as flow_home:
            os.environ["FLOW_HOME"] = flow_home
            try:
                from common.hint_outbox import write_hint, list_pending
                # Re-import after FLOW_HOME set
                for m in [m for m in list(sys.modules) if "hint_outbox" in m or "nudge" in m]:
                    del sys.modules[m]
                from common.hint_outbox import write_hint, list_pending
                write_hint({
                    "task_slug": "01-01-e2e",
                    "task_path": str(self.task),
                    "phase": "phase-2-execute",
                    "last_action": "wrote intent.md",
                    "next_action": "verify SessionStart sees it",
                    "pause_trigger": "manual",
                })
                self.assertEqual(len(list_pending()), 1)
            finally:
                os.environ.pop("FLOW_HOME", None)

        # Now simulate SessionStart with compact matcher
        result = subprocess.run(
            ["python3", str(HOOK_PATH)],
            input=json.dumps({"cwd": str(self.tmp), "trigger": "compact"}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]

        # Verify resume block contains intent body
        self.assertIn("flow-resumed-from-compact", ctx)
        self.assertIn("Current Intent", ctx)
        self.assertIn("shipping v0.5", ctx)
        self.assertIn("Next Action", ctx)
        self.assertIn("MANUAL", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run e2e test**

```bash
python3 -m unittest tests.smoke.test_v05_e2e -v
```
Expected: PASS.

- [ ] **Step 3: Run full smoke suite to confirm no regressions**

```bash
bash tests/smoke/run.sh 2>&1 | tail -5
```
Expected: ~120/120 PASS (95 from earlier + ~25 v05 new).

- [ ] **Step 4: Commit**

```bash
git add tests/smoke/test_v05_e2e.py
git commit -m "test(v0.5): end-to-end pause → simulated compact → resume

Wires /flow:pause's atomic intent.md write + hint outbox write together
with SessionStart compact-matcher reading the same files. Validates the
full chain end-to-end before release."
```

---

## Phase H — Release Polish

### Task 16: Reinstall hooks locally + run flow doctor

**Files:** No code changes.

- [ ] **Step 1: Reinstall hooks**

```bash
cd /data/Claude/flow-framework
flow install all
```
Expected: `>> Install complete. Run flow doctor to verify.`

- [ ] **Step 2: Run flow doctor**

```bash
flow doctor
```
Expected: 4 plugins ✓, hook isolation passes (or surfaces the pre-existing post-pr-review.sh sibling — that's user-side, not a v0.5 issue), context-mode warning OK.

- [ ] **Step 3: Run flow selftest**

```bash
flow selftest
```
Expected: PASS for all categories.

- [ ] **Step 4: Smoke test on this very repo (Lv1 dogfood)**

Manually trigger the new chain:

```bash
# As an active flow task already (05-04-ctxmode-and-autosave is active)
# Verify .checkpoint/ directory will be created on next post-tool event
ls .flow/tasks/05-04-ctxmode-and-autosave/.checkpoint/ 2>&1 || echo "(not yet created — will appear on first post-tool that crosses 50%)"
```

- [ ] **Step 5: Commit any output of `flow install` if it modified settings.json backup files**

```bash
git status
# If settings.json backup files appeared in this repo (they shouldn't — they go to ~/.claude/), nothing to commit
echo "Phase H complete."
```

---

## Acceptance Checklist

Before declaring v0.5.0 ready to tag:

- [ ] All 16 tasks complete + committed
- [ ] `bash tests/smoke/run.sh` shows ~120 / 120 PASS (95 existing + 25 new v05)
- [ ] `bash tests/smoke/test_ralph_loop.sh` shows 12 / 12 PASS
- [ ] `flow install all` re-runs cleanly
- [ ] `flow doctor` shows v0.5 hook isolation OK + PreCompact entry registered
- [ ] `VERSION` reads `0.5.0`
- [ ] `CHANGELOG.md` v0.5.0 section complete
- [ ] `.gitignore` includes `.flow/tasks/*/.checkpoint/`
- [ ] `docs/specs/2026-05-04-auto-resume-design.md` referenced in CHANGELOG
- [ ] No leftover `*.tmp` files in repo
- [ ] `git push origin master` succeeds

After all green:

```bash
git tag -a v0.5.0 -m "v0.5.0 — auto-resume foundation"
git push origin v0.5.0
gh release create v0.5.0 --title "v0.5.0 — auto-resume foundation" \
  --notes-file <(awk '/^## v0.5.0/{flag=1; next} /^## v/{flag=0} flag' CHANGELOG.md)
```

---

## Self-Review

**Spec coverage**: Each spec component (A through I for v0.5) maps to a task:
A → Task 3 ✓ / B → Task 6+7 ✓ / C → Task 8+9 ✓ / D → Task 10 ✓ /
E → Task 11 ✓ / F → Task 12 ✓ / G → documented in spec only (consumer is
external personal `/save`) ✓ / H → Task 1 ✓ / I → Task 2 ✓.
Plus checkpoint_paths (Task 4) + mechanical (Task 5) which the spec implies but
doesn't name explicitly — added as logical decompositions.

**Placeholder scan**: No "TBD" / "TODO" / "implement later" / "fill in details"
in any step. All code blocks are complete and runnable.

**Type consistency**: Function names checked across tasks:
- `atomic_write_text`, `atomic_write_json`, `append_jsonl_locked` (safe_io)
- `write_hint`, `list_pending`, `mark_processed` (hint_outbox)
- `estimate_context_pct` returns `(pct, confidence)` tuple
- `build_payload` signature: `(project_root, task_dir, trigger, transcript_path, recent_files=None)`
- `maybe_nudge_text` / `acknowledge` / `rotate_window` / `derive_window_id` (nudge)
- `intent_path`, `mechanical_path`, `history_path` (checkpoint_paths)
- `_maybe_nudge_and_update_mechanical` returns `Optional[str]` consistently in both bash + edit hooks
All consistent across tasks 1-15.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-05-04-auto-resume-v0.5.0.md`.
Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Best for this plan since tasks have clear boundaries and many can run independently (Tasks 1-5 in particular are parallelizable).

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints. Lower context overhead but slower.

**Which approach?**
