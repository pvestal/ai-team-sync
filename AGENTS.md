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
