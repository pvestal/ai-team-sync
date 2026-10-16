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
        select(Decision).where(Decision.record_kind == admission.ADMISSION_RECORD_KIND))
    ).scalars().all()
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


# ── the reaper: an admission record never keeps a session alive ───────────

async def _stale_admitted_session(client, db_session, governed):
    from datetime import datetime, timedelta, timezone

    from ai_team_sync.config import settings
    from ai_team_sync.models import ScopeLock, Session

    resp = await _start(client, governed, ["src/a.py"], ticket_id=4300)
    assert resp.status_code == 201 and resp.json()["admission"] is not None
    sid = resp.json()["id"]
    old = datetime.now(timezone.utc) - timedelta(hours=settings.session_inactivity_hours + 24)
    sess = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    sess.started_at = old
    sess.last_heartbeat = None
    for lock in (await db_session.execute(
            select(ScopeLock).where(ScopeLock.session_id == sid))).scalars().all():
        lock.created_at = old
    await db_session.commit()
    return sid


@pytest.mark.asyncio
async def test_a_fresh_admission_record_does_not_keep_a_stale_session_alive(
        client, db_session, governed):
    from ai_team_sync.background_tasks import auto_complete_stale_sessions
    from ai_team_sync.models import Session

    sid = await _stale_admitted_session(client, db_session, governed)
    assert len(await _admissions(db_session)) == 1  # recorded just now
    assert await auto_complete_stale_sessions(db_session) == 1
    db_session.expire_all()
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    assert row.status == "completed" and row.auto_completed


@pytest.mark.asyncio
async def test_an_ordinary_fresh_decision_still_counts_as_activity(client, db_session, governed):
    """Control: the exclusion is the admission record only, not decisions."""
    from ai_team_sync.background_tasks import auto_complete_stale_sessions

    sid = await _stale_admitted_session(client, db_session, governed)
    db_session.add(Decision(session_id=sid, ticket_id=4300, title="chose a parser",
                            chosen="ast", reasoning="", files="[]"))
    await db_session.commit()
    assert await auto_complete_stale_sessions(db_session) == 0


# ── dispositions are claims; admission records contradictions ─────────────

def _claim(*dispositions, ticket_kind="new"):
    return AdmissionClaim(kind=ticket_kind, reason="r", dispositions=[
        {"ref": r, "disposition": d, "evidence": "evidence text"} for r, d in dispositions])


SURFACED = {
    "overlaps": [{"id": 3536, "status": "pending"}, {"id": 3537, "status": "completed"}],
    "branches": [{"branch": "fix/3536-hook", "tickets": [3536], "conflict": "merges_cleanly",
                  "files_in_scope": ["src/hooks/ctx.py"]}],
}


def test_every_contradiction_rule_is_observed_not_trusted():
    review = admission.cross_check(
        _claim(("#3536", "separate"), ("#3537", "duplicate"), ("#4999", "related")),
        ticket_id=None, surfaced=SURFACED)
    observed = {(c["ref"], c["claimed"]) for c in review["contested"]}
    assert ("#3536", "separate") in observed       # its branch changes files in this claim
    assert ("#3537", "duplicate") in observed      # a closed ticket cannot be continued silently
    assert review["unverifiable"] == ["#4999"]     # never surfaced, so nothing to check
    assert review["undispositioned"] == ["branch:fix/3536-hook"]

    review = admission.cross_check(_claim(("#3536", "superseded"), ("#3537", "continuation")),
                                   ticket_id=3591, surfaced=SURFACED)
    assert {c["claimed"] for c in review["contested"]} == {"superseded", "continuation"}


def test_a_consistent_disposition_is_not_contested():
    review = admission.cross_check(
        _claim(("#3536", "related"), ("branch:fix/3536-hook", "related"), ("#3537", "related")),
        ticket_id=None, surfaced=SURFACED)
    assert review["contested"] == [] and review["undispositioned"] == []


