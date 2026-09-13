"""Operator ruling: explicit default is distinct from unmatched identities."""

import pytest
from click.testing import CliRunner

from ai_team_sync.cli import cli
from ai_team_sync.delegation import effective_authority
from ai_team_sync.launch_spec import RoutingFailure, build_launch, supported_modes
from ai_team_sync.workers import Authority, WorkerRegistry, _BUILTINS

DENIED = Authority("none", False, "no")
UNKNOWN = ("cursor", "random-agent-123", "unknown", "cursor:delegate", "", None)


@pytest.mark.parametrize("label", UNKNOWN)
@pytest.mark.parametrize("flag", [None, "0", "1", "false"])
def test_unmatched_identity_is_always_restricted(label, flag, monkeypatch):
    if flag is None:
        monkeypatch.delenv("ATS_STRICT_WORKERS", raising=False)
    else:
        monkeypatch.setenv("ATS_STRICT_WORKERS", flag)
    w = WorkerRegistry(_BUILTINS).resolve(label)
    assert w.name == "restricted"
    assert w.authority == DENIED
    assert w.capabilities == ("repo_read",)
    for mode in ("READ_ONLY", "VERIFY", "IMPLEMENT"):
        assert effective_authority(w, mode) == DENIED


@pytest.mark.parametrize("label,expected", [
    ("claude-code", Authority("claimed_scope", True, "conditional")),
    ("claude-code:instance", Authority("claimed_scope", True, "conditional")),
    ("codex", Authority("claimed_scope", True, "conditional")),
    ("codex:delegate", Authority("claimed_scope", True, "conditional")),
    ("local", DENIED), ("local:qwen:instance", DENIED),
    ("restricted", DENIED),
    ("default", Authority("claimed_scope", True, "no")),
])
def test_registered_authority_values_are_unchanged(label, expected):
    w = WorkerRegistry(_BUILTINS).resolve(label)
    assert w.authority == expected
    assert w.authority.task_close != "yes"
    assert effective_authority(w, "READ_ONLY") == DENIED
    assert effective_authority(w, "VERIFY") == DENIED
    assert effective_authority(w, "IMPLEMENT") == Authority(expected.edit, expected.commit, "no")


def test_unmatched_cannot_inherit_a_configured_fallback_grant():
    entries = dict(_BUILTINS)
    entries["restricted"] = {"authority": {"edit": "claimed_scope", "commit": True, "task_close": "yes"}}
    assert WorkerRegistry(entries).resolve("unregistered").authority == DENIED


@pytest.mark.parametrize("worker", ["cursor", "random-agent-123", "unknown"])
@pytest.mark.parametrize("mode", ["READ_ONLY", "VERIFY", "IMPLEMENT"])
def test_unknown_has_no_launcher_and_cli_refusal_does_no_io(worker, mode, monkeypatch):
    assert supported_modes(worker) == ()
    def forbidden(*args, **kwargs):
        pytest.fail("refused launch must not resolve a binary, contact REST, or spawn")
    with pytest.raises(RoutingFailure):
        build_launch(worker, mode, "packet", repo="/tmp", which=forbidden)
    monkeypatch.setattr("ai_team_sync.cli.httpx.Client", forbidden)
    monkeypatch.setattr("ai_team_sync.cli._server_url", lambda: "http://unit")
    monkeypatch.setattr("subprocess.run", forbidden)
    result = CliRunner().invoke(cli, ["delegate", "--parent-session", "unit-parent", "--worker", worker,
        "--mode", mode, "--repo", "/tmp", "--objective", "inspect", "--acceptance", "file:line"])
    assert result.exit_code == 3, result.output
    assert "refusing to spawn" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", ["cursor", "random-agent-123", "unknown"])
async def test_rest_unknown_registers_only_unscoped_with_exact_restricted_values(client, worker):
    claim = await client.post("/api/sessions", json={"developer": "unit", "agent": worker,
        "scope": ["src/**"], "description": "forbidden claim"})
    assert claim.status_code == 403
    assert claim.json()["detail"]["worker"]["authority"] == vars(DENIED)
    listed = await client.get("/api/sessions")
    assert listed.json() == []
    s = await client.post("/api/sessions", json={"developer": "unit", "agent": worker,
        "scope": [], "description": "inspection only"})
    assert s.status_code == 201
    assert s.json()["agent"] == worker
    authority = await client.get(f"/api/workers/{worker}")
    assert authority.json()["worker"] == "restricted"
    assert authority.json()["authority"] == vars(DENIED)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", ["cursor", "random-agent-123", "unknown"])
@pytest.mark.parametrize("binary", [None, "", "/tmp/fake-worker"])
async def test_rest_unknown_delegation_refusal_creates_no_records(client, worker, binary):
    parent = await client.post("/api/sessions", json={"developer": "unit", "agent": "codex:parent", "scope": []})
    pid = parent.json()["id"]
    before = (await client.get("/api/sessions")).json()
    payload = {"parent_session_id": pid, "delegated_worker": worker, "mode": "IMPLEMENT",
               "acceptance": "unit measurement", "objective": "not launched"}
    if binary is not None:
        payload["resolved_binary"] = binary
    r = await client.post("/api/delegations", json=payload)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "unregistered_worker"
    assert (await client.get(f"/api/delegations?parent_session_id={pid}")).json() == []
    after = (await client.get("/api/sessions")).json()
    # idle_seconds is a computed clock value, not a changed session field.
    assert [{k: v for k, v in r.items() if k != "idle_seconds"} for r in after] == [
        {k: v for k, v in r.items() if k != "idle_seconds"} for r in before]


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", ["claude-code", "codex", "local", "restricted", "default"])
async def test_registered_record_only_no_binary_behavior_is_preserved(client, worker):
    parent = await client.post("/api/sessions", json={"developer": "unit", "agent": "codex:parent", "scope": []})
    r = await client.post("/api/delegations", json={"parent_session_id": parent.json()["id"],
        "delegated_worker": worker, "acceptance": "record only", "resolved_binary": ""})
    assert r.status_code == 201, r.text
    assert r.json()["resolved_binary"] is None
