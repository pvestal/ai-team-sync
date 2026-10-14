"""Incidental project mentions never move or block an anchored session.

Incident 2026-10-14 19:01Z: the operator's Stage 1 GO prompt said "Do not
change anime-studio". The ATS-first prompt hook read that as a request to switch
a session anchored to the ATS repository over to /opt/anime-studio, and blocked
the prompt, so the GO never reached the worker. A governed project named in a
prompt only counts as a switch when the prompt instructs work there; negations,
quotations, examples and reports are context, not direction.
"""

from __future__ import annotations

import zlib

import pytest

ANIME_ROOT = "/opt/anime-studio"
ECHO_ROOT = "/opt/tower-echo-brain"
ROOTS = [ANIME_ROOT, ECHO_ROOT]
ATS_ROOT = "/srv/ai-team-sync"  # an anchored, non-governed repository

# Verbatim from the blocked prompt (transcript 7be0617e, 2026-10-14T19:01:19Z).
INCIDENT_GO_PROMPT = """<pasted_content id="d25d">
**STAGE 1 — GO AUTHORIZED, CONDITIONAL ON FINAL PREFLIGHT**

I approve D1, D2, D3, D5, D6, D7 and D8 as reported.

Proceed with the documented Stage 1 deployment from the reviewed candidate `1b094ed`, following `docs/p0-stage1-go-packet.md` section 10.

Before making production changes:

1. Verify current ATS main, branch ancestry and clean fast-forward eligibility.
2. Rerun the live lock-hazard preflight and version/install census.
3. Verify the candidate has not changed since the independent Codex review.
4. Confirm hook configuration, compatibility, backup and terminal rollback access.
5. Preserve the known test failure and all documented residual bypasses in the deployment record.
6. Stop and request a new decision if any precondition differs materially from the reviewed packet.

If preflight passes, execute the Stage 1 steps in their documented order, verifying each checkpoint and stopping on failure.

Afterward, prove the running ATS service and installed package match the deployed code; check hook behavior, explicit ticket claiming, queued handoffs and overall service health without disrupting other agents.

Do not change anime-studio, enable `[restart_gate]`, restart governed rendering services, dispatch GPU work or begin Stage 2 deployment under this authorization.

Report the exact deployed commit, migration state, verification results, remaining bypasses and any rollback actions.

**Stage 2 remains NOT AUTHORIZED for production.** Keep feeder, autodispatch and orchestrator disabled. Keep #3576 on HOLD and the 15 Fae Wilds beats out of GPU dispatch.
</pasted_content id="d25d">"""

HOOK_ERROR_PASTE = (
    "why did this happen:\n"
    "ATS-FIRST context resolution failed for governed work: ATS session "
    "1351ef46-576a-401a-bcdf-43512b4de49e is already anchored to "
    "/home/patrick/code/ai-team-sync; start a new session before switching "
    "governed project to /opt/anime-studio. The prompt was blocked before the "
    "worker could substitute Echo/Git/DB for ATS coordination context."
)

PASTED_HANDBACK = (
    "why is the terminal showing:\n"
    '<agent-message from="ac4a6a93173617cc7">\n'
    "[Subagent hand-back] The text below is the final report of a subagent.\n"
    "  Codex reviewed task 3537 in /opt/tower-echo-brain/.worktrees/fix-3537 "
    "for Echo Brain.\n"
    "</agent-message>"
)

