"""#3585 slice 1: concurrent, non-conflicting contributions to one ticket.

Two agents work the same ticket in parallel without clobbering each other:
  - the OWNER and a scoped CONTRIBUTOR each work in their own git worktree on
    their own branch, on disjoint files;
  - both commit through the real Stage 1 commit guard (ATS git hooks installed
    in a governed repository, a real ATS server, real git);
  - neither can commit the other's file, and the owner cannot merge the
    contributor's work while the contributor still holds it;
  - the contributor hands off; the owner claims the file and integrates both
    branches; both contributions are on main afterwards;
  - throughout, the ticket's general mail goes to the owner, never the
    contributor.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from ai_team_sync import git_hook_install as ghi
from tests.test_hook_delivery_live import live_ats  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
TICKET = 3585


@pytest.fixture(autouse=True)
def _no_live_echo(monkeypatch):
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "0")
    monkeypatch.setenv("ECHO_BRAIN_URL", "http://127.0.0.1:9")


def _git(repo, *args, env, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], env=env, text=True,
                          capture_output=True, check=check, timeout=60)


@pytest.fixture
def team(live_ats, tmp_path):  # noqa: F811
    url, client, _server_env = live_ats
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    os.makedirs(cache, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="ats-3585-", dir=cache))
    try:
        gov = base / "gov"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ATS_", "CLAUDE"))}
        env.update(PYTHONPATH=str(SOURCE), GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q", "-b", "main", str(gov)], env=env, check=True)
        for name in ("a.txt", "b.txt"):
            (gov / name).write_text(f"{name} v1\n")
        _git(gov, "add", ".", env=env)
        _git(gov, "commit", "-qm", "init", env=env)
        ghi.install(gov, ("pre-commit", "pre-merge-commit"), python=sys.executable)
        gov_real = os.path.realpath(gov)
        config = tmp_path / "operator.toml"
        config.write_text(f'[governance]\nrepositories = ["{gov_real}"]\n')
        state = tmp_path / "state"
        state.mkdir(mode=0o700)
        env.update(ATS_SERVER_URL=url, ATS_OPERATOR_CONFIG=str(config),
                   ATS_STATE_DIR=str(state),
                   ATS_COMMIT_GUARD_AUDIT_LOG=str(tmp_path / "audit.log"))

        def start(agent, role, scope):
            made = client.post("/api/sessions", json={
                "developer": "pvestal", "agent": agent, "scope": scope,
                "repo_root": gov_real, "ticket_id": TICKET, "ticket_role": role,
                "lock_mode": "exclusive"})
            assert made.status_code == 201, made.text
            return made.json()["id"], made.headers["X-ATS-Approval-Token"], made.json()

        def as_session(sid):
            return dict(env, ATS_SESSION_ID=sid)

        yield {"client": client, "gov": gov, "gov_real": gov_real, "env": env,
               "start": start, "as_session": as_session, "base": base}
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _commit(worktree, env, path, content, msg):
    (worktree / path).write_text(content)
    _git(worktree, "add", path, env=env)
    return _git(worktree, "commit", "-m", msg, env=env, check=False)


def test_owner_and_contributor_work_in_parallel_and_both_contributions_land(team):
    client, gov, env = team["client"], team["gov"], team["env"]
    owner, owner_tok, owner_row = team["start"]("claude-code:aaaa1111", "owner", ["a.txt"])
    contrib, contrib_tok, contrib_row = team["start"]("codex:bbbb2222", "contributor",
                                                      ["b.txt"])
    assert owner_row["ticket_role"] == "owner"
    assert contrib_row["ticket_role"] == "contributor"
    assert contrib_row["ticket_owner_session_id"] == owner

    wt_a, wt_b = team["base"] / "wt-a", team["base"] / "wt-b"
    _git(gov, "worktree", "add", "-q", "-b", "feat-a", str(wt_a), env=env)
    _git(gov, "worktree", "add", "-q", "-b", "feat-b", str(wt_b), env=env)
    owner_env, contrib_env = team["as_session"](owner), team["as_session"](contrib)

    # Each commits its own file, in parallel, through the real commit guard.
    a = _commit(wt_a, owner_env, "a.txt", "a.txt by owner\n", "owner: a")
    b = _commit(wt_b, contrib_env, "b.txt", "b.txt by contributor\n", "contrib: b")
    assert a.returncode == 0, a.stderr
    assert b.returncode == 0, b.stderr

    # Neither can commit the other's file.
    crossed = _commit(wt_b, contrib_env, "a.txt", "contributor overwrites a\n", "clobber")
    assert crossed.returncode != 0 and "REFUSED" in crossed.stderr, crossed.stderr
    _git(wt_b, "reset", "-q", "--hard", "HEAD", env=env)

    # Ticket mail reaches the owner, never the contributor, while both work.
    sender = client.post("/api/sessions", json={
        "developer": "pvestal", "agent": "codex:cccc3333", "scope": [],
        "ticket_id": TICKET, "ticket_role": "reviewer"})
    sender_tok = sender.headers["X-ATS-Approval-Token"]
    note = client.post("/api/messages", json={
        "sender_session_id": sender.json()["id"], "ticket_id": TICKET,
        "body": "integrate both branches"}, headers={"X-ATS-Approval-Token": sender_tok})
    assert note.json()["recipient_session_id"] == owner
    contrib_inbox = client.get(f"/api/sessions/{contrib}/messages",
                               headers={"X-ATS-Approval-Token": contrib_tok}).json()
    assert note.json()["id"] not in {m["id"] for m in contrib_inbox}

    # The owner integrates its own branch.
    merged_a = _git(gov, "merge", "--no-ff", "-m", "merge a", "feat-a", env=owner_env,
                    check=False)
    assert merged_a.returncode == 0, merged_a.stderr

    # ...but not the contributor's while the contributor still holds b.txt.
    early = _git(gov, "merge", "--no-ff", "-m", "merge b", "feat-b", env=owner_env,
                 check=False)
    assert early.returncode != 0 and "REFUSED" in early.stderr, early.stderr
    _git(gov, "merge", "--abort", env=env, check=False)

    # The contributor hands off; the owner receives it, claims b.txt, integrates.
    done = client.patch(f"/api/sessions/{contrib}", json={
        "status": "completed", "summary": "b.txt done",
        "handoff": {"verdict": "b.txt ready on feat-b", "artifacts": ["feat-b"]}},
        headers={"X-ATS-Approval-Token": contrib_tok})
    assert done.status_code == 200, done.text
    owner_inbox = client.get(f"/api/sessions/{owner}/messages",
                             headers={"X-ATS-Approval-Token": owner_tok}).json()
    assert any(m["kind"] == "handoff" and m["sender_session_id"] == contrib
               for m in owner_inbox)
    claimed = client.post("/api/locks", json={"session_id": owner, "pattern": "b.txt",
                                              "mode": "exclusive", "reason": "integrate"})
    assert claimed.status_code == 201, claimed.text
    merged_b = _git(gov, "merge", "--no-ff", "-m", "merge b", "feat-b", env=owner_env,
                    check=False)
    assert merged_b.returncode == 0, merged_b.stderr

    # Neither contribution was lost.
    assert (gov / "a.txt").read_text() == "a.txt by owner\n"
    assert (gov / "b.txt").read_text() == "b.txt by contributor\n"
    log = _git(gov, "log", "--format=%s", "main", env=env).stdout.split("\n")
    assert {"owner: a", "contrib: b", "merge a", "merge b"} <= set(log)