@pytest.mark.parametrize("ref", ["3536", "#0", "branch:", "fix/x"])
def test_a_disposition_must_name_a_ticket_or_branch(ref):
    with pytest.raises(Exception):
        AdmissionClaim(kind="new", reason="r",
                       dispositions=[{"ref": ref, "disposition": "related", "evidence": "evidence"}])


@pytest.mark.asyncio
async def test_dispositions_and_their_review_are_recorded_with_the_claim(
        client, db_session, governed):
    _branch(governed, "fix/3536-hook", "src/a.py", "other\n", "fix(#3536): hook")
    resp = await _start(client, governed, ["src/a.py"], admission={
        "kind": "new", "reason": "parser rework",
        "dispositions": [{"ref": "#3536", "disposition": "separate",
                          "evidence": "different function in the same file"}]})
    assert resp.status_code == 201, resp.text
    review = resp.json()["admission"]["review"]
    assert review["contested"] and review["contested"][0]["ref"] == "#3536"
    [record] = await _admissions(db_session)
    assert record["verdict"]["review"]["contested"][0]["claimed"] == "separate"


# ── an operator ruling surfaces with no similarity at all ──────────────────

@pytest.mark.asyncio
async def test_a_ruling_reaches_a_claim_through_code_branch_ticket_not_similarity(
        db_session, tmp_path, monkeypatch):
    root = _repo(tmp_path / "studio", files=("pkg/render_admission.py", "pkg/other.py"))
    _branch(root, "fix/3528-binding-change", "pkg/render_admission.py",
            "BINDING_B += option\n", "fix: binding change")
    ruling = ("#2945 remains authoritative: path A is separate from binding B. "
              "Do not merge the #3528 implementation.")
    envs = {3528: {"id": 3528, "title": "checkpoint debt", "status": "pending", "gate": "none",
                   "description": "unrelated wording", "task_context": {"operator_rulings": {
                       "current": [{"id": "d1", "effect": "BLOCK", "prohibition": True,
                                    "ruling": ruling}], "history": []}}}}
    monkeypatch.setattr(briefs, "fetch_tower_task_data", lambda tid, **kw: (
        envs.get(int(tid), {}), None if int(tid) in envs else "not found"))
    monkeypatch.setattr(briefs, "fetch_task_overlaps", lambda objective, **kw: ([], None))

    packet = await briefs.build_brief(
        db_session, objective="add an option to the renderer", repo_root=root,
        scope=["pkg/render_admission.py"], recall=False, resolve_task=False, overlaps=True)
    text = packet["rendered"]
    assert packet["overlaps"] == []  # similarity contributed nothing
    assert "fix/3528-binding-change (#3528)" in text
    assert "#3528 CURRENT OPERATOR RULING (PROHIBITION): #2945 remains authoritative" in text


@pytest.mark.asyncio
async def test_recommendation_prose_is_never_shown_as_a_ruling(db_session, tmp_path, monkeypatch):
    root = _repo(tmp_path / "studio", files=("pkg/render_admission.py",))
    _branch(root, "fix/3528-x", "pkg/render_admission.py", "x\n", "x")
    monkeypatch.setattr(briefs, "fetch_tower_task_data", lambda tid, **kw: (
        {"id": 3528, "title": "t", "status": "pending", "gate": "none", "description": "d",
         "recommendation": "OPERATOR RULING: do not merge", "task_context": {"x": 1}}, None))
    packet = await briefs.build_brief(db_session, objective="o", repo_root=root,
                                      scope=["pkg/render_admission.py"], recall=False,
                                      resolve_task=False)
    assert "CURRENT OPERATOR RULING" not in packet["rendered"]


@pytest.mark.parametrize("prompt,wanted", [
    ("go ahead", False),
    ("yes, ship it, looks good", False),
    ("Let exports follow a reference outline by adding an outline option to the primary "
     "binding in the admission module.", True),   # ~120 chars: a length cut would miss it
    ("fix the flaky parser test in the loader module today", True),
])
def test_cwd_only_prompts_get_overlaps_by_content_not_length(prompt, wanted):
    from ai_team_sync.context_resolution import RequestTarget
    from ai_team_sync.hooks.ats_context import _wants_overlaps

    assert _wants_overlaps(prompt, RequestTarget(None, "/r", "governed_cwd")) is wanted


