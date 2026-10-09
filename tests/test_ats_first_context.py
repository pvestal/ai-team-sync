"""Deterministic ATS-first context resolution at the Claude prompt boundary."""

from __future__ import annotations

from copy import deepcopy

import pytest

ANIME_ROOT = "/opt/anime-studio"
ECHO_ROOT = "/opt/tower-echo-brain"


def test_request_target_resolves_explicit_task_without_a_repo():
    from ai_team_sync.context_resolution import resolve_request_target

    target = resolve_request_target(
        "Continue Tower task #4101", cwd="/tmp", governed_roots=[ANIME_ROOT, ECHO_ROOT]
    )

    assert target is not None
    assert target.task_id == 4101
    assert target.repo_root == ""
    assert target.reason == "explicit_task"


def test_request_target_resolves_governed_project_name():
    from ai_team_sync.context_resolution import resolve_request_target

    target = resolve_request_target(
        "Give me the current Anime Studio status, blockers and recommendations.",
        cwd="/home/patrick/Documents",
        governed_roots=[ANIME_ROOT, ECHO_ROOT],
    )

    assert target is not None
    assert target.task_id is None
    assert target.repo_root == ANIME_ROOT
    assert target.reason == "project_name"


def test_request_target_resolves_governed_repo_cwd(tmp_path):
    from ai_team_sync.context_resolution import resolve_request_target

    repo = tmp_path / "anime-studio"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    target = resolve_request_target(
        "Give me the current status, blockers and recommendations.",
        cwd=str(repo / "packages"),
        governed_roots=[str(repo), ECHO_ROOT],
    )

    assert target is not None
    assert target.repo_root == str(repo)
    assert target.reason == "governed_cwd"


def test_request_target_leaves_generic_conversation_unscoped():
    from ai_team_sync.context_resolution import resolve_request_target

    assert (
        resolve_request_target(
            "Help me rewrite this sentence.",
            cwd="/home/patrick/Documents",
            governed_roots=[ANIME_ROOT, ECHO_ROOT],
        )
        is None
    )


@pytest.mark.asyncio
async def test_project_prompt_anchors_placeholder_and_gets_ats_brief(client, tmp_path, monkeypatch):
    from ai_team_sync import session_pointer as sp
    from ai_team_sync.hooks import ats_context, session_autostart

    cid = "abcddcba-1111-2222-3333-444455556666"
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", cid)
    monkeypatch.setenv("ATS_DEVELOPER", "patrick")
    monkeypatch.setenv("ATS_COORDINATED_REPOS", f"{ANIME_ROOT}:{ECHO_ROOT}")
    monkeypatch.chdir(tmp_path)

    sid = await session_autostart.ensure_session("http://test", client)
    assert sid
    before = (await client.get(f"/api/sessions/{sid}")).json()
    assert before["repo_root"] == ""

    note = await ats_context.resolve_prompt_context(
        "http://test",
        client,
        {
            "session_id": cid,
            "cwd": str(tmp_path),
            "prompt": "Give me the current Anime Studio status, blockers and recommendations.",
        },
    )

    assert note is not None
    assert "ATS-FIRST CONTEXT RESOLUTION" in note
    assert "PROJECT / REPOSITORY CONTEXT" in note
    assert "Echo is supplemental" in note
    after = (await client.get(f"/api/sessions/{sid}")).json()
    assert after["repo_root"] == ANIME_ROOT
    assert after["scope"] == []  # project context is not a file-lock claim
    assert sp.resolve_pointer(cid, allow_global=False) == sid


@pytest.mark.asyncio
async def test_project_prompt_resurrects_auto_reaped_session(
    client, db_session, tmp_path, monkeypatch
):
    from ai_team_sync.hooks import ats_context, session_autostart
    from ai_team_sync.models import Session

    cid = "207985b7-1111-2222-3333-444455556666"
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", cid)
    monkeypatch.setenv("ATS_DEVELOPER", "patrick")
    monkeypatch.setenv("ATS_COORDINATED_REPOS", ANIME_ROOT)
    monkeypatch.chdir(tmp_path)

    sid = await session_autostart.ensure_session("http://test", client)
    row = await db_session.get(Session, sid)
    row.status = "completed"
    row.auto_completed = True
    await db_session.commit()

    note = await ats_context.resolve_prompt_context(
        "http://test",
        client,
        {
            "session_id": cid,
            "cwd": str(tmp_path),
            "prompt": "Give me the current Anime Studio status.",
        },
    )

    assert "ATS-FIRST CONTEXT RESOLUTION" in note
    after = (await client.get(f"/api/sessions/{sid}")).json()
    assert after["status"] == "active"
    assert after["auto_completed"] is False
    assert after["repo_root"] == ANIME_ROOT


@pytest.mark.asyncio
async def test_explicit_task_prompt_binds_session_and_injects_exact_authority(
    client, tmp_path, monkeypatch
):
    import ai_team_sync.briefs as briefs
    from ai_team_sync.hooks import ats_context, session_autostart
    from tests.test_task_brief import TASK_ENVELOPE

    cid = "10101010-1111-2222-3333-444455556666"
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", cid)
    monkeypatch.setenv("ATS_DEVELOPER", "patrick")
    monkeypatch.setenv("ATS_COORDINATED_REPOS", f"{ANIME_ROOT}:{ECHO_ROOT}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        briefs,
        "fetch_tower_task_data",
        lambda task_id, **kw: (TASK_ENVELOPE, None),
    )
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [])

    sid = await session_autostart.ensure_session("http://test", client)
    note = await ats_context.resolve_prompt_context(
        "http://test",
        client,
        {
            "session_id": cid,
            "cwd": str(tmp_path),
            "prompt": "Continue Tower task #4101",
        },
    )

    assert "EXACT TASK-SCOPED CONTEXT" in note
    assert "CURRENT OPERATOR RULINGS / PROHIBITIONS" in note
    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["ticket_id"] == 4101
    assert row["repo_root"] == "", "a Tower project display name is not a repository mapping"