# Prompts that NAME a governed project without instructing work there.
INCIDENTAL = {
    "incident_go_prompt": INCIDENT_GO_PROMPT,
    "negative_do_not": "Fix the ATS hook. Do not touch Echo Brain.",
    "negative_contraction": "Don't edit anime-studio while you rebase the ATS branch.",
    "negative_never": "Never restart anime-studio services from this session.",
    "negative_without": "Run the ATS suite without touching anime-studio.",
    "negative_nothing": "Change nothing in anime-studio; only report.",
    "negative_list_tail": "Do not change ATS main, anime-studio or Echo Brain.",
    "subordinate_clause": "Fix the hook so that it ignores anime-studio mentions.",
    "double_quoted": 'The packet says "fix anime-studio first" but that is out of scope.',
    "single_quoted": "A prompt like 'work on Echo Brain' should be classified correctly.",
    "inline_code": "Explain why `cd /opt/anime-studio && make deploy` was refused.",
    "fenced_code": "Explain this log:\n```\nfix anime-studio now\n```",
    "blockquote": "> Switch to anime-studio and fix the gateway.\nWhat did they mean?",
    "example_eg": "Add tests for directives, e.g. fix anime-studio.",
    "example_for_example": "Cover examples. For example: switch to Echo Brain.",
    "status_report": "#3577 merged and deployed to anime-studio at main 5d55f4ee.",
    "status_report_past_tense": "Fixed the anime-studio hold check yesterday; Echo Brain unchanged.",
    "hook_error_paste": HOOK_ERROR_PASTE,
    "conditional_recommendation": "I would finally build the Anime Studio adapter.",
    "ahead_of": "Move the dirty-tree problem ahead of any further anime-studio work.",
    "lookalike_path": "Commit 86f4196c in /home/patrick/Documents/anime-studio-authz.",
    # Codex CLASS 2 r1 (decision 3b6f0c78) reproductions and their classes.
    "colon_negation": "Never do this: restart anime-studio.",
    "negated_list_continuation": "Do not change ATS, restart anime-studio, or touch Echo Brain.",
    "status_report_noun": "Review of anime-studio complete; Echo Brain unchanged.",
    "read_two_names": "Give me the Anime Studio status; Echo Brain is out of scope.",
    "read_then_negation": "Explain Anime Studio. Do not touch Echo Brain.",
    "pasted_handback": PASTED_HANDBACK,
}

# Prompts that genuinely instruct work in the named governed project.
DIRECTIVES = {
    "correction_ask": ("Correction. Ask Echo Brain to find and render.", ECHO_ROOT),
    "switch_to": ("Switch to anime-studio and fix the hold check.", ANIME_ROOT),
    "edit_path": (
        "Now edit /opt/anime-studio/packages/scene_generation/dispatch_holds.py.",
        ANIME_ROOT,
    ),
    "negation_in_other_clause": (
        "Do not touch ATS; fix the anime-studio gateway instead.",
        ANIME_ROOT,
    ),
    "polite_question": ("Can you work on Echo Brain next?", ECHO_ROOT),
    "we_need_to": ("We need to deploy anime-studio tonight.", ANIME_ROOT),
    "could_you": ("Could you restart anime-studio once?", ANIME_ROOT),
    # Codex CLASS 2 r1 (decision 3b6f0c78) reproductions and their classes.
    "backticked_path": ("Edit `/opt/anime-studio/packages/gateway.py` now.", ANIME_ROOT),
    "go_ahead": ("Go ahead and fix anime-studio.", ANIME_ROOT),
    "but_contrast": ("Don't edit ATS but fix anime-studio.", ANIME_ROOT),
    "comma_instead": ("Do not touch ATS, fix the anime-studio gateway instead.", ANIME_ROOT),
    "interjection_no": ("No, switch to anime-studio.", ANIME_ROOT),
    "governed_path_command": (
        "cd /opt/tower-echo-brain/.worktrees/fix-3537 && run the suite.",
        ECHO_ROOT,
    ),
    "pasted_directive": (
        '<pasted_content id="x1">\nFix the anime-studio hold check.\n'
        '</pasted_content id="x1">',
        ANIME_ROOT,
    ),
    "directive_beside_negated_other": (
        "Fix anime-studio; don't touch Echo Brain.",
        ANIME_ROOT,
    ),
}


def _target(prompt, cwd="/home/patrick/Documents"):
    from ai_team_sync.context_resolution import resolve_request_target

    return resolve_request_target(prompt, cwd=cwd, governed_roots=ROOTS)


