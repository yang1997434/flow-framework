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

Limit resolution priority chain (see `_resolve_limit`):
  1. ``FLOW_CONTEXT_LIMIT`` env var (explicit override, positive int tokens)
  2. ``~/.claude/settings.json::env::ANTHROPIC_DEFAULT_<BASE>_MODEL`` ending
     ``[1m]`` (BASE inferred from detected model: opus / sonnet / haiku)
     -> 1_000_000
  3. ``MODEL_LIMITS`` table lookup
  4. ``DEFAULT_LIMIT`` (200_000)

Rung 2 exists because Claude Code transcripts record the bare model id
(e.g. ``claude-opus-4-7``) regardless of whether the active session is
the 200k or 1M context variant. The only external signal of "this
session is 1M-mode" is the env-var alias in settings.json.
"""
from __future__ import annotations

import json
import os
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
ONE_MILLION_LIMIT = 1_000_000

# How many JSONL lines from the head of the file to scan for a model field.
MODEL_DETECT_HEAD_LINES = 20

# Map detected model id prefix -> settings.json env var that aliases it.
# Order matters only for documentation; iteration is not used in lookup.
_MODEL_BASE_ENV_KEYS: dict[str, str] = {
    "claude-opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "claude-sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "claude-haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}


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
    limit = _resolve_limit(model)
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


def _resolve_limit(model: Optional[str]) -> int:
    """Resolve the context-window limit for the detected model id.

    Implements the 4-rung priority chain documented in this module's
    docstring. Designed to fail-soft on every defensive path: any
    unparseable env var, missing/malformed settings.json, or off-spec
    alias value falls through to the next rung. The MODEL_LIMITS table
    + DEFAULT_LIMIT are the always-available terminal fallback.

    Caller-visible invariant: returns a positive int. Never raises.
    """
    # Rung 1: FLOW_CONTEXT_LIMIT explicit override.
    raw = os.environ.get("FLOW_CONTEXT_LIMIT")
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped:
            try:
                value = int(stripped)
            except (ValueError, TypeError):
                value = 0
            if value > 0:
                return value

    # Rung 2: settings.json env-alias [1m] suffix on the matching base.
    if model:
        env_key = _env_key_for_model(model)
        if env_key is not None:
            alias = _read_settings_env_var(env_key)
            # Type guard (L-class): only act on string values that we can
            # safely call .endswith() on.
            if isinstance(alias, str) and alias.endswith("[1m]"):
                # Only upgrade — never downgrade. If the table somehow lists
                # a higher limit for this exact id, prefer that.
                table_limit = MODEL_LIMITS.get(model, 0)
                return max(ONE_MILLION_LIMIT, table_limit)

    # Rung 3 + 4: existing table lookup with default fallback.
    if model:
        return MODEL_LIMITS.get(model, DEFAULT_LIMIT)
    return DEFAULT_LIMIT


def _env_key_for_model(model: str) -> Optional[str]:
    """Map detected model id -> settings.json env var key (or None)."""
    for prefix, env_key in _MODEL_BASE_ENV_KEYS.items():
        if model.startswith(prefix):
            return env_key
    return None


def _read_settings_env_var(env_key: str):
    """Best-effort read of `~/.claude/settings.json::env::<env_key>`.

    Returns the raw value (any JSON type) or None on any failure path:
    file missing, JSON parse error, missing `env` block, env not a dict,
    key absent. Never raises. Never logs (this runs in hot estimation
    path).
    """
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
    except (RuntimeError, OSError):
        # Path.home() can raise RuntimeError when HOME is unset.
        return None

    try:
        text = settings_path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None
    env_block = data.get("env")
    if not isinstance(env_block, dict):
        return None
    return env_block.get(env_key)
