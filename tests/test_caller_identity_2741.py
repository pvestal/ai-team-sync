"""#2741 — a supplied worker name must not confer mutation authority.

ATS already knew what authority a worker CLASS has. It could not prove a caller
IS that worker: the label was a string in the request. These tests hold the
boundary that replaces it:

  caller --(kernel socket owner, at session creation)--> session binding
         --(recorded, never re-derived from a label)--> worker class
         --(authorize: bound uid == this request's uid)--> grant

Every attack found against the prototype (B1-B5, and N1: X-Forwarded-For
redirecting the kernel lookup to another process's socket) is pinned here.
Peer identity is monkeypatched for HTTP tests because an in-process ASGI
transport has no socket; the kernel lookup itself is tested against fixture
tables, a real socket pair, and a real uvicorn server.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, update

from ai_team_sync import peer_identity, scope_paths
from ai_team_sync.hooks.pre_tool_use_lockcheck import normalize_pattern, scope_matches
from ai_team_sync.models import AuthorityCheck, Delegation, ScopeLock, Session
from ai_team_sync.workers import registry

EXEC_UID = 4242
OTHER_UID = 4343
ROOT = "/srv/echo-2741"

WORKERS_TOML = f"""
[workers.echo-executor]
capabilities = ["repo_read", "isolated_worktree", "tests"]
bind_uids = [{EXEC_UID}]
[workers.echo-executor.authority]
edit = "claimed_scope"
commit = true
land = false
task_close = "conditional"

[workers.lander]
bind_uids = [{EXEC_UID}]
[workers.lander.authority]
edit = "claimed_scope"
commit = false
land = true
"""


@pytest.fixture
def fresh_registry():
    registry.cache_clear()
    yield
    registry.cache_clear()


@pytest.fixture
def bound_registry(tmp_path, monkeypatch, fresh_registry):
    cfg = tmp_path / "workers.toml"
    cfg.write_text(WORKERS_TOML)
    monkeypatch.setenv("ATS_WORKERS_CONFIG", str(cfg))
    registry.cache_clear()
    assert registry().config_error is None
    return cfg


@pytest.fixture
def peer(monkeypatch):
    """The kernel's answer for the requesting socket, controlled per request."""
    state = {"uid": EXEC_UID}
    monkeypatch.setattr(peer_identity, "peer_uid_for_request", lambda request: state["uid"])
    return state


async def _create(client, peer, uid, agent, scope=(), repo_root=ROOT, **extra):
    peer["uid"] = uid
    return await client.post("/api/sessions", json={
        "developer": "t", "agent": agent, "scope": list(scope), "repo_root": repo_root,
        "description": "2741", "auto_lock": True, "lock_mode": "advisory", **extra})