# --------------------------------------------------------------------------- #
# Resolver: classification of the text
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(INCIDENTAL))
def test_incidental_mentions_are_not_directives(name):
    target = _target(INCIDENTAL[name])

    # None = generic (several names, no subject): unchanged long-standing rule.
    assert target is None or target.directed == (), (name, target.directives)


@pytest.mark.parametrize("name", sorted(DIRECTIVES))
def test_work_directives_are_recognized(name):
    prompt, root = DIRECTIVES[name]
    target = _target(prompt)

    assert target is not None
    assert target.directed == (root,), (name, target.directives)


def test_directive_evidence_names_the_triggering_clause():
    target = _target("Thanks. Switch to anime-studio and fix the hold check.")

    assert target.directives == (
        (ANIME_ROOT, "Switch to anime-studio and fix the hold check"),
    )


def test_status_read_is_not_a_work_directive():
    target = _target("Give me the current Echo Brain status.")

    assert target.directed == ()
    assert target.repo_root == ECHO_ROOT  # unchanged: a single name still supplies context


def _old_rule(prompt, cwd, roots):
    """Reference copy of the pre-fix resolver (a0fa3d6), for item 6."""
    from ai_team_sync.briefs import explicit_task_id
    from ai_team_sync.context_resolution import _aliases, _normal_words, _repo_from_cwd

    words = f" {_normal_words(prompt)} "
    matches = [r for r in roots if any(f" {a} " in words for a in _aliases(r))]
    prompt_repo = matches[0] if len(matches) == 1 else ""
    cwd_repo = _repo_from_cwd(cwd, roots)
    if explicit_task_id(prompt) is not None or prompt_repo or cwd_repo:
        return "governed"
    return None


@pytest.mark.parametrize(
    "prompt",
    list(INCIDENTAL.values()) + [p for p, _r in DIRECTIVES.values()],
)
def test_previously_generic_prompts_stay_generic_unless_they_direct_work(prompt):
    cwd = "/home/patrick/Documents"
    target = _target(prompt, cwd=cwd)

    if _old_rule(prompt, cwd, ROOTS) is None:
        assert target is None or target.directed, (prompt, target)


def test_incidental_mention_does_not_outrank_a_governed_cwd(tmp_path):
    repo = tmp_path / "anime-studio"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    from ai_team_sync.context_resolution import resolve_request_target

    target = resolve_request_target(
        "Run the suite here. Do not touch Echo Brain.",
        cwd=str(repo),
        governed_roots=[str(repo), ECHO_ROOT],
    )

    assert target.repo_root == str(repo)
    assert target.reason == "governed_cwd"
    assert target.mentioned == (ECHO_ROOT,)


def test_work_directive_outranks_the_governed_cwd(tmp_path):
    repo = tmp_path / "anime-studio"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    from ai_team_sync.context_resolution import resolve_request_target

    target = resolve_request_target(
        "Correction. Ask Echo Brain to find and render.",
        cwd=str(repo),
        governed_roots=[str(repo), ECHO_ROOT],
    )

    assert target.repo_root == ECHO_ROOT
    assert target.reason == "project_name"


# --------------------------------------------------------------------------- #
# Hook: anchored sessions against a real (in-process) ATS
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_live_echo(monkeypatch):
    """Exact-task briefs must never reach the live Echo Brain from a test."""
    from copy import deepcopy

    import ai_team_sync.briefs as briefs
    from tests.test_task_brief import TASK_ENVELOPE

    def envelope(task_id, **_kwargs):
        env = deepcopy(TASK_ENVELOPE)
        env["id"] = int(task_id)
        env["task_context"]["task"]["id"] = int(task_id)
        rulings = env["task_context"]["operator_rulings"]
        for ruling in rulings["current"] + rulings["history"]:
            ruling["scope"]["tower_task_id"] = int(task_id)
        return env, None

    monkeypatch.setattr(briefs, "fetch_tower_task_data", envelope)
    monkeypatch.setitem(briefs.build_brief.__globals__, "fetch_tower_task_data", envelope)
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [])


