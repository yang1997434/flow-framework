"""The Section 7 hard rule: once ``auto_engaged`` is written, ANY
subsequent path leading to interactive mode MUST go through
``block + user choice``.

T22 owns: SKILL-routing assertions (Phase 2 / Phase 3 SKILL paths route
to ``flow orchestrator --auto-execute`` / ``flow acceptance --run``,
never silent-fallback to interactive).

T17 / T18 / Y8 budget-during-AFK-with-throttled-notification cross-cut
integration scenarios are deferred to v0.8.2 - this file owns the
SKILL-routing surface only.

v0.8.2.1 also pins the SKILL.md exit-code contract (rc=5 AFK park
disambiguated from rc=2 USAGE_ERROR) via ``TestSkillMdExitCodeContract``
below — normalized matching tolerates Markdown formatting drift.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TestSkillRoutingNoSilentSwitch(unittest.TestCase):
    """Section 7 hard rule: every code path that could degrade
    auto -> interactive must instead block. SKILLs are part of that
    surface (operator reads them and decides what to type).
    """

    def setUp(self):
        self.repo_root = REPO_ROOT
        self.phase2 = (
            self.repo_root
            / "claude/skills/flow/flow-phase2-execute/SKILL.md"
        )
        self.phase3 = (
            self.repo_root
            / "claude/skills/flow/flow-phase3-finish/SKILL.md"
        )

    def test_phase2_skill_invokes_flow_orchestrator(self):
        text = self.phase2.read_text(encoding="utf-8")
        self.assertIn(
            "flow orchestrator --auto-execute",
            text,
            "Phase 2 SKILL must invoke flow orchestrator --auto-execute "
            "on auto mode (T22 / Section 7 hard rule)",
        )

    def test_phase2_skill_does_not_say_v0_8_1_reserved(self):
        """v0.8.0 guard must be removed."""
        text = self.phase2.read_text(encoding="utf-8")
        self.assertNotIn("reserved for v0.8.1", text)
        self.assertNotIn(
            "v0.8.0 does not support autonomous dispatch",
            text,
        )

    def test_phase2_skill_states_no_silent_switch(self):
        """Hard rule must be present in the SKILL itself."""
        text = self.phase2.read_text(encoding="utf-8")
        self.assertIn("NEVER silently switch", text)

    def test_phase3_skill_invokes_flow_acceptance_run(self):
        text = self.phase3.read_text(encoding="utf-8")
        self.assertIn(
            "flow acceptance --run",
            text,
            "Phase 3 SKILL must invoke flow acceptance --run for verify gate",
        )

    def test_capabilities_default_promoted(self):
        path = self.repo_root / "claude/capabilities/defaults.json"
        caps_doc = json.loads(path.read_text(encoding="utf-8"))
        caps = caps_doc.get("capabilities", caps_doc)
        # autonomy_orchestrator + acceptance_verify both promoted: must
        # NOT skip silently (skip_if_not_available != True).
        self.assertNotEqual(
            caps["autonomy_orchestrator"].get(
                "skip_if_not_available", True
            ),
            True,
            "autonomy_orchestrator must NOT skip in v0.8.1",
        )
        self.assertNotEqual(
            caps["acceptance_verify"].get("skip_if_not_available", True),
            True,
            "acceptance_verify must NOT skip in v0.8.1",
        )
        # Promoted marker present so future readers know this is the
        # post-v0.8.1 shape, not the v0.8.0 stub layout.
        self.assertTrue(
            caps["autonomy_orchestrator"].get("v0_8_1_promoted"),
            "autonomy_orchestrator must carry v0_8_1_promoted=true",
        )
        self.assertTrue(
            caps["acceptance_verify"].get("v0_8_1_promoted"),
            "acceptance_verify must carry v0_8_1_promoted=true",
        )

    def test_capability_indirection_preserved(self):
        """K-class anti-regression: the ``default`` field must remain a
        skill identifier (string), not be inlined to a Python module
        path. Indirection is preserved per anti-regression rules.
        """
        path = self.repo_root / "claude/capabilities/defaults.json"
        caps_doc = json.loads(path.read_text(encoding="utf-8"))
        caps = caps_doc.get("capabilities", caps_doc)
        for cap in ("autonomy_orchestrator", "acceptance_verify"):
            default = caps[cap].get("default")
            self.assertIsInstance(default, str)
            # The default must be a SKILL handle (plugin:skill or
            # flow:skill form), not a python file path.
            self.assertNotIn(".py", default)
            self.assertIn(":", default)


def _normalize(s: str) -> str:
    """Normalize Markdown text for substring matching: strip backticks,
    bold markers, italic markers; collapse whitespace runs to single
    spaces; lowercase. Tolerates rendering variants like ``5 = AFK
    idle park`` vs ``**5** = AFK idle park``.
    """
    s = s.replace("`", "")
    s = s.replace("**", "")
    s = s.replace("*", "")
    s = re.sub(r"\s+", " ", s)
    return s.lower()


class TestSkillMdExitCodeContract(unittest.TestCase):
    """v0.8.2.1: pin the Phase 2 SKILL.md exit-code documentation
    contract. The narrative + table must reflect rc=5 = AFK idle park
    (recoverable), not the v0.8.2 rc=2 wording. Uses normalized
    matching to tolerate Markdown formatting drift.
    """

    def setUp(self):
        self.skill_path = (
            Path(__file__).resolve().parent.parent.parent
            / "claude/skills/flow/flow-phase2-execute/SKILL.md"
        )
        self.assertTrue(
            self.skill_path.exists(),
            f"SKILL.md missing: {self.skill_path}",
        )
        self.content = self.skill_path.read_text(encoding="utf-8")
        self.normalized = _normalize(self.content)

    def test_skill_md_documents_rc5_afk_park(self):
        """Normalized SKILL.md MUST contain ``5 = afk idle park``
        (any backtick / bold / italic / whitespace variant)."""
        self.assertIn(
            "5 = afk idle park",
            self.normalized,
            "SKILL.md must document rc=5 = AFK idle park "
            "(v0.8.2.1 contract); see normalized form.",
        )

    def test_skill_md_does_not_claim_rc2_park(self):
        """Normalized SKILL.md MUST NOT contain the v0.8.2 wordings
        ``rc=2 is recoverable park`` or ``2 = afk idle park``."""
        forbidden = (
            "rc=2 is recoverable park",
            "2 = afk idle park",
        )
        for needle in forbidden:
            self.assertNotIn(
                needle,
                self.normalized,
                f"SKILL.md must NOT contain {needle!r} "
                "(v0.8.2 wording superseded by v0.8.2.1).",
            )


if __name__ == "__main__":
    unittest.main()