async def _sid(client, peer, uid, agent, scope=(), **extra):
    resp = await _create(client, peer, uid, agent, scope, **extra)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _authorize(client, peer, uid, sid, action, **body):
    peer["uid"] = uid
    resp = await client.post(f"/api/authority/{sid}/authorize", json={"action": action, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _commit(paths, root=ROOT):
    return {"repo_root": root, "paths": list(paths)}


# ── 1. an arbitrary caller naming the executor ─────────────────────────────

@pytest.mark.parametrize("label", ["echo-executor:run1", "echo-executor", " echo-executor",
                                   "echo-executor:run1:deeper"])
@pytest.mark.parametrize("uid", [OTHER_UID, None, 0])
async def test_arbitrary_caller_naming_the_executor_obtains_no_authority(client, bound_registry, peer, uid, label):
    scoped = await _create(client, peer, uid, label, ["src/a.py"])
    assert scoped.status_code == 403
    assert "bound to OS uid" in scoped.json()["detail"]["message"]

    sid = await _sid(client, peer, uid, label, task_id=2741)
    for action, body in (("commit", _commit(["src/a.py"])), ("land", _commit(["src/a.py"])),
                         ("task_close", {"task_id": 2741, "evidence": {"ok": True}})):
        answer = await _authorize(client, peer, uid, sid, action, **body)
        assert answer["allowed"] is False, answer
        assert answer["identity_bound"] is False

    shown = (await client.get(f"/api/authority/{sid}")).json()
    assert shown["worker"] == "restricted"
    assert shown["identity_bound"] is False and shown["grantable"] is False
    assert shown["base_authority"] == {"edit": "none", "commit": False, "land": False, "task_close": "no"}


# ── 2-4. aliases, defaults, suffixes and unknown labels ────────────────────

LABELS = ["claude-code:anything", "claude-code:echo-executor", "codex", "codex:delegate",
          "default", "default:echo-executor", "local:qwen3", "unknown", "cursor",
          "echo-executorx", "Echo-Executor", ""]


@pytest.mark.parametrize("label", LABELS)
@pytest.mark.parametrize("uid", [EXEC_UID, OTHER_UID])
async def test_no_alias_default_or_unknown_label_receives_a_grant(client, bound_registry, peer, label, uid):
    sid = await _sid(client, peer, uid, label, task_id=2741)
    commit = await _authorize(client, peer, uid, sid, "commit", **_commit(["src/a.py"]))
    close = await _authorize(client, peer, uid, sid, "task_close", task_id=2741, evidence={"ok": 1})
    assert commit["allowed"] is False and close["allowed"] is False


async def test_b3_claude_label_holding_its_own_claim_gets_no_grant(client, bound_registry, peer):
    sid = await _sid(client, peer, OTHER_UID, "claude-code:anything", ["src/a.py"], task_id=999)
    commit = await _authorize(client, peer, OTHER_UID, sid, "commit", **_commit(["src/a.py"]))
    close = await _authorize(client, peer, OTHER_UID, sid, "task_close", task_id=999, evidence={"x": 1})
    assert commit["allowed"] is False and close["allowed"] is False
    assert any("not identity-bound" in r for r in commit["reasons"])
    # Claude's coordination authority is untouched; it is reported as advisory.
    shown = (await client.get(f"/api/authority/{sid}")).json()
    assert shown["base_authority"]["commit"] is True
    assert shown["grantable"] is False and "advisory" in shown["binding_note"]


# ── 5. identity cannot be substituted after creation ───────────────────────

async def test_a_bound_session_is_usable_only_from_its_bound_account(client, bound_registry, peer):
    sid = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"], task_id=2741)
    assert (await _authorize(client, peer, EXEC_UID, sid, "commit", **_commit(["src/a.py"])))["allowed"]

    for uid in (OTHER_UID, None):
        assert not (await _authorize(client, peer, uid, sid, "commit", **_commit(["src/a.py"])))["allowed"]
        assert not (await _authorize(client, peer, uid, sid, "task_close", task_id=2741,
                                     evidence={"ok": 1}))["allowed"]
        peer["uid"] = uid
        assert (await client.post("/api/locks", json={"session_id": sid, "pattern": "**"})).status_code == 403
        assert (await client.patch(f"/api/sessions/{sid}", json={"scope": ["**"]})).status_code == 403
        assert (await client.patch(f"/api/sessions/{sid}", json={"repo_root": "/elsewhere"})).status_code in (403, 409)
        assert (await client.post(f"/api/sessions/{sid}/complete", json={})).status_code == 403

    # The label is not writable, and the bound account cannot move the anchor.
    peer["uid"] = EXEC_UID
    assert (await client.patch(f"/api/sessions/{sid}", json={"agent": "lander:x"})).json()["agent"] == "echo-executor:run1"
    assert (await client.patch(f"/api/sessions/{sid}", json={"repo_root": "/elsewhere"})).status_code == 409
    assert (await _authorize(client, peer, EXEC_UID, sid, "commit", **_commit(["src/a.py"])))["allowed"]


async def test_a_session_label_naming_a_bound_class_is_not_a_binding(client, bound_registry, peer, db_session):
    sid = await _sid(client, peer, OTHER_UID, "echo-executor:run1")
    # Even a row forged to look bound fails unless the class binds that uid.
    await db_session.execute(update(Session).where(Session.id == sid)
                             .values(bound_worker="echo-executor", bound_uid=OTHER_UID))
    await db_session.commit()
    answer = await _authorize(client, peer, OTHER_UID, sid, "task_close", task_id=1, evidence={"a": 1})
    assert answer["allowed"] is False


async def test_rebinding_the_class_revokes_existing_sessions(client, bound_registry, peer):
    sid = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"])
    bound_registry.write_text(WORKERS_TOML.replace(f"bind_uids = [{EXEC_UID}]", f"bind_uids = [{OTHER_UID}]", 1))
    registry.cache_clear()
    for uid in (EXEC_UID, OTHER_UID):
        assert not (await _authorize(client, peer, uid, sid, "commit", **_commit(["src/a.py"])))["allowed"]


async def test_an_extra_identity_field_in_the_request_is_refused(client, bound_registry, peer):
    sid = await _sid(client, peer, OTHER_UID, "claude-code:x", ["src/a.py"])
    answer = await _authorize(client, peer, OTHER_UID, sid, "commit", worker="echo-executor",
                              **_commit(["src/a.py"]))
    assert answer["allowed"] is False
    assert any("malformed" in r for r in answer["reasons"])


# ── N1. forwarding headers cannot redirect the kernel lookup ───────────────

_HEADER = ("  sl  local_address                         remote_address                        "
           "st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n")


def _v4(ip, port):
    return f"{socket.inet_aton(ip)[::-1].hex().upper()}:{port:04X}"


def _row(local, remote, uid, state="01"):
    return f"   0: {local} {remote} {state} 00000000:00000000 00:00000000 00000000  {uid}        0 1\n"


def _net(tmp_path, rows=(), rows6=()):
    net = tmp_path / "net"
    net.mkdir()
    (net / "tcp").write_text(_HEADER + "".join(rows))
    (net / "tcp6").write_text(_HEADER + "".join(rows6))
    return net


SCOPE = {"type": "http", "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8400), "headers": []}


def test_the_exact_client_row_answers_not_the_servers_reversed_row(tmp_path):
    net = _net(tmp_path, [_row(_v4("127.0.0.1", 8400), _v4("127.0.0.1", 50000), os.geteuid()),
                          _row(_v4("127.0.0.1", 50000), _v4("127.0.0.1", 8400), 993)])
    assert peer_identity.peer_uid_from_scope(SCOPE, proc_net=net) == 993


@pytest.mark.parametrize("state,uid", [("08", None), ("01", 12345), ("06", None)])
def test_this_servers_own_socket_must_still_be_established_and_ours(tmp_path, state, uid):
    """A caller that already closed leaves our socket in CLOSE_WAIT; a 4-tuple
    could then be re-established by another process before the lookup."""
    net = _net(tmp_path, [_row(_v4("127.0.0.1", 8400), _v4("127.0.0.1", 50000),
                               os.geteuid() if uid is None else uid, state=state),
                          _row(_v4("127.0.0.1", 50000), _v4("127.0.0.1", 8400), 993)])
    assert peer_identity.peer_uid_from_scope(SCOPE, proc_net=net) is None


def test_client_port_alone_listeners_and_other_states_are_not_the_connection(tmp_path):
    net = _net(tmp_path, [_row(_v4("127.0.0.1", 50000), _v4("127.0.0.1", 9999), 993),
                          _row(_v4("127.0.0.1", 50000), "00000000:0000", 993, state="0A"),
                          _row(_v4("127.0.0.1", 50000), _v4("127.0.0.1", 8400), 993, state="06")])
    assert peer_identity.peer_uid_from_scope(SCOPE, proc_net=net) is None


def test_two_owners_for_one_tuple_is_unknown(tmp_path):
    net = _net(tmp_path, [_row(_v4("127.0.0.1", 50000), _v4("127.0.0.1", 8400), 993)],
               ["   0: 0000000000000000FFFF00000100007F:C350 0000000000000000FFFF00000100007F:20D0 "
                "01 00000000:00000000 00:00000000 00000000  1000        0 1\n"])
    assert peer_identity.peer_uid_from_scope(SCOPE, proc_net=net) is None


def test_ipv4_mapped_rows_match(tmp_path):
    net = _net(tmp_path, rows6=[
        "   0: 0000000000000000FFFF00000100007F:C350 0000000000000000FFFF00000100007F:20D0 "
        "01 00000000:00000000 00:00000000 00000000  993        0 1\n",
        "   0: 0000000000000000FFFF00000100007F:20D0 0000000000000000FFFF00000100007F:C350 "
        f"01 00000000:00000000 00:00000000 00000000  {os.geteuid()}        0 1\n"])
    scope = dict(SCOPE, client=("::ffff:127.0.0.1", 50000), server=("::ffff:127.0.0.1", 8400))
    assert peer_identity.peer_uid_from_scope(scope, proc_net=net) == 993


@pytest.mark.parametrize("name", [b"x-forwarded-for", b"X-Forwarded-For", b"forwarded", b"x-real-ip"])
def test_any_forwarding_header_makes_the_caller_unidentifiable(tmp_path, name):
    net = _net(tmp_path, [_row(_v4("127.0.0.1", 50000), _v4("127.0.0.1", 8400), 993),
                          _row(_v4("127.0.0.1", 8400), _v4("127.0.0.1", 50000), os.geteuid())])
    assert peer_identity.peer_uid_from_scope(SCOPE, proc_net=net) == 993, "control: identifiable without it"
    scope = dict(SCOPE, headers=[(name, b"127.0.0.1:50000")])
    assert peer_identity.peer_uid_from_scope(scope, proc_net=net) is None


@pytest.mark.parametrize("client_addr,server_addr", [
    (("10.0.0.5", 50000), ("127.0.0.1", 8400)),
    (("127.0.0.1", True), ("127.0.0.1", 8400)),
    (None, ("127.0.0.1", 8400)),
    (("127.0.0.1", 50000), None),
    (("testclient", 50000), ("127.0.0.1", 8400)),
])
def test_unusable_addresses_are_unknown(tmp_path, client_addr, server_addr):
    net = _net(tmp_path, [_row(_v4("127.0.0.1", 50000), _v4("127.0.0.1", 8400), 993)])
    scope = dict(SCOPE, client=client_addr, server=server_addr)
    assert peer_identity.peer_uid_from_scope(scope, proc_net=net) is None


needs_proc = pytest.mark.skipif(not Path("/proc/net/tcp").exists(), reason="Linux /proc/net only")


@needs_proc
def test_real_kernel_reports_this_process_as_the_owner():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.create_connection(srv.getsockname())
    conn, _ = srv.accept()
    try:
        scope = {"type": "http", "client": cli.getsockname(), "server": srv.getsockname(), "headers": []}
        assert peer_identity.peer_uid_from_scope(scope) == os.getuid()
        forwarded = dict(scope, headers=[(b"x-forwarded-for", b"127.0.0.1")])
        assert peer_identity.peer_uid_from_scope(forwarded) is None
    finally:
        conn.close()
        cli.close()
        srv.close()


@needs_proc
def test_uvicorn_default_proxy_headers_cannot_point_identity_at_another_socket():
    """The live N1 attack, reproduced against a real uvicorn with its defaults."""
    import httpx
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def who(request):
        return JSONResponse({"client": list(request.client),
                             "peer_uid": peer_identity.peer_uid_from_scope(request.scope)})

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    config = uvicorn.Config(Starlette(routes=[Route("/who", who)]), host="127.0.0.1",
                            port=port, log_level="warning")
    assert config.proxy_headers is True, "the attack needs uvicorn's default"
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    victim = None
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        victim = socket.create_connection(("127.0.0.1", port))
        victim_port = victim.getsockname()[1]
        spoofed = httpx.get(f"http://127.0.0.1:{port}/who",
                            headers={"X-Forwarded-For": f"127.0.0.1:{victim_port}"}).json()
        assert spoofed["client"][1] == victim_port, "uvicorn did rewrite the client"
        assert spoofed["peer_uid"] is None, "and identity refused to follow it"
        plain = httpx.get(f"http://127.0.0.1:{port}/who").json()
        assert plain["peer_uid"] == os.getuid()
    finally:
        if victim is not None:
            victim.close()
        server.should_exit = True
        thread.join(5)


def test_ats_server_starts_uvicorn_without_proxy_headers(monkeypatch):
    import uvicorn

    from ai_team_sync import server

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: seen.update(kwargs))
    server.main()
    assert seen["proxy_headers"] is False


# ── 6-8. strict configuration ──────────────────────────────────────────────

GOOD_BOUND = f"""
[workers.bound-ok]
bind_uids = [{EXEC_UID}]
[workers.bound-ok.authority]
edit = "claimed_scope"
commit = true
"""

BAD_CONFIGS = {
    "not toml": "this is not toml [[[",
    "typo bind key": '[workers.x]\nbind_user = ["nobody"]\n',
    "bind_uids string": '[workers.x]\nbind_uids = "4242"\n',
    "bind_uids string 1000": '[workers.x]\nbind_uids = "1000"\n',
    "bind_uids int": "[workers.x]\nbind_uids = 4242\n",
    "bind_uids string entries": '[workers.x]\nbind_uids = ["4242"]\n',
    "bind_uids bool entry": "[workers.x]\nbind_uids = [true]\n",
    "bind_uids negative": "[workers.x]\nbind_uids = [-1]\n",
    "bind root": "[workers.x]\nbind_uids = [0]\n",
    "bind the server account": f"[workers.x]\nbind_uids = [{os.getuid()}]\n",
    "bind_users unknown account": '[workers.x]\nbind_users = ["no-such-account-2741"]\n',
    "bind_users string": '[workers.x]\nbind_users = "root"\n',
    "commit string false": '[workers.x]\n[workers.x.authority]\ncommit = "false"\n',
    "land string false": '[workers.x]\n[workers.x.authority]\nland = "false"\n',
    "land integer": "[workers.x]\n[workers.x.authority]\nland = 1\n",
    "edit typo": '[workers.x]\n[workers.x.authority]\nedit = "claimed-scope"\n',
    "task_close typo": '[workers.x]\n[workers.x.authority]\ntask_close = "maybe"\n',
    "unknown authority key": "[workers.x]\n[workers.x.authority]\nservice_control = false\n",
    "unknown top-level table": "[other]\nx = 1\n",
    "entry not a table": "[workers]\nx = 1\n",
    "concurrency string": '[workers.x]\nconcurrency = "1"\n',
    "unbound child of a bound family": ('[workers."bound-ok:x"]\n[workers."bound-ok:x".authority]\n'
                                       'edit = "claimed_scope"\ncommit = true\n'),
}


@pytest.mark.parametrize("name", sorted(BAD_CONFIGS))
def test_malformed_configuration_is_rejected_whole_and_binds_nobody(tmp_path, monkeypatch, fresh_registry, name):
    cfg = tmp_path / "workers.toml"
    cfg.write_text(BAD_CONFIGS[name] + GOOD_BOUND)
    monkeypatch.setenv("ATS_WORKERS_CONFIG", str(cfg))
    registry.cache_clear()
    reg = registry()
    assert reg.config_error, name
    assert reg.resolve("bound-ok").name == "restricted", "no part of a rejected file is trusted"
    assert not any(w.identity_bound for w in reg.all())
    assert reg.resolve("claude-code").may_claim_scope, "coordination does not wedge"


def test_toml_false_means_false(tmp_path, monkeypatch, fresh_registry):
    cfg = tmp_path / "workers.toml"
    cfg.write_text(GOOD_BOUND.replace("commit = true", "commit = false\nland = false"))
    monkeypatch.setenv("ATS_WORKERS_CONFIG", str(cfg))
    registry.cache_clear()
    authority = registry().resolve("bound-ok").authority
    assert authority.commit is False and authority.land is False


async def test_a_rejected_configuration_breaks_no_session_and_grants_nothing(client, peer, tmp_path, monkeypatch, fresh_registry):
    cfg = tmp_path / "workers.toml"
    cfg.write_text("[workers.x]\nbind_uids = 4242\n" + GOOD_BOUND)
    monkeypatch.setenv("ATS_WORKERS_CONFIG", str(cfg))
    registry.cache_clear()
    assert (await _create(client, peer, EXEC_UID, "claude-code:abc", ["docs/x.md"])).status_code == 201
    assert (await _create(client, peer, EXEC_UID, "bound-ok:run", ["src/a.py"])).status_code == 403
    sid = await _sid(client, peer, EXEC_UID, "bound-ok:run")
    answer = await _authorize(client, peer, EXEC_UID, sid, "commit", **_commit(["src/a.py"]))
    assert answer["allowed"] is False
    assert any("configuration was rejected" in r for r in answer["reasons"])


# ── 9-11. one canonical path; no spelling evades a lock ────────────────────

async def _exec_with_foreign_exclusive_lock(client, peer, pattern="src/a.py", owner_root=ROOT):
    """The executor claims first; another account's exclusive lock arrives
    afterwards through POST /api/locks, which checks no conflicts. Session
    creation would have refused the opposite order, so only the grant-time
    check stands between the executor and the locked file here."""
    executor = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"])
    owner = await _sid(client, peer, OTHER_UID, "claude-code:owner", repo_root=owner_root)
    peer["uid"] = OTHER_UID
    lock = await client.post("/api/locks", json={"session_id": owner, "pattern": pattern, "mode": "exclusive"})
    assert lock.status_code == 201, lock.text
    return owner, lock.json()["id"], executor


@pytest.mark.parametrize("path", ["src//a.py", "src/./a.py", "./src/a.py", "src/a.py/", "src", "src/"])
async def test_request_spellings_cannot_evade_an_exclusive_lock(client, bound_registry, peer, path):
    _, _, executor = await _exec_with_foreign_exclusive_lock(client, peer)
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit([path])))["allowed"]
    assert (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/b.py"])))["allowed"], \
        "control: the lock is what refuses, not the claim"


@pytest.mark.parametrize("pattern", ["src//a.py", "./src/a.py", "src/./a.py", "src/../src/a.py",
                                     f"{ROOT}/src/a.py", f"{ROOT}//src/a.py", "src/a.py/",
                                     "src/*.py", "src/[ab].py", "src/?.py", "src", "src/**", "*", ROOT])
async def test_lock_spellings_still_protect_the_file(client, bound_registry, peer, pattern):
    _, _, executor = await _exec_with_foreign_exclusive_lock(client, peer, pattern=pattern)
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]


