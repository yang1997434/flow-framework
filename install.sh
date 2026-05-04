#!/usr/bin/env bash
# Flow Framework installer
#
# Declarative install driven by dependencies.json:
#   1. System command checks
#   2. Marketplace registration  (`claude plugin marketplace add`)
#   3. Plugin install            (`claude plugin install plugin@marketplace`)
#   4. Hook install              (merge into ~/.claude/settings.json)
#   5. Symlinks for ~/.claude/{commands,skills}/flow
#   6. ~/.local/bin/flow CLI shim
#
# Flags:
#   --dry-run   Show actions without executing
#   --skip-plugins / --skip-hooks   Skip those phases (debugging)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${HOME}/.claude"
FLOW_HOME="${HOME}/.flow"
LOCAL_BIN="${HOME}/.local/bin"

DRY_RUN=""
SKIP_PLUGINS=""
SKIP_HOOKS=""
for arg in "$@"; do
    case "${arg}" in
        --dry-run)      DRY_RUN="--dry-run" ;;
        --skip-plugins) SKIP_PLUGINS=1 ;;
        --skip-hooks)   SKIP_HOOKS=1 ;;
        -h|--help)
            sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown flag: ${arg}" >&2; exit 1 ;;
    esac
done

echo ">> Flow Framework install"
echo "   source: ${REPO_ROOT}"
echo "   target: ${CLAUDE_DIR}"
echo

# --- Pre-check: python3 must exist (otherwise we can't run the orchestrator) ---
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. Install Python 3.11+ first." >&2
    exit 1
fi

# --- 1. System command checks (delegates to flow_install.py) ---
python3 "${REPO_ROOT}/scripts/flow_install.py" check-system ${DRY_RUN}
echo

# --- 2 + 3. Marketplaces + Plugins ---
if [ -z "${SKIP_PLUGINS}" ]; then
    python3 "${REPO_ROOT}/scripts/flow_install.py" register-marketplaces ${DRY_RUN}
    echo
    python3 "${REPO_ROOT}/scripts/flow_install.py" install-plugins ${DRY_RUN}
    echo
else
    echo ">> Skipping plugin install (--skip-plugins)"
    echo
fi

# --- 4. Hook install ---
if [ -z "${SKIP_HOOKS}" ]; then
    python3 "${REPO_ROOT}/scripts/flow_install.py" install-hooks ${DRY_RUN}
    echo
else
    echo ">> Skipping hook install (--skip-hooks)"
    echo
fi

# --- 5a. Render prompt templates → ~/.claude/{commands,skills}/flow ---
# (replaces the old symlink approach — skills/commands are templates now,
#  rendered at install time using flow.config.yaml capability mapping)
if [ -n "${DRY_RUN}" ]; then
    echo ">> Render prompt templates"
    echo "   [dry-run] would render commands/flow and skills/flow"
else
    # If the install target was previously a symlink, unlink it before render writes files
    for kind in commands skills; do
        dst="${CLAUDE_DIR}/${kind}/flow"
        if [ -L "${dst}" ]; then
            rm "${dst}"
            echo "   [unlink] removed legacy symlink ${dst} (will be replaced by rendered files)"
        fi
    done
    python3 "${REPO_ROOT}/scripts/flow_install.py" render-prompts ${DRY_RUN}
fi
echo

# --- 5b. ~/.flow/ + credentials.local stub ---
if [ -z "${DRY_RUN}" ]; then
    mkdir -p "${FLOW_HOME}"
    chmod 700 "${FLOW_HOME}"
    if [ ! -f "${FLOW_HOME}/credentials.local" ] && [ -f "${REPO_ROOT}/templates/flow.config.local.yaml.template" ]; then
        cp "${REPO_ROOT}/templates/flow.config.local.yaml.template" "${FLOW_HOME}/credentials.local"
        chmod 600 "${FLOW_HOME}/credentials.local"
        echo ">> Created ${FLOW_HOME}/credentials.local (chmod 600)"
    fi
fi
echo

# --- 6. ~/.local/bin/flow CLI shim ---
echo ">> CLI shim"
if [ -n "${DRY_RUN}" ]; then
    echo "   [dry-run] would create ${LOCAL_BIN}/flow"
else
    mkdir -p "${LOCAL_BIN}"
    if [ ! -e "${LOCAL_BIN}/flow" ]; then
        cat > "${LOCAL_BIN}/flow" <<EOF
#!/usr/bin/env bash
# Flow Framework CLI dispatcher
exec python3 "${REPO_ROOT}/scripts/flow.py" "\$@"
EOF
        chmod +x "${LOCAL_BIN}/flow"
        echo "   [link] flow CLI → ${LOCAL_BIN}/flow"
    else
        echo "   [skip] ${LOCAL_BIN}/flow already exists"
    fi
fi
echo

# --- 7. Functional self-test (proves the install actually works) ---
if [ -z "${DRY_RUN}" ]; then
    echo ">> Running selftest (functional verification)"
    if python3 "${REPO_ROOT}/scripts/flow_selftest.py"; then
        echo
        echo ">> Install complete and verified."
    else
        echo
        echo "ERROR: selftest detected functional failures." >&2
        echo "       Run 'flow doctor' for static state, 'flow selftest' to retry." >&2
        exit 2
    fi
else
    echo "   [dry-run] would run flow_selftest.py"
    echo
    echo ">> Install dry-run complete."
fi

echo "   Use: cd <some-project> && flow init && /flow:start \"<task>\""