# ── confirmation-round regressions: one per verified Codex blocker ─────────

@pytest.mark.asyncio
async def test_blocker_a_every_named_ticket_gets_context_none_silently_omitted(
        db_session, governed, monkeypatch):
    ids = list(range(4401, 4409))  # eight named tickets
    envs = {t: {"id": t, "title": f"title {t}", "status": "pending", "gate": "none",
                "description": f"acceptance of {t}", "task_context": {"operator_rulings": {
                    "current": [{"id": "r", "effect": "BLOCK", "prohibition": True,
                                 "ruling": f"ruling on {t}"}] if t == 4408 else []}}}
            for t in ids}
    monkeypatch.setattr(briefs, "fetch_tower_task_data",
                        lambda tid, **kw: (envs[int(tid)], None))
    packet = await briefs.build_brief(db_session, objective="x", repo_root=governed,
                                      recall=False, resolve_task=False, task_ids=ids)
    text = packet["rendered"]
    for t in ids:
        assert f"#{t} [pending, gate=none] title {t}" in text, t
    # The last ticket is past the full-card limit and still carries its ruling.
    assert "CURRENT OPERATOR RULING (PROHIBITION): ruling on 4408" in text
    assert "not expanded" not in text


@pytest.mark.parametrize("prompt", ["fix parser bug", "rename the loader", "bump timeout"])
def test_blocker_b_short_ticketless_work_still_runs_discovery(prompt):
    from ai_team_sync.context_resolution import RequestTarget
    from ai_team_sync.hooks.ats_context import _wants_overlaps

    for reason in ("governed_cwd", "context_cwd"):
        assert _wants_overlaps(prompt, RequestTarget(None, "/r", reason)) is True


@pytest.mark.asyncio
async def test_blocker_d_an_agent_titled_decision_is_still_activity(client, db_session, governed):
    """An agent cannot make an ordinary decision look like an admission record:
    the identity is server-set, not the title, and not a request field."""
    from ai_team_sync.background_tasks import auto_complete_stale_sessions

    sid = await _stale_admitted_session(client, db_session, governed)
    resp = await client.post("/api/decisions", json={
        "session_id": sid, "title": admission.ADMISSION_TITLE, "chosen": "ast",
        "record_kind": "work_admission"})  # not a request field: ignored
    assert resp.status_code == 201, resp.text
    assert await auto_complete_stale_sessions(db_session) == 0  # still live


@pytest.mark.asyncio
async def test_blocker_e_a_linked_worktree_inherits_its_repository_mode(
        client, tmp_path, monkeypatch, governed):
    worktree = str(tmp_path / "wt-feature")
    _git(governed, "worktree", "add", "-q", "-b", "feature", worktree)
    _config(monkeypatch, tmp_path,
            f'[governance]\nrepositories = ["{governed}"]\n'
            f'[admission]\nmode = "enforce"\nenforce_repositories = ["{governed}"]\n')
    assert admission_mode_for(worktree) == "enforce"
    refused = await _start(client, worktree, ["src/a.py"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == "admission_refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"kind": "duplicate"}, {"kind": "new", "reason": "  "},
                                 {"kind": "banana"}, "not an object"])
async def test_blocker_f_off_and_shadow_never_refuse_a_malformed_classification(
        client, db_session, tmp_path, monkeypatch, governed, bad):
    for mode in ("shadow", "off"):
        _config(monkeypatch, tmp_path,
                f'[governance]\nrepositories = ["{governed}"]\n[admission]\nmode = "{mode}"\n')
        resp = await _start(client, governed, ["src/a.py"], ticket_id=4500, admission=bad)
        assert resp.status_code == 201, (mode, resp.text)
        verdict = resp.json()["admission"]
        if mode == "shadow":  # recorded as malformed, admitted
            assert verdict["invalid"] and not verdict["refused"]
            assert any(r["verdict"].get("invalid") for r in await _admissions(db_session))
        else:
            assert verdict is None
        await client.patch(f"/api/sessions/{resp.json()['id']}", json={"status": "completed"},
                           headers={"X-ATS-Approval-Token": resp.headers["X-ATS-Approval-Token"]})
    _config(monkeypatch, tmp_path,
            f'[governance]\nrepositories = ["{governed}"]\n'
            f'[admission]\nmode = "enforce"\nenforce_repositories = ["{governed}"]\n')
    enforced = await _start(client, governed, ["src/a.py"], ticket_id=4500, admission=bad)
    assert enforced.status_code == 422 and enforced.json()["detail"]["error"] == "admission_invalid"