@pytest.mark.parametrize("owner_root", [ROOT + "/", "/srv//echo-2741", ""])
async def test_root_spellings_and_unanchored_locks_still_apply(client, bound_registry, peer, owner_root):
    _, _, executor = await _exec_with_foreign_exclusive_lock(client, peer, owner_root=owner_root)
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]


async def test_a_lock_in_a_different_repository_does_not_apply(client, bound_registry, peer):
    _, _, executor = await _exec_with_foreign_exclusive_lock(client, peer, owner_root="/srv/other-repo")
    assert (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]


@pytest.mark.parametrize("path", ["../etc/passwd", "src/../../x", "src/../src/a.py", f"{ROOT}/src/a.py",
                                  "/etc/passwd", "src\\a.py", ":(glob)src/a.py", "-rf", ".git/hooks/pre-commit",
                                  "src/.git/config", "", ".", "./", "src/a\n.py",
                                  "src/?.py", "src/[a].py", "src/*", "src/**", "src/a.py*"])
async def test_escape_glob_and_ambiguous_paths_are_refused(client, bound_registry, peer, path):
    executor = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"])
    answer = await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit([path]))
    assert answer["allowed"] is False
    assert answer["paths"] == []


async def test_symlinks_cannot_escape_or_alias_a_locked_file(client, bound_registry, peer, tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("a")
    (repo / "src" / "alias.py").symlink_to("a.py")
    (repo / "src" / "out").symlink_to("/etc")
    root = str(repo)
    executor = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"], repo_root=root)
    owner = await _sid(client, peer, OTHER_UID, "claude-code:owner", repo_root=root)
    peer["uid"] = OTHER_UID
    assert (await client.post("/api/locks", json={"session_id": owner, "pattern": "src/a.py",
                                                  "mode": "exclusive"})).status_code == 201
    escaped = await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/out/passwd"], root))
    assert escaped["allowed"] is False and any("outside the repository" in r for r in escaped["reasons"])
    aliased = await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/alias.py"], root))
    assert aliased["allowed"] is False


@pytest.mark.parametrize("claim", ["src/*.py", "src/?.py", "**", "src/../x", "/abs/x", "src/**/x.py", ".git/**"])
async def test_a_bound_session_cannot_declare_an_ambiguous_claim(client, bound_registry, peer, claim):
    assert (await _create(client, peer, EXEC_UID, "echo-executor:run1", [claim])).status_code == 422


async def test_a_bound_claim_is_stored_canonical_and_needs_an_absolute_root(client, bound_registry, peer):
    made = await _create(client, peer, EXEC_UID, "echo-executor:run1", ["src//a.py", "./docs/**"], repo_root=ROOT + "/")
    assert made.status_code == 201
    assert made.json()["scope"] == ["src/a.py", "docs/**"] and made.json()["repo_root"] == ROOT
    assert (await _create(client, peer, EXEC_UID, "echo-executor:run2", ["src/a.py"], repo_root="")).status_code == 422


# ── 12. a caller cannot manufacture its own prerequisite ───────────────────

@pytest.mark.parametrize("mode", ["advisory", "exclusive"])
async def test_a_self_created_lock_bootstraps_nothing(client, bound_registry, peer, mode):
    executor = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["docs/x.md"])
    peer["uid"] = EXEC_UID
    for pattern in ("src/**", "*", "deploy/prod.yaml"):
        assert (await client.post("/api/locks", json={"session_id": executor, "pattern": pattern,
                                                      "mode": mode})).status_code == 201
    assert (await client.patch(f"/api/sessions/{executor}", json={"scope": ["**"]})).status_code == 200
    for path in ("src/a.py", "deploy/prod.yaml"):
        answer = await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit([path]))
        assert answer["allowed"] is False
        assert any("identity-bound live claim" in r for r in answer["reasons"])
    assert (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["docs/x.md"])))["allowed"]


