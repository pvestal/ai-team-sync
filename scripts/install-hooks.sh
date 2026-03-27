#!/usr/bin/env bash
# Install ai-team-sync git hooks into the current repository.
# Usage: ./install-hooks.sh [repo-path]

set -euo pipefail

REPO="${1:-.}"
HOOKS_DIR="${REPO}/.git/hooks"

if [ ! -d "${HOOKS_DIR}" ]; then
    echo "Error: ${REPO} is not a git repository (no .git/hooks directory)"
    exit 1
fi

echo "Installing ai-team-sync hooks into ${REPO}..."

# Pre-commit hook
cat > "${HOOKS_DIR}/pre-commit-ats" << 'EOF'
#!/usr/bin/env bash
python3 -m ai_team_sync.hooks.pre_commit
EOF
chmod +x "${HOOKS_DIR}/pre-commit-ats"

# Post-commit hook
cat > "${HOOKS_DIR}/post-commit-ats" << 'EOF'
#!/usr/bin/env bash
python3 -m ai_team_sync.hooks.post_commit
EOF
chmod +x "${HOOKS_DIR}/post-commit-ats"

# Prepare-commit-msg hook
cat > "${HOOKS_DIR}/prepare-commit-msg-ats" << 'EOF'
#!/usr/bin/env bash
python3 -m ai_team_sync.hooks.prepare_commit_msg "$@"
EOF
chmod +x "${HOOKS_DIR}/prepare-commit-msg-ats"

# Wire into existing hooks (append if they exist, create if not)
for hook in pre-commit post-commit prepare-commit-msg; do
    hook_file="${HOOKS_DIR}/${hook}"
    ats_hook="${HOOKS_DIR}/${hook}-ats"

    if [ -f "${hook_file}" ]; then
        # Check if already installed
        if grep -q "ai-team-sync" "${hook_file}" 2>/dev/null; then
            echo "  ${hook}: already installed, skipping"
            continue
        fi
        echo "  ${hook}: appending to existing hook"
        echo "" >> "${hook_file}"
        echo "# ai-team-sync hook" >> "${hook_file}"
        echo "${ats_hook}" >> "${hook_file}"
    else
        echo "  ${hook}: creating new hook"
        cat > "${hook_file}" << HOOKEOF
#!/usr/bin/env bash
# ai-team-sync hook
${ats_hook}
HOOKEOF
        chmod +x "${hook_file}"
    fi
done

echo ""
echo "Done! ai-team-sync hooks installed."
echo "Make sure the ats server is running: ats-server"
