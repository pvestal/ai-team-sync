"""#3589 claim verification: each rule that decides whether a claim can be trusted.

Every test builds its own small world (git repo, transcripts, ATS file) in a
temporary directory; nothing reads live state.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from ai_team_sync.console import evidence as ev
from ai_team_sync.console.evidence import DeployTarget, EvidenceConfig
from ai_team_sync.console.verify import (
    CONTRADICTED,
    OBSERVED,
    SELF_REPORTED,
    STALE,
    UNVERIFIED,
    VERIFIED,
    Claim,
    Evidence,
    TicketVerifier,
    acceptance_criteria,
    claimed_counts,
    decide,
)
from ai_team_sync.models import Base

T0 = datetime(2026, 10, 15, 0, 40, 0, tzinfo=timezone.utc)


def _claim(*items: Evidence, missing: list[str] | None = None) -> Claim:
    claim = Claim(
        id="c", kind="k", title="t", statement="s", claimant="a", made_at=None, source_record="r"
    )
    claim.add(*items)
    claim.missing = list(missing or [])
    return decide(claim)


def _e(kind: str, relation: str, admission: bool = False) -> Evidence:
    return Evidence(
        source="x", kind=kind, relation=relation, summary=f"{kind}/{relation}", admission=admission
    )


# ----------------------------------------------------------------- verdict rule


def test_self_reported_support_never_verifies():
    assert _claim(_e(SELF_REPORTED, "supports")).verdict == UNVERIFIED
    assert _claim(_e(OBSERVED, "supports")).verdict == VERIFIED


def test_verdict_precedence_contradicted_then_stale_then_verified():
    assert _claim(_e(OBSERVED, "supports"), _e(OBSERVED, "contradicts")).verdict == CONTRADICTED
    assert _claim(_e(OBSERVED, "supports"), _e(OBSERVED, "stale")).verdict == STALE
    assert _claim(_e(OBSERVED, "stale"), _e(OBSERVED, "contradicts")).verdict == CONTRADICTED


def test_self_report_contradicts_only_as_an_admission():
    assert _claim(_e(SELF_REPORTED, "contradicts")).verdict == UNVERIFIED
    assert _claim(_e(SELF_REPORTED, "contradicts", admission=True)).verdict == CONTRADICTED


def test_unverified_basis_names_what_is_missing():
    claim = _claim(_e(OBSERVED, "context"), missing=["no transcript bound"])
    assert claim.verdict == UNVERIFIED
    assert "no transcript bound" in claim.basis


# ----------------------------------------------------------------- command classes


@pytest.mark.parametrize(
    "command,invokes,display",
    [
        ("env -u X ../../venv/bin/pytest -q tests/test_a.py", True, False),
        ("python3 -m pytest tests", True, False),
        ("cat $SP/runs/meta.txt; grep -E '^FAILED|pytest' $SP/runs/out.txt", False, True),
        ("echo 'run pytest later'", False, True),
        ("D=/tmp/x; sleep 3; tail -5 $D/pytest.log", False, False),
    ],
)
def test_pytest_invocation_and_display_only(command, invokes, display):
    assert ev.invokes_pytest(command) is invokes
    assert ev.display_only(command) is display


# ----------------------------------------------------------------- codex binding


def _rollout(path: Path, packet: str, calls: list[tuple[str, str]], final: str, model="gpt-x"):
    rows = [
        {
            "type": "session_meta",
            "timestamp": (T0 + timedelta(seconds=2)).isoformat(),
            "payload": {
                "timestamp": (T0 + timedelta(seconds=2)).isoformat(),
                "cli_version": "0.1",
                "cwd": "/repo",
                "originator": "codex_exec",
            },
        },
        {"type": "turn_context", "payload": {"model": model}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": packet}],
            },
        },
    ]
    for index, (call_input, output) in enumerate(calls):
        rows.append(
            {
                "type": "response_item",
                "timestamp": (T0 + timedelta(seconds=10 + index)).isoformat(),
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": f"c{index}",
                    "name": "exec",
                    "input": call_input,
                },
            }
        )
        rows.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": f"c{index}",
                    "output": [{"type": "input_text", "text": output}],
                },
            }
        )
    rows.append(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": final}],
            },
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


DELEG = "11111111-2222-4333-8444-555555555555"
BASE = "1234567890abcdef1234567890abcdef12345678"
HEAD = "abcdef1234567890abcdef1234567890abcdef12"


def test_transcript_binds_only_by_its_own_packet(tmp_path):
    day = tmp_path / ".codex/sessions" / f"{T0.astimezone():%Y/%m/%d}"
    _rollout(
        day / "rollout-a.jsonl",
        f"DELEGATED TASK\ndelegation: {DELEG}\nparent task: 1001",
        [],
        "PASS",
    )
    # A later session that merely mentions the id is not a binding.
    _rollout(
        day / "rollout-b.jsonl",
        f"DELEGATED TASK\ndelegation: {'0' * 8}-0000-0000-0000-{'0' * 12}",
        [(f'const r = "{DELEG}"', "")],
        "PASS",
    )
    found = ev.find_codex_transcript(str(tmp_path), DELEG, T0, T0 + timedelta(minutes=10))
    assert [Path(p).name for p in found] == ["rollout-a.jsonl"]


def test_transcript_extracts_model_commands_ranges_and_exit_codes(tmp_path):
    path = tmp_path / "r.jsonl"
    _rollout(
        path,
        f"delegation: {DELEG}\nparent task: 1001",
        [
            (
                f'await tools.exec_command({{cmd: "git diff {BASE} {HEAD} -- src/x.py"}})',
                '{"exit_code":0}',
            ),
            (
                'await tools.exec_command({cmd: "../../venv/bin/pytest -q tests/test_x.py"})',
                '{"exit_code":0} 12 passed, 21 skipped in 0.18s',
            ),
        ],
        "Verdict: PASS. No blocking findings.",
        model="model-x",
    )
    t = ev.read_codex_transcript(str(path))
    assert t.delegation_id == DELEG and t.packet_task == "1001"
    assert t.model == "model-x" and t.originator == "codex_exec"
    assert t.runs[0].ranges == sorted([BASE, HEAD])
    assert t.runs[1].is_test and t.runs[1].exit_codes == [0]
    assert t.runs[1].summaries == ["12 passed, 21 skipped"]


# ----------------------------------------------------------------- verifier fixtures


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    return path


def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"change {name}")
    return _git(repo, "rev-parse", "HEAD")


def _verifier(tmp_path, *, delegations=(), sessions=(), verified_by=None, task=None, targets=()):
    verifier = object.__new__(TicketVerifier)
    verifier.ticket_id = 1001
    verifier.config = EvidenceConfig(
        ats_db=str(tmp_path / "none.db"), home=str(tmp_path), targets=tuple(targets)
    )
    verifier.ats = {
        "sessions": list(sessions),
        "delegations": list(delegations),
        "handoffs": [],
        "decisions": [],
        "commits": [],
        "file_activity": [],
        "restarts": [],
    }
    verifier.sessions = {row["id"]: row for row in sessions}
    verifier.task_state = "OBSERVED"
    verifier.task = task or {}
    verifier.verified_by = verified_by or {}
    verifier.codex = {}
    verifier._claude_runs = []
    return verifier


def test_checkout_deploy_claim_verified_then_stale_then_contradicted(tmp_path, repo, monkeypatch):
    first = _commit(repo, "a.py", "1")
    monkeypatch.setattr(
        ev, "http_json", lambda url, timeout=5.0: {"state": "OBSERVED", "status": 200, "body": {}}
    )
    target = DeployTarget(repo=str(repo), units=(), probe_url="http://127.0.0.1:1/health")
    spec = {"app": {"sha": first, "repo": str(repo), "commits": [first[:8]]}}
    v = _verifier(tmp_path, verified_by={"deployed": spec}, targets=[target])
    assert decide(v.deployment_claims()[0]).verdict == VERIFIED

    _commit(repo, "a.py", "2")
    claim = decide(v.deployment_claims()[0])
    assert claim.verdict == STALE
    assert "moved past" in claim.basis

    spec["app"]["sha"] = "f" * 40
    assert decide(v.deployment_claims()[0]).verdict == CONTRADICTED


def test_review_coverage_is_stale_when_code_changed_after_the_review(tmp_path, repo):
    base = _commit(repo, "a.py", "1")
    reviewed = _commit(repo, "a.py", "2")
    deployed = _commit(repo, "m.sql", "rename only")
    deleg = {
        "id": DELEG,
        "structured_verdict": json.dumps(
            {
                "verdict_code": "PASS",
                "reviewed": {"repo": str(repo), "base_sha": base, "head_sha": reviewed},
            }
        ),
        "verdict_code": "PASS",
    }
    spec = {"app": {"sha": deployed, "repo": str(repo)}}
    v = _verifier(tmp_path, delegations=[deleg], verified_by={"deployed": spec})
    reviewed_ok = {DELEG: VERIFIED}
    claim = decide(v.coverage_claims(reviewed_ok)[0])
    assert claim.verdict == STALE
    stale = next(e for e in claim.evidence if e.relation == "stale")
    assert "m.sql" in stale.detail["diff"]

    spec["app"]["sha"] = reviewed
    assert decide(v.coverage_claims(reviewed_ok)[0]).verdict == VERIFIED
    # The same passing review, if its own claim is unverified, proves neither state.
    assert decide(v.coverage_claims({DELEG: UNVERIFIED})[0]).verdict == UNVERIFIED


def _review_world(tmp_path, repo, final: str, *, base=None, head=None, calls=None):
    base = base or _commit(repo, "a.py", "1")
    head = head or _commit(repo, "a.py", "2")
    day = tmp_path / ".codex/sessions" / f"{T0.astimezone():%Y/%m/%d}"
    _rollout(
        day / "rollout-r.jsonl",
        f"delegation: {DELEG}\nparent task: 1001",
        calls
        or [(f'await tools.exec_command({{cmd: "git diff {base} {head}"}})', '{"exit_code":0}')],
        final,
    )
    deleg = {
        "id": DELEG,
        "delegated_worker": "codex",
        "mode": "VERIFY",
        "state": "closed",
        "created_at": T0.isoformat(),
        "closed_at": (T0 + timedelta(minutes=5)).isoformat(),
        "verdict_code": "PASS",
        "structured_verdict": json.dumps(
            {
                "verdict_code": "PASS",
                "reason": "ok",
                "reviewed": {"repo": str(repo), "base_sha": base, "head_sha": head},
            }
        ),
    }
    return _verifier(tmp_path, delegations=[deleg])


def test_review_verified_when_reviewer_diffed_the_exact_range_and_said_so(tmp_path, repo):
    claim = decide(_review_world(tmp_path, repo, "Verdict: PASS.").review_claims()[0])
    assert claim.verdict == VERIFIED
    assert any("successful git inspection(s) of exactly" in e.summary for e in claim.evidence)


def test_review_contradicted_when_the_reviewers_own_last_word_differs(tmp_path, repo):
    claim = decide(_review_world(tmp_path, repo, "Verdict: BLOCK on N1.").review_claims()[0])
    assert claim.verdict == CONTRADICTED


def test_empty_delegation_result_is_an_admission_against_the_return(tmp_path):
    deleg = {
        "id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "parent_session_id": "p",
        "child_session_id": None,
        "delegated_worker": "codex",
        "delegating_worker": "claude-code",
        "mode": "VERIFY",
        "state": "returned",
        "resolved_binary": "/usr/bin/codex",
        "objective": "review",
        "result_summary": "(no result: )",
        "evidence": '{"exit_ok": true}',
        "created_at": T0.isoformat(),
        "closed_at": None,
    }
    claim = decide(_verifier(tmp_path, delegations=[deleg]).delegation_claims()[0])
    assert claim.verdict == CONTRADICTED
    assert "produced no output" in claim.basis


# ----------------------------------------------------------------- test claims


def test_claimed_counts_cover_every_number_stated():
    assert claimed_counts("branch 2F/1885P/43S vs main 2F/1857P/43S") == {
        ("failed", 2),
        ("passed", 1885),
        ("passed", 1857),
    }
    assert claimed_counts("44 passed on throwaway PG") == {("passed", 44)}
    assert claimed_counts("18/18 isolated") == {("passed", 18)}


def _run(command: str, summary: str, code: int = 0) -> ev.ToolRun:
    return ev.ToolRun(
        at=T0.isoformat(),
        commands=[command],
        exit_codes=[code],
        summaries=[summary],
        source="claude transcript",
        raw_ref="t#1",
    )


def test_test_claim_needs_every_count_from_an_actual_execution(tmp_path):
    v = _verifier(
        tmp_path,
        verified_by={
            "tests": {
                # The claim states the branch run's failures; omitting them would be a
                # failure the claim hides (round 3).
                "suite": "branch 2F/1885P vs main 1857P",
                "printed": "44 passed",
            }
        },
    )
    v._claude_runs = [
        _run("pytest -q", "2 failed, 1885 passed"),
        # The baseline count only appears as output an agent printed from a file.
        _run("cat runs/base/meta.txt", "1857 passed"),
        _run("cat runs/x.txt", "44 passed"),
    ]
    claims = {c.id: decide(c) for c in v.test_claims()}
    suite = claims["test:suite"]
    assert suite.verdict == UNVERIFIED
    assert any("1857 passed" in m for m in suite.missing)
    assert all(e.relation == "context" for e in suite.evidence)
    assert claims["test:printed"].verdict == UNVERIFIED

    v._claude_runs.append(_run("pytest -q tests/base", "1857 passed"))
    assert decide(v.test_claims()[0]).verdict == VERIFIED


# ----------------------------------------------------------------- completion / acceptance


def test_acceptance_criteria_parse_and_residual_admission(tmp_path):
    description = (
        "DEFECTS: x\n\nACCEPTANCE: unit tests cannot reach the live service; 188 archived "
        "records replayed; health exposes unconsumed/failed events."
    )
    assert acceptance_criteria(description) == [
        "unit tests cannot reach the live service",
        "188 archived records replayed",
        "health exposes unconsumed/failed events",
    ]
    v = _verifier(
        tmp_path,
        task={"status": "completed", "description": description},
        verified_by={
            "residuals": [
                "replay of 258 archived records not done (separate decision)",
                "#3556 archive containment and #3586 ATS CLI defect remain open separately",
            ]
        },
    )
    claim = decide(v.completion_claim()[0])
    assert claim.verdict == CONTRADICTED
    statuses = {r["criterion"]: r["status"] for r in claim.requirements}
    assert statuses["188 archived records replayed"] == CONTRADICTED
    assert statuses["unit tests cannot reach the live service"] == UNVERIFIED


def _claude_transcript(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_operator_acceptance_requires_a_typed_operator_turn(tmp_path):
    when = (T0 + timedelta(hours=1)).isoformat()
    main = tmp_path / ".claude/projects/p/c0ffee00-aaaa.jsonl"
    phrase = "#1001 ACCEPTED / RELEASE APPROVED"
    _claude_transcript(
        main,
        [
            # Injected and tool rows carry the phrase too; neither is the operator.
            {"type": "user", "isMeta": True, "timestamp": when, "message": {"content": phrase}},
            {
                "type": "user",
                "timestamp": when,
                "message": {"content": [{"type": "tool_result", "content": phrase}]},
            },
            {
                "type": "user",
                "timestamp": when,
                "message": {"content": f"<task-notification>{phrase}</task-notification>"},
            },
        ],
    )
    session = {"id": "s1", "agent": "claude-code:c0ffee00", "ticket_id": 1001}
    vb = {
        "accepted_by": f"operator decision 2026-10-15 '{phrase}'",
        "verified_at": (T0 + timedelta(hours=2)).isoformat(),
        "authority": {"source": "operator_mcp", "caller_uid": 1000},
    }
    v = _verifier(tmp_path, sessions=[session], verified_by=vb, task={"created_at": T0.isoformat()})
    assert decide(v.acceptance_claim()[0]).verdict == UNVERIFIED

    with main.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": when,
                    "uuid": "u1",
                    "message": {"content": f"OPERATOR DECISION -- {phrase}. Close it."},
                }
            )
            + "\n"
        )
    # Operator ruling 2026-10-15: never verified automatically. The typed turn is shown
    # as a candidate for the operator's own inspection; the verdict stays UNVERIFIED.
    claim = decide(v.acceptance_claim()[0])
    assert claim.verdict == UNVERIFIED
    candidate = next(e for e in claim.evidence if e.source == "claude transcript")
    assert candidate.relation == "context" and "candidate" in candidate.summary.lower()
    assert phrase in candidate.detail["excerpt"]
    stamp = next(e for e in claim.evidence if e.kind == SELF_REPORTED)
    assert "cannot tell" in stamp.summary


# ----------------------------------------------------------------- ATS file


def test_ats_records_attach_delegated_children_and_open_read_only(tmp_path):
    from sqlalchemy.orm import Session as OrmSession

    from ai_team_sync.models import Delegation, Session

    db = tmp_path / "ats.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    with OrmSession(engine) as orm:
        orm.add_all(
            [
                Session(
                    id="owner",
                    developer="o",
                    agent="claude-code:a",
                    scope="[]",
                    description="w",
                    status="completed",
                    started_at=T0,
                    ticket_id=1001,
                ),
                Session(
                    id="child",
                    developer="o",
                    agent="codex:delegate",
                    scope="[]",
                    description="r",
                    status="completed",
                    started_at=T0,
                ),
                Delegation(
                    id="d1",
                    parent_session_id="owner",
                    parent_task="1001",
                    delegating_worker="claude-code",
                    delegated_worker="codex",
                    mode="VERIFY",
                    repo_root="/r",
                    scope="[]",
                    objective="o",
                    acceptance="a",
                    prohibitions="[]",
                    child_session_id="child",
                    lease_expires_at=T0,
                    state="closed",
                    created_at=T0,
                ),
            ]
        )
        orm.commit()
    engine.dispose()
    records = ev.AtsRecords(str(db)).ticket(1001)
    assert {row["id"] for row in records["sessions"]} == {"owner", "child"}
    assert [d["id"] for d in records["delegations"]] == ["d1"]
    with pytest.raises(sqlite3.OperationalError):
        with ev.AtsRecords(str(db))._connect() as ro:
            ro.execute("DELETE FROM sessions")


# ----------------------------------------------------------------- browser view


def _serve(build):
    import threading
    from http.server import ThreadingHTTPServer

    from ai_team_sync.console.web import _ReportCache, make_handler

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_ReportCache(build)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_browser_view_is_get_only_and_serves_page_and_report():
    import httpx

    calls: list[int] = []

    def build(ticket: int) -> dict:
        calls.append(ticket)
        return {"ticket": {"id": ticket}, "claims": []}

    server, base = _serve(build)
    try:
        page = httpx.get(f"{base}/")
        assert page.status_code == 200 and "Claim verification" in page.text
        assert httpx.get(f"{base}/api/ticket/1001").json()["ticket"]["id"] == 1001
        httpx.get(f"{base}/api/ticket/1001")  # cached
        httpx.get(f"{base}/api/ticket/1001?refresh=1")
        assert calls == [1001, 1001]
        assert httpx.get(f"{base}/api/ticket/x").status_code == 404
        assert httpx.post(f"{base}/api/ticket/1001").status_code == 501
        assert httpx.delete(f"{base}/").status_code == 501
    finally:
        server.shutdown()


def test_browser_view_reports_a_build_failure_instead_of_a_blank_page():
    import httpx

    def build(ticket: int) -> dict:
        raise RuntimeError("ats.db unreadable")

    server, base = _serve(build)
    try:
        response = httpx.get(f"{base}/api/ticket/1")
        assert response.status_code == 500
        assert response.json() == {"error": "RuntimeError", "detail": "ats.db unreadable"}
    finally:
        server.shutdown()


# ----------------------------------------------------------------- Codex round-1 BLOCK findings


def test_r1_f1_no_verdict_is_assigned_outside_decide(tmp_path, monkeypatch):
    """Handoff edges used to compute VERIFIED inline in graph()."""
    import inspect
    import re as _re

    from ai_team_sync.console import verify as verify_module

    source = inspect.getsource(verify_module)
    decide_src = inspect.getsource(verify_module.decide)
    outside = source.replace(decide_src, "")
    assert not _re.search(r"\b(VERIFIED|CONTRADICTED|STALE|UNVERIFIED)[ \t]+if\b", outside)
    assert not _re.search(r"\.verdict\s*=(?!=)", outside)

    session = {"id": "src-session", "agent": "codex:delegate", "ticket_id": 1001}
    v = _verifier(tmp_path, sessions=[session])
    v.ats["handoffs"] = [
        {
            "id": "h1",
            "source_session_id": "src-session",
            "verdict": "PASS",
            "addressing_mode": "ticket",
            "delivery_history": "[]",
            "created_at": T0.isoformat(),
            "acknowledged_at": None,
            "recipient_session_id": None,
            "blockers": "[]",
            "next_steps": "[]",
        }
    ]
    event = f"ats.session.completed/v1/src-session/{T0.isoformat()}"
    v.ats["completion_events"] = [
        {"event_id": event, "session_id": "src-session", "handoff_id": "h1"}
    ]
    monkeypatch.setattr(
        v,
        "_intake_receipts",
        lambda sid: [
            {
                "stored": True,
                "job_status": "done",
                "finished_at": T0.isoformat(),
                "event_id": event,
            }
        ],
    )
    claims = [decide(c) for c in v.handoff_claims()]
    edge = next(e for e in v.graph({c.id: c for c in claims})["edges"] if e["kind"] == "handoff")
    assert edge["claim_id"] == "handoff:h1"
    assert edge["verdict"] == claims[0].verdict == VERIFIED


def test_r1_f2_review_without_a_bound_transcript_is_not_verified(tmp_path, repo):
    base = _commit(repo, "a.py", "1")
    head = _commit(repo, "a.py", "2")
    deleg = {
        "id": DELEG,
        "delegated_worker": "codex",
        "mode": "VERIFY",
        "state": "closed",
        "created_at": T0.isoformat(),
        "closed_at": (T0 + timedelta(minutes=5)).isoformat(),
        "verdict_code": "PASS",
        "structured_verdict": json.dumps(
            {
                "verdict_code": "PASS",
                "reviewed": {"repo": str(repo), "base_sha": base, "head_sha": head},
            }
        ),
    }
    claim = decide(_verifier(tmp_path, delegations=[deleg]).review_claims()[0])
    assert claim.verdict == UNVERIFIED


@pytest.mark.parametrize(
    "final,expected",
    [
        ("CHANGE: A\nFINDING: PASS\nCHANGE: B\nFINDING: BLOCK\nVERDICT = BLOCK", CONTRADICTED),
        ('Item 1 PASS\n```ats-verdict\n{"verdict_code":"BLOCK"}\n```', CONTRADICTED),
        ("Item 1 PASS. Item 2 looks fine.", UNVERIFIED),
        ("Item 1 BLOCK was withdrawn.\nVERDICT = PASS", VERIFIED),
    ],
)
def test_r1_f3_reviewer_verdict_is_the_overall_verdict_not_the_first_token(
    tmp_path, repo, final, expected
):
    claim = decide(_review_world(tmp_path, repo, final).review_claims()[0])
    assert claim.verdict == expected


def test_r1_f4_coverage_counts_only_passing_reviews(tmp_path, repo):
    base = _commit(repo, "a.py", "1")
    head = _commit(repo, "a.py", "2")
    deleg = {
        "id": DELEG,
        "verdict_code": "BLOCK",
        "structured_verdict": json.dumps(
            {
                "verdict_code": "BLOCK",
                "reviewed": {"repo": str(repo), "base_sha": base, "head_sha": head},
            }
        ),
    }
    v = _verifier(
        tmp_path,
        delegations=[deleg],
        verified_by={"deployed": {"svc": {"sha": head, "repo": str(repo)}}},
    )
    assert decide(v.coverage_claims()[0]).verdict != VERIFIED
    # Even a fully verified review cannot cover code it BLOCKED: that is the record
    # contradicting "passed independent review".
    claim = decide(v.coverage_claims({DELEG: VERIFIED})[0])
    assert claim.verdict == CONTRADICTED


def test_r1_f4_deployment_needs_runtime_evidence_not_commit_existence(tmp_path, repo):
    sha = _commit(repo, "a.py", "1")
    v = _verifier(
        tmp_path,
        verified_by={"deployed": {"svc": {"sha": sha, "repo": str(repo), "commits": [sha[:8]]}}},
    )
    claim = decide(v.deployment_claims()[0])
    assert claim.verdict == UNVERIFIED
    assert any("runtime" in m for m in claim.missing)


def test_r1_f5_only_the_initial_prompt_turn_can_bind_a_transcript(tmp_path):
    day = tmp_path / ".codex/sessions" / f"{T0.astimezone():%Y/%m/%d}"
    late = day / "rollout-late.jsonl"
    # The opening prompt names no delegation; one appears only after work began.
    _rollout(late, "Please review the repository.", [("echo hi", "")], "PASS")
    rows = [json.loads(line) for line in late.read_text().splitlines()]
    rows.insert(
        -1,
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"delegation: {DELEG}\nparent task: 1001"}
                ],
            },
        },
    )
    late.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    # Real rollouts open with AGENTS.md instructions before the packet; that is still the
    # initial prompt turn and must bind.
    real = day / "rollout-real.jsonl"
    _rollout(real, f"delegation: {DELEG}\nparent task: 1001", [], "PASS")
    rows = [json.loads(line) for line in real.read_text().splitlines()]
    rows.insert(
        2,
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}],
            },
        },
    )
    real.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    found = ev.find_codex_transcript(str(tmp_path), DELEG, T0, T0 + timedelta(minutes=10))
    assert [Path(p).name for p in found] == ["rollout-real.jsonl"]


def test_r1_f6_every_named_test_file_must_have_run(tmp_path):
    v = _verifier(
        tmp_path, verified_by={"tests": {"pair": "test_a.py 5 passed; test_b.py 5 passed"}}
    )
    v._claude_runs = [_run("pytest -q tests/test_a.py", "5 passed")]
    claim = decide(v.test_claims()[0])
    assert claim.verdict == UNVERIFIED
    assert any("test_b.py" in m for m in claim.missing)
    v._claude_runs.append(_run("pytest -q tests/test_b.py", "5 passed"))
    assert decide(v.test_claims()[0]).verdict == VERIFIED


def test_long_reviewer_message_keeps_its_overall_verdict(tmp_path, repo):
    """Found by running the checker on #3589's own reviews: real final messages run
    ~9 KB with the overall verdict at the END, and display truncation dropped it."""
    findings = "\n".join(
        f"CHANGE: item {i}\nFINDING: PASS\nEVIDENCE: {'x' * 120}" for i in range(60)
    )
    v = _review_world(tmp_path, repo, findings + "\nVERDICT = PASS")
    claim = decide(v.review_claims()[0])
    assert claim.verdict == VERIFIED
    shown = next(e for e in claim.evidence if "overall verdict" in e.summary)
    assert shown.detail["final_message"].rstrip().endswith("VERDICT = PASS")


# ----------------------------------------------------------------- Codex round-3 BLOCK findings


@pytest.mark.parametrize(
    "final,expected",
    [
        # A quoted example block early in the message is not the reviewer's verdict.
        (
            'Quoted example:\n```ats-verdict\n{"verdict_code":"PASS"}\n```\nVERDICT: BLOCK',
            "BLOCK",
        ),
        ('VERDICT: BLOCK\n\n```ats-verdict\n{"verdict_code":"BLOCK"}\n```', "BLOCK"),
        ('VERDICT: PASS\n\n```ats-verdict\n{"verdict_code":"PASS"}\n```', "PASS"),
        ('Round 1 said VERDICT: BLOCK.\n```ats-verdict\n{"verdict_code":"PASS"}\n```', "PASS"),
    ],
)
def test_r3_f1_the_last_stated_verdict_is_the_overall_verdict(final, expected):
    assert ev.overall_verdict(final) == expected


def _range_world(tmp_path, repo, call, output):
    base = _commit(repo, "a.py", "1")
    head = _commit(repo, "a.py", "2")
    v = _review_world(
        tmp_path,
        repo,
        "VERDICT: PASS",
        base=base,
        head=head,
        calls=[(call.replace("{base}", base).replace("{head}", head), output)],
    )
    return decide(v.review_claims()[0])


@pytest.mark.parametrize(
    "call,output,expected",
    [
        # Display-only: a command that merely prints both ids is not an inspection.
        ('await tools.exec_command({cmd: "printf {base} {head}"})', '{"exit_code":1}', UNVERIFIED),
        # A diff of exactly that range that FAILED inspected nothing.
        (
            'await tools.exec_command({cmd: "git diff {base} {head}"})',
            '{"exit_code":128}',
            UNVERIFIED,
        ),
        # No exit code recorded, but git's own diff output is in the result.
        (
            'await tools.exec_command({cmd: "git diff {base} {head} -- a.py"})',
            "Output:\ndiff --git a/a.py b/a.py\n@@ -1 +1 @@",
            VERIFIED,
        ),
        # No exit code and no git output: nothing shows the command succeeded.
        ('await tools.exec_command({cmd: "git diff {base} {head}"})', "Output:\n", UNVERIFIED),
        (
            'await tools.exec_command({cmd: "git log --oneline {base}..{head}"})',
            '{"exit_code":0}',
            VERIFIED,
        ),
    ],
)
def test_r3_f2_review_range_needs_a_successful_git_command_over_that_range(
    tmp_path, repo, call, output, expected
):
    assert _range_world(tmp_path, repo, call, output).verdict == expected


def test_r3_f2_review_range_in_a_different_repository_does_not_count(tmp_path, repo):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    call = (
        f'const wd="{other}";\n'
        'await tools.exec_command({cmd: "git diff {base} {head}", workdir: wd})'
    )
    claim = _range_world(tmp_path, repo, call, '{"exit_code":0}')
    assert claim.verdict == UNVERIFIED


def test_r3_f3_a_failing_run_cannot_verify_a_passing_claim(tmp_path):
    v = _verifier(tmp_path, verified_by={"tests": {"t": "tests PASS: 34 passed"}})
    v._claude_runs = [_run("pytest -q tests/test_a.py", "1 failed, 34 passed", code=1)]
    claim = decide(v.test_claims()[0])
    assert claim.verdict == CONTRADICTED
    assert "1 failed" in claim.basis
    # A claim that states the failures it had is consistent with that run.
    v.verified_by = {"tests": {"t": "12 failed, 1912 passed (pre-existing failures)"}}
    v._claude_runs = [_run("pytest -q", "12 failed, 1912 passed", code=1)]
    assert decide(v.test_claims()[0]).verdict == VERIFIED
    # Exit 1 with no failure in the summary is an unexplained failure, never support.
    v.verified_by = {"tests": {"t": "34 passed"}}
    v._claude_runs = [_run("pytest -q tests/test_a.py", "34 passed", code=1)]
    assert decide(v.test_claims()[0]).verdict != VERIFIED


def _delegation_world(tmp_path, calls, final):
    day = tmp_path / ".codex/sessions" / f"{T0.astimezone():%Y/%m/%d}"
    _rollout(day / "rollout-d.jsonl", f"delegation: {DELEG}\nparent task: 1001", calls, final)
    deleg = {
        "id": DELEG,
        "parent_session_id": "p",
        "child_session_id": None,
        "delegated_worker": "codex",
        "delegating_worker": "claude-code",
        "mode": "VERIFY",
        "state": "closed",
        "resolved_binary": "/usr/bin/codex",
        "objective": "prove behavior",
        "result_summary": "done",
        "evidence": "{}",
        "created_at": T0.isoformat(),
        "closed_at": (T0 + timedelta(minutes=5)).isoformat(),
    }
    return decide(_verifier(tmp_path, delegations=[deleg]).delegation_claims()[0])


def test_r3_f4_a_delegation_transcript_without_recorded_work_is_not_verified(tmp_path):
    claim = _delegation_world(tmp_path, [], "ok")
    assert claim.verdict == UNVERIFIED
    assert any("no recorded work" in m for m in claim.missing)


def test_r3_f4_a_delegation_that_ran_commands_and_answered_is_verified(tmp_path):
    claim = _delegation_world(
        tmp_path, [('await tools.exec_command({cmd: "git status"})', '{"exit_code":0}')], "PASS"
    )
    assert claim.verdict == VERIFIED
    assert "objective" in claim.meaning


def _install_world(tmp_path, repo, monkeypatch, *, state="active", pid="4242", probe=None):
    (repo / "src/pkg").mkdir(parents=True)
    (repo / "src/pkg/a.py").write_text("print(1)\n")
    (repo / "src/pkg/page.html").write_text("<p>v1</p>\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "pkg")
    sha = _git(repo, "rev-parse", "HEAD")
    install = tmp_path / "site/pkg"
    install.mkdir(parents=True)
    (install / "a.py").write_text("print(1)\n")
    (install / "page.html").write_text("<p>v1</p>\n")
    started = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(
        ev,
        "unit_started",
        lambda unit, user: {"unit": unit, "state": state, "main_pid": pid, "started_at": started},
    )
    body = (
        probe
        if probe is not None
        else {"commit": sha[:7], "pid": int(pid), "package_path": str(install)}
    )
    monkeypatch.setattr(
        ev, "http_json", lambda url, timeout=5.0: {"state": "OBSERVED", "status": 200, "body": body}
    )
    target = DeployTarget(
        repo=str(repo),
        units=(("svc", True),),
        install_dir=str(install),
        source_prefix="src/pkg",
        probe_url="http://127.0.0.1:1/api/version",
        probe_reports_commit=True,
    )
    v = _verifier(
        tmp_path,
        verified_by={"deployed": {"svc": {"sha": sha, "repo": str(repo)}}},
        targets=[target],
    )
    return v, install, sha


def test_r3_f5_deployment_verified_only_with_files_live_process_and_running_build(
    tmp_path, repo, monkeypatch
):
    v, install, sha = _install_world(tmp_path, repo, monkeypatch)
    claim = decide(v.deployment_claims()[0])
    assert claim.verdict == VERIFIED
    assert any(e.source == "running service" for e in claim.evidence)
    assert "behavior" in claim.meaning  # says plainly what it does not check


def test_r3_f5_an_inactive_service_contradicts_a_deployment(tmp_path, repo, monkeypatch):
    v, _, _ = _install_world(tmp_path, repo, monkeypatch, state="inactive", pid="0")
    assert decide(v.deployment_claims()[0]).verdict == CONTRADICTED


def test_r3_f5_non_python_package_files_are_compared(tmp_path, repo, monkeypatch):
    v, install, _ = _install_world(tmp_path, repo, monkeypatch)
    (install / "page.html").write_text("<p>stale</p>\n")
    claim = decide(v.deployment_claims()[0])
    assert claim.verdict == CONTRADICTED
    assert any("page.html" in str(e.detail) for e in claim.evidence)


def test_r3_f5_a_running_build_other_than_the_claim_is_not_verified(tmp_path, repo, monkeypatch):
    v, _, _ = _install_world(tmp_path, repo, monkeypatch, probe={"commit": "0000000"})
    assert decide(v.deployment_claims()[0]).verdict == CONTRADICTED


def test_r3_f5_without_a_runtime_probe_a_deployment_stays_unverified(tmp_path, repo, monkeypatch):
    v, _, _ = _install_world(tmp_path, repo, monkeypatch)
    v.config = EvidenceConfig(
        ats_db="x",
        home=str(tmp_path),
        targets=tuple(
            DeployTarget(t.repo, t.units, t.install_dir, t.source_prefix) for t in v.config.targets
        ),
    )
    claim = decide(v.deployment_claims()[0])
    assert claim.verdict == UNVERIFIED
    assert any("probe" in m for m in claim.missing)


def test_r3_f6_a_later_failing_run_contradicts_an_earlier_pass(tmp_path):
    v = _verifier(tmp_path, verified_by={"tests": {"t": "test_a.py 5 passed"}})
    old = _run("pytest -q tests/test_a.py", "5 passed")
    new = _run("pytest -q tests/test_a.py", "1 failed, 4 passed", code=1)
    new.at = (T0 + timedelta(minutes=1)).isoformat()
    v._claude_runs = [old, new]
    claim = decide(v.test_claims()[0])
    assert claim.verdict == CONTRADICTED
    assert not any(e.relation == "supports" for e in claim.evidence)
    # Fixed and re-run: the earlier failure is shown as history, the later pass verifies.
    old.at, new.at = new.at, old.at
    claim = decide(v.test_claims()[0])
    assert claim.verdict == VERIFIED
    assert any("earlier" in e.summary and "1 failed" in e.summary for e in claim.evidence)


def test_r3_f7_a_long_reviewer_message_is_shown_whole_or_visibly_cut(tmp_path):
    path = tmp_path / "r.jsonl"
    message = "FINDINGS-START\n" + "x" * 20000 + "\nVERDICT: PASS"
    _rollout(path, f"delegation: {DELEG}", [], message)
    t = ev.read_codex_transcript(str(path))
    assert t.final_message.startswith("FINDINGS-START")
    assert "characters omitted" in t.final_message
    assert t.final_message.rstrip().endswith("VERDICT: PASS")
    assert t.final_verdict == "PASS"
    _rollout(path, f"delegation: {DELEG}", [], "short\nVERDICT: PASS")
    assert ev.read_codex_transcript(str(path)).final_message == "short\nVERDICT: PASS"


def test_r3_f7_an_open_delegation_is_searched_up_to_now(tmp_path):
    created = datetime.now(timezone.utc) - timedelta(days=3)
    day = tmp_path / ".codex/sessions" / f"{datetime.now().astimezone():%Y/%m/%d}"
    _rollout(day / "rollout-open.jsonl", f"delegation: {DELEG}", [], "PASS")
    found = ev.find_codex_transcript(str(tmp_path), DELEG, created, None)
    assert [Path(p).name for p in found] == ["rollout-open.jsonl"]


def test_r3_checkout_freshness_uses_when_the_checkout_moved_not_the_commit_date(
    tmp_path, repo, monkeypatch
):
    """A commit written at 08:58 and merged at 09:02 is not running in a service
    started at 09:00, though that service started after the commit's own date."""
    _commit(repo, "a.py", "1")
    _git(repo, "checkout", "-q", "-b", "feat")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    (repo / "a.py").write_text("2")
    _git(repo, "add", "a.py")
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "late merge"],
        check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": old, "GIT_AUTHOR_DATE": old},
    )
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "merge", "-q", "--ff-only", "feat")  # HEAD moves now
    started = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    monkeypatch.setattr(
        ev,
        "unit_started",
        lambda unit, user: {
            "unit": unit,
            "state": "active",
            "main_pid": "7",
            "started_at": started,
        },
    )
    monkeypatch.setattr(
        ev, "http_json", lambda url, timeout=5.0: {"state": "OBSERVED", "status": 200, "body": {}}
    )
    target = DeployTarget(repo=str(repo), units=(("svc", False),), probe_url="http://x/health")
    v = _verifier(
        tmp_path,
        verified_by={"deployed": {"svc": {"sha": sha, "repo": str(repo)}}},
        targets=[target],
    )
    claim = decide(v.deployment_claims()[0])
    assert claim.verdict == STALE
    assert any("checkout moved" in e.summary for e in claim.evidence if e.relation == "stale")


