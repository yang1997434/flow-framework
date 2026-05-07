"""T16 unit — Notifier 3-tier dispatch + R9 throttle semantics.

Coverage (per plan §T16):
- Tier 2 OSC 9 emission with TERM_PROGRAM allowlist (kitty / unknown fallback)
- R9 throttle: throttle_min=0 fires every event; throttle_min=5 suppresses same-key
- Tier 1 (blocked.md) ALWAYS writes regardless of throttle
- tier2_enabled=false silences both OSC 9 and BEL
- Different (task_id, issue_id) keys do NOT cross-suppress
- §5 line 211–212: terminal events bypass throttle
- Q5.3: archive_on_resume moves blocked.md → archive/blocked/<ts>.md
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_notification import Notifier  # noqa: E402  type: ignore
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


if __name__ == "__main__":
    unittest.main()
