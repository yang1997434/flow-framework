"""T16 unit — Notifier 3-tier dispatch + R9 throttle semantics.

Coverage (per plan §T16):
- Tier 2 OSC 9 emission with TERM_PROGRAM allowlist (kitty / unknown fallback)
- R9 throttle: throttle_min=0 fires every event; throttle_min=5 suppresses same-key
- Tier 1 (blocked.md) ALWAYS writes regardless of throttle
- tier2_enabled=false silences both OSC 9 and BEL
- Different (task_id, issue_id) keys do NOT cross-suppress
- §5 line 211–212: terminal events bypass throttle
- Q5.3: archive_on_resume moves blocked.md → archive/blocked/<ts>.md
- T16 fix-pass: lock-timeout fail-open (per docstring contract)
- T16 fix-pass: OSC 9 body sanitize (control-char injection / truncation /
  legal-pass-through)
"""
from __future__ import annotations

import fcntl
import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_notification import (  # noqa: E402  type: ignore
    Notifier,
    THROTTLE_FILENAME,
    _sanitize_osc_text,
)
from flow_contract import Contract  # noqa: E402  type: ignore


def _make_contract(*, throttle_min: int = 0, tier2_enabled: bool = True,
                   command=None) -> Contract:
    return Contract(
        contract_schema_version=1,
        autonomy_mode="auto",
        created_at="2026-05-06T00:00:00Z",
        notification={
            "command": command,
            "throttle_min": throttle_min,
            "tier2_enabled": tier2_enabled,
        },
    )


class TestTier2Emission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def _notifier(self, term="kitty"):
        n = Notifier(
            contract=_make_contract(throttle_min=0),
            slug="demo",
            task_dir=Path(self.tmp),
            term_program=term,
        )
        n._stderr = io.StringIO()
        return n

    def test_kitty_emits_osc9_plus_bel(self):
        n = self._notifier(term="kitty")
        n.fire_block(
            block_type="acceptance_fail", phase=2,
            task_id="T1", issue_id="abc12345",
            why_blocked="x", required_choice=["abort_task"],
            safe_resume_command="flow resume demo",
        )
        out = n._stderr.getvalue()
        self.assertIn("\x1b]9;", out)  # OSC 9
        self.assertIn("\x07", out)     # BEL safety floor

    def test_ghostty_emits_osc9(self):
        n = self._notifier(term="ghostty")
        n.fire_block(
            block_type="acceptance_fail", phase=2,
            task_id="T1", issue_id="abc12345",
            why_blocked="x", required_choice=["abort_task"],
            safe_resume_command="flow resume demo",
        )
        self.assertIn("\x1b]9;", n._stderr.getvalue())

    def test_iterm_emits_osc9(self):
        n = self._notifier(term="iTerm.app")
        n.fire_block(
            block_type="acceptance_fail", phase=2,
            task_id="T1", issue_id="abc12345",
            why_blocked="x", required_choice=["abort_task"],
            safe_resume_command="flow resume demo",
        )
        self.assertIn("\x1b]9;", n._stderr.getvalue())

    def test_unknown_terminal_falls_back_to_bel_only(self):
        n = self._notifier(term="screen")
        n.fire_block(
            block_type="acceptance_fail", phase=2,
            task_id="T1", issue_id="abc12345",
            why_blocked="x", required_choice=["abort_task"],
            safe_resume_command="flow resume demo",
        )
        out = n._stderr.getvalue()
        self.assertNotIn("\x1b]9;", out)
        self.assertIn("\x07", out)