def test_r3_every_shipped_non_python_file_is_declared_package_data():
    """pipx installs only declared package data; an undeclared page would be missing."""
    import tomllib

    root = Path(__file__).resolve().parents[1]
    declared = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["setuptools"][
        "package-data"
    ]
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "src/ai_team_sync"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    for rel in (p for p in tracked if not p.endswith(".py")):
        package, name = rel[len("src/") :].rsplit("/", 1)
        assert name in declared.get(package.replace("/", "."), []), rel


def test_r3_a_red_run_before_the_fix_and_an_unfinished_run_do_not_contradict(tmp_path):
    """Live #1001 'ats_fault_suite': red pre-fix (11 failed), then 5x 12 passed, then a
    reviewer's sandbox run that exited 4 before collecting anything."""
    v = _verifier(tmp_path, verified_by={"tests": {"t": "test_f.py 12 tests, red pre-fix"}})
    red = _run("pytest -q tests/test_f.py", "11 failed, 1 passed")
    green = _run("pytest -q tests/test_f.py", "12 passed")
    green.at = (T0 + timedelta(minutes=5)).isoformat()
    usage = _run("pytest -q tests/test_f.py", "", code=4)
    usage.summaries, usage.at = [], (T0 + timedelta(minutes=30)).isoformat()
    v._claude_runs = [red, green, usage]
    claim = decide(v.test_claims()[0])
    assert claim.verdict == UNVERIFIED  # states no count to match, and nothing contradicts it
    summaries = [e.summary for e in claim.evidence]
    assert any(s.startswith("An earlier run failed") and "11 failed" in s for s in summaries)
    assert any("did not complete" in s for s in summaries)
    # Exit 1 is pytest's "tests failed": after the last clean pass, it contradicts.
    usage.exit_codes = [1]
    assert decide(v.test_claims()[0]).verdict == CONTRADICTED


