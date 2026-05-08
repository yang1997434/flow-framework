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
  2a. ``~/.claude/settings.json::env::ANTHROPIC_DEFAULT_<BASE>_MODEL``
      (BASE inferred from detected model: opus / sonnet / haiku):
        - alias ends ``[1m]`` -> 1_000_000 (upgrade)
        - alias is a string without ``[1m]`` -> the user has explicitly
          selected the 200k base; short-circuit to ``MODEL_LIMITS`` (i.e.
          skip 2b entirely). This respects an explicit non-1M choice.
        - alias is absent (None) or non-string -> fall through to 2b.
  2b. *Plan-level heuristic*: any ``ANTHROPIC_DEFAULT_*_MODEL`` entry in
      settings.json ending ``[1m]`` -> 1_000_000 for the current model.
      1M context is a plan/pricing-level Anthropic add-on (not per-
      model), so any [1m] alias is a strong signal the user's plan
      grants 1M to all models — even ones the user hasn't aliased.
      **Only applies when the matching base alias is absent, not merely
      when it exists without a ``[1m]`` suffix** (round-2 [P2] guard).
  3. ``MODEL_LIMITS`` table lookup
  4. ``DEFAULT_LIMIT`` (200_000)

Rung 2 exists because Claude Code transcripts record the bare model id
(e.g. ``claude-opus-4-7``) regardless of whether the active session is
the 200k or 1M context variant. The only external signal of "this
session is 1M-mode" is the env-var alias in settings.json. 2a covers
the case where the user explicitly aliased the matching base; 2b
covers the common case where the user only aliased *one* base (e.g.
sonnet) and runs other models (e.g. opus) in the same 1M-enabled plan.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional, Tuple

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

    # Rung 2a: settings.json env-alias on the matching base.
    #
    # Three sub-cases on the matching base alias:
    #   (i)   alias is a string ending ``[1m]``         -> 1M (upgrade)
    #   (ii)  alias is a string but does NOT end ``[1m]``
    #         -> user has *explicitly* selected the 200k base; respect that
    #            choice and short-circuit straight to the table value.
    #            DO NOT fall through to rung 2b — round-2 [P2] guard.
    #   (iii) alias is absent entirely (None) — i.e. the user has not
    #         aliased this base at all -> fall through to rung 2b plan-
    #         level heuristic, which can still infer 1M from a sibling
    #         base alias.
    # Non-string alias values (lists, ints, etc.) are treated like (iii):
    # we cannot trust them to express user intent in either direction, so
    # we let rung 2b decide.
    if model:
        env_key = _env_key_for_model(model)
        if env_key is not None:
            alias = _read_settings_env_var(env_key)
            # L-class type guard: only string aliases carry user intent.
            if isinstance(alias, str):
                if alias.endswith("[1m]"):
                    # Case (i): only upgrade — never downgrade. If the
                    # table somehow lists a higher limit for this exact
                    # id, prefer that.
                    table_limit = MODEL_LIMITS.get(model, 0)
                    return max(ONE_MILLION_LIMIT, table_limit)
                # Case (ii): explicit non-1M choice. Respect it and skip
                # rung 2b plan-level scan entirely. Codex round-2 [P2]:
                # without this short-circuit, an unrelated sibling alias
                # (e.g. ``ANTHROPIC_DEFAULT_SONNET_MODEL=...[1m]``) would
                # silently upgrade this model from 200k -> 1M and
                # contradict the user's explicit selection.
                return MODEL_LIMITS.get(model, DEFAULT_LIMIT)
            # else (non-string / None): fall through to rung 2b.

    # Rung 2b: plan-level heuristic. 1M context is an Anthropic plan-level
    # add-on, not a per-model setting. If the user has aliased *any* base
    # model with a [1m] suffix, infer the plan covers 1M for all models —
    # so the current model (which 2a missed because the matching base
    # alias is absent) is also 1M-enabled.
    #
    # Round-2 [P2] guard: this rung only applies when the matching alias
    # is *absent*, not merely when it exists without a ``[1m]`` suffix.
    # The case (ii) short-circuit above ensures we only reach here when
    # case (iii) — alias absent — holds.
    if model and _any_settings_alias_signals_1m():
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


def _any_settings_alias_signals_1m() -> bool:
    """Plan-level heuristic: scan settings.json env block for any
    ``ANTHROPIC_DEFAULT_*_MODEL`` value ending ``[1m]``.

    1M context is an Anthropic plan-level paid add-on. A single ``[1m]``
    alias (on any base) is a strong signal the plan covers 1M for all
    models in the session — even bases the user hasn't aliased.

    Returns True if any matching alias exists, False on every
    defensive path (file missing, JSON parse error, env block absent
    or malformed, no matching keys, non-string values, etc.). Never
    raises.
    """
    env_block = _read_settings_env_block()
    if env_block is None:
        return False
    # Iterate keys defensively. Match the same prefix/suffix pattern the
    # Claude Code env loader recognises: ``ANTHROPIC_DEFAULT_<BASE>_MODEL``.
    for key, value in env_block.items():
        if not isinstance(key, str):
            continue
        if not (key.startswith("ANTHROPIC_DEFAULT_")
                and key.endswith("_MODEL")):
            continue
        # L-class type guard: skip non-string values silently.
        if isinstance(value, str) and value.endswith("[1m]"):
            return True
    return False


def _read_settings_env_block():
    """Best-effort read of `~/.claude/settings.json::env` as a dict.

    Returns the env dict or None on any failure (file missing, JSON
    parse error, top-level not dict, env not dict). Never raises.
    """
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
    except (RuntimeError, OSError):
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
    return env_block


def _read_settings_env_var(env_key: str):
    """Best-effort read of `~/.claude/settings.json::env::<env_key>`.

    Returns the raw value (any JSON type) or None on any failure path:
    file missing, JSON parse error, missing `env` block, env not a dict,
    key absent. Never raises. Never logs (this runs in hot estimation
    path).
    """
    env_block = _read_settings_env_block()
    if env_block is None:
        return None
    return env_block.get(env_key)


# ---------------------------------------------------------------------------
# v0.8.2 T1 — budget slack helper.
#
# Pure stateless function used by `budget_counter` (and any caller that
# wants the same 90 / 100 trip-wire policy) to classify a usage value
# against a limit. Lives here so the estimator's ±20 % coarse warning
# (see module docstring) and the slack policy live in the same file —
# changing one without the other should fail review.
# ---------------------------------------------------------------------------
def slack_state(used: float, limit: float) -> Literal["ok", "warn", "hit"]:
    """Classify `used` vs `limit` into an `ok`/`warn`/`hit` band.

    - ``hit`` when ``used >= 1.0 * limit``
    - ``warn`` when ``used >= 0.9 * limit`` (but not yet hit)
    - ``ok`` otherwise

    A non-positive `limit` is treated as an unset/unbounded limit and
    always returns ``"ok"`` — callers wanting a stricter policy must
    validate before calling.
    """
    if limit <= 0:
        return "ok"
    if used >= 1.0 * limit:
        return "hit"
    if used >= 0.9 * limit:
        return "warn"
    return "ok"
