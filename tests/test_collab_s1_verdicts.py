"""#3585 slice 1: structured VERIFY verdicts and independent review.

Three layers:
  - the verdict rules themselves (verdicts.py), as pure functions;
  - the delegation API on a REAL ATS server: a VERIFY return must carry a
    structured verdict, the verdict that counts is derived from its items, and a
    reviewer of the same worker class as the requester is refused;
  - the REAL `ats delegate` supervisor, run as a subprocess against that
    server, with a stand-in `codex` executable on PATH that prints a fixed
    answer. Nothing in ATS is mocked; only the reviewer's words are fixed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_team_sync import verdicts
from ai_team_sync.verdicts import (BLOCK, NO_RESULT, PASS, PASS_WITH_CITATION,
                                   VerdictError, derive, from_child_output, normalize)
from tests.test_hook_delivery_live import live_ats  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
HEAD = "eba4b53532e893b263797df06a946f24fbeb5728"


@pytest.fixture(autouse=True)
def _no_live_echo(monkeypatch):
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "0")
    monkeypatch.setenv("ECHO_BRAIN_URL", "http://127.0.0.1:9")


def item(item_id, status=BLOCK, severity="material_correctness", category="behavior",
         citation="src/x.py:10"):
    return {"item_id": item_id, "status": status, "severity": severity,
            "claim_category": category, "citation": citation}


def verdict(code, *items, head=HEAD):
    return {"verdict_code": code, "reviewed": {"repo": "/r", "base_sha": "", "head_sha": head},
            "items": list(items)}


def fenced(payload) -> str:
    return f"Review notes...\n\n```{verdicts.FENCE}\n{json.dumps(payload)}\n```\n"


# --- the rules ----------------------------------------------------------------------

def test_the_effective_verdict_is_derived_from_the_items():
    material = item("1", severity="material_safety", category="security")
    wording = item("2", severity="nonmaterial", category="other")
    passing = item("3", status=PASS, severity="nonmaterial", category="other", citation="")
    assert derive(BLOCK, [normalize(verdict(BLOCK, material))["items"][0]]) == BLOCK
    assert normalize(verdict(BLOCK, wording))["effective_verdict"] == PASS_WITH_CITATION
    assert normalize(verdict(BLOCK, wording))["mismatch"] is True
    assert normalize(verdict(PASS, passing))["effective_verdict"] == PASS
    upgraded = normalize(verdict(PASS, material))
    assert upgraded["effective_verdict"] == BLOCK and upgraded["mismatch"] is True


@pytest.mark.parametrize("category", ["security", "authorization", "data_integrity",
                                      "enforcement_coverage", "behavior"])
def test_an_inaccurate_material_claim_cannot_be_filed_as_wording(category):
    # Operator refinement 2: a docs-only defect in an enforcement claim is material.
    with pytest.raises(VerdictError, match="material by rule"):
        normalize(verdict(BLOCK, item("6", severity="nonmaterial", category=category,
                                      citation="docs/commit-guard.md:12")))


def test_a_docs_only_enforcement_coverage_item_keeps_the_verdict_block():
    stored = normalize(verdict(BLOCK, item("6", severity="material_safety",
                                           category="enforcement_coverage",
                                           citation="CHANGELOG.md:12 overstates the guard")))
    assert stored["effective_verdict"] == BLOCK


@pytest.mark.parametrize("bad, reason", [
    ({"verdict_code": "MAYBE"}, "verdict_code"),
    (verdict(BLOCK), "at least one BLOCK item"),
    (verdict(PASS, head="not-a-sha"), "hex commit id"),
    (verdict(BLOCK, item("1"), item("1")), "unique"),
    (verdict(BLOCK, item("1", citation="")), "citation"),
    ({"verdict_code": NO_RESULT, "items": [item("1")]}, "no items"),
])
def test_malformed_verdicts_are_refused(bad, reason):
    with pytest.raises(VerdictError, match=reason):
        normalize(bad)


def test_child_output_without_a_valid_block_is_no_result_never_a_pass():
    assert from_child_output("")["verdict_code"] == NO_RESULT
    assert from_child_output("Looks good to me. PASS.")["verdict_code"] == NO_RESULT
    assert from_child_output("```ats-verdict\n{not json\n```")["verdict_code"] == NO_RESULT
    refused = from_child_output(fenced(verdict(BLOCK, item(
        "6", severity="nonmaterial", category="enforcement_coverage"))))
    assert refused["verdict_code"] == NO_RESULT and "material by rule" in refused["reason"]
    assert from_child_output(fenced(verdict(PASS)), launch_error="lease expired")[
        "verdict_code"] == NO_RESULT
    # The LAST block wins, so quoting the contract earlier is harmless.
    two = fenced(verdict(PASS)) + fenced(verdict(BLOCK, item("1")))
    assert from_child_output(two)["verdict_code"] == BLOCK


def test_every_verify_packet_carries_the_output_contract():
    from ai_team_sync.delegation_packet import build_child_packet
    packet = build_child_packet(mode="VERIFY", delegation={"id": "d", "prohibitions": []},
                                objective="o", acceptance="a")
    assert f"```{verdicts.FENCE}" in packet and "claim_category" in packet
    assert verdicts.FENCE not in build_child_packet(
        mode="READ_ONLY", delegation={"id": "d", "prohibitions": []},
        objective="o", acceptance="a")


# --- the delegation API on a real server ---------------------------------------------

def _session(client, agent):
    made = client.post("/api/sessions", json={"developer": "pvestal", "agent": agent,
                                              "scope": []})
    assert made.status_code == 201, made.text
    return made.json()["id"], made.headers["X-ATS-Approval-Token"]


def _verify(client, parent, worker="codex", mode="VERIFY"):
    made = client.post("/api/delegations", json={
        "parent_session_id": parent, "delegated_worker": worker, "mode": mode,
        "acceptance": "per-item verdict"})
    return made


def test_a_verify_return_without_a_verdict_is_refused_and_stays_open(live_ats):
    url, client, _ = live_ats
    parent, _ = _session(client, "claude-code:aaaa1111")
    delegation = _verify(client, parent).json()["id"]
    bare = client.post(f"/api/delegations/{delegation}/return", json={
        "result_summary": "LGTM"})
    assert bare.status_code == 422 and bare.json()["detail"]["error"] == "verdict_required"
    assert client.get(f"/api/delegations/{delegation}").json()["state"] == "open"
    bad = client.post(f"/api/delegations/{delegation}/return", json={
        "result_summary": "x", "structured_verdict": verdict(BLOCK, item(
            "6", severity="nonmaterial", category="authorization"))})
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "invalid_verdict"
    assert client.get(f"/api/delegations/{delegation}").json()["state"] == "open"


def test_structured_verdicts_are_stored_with_the_derived_verdict(live_ats):
    url, client, _ = live_ats
    parent, _ = _session(client, "claude-code:aaaa1111")
    cases = [
        (verdict(BLOCK, item("1", severity="material_safety", category="security"),
                 item("2", severity="nonmaterial", category="other")), BLOCK, False),
        (verdict(BLOCK, item("2", severity="nonmaterial", category="other")),
         PASS_WITH_CITATION, True),
        ({"verdict_code": NO_RESULT, "reason": "child produced nothing"}, NO_RESULT, False),
    ]
    for submitted, effective, mismatch in cases:
        delegation = _verify(client, parent).json()["id"]
        ret = client.post(f"/api/delegations/{delegation}/return", json={
            "result_summary": "see verdict", "structured_verdict": submitted})
        assert ret.status_code == 200, ret.text
        row = client.get(f"/api/delegations/{delegation}").json()
        assert row["state"] == "returned"
        assert row["effective_verdict"] == effective
        assert row["verdict_code"] == submitted["verdict_code"]
        assert row["structured_verdict"]["mismatch"] is mismatch
        # The parent's own reconciliation note is a different field, untouched.
        assert row["verdict"] == ""


def test_a_reviewer_of_the_same_worker_class_is_refused(live_ats):
    url, client, _ = live_ats
    claude, _ = _session(client, "claude-code:aaaa1111")
    codex, _ = _session(client, "codex:bbbb2222")
    same = _verify(client, claude, worker="claude-code")
    assert same.status_code == 409, same.text
    assert same.json()["detail"]["error"] == "review_not_independent"
    assert _verify(client, codex, worker="codex").status_code == 409
    assert _verify(client, claude, worker="codex").status_code == 201
    assert _verify(client, codex, worker="claude-code").status_code == 201
    # Only VERIFY is a review; a same-class READ_ONLY investigation is allowed.
    assert _verify(client, claude, worker="claude-code", mode="READ_ONLY").status_code == 201


# --- the real `ats delegate` supervisor -----------------------------------------------

@pytest.fixture
def fake_codex(tmp_path):
    """A `codex` executable on PATH that prints a fixed answer and exits 0."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    answer = tmp_path / "answer.txt"
    exe = bin_dir / "codex"
    exe.write_text(f"#!{sys.executable}\nimport sys\n"
                   f"sys.stdout.write(open({str(answer)!r}).read())\n")
    exe.chmod(0o755)
    return bin_dir, answer


