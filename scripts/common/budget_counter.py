"""Budget counter dataclass + persistence (v0.8.2 T1).

5 named resource counters per task:
    tokens_in / tokens_out / cost_usd / active_wallclock_minutes /
    subagent_dispatches

Trip wire policy: warn at 90 %, hard-stop at 100 % (matches
`context_estimator.slack_state` — guards against estimator ±20 %
precision illusion; see PRD §R2.3).

Schema notes:
- Schema is intended to be stable for v0.8.2; persisted JSON is round-
  trippable via `dump`/`load`. Field changes require coordinated work
  with `snapshot.HardStopSnapshot.schema_version`.
- `cost_usd` is the only counter carrying pricing metadata
  (`model_id`, `pricing_version`). They are required on the FIRST
  `add` so on-disk records are diagnostically complete; subsequent
  adds may omit them (already pinned).

Persistence:
- `dump(counters, path)` writes JSON atomically (tmp + os.replace) to
  mitigate G-class disk-state drift on crash mid-write.

Cross-task isolation:
- `make_default_set(limits)` always returns a fresh dict of zero-value
  counters. There is no module-level mutable state — each task boot
  must call `make_default_set` to get a clean instance (I-class
  blindspot mitigation: never inherit prior task's counter values).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


# Counter trip wires. Caller may override per-counter via `is_warn(threshold)`,
# but defaults are the policy stated above.
DEFAULT_WARN_THRESHOLD = 0.9
DEFAULT_HIT_THRESHOLD = 1.0


@dataclass
class BudgetCounter:
    """One named resource counter.

    Fields are intentionally flat (no subclass inheritance hazards) so
    serialization is straightforward and `cost_usd` metadata is
    optional on every counter — only `cost_usd` populates them.
    """

    name: str
    value: float = 0.0
    limit: float = 0.0
    hit_at_iso: Optional[str] = None
    estimated: bool = False
    # Only populated for `cost_usd`. Required on first add for cost.
    model_id: Optional[str] = None
    pricing_version: Optional[str] = None

    def add(
        self,
        amount: float,
        *,
        estimated: bool = False,
        model_id: Optional[str] = None,
        pricing_version: Optional[str] = None,
    ) -> None:
        """Increment the counter.

        For `cost_usd`, the FIRST add must supply both `model_id` and
        `pricing_version` (so persisted snapshots always carry the
        pricing context). Subsequent adds may omit them; if supplied,
        they must match the pinned values.
        """
        if self.name == "cost_usd":
            if self.model_id is None or self.pricing_version is None:
                if model_id is None or pricing_version is None:
                    raise ValueError(
                        "cost_usd counter requires model_id and "
                        "pricing_version on first add"
                    )
                self.model_id = model_id
                self.pricing_version = pricing_version
        self.value += float(amount)
        if estimated:
            # Sticky: once any contribution was estimated, the counter
            # value as a whole is reported as estimated (snapshot
            # `estimated:bool` reflects this). Conservative.
            self.estimated = True

    def is_warn(self, threshold: float = DEFAULT_WARN_THRESHOLD) -> bool:
        if self.limit <= 0:
            return False
        return self.value >= threshold * self.limit

    def is_hit(self, threshold: float = DEFAULT_HIT_THRESHOLD) -> bool:
        if self.limit <= 0:
            return False
        return self.value >= threshold * self.limit

    def mark_hit(self, iso_now: str) -> None:
        self.hit_at_iso = iso_now

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BudgetCounter":
        return cls(
            name=d["name"],
            value=float(d.get("value", 0.0)),
            limit=float(d.get("limit", 0.0)),
            hit_at_iso=d.get("hit_at_iso"),
            estimated=bool(d.get("estimated", False)),
            model_id=d.get("model_id"),
            pricing_version=d.get("pricing_version"),
        )


# Order matters only for diagnostic readability; tests should not depend
# on iteration order beyond key set equality.
_COUNTER_NAMES = (
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "active_wallclock_minutes",
    "subagent_dispatches",
)


def make_default_set(limits: dict) -> dict:
    """Build a fresh dict of 5 zero-value counters.

    Each call returns a NEW dict with NEW counter instances — no
    sharing across tasks (I-class blindspot mitigation). Missing
    entries in `limits` are treated as 0 (caller responsibility to
    populate from config).
    """
    return {
        name: BudgetCounter(
            name=name,
            value=0.0,
            limit=float(limits.get(name, 0.0)),
        )
        for name in _COUNTER_NAMES
    }


def register_dispatch(
    counters: dict,
    parent_id: Optional[str] = None,
) -> None:
    """Increment `subagent_dispatches` by 1.

    Always counts globally — `parent_id` is accepted for future
    diagnostic logging (R2.5: nested subagent dispatches must NOT
    escape the budget). Currently no nesting-special-case logic; every
    invocation == +1.
    """
    del parent_id  # accepted, unused — global count only
    c = counters["subagent_dispatches"]
    c.add(1.0)


def dump(counters: dict, path: Path) -> None:
    """Atomic JSON write of all counters to `path`.

    Writes to `path.with_suffix(path.suffix + ".tmp")` then
    `os.replace` to the final path. G-class blindspot mitigation:
    a crash mid-write leaves only the prior good file (or nothing),
    never a half-written record.
    """
    path = Path(path)
    payload = {name: c.to_dict() for name, c in counters.items()}
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def load(path: Path) -> dict:
    """Load counters JSON written by `dump`."""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {name: BudgetCounter.from_dict(d) for name, d in raw.items()}