def _env(monkeypatch, tmp_path, cid):
    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", cid)
    monkeypatch.setenv("ATS_DEVELOPER", "patrick")
    monkeypatch.setenv("ATS_COORDINATED_REPOS", f"{ANIME_ROOT}:{ECHO_ROOT}")
    monkeypatch.chdir(tmp_path)


async def _claimed_session(client, tmp_path, monkeypatch, cid, repo_root):
    """A session that holds authority (scope) and is anchored to repo_root."""
    from ai_team_sync.hooks import session_autostart

    _env(monkeypatch, tmp_path, cid)
    sid = await session_autostart.ensure_session("http://test", client)
    response = await client.patch(
        f"/api/sessions/{sid}", json={"repo_root": repo_root, "scope": ["src/**"]}
    )
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["repo_root"] == repo_root and row["scope"] == ["src/**"]
    return sid


async def _prompt(client, cid, cwd, prompt):
    from ai_team_sync.hooks import ats_context

    return await ats_context.resolve_prompt_context(
        "http://test", client, {"session_id": cid, "cwd": str(cwd), "prompt": prompt}
    )


@pytest.mark.asyncio
async def test_incident_go_prompt_reaches_the_anchored_owner(client, tmp_path, monkeypatch):
    """Replay of 2026-10-14 19:01Z: the GO must be delivered, the anchor kept."""
    cid = "60606060-1111-2222-3333-444455556666"
    sid = await _claimed_session(client, tmp_path, monkeypatch, cid, ATS_ROOT)

    note = await _prompt(client, cid, tmp_path, INCIDENT_GO_PROMPT)

    assert "ATS-FIRST CONTEXT RESOLUTION" in note
    assert f"resolved repo: {ATS_ROOT}" in note
    assert "NOT A PROJECT SWITCH" in note and ANIME_ROOT in note
    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["repo_root"] == ATS_ROOT
    assert row["scope"] == ["src/**"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(INCIDENTAL))
