#!/usr/bin/env python3
"""Smoke tests for sub-project #1 — install / doctor logic.

Covers the dangerous bits without touching the real ~/.claude/:

  - dependencies.json schema sanity
  - settings.template.json renders to valid JSON
  - merge_hooks idempotency + isolation guarantees (Issue #415 risk)
  - flow_install.py / flow_doctor.py importability
"""
from __future__ import annotations

import importlib
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class DependenciesJsonShape(unittest.TestCase):
    """dependencies.json must have the structure that install/doctor expects."""

    @classmethod
    def setUpClass(cls):
        cls.deps = json.loads((REPO_ROOT / "dependencies.json").read_text(encoding="utf-8"))

    def test_top_level_keys(self):
        for key in ("version", "system_commands", "marketplaces", "plugins"):
            self.assertIn(key, self.deps, f"missing top-level key: {key}")

    def test_system_commands_required_have_name_and_hint(self):
        for entry in self.deps["system_commands"]["required"]:
            self.assertIn("name", entry)
            self.assertIn("hint", entry)

    def test_marketplaces_have_name_and_source(self):
        for mp in self.deps["marketplaces"]:
            self.assertIn("name", mp)
            self.assertIn("source", mp)

    def test_required_plugins_reference_known_marketplace(self):
        mp_names = {mp["name"] for mp in self.deps["marketplaces"]}
        for plugin in self.deps["plugins"]["required"]:
            self.assertIn(
                plugin["marketplace"], mp_names,
                f"required plugin {plugin['name']} references unknown marketplace {plugin['marketplace']}",
            )

    def test_plugins_have_capabilities_listed(self):
        for tier in ("required", "optional"):
            for plugin in self.deps["plugins"].get(tier, []):
                self.assertIn("capabilities", plugin, f"plugin {plugin['name']} missing capabilities")
                self.assertIsInstance(plugin["capabilities"], list)
                self.assertGreater(len(plugin["capabilities"]), 0)


class HookTemplateRendersValidJson(unittest.TestCase):
    """The template's {{REPO_ROOT}} placeholder must yield valid JSON when substituted."""

    def test_render_with_repo_root(self):
        template = (REPO_ROOT / "claude" / "hooks" / "settings.template.json").read_text(encoding="utf-8")
        rendered = template.replace("{{REPO_ROOT}}", "/data/Claude/flow-framework")
        try:
            data = json.loads(rendered)
        except json.JSONDecodeError as e:
            self.fail(f"rendered template is not valid JSON: {e}")
        self.assertIn("hooks", data)
        events = data["hooks"]
        # 5 hook scripts should have entries
        all_commands = []
        for entries in events.values():
            for entry in entries:
                for h in entry.get("hooks", []):
                    if h.get("command"):
                        all_commands.append(h["command"])
        self.assertEqual(len(all_commands), 10,
                         "expect 10 commands: 3x SessionStart + 1 UserPromptSubmit + 1 PreToolUse(Task) "
                         "+ 3 PostToolUse(Bash/Edit/Write) + 1 Stop + 1 PreCompact")
        for cmd in all_commands:
            self.assertIn("/data/Claude/flow-framework", cmd)
            self.assertNotIn("{{REPO_ROOT}}", cmd, "all placeholders must be replaced")

    def test_each_event_entry_has_explicit_matcher(self):
        """Issue #415 mitigation: every flow hook entry must have its own matcher field."""
        template = (REPO_ROOT / "claude" / "hooks" / "settings.template.json").read_text(encoding="utf-8")
        data = json.loads(template.replace("{{REPO_ROOT}}", "/x"))
        for event_name, entries in data["hooks"].items():
            for i, entry in enumerate(entries):
                self.assertIn("matcher", entry, f"{event_name}[{i}] missing matcher field")


class MergeHooksIsolation(unittest.TestCase):
    """merge_hooks must preserve user's existing entries AND keep flow's hooks
    in their own matcher entries (never bundled with sibling commands)."""

    @classmethod
    def setUpClass(cls):
        sys.modules.pop("flow_install", None)
        cls.mod = importlib.import_module("flow_install")

    def _new_template(self) -> dict:
        return {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{
                            "type": "command",
                            "command": "python3 /tmp/test-flow-framework/claude/hooks/post-tool-bash.py",
                        }],
                    }
                ],
            }
        }

    def test_empty_existing_settings_gets_flow_hooks(self):
        result = self.mod.merge_hooks({}, self._new_template())
        self.assertEqual(len(result["hooks"]["PostToolUse"]), 1)
        cmds = [h["command"] for h in result["hooks"]["PostToolUse"][0]["hooks"]]
        self.assertEqual(len(cmds), 1)
        self.assertIn("post-tool-bash.py", cmds[0])

    def test_idempotent_no_duplicate_on_reinstall(self):
        existing = self._new_template()  # already has flow's entry
        result = self.mod.merge_hooks(existing, self._new_template())
        self.assertEqual(len(result["hooks"]["PostToolUse"]), 1, "must not duplicate an identical entry")

    def test_user_existing_command_preserved_alongside_flow(self):
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "/usr/bin/user-script.sh"}],
                    }
                ],
            }
        }
        result = self.mod.merge_hooks(existing, self._new_template())
        # User's entry stays, flow's appended as a SEPARATE entry
        self.assertEqual(len(result["hooks"]["PostToolUse"]), 2)
        all_cmds = [
            h["command"]
            for entry in result["hooks"]["PostToolUse"]
            for h in entry["hooks"]
        ]
        self.assertIn("/usr/bin/user-script.sh", all_cmds)
        self.assertTrue(any("post-tool-bash.py" in c for c in all_cmds))

    def test_repo_root_change_does_not_accumulate_duplicates(self):
        """Re-installing flow from a DIFFERENT repo path must replace the old
        flow-owned entry, not append a sibling. Otherwise both fire forever."""
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command",
                                   "command": "python3 /old/path/flow-framework/claude/hooks/post-tool-bash.py"}],
                    }
                ],
            }
        }
        new_template = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command",
                                   "command": "python3 /new/path/flow-framework/claude/hooks/post-tool-bash.py"}],
                    }
                ],
            }
        }
        result = self.mod.merge_hooks(existing, new_template)
        commands = [
            h["command"] for entry in result["hooks"]["PostToolUse"] for h in entry["hooks"]
        ]
        self.assertEqual(len(commands), 1, "old flow entry must be replaced, not duplicated")
        self.assertIn("/new/path/", commands[0])

    def test_other_settings_keys_preserved(self):
        existing = {
            "model": "claude-opus-4-7",
            "theme": "dark",
            "hooks": {},
        }
        result = self.mod.merge_hooks(existing, self._new_template())
        self.assertEqual(result["model"], "claude-opus-4-7")
        self.assertEqual(result["theme"], "dark")
        self.assertIn("hooks", result)


