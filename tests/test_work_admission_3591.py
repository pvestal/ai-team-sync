"""#3591: work admission at the existing ATS entry points.

Operator ruling 2026-10-15: repair the entry points that let new work bypass
existing tickets, owners and unfinished branches -- without a new coordination
system, without blocking on similarity, and without blocking on same-file
overlap. Every test runs against a real git repository and the in-process
server; only Echo (an external service) is stubbed.
"""
from __future__ import annotations

import json
import subprocess

import pytest
from sqlalchemy import select

from ai_team_sync import admission, briefs
from ai_team_sync.active_work import unfinished_branches
from ai_team_sync.context_resolution import resolve_request_target
from ai_team_sync.models import Decision
from ai_team_sync.operator_config import (OperatorConfigError, admission_mode_for,
                                          context_aliases, context_repositories)
from ai_team_sync.schemas import AdmissionClaim

_REAL_FETCH_TASK_OVERLAPS = briefs.fetch_task_overlaps  # before the autouse stub


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@example.invalid",
                           "-c", "user.name=t", *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(path, files=("src/a.py", "src/b.py", "docs/x.md")):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    for rel in files:
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(f"{rel}\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return str(path.resolve())


def _branch(root, name, rel, text, message):
    _git(root, "checkout", "-q", "-b", name, "main")
    (__import__("pathlib").Path(root) / rel).write_text(text)
    _git(root, "commit", "-q", "-am", message)
    _git(root, "checkout", "-q", "main")


def _config(monkeypatch, tmp_path, body):
    path = tmp_path / "operator.toml"
    path.write_text(body)
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(path))


@pytest.fixture
def governed(tmp_path, monkeypatch):
    root = _repo(tmp_path / "gov")
    _config(monkeypatch, tmp_path, f'[governance]\nrepositories = ["{root}"]\n')
    return root


@pytest.fixture(autouse=True)
def _no_echo(monkeypatch):
    """Echo is external; the overlap lookup is answered locally per test."""
    monkeypatch.setattr(briefs, "fetch_task_overlaps",
                        lambda objective, **kw: ([], "Echo stub: not consulted"))


# ── request resolution ─────────────────────────────────────────────────────

def test_a_prompt_naming_several_tickets_gets_every_one_never_none():
    target = resolve_request_target("reconcile ticket #3536 with #3560 and #3517",
                                    cwd="/nowhere", governed_roots=[], context_roots=[])
    assert target is not None and target.task_ids == (3517, 3536, 3560)
    assert target.task_id is None and target.reason == "explicit_tasks"
    assert target.best_effort  # added context never adds fail-closed exposure


def test_a_single_ticket_is_still_exact_and_fail_closed():
    target = resolve_request_target("work on ticket #3536", cwd="/nowhere",
                                    governed_roots=[], context_roots=[])
    assert target.task_id == 3536 and target.task_ids == () and not target.best_effort


def test_context_repositories_resolve_without_governance(tmp_path, monkeypatch):
    ctx = _repo(tmp_path / "coord-tool")
    _config(monkeypatch, tmp_path,
            f'[context]\nrepositories = ["{ctx}"]\naliases = {{ "{ctx}" = ["ats"] }}\n')
    assert context_repositories() == [ctx] and context_aliases() == {ctx: ["ats"]}

    by_cwd = resolve_request_target("add a verification view", cwd=ctx + "/src",
                                    governed_roots=[])
    assert by_cwd.repo_root == ctx and by_cwd.context_only and by_cwd.best_effort
    assert by_cwd.reason == "context_cwd"
    by_alias = resolve_request_target("change the ATS brief", cwd="/nowhere", governed_roots=[])
    assert by_alias.repo_root == ctx and by_alias.context_only


def test_several_projects_get_context_without_an_anchor(tmp_path, monkeypatch):
    a, b = _repo(tmp_path / "studio"), _repo(tmp_path / "brain")
    target = resolve_request_target("audit studio and brain task overlap", cwd="/nowhere",
                                    governed_roots=[a, b], context_roots=[])
    assert target.reason == "multi_project" and target.repo_root == ""
    assert target.best_effort


