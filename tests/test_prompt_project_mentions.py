"""Prompt text never moves or blocks an anchored session.

Incident 2026-10-14 19:01Z: the operator's Stage 1 GO prompt said "Do not
change anime-studio". The ATS-first prompt hook read that as a request to switch
a session anchored to the ATS repository over to /opt/anime-studio, and blocked
the prompt, so the GO never reached the worker.

Operator ruling 2026-10-14: the prompt hook is advisory for cross-project
mentions and context routing. Switching is the structured start_session action;
authority is enforced by the operation guards (tests/test_cross_project_operations.py).
The corpora below keep every phrasing collected while a keyword classifier was
tried and reviewed, so none of them can regain the power to block.
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

# Prompts that name a governed project without instructing work there.
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

# Prompts that instruct work in the named project (the root is kept for reference).
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


# Codex CLASS 2 r2 (decision 30bea255) strings: under the 2026-10-14 ruling
# these are ordinary mentions too -- prompt text never switches a session.
LATER_REVIEW_FORMS = {
    "investigate": "Investigate the anime-studio deadlock.",
    "test": "Test anime-studio now.",
    "review": "Review the anime-studio gateway implementation.",
    "then_after_negation": "Do not touch ai-team-sync, then fix anime-studio.",
    "now_after_negation": "Do not touch ai-team-sync, now restart anime-studio.",
}

# Every governed-project mention we have collected, whatever its grammar.
MENTIONS = {
    **INCIDENTAL,
    **{f"directive_{k}": prompt for k, (prompt, _root) in DIRECTIVES.items()},
    **{f"later_{k}": prompt for k, prompt in LATER_REVIEW_FORMS.items()},
}


def _old_rule(prompt, cwd, roots):
    """Reference copy of the pre-change resolver (a0fa3d6): generic or governed."""
    from ai_team_sync.briefs import explicit_task_id
    from ai_team_sync.context_resolution import _aliases, _normal_words, _repo_from_cwd

    words = f" {_normal_words(prompt)} "
    matches = [r for r in roots if any(f" {a} " in words for a in _aliases(r))]
    prompt_repo = matches[0] if len(matches) == 1 else ""
    if explicit_task_id(prompt) is not None or prompt_repo or _repo_from_cwd(cwd, roots):
        return "governed"
    return None


@pytest.mark.parametrize("name", sorted(MENTIONS))
def test_generic_versus_governed_is_unchanged(name):
    """No prompt gains or loses an ATS call (and so fail-closed exposure)."""
    from ai_team_sync.context_resolution import resolve_request_target

    for cwd in ("/home/patrick/Documents", ANIME_ROOT):
        new = resolve_request_target(MENTIONS[name], cwd=cwd, governed_roots=ROOTS)
        assert (new is None) == (_old_rule(MENTIONS[name], cwd, ROOTS) is None), (name, cwd)


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


async def _placeholder(client, tmp_path, monkeypatch, cid, first_prompt):
    """An unclaimed placeholder anchored by its first governed prompt."""
    from ai_team_sync.hooks import session_autostart

    _env(monkeypatch, tmp_path, cid)
    sid = await session_autostart.ensure_session("http://test", client)
    await _prompt(client, cid, tmp_path, first_prompt)
    return sid


async def _prompt(client, cid, cwd, prompt):
    from ai_team_sync.hooks import ats_context

    return await ats_context.resolve_prompt_context(
        "http://test", client, {"session_id": cid, "cwd": str(cwd), "prompt": prompt}
    )


async def _row(client, sid):
    return (await client.get(f"/api/sessions/{sid}")).json()


@pytest.mark.asyncio
async def test_incident_go_prompt_reaches_the_anchored_owner(client, tmp_path, monkeypatch):
    """Replay of 2026-10-14 19:01Z: the GO is delivered and the anchor kept."""
    cid = "60606060-1111-2222-3333-444455556666"
    sid = await _claimed_session(client, tmp_path, monkeypatch, cid, ATS_ROOT)

    note = await _prompt(client, cid, tmp_path, INCIDENT_GO_PROMPT)

    assert "ATS-FIRST CONTEXT RESOLUTION" in note
    assert f"resolved repo: {ATS_ROOT}" in note
    assert "PROJECT MENTION (advisory, not a switch)" in note and ANIME_ROOT in note
    assert "start_session with repo_root=/opt/anime-studio" in note
    row = await _row(client, sid)
    assert (row["repo_root"], row["scope"], row["ticket_id"]) == (ATS_ROOT, ["src/**"], None)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(MENTIONS))
async def test_prompt_mention_never_moves_or_blocks_a_claimed_session(
    client, tmp_path, monkeypatch, name
):
    cid = f"{zlib.crc32(name.encode()):08x}-1111-2222-3333-444455556666"
    sid = await _claimed_session(client, tmp_path, monkeypatch, cid, ATS_ROOT)

    note = await _prompt(client, cid, tmp_path, MENTIONS[name])  # must not raise

    assert note is None or f"resolved repo: {ATS_ROOT}" in note
    row = await _row(client, sid)
    assert (row["repo_root"], row["ticket_id"]) == (ATS_ROOT, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(MENTIONS))
async def test_prompt_mention_never_moves_an_anchored_placeholder(
    client, tmp_path, monkeypatch, name
):
    cid = f"{zlib.crc32(name.encode()):08x}-7777-2222-3333-444455556666"
    sid = await _placeholder(client, tmp_path, monkeypatch, cid, "Echo Brain status, please.")
    assert (await _row(client, sid))["repo_root"] == ECHO_ROOT

    note = await _prompt(client, cid, tmp_path, MENTIONS[name])

    assert note is None or f"resolved repo: {ECHO_ROOT}" in note
    assert (await _row(client, sid))["repo_root"] == ECHO_ROOT


@pytest.mark.asyncio
async def test_cwd_inside_another_governed_repo_still_blocks_a_claimed_session(
    client, tmp_path, monkeypatch
):
    """The physical switch (not keyword inference) keeps its existing refusal."""
    from ai_team_sync.hooks import ats_context

    repo = tmp_path / "anime-studio"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    cid = "73737373-1111-2222-3333-444455556666"
    sid = await _claimed_session(client, tmp_path, monkeypatch, cid, ECHO_ROOT)
    monkeypatch.setenv("ATS_COORDINATED_REPOS", f"{repo}:{ECHO_ROOT}")

    with pytest.raises(ats_context.ContextResolutionError, match="start a new session"):
        await _prompt(client, cid, repo / "packages", "Run the tests here.")

    assert (await _row(client, sid))["repo_root"] == ECHO_ROOT


@pytest.mark.asyncio
async def test_structured_start_session_is_the_switch(client, tmp_path, monkeypatch):
    """Switching projects = a new ATS session anchored there (what MCP start_session does)."""
    from ai_team_sync import session_pointer as sp

    cid = "76767676-1111-2222-3333-444455556666"
    old = await _claimed_session(client, tmp_path, monkeypatch, cid, ATS_ROOT)
    repo = tmp_path / "anime-studio"  # a disposable governed repo, never the live tree
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "holds.py").write_text("x = 1\n")
    target = str(repo.resolve())
    monkeypatch.setenv("ATS_COORDINATED_REPOS", f"{target}:{ECHO_ROOT}")

    made = await client.post(
        "/api/sessions",
        json={"developer": "patrick", "agent": f"claude-code:{cid[:8]}", "scope": ["holds.py"],
              "description": "explicit switch", "repo_root": target, "auto_lock": True},
    )
    assert made.status_code == 201, made.text
    new = made.json()["id"]
    sp.save_pointer(new)
    sp.save_approval_token(new, made.headers.get("X-ATS-Approval-Token", ""))

    note = await _prompt(client, cid, tmp_path, "Continue the anime-studio hold check.")

    assert f"session: {new}" in note and f"resolved repo: {target}" in note
    assert "PROJECT MENTION" not in note
    assert (await _row(client, new))["repo_root"] == target
    assert (await _row(client, old))["repo_root"] == ATS_ROOT  # the old row is untouched