class PromptRendererSubstitutesRepoRoot(unittest.TestCase):
    """Slash command and skill prompts embed {{REPO_ROOT}} so the model can
    `sys.path.insert` to import helpers under scripts/ at runtime. The
    flow_capability.render() pipeline only matches {{capability:...}} and
    {{model:...}} — so cmd_render_prompts MUST substitute {{REPO_ROOT}} itself
    before delegating, otherwise the literal token lands in the user's
    ~/.claude/commands/flow/pause.md and Steps 6-8 silently no-op.
    """

    def test_pause_md_repo_root_is_substituted(self):
        """Render pause.md the same way cmd_render_prompts does and verify
        no {{REPO_ROOT}} survives + the absolute repo path is present."""
        sys.modules.pop("flow_capability", None)
        from flow_capability import load_registry, render

        repo_abs = REPO_ROOT.resolve()
        src = REPO_ROOT / "claude" / "commands" / "flow" / "pause.md"
        self.assertTrue(src.is_file(), f"missing prompt source: {src}")

        text = src.read_text(encoding="utf-8")
        # Sanity: the source DOES contain the placeholder (otherwise this
        # test is vacuous; if Task 11's pause.md changes, update this).
        self.assertIn("{{REPO_ROOT}}", text,
                      "pause.md is expected to contain {{REPO_ROOT}} as a substitution target")

        # Mirror the install-time render path
        text = text.replace("{{REPO_ROOT}}", str(repo_abs))
        rendered, errors = render(text, load_registry())

        self.assertEqual(errors, [], f"unexpected render errors: {errors}")
        self.assertNotIn("{{REPO_ROOT}}", rendered,
                         "{{REPO_ROOT}} must be fully substituted before write")
        # The injected sys.path line must point at our absolute scripts dir
        self.assertIn(f'sys.path.insert(0, "{repo_abs}/scripts")', rendered,
                      "rendered pause.md must contain the absolute scripts path")

    def test_no_unsubstituted_repo_root_in_any_rendered_prompt(self):
        """Walk the same RENDER_TARGETS that cmd_render_prompts walks and
        verify NO rendered output contains the literal {{REPO_ROOT}} token."""
        sys.modules.pop("flow_install", None)
        sys.modules.pop("flow_capability", None)
        from flow_install import RENDER_TARGETS  # noqa: WPS433
        from flow_capability import load_registry, render

        repo_abs = REPO_ROOT.resolve()
        registry = load_registry()

        for src_rel, _dst in RENDER_TARGETS:
            src_root = REPO_ROOT / src_rel
            if not src_root.is_dir():
                continue
            for src_file in src_root.rglob("*"):
                if not src_file.is_file():
                    continue
                if src_file.suffix not in (".md", ".yaml", ".yml", ".json"):
                    continue
                text = src_file.read_text(encoding="utf-8")
                text = text.replace("{{REPO_ROOT}}", str(repo_abs))
                rendered, _errors = render(text, registry)
                self.assertNotIn(
                    "{{REPO_ROOT}}", rendered,
                    f"unsubstituted {{{{REPO_ROOT}}}} in rendered {src_file.relative_to(REPO_ROOT)}",
                )


class FlowInstallSubcommandsImportable(unittest.TestCase):
    """flow_install.py must expose all subcommands documented in the docstring."""

    def test_module_imports(self):
        sys.modules.pop("flow_install", None)
        mod = importlib.import_module("flow_install")
        for func in (
            "cmd_check_system",
            "cmd_register_marketplaces",
            "cmd_install_plugins",
            "cmd_install_hooks",
            "cmd_all",
            "merge_hooks",
        ):
            self.assertTrue(hasattr(mod, func), f"missing {func}")


class FlowDoctorImportable(unittest.TestCase):
    def test_module_imports(self):
        sys.modules.pop("flow_doctor", None)
        mod = importlib.import_module("flow_doctor")
        for func in ("check_system_commands", "check_plugins", "check_hook_isolation"):
            self.assertTrue(hasattr(mod, func), f"missing {func}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
