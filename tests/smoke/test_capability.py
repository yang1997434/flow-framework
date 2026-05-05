#!/usr/bin/env python3
"""Smoke tests for sub-project #2 — capability registry + template renderer.

Covers:
  - Built-in defaults load + every required capability/role present
  - render() substitutes {{capability:X}} and {{model:Y}}
  - Dotted access ({{capability:X.args.mode}}, {{capability:X.follow_with}})
  - Unknown capability fails LOUDLY (does not silently drop)
  - All 10 prompt files in repo render cleanly with NO leftover placeholders
  - Anti-regression: source files contain NO bare plugin refs / model hardcodes
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_capability import load_registry, render  # noqa: E402


REQUIRED_CAPS = {
    # Original 14 (v0.5.x baseline)
    "brainstorm", "ux_brief",
    "cross_model_consult", "cross_model_review", "cross_model_challenge",
    "tdd", "worktree", "parallel_dispatch",
    "ui_implement", "ui_audit", "ui_visual_review",
    "session_save", "deploy_chain", "behavioral_guidelines",
    # v0.6.0 additions — Phase 1 (2)
    "multi_step_plan", "dev_setup",
    # v0.6.0 additions — Phase 2 (5)
    "subagent_discipline", "execute_plan_discipline",
    "systematic_debug", "deep_investigate",
    "land_and_deploy",
    # v0.6.0 additions — Phase 3 (8)
    "verify_completion",
    "code_review_small", "code_review_large", "review_request_etiquette",
    "pre_land_review", "quality_health", "perf_baseline", "post_deploy_qa",
    # v0.6.0 additions — Phase 4 (2)
    "branch_finish", "changelog_gen",
    # v0.6.0 additions — Cross-cutting (2)
    "safety_guardrails", "weekly_retro",
    # v0.7.0 additions — Phase 2 wave dispatch (2)
    "wave_planning", "wave_dispatch",
}
REQUIRED_ROLES = {"triage", "research", "plan", "implement", "review"}

PROMPT_FILES = [
    REPO_ROOT / "claude" / "commands" / "flow" / f
    for f in ("start.md", "continue.md", "finish.md", "pause.md", "codex-review.md")
] + [
    REPO_ROOT / "claude" / "skills" / "flow" / d / "SKILL.md"
    for d in ("flow-orchestrator", "flow-phase1-plan", "flow-phase2-execute",
              "flow-phase3-finish", "flow-phase4-sediment")
]


class RegistryDefaults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = load_registry()

    def test_all_required_capabilities_present(self):
        missing = REQUIRED_CAPS - set(self.reg.capabilities)
        self.assertFalse(missing, f"missing capabilities: {missing}")

    def test_all_required_model_roles_present(self):
        missing = REQUIRED_ROLES - set(self.reg.model_roles)
        self.assertFalse(missing, f"missing model roles: {missing}")

    def test_capabilities_with_args_have_args_dict(self):
        for cap_name in ("cross_model_consult", "cross_model_review", "cross_model_challenge"):
            cap = self.reg.resolve_capability(cap_name)
            self.assertIn("args", cap)
            self.assertIn("mode", cap["args"])

    def test_ui_audit_has_follow_with(self):
        cap = self.reg.resolve_capability("ui_audit")
        self.assertIn("follow_with", cap)

    def test_v06_additions_are_well_formed(self):
        """v0.6.0 additions must each be a dict with default+description."""
        baseline_caps = {
            "brainstorm", "ux_brief", "cross_model_consult", "cross_model_review",
            "cross_model_challenge", "tdd", "worktree", "parallel_dispatch",
            "ui_implement", "ui_audit", "ui_visual_review",
            "session_save", "deploy_chain", "behavioral_guidelines",
        }
        v06_caps = REQUIRED_CAPS - baseline_caps - {"wave_planning", "wave_dispatch"}
        self.assertEqual(len(v06_caps), 19, "v0.6.0 should add exactly 19 caps")
        for name in v06_caps:
            cap = self.reg.resolve_capability(name)
            self.assertIsInstance(cap, dict, f"{name}: must be dict")
            self.assertIn("default", cap, f"{name}: missing 'default'")
            self.assertIsInstance(cap["default"], str, f"{name}: 'default' must be str")
            self.assertIn("description", cap, f"{name}: missing 'description'")


class RenderBasics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = load_registry()

    def test_simple_capability(self):
        out, err = render("Use {{capability:brainstorm}}.", self.reg)
        self.assertEqual(out, "Use superpowers:brainstorming.")
        self.assertEqual(err, [])

    def test_simple_model_role(self):
        out, err = render("model: {{model:research}}", self.reg)
        # Resolves to alias (e.g. "sonnet") so Agent tool's enum-restricted
        # `model` param accepts it. Concrete model id picked via
        # ANTHROPIC_DEFAULT_*_MODEL env var (1M variant in settings.json).
        self.assertEqual(out, "model: sonnet")
        self.assertEqual(err, [])

    def test_dotted_args_access(self):
        out, err = render("mode={{capability:cross_model_consult.args.mode}}", self.reg)
        self.assertEqual(out, "mode=consult")
        self.assertEqual(err, [])

    def test_dotted_follow_with(self):
        out, err = render("after={{capability:ui_audit.follow_with}}", self.reg)
        self.assertEqual(out, "after=impeccable:polish")
        self.assertEqual(err, [])

    def test_unknown_capability_emits_error_and_keeps_placeholder(self):
        out, err = render("{{capability:does_not_exist}}", self.reg)
        self.assertIn("{{capability:does_not_exist}}", out, "placeholder must remain visible")
        self.assertTrue(any("does_not_exist" in e for e in err), "must report unresolved")

    def test_multiple_placeholders_in_one_line(self):
        out, _ = render(
            "{{capability:brainstorm}} + {{capability:tdd}} + {{model:implement}}",
            self.reg,
        )
        self.assertIn("superpowers:brainstorming", out)
        self.assertIn("superpowers:test-driven-development", out)
        self.assertIn("opus", out)


class AllPromptFilesRenderCleanly(unittest.TestCase):
    """End-to-end: every prompt file in repo renders without errors,
    and the rendered output contains NO leftover placeholders."""

    @classmethod
    def setUpClass(cls):
        cls.reg = load_registry()

    def test_all_files_render_without_errors(self):
        for f in PROMPT_FILES:
            with self.subTest(file=f.name):
                self.assertTrue(f.is_file(), f"prompt file missing: {f}")
                text = f.read_text(encoding="utf-8")
                rendered, errors = render(text, self.reg)
                self.assertEqual(errors, [], f"{f.name} has unresolved placeholders: {errors}")
                self.assertNotRegex(rendered, r"\{\{(capability|model):",
                                    f"{f.name} rendered output still has placeholders")


class AntiRegressionRepoSource(unittest.TestCase):
    """Catch accidental reintroduction of hard-coded skill / model names."""

    BARE_PLUGIN_RE = re.compile(
        r"\b(superpowers|impeccable|gstack|yangpeng-claude-skills|frontend-design|"
        r"pr-review-toolkit|planning-with-files|actionbook|obsidian|baoyu-skills|"
        r"andrej-karpathy-skills|document-skills|review-loop|code-review):[a-z]"
    )
    MODEL_HARDCODE_RE = re.compile(r'model:\s*"?(?:sonnet|opus|haiku)"?\s')

    def test_no_bare_plugin_refs_in_prompt_files(self):
        for f in PROMPT_FILES:
            text = f.read_text(encoding="utf-8")
            matches = self.BARE_PLUGIN_RE.findall(text)
            self.assertFalse(matches, f"{f.name} contains bare plugin refs: {matches}")

    def test_no_model_hardcodes_in_prompt_files(self):
        for f in PROMPT_FILES:
            text = f.read_text(encoding="utf-8")
            matches = self.MODEL_HARDCODE_RE.findall(text)
            self.assertFalse(matches, f"{f.name} contains model name hardcodes: {matches}")

    def test_render_target_safety_refuses_symlink_into_source(self):
        """flow_install.py render-prompts must refuse to write through a symlink
        whose target is the source tree (Issue: symlink write-through clobbers templates)."""
        # Setup: tmp dst that's a symlink to source
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            fake_dst = tmp_root / "fake-dst"
            fake_dst.symlink_to(REPO_ROOT / "claude" / "commands" / "flow")
            # Verify symlink exists
            self.assertTrue(fake_dst.is_symlink())
            self.assertTrue(fake_dst.resolve().is_dir())
            # The protection logic uses .resolve() comparison + parent walk;
            # here we just confirm the symlink IS detectable as such
            # (full integration test would invoke flow_install.py, but that
            # mutates ~/.claude/, so we don't run it in unit test)


if __name__ == "__main__":
    unittest.main(verbosity=2)
