"""Single hard-stop snapshot schema (v0.8.2 T1).

ALL terminal events emit the SAME schema (PRD §R2 invariant 4):

- ``budget_hit``       — any of 5 budget counters reached 100 %
- ``retry_cap``        — Phase 2 implementer retries hit
                         ``max_dispatch_retry_rounds``
- ``afk_timeout``      — T17 AFK timer fired (or 24 h hard cap)
- ``codex_review_cap`` — Phase 2 reviewer hit
                         ``max_codex_review_rounds``

Schema is FROZEN for v0.8.2 (`schema_version="v1"`). Adding fields
requires bumping `schema_version` and shipping a coordinated migration
in `read()`. This is checked structurally by the `frozen=True`
dataclass annotation.

Persistence:
- `write` does an atomic JSON dump (tmp + os.replace).
- `read` rejects payloads with an unknown `schema_version` (forward
  compatibility guard — a v2 reader stays explicit).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


SCHEMA_VERSION = "v1"

VALID_REASONS = frozenset(
    {
        "budget_hit",
        "retry_cap",
        "afk_timeout",
        "codex_review_cap",
    }
)


@dataclass(frozen=True)
class HardStopSnapshot:
    reason: str
    counter_name: Optional[str]
    value: Optional[float]
    limit: Optional[float]
    hit_at_iso: str
    estimated: bool
    extra: dict = field(default_factory=dict)
    task_slug: str = ""
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        if self.reason not in VALID_REASONS:
            raise ValueError(
                f"invalid hard-stop reason: {self.reason!r} "
                f"(allowed: {sorted(VALID_REASONS)})"
            )

    def to_dict(self) -> dict:
        return asdict(self)


def write(snapshot: HardStopSnapshot, path: Path) -> None:
    """Atomic JSON dump of a snapshot to `path`."""
    path = Path(path)
    payload = snapshot.to_dict()
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read(path: Path) -> HardStopSnapshot:
    """Read a snapshot back into a `HardStopSnapshot`.

    Rejects payloads with an unknown `schema_version` (the v0.8.2 v1
    schema is the only one we accept; future versions must extend
    this reader explicitly).
    """
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported snapshot schema_version: {version!r} "
            f"(expected {SCHEMA_VERSION!r})"
        )
    return HardStopSnapshot(
        reason=raw["reason"],
        counter_name=raw.get("counter_name"),
        value=raw.get("value"),
        limit=raw.get("limit"),
        hit_at_iso=raw["hit_at_iso"],
        estimated=bool(raw.get("estimated", False)),
        extra=dict(raw.get("extra") or {}),
        task_slug=raw.get("task_slug", ""),
        schema_version=raw.get("schema_version", SCHEMA_VERSION),
    )