# ── 13. conflicting exclusive ownership ────────────────────────────────────

async def test_a_bound_worker_cannot_clear_another_accounts_exclusive_lock(client, bound_registry, peer):
    owner, lock_id, executor = await _exec_with_foreign_exclusive_lock(client, peer)
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]

    peer["uid"] = EXEC_UID
    assert (await client.delete(f"/api/locks/{lock_id}", params={"actor_session_id": owner})).status_code == 403
    assert (await client.patch(f"/api/sessions/{owner}", json={"status": "completed"})).status_code == 403
    assert (await client.post(f"/api/sessions/{owner}/complete", json={})).status_code == 403
    peer["uid"] = None
    assert (await client.delete(f"/api/locks/{lock_id}", params={"actor_session_id": owner})).status_code == 403
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]

    peer["uid"] = OTHER_UID
    assert (await client.delete(f"/api/locks/{lock_id}", params={"actor_session_id": owner})).status_code == 204
    assert (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]


# ── 14-15. the legitimate caller gets exactly its class ────────────────────

async def test_the_bound_caller_receives_its_grant_and_it_is_audited(client, bound_registry, peer, db_session):
    sid = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**", "README.md"], task_id=2741)
    answer = await _authorize(client, peer, EXEC_UID, sid, "commit",
                              **_commit(["src/a.py", "src//b/c.py", "README.md"]))
    assert answer["allowed"] is True, answer["reasons"]
    assert answer["paths"] == ["src/a.py", "src/b/c.py", "README.md"]
    assert answer["identity_bound"] is True and answer["worker"] == "echo-executor"
    assert answer["peer_uid"] == EXEC_UID == answer["bound_uid"]

    shown = (await client.get(f"/api/authority/{sid}")).json()
    assert shown["identity_bound"] is True and shown["grantable"] is True
    assert shown["bound_worker"] == "echo-executor" and shown["task_id"] == 2741

    rows = (await db_session.execute(select(AuthorityCheck))).scalars().all()
    assert [(r.allowed, r.action, r.bound_uid, r.peer_uid, r.worker) for r in rows] == \
        [(True, "commit", EXEC_UID, EXEC_UID, "echo-executor")]


async def test_a_bound_caller_gets_nothing_its_class_lacks(client, bound_registry, peer):
    executor = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"], task_id=7)
    land = await _authorize(client, peer, EXEC_UID, executor, "land", **_commit(["src/a.py"]))
    assert land["allowed"] is False and any("no land authority" in r for r in land["reasons"])
    for action in ("service_control", "delete_repo", "COMMIT"):
        assert not (await _authorize(client, peer, EXEC_UID, executor, action, **_commit(["src/a.py"])))["allowed"]

    lander = await _sid(client, peer, EXEC_UID, "lander:run1", ["src/**"], task_id=7)
    assert not (await _authorize(client, peer, EXEC_UID, lander, "commit", **_commit(["src/a.py"])))["allowed"]
    assert (await _authorize(client, peer, EXEC_UID, lander, "land", **_commit(["src/a.py"])))["allowed"]
    assert not (await _authorize(client, peer, EXEC_UID, lander, "task_close", task_id=7,
                                 evidence={"a": 1}))["allowed"]


# ── 16. task close is scoped to the session's task ─────────────────────────

async def test_task_close_authority_is_for_the_declared_task_only(client, bound_registry, peer):
    sid = await _sid(client, peer, EXEC_UID, "echo-executor:close", task_id=2741)
    ok = await _authorize(client, peer, EXEC_UID, sid, "task_close", task_id=2741, evidence={"acceptance": "met"})
    assert ok["allowed"] is True, ok["reasons"]
    assert not (await _authorize(client, peer, EXEC_UID, sid, "task_close", task_id=2742,
                                 evidence={"acceptance": "met"}))["allowed"]
    assert not (await _authorize(client, peer, EXEC_UID, sid, "task_close", task_id=2741))["allowed"]
    assert not (await _authorize(client, peer, EXEC_UID, sid, "task_close", task_id="2741",
                                 evidence={"a": 1}))["allowed"]

    no_task = await _sid(client, peer, EXEC_UID, "echo-executor:close2")
    answer = await _authorize(client, peer, EXEC_UID, no_task, "task_close", task_id=2741, evidence={"a": 1})
    assert answer["allowed"] is False and any("without a task" in r for r in answer["reasons"])
    assert (await _create(client, peer, EXEC_UID, "echo-executor:close3", task_id="2741")).status_code == 422
    peer["uid"] = EXEC_UID
    await client.patch(f"/api/sessions/{no_task}", json={"task_id": 2741})
    assert not (await _authorize(client, peer, EXEC_UID, no_task, "task_close", task_id=2741,
                                 evidence={"a": 1}))["allowed"], "task_id is not patchable"


# ── 17. delegation never raises authority ──────────────────────────────────

