# Flow Hooks

Hook scripts for auto-loading framework state, detecting framework keywords, and credential safety.

## Installation

Hooks are **opt-in** — `install.sh` does not auto-modify your `~/.claude/settings.json`.

To enable:

1. Open `~/.claude/settings.json`
2. **If you don't already have a `hooks` section**, append the contents of `settings.json.snippet`
3. **If you already have hooks**, merge carefully — preserve your existing handlers

Example merge for SessionStart:

```json
"SessionStart": [
  { "matcher": "startup", "hooks": [
    { "type": "command", "command": "your-existing-hook.sh" },
    { "type": "command", "command": "python3 ~/projects/flow-framework/claude/hooks/session-start.py", "timeout": 10 }
  ]}
]
```

## Hook list

| File | Trigger | Purpose | Timeout |
|------|---------|---------|---------|
| `session-start.py` | startup / clear / compact | Inject Quick Read Guide + active task + relevant pitfalls | 10s |
| `user-prompt-submit.py` | each user message | Detect Flow trigger keywords + active phase breadcrumb | 5s |
| `post-tool-bash.py` | after Bash tool | Credential grep on git commit | 15s |
| `stop.py` | session end | Auto-save current task journal entry | 15s |

## Path adjustment

If you cloned the repo somewhere other than `~/projects/flow-framework`, edit the snippet paths.

## Disabling

To disable hooks: remove the corresponding entries from `settings.json`. Or use `~/.claude/settings.local.json` to override (set the same handlers to empty).

## Output format

All hooks output JSON to stdout in this shape:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "<EventName>",
    "additionalContext": "<text injected into the model's context>"
  }
}
```

Hooks that don't need to inject anything `sys.exit(0)` with no output.

## Failure modes

- All hooks are **best-effort**: any exception → silent exit, never blocks the session
- Timeouts kill the hook process, no error to the user
- If `~/projects/flow-framework/` moves, hooks fail silently — re-run `install.sh` to fix
