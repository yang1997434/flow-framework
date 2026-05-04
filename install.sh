#!/usr/bin/env bash
# Flow Framework installer
# Symlinks claude/{commands,skills,hooks} into ~/.claude/, sets up ~/.flow/

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"
FLOW_HOME="${HOME}/.flow"

echo ">> Flow Framework installer"
echo "   Source: ${REPO_ROOT}"
echo "   Claude config: ${CLAUDE_DIR}"
echo "   Flow home: ${FLOW_HOME}"
echo

# 1. Ensure target dirs exist
mkdir -p "${CLAUDE_DIR}/commands" "${CLAUDE_DIR}/skills" "${CLAUDE_DIR}/hooks"
mkdir -p "${FLOW_HOME}"
chmod 700 "${FLOW_HOME}"

# 2. Symlink slash commands
if [ -d "${REPO_ROOT}/claude/commands/flow" ]; then
    if [ -L "${CLAUDE_DIR}/commands/flow" ] || [ -d "${CLAUDE_DIR}/commands/flow" ]; then
        echo "   [skip] ${CLAUDE_DIR}/commands/flow already exists"
    else
        ln -s "${REPO_ROOT}/claude/commands/flow" "${CLAUDE_DIR}/commands/flow"
        echo "   [link] commands/flow → ${REPO_ROOT}/claude/commands/flow"
    fi
fi

# 3. Symlink skills
if [ -d "${REPO_ROOT}/claude/skills/flow" ]; then
    if [ -L "${CLAUDE_DIR}/skills/flow" ] || [ -d "${CLAUDE_DIR}/skills/flow" ]; then
        echo "   [skip] ${CLAUDE_DIR}/skills/flow already exists"
    else
        ln -s "${REPO_ROOT}/claude/skills/flow" "${CLAUDE_DIR}/skills/flow"
        echo "   [link] skills/flow → ${REPO_ROOT}/claude/skills/flow"
    fi
fi

# 4. Hook installation is opt-in (modifies settings.json)
echo
echo ">> Hooks NOT auto-installed (modify settings.json)"
echo "   To install hooks: see ${REPO_ROOT}/claude/hooks/README.md"
echo

# 5. Set up ~/.flow/credentials.local stub
if [ ! -f "${FLOW_HOME}/credentials.local" ]; then
    cp "${REPO_ROOT}/templates/flow.config.local.yaml.template" "${FLOW_HOME}/credentials.local"
    chmod 600 "${FLOW_HOME}/credentials.local"
    echo "   [created] ${FLOW_HOME}/credentials.local (chmod 600)"
fi

# 6. Symlink scripts dir for `flow` command (optional convenience)
LOCAL_BIN="${HOME}/.local/bin"
mkdir -p "${LOCAL_BIN}"
if [ ! -e "${LOCAL_BIN}/flow" ]; then
    cat > "${LOCAL_BIN}/flow" <<EOF
#!/usr/bin/env bash
# Flow Framework CLI dispatcher
exec python3 "${REPO_ROOT}/scripts/flow.py" "\$@"
EOF
    chmod +x "${LOCAL_BIN}/flow"
    echo "   [link] flow CLI → ${LOCAL_BIN}/flow"
fi

echo
echo ">> Install complete."
echo "   Test: cd <some-project> && flow init"
echo "   Or in Claude Code: /flow:start \"<task>\""
echo
echo "   To enable hooks (optional):"
echo "     Add the snippet from ${REPO_ROOT}/claude/hooks/settings.json.snippet"
echo "     into your ~/.claude/settings.json"