def _delegate(url, server_env, bin_dir, parent, repo, tmp_path):
    env = {k: v for k, v in server_env.items()
           if not k.startswith(("ATS_SESSION", "CLAUDE_CODE_SESSION"))}
    env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", ATS_SERVER_URL=url,
               PYTHONPATH=str(SOURCE), ATS_STATE_DIR=str(tmp_path / "state"),
               ATS_AGENT="claude-code")
    run = subprocess.run(
        [sys.executable, "-m", "ai_team_sync.cli", "delegate", "--parent-session", parent,
         "--worker", "codex", "--mode", "VERIFY", "--repo", str(repo),
         "--objective", "review the slice", "--acceptance", "per-item verdict",
         "--lease-minutes", "5"],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(repo))
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout[run.stdout.index("{"):])


@pytest.mark.parametrize("answer, effective", [
    (fenced(verdict(BLOCK, item("1", severity="material_correctness", category="behavior"),
                    item("2", severity="nonmaterial", category="other"))), BLOCK),
    ("", NO_RESULT),
    ("Everything looks fine, PASS.", NO_RESULT),
])
def test_the_real_supervisor_records_the_childs_structured_verdict(
        live_ats, fake_codex, tmp_path, answer, effective):
    url, client, server_env = live_ats
    bin_dir, answer_file = fake_codex
    answer_file.write_text(answer)
    repo = tmp_path / "repo"
    repo.mkdir()
    parent, _ = _session(client, "claude-code:aaaa1111")

    out = _delegate(url, server_env, bin_dir, parent, repo, tmp_path)

    assert out["state"] == "returned", out
    assert out["effective_verdict"] == effective
    row = client.get(f"/api/delegations/{out['delegation_id']}").json()
    assert row["effective_verdict"] == effective
    assert row["mode"] == "VERIFY" and row["state"] == "returned"
    child = client.get(f"/api/sessions/{out['child_session_id']}").json()
    assert child["status"] == "completed"