async def _delegate(client, parent, mode, worker="echo-executor"):
    resp = await client.post("/api/delegations", json={
        "parent_session_id": parent, "delegated_worker": worker, "mode": mode,
        "acceptance": "evidence returned", "objective": "bounded"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_a_delegated_child_is_narrowed_by_mode_and_parent(client, bound_registry, peer):
    parent = await _sid(client, peer, EXEC_UID, "echo-executor:parent", ["src/**"], task_id=2741)
    implement = await _delegate(client, parent, "IMPLEMENT")
    child = await _sid(client, peer, EXEC_UID, "echo-executor:delegate", ["src/**"],
                       delegation_id=implement, task_id=2741)
    assert (await _authorize(client, peer, EXEC_UID, child, "commit", **_commit(["src/a.py"])))["allowed"]
    assert not (await _authorize(client, peer, EXEC_UID, child, "land", **_commit(["src/a.py"])))["allowed"]
    assert not (await _authorize(client, peer, EXEC_UID, child, "task_close", task_id=2741,
                                 evidence={"a": 1}))["allowed"]

    read_only = await _delegate(client, parent, "READ_ONLY")
    assert (await _create(client, peer, EXEC_UID, "echo-executor:delegate", ["src/**"],
                          delegation_id=read_only)).status_code == 403
    ro_child = await _sid(client, peer, EXEC_UID, "echo-executor:delegate", delegation_id=read_only)
    assert not (await _authorize(client, peer, EXEC_UID, ro_child, "commit", **_commit(["src/a.py"])))["allowed"]


async def test_an_unbound_parent_cannot_launder_a_bound_child(client, bound_registry, peer):
    parent = await _sid(client, peer, EXEC_UID, "claude-code:parent")
    implement = await _delegate(client, parent, "IMPLEMENT")
    child = await _sid(client, peer, EXEC_UID, "echo-executor:delegate", ["src/**"], delegation_id=implement)
    answer = await _authorize(client, peer, EXEC_UID, child, "commit", **_commit(["src/a.py"]))
    assert answer["allowed"] is False
    assert any("parent is an active identity-bound session" in r for r in answer["reasons"])


async def test_a_child_on_another_account_is_not_bound(client, bound_registry, peer):
    parent = await _sid(client, peer, EXEC_UID, "echo-executor:parent", ["src/**"])
    implement = await _delegate(client, parent, "IMPLEMENT")
    assert (await _create(client, peer, OTHER_UID, "echo-executor:delegate", ["src/**"],
                          delegation_id=implement)).status_code == 403


async def test_a_returned_or_orphaned_child_cannot_act(client, bound_registry, peer):
    parent = await _sid(client, peer, EXEC_UID, "echo-executor:parent", ["src/**"])
    implement = await _delegate(client, parent, "IMPLEMENT")
    child = await _sid(client, peer, EXEC_UID, "echo-executor:delegate", ["src/**"], delegation_id=implement)
    assert (await client.post(f"/api/delegations/{implement}/return",
                              json={"actor_session_id": child, "result_summary": "done"})).status_code == 200
    assert not (await _authorize(client, peer, EXEC_UID, child, "commit", **_commit(["src/a.py"])))["allowed"]

    implement2 = await _delegate(client, parent, "IMPLEMENT")
    child2 = await _sid(client, peer, EXEC_UID, "echo-executor:delegate", ["src/**"], delegation_id=implement2)
    peer["uid"] = EXEC_UID
    await client.post(f"/api/delegations/{implement}/close", json={"actor_session_id": parent})
    await client.post(f"/api/delegations/{implement2}/close", json={"actor_session_id": parent})
    await client.post(f"/api/sessions/{parent}/complete", json={})
    assert not (await _authorize(client, peer, EXEC_UID, child2, "commit", **_commit(["src/a.py"])))["allowed"]


# ── 18. finished, paused and expired authority is not reusable ─────────────

async def test_expired_claims_authorize_nothing(client, bound_registry, peer, db_session):
    sid = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"])
    assert (await _authorize(client, peer, EXEC_UID, sid, "commit", **_commit(["src/a.py"])))["allowed"]
    await db_session.execute(update(ScopeLock).where(ScopeLock.session_id == sid)
                             .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    await db_session.commit()
    assert not (await _authorize(client, peer, EXEC_UID, sid, "commit", **_commit(["src/a.py"])))["allowed"]


async def test_paused_and_completed_sessions_cannot_reuse_authority(client, bound_registry, peer, db_session):
    sid = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"], task_id=2741)
    peer["uid"] = EXEC_UID
    assert (await client.patch(f"/api/sessions/{sid}", json={"status": "paused"})).status_code == 200
    assert not (await _authorize(client, peer, EXEC_UID, sid, "commit", **_commit(["src/a.py"])))["allowed"]
    peer["uid"] = OTHER_UID
    assert (await client.patch(f"/api/sessions/{sid}", json={"status": "active"})).status_code == 403
    peer["uid"] = EXEC_UID
    assert (await client.patch(f"/api/sessions/{sid}", json={"status": "active"})).status_code == 200
    assert (await _authorize(client, peer, EXEC_UID, sid, "commit", **_commit(["src/a.py"])))["allowed"]

    assert (await client.post(f"/api/sessions/{sid}/complete", json={})).status_code == 200
    assert not (await _authorize(client, peer, EXEC_UID, sid, "commit", **_commit(["src/a.py"])))["allowed"]
    assert not (await _authorize(client, peer, EXEC_UID, sid, "task_close", task_id=2741,
                                 evidence={"a": 1}))["allowed"]
    peer["uid"] = EXEC_UID
    assert (await client.patch(f"/api/sessions/{sid}", json={"status": "active"})).status_code == 409
    assert (await client.post(f"/api/sessions/{sid}/heartbeat")).status_code == 409
    await db_session.execute(update(Session).where(Session.id == sid).values(auto_completed=True))
    await db_session.commit()
    assert (await client.post(f"/api/sessions/{sid}/heartbeat")).status_code == 409


# ── audit: refusals are on the record, evidence content is not ─────────────

async def test_every_refusal_is_audited_and_evidence_content_is_not_stored(client, bound_registry, peer, db_session):
    peer["uid"] = OTHER_UID
    assert (await client.post("/api/authority/nope/authorize", json={"action": "commit"})).json()["allowed"] is False
    assert (await client.post("/api/authority/nope/authorize", content=b"not json",
                              headers={"content-type": "application/json"})).json()["allowed"] is False
    assert (await client.post("/api/authority/nope/authorize",
                              json={"action": "commit", "paths": "src/a.py"})).json()["allowed"] is False
    sid = await _sid(client, peer, EXEC_UID, "echo-executor:close", task_id=5)
    await _authorize(client, peer, EXEC_UID, sid, "task_close", task_id=5, evidence={"secret_token": "s3cr3t-2741"})

    rows = (await db_session.execute(select(AuthorityCheck))).scalars().all()
    assert len(rows) == 4
    assert [r.allowed for r in rows].count(True) == 1
    for row in rows:
        assert "s3cr3t-2741" not in " ".join(str(v) for v in row.__dict__.values())
    assert any(r.evidence_keys == '["secret_token"]' for r in rows)


# ── adversarial review round 1 (all reproduced as grants before the fix) ────

async def test_r1_f1_a_delegation_child_is_set_once_by_the_parents_account(client, bound_registry, peer):
    parent = await _sid(client, peer, EXEC_UID, "echo-executor:parent", ["src/**"], task_id=2741)
    implement = await _delegate(client, parent, "IMPLEMENT")
    child = await _sid(client, peer, EXEC_UID, "echo-executor:delegate", ["src/**"],
                       delegation_id=implement, task_id=2741)
    for uid in (OTHER_UID, None, EXEC_UID):
        repoint = await _create(client, peer, uid, "claude-code:attacker", delegation_id=implement)
        assert repoint.status_code in (403, 409), (uid, repoint.text)
    assert not (await _authorize(client, peer, EXEC_UID, child, "task_close", task_id=2741,
                                 evidence={"a": 1}))["allowed"]

    read_only = await _delegate(client, parent, "READ_ONLY")
    ro_child = await _sid(client, peer, EXEC_UID, "echo-executor:delegate", delegation_id=read_only, task_id=2741)
    for uid in (OTHER_UID, None):
        assert (await _create(client, peer, uid, "claude-code:attacker",
                              delegation_id=read_only)).status_code in (403, 409)
    assert not (await _authorize(client, peer, EXEC_UID, ro_child, "task_close", task_id=2741,
                                 evidence={"a": 1}))["allowed"]

    fresh = await _delegate(client, parent, "IMPLEMENT")
    for uid in (OTHER_UID, None):
        assert (await _create(client, peer, uid, "claude-code:x", delegation_id=fresh)).status_code == 403


async def test_r1_f1_a_repointed_delegation_row_never_widens_the_child(client, bound_registry, peer, db_session):
    parent = await _sid(client, peer, EXEC_UID, "echo-executor:parent", ["src/**"], task_id=2741)
    implement = await _delegate(client, parent, "READ_ONLY")
    child = await _sid(client, peer, EXEC_UID, "echo-executor:delegate", delegation_id=implement, task_id=2741)
    other = await _sid(client, peer, OTHER_UID, "claude-code:x")
    await db_session.execute(update(Delegation).where(Delegation.id == implement)
                             .values(child_session_id=other))
    await db_session.commit()
    close = await _authorize(client, peer, EXEC_UID, child, "task_close", task_id=2741, evidence={"a": 1})
    assert close["allowed"] is False
    assert any("inconsistent" in r for r in close["reasons"])
    await client.post(f"/api/sessions/{parent}/complete", json={})
    assert not (await _authorize(client, peer, EXEC_UID, child, "task_close", task_id=2741,
                                 evidence={"a": 1}))["allowed"]


async def test_r1_f1_a_wider_delegation_pointed_at_a_child_is_not_adopted(client, bound_registry, peer, db_session):
    """The child was created under READ_ONLY; an IMPLEMENT delegation is later
    pointed at it. Only the delegation recorded at creation may govern it."""
    parent = await _sid(client, peer, EXEC_UID, "echo-executor:parent", ["src/**"], task_id=2741)
    read_only = await _delegate(client, parent, "READ_ONLY")
    child = await _sid(client, peer, EXEC_UID, "echo-executor:delegate", delegation_id=read_only)
    other = await _sid(client, peer, EXEC_UID, "echo-executor:other")
    wider = await _delegate(client, parent, "IMPLEMENT")
    await db_session.execute(update(Delegation).where(Delegation.id == read_only)
                             .values(child_session_id=other))
    await db_session.execute(update(Delegation).where(Delegation.id == wider)
                             .values(child_session_id=child))
    await db_session.execute(update(Session).where(Session.id == child)
                             .values(scope='["src/**"]'))
    db_session.add(ScopeLock(session_id=child, pattern="src/**", mode="advisory", authority_bearing=True))
    await db_session.commit()
    commit = await _authorize(client, peer, EXEC_UID, child, "commit", **_commit(["src/a.py"]))
    assert commit["allowed"] is False
    assert any("inconsistent" in r for r in commit["reasons"])


@pytest.mark.parametrize("attack", ["delete_lock", "complete", "patch_complete", "reanchor", "add_lock"])
async def test_r1_f2_a_silent_session_of_another_account_cannot_be_cleared(client, bound_registry, peer, db_session, attack):
    owner, lock_id, executor = await _exec_with_foreign_exclusive_lock(client, peer)
    await db_session.execute(update(Session).where(Session.id == owner).values(
        started_at=datetime.now(timezone.utc) - timedelta(minutes=25), last_heartbeat=None))
    await db_session.commit()
    peer["uid"] = EXEC_UID
    if attack == "delete_lock":
        resp = await client.delete(f"/api/locks/{lock_id}", params={"actor_session_id": owner})
    elif attack == "complete":
        resp = await client.post(f"/api/sessions/{owner}/complete", json={})
    elif attack == "patch_complete":
        resp = await client.patch(f"/api/sessions/{owner}", json={"status": "completed"})
    elif attack == "reanchor":
        resp = await client.patch(f"/api/sessions/{owner}", json={"repo_root": "/srv/elsewhere"})
    else:
        resp = await client.post("/api/locks", json={"session_id": owner, "pattern": "x"})
    assert resp.status_code == 403, resp.text
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]


@pytest.mark.parametrize("attacker", [EXEC_UID, None])
async def test_r1_f2_an_unidentified_owner_is_not_clearable_by_a_bound_or_unidentified_caller(
        client, bound_registry, peer, db_session, attacker):
    owner, lock_id, executor = await _exec_with_foreign_exclusive_lock(client, peer)
    await db_session.execute(update(Session).where(Session.id == owner).values(creator_uid=None))
    await db_session.commit()
    peer["uid"] = attacker
    assert (await client.delete(f"/api/locks/{lock_id}", params={"actor_session_id": owner})).status_code == 403
    assert (await client.post(f"/api/sessions/{owner}/complete", json={})).status_code == 403
    assert (await client.patch(f"/api/sessions/{owner}", json={"repo_root": "/srv/x"})).status_code == 403
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]


async def test_r1_f2_unidentified_owners_stay_manageable_by_an_ordinary_account(client, bound_registry, peer, db_session):
    owner = await _sid(client, peer, OTHER_UID, "claude-code:legacy")
    await db_session.execute(update(Session).where(Session.id == owner).values(creator_uid=None))
    await db_session.commit()
    peer["uid"] = OTHER_UID
    assert (await client.post(f"/api/sessions/{owner}/complete", json={})).status_code == 200


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "deploy").mkdir()
    (repo / "src" / "a.py").write_text("a")
    (repo / "deploy" / "prod.yaml").write_text("p")
    return repo


