---
name: agent-sonnet-alias-stale
date: 2026-05-05
project: cross
severity: high
status: active
trigger_paths:
  - "~/.claude/settings.json"
  - "claude/capabilities/defaults.json"
  - "claude/commands/flow/start.md"
last_verified: 2026-05-05
---

# agent-sonnet-alias-stale

## Symptom

Dispatching `Agent(model: "sonnet", ...)` fails with:

```
There's an issue with the selected model
(us.anthropic.claude-sonnet-4-5-20250929-v1:0). It may not exist or you may
not have access to it.
```

Repro: invoke `Agent` tool with `model: "sonnet"` from a session where
`~/.claude/settings.json` `env.ANTHROPIC_DEFAULT_SONNET_MODEL` points at a
Bedrock-format ID, but the runtime is in subscription mode (or vice versa).

## Root cause

Two issues compound:
1. **Format mismatch**: `us.anthropic.claude-sonnet-4-5-20250929-v1:0` is a
   Bedrock model ID. Subscription-mode Claude Code rejects it.
2. **Version drift**: even on Bedrock, the pinned `4-5-20250929` version may
   not be in the user's IAM allow-list while `4-6` is.

Settings.json `env` block overrides shell env at startup, so sourcing
`bedrock-switch.sh` (which sets the right ID) doesn't help — settings.json
wins.

## Fix

Set the env var to the **alias-resolvable subscription-format ID**, choosing
1M-context variant when long-research is the use case:

```json
"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6[1m]"
```

Verify with: `claude --model sonnet --print "ping"`.

## Prevention

Before any Flow task that dispatches sub-agents:
- `grep ANTHROPIC_DEFAULT_SONNET_MODEL ~/.claude/settings.json` — confirm
  format matches active provider (subscription = no `us.` prefix)
- `claude --model sonnet --print ping` to verify alias resolves
- If switching providers (sub ↔ Bedrock), edit settings.json to match —
  shell env alone won't override.

Also: `defaults.json` `model_roles` should hold **aliases** (`sonnet`,
`opus`, `haiku`) not full IDs, so the Agent tool's enum-restricted `model`
param accepts the rendered output.

## Why it matters

Sub-agent dispatch failure during Phase 1 research forced an emergency
downgrade to `haiku` (research-depth-inadequate) and burned ~1h debugging
mid-task. Recurrence cost: each instance ≈ 30-60min lost + risk of running
research with a depth-inadequate model.

## References

- Commit: (pending)
- Related: flow-protocol-needs-fallback-chain.md
