"""progress_meta — read autonomy-related frontmatter pointers from progress.md.

v0.8.0 introduces four pointer fields in progress.md frontmatter:
  contract_path: <relative path to contract.json>
  contract_schema_version: <int>
  autonomy_mode: auto | interactive
  last_checkpoint: <iso8601 ts>

The contract itself is NOT in frontmatter (too brittle for nested schemas).
This module's only job: read the small pointer payload + fail closed on
invalid values for known fields. Unknown fields ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


VALID_AUTONOMY_MODES = ("auto", "interactive")
DEFAULT_AUTONOMY_MODE = "interactive"


@dataclass
class ProgressMeta:
    autonomy_mode: str = DEFAULT_AUTONOMY_MODE
    contract_path: Optional[str] = None
    contract_schema_version: Optional[int] = None
    last_checkpoint: Optional[str] = None


def read_progress_meta(path: Path) -> ProgressMeta:
    """Read autonomy pointer fields from progress.md frontmatter.

    Missing file or missing frontmatter → default ProgressMeta.
    Invalid value for a known field → fail-closed to default for THAT field.
    """
    if not path.is_file():
        return ProgressMeta()
    text = path.read_text(encoding="utf-8")
    fm = _extract_frontmatter(text)
    if not fm:
        return ProgressMeta()

    meta = ProgressMeta()
    am = fm.get("autonomy_mode")
    if am in VALID_AUTONOMY_MODES:
        meta.autonomy_mode = am
    # else fall through to default (fail-closed)

    cp = fm.get("contract_path")
    if isinstance(cp, str) and cp:
        meta.contract_path = cp

    csv = fm.get("contract_schema_version")
    if isinstance(csv, int) and csv >= 1:
        meta.contract_schema_version = csv

    lc = fm.get("last_checkpoint")
    if isinstance(lc, str) and lc:
        meta.last_checkpoint = lc

    return meta


_FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _extract_frontmatter(text: str) -> dict:
    """Minimal YAML-ish frontmatter parser for `key: value` lines only.

    We deliberately avoid pulling in pyyaml — Flow's frontmatter is always
    flat key:value (per project convention). Lines that aren't `key: value`
    are silently skipped.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        # int coercion for known int fields
        if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
            try:
                out[k] = int(v)
                continue
            except ValueError:
                pass
        out[k] = v
    return out
