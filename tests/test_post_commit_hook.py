"""hooks/post_commit.py: which session a commit is credited to, and the installer.

A recorded commit is a mutation that also counts as liveness for the reaper, so
the hook must resolve its session the way every other mutation does: never
through the shared ~/.ats_session, which names whichever session on the box
wrote it last (resolve_pointer_source docstring, proven live 2026-09-11).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ai_team_sync.hooks import post_commit

REPO = Path(__file__).resolve().parents[1]
CID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def state(tmp_path, monkeypatch):
    for var in ("ATS_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "ATS_SESSION", "CLAUDE_PID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def posted(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json): calls.append((url, json))

    monkeypatch.setattr(post_commit.httpx, "Client", Client)
    monkeypatch.setattr(post_commit, "_head", lambda: ("c" * 40, "msg"))
    return calls


def _run():
    with pytest.raises(SystemExit) as e:
        post_commit.main()
    assert e.value.code == 0  # never fails the commit


def test_global_pointer_alone_credits_nobody(state, posted):
    (state / ".ats_session").write_text("someone-elses-session")
    _run()
    assert posted == []


def test_per_session_pointer_is_credited(state, posted, monkeypatch):
    from ai_team_sync import session_pointer as sp
    (state / ".ats_session").write_text("someone-elses-session")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CID)
    sp.session_pointer_path(CID).write_text("mine")
    _run()
    assert [c[1]["session_id"] for c in posted] == ["mine"]
    assert posted[0][0].endswith("/api/sessions/mine/commits")


def test_explicit_env_session_is_credited(state, posted, monkeypatch):
    monkeypatch.setenv("ATS_SESSION_ID", "explicit")
    _run()
    assert [c[1]["session_id"] for c in posted] == ["explicit"]


def test_installer_post_commit_only_chains_and_uses_ats_python(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    existing = repo / ".git" / "hooks" / "post-commit"
    existing.write_text("#!/usr/bin/env bash\necho existing-audit\n")
    existing.chmod(0o755)

    env = {**os.environ, "ATS_HOOKS": "post-commit", "ATS_PYTHON": "/opt/fake/python"}
    subprocess.run(["bash", str(REPO / "scripts" / "install-hooks.sh"), str(repo)],
                   check=True, env=env, capture_output=True)

    hooks = repo / ".git" / "hooks"
    assert not (hooks / "pre-commit").exists(), "pre-commit must not be installed"
    assert not (hooks / "prepare-commit-msg").exists()
    body = existing.read_text()
    assert "echo existing-audit" in body, "existing hook must be preserved"
    assert str(hooks / "post-commit-ats") in body
    assert "/opt/fake/python -m ai_team_sync.hooks.post_commit" in (hooks / "post-commit-ats").read_text()

    # idempotent
    subprocess.run(["bash", str(REPO / "scripts" / "install-hooks.sh"), str(repo)],
                   check=True, env=env, capture_output=True)
    assert existing.read_text().count("post-commit-ats") == 1
