"""T1 — Budget counter foundation tests.

Covers Acceptance R2.1, R2.2, R2.3 from
`.flow/tasks/05-08-v0.8.2-p0-core/prd.md`:

- R2.1: 5 counters (tokens_in/out, cost_usd, active_wallclock_minutes,
  subagent_dispatches) — hit + near-hit per counter; one hit -> single
  hard-stop snapshot schema.
- R2.2: snapshot schema fields stable; `cost_usd` extra
  `model_id / pricing_version`.
- R2.3: token slack helper at 90%/100% boundaries (estimator ±20%
  precision-illusion guard).

I-class blindspot: counters reset on `make_default_set` (no leakage
from prior task instance).
G-class blindspot: dump/load round-trip preserves all fields; atomic
write semantics tested via persistence round-trip.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import budget_counter as bc  # noqa: E402  type: ignore
from common import snapshot as snap  # noqa: E402  type: ignore
from common.context_estimator import slack_state  # noqa: E402  type: ignore


_LIMITS = {
    "tokens_in": 1000.0,
    "tokens_out": 1000.0,
    "cost_usd": 10.0,
    "active_wallclock_minutes": 60.0,
    "subagent_dispatches": 5.0,
}


class TestFiveCounters(unittest.TestCase):
    """R2.1 — hit + near-hit per counter."""

    def test_make_default_set_has_five_named_counters(self):
        cs = bc.make_default_set(_LIMITS)
        self.assertEqual(
            set(cs.keys()),
            {
                "tokens_in",
                "tokens_out",
                "cost_usd",
                "active_wallclock_minutes",
                "subagent_dispatches",
            },
        )

    def test_make_default_set_resets_counters_no_leakage(self):
        # I-class: starting a new task must NOT inherit prior values.
        cs1 = bc.make_default_set(_LIMITS)
        cs1["tokens_in"].add(500.0)
        cs2 = bc.make_default_set(_LIMITS)
        self.assertEqual(cs2["tokens_in"].value, 0.0)
        self.assertIsNone(cs2["tokens_in"].hit_at_iso)

    def test_tokens_in_hit_and_near_hit(self):
        # T6.2 P2: warn boundary lowered to 80% (matches slack_state).
        cs = bc.make_default_set(_LIMITS)
        c = cs["tokens_in"]
        c.add(790.0)  # 79%
        self.assertFalse(c.is_warn())
        self.assertFalse(c.is_hit())
        c.add(10.0)  # 80%
        self.assertTrue(c.is_warn())
        self.assertFalse(c.is_hit())
        c.add(210.0)  # 101% (1010 of 1000)
        self.assertTrue(c.is_hit())

    def test_tokens_out_exact_100_pct_is_hit(self):
        cs = bc.make_default_set(_LIMITS)
        c = cs["tokens_out"]
        c.add(1000.0)
        self.assertTrue(c.is_hit())

    def test_active_wallclock_minutes_hit(self):
        # T6.2 P2: warn at 80% (matches slack_state).
        cs = bc.make_default_set(_LIMITS)
        c = cs["active_wallclock_minutes"]
        c.add(48.0)  # 80%
        self.assertTrue(c.is_warn())
        self.assertFalse(c.is_hit())
        c.add(13.0)  # 101.6%
        self.assertTrue(c.is_hit())

    def test_subagent_dispatches_hit(self):
        cs = bc.make_default_set(_LIMITS)
        c = cs["subagent_dispatches"]
        for _ in range(5):
            bc.register_dispatch(counters=cs)
        self.assertTrue(c.is_hit())

    def test_cost_usd_hit_with_pricing_metadata(self):
        cs = bc.make_default_set(_LIMITS)
        c = cs["cost_usd"]
        c.add(9.5, model_id="claude-opus-4-7", pricing_version="2026-05-01")
        self.assertTrue(c.is_warn())
        self.assertFalse(c.is_hit())
        c.add(0.6)
        self.assertTrue(c.is_hit())
        self.assertEqual(c.model_id, "claude-opus-4-7")
        self.assertEqual(c.pricing_version, "2026-05-01")

    def test_cost_usd_first_add_requires_model_and_pricing(self):
        cs = bc.make_default_set(_LIMITS)
        c = cs["cost_usd"]
        with self.assertRaises(ValueError):
            c.add(1.0)
        with self.assertRaises(ValueError):
            c.add(1.0, model_id="claude-opus-4-7")
        with self.assertRaises(ValueError):
            c.add(1.0, pricing_version="2026-05-01")
        # First valid add succeeds.
        c.add(1.0, model_id="claude-opus-4-7", pricing_version="2026-05-01")
        # Subsequent adds may omit metadata (already pinned).
        c.add(0.5)
        self.assertEqual(c.value, 1.5)


class TestSlackHelper(unittest.TestCase):
    """R2.3 — slack_state at 80% / 100% boundaries.

    T6.1 P2.2: warn threshold lowered from 90% to 80% to give 20%
    headroom matching the context_estimator's self-declared ±20%
    coarseness. A "85% used" estimate could already be at 102% real
    if undercounted by 20%.
    """

    def test_slack_state_below_warn_is_ok(self):
        self.assertEqual(slack_state(79.0, 100.0), "ok")

    def test_slack_state_at_80_pct_is_warn(self):
        self.assertEqual(slack_state(80.0, 100.0), "warn")

    def test_slack_state_between_warn_and_hit(self):
        self.assertEqual(slack_state(95.0, 100.0), "warn")

    def test_slack_state_at_100_pct_is_hit(self):
        self.assertEqual(slack_state(100.0, 100.0), "hit")

    def test_slack_state_above_100_pct_is_hit(self):
        self.assertEqual(slack_state(120.0, 100.0), "hit")


class TestSnapshotSchema(unittest.TestCase):
    """R2.2 — single hard-stop snapshot schema for terminal events."""

    def test_budget_hit_snapshot_fields_stable(self):
        cs = bc.make_default_set(_LIMITS)
        c = cs["tokens_in"]
        c.add(1000.0)
        c.mark_hit("2026-05-08T12:34:56Z")
        s = snap.HardStopSnapshot(
            reason="budget_hit",
            counter_name="tokens_in",
            value=c.value,
            limit=c.limit,
            hit_at_iso=c.hit_at_iso,
            estimated=c.estimated,
            extra={},
            task_slug="v0.8.2-p0-core",
        )
        self.assertEqual(s.reason, "budget_hit")
        self.assertEqual(s.counter_name, "tokens_in")
        self.assertEqual(s.value, 1000.0)
        self.assertEqual(s.limit, 1000.0)
        self.assertEqual(s.hit_at_iso, "2026-05-08T12:34:56Z")
        self.assertEqual(s.schema_version, "v1")

    def test_cost_usd_snapshot_carries_model_and_pricing(self):
        cs = bc.make_default_set(_LIMITS)
        c = cs["cost_usd"]
        c.add(10.0, model_id="claude-opus-4-7", pricing_version="2026-05-01")
        c.mark_hit("2026-05-08T12:34:56Z")
        s = snap.HardStopSnapshot(
            reason="budget_hit",
            counter_name="cost_usd",
            value=c.value,
            limit=c.limit,
            hit_at_iso=c.hit_at_iso,
            estimated=c.estimated,
            extra={
                "model_id": c.model_id,
                "pricing_version": c.pricing_version,
            },
            task_slug="v0.8.2-p0-core",
        )
        self.assertEqual(s.extra["model_id"], "claude-opus-4-7")
        self.assertEqual(s.extra["pricing_version"], "2026-05-01")

    def test_snapshot_terminal_reasons_all_use_same_schema(self):
        # R2.1 (one hit -> single schema), invariant 4 from PRD R2.
        for reason in (
            "budget_hit",
            "retry_cap",
            "afk_timeout",
            "codex_review_cap",
        ):
            s = snap.HardStopSnapshot(
                reason=reason,
                counter_name=None,
                value=None,
                limit=None,
                hit_at_iso="2026-05-08T12:34:56Z",
                estimated=False,
                extra={},
                task_slug="v0.8.2-p0-core",
            )
            self.assertEqual(s.schema_version, "v1")
            self.assertEqual(s.reason, reason)

    def test_snapshot_round_trip_preserves_fields(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "snap.json"
            s = snap.HardStopSnapshot(
                reason="budget_hit",
                counter_name="cost_usd",
                value=10.0,
                limit=10.0,
                hit_at_iso="2026-05-08T12:34:56Z",
                estimated=True,
                extra={"model_id": "claude-opus-4-7", "pricing_version": "v1"},
                task_slug="v0.8.2-p0-core",
            )
            snap.write(s, p)
            loaded = snap.read(p)
            self.assertEqual(loaded, s)
            # Verify on-disk JSON has all stable fields.
            data = json.loads(p.read_text(encoding="utf-8"))
            for field in (
                "reason",
                "counter_name",
                "value",
                "limit",
                "hit_at_iso",
                "estimated",
                "extra",
                "task_slug",
                "schema_version",
            ):
                self.assertIn(field, data)


class TestPersistenceRoundTrip(unittest.TestCase):
    """G-class — disk-state drift; counter dump/load preserves all fields."""

    def test_dump_load_preserves_all_counter_fields(self):
        cs = bc.make_default_set(_LIMITS)
        cs["tokens_in"].add(500.0, estimated=True)
        cs["cost_usd"].add(
            3.0, model_id="claude-opus-4-7", pricing_version="2026-05-01"
        )
        cs["tokens_in"].mark_hit("2026-05-08T01:00:00Z")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "counters.json"
            bc.dump(cs, p)
            loaded = bc.load(p)
        self.assertEqual(loaded["tokens_in"].value, 500.0)
        self.assertTrue(loaded["tokens_in"].estimated)
        self.assertEqual(loaded["tokens_in"].hit_at_iso, "2026-05-08T01:00:00Z")
        self.assertEqual(loaded["cost_usd"].value, 3.0)
        self.assertEqual(loaded["cost_usd"].model_id, "claude-opus-4-7")
        self.assertEqual(loaded["cost_usd"].pricing_version, "2026-05-01")

    def test_dump_uses_atomic_write(self):
        # Atomic write: tmp file should not linger; final path holds JSON.
        cs = bc.make_default_set(_LIMITS)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "counters.json"
            bc.dump(cs, p)
            self.assertTrue(p.exists())
            self.assertFalse((p.parent / (p.name + ".tmp")).exists())


if __name__ == "__main__":
    unittest.main()
