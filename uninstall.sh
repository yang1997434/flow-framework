#!/usr/bin/env bash
# Flow Framework uninstaller — removes symlinks, leaves data alone

set -euo pipefail

CLAUDE_DIR="${HOME}/.claude"
LOCAL_BIN="${HOME}/.local/bin"

echo ">> Flow Framework uninstaller"
echo

remove_link() {
    local path="$1"
    if [ -L "$path" ]; then
        rm "$path"
        echo "   [unlink] $path"
    elif [ -e "$path" ]; then
        echo "   [skip] $path is not a symlink (untouched)"
    fi
}

remove_link "${CLAUDE_DIR}/commands/flow"
remove_link "${CLAUDE_DIR}/skills/flow"
remove_link "${LOCAL_BIN}/flow"

echo
echo ">> Uninstall complete."
echo "   ~/.flow/credentials.local NOT removed (your secrets)"
echo "   ~/.claude/settings.json NOT modified (remove hook entries manually)"
echo "   Project .flow/ directories NOT touched"
