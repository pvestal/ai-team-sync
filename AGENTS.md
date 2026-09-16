# AGENTS.md

Guidance for AI coding agents (and humans) contributing to **ai-team-sync**.

## What this project is
A coordination layer that lets multiple AI coding agents — Claude Code, Codex,
Cursor, Copilot, Ollama-driven agents — and humans work the same repository
without clobbering each other: scope locks, a shared decision log, presence, and
git hooks. Entry points: `ats` (CLI), `ats-server` (FastAPI :8400), `ats-mcp`
(MCP server).

## Project layout
- `src/ai_team_sync/` — package (CLI, FastAPI server, routers, models, hooks).
- `tests/` — pytest-compatible tests (several also run standalone).
- `vscode-extension/` — optional editor integration.

## Dev workflow
- Install for development: `pip install -e .`.
- **Deploy with `scripts/deploy.sh`, never a bare `pipx install --force`** —
  only the script stamps the commit, so a bare install leaves `ats_version`
  reporting the previous build. `scripts/proof_context.sh` prints what is
  actually deployed before you claim anything about it.
- Run the server: `ats-server` (binds `127.0.0.1:8400` by default).
- Tests: `pytest` — or run a file directly, e.g.
  `python tests/test_detect_agent.py`. Follow TDD: write the failing test first.
  Before claiming anything about a suite, read **Test validity** below: a bare
  `pytest` run answers a narrower question than it appears to.

## Test validity — what a green suite actually proves

A bare `pytest` run validates **the current workstation environment**. It is not
evidence that GitHub Actions will be green, because two things this repo reads at
runtime differ between a developer box and CI.

**Ambient identity.** `session_pointer.detect_agent` resolves the worker label
from `ATS_AGENT`, then `CLAUDECODE` / `CLAUDE_CODE`. A dev shell exports these; a
CI runner exports none. The label decides the worker class, the worker class
decides authority, and authority decides whether a mutating call (session
creation, `extend_scope`) is served or refused `403 worker_authority`. The same
test can therefore run as an authorized worker locally and as `restricted` in CI.
`CLAUDE_CODE_SESSION_ID` matters for the same reason one level down: it keys the
per-session pointer, so it decides which ATS session a tool call speaks as.

**Registry composition.** `workers._config_path()` returns `$ATS_WORKERS_CONFIG`
if set, else `~/.config/ai-team-sync/workers.toml` if it exists, else `None`
(built-in defaults). A workstation with that file gets extra workers CI does not
have, which changes both authority answers and test collection.

### The canonical CI-equivalent invocation

```bash
env -u ATS_AGENT -u CLAUDECODE -u CLAUDE_CODE -u CLAUDE_CODE_SESSION_ID \
    -u ATS_WORKERS_CONFIG -u ATS_STATE_DIR HOME=$(mktemp -d) pytest -q
```

Each part earns its place:

| part | why |
|------|-----|
| `-u ATS_AGENT`, `-u CLAUDECODE`, `-u CLAUDE_CODE` | drop the ambient worker label, so authority is CI's, not your shell's |
| `-u CLAUDE_CODE_SESSION_ID` | drop the per-session pointer key, so no test silently speaks as your live session |
| `-u ATS_WORKERS_CONFIG` | remove an explicit registry override |
| `HOME=$(mktemp -d)` | a home with no `~/.config/ai-team-sync/workers.toml`, so the registry falls back to the built-ins CI uses |
| `-u ATS_STATE_DIR` | let pointer state follow that throwaway HOME instead of your real one |

**`ATS_WORKERS_CONFIG=deploy/workers.toml` is NOT this command.** That file
REPLACES the built-in registry rather than extending it, dropping `default` and
`restricted`; using it as a stand-in changes authority and collection and fails
unrelated tests.

### Tests must establish the identity they depend on

A test whose behaviour depends on worker authority or caller identity declares
that identity itself — it never inherits `ATS_AGENT`, `CLAUDECODE`,
`CLAUDE_CODE`, `CLAUDE_CODE_SESSION_ID` or equivalent ambient state. Set it in
the test that needs it, not in a shared fixture, so a test asserting an
*unresolved* or *unauthorized* caller cannot pass for the wrong reason. A fixture
may legitimately STRIP ambient identity; it should not hand one out.

### Four levels of evidence — never describe one as another

1. **Focused tests** — the tests for the change. Fastest signal, narrowest claim.
2. **Workstation full suite** — diagnostic. Reflects your config, including any
   local-only failures and any local-only passes.
3. **CI-equivalent full suite** — the command above. Reproduces CI's
   configuration and ambient environment.
4. **The GitHub Actions run for the pushed SHA** — the only authority.

Level 3 does **not** reproduce level 4's dependency resolution: CI installs
freshly-resolved dependencies (`pip install ".[dev]"`), while a dev venv holds
whatever it was pinned at. That gap is not theoretical — the header of
`.github/workflows/tests.yml` records the outage it caused. So level 3 is the
cheapest way to catch configuration and identity divergence, and level 4 remains
mandatory for a landing claim.

## Conventions
- **Security:** `ATS_HOST` defaults to `127.0.0.1`; never default a bind to
  `0.0.0.0` — the write API is unauthenticated. Never commit secrets; use
  `.env` (gitignored) or environment variables.
- **Versioning:** keep `src/ai_team_sync/__init__.py`, the FastAPI `version=`,
  and `pyproject.toml` in sync.
- **Agent identity — two different questions.** `ATS_AGENT` (explicit, any
  agent) then known env signatures resolve a session's **own label**; the one
  detection lives in `session_pointer.detect_agent` and `cli._detect_agent`
  delegates to it. That label is NOT evidence of which worker ran a
  **delegation**: `delegation.child_env` injects `ATS_AGENT` into the child, so
  asking the child who it is returns the parent's own text. Delegation
  provenance is the executable the parent resolved at spawn, recorded as
  `delegations.resolved_binary` and re-validated server-side against the worker
  registry. See [Delegation](docs/delegation.md).

## Coordinate while you work
If an ai-team-sync server is running, use it on yourself:
`ats lock check <path>` before editing, `ats session start` when you begin,
`ats decision list --all` to read prior decisions, `ats session complete` at the end.