@pytest.mark.asyncio
async def test_codex_prompt_for_3522_requires_exact_3522_context(
    client, tmp_path, monkeypatch
):
    import ai_team_sync.briefs as briefs
    from ai_team_sync import session_pointer as sp
    from ai_team_sync.hooks import ats_context
    from ai_team_sync.hooks.session_registration import lifecycle_session_key
    from tests.test_task_brief import TASK_ENVELOPE

    cid = "35223522-1111-2222-3333-444455556666"
    envelope = deepcopy(TASK_ENVELOPE)
    envelope["id"] = 3522
    envelope["task_context"]["task"]["id"] = 3522
    for ruling in (
        envelope["task_context"]["operator_rulings"]["current"]
        + envelope["task_context"]["operator_rulings"]["history"]
    ):
        ruling["scope"]["tower_task_id"] = 3522
    called = []

    def exact_task(task_id, **kwargs):
        called.append(task_id)
        assert task_id == 3522
        return envelope, None

    monkeypatch.setattr(briefs, "fetch_tower_task_data", exact_task)
    monkeypatch.setitem(briefs.build_brief.__globals__, "fetch_tower_task_data", exact_task)
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [])
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATS_DEVELOPER", "patrick")
    monkeypatch.setenv("ATS_COORDINATED_REPOS", f"{ANIME_ROOT}:{ECHO_ROOT}")

    note = await ats_context.resolve_prompt_context(
        "http://test",
        client,
        {
            "session_id": cid,
            "cwd": str(tmp_path),
            "prompt": "Give me the status of #3522.",
        },
        agent="codex",
    )

    assert called == [3522], note
    assert "EXACT TASK-SCOPED CONTEXT" in note
    sid = sp.resolve_pointer(lifecycle_session_key("codex", cid), allow_global=False)
    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["ticket_id"] == 3522


@pytest.mark.asyncio
async def test_generic_prompt_does_not_call_ats_context_api(monkeypatch):
    from ai_team_sync.hooks import ats_context

    monkeypatch.setenv("ATS_COORDINATED_REPOS", f"{ANIME_ROOT}:{ECHO_ROOT}")

    class NoCalls:
        def __getattr__(self, name):
            raise AssertionError(f"generic prompt unexpectedly called client.{name}")

    note = await ats_context.resolve_prompt_context(
        "http://test",
        NoCalls(),
        {
            "session_id": "generic-session",
            "cwd": "/home/patrick/Documents",
            "prompt": "Help me rewrite this sentence.",
        },
    )

    assert note is None


def test_governed_failure_is_a_blocking_hook_result(monkeypatch, capsys):
    from ai_team_sync.hooks import ats_context

    monkeypatch.setenv("ATS_COORDINATED_REPOS", ANIME_ROOT)

    async def fail(*args, **kwargs):
        raise ats_context.ContextResolutionError("ATS refused the brief")

    monkeypatch.setattr(ats_context, "resolve_prompt_context", fail)
    monkeypatch.setattr(
        ats_context.sys,
        "stdin",
        __import__("io").StringIO(
            '{"session_id":"fresh","cwd":"/tmp","prompt":"status of Anime Studio"}'
        ),
    )

    with pytest.raises(SystemExit) as exc:
        ats_context.main([])

    assert exc.value.code == 2
    assert "ATS-FIRST context resolution failed" in capsys.readouterr().err


def test_malformed_operator_config_blocks_instead_of_failing_open(monkeypatch, capsys):
    # Exit 1 is a non-blocking hook error to Claude Code: an uncaught config
    # error would let every governed prompt through with no ATS context.
    from ai_team_sync.hooks import ats_context

    monkeypatch.setenv("ATS_COORDINATED_REPOS", "relative/path")
    monkeypatch.setattr(
        ats_context.sys,
        "stdin",
        __import__("io").StringIO(
            '{"session_id":"fresh","cwd":"/tmp","prompt":"continue #2003"}'
        ),
    )

    with pytest.raises(SystemExit) as exc:
        ats_context.main([])

    assert exc.value.code == 2
    assert "operator config" in capsys.readouterr().err


def test_governed_prompt_runs_ats_before_supplement(monkeypatch, capsys):
    from ai_team_sync.hooks import ats_context

    events = []
    monkeypatch.setenv("ATS_COORDINATED_REPOS", ANIME_ROOT)

    async def ats_first(*args, **kwargs):
        events.append("ats")
        return "ATS CONTEXT"

    class Completed:
        returncode = 0
        stdout = "ECHO SUPPLEMENT\n"
        stderr = ""

    def supplement(*args, **kwargs):
        events.append("echo")
        assert kwargs["input"].startswith('{"session_id"')
        return Completed()

    monkeypatch.setattr(ats_context, "resolve_prompt_context", ats_first)
    monkeypatch.setattr(ats_context.subprocess, "run", supplement)
    monkeypatch.setattr(
        ats_context.sys,
        "stdin",
        __import__("io").StringIO(
            '{"session_id":"fresh","cwd":"/tmp","prompt":"status of Anime Studio"}'
        ),
    )

    with pytest.raises(SystemExit) as exc:
        ats_context.main(
            [
                "--supplement-command",
                "python echo-ambient.py --mode hook",
            ]
        )

    assert exc.value.code == 0
    assert events == ["ats", "echo"]
    output = capsys.readouterr().out
    assert output.index("ATS CONTEXT") < output.index("ECHO SUPPLEMENT")