async def _foreign_lock(client, peer, root, pattern, owner_root=None):
    owner = await _sid(client, peer, OTHER_UID, "claude-code:owner",
                       repo_root=root if owner_root is None else owner_root)
    peer["uid"] = OTHER_UID
    assert (await client.post("/api/locks", json={"session_id": owner, "pattern": pattern,
                                                  "mode": "exclusive"})).status_code == 201


async def test_r1_f3_a_lock_spelled_through_a_symlink_protects_the_real_file(client, bound_registry, peer, tmp_path):
    repo = _repo(tmp_path)
    (repo / "lnk").symlink_to("src")
    root = str(repo)
    executor = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"], repo_root=root)
    await _foreign_lock(client, peer, root, "lnk/a.py")
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"], root)))["allowed"]
    assert (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/b.py"], root)))["allowed"]


async def test_r1_f3_a_symlinked_repository_root_is_the_same_repository(client, bound_registry, peer, tmp_path):
    repo = _repo(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(repo)
    executor = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"], repo_root=str(repo))
    await _foreign_lock(client, peer, str(repo), "src/a.py", owner_root=str(alias))
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit",
                                 **_commit(["src/a.py"], str(repo))))["allowed"]


@pytest.mark.parametrize("owner_root,pattern", [(ROOT + "/src", "a.py"), (ROOT + "/src", "*.py"),
                                                ("/srv", "echo-2741/src/a.py"), (ROOT + "/src", ".")])
async def test_r1_f4_a_lock_anchored_below_or_above_this_root_still_applies(client, bound_registry, peer, owner_root, pattern):
    executor = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"])
    await _foreign_lock(client, peer, ROOT, pattern, owner_root=owner_root)
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]


async def test_r1_f5_claim_coverage_is_checked_where_the_path_really_lands(client, bound_registry, peer, tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "lnk").symlink_to("../deploy")
    root = str(repo)
    executor = await _sid(client, peer, EXEC_UID, "echo-executor:run1", ["src/**"], repo_root=root)
    answer = await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/lnk/prod.yaml"], root))
    assert answer["allowed"] is False
    assert any("identity-bound live claim" in r for r in answer["reasons"])


async def test_r1_an_expired_exclusive_lock_of_a_live_owner_still_blocks(client, bound_registry, peer, db_session):
    _, lock_id, executor = await _exec_with_foreign_exclusive_lock(client, peer)
    await db_session.execute(update(ScopeLock).where(ScopeLock.id == lock_id)
                             .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    await db_session.commit()
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]


@pytest.mark.parametrize("evidence", [{"k": None}, {"k": ""}, {"k": []}, {"k": {}}])
async def test_r1_f9_empty_evidence_values_are_not_evidence(client, bound_registry, peer, evidence):
    sid = await _sid(client, peer, EXEC_UID, "echo-executor:close", task_id=2741)
    assert not (await _authorize(client, peer, EXEC_UID, sid, "task_close", task_id=2741,
                                 evidence=evidence))["allowed"]


# ── adversarial review round 2: ATS's own background tasks ─────────────────

@pytest.mark.parametrize("via", ["heartbeat", "session_header", "agent_header"])
async def test_r2_f1_no_account_can_put_another_accounts_session_on_the_fast_reaper(
        client, bound_registry, peer, db_session, via):
    from ai_team_sync.background_tasks import auto_complete_stale_sessions

    owner, _, executor = await _exec_with_foreign_exclusive_lock(client, peer)
    await db_session.execute(update(Session).where(Session.id == owner).values(
        started_at=datetime.now(timezone.utc) - timedelta(minutes=25), last_heartbeat=None))
    await db_session.commit()

    peer["uid"] = EXEC_UID
    if via == "heartbeat":
        assert (await client.post(f"/api/sessions/{owner}/heartbeat")).status_code == 403
    elif via == "session_header":
        await client.get("/health", headers={"X-ATS-Session-Id": owner})
    else:
        await client.get("/health", headers={"X-ATS-Agent": "claude-code:owner"})

    db_session.expire_all()
    row = (await db_session.execute(select(Session).where(Session.id == owner))).scalar_one()
    assert row.last_heartbeat is None, "another account proved this session alive"
    await auto_complete_stale_sessions(db_session)
    db_session.expire_all()
    row = (await db_session.execute(select(Session).where(Session.id == owner))).scalar_one()
    assert row.status == "active"
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]


async def test_r2_f1_the_owner_still_proves_its_own_liveness(client, bound_registry, peer):
    owner = await _sid(client, peer, OTHER_UID, "claude-code:owner")
    peer["uid"] = OTHER_UID
    assert (await client.post(f"/api/sessions/{owner}/heartbeat")).status_code == 200
    await client.get("/health", headers={"X-ATS-Session-Id": owner})
    assert (await client.get(f"/api/sessions/{owner}")).json()["last_heartbeat"] is not None


