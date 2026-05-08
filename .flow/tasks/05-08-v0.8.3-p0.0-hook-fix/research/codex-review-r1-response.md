codex
I’ll read the bundled implementation context and check it directly against the accepted Round 5 spec, staying within the listed audit points.
exec
/bin/bash -lc 'wc -l /tmp/codex-review-r1-context.txt' in /data/Claude/flow-framework
exec
/bin/bash -lc "sed -n '1,260p' /tmp/codex-review-r1-context.txt" in /data/Claude/flow-framework
 succeeded in 0ms:
523 /tmp/codex-review-r1-context.txt

 succeeded in 0ms:
=== HOOK .py (~300 lines, full) ===
#!/usr/bin/env python3
"""Pre-commit review gate (v0.8.3 P0.0 — D''''+SoleRoot+WrapperDetect).

PreToolUse:Bash hook that BLOCKs `git commit` unless a fresh marker file from
the reviewer flow validates current repo + HEAD + staged tree state.

Design: see flow-framework `.flow/tasks/05-08-v0.8.3-p0.0-hook-fix/prd.md`
ADR (Round 5 final spec). Closes the double-direction bug:
- false-negative: `<noop> && git commit` no longer bypasses (sole-root rule)
- false-positive: heredoc body containing `git commit` text no longer blocks
  (bashlex-aware analysis instead of per-line regex)

Input: JSON on stdin with `tool_input.command` field.
Output: empty PASS, or `hookSpecificOutput` JSON with permissionDecision=deny.
Always exits 0 (decision conveyed via stdout JSON per Claude Code hook protocol).
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Vendored bashlex import
# ---------------------------------------------------------------------------

_HOOK_DIR = Path(__file__).resolve().parent
_VENDOR_DIR = _HOOK_DIR / "_vendor"
sys.path.insert(0, str(_VENDOR_DIR))

try:
    import bashlex  # type: ignore
except ImportError as exc:
    # Vendor missing or broken — fail closed with explicit reason
    _import_error = str(exc)
    bashlex = None  # type: ignore