# ----------------------------------------------------------------- Codex round-4 BLOCK findings


@pytest.mark.parametrize(
    "call,output,expected",
    [
        # The reversed range head..base is a different (here empty) range.
        (
            'await tools.exec_command({cmd: "git log --oneline {head}..{base}"})',
            '{"exit_code":0}',
            UNVERIFIED,
        ),
        (
            'await tools.exec_command({cmd: "git diff {head} {base}"})',
            '{"exit_code":0}',
            UNVERIFIED,
        ),
        # `|| true` makes the shell's exit 0 even when git failed.
        (
            'await tools.exec_command({cmd: "git -C /nope diff {base} {head} || true"})',
            "{\"exit_code\":0} fatal: cannot change to '/nope': No such file or directory",
            UNVERIFIED,
        ),
        # A pipe reports the last command's exit, not git's: only git's own output counts.
        (
            'await tools.exec_command({cmd: "git diff {base} {head} | head -50"})',
            '{"exit_code":0}',
            UNVERIFIED,
        ),
        (
            'await tools.exec_command({cmd: "git diff {base} {head} | head -50"})',
            '{"exit_code":0} diff --git a/a.py b/a.py',
            VERIFIED,
        ),
    ],
)
def test_r4_f2_review_range_needs_direction_and_git_s_own_success(
    tmp_path, repo, call, output, expected
):
    assert _range_world(tmp_path, repo, call, output).verdict == expected


