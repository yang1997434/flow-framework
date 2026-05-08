"""Bugfix unit — context_estimator 1M context misdetection.

Repro: transcript JSONL writes bare `claude-opus-4-7` (no `[1m]`
suffix) but the live session runs in 1M-context mode (settings.json
env aliases). The estimator's `MODEL_LIMITS` lookup returns 200_000,
so a 999kB transcript reports ~125% (capped 100%) when the truth is
~25%. 5x inflation.

Fix design (priority chain inside `_resolve_limit`):
  1. ``FLOW_CONTEXT_LIMIT`` env var (explicit override — positive int)
  2. ``~/.claude/settings.json::env::ANTHROPIC_DEFAULT_<BASE>_MODEL``
     ending with ``[1m]`` -> 1_000_000 (BASE inferred from detected
     model: opus / sonnet / haiku)
  3. ``MODEL_LIMITS`` table (existing behavior preserved)
  4. ``DEFAULT_LIMIT = 200_000`` fallback

Tests below cover all four rungs + malformed/missing settings.json
defensive paths and one integration smoke proving a 999kB transcript
reports ~25% (not 100%) when the env alias signals 1M.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import context_estimator  # noqa: E402  type: ignore
from common.context_estimator import (  # noqa: E402  type: ignore
    DEFAULT_LIMIT,
    MODEL_LIMITS,
    _resolve_limit,
    estimate_context_pct,
)


class _SettingsHomeMixin:
    """Provide a tmp HOME with a controllable .claude/settings.json."""

    def _make_home(self, *, settings_payload=None, write_raw=None):
        """Create tmp HOME; return its Path. Patch Path.home() automatically."""
        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(home,
                                                             ignore_errors=True))
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path = claude_dir / "settings.json"
        if write_raw is not None:
            settings_path.write_text(write_raw, encoding="utf-8")
        elif settings_payload is not None:
            settings_path.write_text(
                json.dumps(settings_payload), encoding="utf-8"
            )
        # else: settings.json missing entirely
        patcher = mock.patch.object(Path, "home", return_value=home)
        patcher.start()
        self.addCleanup(patcher.stop)
        return home


class TestResolveLimitPriorityChain(_SettingsHomeMixin, unittest.TestCase):
    """Verify the 4-rung priority chain in `_resolve_limit`."""

    # ---- Rung 1: FLOW_CONTEXT_LIMIT explicit env override ------------------
    def test_flow_context_limit_env_overrides_all(self):
        """FLOW_CONTEXT_LIMIT=500000 wins regardless of model id / settings."""
        # Even with a settings.json that would imply 1M, the env override wins.
        self._make_home(settings_payload={
            "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7[1m]"}
        })
        with mock.patch.dict(os.environ,
                             {"FLOW_CONTEXT_LIMIT": "500000"}, clear=False):
            self.assertEqual(_resolve_limit("claude-opus-4-7"), 500_000)
            self.assertEqual(_resolve_limit("claude-future-x-1"), 500_000)
            # Even when the model id maps to the table value, override wins:
            self.assertEqual(_resolve_limit("claude-opus-4-7[1m]"), 500_000)

    def test_flow_context_limit_env_invalid_falls_through(self):
        """Non-int / non-positive FLOW_CONTEXT_LIMIT is ignored, not crash."""
        self._make_home()  # no settings.json
        for bad in ("abc", "-1", "0", "", "  ", "1.5"):
            with mock.patch.dict(os.environ,
                                 {"FLOW_CONTEXT_LIMIT": bad}, clear=False):
                self.assertEqual(
                    _resolve_limit("claude-opus-4-7"),
                    MODEL_LIMITS["claude-opus-4-7"],
                    msg=f"bad value {bad!r} should fall through",
                )

    # ---- Rung 2: settings.json [1m] alias upgrades to 1_000_000 ------------
    def test_settings_json_opus_1m_alias_upgrades_limit(self):
        """opus base + ANTHROPIC_DEFAULT_OPUS_MODEL=...[1m] -> 1_000_000."""
        self._make_home(settings_payload={
            "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7[1m]"}
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(_resolve_limit("claude-opus-4-7"), 1_000_000)

    def test_settings_json_sonnet_1m_alias_upgrades_limit(self):
        """sonnet base + ANTHROPIC_DEFAULT_SONNET_MODEL=...[1m] -> 1_000_000."""
        self._make_home(settings_payload={
            "env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6[1m]"}
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(_resolve_limit("claude-sonnet-4-6"), 1_000_000)

    def test_settings_json_haiku_1m_alias_upgrades_limit(self):
        """haiku base + ANTHROPIC_DEFAULT_HAIKU_MODEL=...[1m] -> 1_000_000."""
        self._make_home(settings_payload={
            "env": {"ANTHROPIC_DEFAULT_HAIKU_MODEL":
                    "claude-haiku-4-5-20251001[1m]"}
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-haiku-4-5-20251001"), 1_000_000)

    def test_settings_json_alias_only_upgrades_for_matching_base_2a(self):
        """2a alone (no plan-level fallback) only upgrades the matching base.

        This pins the rung-2a *direct match* semantics: the OPUS env key
        derived from the model is consulted directly. With plan-level
        rung 2b layered on top, the sonnet [1m] alias *does* upgrade opus
        — that path is covered by
        `test_plan_level_heuristic_sonnet_alias_upgrades_opus`. Here we
        verify rung 2a in isolation by patching out 2b's helper, so
        regressions to either layer surface independently.
        """
        self._make_home(settings_payload={
            "env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6[1m]"}
            # no OPUS alias
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            with mock.patch.object(
                context_estimator,
                "_any_settings_alias_signals_1m",
                return_value=False,
            ):
                # opus model with 2b stubbed off -> falls through to
                # MODEL_LIMITS table = 200k (2a never matches because
                # OPUS alias is missing).
                self.assertEqual(
                    _resolve_limit("claude-opus-4-7"),
                    MODEL_LIMITS["claude-opus-4-7"],
                )

    # ---- Rung 2 negative: [1m] suffix absent => not upgraded ----------------
    def test_settings_json_no_1m_suffix_uses_default_table(self):
        """env alias without [1m] suffix -> use MODEL_LIMITS table."""
        self._make_home(settings_payload={
            "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7"}
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-opus-4-7"),
                MODEL_LIMITS["claude-opus-4-7"],
            )

    def test_settings_json_alias_non_string_falls_through(self):
        """L-class type guard: non-string value -> ignore, fall through."""
        self._make_home(settings_payload={
            "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": 12345}  # not a str
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-opus-4-7"),
                MODEL_LIMITS["claude-opus-4-7"],
            )

    # ---- Rung 3 & 4: settings.json missing / malformed defensive paths -----
    def test_settings_json_missing_falls_back_to_table(self):
        """settings.json absent -> table lookup."""
        self._make_home()  # no settings.json
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-opus-4-7"),
                MODEL_LIMITS["claude-opus-4-7"],
            )

    def test_settings_json_malformed_falls_back_to_table(self):
        """settings.json with invalid JSON -> defensive fall-through, no raise."""
        self._make_home(write_raw="{not valid json")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-opus-4-7"),
                MODEL_LIMITS["claude-opus-4-7"],
            )

    def test_settings_json_no_env_block_falls_back_to_table(self):
        """settings.json present but no `env` key -> table lookup."""
        self._make_home(settings_payload={"some_other_setting": True})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-opus-4-7"),
                MODEL_LIMITS["claude-opus-4-7"],
            )

    def test_settings_json_env_not_dict_falls_back_to_table(self):
        """settings.json `env` is not a dict -> defensive fall-through."""
        self._make_home(settings_payload={"env": "not a dict"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-opus-4-7"),
                MODEL_LIMITS["claude-opus-4-7"],
            )

    # ---- Rung 4: unknown model id -----------------------------------------
    def test_unknown_model_uses_default_limit(self):
        """model not in MODEL_LIMITS table -> DEFAULT_LIMIT."""
        self._make_home()  # no settings.json
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-future-model-x"), DEFAULT_LIMIT)
            self.assertEqual(_resolve_limit(None), DEFAULT_LIMIT)

    # ---- Rung 3 still wins when alias has no [1m] but model in table ------
    def test_settings_json_with_unrelated_keys_does_not_break_table(self):
        """settings.json with other env keys but no relevant alias -> table."""
        self._make_home(settings_payload={
            "env": {"FOO": "bar", "ANTHROPIC_BASE_URL": "https://example"}
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-opus-4-7"),
                MODEL_LIMITS["claude-opus-4-7"],
            )

    # ---- Rung 2b: plan-level [1m] heuristic --------------------------------
    # 1M context is an Anthropic plan-level paid add-on. ANY *_MODEL alias
    # ending [1m] signals the plan grants 1M to all models in the session,
    # so a model whose own alias is missing should still upgrade to 1M.
    def test_plan_level_heuristic_sonnet_alias_upgrades_opus(self):
        """[P1] reproduce: sonnet [1m] alias, no opus alias -> opus = 1M.

        Codex round-1 finding: production settings.json has
        ANTHROPIC_DEFAULT_SONNET_MODEL=...[1m] but no OPUS variant.
        Pre-2b code derived only the OPUS env key, missed the signal,
        and fell through to MODEL_LIMITS table = 200k. Result: bare
        `claude-opus-4-7` transcript reported ~100% instead of ~25%.
        """
        self._make_home(settings_payload={
            "env": {
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6[1m]",
                # no OPUS alias — this is the bug-trigger configuration
            }
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(_resolve_limit("claude-opus-4-7"), 1_000_000)

    def test_plan_level_heuristic_haiku_alias_upgrades_sonnet(self):
        """haiku [1m] alias, no sonnet alias -> sonnet = 1M (plan-level)."""
        self._make_home(settings_payload={
            "env": {
                "ANTHROPIC_DEFAULT_HAIKU_MODEL":
                    "claude-haiku-4-5-20251001[1m]",
                # no SONNET alias
            }
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(_resolve_limit("claude-sonnet-4-6"), 1_000_000)

    def test_plan_level_heuristic_no_aliases_with_1m_no_upgrade(self):
        """No alias has [1m] suffix -> stay on MODEL_LIMITS table."""
        self._make_home(settings_payload={
            "env": {
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL":
                    "claude-haiku-4-5-20251001",
            }
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-opus-4-7"),
                MODEL_LIMITS["claude-opus-4-7"],
            )

    def test_plan_level_heuristic_explicit_alias_takes_precedence(self):
        """When 2a matches, 2b is unreachable. Both paths lead to 1M but 2a
        runs first — verifies precedence ordering didn't regress.
        """
        self._make_home(settings_payload={
            "env": {
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7[1m]",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6[1m]",
            }
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            # opus -> 2a hits (matching base alias) -> 1M
            self.assertEqual(_resolve_limit("claude-opus-4-7"), 1_000_000)

    def test_plan_level_heuristic_non_matching_keys_ignored(self):
        """Random *_MODEL-suffixed keys without ANTHROPIC_DEFAULT_ prefix
        must not trigger the plan-level upgrade.
        """
        self._make_home(settings_payload={
            "env": {
                "OTHER_VENDOR_MODEL": "some-model[1m]",  # not Anthropic
                "ANTHROPIC_BASE_URL": "https://example[1m]",  # wrong shape
            }
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-opus-4-7"),
                MODEL_LIMITS["claude-opus-4-7"],
            )

    def test_plan_level_heuristic_non_string_value_ignored(self):
        """L-class: non-string [1m]-ish payload doesn't fool the scan."""
        self._make_home(settings_payload={
            "env": {
                "ANTHROPIC_DEFAULT_SONNET_MODEL": ["claude-sonnet-4-6[1m]"],
            }
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-opus-4-7"),
                MODEL_LIMITS["claude-opus-4-7"],
            )

    # ---- Round-2 [P2]: explicit non-1M alias must short-circuit 2b -------
    # Codex round-2 finding: round-1's plan-level heuristic (rung 2b)
    # fired whenever rung 2a didn't return 1M, including the case where
    # the user explicitly set the matching base alias to a non-1M value
    # (e.g. ``ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7``, no ``[1m]``).
    # In that case a sibling alias with [1m] could silently upgrade the
    # explicitly-200k model to 1M, contradicting the user's selection
    # and underreporting context fill by 5x.
    #
    # Fix: rung 2b only fires when the matching alias is *absent* (None /
    # non-string), not merely "exists but no [1m]". Cases below pin both
    # halves of that distinction.
    def test_explicit_non_1m_alias_blocks_plan_level_upgrade(self):
        """[P2] reproduce: explicit non-1M opus alias + sibling [1m] -> 200k.

        Production case: ``ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7``
        (bare, explicit 200k) coexists with
        ``ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6[1m]``. Pre-fix
        rung 2b would scan the env block, find the sonnet [1m] alias,
        and upgrade the opus transcript to 1M — contradicting the
        explicit OPUS choice. Post-fix: rung 2a case (ii) short-circuits
        to the table value (200k) and rung 2b never runs.
        """
        self._make_home(settings_payload={
            "env": {
                # Explicit non-1M choice for OPUS
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7",
                # Sibling [1m] alias that pre-fix would have caused
                # spurious plan-level upgrade
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6[1m]",
            }
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(
                _resolve_limit("claude-opus-4-7"),
                MODEL_LIMITS["claude-opus-4-7"],  # 200_000
            )
            # Sanity: sonnet itself still resolves to 1M via rung 2a
            self.assertEqual(_resolve_limit("claude-sonnet-4-6"), 1_000_000)

    def test_absent_alias_still_triggers_plan_level_upgrade(self):
        """Sanity: round-1 behavior preserved when matching alias absent.

        Distinct from ``test_plan_level_heuristic_sonnet_alias_upgrades_opus``
        only in framing: that test was the round-1 [P1] reproduce; this
        one re-asserts the same invariant from the round-2 perspective —
        rung 2b *must* still upgrade when the matching base alias is
        truly absent (not merely "exists but no [1m]"), so the round-2
        guard didn't accidentally lobotomise the plan-level heuristic.
        """
        self._make_home(settings_payload={
            "env": {
                # OPUS alias deliberately absent
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6[1m]",
            }
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            self.assertEqual(_resolve_limit("claude-opus-4-7"), 1_000_000)


class TestEstimatePctIntegration(_SettingsHomeMixin, unittest.TestCase):
    """End-to-end: real transcript fixture + env alias -> sane pct."""

    def test_estimate_pct_uses_resolved_limit_real_session(self):
        """999kB transcript + opus 1M alias -> ~25% (not 100%).

        Reproduces the user-reported bug exactly: bare `claude-opus-4-7`
        in transcript, env alias signals 1M context. Pre-fix returned 100;
        post-fix returns ~25.
        """
        # Build a tmp transcript with a leading message containing model id
        tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(
            tmpdir, ignore_errors=True))
        transcript = tmpdir / "session.jsonl"
        # First line: a JSONL record carrying message.model = bare opus.
        # Pad the rest of the file to ~999_000 bytes so size_bytes/4
        # = 249_750 estimated tokens. With 1M ceiling that's ~25%.
        first_line = json.dumps(
            {"type": "user", "message": {"model": "claude-opus-4-7",
                                          "content": "hi"}}
        ) + "\n"
        # Pad with anonymous lines (won't override model detection — first
        # match wins per `_detect_model`).
        target_size = 999_000
        with transcript.open("w", encoding="utf-8") as f:
            f.write(first_line)
            remaining = target_size - len(first_line.encode("utf-8"))
            # one filler line is ~80 bytes
            filler = json.dumps({"type": "filler", "data": "x" * 60}) + "\n"
            count = remaining // len(filler.encode("utf-8"))
            for _ in range(count):
                f.write(filler)

        size_bytes = transcript.stat().st_size
        self.assertGreater(size_bytes, 900_000)
        self.assertLess(size_bytes, 1_050_000)

        # Set env alias signaling opus is in 1M mode.
        self._make_home(settings_payload={
            "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-4-7[1m]"}
        })
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            pct, conf = estimate_context_pct(transcript)

        # 999_000 / 4 / 1_000_000 = 24.975 -> rounded 25
        self.assertIsNotNone(pct)
        self.assertGreaterEqual(pct, 20)
        self.assertLessEqual(pct, 30)
        self.assertEqual(conf, "high")  # >= 10kb + model identified

    def test_estimate_pct_pre_fix_behavior_without_alias(self):
        """Sanity: same fixture without [1m] alias -> pre-fix style 100% cap.

        This pins existing behavior so we can prove the alias is what
        flips the answer (not the test fixture changing).
        """
        tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(
            tmpdir, ignore_errors=True))
        transcript = tmpdir / "session.jsonl"
        first_line = json.dumps(
            {"type": "user", "message": {"model": "claude-opus-4-7",
                                          "content": "hi"}}
        ) + "\n"
        target_size = 999_000
        with transcript.open("w", encoding="utf-8") as f:
            f.write(first_line)
            remaining = target_size - len(first_line.encode("utf-8"))
            filler = json.dumps({"type": "filler", "data": "x" * 60}) + "\n"
            count = remaining // len(filler.encode("utf-8"))
            for _ in range(count):
                f.write(filler)

        # No env alias / no settings.json
        self._make_home()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLOW_CONTEXT_LIMIT", None)
            pct, conf = estimate_context_pct(transcript)

        # 999_000 / 4 / 200_000 = 124.875 -> capped at 100
        self.assertEqual(pct, 100)
        self.assertEqual(conf, "high")


if __name__ == "__main__":
    unittest.main()