def test_a_subagent_handback_never_anchors_and_never_blocks(tmp_path):
    a = _repo(tmp_path / "studio")
    prompt = '<agent-message from="x">\n[Subagent hand-back] report naming studio'
    target = resolve_request_target(prompt, cwd="/nowhere", governed_roots=[a],
                                    context_roots=[])
    assert target.reason == "subagent_handback" and target.repo_root == ""
    assert target.best_effort


def test_unknown_context_or_admission_keys_are_refused(tmp_path, monkeypatch):
    _config(monkeypatch, tmp_path, '[admission]\nmode = "strict"\n')
    with pytest.raises(OperatorConfigError):
        admission_mode_for("/x")
    _config(monkeypatch, tmp_path, '[context]\nrepos = []\n')
    with pytest.raises(OperatorConfigError):
        context_repositories()


def test_enforcement_applies_only_to_listed_repositories(tmp_path, monkeypatch):
    a, b = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    _config(monkeypatch, tmp_path,
            f'[admission]\nmode = "enforce"\nenforce_repositories = ["{a}"]\n')
    assert admission_mode_for(a) == "enforce" and admission_mode_for(b) == "shadow"
    _config(monkeypatch, tmp_path, "")
    assert admission_mode_for(a) == "shadow"  # absent config: record, refuse nothing


# ── the verdict: no similarity, no same-file blocking ──────────────────────

@pytest.mark.parametrize("claim,ticket,governed_repo,codes", [
    (None, 3591, True, []),                                   # continue
    (None, None, True, ["unclassified_canonical_claim"]),
    (None, None, False, []),                                  # not governed: nothing
    (AdmissionClaim(kind="new", reason="r"), None, True, []),
    (AdmissionClaim(kind="exception", reason="operator said"), None, True, []),
    (AdmissionClaim(kind="related", related_to=[1]), None, True, []),
    (AdmissionClaim(kind="duplicate", duplicate_of=3536), 3560, True, ["declared_duplicate"]),
])
def test_only_unclassified_governed_claims_and_declared_duplicates_conflict(
        claim, ticket, governed_repo, codes):
    verdict = admission.evaluate(claim, ticket_id=ticket, governed=governed_repo, mode="enforce")
    assert [c["code"] for c in verdict.hard_conflicts] == codes
    assert verdict.refuse == bool(codes)
    shadow = admission.evaluate(claim, ticket_id=ticket, governed=governed_repo, mode="shadow")
    assert not shadow.refuse and shadow.as_dict()["would_refuse"] == bool(codes)


@pytest.mark.parametrize("claim,ticket", [
    (AdmissionClaim(kind="continue"), None),
    (AdmissionClaim(kind="new"), None),
    (AdmissionClaim(kind="exception", reason="  "), None),
    (AdmissionClaim(kind="related"), None),
    (AdmissionClaim(kind="duplicate"), None),
])
def test_malformed_classifications_are_rejected(claim, ticket):
    assert admission.validate(claim, ticket) is not None


# ── the claim doors ────────────────────────────────────────────────────────

async def _start(client, root, scope, **extra):
    return await client.post("/api/sessions", json={
        "developer": "dev", "agent": "default", "scope": scope, "auto_lock": True,
        "repo_root": root, "description": "change the parser", **extra})


async def _admissions(db_session):
    rows = (await db_session.execute(
        select(Decision).where(Decision.title == admission.ADMISSION_TITLE))).scalars().all()
    return [json.loads(r.reasoning) for r in rows]


@pytest.mark.asyncio
async def test_shadow_admits_and_records_what_enforcement_would_refuse(
        client, db_session, governed):
    resp = await _start(client, governed, ["src/a.py"])
    assert resp.status_code == 201, resp.text
    verdict = resp.json()["admission"]
    assert verdict["mode"] == "shadow" and verdict["would_refuse"] and not verdict["refused"]
    records = await _admissions(db_session)
    assert len(records) == 1 and records[0]["verdict"]["classification"] == "unclassified"