def _phased(path: Path, messages: list[tuple[str, str]]):
    """Replace a rollout's assistant message with phase-tagged ones (commentary/final_answer)."""
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows = [r for r in rows if (r.get("payload") or {}).get("role") != "assistant"]
    for phase, text in messages:
        rows.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": phase,
                    "content": [{"type": "output_text", "text": text}],
                },
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_r4_f4_only_the_final_answer_is_the_final_message(tmp_path):
    """Codex tags assistant messages commentary (mid-run) or final_answer; a reviewer
    that is still running has said nothing final, whatever its commentary mentions."""
    day = tmp_path / ".codex/sessions" / f"{T0.astimezone():%Y/%m/%d}"
    path = day / "rollout-d.jsonl"
    _rollout(
        path,
        f"delegation: {DELEG}\nparent task: 1001",
        [('await tools.exec_command({cmd: "git status"})', '{"exit_code":0}')],
        "x",
    )
    _phased(path, [("commentary", "Looking at the diff."), ("commentary", "VERDICT: PASS soon")])
    t = ev.read_codex_transcript(str(path))
    assert t.final_message == "" and t.final_verdict is None
    open_delegation = {
        "id": DELEG,
        "parent_session_id": "p",
        "child_session_id": None,
        "delegated_worker": "codex",
        "delegating_worker": "claude-code",
        "mode": "VERIFY",
        "state": "open",
        "resolved_binary": "/usr/bin/codex",
        "objective": "o",
        "result_summary": "",
        "evidence": "{}",
        "created_at": T0.isoformat(),
        "closed_at": None,
    }
    claim = decide(_verifier(tmp_path, delegations=[open_delegation]).delegation_claims()[0])
    assert claim.verdict == UNVERIFIED
    # Finished: the final_answer is the final message.
    _phased(
        path, [("commentary", "VERDICT: BLOCK draft"), ("final_answer", "Done.\nVERDICT: PASS")]
    )
    t = ev.read_codex_transcript(str(path))
    assert t.final_message == "Done.\nVERDICT: PASS" and t.final_verdict == "PASS"