async def test_incidental_mention_neither_moves_nor_blocks_a_claimed_session(
    client, tmp_path, monkeypatch, name
):
    cid = f"{zlib.crc32(name.encode()):08x}-1111-2222-3333-444455556666"
    sid = await _claimed_session(client, tmp_path, monkeypatch, cid, ATS_ROOT)

    note = await _prompt(client, cid, tmp_path, INCIDENTAL[name])

    assert note is None or f"resolved repo: {ATS_ROOT}" in note
    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["repo_root"] == ATS_ROOT
    assert row["ticket_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(DIRECTIVES))
async def test_work_directive_to_another_project_blocks_a_claimed_session(
    client, tmp_path, monkeypatch, name
):
    from ai_team_sync.hooks import ats_context

    cid = f"{zlib.crc32(name.encode()):08x}-9999-2222-3333-444455556666"
    sid = await _claimed_session(client, tmp_path, monkeypatch, cid, ATS_ROOT)
    prompt, root = DIRECTIVES[name]

    with pytest.raises(ats_context.ContextResolutionError, match="start a new session") as exc:
        await _prompt(client, cid, tmp_path, prompt)

    assert root in str(exc.value)
    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["repo_root"] == ATS_ROOT


@pytest.mark.asyncio
async def test_block_message_quotes_the_directive(client, tmp_path, monkeypatch):
    from ai_team_sync.hooks import ats_context

    cid = "70707070-1111-2222-3333-444455556666"
    await _claimed_session(client, tmp_path, monkeypatch, cid, ATS_ROOT)

    with pytest.raises(ats_context.ContextResolutionError) as exc:
        await _prompt(client, cid, tmp_path, "Switch to anime-studio and fix the hold check.")

    assert '"Switch to anime-studio and fix the hold check"' in str(exc.value)


@pytest.mark.asyncio
async def test_directive_to_a_second_project_blocks_even_when_it_names_the_anchor(
    client, tmp_path, monkeypatch
):
    from ai_team_sync.hooks import ats_context

    cid = "71717171-1111-2222-3333-444455556666"
    sid = await _claimed_session(client, tmp_path, monkeypatch, cid, ANIME_ROOT)

    with pytest.raises(ats_context.ContextResolutionError, match=ECHO_ROOT):
        await _prompt(client, cid, tmp_path, "Fix anime-studio and Echo Brain together.")

    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["repo_root"] == ANIME_ROOT


@pytest.mark.asyncio
async def test_status_read_of_another_project_does_not_block_a_claimed_session(
    client, tmp_path, monkeypatch
):
    cid = "72727272-1111-2222-3333-444455556666"
    sid = await _claimed_session(client, tmp_path, monkeypatch, cid, ANIME_ROOT)

    note = await _prompt(client, cid, tmp_path, "Give me the current Echo Brain status.")

    assert f"resolved repo: {ANIME_ROOT}" in note
    assert "NOT A PROJECT SWITCH" in note
    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["repo_root"] == ANIME_ROOT


@pytest.mark.asyncio
async def test_working_from_another_governed_repo_still_blocks(client, tmp_path, monkeypatch):
    """A physical switch (cwd inside a different governed repo) is still refused."""
    from ai_team_sync.hooks import ats_context

    repo = tmp_path / "anime-studio"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    cid = "73737373-1111-2222-3333-444455556666"
    _env(monkeypatch, tmp_path, cid)
    monkeypatch.setenv("ATS_COORDINATED_REPOS", f"{repo}:{ECHO_ROOT}")
    from ai_team_sync.hooks import session_autostart

    sid = await session_autostart.ensure_session("http://test", client)
    response = await client.patch(
        f"/api/sessions/{sid}", json={"repo_root": ECHO_ROOT, "scope": ["src/**"]}
    )
    assert response.status_code == 200

    with pytest.raises(ats_context.ContextResolutionError, match="start a new session"):
        await _prompt(client, cid, repo / "packages", "Run the tests here.")

    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["repo_root"] == ECHO_ROOT


@pytest.mark.asyncio
async def test_incidental_mention_does_not_redirect_a_placeholder(client, tmp_path, monkeypatch):
    """An unclaimed placeholder keeps its anchor when the next prompt only negates another."""
    cid = "74747474-1111-2222-3333-444455556666"
    _env(monkeypatch, tmp_path, cid)
    from ai_team_sync.hooks import session_autostart

    sid = await session_autostart.ensure_session("http://test", client)
    first = await _prompt(client, cid, tmp_path, "Give me the current Anime Studio status.")
    assert f"resolved repo: {ANIME_ROOT}" in first

    second = await _prompt(client, cid, tmp_path, "Keep going. Do not touch Echo Brain.")

    assert f"resolved repo: {ANIME_ROOT}" in second
    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["repo_root"] == ANIME_ROOT


@pytest.mark.asyncio
async def test_status_report_does_not_redirect_a_placeholder(client, tmp_path, monkeypatch):
    """Codex r1 (3b6f0c78): a report naming another project moved a placeholder."""
    cid = "75757575-1111-2222-3333-444455556666"
    _env(monkeypatch, tmp_path, cid)
    from ai_team_sync.hooks import session_autostart

    sid = await session_autostart.ensure_session("http://test", client)
    await _prompt(client, cid, tmp_path, "Ask Echo Brain to find and render.")
    assert (await client.get(f"/api/sessions/{sid}")).json()["repo_root"] == ECHO_ROOT

    note = await _prompt(
        client, cid, tmp_path, "Review of anime-studio complete; Echo Brain unchanged."
    )

    assert note is None or f"resolved repo: {ECHO_ROOT}" in note
    row = (await client.get(f"/api/sessions/{sid}")).json()
    assert row["repo_root"] == ECHO_ROOT
