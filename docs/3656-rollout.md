# #3656 rollout (prepared, not performed)

ATS-run review tests: `ats delegate --review-tests` and `ats review-tests`. Nothing
in this branch deploys itself. The W11/W12 train it was prepared beside already
landed and deployed (main 05cd2e2, 2026-11-15 21:54 PT); this branch merges
cleanly onto it and is deployed on its own, in a quiet window, by the release
worker.

## What changes where

| Component | Change | Picks it up |
|---|---|---|
| new `review_runner.py` | checkout at the exact commit, trusted launcher, in-sandbox isolation probe, evidence | the `ats` CLI after the pipx reinstall |
| `cli.py` | `delegate --review-tests/--review-repo/--review-sha/--review-base/--review-timeout`; new `review-tests` command | same |
| `delegation_packet.py` | `review_tests_text` block ahead of the objective | same |
| `~/.config/ai-team-sync/review_runner.toml` | NEW machine-local file (staged copy in the evidence folder) | read on each run; old code never reads it |

No REST route, schema, migration, MCP tool or hook change. `ats-server` and
`ats-mcp` import none of the changed modules, so neither needs a restart for this
branch; `deploy.sh` (the only sanctioned install path, because it stamps the
commit) restarts `ats-server` anyway (~2-3 s: MCP calls fail and the Bash/commit
guards fail closed in governed repos, hence the quiet window). The MCP
`delegate` tool does not pass the new flags yet: its handler is in
`mcp/server.py`, which W11 and W12 both change; a follow-up adds the passthrough
and raises that handler's subprocess timeout by `--review-timeout`.

## Procedure (inside the W11/W12 window)

```bash
cd /home/patrick/code/ai-team-sync
git status --short --untracked-files=no        # must be empty
PREV=$(git rev-parse --short HEAD)             # expect 05cd2e2 or later
curl -s 'http://127.0.0.1:8400/api/restarts/leases?active=true'   # must be []
git merge --no-ff fix/3656-review-runner -m "Merge fix/3656-review-runner (#3656)"
# focused check on the merged tree (CI recipe):
env -u ATS_AGENT -u CLAUDECODE -u CLAUDE_CODE -u CLAUDE_CODE_SESSION_ID -u ATS_WORKERS_CONFIG \
    -u ATS_STATE_DIR -u ATS_REVIEW_RUNNER_CONFIG HOME=$(mktemp -d) PYTHONPATH=$PWD/src \
    nice -n 10 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_review_runner_3656.py \
    tests/test_child_session_lifecycle.py tests/test_task_envelope_reaches_the_child.py \
    tests/test_verify_worktree_containment.py tests/test_collab_s1_verdicts.py
scripts/deploy.sh                      # stamp, pipx reinstall, hooks, ats-server restart
curl -s http://127.0.0.1:8400/api/version   # commit = the merge, dirty false
install -m 600 /opt/tower-evidence/echo_autonomous_production_2026_11_15/w3656_review_runner/review_runner.toml \
    ~/.config/ai-team-sync/review_runner.toml
```

## Post-deploy verification (live, writes nothing outside /var/tmp/ats-review and the log dir)

```bash
ats review-tests --repo /opt/anime-studio --sha main -- tests/test_sandbox_isolation.py -q -p no:cacheprovider
#   expect: "isolation probe   : PASS", "result : PASSED (pytest exit 0)", exit 0
ats review-tests --repo /opt/tower-echo-brain --sha main -- tests/test_conftest_db_guard.py -q -p no:cacheprovider
#   expect: mode hosted, probe PASS, exit 0
git -C /opt/anime-studio worktree list | grep -c ats-review     # 0: throwaway trees removed
ls /var/tmp/ats-review                                          # empty
```

Then the first real review that wants DB evidence passes `--review-tests "<selection>"`
(and `--review-base <base sha>` for a base comparison) to `ats delegate`; the
`delegation_status` evidence carries `review_tests`.

## Launcher instructions (after the deploy, not before)

`~/.claude/agents/adversarial-reviewer.md` launches `ats delegate ...`. Passing a
flag the installed `ats` does not know fails the launch, so edit it only after
`deploy.sh`. Add to the launch command, when the packet's acceptance includes
database-backed tests:

```
  --review-tests "<the focused selection>" [--review-sha <head sha>] [--review-base <base sha>]
  [--review-repo <other repo tree, when the tests live in the other repository>]
```

and in "Execution policy", replace "say who ran what from the packet's recorded
logs" with: "cite the ATS REVIEW TESTS block (run id, commit, probe, exit) when the
packet has one; it was run by ATS on the stated commit, not by the implementer."

## Rollback

`git checkout "$PREV"` (or revert the merge) and run `scripts/deploy.sh`; remove
`~/.config/ai-team-sync/review_runner.toml` (optional: old code never reads it).
Logs under `~/.local/state/ai-team-sync/review-runs/` are inert.