# ── one admission contract through MCP and the API (final confirmation finding) ──

_ABSENT = object()
_VALUES = {"absent": _ABSENT, "null": None, "empty": {}, "malformed": {"kind": "banana"},
           "valid": {"kind": "new", "reason": "nothing covers this"}}
# (admitted?, classification, invalid?, claim_input) per mode, ticketed claim (#4101).
_EXPECTED = {
    "off": {v: (True, None, False, None) for v in _VALUES},
    "shadow": {"absent": (True, "continue", False, "absent"),
               "null": (True, "continue", False, "null"),
               "empty": (True, "continue", True, "provided"),
               "malformed": (True, "continue", True, "provided"),
               "valid": (True, "new", False, "provided")},
    "enforce": {"absent": (True, "continue", False, "absent"),
                "null": (True, "continue", False, "null"),
                "empty": (False, None, None, None),
                "malformed": (False, None, None, None),
                "valid": (True, "new", False, "provided")},
}


def _bridge_mcp(client, monkeypatch, tmp_path):
    """The MCP handlers' HTTP client, routed into the same in-process app."""
    import uuid

    import ai_team_sync.mcp.server as mcp
    from httpx import AsyncClient

    class _ASGIClient:
        def __init__(self, *a, **k):
            self._c = AsyncClient(transport=client._transport, base_url="http://localhost:8400")

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *exc):
            await self._c.aclose()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _ASGIClient)
    # Each case is a fresh MCP process: no session left in this module's pointer.
    monkeypatch.setattr(mcp, "_IN_PROCESS_SESSION_ID", None)
    monkeypatch.setattr(mcp, "_IN_PROCESS_APPROVAL_TOKEN", None)
    # A ticketed MCP start_session first fetches the exact task authority (fail
    # closed); answer it with the suite's valid envelope for ticket 4101.
    from tests.test_task_brief import TASK_ENVELOPE
    monkeypatch.setattr(briefs, "fetch_tower_task_data", lambda tid, **kw: (TASK_ENVELOPE, None))
    monkeypatch.setattr(briefs, "recall_memories", lambda *a, **kw: [])
    state = tmp_path / f"mcp-state-{uuid.uuid4().hex[:6]}"
    state.mkdir()
    monkeypatch.setenv("ATS_STATE_DIR", str(state))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", str(uuid.uuid4()))
    return mcp


async def _verdict_for(db_session, sid):
    db_session.expire_all()
    row = (await db_session.execute(
        select(Decision).where(Decision.session_id == sid)
        .where(Decision.record_kind == admission.ADMISSION_RECORD_KIND))).scalars().first()
    return json.loads(row.reasoning)["verdict"] if row else None


async def _via_api(client, door, governed, value):
    if door == "start_session":
        body = {"developer": "dev", "agent": "default", "scope": ["src/a.py"], "auto_lock": True,
                "repo_root": governed, "description": "parser", "ticket_id": 4101}
        if value is not _ABSENT:
            body["admission"] = value
        r = await client.post("/api/sessions", json=body)
        return r.status_code in (200, 201), (r.json().get("id") if r.status_code == 201 else None)
    s = await client.post("/api/sessions", json={
        "developer": "dev", "agent": "default", "scope": [], "repo_root": governed,
        "description": "auto-registered on SessionStart (UNSCOPED)", "ticket_id": 4101})
    body = {"session_id": s.json()["id"], "pattern": "src/a.py"}
    if value is not _ABSENT:
        body["admission"] = value
    r = await client.post("/api/locks", json=body)
    return r.status_code in (200, 201), s.json()["id"]


