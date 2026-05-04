"""Coarse context % estimator from a Claude Code transcript_path.

Hook input includes `transcript_path` — the JSONL file backing the active
conversation. We approximate token count as `file_size_bytes / 4` and
divide by the model's context limit. Confidence levels reflect ambiguity:

  high   — file >= 10 KB AND model identified
  medium — file readable AND model identified, but small
  low    — file unreadable OR model unknown OR estimator unsure

Caller MUST treat (None, 'low') as 'skip this trigger' (do not false-fire).
This is a coarse trigger for nudges, NOT a safety boundary — actual context
fill may diverge by ±20% due to JSON metadata, tool payload escaping, etc.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Tuple

# Model context window sizes (tokens). Update as new models ship.
MODEL_LIMITS: dict[str, int] = {
    "claude-opus-4-7": 200_000,
    "claude-opus-4-7[1m]": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}
DEFAULT_LIMIT = 200_000

# How many JSONL lines from the head of the file to scan for a model field.
MODEL_DETECT_HEAD_LINES = 20


def estimate_context_pct(transcript_path) -> Tuple[Optional[int], str]:
    """Return (pct, confidence). pct in [0, 100] or None on hard failure."""
    p = Path(transcript_path)
    if not p.is_file():
        return (None, "low")

    try:
        size_bytes = p.stat().st_size
    except OSError:
        return (None, "low")

    model = _detect_model(p)
    limit = MODEL_LIMITS.get(model, DEFAULT_LIMIT) if model else DEFAULT_LIMIT
    estimated_tokens = size_bytes / 4
    pct = min(100, max(0, round(estimated_tokens / limit * 100)))

    if model is None:
        confidence = "low"
    elif size_bytes >= 10_000:
        confidence = "high"
    else:
        confidence = "medium"

    return (pct, confidence)


def _detect_model(path: Path) -> Optional[str]:
    """Scan the head of the JSONL for a 'model' field. None if not found."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= MODEL_DETECT_HEAD_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = _extract_model_field(obj)
                if m:
                    return m
    except OSError:
        return None
    return None


def _extract_model_field(obj) -> Optional[str]:
    """Recursively look for a 'model' field in an object."""
    if isinstance(obj, dict):
        if "model" in obj and isinstance(obj["model"], str):
            return obj["model"]
        for v in obj.values():
            r = _extract_model_field(v)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _extract_model_field(item)
            if r:
                return r
    return None