async def test_r2_f2_the_ttl_sweep_keeps_a_live_owners_exclusive_lock(client, bound_registry, peer, db_session):
    from ai_team_sync.background_tasks import check_expired_locks

    owner, lock_id, executor = await _exec_with_foreign_exclusive_lock(client, peer)
    await db_session.execute(update(ScopeLock).where(ScopeLock.id == lock_id)
                             .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    await db_session.commit()
    assert await check_expired_locks(db_session) == 0
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]

    await db_session.execute(update(Session).where(Session.id == owner).values(status="completed"))
    await db_session.commit()
    assert await check_expired_locks(db_session) == 1, "a finished owner's expired lock is still swept"


def _worktree_pair(tmp_path):
    """A main checkout with two linked worktrees, laid out as git lays them out:
    a `.git` file in each worktree and a registry entry naming it back."""
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    (main / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    linked = []
    for name in ("wt", "wtB"):
        checkout = tmp_path / name
        checkout.mkdir()
        entry = main / ".git" / "worktrees" / name
        entry.mkdir(parents=True)
        (entry / "gitdir").write_text(f"{checkout}/.git\n")
        (checkout / ".git").write_text(f"gitdir: {entry}\n")
        linked.append(checkout)
    for checkout in (main, *linked):
        (checkout / "src").mkdir()
        (checkout / "src" / "a.py").write_text("a")
    return str(main), str(linked[0]), str(linked[1])


@pytest.mark.parametrize("owner_sub,pattern", [("", "src/a.py"), ("/src", "a.py"), ("", "{main}/src/a.py")])
async def test_r2_f4_land_honours_a_lock_taken_in_another_worktree(client, bound_registry, peer, tmp_path, owner_sub, pattern):
    main, wt, _ = _worktree_pair(tmp_path)
    lander = await _sid(client, peer, EXEC_UID, "lander:run", ["src/**"], repo_root=wt)
    executor = await _sid(client, peer, EXEC_UID, "echo-executor:run", ["src/**"], repo_root=wt)
    await _foreign_lock(client, peer, main, pattern.format(main=main), owner_root=main + owner_sub)

    assert not (await _authorize(client, peer, EXEC_UID, lander, "land", **_commit(["src/a.py"], wt)))["allowed"]
    assert (await _authorize(client, peer, EXEC_UID, lander, "land", **_commit(["src/b.py"], wt)))["allowed"]
    assert (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"], wt)))["allowed"], \
        "a commit in an isolated worktree does not touch the other checkout's file"


# ── adversarial review round 3: every writer, every lock shape ─────────────

@pytest.mark.parametrize("attacker", [EXEC_UID, None])
async def test_r3_f1_a_presence_post_from_another_account_proves_nothing(
        client, bound_registry, peer, db_session, attacker):
    from ai_team_sync.background_tasks import auto_complete_stale_sessions

    owner, _, executor = await _exec_with_foreign_exclusive_lock(client, peer)
    await db_session.execute(update(Session).where(Session.id == owner).values(
        started_at=datetime.now(timezone.utc) - timedelta(minutes=25), last_heartbeat=None))
    await db_session.commit()

    peer["uid"] = attacker
    posted = await client.post("/api/presence", json={"developer": "x", "agent": "claude-code:owner",
                                                      "files": ["src/a.py"]})
    assert posted.status_code == 200
    db_session.expire_all()
    row = (await db_session.execute(select(Session).where(Session.id == owner))).scalar_one()
    assert row.last_heartbeat is None, "another account's presence proved this session alive"
    await auto_complete_stale_sessions(db_session)
    db_session.expire_all()
    assert (await db_session.execute(select(Session.status).where(Session.id == owner))).scalar_one() == "active"
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]

    peer["uid"] = OTHER_UID
    await client.post("/api/presence", json={"developer": "x", "agent": "claude-code:owner"})
    assert (await client.get(f"/api/sessions/{owner}")).json()["last_heartbeat"] is not None, \
        "the owner's own presence still proves liveness"


@pytest.mark.parametrize("shape", ["parent_anchor", "unanchored_absolute", "other_worktree",
                                   "other_worktree_absolute", "main_checkout"])
async def test_r3_f2_land_honours_a_lock_on_the_file_in_any_checkout_however_anchored(
        client, bound_registry, peer, tmp_path, shape):
    main, wt, wt_b = _worktree_pair(tmp_path)
    lander = await _sid(client, peer, EXEC_UID, "lander:run", ["src/**"], repo_root=wt)
    owner_root, pattern = {
        "parent_anchor": (str(tmp_path), "main/src/a.py"),
        "unanchored_absolute": ("", f"{main}/src/a.py"),
        "other_worktree": (wt_b, "src/a.py"),
        "other_worktree_absolute": ("", f"{wt_b}/src/a.py"),
        "main_checkout": (main, "src/a.py"),
    }[shape]
    await _foreign_lock(client, peer, main, pattern, owner_root=owner_root)
    assert not (await _authorize(client, peer, EXEC_UID, lander, "land", **_commit(["src/a.py"], wt)))["allowed"]
    assert (await _authorize(client, peer, EXEC_UID, lander, "land", **_commit(["src/b.py"], wt)))["allowed"]


async def test_r3_f3_paused_owners_exclusive_locks_are_kept_while_live_and_swept_when_silent(
        client, bound_registry, peer, db_session):
    from ai_team_sync.background_tasks import check_expired_locks

    owner, lock_id, executor = await _exec_with_foreign_exclusive_lock(client, peer)
    peer["uid"] = OTHER_UID
    assert (await client.patch(f"/api/sessions/{owner}", json={"status": "paused"})).status_code == 200
    await db_session.execute(update(ScopeLock).where(ScopeLock.id == lock_id)
                             .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    await db_session.commit()

    assert await check_expired_locks(db_session) == 0
    assert any(lock["id"] == lock_id for lock in (await client.get("/api/locks")).json()), \
        "a lock that blocks grants is visible on the board"
    assert not (await _authorize(client, peer, EXEC_UID, executor, "commit", **_commit(["src/a.py"])))["allowed"]

    await db_session.execute(update(Session).where(Session.id == owner).values(
        started_at=datetime.now(timezone.utc) - timedelta(days=3), last_heartbeat=None))
    await db_session.commit()
    assert await check_expired_locks(db_session) == 1, "a silent paused ghost's expired lock is not kept forever"


async def test_r3_f4_delegations_are_changed_only_by_the_owning_account(client, bound_registry, peer):
    victim = await _sid(client, peer, OTHER_UID, "claude-code:victim")
    for uid in (EXEC_UID, None):
        peer["uid"] = uid
        assert (await client.post("/api/delegations", json={
            "parent_session_id": victim, "delegated_worker": "codex", "mode": "READ_ONLY",
            "acceptance": "x"})).status_code == 403

    peer["uid"] = OTHER_UID
    delegation = await _delegate(client, victim, "READ_ONLY", worker="codex")
    child = await _sid(client, peer, OTHER_UID, "codex:delegate", delegation_id=delegation)
    for uid in (EXEC_UID, None):
        peer["uid"] = uid
        assert (await client.post(f"/api/delegations/{delegation}/return",
                                  json={"actor_session_id": child})).status_code == 403
        assert (await client.post(f"/api/delegations/{delegation}/close",
                                  json={"actor_session_id": victim})).status_code == 403
    peer["uid"] = OTHER_UID
    assert (await client.post(f"/api/delegations/{delegation}/return",
                              json={"actor_session_id": child})).status_code == 200
    assert (await client.post(f"/api/delegations/{delegation}/close",
                              json={"actor_session_id": victim})).status_code == 200


async def test_r4_a_childless_delegation_cannot_be_returned_from_another_account(client, bound_registry, peer):
    parent = await _sid(client, peer, OTHER_UID, "claude-code:parent")
    peer["uid"] = OTHER_UID
    delegation = await _delegate(client, parent, "READ_ONLY", worker="codex")
    for uid in (EXEC_UID, None):
        peer["uid"] = uid
        assert (await client.post(f"/api/delegations/{delegation}/return",
                                  json={"result_summary": "not mine"})).status_code == 403
    peer["uid"] = OTHER_UID
    assert (await client.get(f"/api/delegations/{delegation}")).json()["state"] == "open"
    assert (await _create(client, peer, OTHER_UID, "codex:delegate",
                          delegation_id=delegation)).status_code == 201, "the real child still attaches"


async def _reap(db_session, sid):
    """What the reaper leaves behind for a silent session: completed, revivable."""
    await db_session.execute(update(Session).where(Session.id == sid).values(
        status="completed", auto_completed=True, completed_at=datetime.now(timezone.utc)))
    await db_session.commit()


async def test_r5_a_reaped_parents_delegations_still_belong_to_its_account(client, bound_registry, peer, db_session):
    parent = await _sid(client, peer, OTHER_UID, "claude-code:parent")
    peer["uid"] = OTHER_UID
    delegation = await _delegate(client, parent, "READ_ONLY", worker="codex")
    closable = await _delegate(client, parent, "READ_ONLY", worker="codex")
    await _reap(db_session, parent)

    for uid in (EXEC_UID, None):
        peer["uid"] = uid
        assert (await client.post(f"/api/delegations/{delegation}/return",
                                  json={"result_summary": "forged"})).status_code == 403
        assert (await client.post(f"/api/delegations/{closable}/close",
                                  json={"state": "rejected", "actor_session_id": parent})).status_code == 403
        assert (await client.post("/api/delegations", json={
            "parent_session_id": parent, "delegated_worker": "codex", "mode": "READ_ONLY",
            "acceptance": "x"})).status_code == 403

    peer["uid"] = OTHER_UID
    assert (await client.post(f"/api/sessions/{parent}/heartbeat")).status_code == 200, "the owner revives it"
    assert (await client.get(f"/api/delegations/{delegation}")).json()["state"] == "open"
    assert (await _create(client, peer, OTHER_UID, "codex:delegate",
                          delegation_id=delegation)).status_code == 201, "the real child still attaches"


async def test_r5_a_dead_child_is_no_door_to_forge_its_result(client, bound_registry, peer, db_session):
    parent = await _sid(client, peer, OTHER_UID, "claude-code:parent")
    peer["uid"] = OTHER_UID
    delegation = await _delegate(client, parent, "READ_ONLY", worker="codex")
    child = await _sid(client, peer, OTHER_UID, "codex:delegate", delegation_id=delegation)
    await _reap(db_session, child)

    for uid in (EXEC_UID, None):
        peer["uid"] = uid
        assert (await client.post(f"/api/delegations/{delegation}/return",
                                  json={"actor_session_id": child, "result_summary": "forged"})).status_code == 403
    peer["uid"] = OTHER_UID
    assert (await client.get(f"/api/delegations/{delegation}")).json()["state"] == "open"
    assert (await client.post(f"/api/delegations/{delegation}/return",
                              json={"actor_session_id": child, "result_summary": "real"})).status_code == 200


async def test_r6_a_missing_session_row_establishes_no_owner(client, bound_registry, peer, db_session):
    from sqlalchemy import delete

    parent = await _sid(client, peer, OTHER_UID, "claude-code:parent")
    peer["uid"] = OTHER_UID
    childless = await _delegate(client, parent, "READ_ONLY", worker="codex")
    with_child = await _delegate(client, parent, "READ_ONLY", worker="codex")
    child = await _sid(client, peer, OTHER_UID, "codex:delegate", delegation_id=with_child)

    await db_session.execute(delete(Session).where(Session.id == child))
    await db_session.commit()
    for uid in (EXEC_UID, None, OTHER_UID):
        peer["uid"] = uid
        assert (await client.post(f"/api/delegations/{with_child}/return",
                                  json={"actor_session_id": child, "result_summary": "forged"})).status_code == 409

    await db_session.execute(delete(Session).where(Session.id == parent))
    await db_session.commit()
    for uid in (EXEC_UID, None, OTHER_UID):
        peer["uid"] = uid
        assert (await client.post(f"/api/delegations/{childless}/return",
                                  json={"result_summary": "forged"})).status_code == 409
        assert (await client.post(f"/api/delegations/{childless}/close",
                                  json={"state": "rejected", "actor_session_id": parent})).status_code == 409
    for delegation in (childless, with_child):
        assert (await client.get(f"/api/delegations/{delegation}")).json()["state"] == "open"


async def test_r6_another_account_cannot_revive_or_edit_a_reaped_session(client, bound_registry, peer, db_session):
    sid = await _sid(client, peer, OTHER_UID, "claude-code:owner")
    await _reap(db_session, sid)
    for uid in (EXEC_UID, None):
        peer["uid"] = uid
        assert (await client.patch(f"/api/sessions/{sid}", json={"status": "active"})).status_code == 403
        assert (await client.patch(f"/api/sessions/{sid}", json={"summary": "not mine"})).status_code == 403
    peer["uid"] = OTHER_UID
    assert (await client.patch(f"/api/sessions/{sid}", json={"status": "active"})).json()["status"] == "active"


@needs_proc
def test_the_real_server_records_and_enforces_kernel_identity_end_to_end(tmp_path, monkeypatch):
    """No stand-in: the production identity path through a real uvicorn socket."""
    import sqlite3

    import httpx
    import uvicorn
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from ai_team_sync.database import get_db
    from ai_team_sync.models import Base
    from ai_team_sync.server import create_app

    monkeypatch.setattr(peer_identity, "peer_uid_for_request",
                        lambda request: peer_identity.peer_uid_from_scope(request.scope))
    monkeypatch.setenv("ATS_EMIT_COMPLETION", "0")
    db_file = tmp_path / "ats.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    forwarded = {"X-Forwarded-For": "127.0.0.1"}

    def row(sid):
        with sqlite3.connect(db_file) as conn:
            return conn.execute("select creator_uid, status, last_heartbeat from sessions where id = ?",
                                (sid,)).fetchone()

    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            made = http.post("/api/sessions", json={"developer": "t", "agent": "claude-code:real2741"})
            assert made.status_code == 201, made.text
            sid = made.json()["id"]
            assert row(sid)[0] == os.getuid(), "the kernel's answer was recorded as the creator"
            assert http.post(f"/api/sessions/{sid}/complete", json={}, headers=forwarded).status_code == 403
            assert http.post("/api/presence", json={"developer": "t", "agent": "claude-code:real2741"},
                             headers=forwarded).status_code == 200
            assert row(sid)[2] is None, "an unidentifiable presence post proved nothing"
            assert http.post(f"/api/sessions/{sid}/heartbeat").status_code == 200
            assert row(sid)[2] is not None
            anon = http.post("/api/sessions", json={"developer": "t", "agent": "claude-code:anon2741"},
                             headers=forwarded)
            assert anon.status_code == 201 and row(anon.json()["id"])[0] is None
            assert http.post(f"/api/sessions/{sid}/complete", json={}).status_code == 200
            assert row(sid)[1] == "completed"
    finally:
        server.should_exit = True
        thread.join(5)


# ── canonical path helper ──────────────────────────────────────────────────

@pytest.mark.parametrize("raw,canonical", [("src/a.py", "src/a.py"), ("src//a.py", "src/a.py"),
                                           ("./src/./a.py", "src/a.py"), ("src/a.py/", "src/a.py")])
def test_canonical_relpath_collapses_equivalent_spellings(raw, canonical):
    assert scope_paths.canonical_relpath(raw) == canonical


@pytest.mark.parametrize("raw", ["", ".", "./", "/abs", "../x", "a/../b", "a\\b", ":(top)x", "src/*.py",
                                 "src/?", "src/[a]", ".git/config", "-x", "a\x00b", None, 5])
def test_canonical_relpath_refuses_what_it_cannot_give_one_meaning(raw):
    with pytest.raises(scope_paths.UnsafePath):
        scope_paths.canonical_relpath(raw)


@pytest.mark.parametrize("path,pattern,expected", [
    ("src", "src/a.py", True), ("src/a.py", "src", True), ("src/b.py", "src/a.py", False),
    ("docs/x.md", "src/*.py", False), ("src/a.py", "*", True), ("x/a.py", "*/a.py", True),
    ("src/a.py", "", True),
])
def test_may_overlap_is_conservative(path, pattern, expected):
    assert scope_paths.may_overlap(path, pattern) is expected


def test_the_lockcheck_hook_uses_the_same_canonical_form():
    assert normalize_pattern("src//a.py") == "src/a.py"
    assert normalize_pattern("/opt/x//src/./a.py", "/opt/x") == "src/a.py"
    assert scope_matches("src/a.py", "src/./a.py")
