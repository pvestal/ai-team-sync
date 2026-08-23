"""#2517: one agent label, built where the lock guard can attribute it.

The PreToolUse lock guard attributes "my session" by matching the hook
payload's live Claude session id against the ATS row's agent string. Every
registrar must therefore build the label from session_pointer.agent_label —
the CLI built a bare 'claude-code' (and, worse, detected 'unknown' because
its env check missed CLAUDECODE), so a session registered via
`ats session start` could never be attributed to its own creator: the guard
named the caller's scope as "another ACTIVE session's scope" and every
re-register added another blocker. The CLI also kept only the shared global
pointer file, so two concurrent sessions clobbered each other's "current
session" (`ats status`: intermittent "No active session").
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_team_sync import session_pointer as sp
from ai_team_sync import cli
from ai_team_sync.hooks import session_autostart
from ai_team_sync.hooks.pre_tool_use_lockcheck import find_conflicts

CID = "5ead4223-7e14-4b58-8cc0-38bc54ec3bd6"


def test_agent_label_suffixes_known_base(monkeypatch):
    monkeypatch.setattr(sp, "claude_session_id", lambda: CID)
    assert sp.agent_label("claude-code") == "claude-code:5ead4223"


def test_agent_label_never_suffixes_unknown(monkeypatch):
    monkeypatch.setattr(sp, "claude_session_id", lambda: CID)
    assert sp.agent_label("unknown") == "unknown"


def test_agent_label_bare_without_cid(monkeypatch):
    monkeypatch.setattr(sp, "claude_session_id", lambda: None)
    assert sp.agent_label("claude-code") == "claude-code"


def test_autostart_label_is_the_ssot_label():
    assert session_autostart._agent_label(CID) == sp.agent_label("claude-code", CID)


def test_cli_session_start_posts_attributable_label(monkeypatch):
    monkeypatch.setattr(sp, "claude_session_id", lambda: CID)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("ATS_AGENT", raising=False)
    posted = {}

    class _Resp:
        def json(self):
            return {"id": "sess-1", "developer": "d", "scope": [], "branch": "",
                    "lock_count": 0}

    def _api(method, path, **kw):
        posted.update(kw.get("json") or {})
        return _Resp()
    monkeypatch.setattr(cli, "_api", _api)
    monkeypatch.setattr(cli, "_save_active_session", lambda sid: None)
    monkeypatch.setattr(cli, "_get_branch", lambda: "")
    monkeypatch.setattr(cli, "_get_developer", lambda: "d")

    cli.session_start.callback(scope=("packages/x/*",), desc="t", agent=None,
                               no_lock=True, exclusive=False)
    assert posted["agent"] == "claude-code:5ead4223"


def test_cli_explicit_agent_still_gets_the_token(monkeypatch):
    monkeypatch.setattr(sp, "claude_session_id", lambda: CID)
    posted = {}

    class _Resp:
        def json(self):
            return {"id": "sess-1", "developer": "d", "scope": [], "branch": "",
                    "lock_count": 0}
    monkeypatch.setattr(cli, "_api", lambda m, p, **kw: (posted.update(kw.get("json") or {}), _Resp())[1])
    monkeypatch.setattr(cli, "_save_active_session", lambda sid: None)
    monkeypatch.setattr(cli, "_get_branch", lambda: "")
    monkeypatch.setattr(cli, "_get_developer", lambda: "d")
    cli.session_start.callback(scope=("a/*",), desc="", agent="codex",
                               no_lock=True, exclusive=False)
    assert posted["agent"] == "codex:5ead4223"


def test_hook_attributes_a_cli_registered_session(monkeypatch):
    # The filed reproduction: MY OWN session's scope must never conflict.
    sessions = [{"agent": "claude-code:5ead4223", "status": "active",
                 "scope": ["packages/scene_generation/*"],
                 "description": "mine", "repo_root": ""}]
    out = find_conflicts("packages/scene_generation/x.py", sessions, CID, "")
    assert out == []


def test_cli_pointer_prefers_per_session_file(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_session_file", lambda: str(tmp_path / "legacy"))
    monkeypatch.setattr(sp, "claude_session_id", lambda: CID)
    monkeypatch.delenv("ATS_SESSION_ID", raising=False)
    # another concurrent session clobbers the global file...
    cli._save_active_session("mine-1234")
    (tmp_path / sp.GLOBAL_FILE_NAME).write_text("theirs-9999")
    # ...but this session still resolves its own row.
    assert cli._load_active_session() == "mine-1234"


def test_cli_clear_drops_the_per_session_pointer(monkeypatch, tmp_path):
    # In production cli._session_file() IS sp.global_pointer_path() (~/.ats_session
    # with ATS_STATE_DIR unset) — mirror that, or the test invents a split that
    # cannot happen and fails on resolve_pointer's global fallback.
    monkeypatch.setattr(sp, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "_session_file",
                        lambda: str(tmp_path / sp.GLOBAL_FILE_NAME))
    monkeypatch.setattr(sp, "claude_session_id", lambda: CID)
    monkeypatch.delenv("ATS_SESSION_ID", raising=False)
    cli._save_active_session("mine-1234")
    cli._clear_active_session()
    assert cli._load_active_session() is None