def test_r4_f5_a_commit_reporting_probe_must_name_the_compared_install(tmp_path, repo, monkeypatch):
    """ATS /api/version names its commit AND the package it imports; without the path the
    answering process is not bound to the installed files compared above."""
    v, install, sha = _install_world(tmp_path, repo, monkeypatch)
    monkeypatch.setattr(
        ev,
        "http_json",
        lambda url, timeout=5.0: {
            "state": "OBSERVED",
            "status": 200,
            "body": {"commit": sha[:7], "pid": 4242},
        },
    )
    claim = decide(v.deployment_claims()[0])
    assert claim.verdict == UNVERIFIED
    assert any("package_path" in m for m in claim.missing)


def test_r4_f6_a_pass_of_one_file_does_not_clear_a_newer_failure_of_another(tmp_path):
    v = _verifier(tmp_path, verified_by={"tests": {"t": "test_a.py 5 passed; test_b.py 5 passed"}})
    b_old = _run("pytest -q tests/test_b.py", "5 passed")
    b_new = _run("pytest -q tests/test_b.py", "1 failed, 4 passed", code=1)
    b_new.at = (T0 + timedelta(minutes=1)).isoformat()
    a = _run("pytest -q tests/test_a.py", "5 passed")
    a.at = (T0 + timedelta(minutes=2)).isoformat()
    v._claude_runs = [b_old, b_new, a]
    claim = decide(v.test_claims()[0])
    assert claim.verdict == CONTRADICTED
    # Re-running test_b cleanly afterwards does clear it.
    b_fix = _run("pytest -q tests/test_b.py", "5 passed")
    b_fix.at = (T0 + timedelta(minutes=3)).isoformat()
    v._claude_runs.append(b_fix)
    assert decide(v.test_claims()[0]).verdict == VERIFIED


def test_r4_f2_a_fatal_git_error_in_the_same_call_is_not_success(tmp_path, repo):
    """One Codex call can run several commands; their output is pooled. Diff output
    from another command does not show that the claimed-range diff succeeded."""
    call = (
        'await Promise.all([tools.exec_command({cmd: "git diff {base} {head}"}), '
        'tools.exec_command({cmd: "git diff HEAD~1 HEAD"})])'
    )
    output = "fatal: bad object {base}\ndiff --git a/a.py b/a.py"
    assert _range_world(tmp_path, repo, call, output).verdict == UNVERIFIED