@pytest.mark.asyncio
async def test_enforce_refuses_only_the_unclassified_ticketless_claim(
        client, tmp_path, monkeypatch, governed):
    _config(monkeypatch, tmp_path,
            f'[governance]\nrepositories = ["{governed}"]\n'
            f'[admission]\nmode = "enforce"\nenforce_repositories = ["{governed}"]\n')
    refused = await _start(client, governed, ["src/a.py"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "admission_refused"

    for extra in ({"ticket_id": 3591},
                  {"admission": {"kind": "new", "reason": "no ticket covers this"}},
                  {"admission": {"kind": "exception", "reason": "operator: urgent fix"}}):
        ok = await _start(client, governed, ["src/b.py"], **extra)
        assert ok.status_code == 201, (extra, ok.text)
        await client.patch(f"/api/sessions/{ok.json()['id']}", json={"status": "completed"},
                           headers={"X-ATS-Approval-Token": ok.headers["X-ATS-Approval-Token"]})


@pytest.mark.asyncio
async def test_a_declared_duplicate_is_redirected_to_the_original(
        client, tmp_path, monkeypatch, governed):
    _config(monkeypatch, tmp_path,
            f'[governance]\nrepositories = ["{governed}"]\n'
            f'[admission]\nmode = "enforce"\nenforce_repositories = ["{governed}"]\n')
    resp = await _start(client, governed, ["src/a.py"], ticket_id=3560,
                        admission={"kind": "duplicate", "duplicate_of": 3536})
    assert resp.status_code == 409
    assert "continue #3536" in resp.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_same_file_as_an_unfinished_branch_never_blocks_but_is_surfaced(
        client, tmp_path, monkeypatch, governed):
    _branch(governed, "fix/4101-parser", "src/a.py", "other change\n", "fix(#4101): parser")
    _config(monkeypatch, tmp_path,
            f'[governance]\nrepositories = ["{governed}"]\n'
            f'[admission]\nmode = "enforce"\nenforce_repositories = ["{governed}"]\n')
    resp = await _start(client, governed, ["src/a.py"], ticket_id=4200)
    assert resp.status_code == 201, resp.text
    surfaced = resp.json()["admission"]["surfaced"]["branches"]
    assert [b["branch"] for b in surfaced] == ["fix/4101-parser"]
    assert surfaced[0]["tickets"] == [4101]


@pytest.mark.asyncio
async def test_extend_scope_admits_an_autoregistered_session_once(
        client, db_session, governed):
    resp = await client.post("/api/sessions", json={
        "developer": "dev", "agent": "default", "scope": [], "repo_root": governed,
        "description": "auto-registered on SessionStart (UNSCOPED)"})
    sid = resp.json()["id"]
    first = await client.post("/api/locks", json={"session_id": sid, "pattern": "src/a.py",
                                                  "admission": {"kind": "new",
                                                                "reason": "new parser"}})
    assert first.status_code == 201, first.text
    assert first.json()["admission"]["classification"] == "new"
    second = await client.post("/api/locks", json={"session_id": sid, "pattern": "src/b.py"})
    assert second.status_code == 201 and second.json()["admission"] is None
    assert len(await _admissions(db_session)) == 1


@pytest.mark.asyncio
async def test_off_records_nothing(client, db_session, tmp_path, monkeypatch, governed):
    _config(monkeypatch, tmp_path,
            f'[governance]\nrepositories = ["{governed}"]\n[admission]\nmode = "off"\n')
    resp = await _start(client, governed, ["src/a.py"])
    assert resp.status_code == 201 and resp.json()["admission"] is None
    assert await _admissions(db_session) == []


# ── unfinished-branch census ───────────────────────────────────────────────

def test_census_reports_ticket_scope_and_real_conflict_state(tmp_path):
    root = _repo(tmp_path / "r")
    _branch(root, "fix/4101-clean", "src/b.py", "clean\n", "fix: b")
    _branch(root, "feat/clash", "src/a.py", "theirs\n", "feat(#4102): a")
    (tmp_path / "r" / "src" / "a.py").write_text("ours\n")
    _git(root, "commit", "-q", "-am", "main moves a")
    _branch(root, "docs-only", "docs/x.md", "d\n", "docs")
    # Squash-landed: the same patch is already on main.
    _branch(root, "fix/4103-landed", "src/b.py", "landed\n", "fix: landed")
    (tmp_path / "r" / "src" / "b.py").write_text("landed\n")
    _git(root, "commit", "-q", "-am", "squash of fix/4103")

    found = {b.branch: b for b in unfinished_branches(root, scope=["src/**"])}
    assert set(found) == {"fix/4101-clean", "feat/clash"}
    assert found["fix/4101-clean"].tickets == [4101]
    assert found["feat/clash"].tickets == [4102]
    assert found["feat/clash"].conflict == "conflicts_with_default"
    assert found["feat/clash"].conflict_files == ["src/a.py"]
    assert found["fix/4101-clean"].files_in_scope == ["src/b.py"]

    by_ticket = unfinished_branches(root, tickets=[4102])
    assert [b.branch for b in by_ticket] == ["feat/clash"]
    assert unfinished_branches(root) == []  # no scope, no tickets: nothing, not everything


def test_census_reads_a_lifecycle_lock_owner(tmp_path):
    root = _repo(tmp_path / "r")
    _branch(root, "fix/4101-x", "src/a.py", "x\n", "x")
    wt = tmp_path / "wt"
    _git(root, "worktree", "add", "-q", str(wt), "fix/4101-x")
    _git(root, "worktree", "lock", "--reason", "ats session=abcdef12-0000 task=4101", str(wt))
    [b] = unfinished_branches(root, tickets=[4101])
    assert b.worktree == str(wt.resolve()) and b.worktree_session == "abcdef12-0000"


# ── the brief: every named ticket, advisory overlaps, owners ───────────────

@pytest.mark.asyncio
async def test_brief_carries_every_named_ticket_and_labels_overlaps_advisory(
        client, db_session, governed, monkeypatch):
    envs = {7: {"id": 7, "title": "seven", "status": "pending", "gate": "decision",
                "description": "seven's acceptance", "task_context": {"x": 1}},
            8: {"id": 8, "title": "eight", "status": "in_progress", "gate": "none",
                "description": "eight's acceptance", "task_context": {"x": 1}}}
    monkeypatch.setattr(briefs, "fetch_tower_task_data",
                        lambda tid, **kw: (envs.get(int(tid), {}),
                                           None if int(tid) in envs else "not found"))
    monkeypatch.setattr(briefs, "fetch_task_overlaps", lambda objective, **kw: (
        [{"id": 9, "title": "nine", "status": "pending", "gate": "decision",
          "matched_terms": ["parser"], "operator_rulings": [{"prohibition": True}]}], None))
    owner = await _start(client, governed, ["src/b.py"], ticket_id=9)
    assert owner.status_code == 201

    packet = await briefs.build_brief(db_session, objective="parser work", repo_root=governed,
                                      recall=False, resolve_task=False, task_ids=[7, 8, 404],
                                      overlaps=True)
    text = packet["rendered"]
    assert packet["task_id"] is None  # nothing was selected
    assert "#7 [pending, gate=decision] seven" in text and "#8 [in_progress" in text
    assert "#404 UNAVAILABLE: not found" in text  # never silently dropped
    assert "ADVISORY" in text and "#9 [pending, gate=decision] nine" in text
    assert f"live owner: default session {owner.json()['id']}" in text
    assert "PROHIBITION" in text


def test_an_echo_without_overlap_mode_is_unavailable_not_empty(monkeypatch):
    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"status": "resolved", "task_id": 1, "candidates": []}

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return Resp()

    monkeypatch.setattr("httpx.Client", Client)
    found, error = _REAL_FETCH_TASK_OVERLAPS("x y", exclude_ids=[])
    assert found == [] and "not supported" in error


# ── the prompt hook ────────────────────────────────────────────────────────

async def _placeholder(client, tmp_path, monkeypatch, cid):
    from ai_team_sync.hooks import session_autostart

    monkeypatch.setenv("ATS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", cid)
    monkeypatch.setenv("ATS_DEVELOPER", "dev")
    monkeypatch.chdir(tmp_path)
    sid = await session_autostart.ensure_session("http://test", client)
    assert sid and (await client.get(f"/api/sessions/{sid}")).json()["repo_root"] == ""
    return sid


@pytest.mark.asyncio
async def test_an_unanchored_handback_neither_anchors_nor_briefs(
        client, tmp_path, monkeypatch, governed):
    from ai_team_sync.hooks import ats_context

    cid = "aaaa1111-1111-2222-3333-444455556666"
    sid = await _placeholder(client, tmp_path, monkeypatch, cid)
    prompt = ('<agent-message from="x">\n[Subagent hand-back] I reviewed '
              f"{governed.rsplit('/', 1)[-1]} and changed nothing.")
    note = await ats_context.resolve_prompt_context(
        "http://test", client, {"session_id": cid, "cwd": str(tmp_path), "prompt": prompt})
    assert note is None
    assert (await client.get(f"/api/sessions/{sid}")).json()["repo_root"] == ""


@pytest.mark.asyncio
async def test_a_context_repository_is_briefed_but_never_anchored(
        client, tmp_path, monkeypatch):
    from ai_team_sync.hooks import ats_context

    ctx = _repo(tmp_path / "coord-tool")
    _config(monkeypatch, tmp_path, f'[context]\nrepositories = ["{ctx}"]\n')
    cid = "bbbb1111-1111-2222-3333-444455556666"
    sid = await _placeholder(client, tmp_path, monkeypatch, cid)
    note = await ats_context.resolve_prompt_context(
        "http://test", client,
        {"session_id": cid, "cwd": ctx, "prompt": "status of the console work"})
    assert note is not None and "context only: not governed, session not anchored" in note
    assert (await client.get(f"/api/sessions/{sid}")).json()["repo_root"] == ""


@pytest.mark.asyncio
async def test_a_multi_ticket_prompt_briefs_every_ticket(client, tmp_path, monkeypatch):
    from ai_team_sync.hooks import ats_context

    monkeypatch.setattr(briefs, "fetch_tower_task_data", lambda tid, **kw: (
        {"id": int(tid), "title": f"t{tid}", "status": "pending", "gate": "none",
         "description": f"acceptance of {tid}", "task_context": {"x": 1}}, None))
    cid = "cccc1111-1111-2222-3333-444455556666"
    await _placeholder(client, tmp_path, monkeypatch, cid)
    note = await ats_context.resolve_prompt_context(
        "http://test", client,
        {"session_id": cid, "cwd": str(tmp_path),
         "prompt": "reconcile ticket #3536 against #3560"})
    assert "trigger: explicit_tasks" in note
    assert "#3536 [pending" in note and "#3560 [pending" in note


def _run_hook(monkeypatch, prompt):
    from ai_team_sync.hooks import ats_context

    async def fail(*args, **kwargs):
        raise ats_context.ContextResolutionError("ATS is down")

    monkeypatch.setattr(ats_context, "resolve_prompt_context", fail)
    monkeypatch.setattr(ats_context.sys, "stdin", __import__("io").StringIO(
        json.dumps({"session_id": "fresh", "cwd": "/nowhere", "prompt": prompt})))
    with pytest.raises(SystemExit) as exc:
        ats_context.main([])
    return exc.value.code


def test_added_context_never_blocks_when_ats_is_down(monkeypatch, capsys):
    assert _run_hook(monkeypatch, "reconcile ticket #3536 against #3560") == 0
    assert "continuing without it" in capsys.readouterr().err


def test_a_single_ticket_still_fails_closed_when_ats_is_down(monkeypatch, capsys):
    assert _run_hook(monkeypatch, "work on ticket #3536") == 2
    assert "ATS-FIRST context resolution failed" in capsys.readouterr().err


def test_an_unverified_delegated_child_blocks_even_on_a_multi_ticket_packet(monkeypatch, capsys):
    """Integration of #3591 with the delegated-child proof: best-effort context
    never applies to a delegation claim; an unconfirmed child stays blocked."""
    monkeypatch.setenv("ATS_DELEGATION", "dddd0000-0000-0000-0000-000000000000")
    assert _run_hook(monkeypatch, "VERIFY ticket #3536 and #3560 against #3517") == 2
    assert "ATS-FIRST context resolution failed" in capsys.readouterr().err
