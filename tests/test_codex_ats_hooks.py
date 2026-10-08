"""Codex lifecycle mapping, registration states, and startup-race recovery."""

from __future__ import annotations

import pytest

from ai_team_sync import session_pointer as sp
from ai_team_sync.hooks import ats_context, codex_session_autostart
from ai_team_sync.hooks.session_registration import RegistrationInput, ensure_session

ANIME_ROOT = "/opt/anime-studio"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATS_DEVELOPER", "patrick")
    monkeypatch.setenv("ATS_COORDINATED_REPOS", ANIME_ROOT)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("ATS_SESSION_ID", raising=False)


def _input(cid: str = "codex-life-1111", **overrides) -> RegistrationInput:
    values = {
        "lifecycle_session_id": cid,
        "agent": "codex",
        "cwd": "/home/patrick/Documents",
        "hook_event_name": "SessionStart",
        "model": "gpt-5.6-sol",
        "source": "startup",
    }
    values.update(overrides)
    return RegistrationInput(**values)


def test_codex_adapter_maps_hook_fields_without_claude_environment(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    mapped = codex_session_autostart.registration_input(
        {
            "session_id": "codex-hook-session",
            "cwd": "/opt/anime-studio",
            "hook_event_name": "SessionStart",
            "model": "gpt-6.1-sol",
            "source": "resume",
        }
    )

    assert mapped == RegistrationInput(
        lifecycle_session_id="codex-hook-session",
        agent="codex",
        cwd="/opt/anime-studio",
        hook_event_name="SessionStart",
        model="gpt-6.1-sol",
        source="resume",
    )


@pytest.mark.asyncio
async def test_active_codex_session_is_reused(client):
    sid = await ensure_session("http://test", client, _input())
    resumed = await ensure_session("http://test", client, _input(source="resume"))

    assert resumed == sid
    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["status"] == "active"
    assert row["agent"] == "codex:codex-li"
    assert "model=gpt-5.6-sol" in row["description"]


@pytest.mark.asyncio
async def test_auto_reaped_codex_session_is_resurrected_in_place(client, db_session):
    from ai_team_sync.models import Session

    sid = await ensure_session("http://test", client, _input())
    row = await db_session.get(Session, sid)
    row.status = "completed"
    row.auto_completed = True
    await db_session.commit()

    resumed = await ensure_session("http://test", client, _input(source="resume"))

    assert resumed == sid
    after = (await client.get(f"/api/sessions/{sid}")).json()
    assert after["status"] == "active"
    assert after["auto_completed"] is False


@pytest.mark.asyncio
async def test_explicitly_completed_session_stays_terminal_and_gets_replacement(client, db_session):
    from ai_team_sync.models import Session

    sid = await ensure_session("http://test", client, _input())
    row = await db_session.get(Session, sid)
    row.status = "completed"
    row.auto_completed = False
    await db_session.commit()

    replacement = await ensure_session("http://test", client, _input(source="resume"))

    assert replacement and replacement != sid
    old = (await client.get(f"/api/sessions/{sid}")).json()
    new = (await client.get(f"/api/sessions/{replacement}")).json()
    assert old["status"] == "completed"
    assert old["auto_completed"] is False
    assert new["status"] == "active"
    assert sp.resolve_pointer("codex-life-1111", allow_global=False) == replacement


@pytest.mark.asyncio
async def test_unknown_session_pointer_creates_replacement(client):
    sp.save_pointer("missing-session", "codex-life-1111")

    sid = await ensure_session("http://test", client, _input())

    assert sid and sid != "missing-session"
    assert (await client.get(f"/api/sessions/{sid}")).status_code == 200


@pytest.mark.asyncio
async def test_prompt_stage_recovers_sessionstart_rest_race(client, monkeypatch):
    class UnavailableAtStartup:
        async def post(self, *args, **kwargs):
            raise OSError("ATS not ready")

    data = _input(cid="race-codex-2222")
    assert await ensure_session("http://offline", UnavailableAtStartup(), data) is None
    assert sp.resolve_pointer(data.lifecycle_session_id, allow_global=False) is None

    note = await ats_context.resolve_prompt_context(
        "http://test",
        client,
        {
            "session_id": data.lifecycle_session_id,
            "cwd": "/home/patrick/Documents",
            "hook_event_name": "UserPromptSubmit",
            "model": data.model,
            "prompt": "Give me the current Anime Studio status, blockers and recommendations.",
        },
        agent="codex",
    )

    sid = sp.resolve_pointer(data.lifecycle_session_id, allow_global=False)
    assert sid
    assert "ATS-FIRST CONTEXT RESOLUTION" in note
    assert f"session: {sid}" in note
    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["agent"] == "codex:race-cod"
    assert row["repo_root"] == ANIME_ROOT


@pytest.mark.asyncio
async def test_generic_codex_session_registers_but_remains_unscoped(client):
    sid = await ensure_session("http://test", client, _input(cid="generic-codex"))

    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["repo_root"] == ""
    assert row["scope"] == []
    assert (
        await ats_context.resolve_prompt_context(
            "http://test",
            client,
            {
                "session_id": "generic-codex",
                "cwd": "/home/patrick/Documents",
                "prompt": "what is 2+2?",
            },
            agent="codex",
        )
        is None
    )
