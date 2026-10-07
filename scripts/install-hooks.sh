#!/usr/bin/env bash
# Install ai-team-sync git hooks into a repository.
# Usage: ./install-hooks.sh [repo-path]
#   ATS_HOOKS   which hooks to install (default: "pre-commit post-commit prepare-commit-msg").
#               pre-commit can REFUSE a commit that overlaps another session's lock;
#               "post-commit" alone only records commits.
#   ATS_PYTHON  interpreter that can import ai_team_sync (default: the one beside `ats`).

set -euo pipefail

REPO="${1:-.}"
HOOKS="${ATS_HOOKS:-pre-commit post-commit prepare-commit-msg}"

if ! HOOKS_DIR="$(git -C "${REPO}" rev-parse --path-format=absolute --git-path hooks 2>/dev/null)"; then
    echo "Error: ${REPO} is not a git repository"
    exit 1
fi
mkdir -p "${HOOKS_DIR}"

# A pipx install keeps ai_team_sync out of the system python3, where a hook
# running `python3 -m ai_team_sync...` fails on import, silently, every commit.
if [ -z "${ATS_PYTHON:-}" ]; then
    ATS_BIN="$(command -v ats || true)"
    if [ -n "${ATS_BIN}" ]; then
        ATS_PYTHON="$(dirname "$(readlink -f "${ATS_BIN}")")/python"
    else
        ATS_PYTHON="python3"
    fi
    if ! "${ATS_PYTHON}" -c "import ai_team_sync" 2>/dev/null; then
        echo "Error: ${ATS_PYTHON} cannot import ai_team_sync; set ATS_PYTHON"
        exit 1
    fi
fi

echo "Installing ai-team-sync hooks (${HOOKS}) into ${HOOKS_DIR} using ${ATS_PYTHON}..."

for hook in ${HOOKS}; do
    case "${hook}" in
        pre-commit)         cmd="${ATS_PYTHON} -m ai_team_sync.hooks.pre_commit" ;;
        post-commit)        cmd="${ATS_PYTHON} -m ai_team_sync.hooks.post_commit" ;;
        prepare-commit-msg) cmd="${ATS_PYTHON} -m ai_team_sync.hooks.prepare_commit_msg \"\$@\"" ;;
        *) echo "Error: unknown hook ${hook}"; exit 1 ;;
    esac

    ats_hook="${HOOKS_DIR}/${hook}-ats"
    printf '#!/usr/bin/env bash\n%s\n' "${cmd}" > "${ats_hook}"
    chmod +x "${ats_hook}"

    hook_file="${HOOKS_DIR}/${hook}"
    if [ -f "${hook_file}" ]; then
        if grep -q "ai-team-sync" "${hook_file}" 2>/dev/null; then
            echo "  ${hook}: already installed, skipping"
            continue
        fi
        echo "  ${hook}: appending to existing hook"
        printf '\n# ai-team-sync hook\n%s "$@"\n' "${ats_hook}" >> "${hook_file}"
    else
        echo "  ${hook}: creating new hook"
        printf '#!/usr/bin/env bash\n# ai-team-sync hook\n%s "$@"\n' "${ats_hook}" > "${hook_file}"
        chmod +x "${hook_file}"
    fi
done

echo ""
echo "Done! ai-team-sync hooks installed."
echo "Make sure the ats server is running: ats-server"