class TestThrottleSemantics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def _notifier(self, throttle_min, tier2_enabled=True):
        n = Notifier(
            contract=_make_contract(throttle_min=throttle_min,
                                    tier2_enabled=tier2_enabled),
            slug="d",
            task_dir=Path(self.tmp),
            term_program="kitty",
        )
        n._stderr = io.StringIO()
        return n

    def test_throttle_zero_fires_every_event(self):
        """R9 clarified: throttle_min=0 = no throttle, every event fires."""
        n = self._notifier(throttle_min=0)
        for _ in range(3):
            n.fire_block(
                block_type="x", phase=2, task_id="T1", issue_id="i1",
                why_blocked="x", required_choice=["abort"],
                safe_resume_command="flow resume d",
            )
        self.assertEqual(n._stderr.getvalue().count("\x1b]9;"), 3)

    def test_throttle_5min_suppresses_same_task_issue(self):
        n = self._notifier(throttle_min=5)
        for _ in range(3):
            n.fire_block(
                block_type="x", phase=2, task_id="T1", issue_id="i1",
                why_blocked="x", required_choice=["abort"],
                safe_resume_command="flow resume d",
            )
        # Only first emission; second + third throttled
        self.assertEqual(n._stderr.getvalue().count("\x1b]9;"), 1)

    def test_throttle_does_not_suppress_tier1(self):
        """§5 row Tier 1: always writes blocked.md regardless of throttle."""
        n = self._notifier(throttle_min=5)
        for i in range(3):
            n.fire_block(
                block_type="x", phase=2, task_id="T1", issue_id="i1",
                why_blocked=f"event {i}", required_choice=["abort"],
                safe_resume_command="flow resume d",
            )
        # blocked.md exists each time (overwritten); content reflects last event
        live = Path(self.tmp) / "blocked.md"
        self.assertTrue(live.exists())
        self.assertIn("event 2", live.read_text(encoding="utf-8"))

    def test_tier2_disabled_silences_both_osc9_and_bel(self):
        n = self._notifier(throttle_min=0, tier2_enabled=False)
        n.fire_block(
            block_type="x", phase=2, task_id="T1", issue_id="i1",
            why_blocked="x", required_choice=["abort"],
            safe_resume_command="flow resume d",
        )
        out = n._stderr.getvalue()
        self.assertNotIn("\x1b]9;", out)
        self.assertNotIn("\x07", out)

    def test_throttle_does_not_apply_across_different_issue_ids(self):
        n = self._notifier(throttle_min=5)
        n.fire_block(
            block_type="x", phase=2, task_id="T1", issue_id="i1",
            why_blocked="x", required_choice=["abort"],
            safe_resume_command="flow resume d",
        )
        n.fire_block(
            block_type="y", phase=2, task_id="T1", issue_id="i2",
            why_blocked="y", required_choice=["abort"],
            safe_resume_command="flow resume d",
        )
        # Different (task, issue) keys → both fire
        self.assertEqual(n._stderr.getvalue().count("\x1b]9;"), 2)

    def test_throttle_does_not_apply_across_different_task_ids(self):
        n = self._notifier(throttle_min=5)
        n.fire_block(
            block_type="x", phase=2, task_id="T1", issue_id="i1",
            why_blocked="x", required_choice=["abort"],
            safe_resume_command="flow resume d",
        )
        n.fire_block(
            block_type="x", phase=2, task_id="T2", issue_id="i1",
            why_blocked="x", required_choice=["abort"],
            safe_resume_command="flow resume d",
        )
        self.assertEqual(n._stderr.getvalue().count("\x1b]9;"), 2)

    def test_terminal_event_bypasses_throttle(self):
        """§5 line 211–212: AFK-abort terminal events fire unconditionally."""
        n = self._notifier(throttle_min=5)
        # Burn the throttle window
        n.fire_block(
            block_type="x", phase=2, task_id="T1", issue_id="i1",
            why_blocked="x", required_choice=["abort"],
            safe_resume_command="flow resume d",
        )
        # Now fire terminal — should still emit
        n.fire_terminal(
            block_type="aborted_afk", task_id="T1",
            issue_id="i1", body="afk timeout reached",
        )
        # 2 emissions: 1 from fire_block + 1 from fire_terminal
        self.assertEqual(n._stderr.getvalue().count("\x1b]9;"), 2)

    def test_tier3_command_logs_note_to_stderr(self):
        """v0.8.1 schema-only — accepted but not executed; note written."""
        n = Notifier(
            contract=_make_contract(throttle_min=0,
                                    command="rm -rf /etc"),
            slug="d",
            task_dir=Path(self.tmp),
            term_program="kitty",
        )
        n._stderr = io.StringIO()
        n.fire_block(
            block_type="x", phase=2, task_id="T1", issue_id="i1",
            why_blocked="x", required_choice=["abort"],
            safe_resume_command="flow resume d",
        )
        out = n._stderr.getvalue()
        self.assertIn("Tier 3", out)  # NOTE about deferred Tier 3
        self.assertIn("v0.8.2", out)