async def _via_mcp(mcp, client, monkeypatch, door, governed, value):
    args = {"description": "parser", "scope": ["src/a.py"], "repo_root": governed,
            "ticket_id": 4101} if door == "start_session" else {"patterns": ["src/a.py"]}
    if door == "extend_scope":
        s = await client.post("/api/sessions", json={
            "developer": "dev", "agent": "default", "scope": [], "repo_root": governed,
            "description": "auto-registered on SessionStart (UNSCOPED)", "ticket_id": 4101})
        sid = s.json()["id"]
        monkeypatch.setattr(mcp, "load_session_id", lambda: sid)
    if value is not _ABSENT:
        args["admission"] = value
    text = (await mcp.call_tool(door, args))[0].text
    if door == "start_session":
        admitted = "Session started" in text
        sid = text.split("Session ID: ")[1].split()[0] if admitted else None
    else:
        admitted = "Scope extended" in text
    return admitted, sid, text


@pytest.mark.asyncio
async def test_codex_finding_an_explicit_empty_admission_via_mcp_is_not_absent(
        client, tmp_path, monkeypatch, governed):
    """The exact finding: enforce mode, a ticketed MCP start_session carrying {} was
    admitted as an implicit continue while the API answered 422."""
    _config(monkeypatch, tmp_path,
            f'[governance]\nrepositories = ["{governed}"]\n'
            f'[admission]\nmode = "enforce"\nenforce_repositories = ["{governed}"]\n')
    api_ok, _ = await _via_api(client, "start_session", governed, {})
    mcp = _bridge_mcp(client, monkeypatch, tmp_path)
    mcp_ok, _, text = await _via_mcp(mcp, client, monkeypatch, "start_session", governed, {})
    assert api_ok is False
    assert mcp_ok is False, text
    assert "work admission" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["start_session", "extend_scope"])
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("name", list(_VALUES))
async def test_mcp_and_api_judge_one_admission_contract(
        client, db_session, tmp_path, monkeypatch, governed, door, mode, name):
    admission_table = f'\n[admission]\nmode = "{mode}"\n' + (
        f'enforce_repositories = ["{governed}"]\n' if mode == "enforce" else "")
    _config(monkeypatch, tmp_path, f'[governance]\nrepositories = ["{governed}"]\n'
            + admission_table)
    value = _VALUES[name]
    admitted_exp, cls_exp, invalid_exp, input_exp = _EXPECTED[mode][name]

    outcomes = {}
    api_ok, api_sid = await _via_api(client, door, governed, value)
    outcomes["api"] = (api_ok, await _verdict_for(db_session, api_sid) if api_ok else None)
    if api_ok and door == "start_session":  # free the file for the MCP twin
        from ai_team_sync.models import Session
        db_session.expire_all()
        row = (await db_session.execute(select(Session).where(Session.id == api_sid))).scalar_one()
        row.status = "completed"
        await db_session.commit()
    elif api_ok:
        from ai_team_sync.models import ScopeLock
        for lock in (await db_session.execute(select(ScopeLock).where(
                ScopeLock.session_id == api_sid))).scalars().all():
            await db_session.delete(lock)
        await db_session.commit()
    mcp = _bridge_mcp(client, monkeypatch, tmp_path)
    mcp_ok, mcp_sid, text = await _via_mcp(mcp, client, monkeypatch, door, governed, value)
    outcomes["mcp"] = (mcp_ok, await _verdict_for(db_session, mcp_sid) if mcp_ok else None)

    for path, (ok, verdict) in outcomes.items():
        assert ok is admitted_exp, (path, name, mode, door, text if path == "mcp" else "")
        if not ok or cls_exp is None:
            assert verdict is None, (path, verdict)
            continue
        assert verdict["classification"] == cls_exp, (path, verdict)
        assert bool(verdict["invalid"]) is invalid_exp, (path, verdict)
        assert verdict["claim_input"] == input_exp, (path, verdict)
        assert not verdict["refused"]
