---
name: context-mode-mcp-flake
date: 2026-05-05
project: cross
severity: medium
status: active
trigger_paths:
  - "~/.claude/plugins/marketplaces/context-mode/.mcp.json"
  - "~/.claude/hooks/context-mode-cache-heal.mjs"
last_verified: 2026-05-05
---

# context-mode-mcp-flake

## Symptom

Mid-session, the system injects:

```
The following deferred tools are no longer available (their MCP server
disconnected). Do not search for them — ToolSearch will return no match:
mcp__plugin_context-mode_context-mode__ctx_batch_execute
mcp__plugin_context-mode_context-mode__ctx_search
... (11 tools)
```

Subsequent operations that relied on `ctx_batch_execute` / `ctx_execute_file`
/ `ctx_search` must fall back to native `Bash` / `Read` / `Edit` (paying a
context-window tax). On `/clear` or new session resume, the tools usually
return — confirming it's a transient transport disconnect.

## Root cause

The context-mode plugin process **stays alive** (verified via
`pgrep -af context-mode/start.mjs`), but Claude Code's MCP client SDK loses
the stdio transport connection. Root cause is **upstream in claude-code**,
not in flow or in the plugin itself. No CLI command exists to force
reconnect a single MCP server mid-session.

`SessionStart` runs `~/.claude/hooks/context-mode-cache-heal.mjs` which
fixes symlink hygiene but does NOT re-establish a dropped transport.

## Fix

Mid-session: switch to native tools (`Bash` for git/mkdir/rm; `Read` for
files you'll edit; `Edit`/`Write` for changes). Accept the context tax —
it's bounded and recoverable.

End-of-session: `/clear` or session resume re-spawns the MCP transport.

For persistent failure (server actually died): `pgrep -af context-mode` —
if no process, run `npm start` from the plugin dir or trigger
`SessionStart` (e.g. `/clear`) to let cache-heal respawn.

## Prevention

- Don't design Flow workflows that **strictly require** context-mode tools.
  All flow scripts should work with native Bash/Read/Edit as fallback.
- For long sessions, watch for the disconnect message and adapt rather than
  assuming the tools remain available.
- Document this as expected transient behavior so users don't waste cycles
  diagnosing a "broken plugin" that's actually a transport flake.

## Why it matters

Disconnect costs ~2-5x more context-window per Bash/Read call until
session ends. Multiplied across long Phase 1 research sessions (4+ parallel
agents reading research files), can blow context budget. Single-session
cost: 2-5x token waste on file reads. Cross-session cost: zero (auto-heals).

## References

- Commit: (pending)
- Related: claude-code upstream MCP transport reconnect (no public issue
  link known yet)