class TestThrottleInputValidation(unittest.TestCase):
    """L-class: keys-in-dict bypass with non-string fields. task_id/issue_id
    must be string; non-string raises TypeError up-front."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def _notifier(self):
        n = Notifier(
            contract=_make_contract(throttle_min=5),
            slug="d",
            task_dir=Path(self.tmp),
            term_program="kitty",
        )
        n._stderr = io.StringIO()
        return n

    def test_non_string_task_id_rejected(self):
        n = self._notifier()
        with self.assertRaises(TypeError):
            n.fire_block(
                block_type="x", phase=2, task_id=42, issue_id="i1",
                why_blocked="x", required_choice=["abort"],
                safe_resume_command="flow resume d",
            )

    def test_non_string_issue_id_rejected(self):
        n = self._notifier()
        with self.assertRaises(TypeError):
            n.fire_block(
                block_type="x", phase=2, task_id="T1", issue_id=None,
                why_blocked="x", required_choice=["abort"],
                safe_resume_command="flow resume d",
            )


class TestArchiveOnResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def _notifier(self):
        n = Notifier(
            contract=_make_contract(throttle_min=0),
            slug="d",
            task_dir=Path(self.tmp),
            term_program="kitty",
        )
        n._stderr = io.StringIO()
        return n

    def test_resume_archive_moves_blocked_md(self):
        """Q5.3: live blocked.md → archive/blocked/<ts>.md on resume."""
        n = self._notifier()
        n.fire_block(
            block_type="x", phase=2, task_id="T1", issue_id="i1",
            why_blocked="x", required_choice=["abort"],
            safe_resume_command="flow resume d",
        )
        live_path = Path(self.tmp) / "blocked.md"
        self.assertTrue(live_path.exists())

        archived = n.archive_on_resume(ts="2026-05-06T00:05:00Z")
        self.assertFalse(live_path.exists())
        self.assertTrue(archived.exists())
        self.assertEqual(archived.parent.name, "blocked")
        self.assertEqual(archived.parent.parent.name, "archive")

    def test_archive_when_no_blocked_md_raises(self):
        n = self._notifier()
        with self.assertRaises(FileNotFoundError):
            n.archive_on_resume(ts="2026-05-06T00:05:00Z")


class TestLockTimeoutFailOpen(unittest.TestCase):
    """T16 fix-pass [85]: when _locked_throttle_rmw times out (external
    holder of LOCK_EX for >2s), _allowed_by_throttle MUST fail open per
    module docstring contract — throttle is ergonomic, not a safety
    boundary. Pre-fix: default decision={"allowed": False} caused
    fail-closed under contention (operator missed emissions)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def test_lock_timeout_emits_osc9_anyway(self):
        n = Notifier(
            contract=Contract(
                contract_schema_version=1,
                autonomy_mode="auto",
                created_at="2026-05-06T00:00:00Z",
                notification={
                    "command": None,
                    "throttle_min": 5,  # throttle on — exercises lock path
                    "tier2_enabled": True,
                },
            ),
            slug="d",
            task_dir=Path(self.tmp),
            term_program="kitty",
        )
        n._stderr = io.StringIO()

        # External holder grabs LOCK_EX on the throttle file and holds it
        # past the Notifier's 2.0s timeout. We pre-create the file so the
        # holder and Notifier touch the SAME inode (the helper opens
        # "a+" which is create-if-missing, so even if we didn't pre-create
        # this would work — but being explicit makes the test less
        # filesystem-dependent).
        throttle_path = Path(self.tmp) / THROTTLE_FILENAME
        throttle_path.write_text("{}\n", encoding="utf-8")

        release = threading.Event()
        ready = threading.Event()

        def _hold_lock():
            with open(throttle_path, "a+", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                ready.set()
                # Hold past Notifier's 2.0s deadline.
                release.wait(timeout=5.0)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        holder = threading.Thread(target=_hold_lock, daemon=True)
        holder.start()
        try:
            ready.wait(timeout=2.0)
            self.assertTrue(ready.is_set(), "lock holder failed to start")
            t0 = time.monotonic()
            n.fire_block(
                block_type="x", phase=2, task_id="T1", issue_id="i1",
                why_blocked="contended", required_choice=["abort"],
                safe_resume_command="flow resume d",
            )
            elapsed = time.monotonic() - t0
        finally:
            release.set()
            holder.join(timeout=2.0)

        # Sanity: we actually hit the lock-timeout path (~2s wait).
        self.assertGreaterEqual(
            elapsed, 1.5,
            f"expected to wait for lock timeout (~2s), got {elapsed:.2f}s",
        )
        # Contract: OSC 9 STILL emitted despite lock timeout.
        out = n._stderr.getvalue()
        self.assertIn(
            "\x1b]9;", out,
            "lock timeout MUST fail open per docstring (operator gets the "
            "emission); pre-fix this was fail-closed and emitted nothing",
        )


class TestOSCInjectionSanitize(unittest.TestCase):
    """T16 fix-pass [82]: OSC 9 body must reject control-char injection
    (e.g. nested OSC sequences via embedded BEL/ESC) and truncate to
    bounded length. Tier 1 markdown unaffected (no terminal escape
    context)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def _notifier(self):
        n = Notifier(
            contract=Contract(
                contract_schema_version=1,
                autonomy_mode="auto",
                created_at="2026-05-06T00:00:00Z",
                notification={
                    "command": None,
                    "throttle_min": 0,
                    "tier2_enabled": True,
                },
            ),
            slug="d",
            task_dir=Path(self.tmp),
            term_program="kitty",
        )
        n._stderr = io.StringIO()
        return n

    def test_control_chars_stripped_from_osc9_body(self):
        """\\x07 (BEL) closes OSC 9; \\x1b]0; would open OSC 0
        (window-title set). The exploit string ``pwn\\x07\\x1b]0;HACKED\\x07``
        must NOT appear verbatim in the emitted OSC 9. After sanitize,
        the only \\x07 in output is the legitimate single OSC 9 closer."""
        n = self._notifier()
        attack = "pwn\x07\x1b]0;HACKED\x07"
        n.fire_block(
            block_type="x", phase=2, task_id="T1", issue_id="i1",
            why_blocked=attack, required_choice=["abort"],
            safe_resume_command="flow resume d",
        )
        out = n._stderr.getvalue()
        # Critical: the OSC 0 introducer (\x1b]0;) must not survive —
        # that's the actual injection vector. Without ESC, the literal
        # bytes "]0;HACKED" are inert ASCII payload inside our OSC 9.
        self.assertNotIn("\x1b]0;", out)
        # No stray ESC: only one — the OSC 9 opener (\x1b]9;).
        self.assertEqual(out.count("\x1b"), 1)
        # Exactly one BEL — the OSC 9 closer. Pre-fix the embedded
        # \x07 in body would close our OSC 9 prematurely AND a second
        # \x07 from the attacker's "]0;HACKED\x07" would close the
        # attacker's nested OSC. Post-fix: only the legitimate trailer.
        self.assertEqual(out.count("\x07"), 1)
        # Legal characters of the attack ("pwn", ";", "0", "HACKED")
        # are printable ASCII and DO survive as inert plain text inside
        # the OSC 9 body — without ESC]0; introducer, no terminal
        # interprets them as an escape sequence.
        self.assertIn("pwn", out)

    def test_long_body_truncated_to_max_len(self):
        """Reviewer note: long OSC strings can be silently dropped by
        terminals (xterm caps internal OSC buffer). Cap at 200 chars."""
        n = self._notifier()
        long_body = "A" * 5000
        n.fire_block(
            block_type="x", phase=2, task_id="T1", issue_id="i1",
            why_blocked=long_body, required_choice=["abort"],
            safe_resume_command="flow resume d",
        )
        out = n._stderr.getvalue()
        # Total emitted OSC 9 = "\x1b]9;flow blocked: " + body + "\x07"
        # body length must be ≤ 200. Count the run of 'A's.
        a_run = out.count("A")
        self.assertLessEqual(a_run, 200)
        self.assertGreater(a_run, 0)  # didn't accidentally strip everything

    def test_legal_ascii_and_tab_pass_through(self):
        """Allowlist must not over-restrict: printable ASCII + tab
        survive verbatim. Operator-facing messages need full punctuation
        and (occasional) tabs without being mangled."""
        n = self._notifier()
        legal = "validation failed: file=src/foo.py\tline=42 (issue 'x')"
        n.fire_block(
            block_type="x", phase=2, task_id="T1", issue_id="i1",
            why_blocked=legal, required_choice=["abort"],
            safe_resume_command="flow resume d",
        )
        out = n._stderr.getvalue()
        self.assertIn(legal, out)


class TestSanitizeHelperUnit(unittest.TestCase):
    """Direct unit coverage for _sanitize_osc_text (helper exposed at
    module level so it's testable without going through the full
    notifier dispatch)."""

    def test_strips_bel(self):
        self.assertEqual(_sanitize_osc_text("a\x07b"), "ab")

    def test_strips_esc(self):
        self.assertEqual(_sanitize_osc_text("a\x1bb"), "ab")

    def test_strips_other_c0_controls(self):
        # NUL, BS, VT, FF, CR — all control chars except \t
        for ch in ("\x00", "\x08", "\x0b", "\x0c", "\r", "\n"):
            self.assertEqual(_sanitize_osc_text(f"x{ch}y"), "xy",
                             f"failed to strip {ch!r}")

    def test_keeps_tab(self):
        self.assertEqual(_sanitize_osc_text("a\tb"), "a\tb")

    def test_strips_non_ascii(self):
        # Non-ASCII handling inside OSC is implementation-defined;
        # we drop to be safe.
        self.assertEqual(_sanitize_osc_text("café"), "caf")

    def test_truncates_after_strip(self):
        # 250 chars of legal ASCII → 200 (default max_len).
        self.assertEqual(len(_sanitize_osc_text("A" * 250)), 200)
        # Strip happens BEFORE truncation: 250 control chars → 0,
        # then no truncation needed.
        self.assertEqual(_sanitize_osc_text("\x07" * 250), "")


if __name__ == "__main__":
    unittest.main()
