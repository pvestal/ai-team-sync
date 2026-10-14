#!/usr/bin/env bash
# Install ai-team-sync git hooks into a repository.
# Usage: ./install-hooks.sh [repo-path]
#   ATS_HOOKS   which hooks to install (default: "pre-commit pre-merge-commit post-commit prepare-commit-msg").
#               pre-commit / pre-merge-commit can REFUSE a commit or merge that touches
#               another session's lock (docs/commit-guard.md); "post-commit" alone only
#               records commits.
#   ATS_PYTHON  interpreter the installed hooks run (default: the one beside `ats`).
#   ATS_INSTALLER_PYTHON  interpreter that runs the installer itself (default: the first
#               of ATS_PYTHON, the one beside `ats`, python3 that can import it).
#
# The work is done by ai_team_sync.git_hook_install (also `ats hooks install`): hooks
# go to the COMMON git dir so every worktree is covered, an existing guard hook is
# preserved as <hook>.ats-chained and run first, an existing post hook is only
# appended to, and re-running is a no-op.

set -euo pipefail

REPO="${1:-.}"
HOOKS="${ATS_HOOKS:-pre-commit pre-merge-commit post-commit prepare-commit-msg}"

if ! git -C "${REPO}" rev-parse --git-dir >/dev/null 2>&1; then
    echo "Error: ${REPO} is not a git repository"
    exit 1
fi

# A pipx install keeps ai_team_sync out of the system python3, where a hook
# running `python3 -m ai_team_sync...` fails on import, silently, every commit.
ATS_BIN="$(command -v ats || true)"
BESIDE_ATS=""
if [ -n "${ATS_BIN}" ]; then
    BESIDE_ATS="$(dirname "$(readlink -f "${ATS_BIN}")")/python"
fi
if [ -z "${ATS_PYTHON:-}" ]; then
    ATS_PYTHON="${BESIDE_ATS:-python3}"
    if ! "${ATS_PYTHON}" -c "import ai_team_sync" 2>/dev/null; then
        echo "Error: ${ATS_PYTHON} cannot import ai_team_sync; set ATS_PYTHON"
        exit 1
    fi
fi

INSTALLER=""
for candidate in "${ATS_INSTALLER_PYTHON:-}" "${ATS_PYTHON}" "${BESIDE_ATS}" python3; do
    [ -n "${candidate}" ] || continue
    if "${candidate}" -c "import ai_team_sync.git_hook_install" 2>/dev/null; then
        INSTALLER="${candidate}"
        break
    fi
done
if [ -z "${INSTALLER}" ]; then
    echo "Error: no interpreter can import ai_team_sync.git_hook_install; set ATS_INSTALLER_PYTHON"
    exit 1
fi

ARGS=()
for hook in ${HOOKS}; do
    ARGS+=(--hook "${hook}")
done

echo "Installing ai-team-sync hooks (${HOOKS}) using ${ATS_PYTHON}..."
"${INSTALLER}" -m ai_team_sync.git_hook_install "${REPO}" --python "${ATS_PYTHON}" "${ARGS[@]}"

echo ""
echo "Done! ai-team-sync hooks installed."
echo "Make sure the ats server is running: ats-server"
