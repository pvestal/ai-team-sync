"""Restart safety gate (#3580): policy, probes, leases, and the safe-restart driver.

Operator ruling 2026-10-13: governed restarts are REFUSED while requests, jobs or
GPU queues are in flight; the check and the restart cannot race (lease + drain);
a failed or unavailable check refuses; attempts AND outcomes are recorded.
Codex VERIFY BLOCK (delegation 9b4b7087) added: required evidence per unit with
explicit waivers only (2), lease/drain renewal + TTL validation + final-round
and commit-time checks + single-worker proof (3), outcomes recorded on every
exit path + an eager expiry sweeper (5), and a bootstrap path for the first
load of the middleware (7).

These are in-process tests: probes and drain calls go through an
httpx.MockTransport standing in for the service. The same flows against real
processes are in test_restart_gate_acceptance_3580.py.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_team_sync import restart_gate as rg
from ai_team_sync import safe_restart as sr
from ai_team_sync.operator_config import governed_repositories
from ai_team_sync.restart_lease_model import RestartLease
from ai_team_sync.routers import restart_leases

UNIT_TOML = """
[governance]
repositories = ["/opt/anime-studio"]

[restart_gate]
governed_units = ["svc"]

[restart_gate.units."svc"]
scope = "user"
health_url = "http://svc.test/health"
drain_url = "http://svc.test/api/system/drain"
release_url = "http://svc.test/api/system/drain/release"
lease_ttl_s = 120
drain_wait_s = 2
restart_timeout_s = 5
verify_timeout_s = 5
probes = [
  { name = "api", kind = "inflight", url = "http://svc.test/api/system/inflight" },
  { name = "gpu", kind = "comfyui", url = "http://gpu.test/queue" },
]

[restart_gate.units."svc".bootstrap]
port = 18401
jobs = { kind = "http", url = "http://svc.test/jobs" }
"""

COMPLETE = """
[restart_gate.units."{u}"]
lease_ttl_s = 120
drain_wait_s = 2
restart_timeout_s = 5
verify_timeout_s = 5
drain_url = "http://x.test/d"
release_url = "http://x.test/r"
probes = [
  {{ kind = "inflight", url = "http://x.test/i" }},
  {{ kind = "comfyui", url = "http://x.test/q" }},
]
"""


class FakeService:
    """The anime-studio inflight contract + a ComfyUI /queue, in memory."""

    def __init__(self):
        self.in_flight: list[dict] = []
        self.jobs: dict = {"generating_shots": 0}
        self.jobs_ok = True
        self.process = {"pid": 4242, "workers": 1, "mode": "single"}
        self.queue = {"queue_running": [], "queue_pending": []}
        self.drain: str | None = None
        self.drain_calls: list[dict] = []
        self.release_calls: list[dict] = []
        self.inflight_override = None  # callable(request) -> httpx.Response
        self.middleware_loaded = True
        self.bootstrap_jobs: object = {"generating_shots": 0}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "gpu.test":
            return httpx.Response(200, json=self.queue)
        if path == "/jobs":
            return httpx.Response(200, json=self.bootstrap_jobs)
        if path == "/api/system/inflight":
            if not self.middleware_loaded:
                return httpx.Response(404, json={"detail": "Not Found"})
            if self.inflight_override:
                return self.inflight_override(request)
            return httpx.Response(200, json={
                "in_flight": len(self.in_flight), "routes": self.in_flight,
                "oldest_age_s": 0.0, "draining": self.drain is not None,
                "drain": {"lease_id": self.drain} if self.drain else None,
                "active_jobs": self.jobs, "active_jobs_ok": self.jobs_ok,
                "process": self.process})
        if path == "/api/system/drain":
            body = json.loads(request.content)
            self.drain_calls.append(body)
            assert request.headers.get("X-ATS-Drain-Token") == "t0ken"
            self.drain = body["lease_id"]
            return httpx.Response(200, json={"draining": True})
        if path == "/api/system/drain/release":
            body = json.loads(request.content)
            self.release_calls.append(body)
            if self.drain == body["lease_id"]:
                self.drain = None
            return httpx.Response(200, json={"draining": False})
        return httpx.Response(404)


def _write_config(path, text):
    path.write_text(text)


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    cfg = tmp_path / "operator.toml"
    _write_config(cfg, UNIT_TOML)
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(cfg))
    monkeypatch.delenv("ATS_COORDINATED_REPOS", raising=False)
    monkeypatch.setenv("ATS_DRAIN_TOKEN", "t0ken")
    monkeypatch.setattr(restart_leases, "POLL_INTERVAL_S", 0.05)
    svc = FakeService()
    monkeypatch.setattr(rg, "http_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(svc.handler)))
    ss_out = {"tcp": ""}

    async def fake_ss(args):
        return 0, ss_out["tcp"] if "-Htn" in args else ""

    monkeypatch.setattr(rg, "_run_ss", fake_ss)
    svc.ss = ss_out
    return svc, cfg


async def _restarts(client, unit="svc"):
    rows = (await client.get("/api/restarts", params={"unit": unit, "limit": 50})).json()
    return sorted(rows, key=lambda r: r["created_at"])


async def _cleared(client, unit="svc"):
    lease = (await client.post("/api/restarts/leases",
                               json={"unit": unit, "reason": "x"})).json()["lease"]
    resp = await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()["lease"]


# ── policy (finding 2) ──────────────────────────────────────────────────────


def test_governed_repositories_still_load_with_a_restart_gate_table(gate_env):
    assert governed_repositories() == ["/opt/anime-studio"]


def test_no_config_refuses_everything_but_still_names_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(tmp_path / "absent.toml"))
    cfg = rg.load_gate_config()
    assert cfg.error and "anime-studio" in cfg.governed
    gate, status, why = rg.unit_policy(cfg, "anime-studio.service")
    assert gate is None and status == 503 and "fail closed" in why


@pytest.mark.parametrize("bad,needle", [
    ('[restart_gate]\nbogus = 1\n', "unknown key"),
    ('[restart_gate]\ngoverned_units = ["x"]\n', "no [restart_gate.units"),
    # health-only: no inflight, no jobs, no gpu queue, no drain
    ('[restart_gate]\ngoverned_units = []\n[restart_gate.units."x"]\nlease_ttl_s = 120\n'
     'drain_wait_s = 2\nrestart_timeout_s = 5\nverify_timeout_s = 5\n'
     'probes = [{ kind = "health", url = "http://x/h" }]\n', "lacks required evidence"),
    # partial: inflight + gpu but no drain, not waived
    ('[restart_gate]\ngoverned_units = []\n[restart_gate.units."x"]\nlease_ttl_s = 120\n'
     'drain_wait_s = 2\nrestart_timeout_s = 5\nverify_timeout_s = 5\nprobes = ['
     '{ kind = "inflight", url = "http://x/i" }, { kind = "comfyui", url = "http://x/q" }]\n',
     "['drain']"),
    # TTL that cannot outlive drain + restart + verify
    ('[restart_gate]\ngoverned_units = []\n' + COMPLETE.format(u="x").replace(
        "lease_ttl_s = 120", "lease_ttl_s = 22"), "must exceed"),
    # waiver without a written reason
    ('[restart_gate]\ngoverned_units = []\n' + COMPLETE.format(u="x").replace(
        'probes = [', 'waive = { drain = "no" }\nprobes = ['), "written reason"),
    ('[restart_gate]\ngoverned_units = []\n' + COMPLETE.format(u="x").replace(
        'probes = [', 'waive = { magic = "because reasons" }\nprobes = ['), "unknown evidence"),
    ('[restart_gate.units."x"]\nprobes = [{ url = "ftp://x/q" }]\n', "http(s) URL"),
    ('not toml at all [[[', "cannot read"),
])
def test_an_untrustworthy_or_partial_config_refuses_every_unit(tmp_path, monkeypatch, bad, needle):
    path = tmp_path / "operator.toml"
    path.write_text(bad)
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(path))
    cfg = rg.load_gate_config()
    assert needle in cfg.error, cfg.error
    assert cfg.governed == frozenset(rg.DEFAULT_GOVERNED_UNITS), "fail closed on the defaults"
    gate, status, why = rg.unit_policy(cfg, "x")
    assert gate is None and status == 503 and "fail closed" in why


def test_a_complete_config_and_an_explicit_waiver_are_accepted(tmp_path, monkeypatch):
    text = ('[restart_gate]\ngoverned_units = ["a", "comfy"]\n' + COMPLETE.format(u="a")
            + '[restart_gate.units."comfy"]\nlease_ttl_s = 120\ndrain_wait_s = 2\n'
              'restart_timeout_s = 5\nverify_timeout_s = 5\n'
              'waive = { inflight = "ComfyUI serves no in-flight endpoint", '
              'jobs = "ComfyUI jobs ARE its queue, probed below", '
              'drain = "ComfyUI has no drain endpoint" }\n'
              'probes = [{ kind = "comfyui", url = "http://x.test/q" }]\n')
    path = tmp_path / "operator.toml"
    path.write_text(text)
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(path))
    cfg = rg.load_gate_config()
    assert not cfg.error, cfg.error
    assert cfg.units["comfy"].waived.keys() == {"inflight", "jobs", "drain"}
    assert cfg.units["comfy"].public()["waivers"]["drain"] == "ComfyUI has no drain endpoint"


# ── probe interpretation ────────────────────────────────────────────────────


def _probe(kind="inflight"):
    return rg.Probe(name="p", kind=kind, url="http://p.test/x", timeout_s=0.2)


async def _run(kind, handler, **kw):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await rg.run_probe(c, _probe(kind), **kw)


def _snap(**over):
    base = {"in_flight": 0, "draining": False, "active_jobs": {"generating_shots": 0},
            "active_jobs_ok": True, "process": {"pid": 1, "workers": 1, "mode": "single"}}
    base.update(over)
    return lambda r: httpx.Response(200, json=base)


@pytest.mark.asyncio
@pytest.mark.parametrize("response,status", [
    (lambda r: httpx.Response(200, text="<html>garbage"), "unavailable"),
    (lambda r: httpx.Response(200, json=[1, 2]), "unavailable"),
    (lambda r: httpx.Response(200, json={"in_flight": "0"}), "unavailable"),
    (lambda r: httpx.Response(500, text="boom"), "unavailable"),
    (_snap(active_jobs={"error": "db down"}, active_jobs_ok=False), "unavailable"),
    (_snap(active_jobs={"x": "many"}), "unavailable"),
    (_snap(active_jobs={}), "unavailable"),               # no job evidence, not waived
    (_snap(process=None), "unavailable"),                 # finding 3e: unidentified
    (_snap(process={"pid": 1, "workers": 3, "mode": "supervised"}), "unavailable"),
    (_snap(process={"pid": 1, "workers": None, "mode": "unknown"}), "unavailable"),
    (_snap(active_jobs={"generating_shots": 2}), "busy"),
    (_snap(), "clear"),
])
async def test_inflight_probe_reads_only_a_well_formed_zero_as_clear(response, status):
    assert (await _run("inflight", response)).status == status


@pytest.mark.asyncio
async def test_empty_jobs_pass_only_when_waived():
    res = await _run("inflight", _snap(active_jobs={}), require_jobs=False)
    assert res.status == "clear"


@pytest.mark.asyncio
async def test_a_busy_probe_names_the_request():
    res = await _run("inflight", _snap(in_flight=1, routes=[
        {"method": "POST", "path": "/api/scenes/beats/899/story-render", "age_s": 4.2}]))
    assert res.status == "busy"
    assert "POST /api/scenes/beats/899/story-render" in res.detail


@pytest.mark.asyncio
async def test_unreachable_and_timeout_are_unavailable():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert (await _run("inflight", down)).status == "unavailable"
    res = await _run("comfyui", slow)
    assert res.status == "unavailable" and "timed out" in res.detail


@pytest.mark.asyncio
async def test_comfyui_queue_probe():
    busy = await _run("comfyui", lambda r: httpx.Response(200, json={
        "queue_running": [[0, "abc-prompt", {}]], "queue_pending": []}))
    assert busy.status == "busy" and "abc-prompt" in busy.detail
    clear = await _run("comfyui", lambda r: httpx.Response(200, json={
        "queue_running": [], "queue_pending": []}))
    assert clear.status == "clear"
    bad = await _run("comfyui", lambda r: httpx.Response(200, json={"queue_running": 3}))
    assert bad.status == "unavailable"


@pytest.mark.asyncio
async def test_during_drain_a_lost_drain_is_unavailable_not_clear():
    res = await _run("inflight", _snap(), lease_id="L1", expect_drain=True)
    assert res.status == "unavailable" and "not draining for lease L1" in res.detail
    other = await _run("inflight", _snap(draining=True, drain={"lease_id": "L2"}),
                       lease_id="L1", expect_drain=True)
    assert other.status == "unavailable"


@pytest.mark.asyncio
async def test_probes_in_a_round_run_concurrently(gate_env, monkeypatch):
    """Finding 3c: a round is concurrent, so its wall time is the slowest probe,
    not the sum -- which is what lets a round finish inside the lease TTL."""
    starts = []

    async def slow_probe(client, probe, **kw):
        starts.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.2)
        return rg.ProbeResult(probe.name, probe.kind, probe.url, "clear", "")

    monkeypatch.setattr(rg, "run_probe", slow_probe)
    gate = rg.load_gate_config().units["svc"]
    t0 = asyncio.get_running_loop().time()
    await rg.run_probes(gate)
    assert asyncio.get_running_loop().time() - t0 < 0.35
    assert max(starts) - min(starts) < 0.1


# ── leases over REST ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_busy_service_refuses_the_lease_and_records_the_attempt(client, gate_env):
    svc, _ = gate_env
    svc.in_flight = [{"method": "POST", "path": "/api/scenes/beats/899/story-render",
                      "age_s": 3.0}]
    resp = await client.post("/api/restarts/leases", json={"unit": "svc", "reason": "deploy"})
    assert resp.status_code == 409, resp.text
    assert "/api/scenes/beats/899/story-render" in resp.json()["detail"]
    rows = await _restarts(client)
    assert [r["outcome"] for r in rows] == ["refused"]
    assert rows[0]["before"]["stage"] == "probe"
    assert svc.drain_calls == [], "a refused lease never drains the service"


@pytest.mark.asyncio
async def test_full_flow_records_authorized_attempt_and_outcome(client, gate_env):
    svc, _ = gate_env
    resp = await client.post("/api/restarts/leases", json={"unit": "svc.service",
                                                           "reason": "deploy abc"})
    assert resp.status_code == 201, resp.text
    lease = resp.json()["lease"]
    assert resp.json()["policy"]["scope"] == "user"
    assert lease["waivers"] == {} and lease["mode"] == "normal"

    drained = await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={})
    assert drained.status_code == 200, drained.text
    assert drained.json()["lease"]["state"] == "cleared"
    assert svc.drain_calls and svc.drain_calls[0]["lease_id"] == lease["id"]

    done = await client.post(f"/api/restarts/leases/{lease['id']}/complete", json={
        "outcome": "success", "old_pid": 10, "new_pid": 11, "after": {"health_status": 200}})
    assert done.status_code == 200, done.text
    rows = await _restarts(client)
    assert [r["outcome"] for r in rows] == ["authorized", "completed"]
    assert rows[1]["old_pid"] == 10 and rows[1]["new_pid"] == 11
    assert rows[1]["before"]["attempt_id"] == rows[0]["id"]
    assert svc.release_calls, "completion releases a drain a failed restart would leave behind"

    again = await client.post(f"/api/restarts/leases/{lease['id']}/complete",
                              json={"outcome": "success"})
    assert again.status_code == 409, "one outcome per lease"


@pytest.mark.asyncio
async def test_waivers_are_echoed_and_recorded_and_a_waived_drain_is_skipped(
        client, gate_env):
    svc, cfg = gate_env
    _write_config(cfg, UNIT_TOML.replace(
        'drain_url = "http://svc.test/api/system/drain"\n'
        'release_url = "http://svc.test/api/system/drain/release"\n',
        'waive = { drain = "test unit: no drain endpoint on purpose" }\n'))
    resp = await client.post("/api/restarts/leases", json={"unit": "svc", "reason": "w"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["policy"]["waivers"] == {"drain": "test unit: no drain endpoint on purpose"}
    assert body["lease"]["waivers"] == body["policy"]["waivers"]
    drained = await client.post(f"/api/restarts/leases/{body['lease']['id']}/drain", json={})
    assert drained.status_code == 200 and svc.drain_calls == []
    rows = await _restarts(client)
    assert rows[0]["outcome"] == "authorized"
    assert rows[0]["before"]["waivers"] == body["policy"]["waivers"]
    assert rows[0]["before"]["drained"] is False


@pytest.mark.asyncio
async def test_one_active_lease_per_unit(client, gate_env):
    first = await client.post("/api/restarts/leases", json={"unit": "svc", "reason": "a"})
    second = await client.post("/api/restarts/leases", json={"unit": "svc", "reason": "b"})
    assert first.status_code == 201
    assert second.status_code == 409
    assert first.json()["lease"]["id"] in second.json()["detail"]
    assert [r["outcome"] for r in await _restarts(client)] == ["refused"]


@pytest.mark.asyncio
async def test_concurrent_acquires_admit_exactly_one(gate_env, tmp_path):
    """On a FILE database, where each request has its own connection as in
    production. (The shared in-memory fixture runs every session on one
    connection, so one request's rollback can undo another's commit -- a test
    artifact, not the race under test.)"""
    from sqlalchemy.ext.asyncio import create_async_engine

    from ai_team_sync.database import get_db
    from ai_team_sync.models import Base
    from ai_team_sync.server import create_app

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/race.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override():
        async with factory() as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_db] = override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        results = await asyncio.gather(*[
            c.post("/api/restarts/leases", json={"unit": "svc", "reason": f"r{i}"})
            for i in range(6)])
        codes = sorted(r.status_code for r in results)
        assert codes.count(201) == 1 and codes.count(409) == 5, codes
        rows = await _restarts(c)
        assert [r["outcome"] for r in rows] == ["refused"] * 5
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_ttl_shorter_than_the_sequence_is_refused(client, gate_env):
    resp = await client.post("/api/restarts/leases",
                             json={"unit": "svc", "reason": "x", "ttl_s": 20})
    assert resp.status_code == 422 and "must exceed 22" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_drain_that_never_clears_is_refused_and_released(client, gate_env):
    svc, _ = gate_env
    lease = (await client.post("/api/restarts/leases",
                               json={"unit": "svc", "reason": "x"})).json()["lease"]
    svc.jobs = {"generating_shots": 1}  # work starts after the grant
    resp = await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={"wait_s": 0})
    assert resp.status_code == 409
    assert "generating_shots=1" in resp.json()["detail"]
    assert resp.json()["lease"]["state"] == "refused"
    assert svc.release_calls[-1]["lease_id"] == lease["id"]
    assert svc.drain is None
    assert [r["outcome"] for r in await _restarts(client)] == ["refused"]


@pytest.mark.asyncio
async def test_drain_waits_for_inflight_work_to_finish(client, gate_env):
    svc, _ = gate_env
    lease = (await client.post("/api/restarts/leases",
                               json={"unit": "svc", "reason": "x"})).json()["lease"]
    svc.in_flight = [{"method": "POST", "path": "/w", "age_s": 0.1}]

    async def finish_soon():
        await asyncio.sleep(0.3)
        svc.in_flight = []

    task = asyncio.create_task(finish_soon())
    resp = await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={})
    await task
    assert resp.status_code == 200 and resp.json()["lease"]["state"] == "cleared"


@pytest.mark.asyncio
async def test_unavailable_probe_mid_drain_refuses(client, gate_env):
    svc, _ = gate_env
    lease = (await client.post("/api/restarts/leases",
                               json={"unit": "svc", "reason": "x"})).json()["lease"]
    svc.inflight_override = lambda r: httpx.Response(200, text="garbage")
    resp = await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={})
    assert resp.status_code == 503 and "malformed JSON" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_drain_refused_when_the_service_rejects_it(client, gate_env, monkeypatch):
    monkeypatch.delenv("ATS_DRAIN_TOKEN")
    lease = (await client.post("/api/restarts/leases",
                               json={"unit": "svc", "reason": "x"})).json()["lease"]
    resp = await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={})
    assert resp.status_code == 503 and "no drain token" in resp.json()["detail"]


async def _expire(db_engine, lease_id):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        await s.execute(update(RestartLease).where(RestartLease.id == lease_id).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await s.commit()


@pytest.mark.asyncio
async def test_a_lease_that_expires_during_the_drain_is_never_cleared(client, gate_env,
                                                                      db_engine):
    """Finding 3c, fault injected: the final round comes back clear, but the
    lease expired while the round ran. The commit-time condition refuses it."""
    svc, _ = gate_env
    lease = (await client.post("/api/restarts/leases",
                               json={"unit": "svc", "reason": "x"})).json()["lease"]
    svc.in_flight = [{"method": "POST", "path": "/w", "age_s": 0.1}]

    async def expire_then_clear():
        await asyncio.sleep(0.2)
        await _expire(db_engine, lease["id"])
        svc.in_flight = []

    task = asyncio.create_task(expire_then_clear())
    resp = await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={})
    await task
    assert resp.status_code == 410, resp.text
    assert resp.json()["lease"]["state"] == "expired"
    assert [r["outcome"] for r in await _restarts(client)] == ["refused"]
    assert svc.drain is None, "expiry released the drain"


@pytest.mark.asyncio
async def test_policy_refusals_are_recorded(client, gate_env):
    ungoverned = await client.post("/api/restarts/leases",
                                   json={"unit": "nobody", "reason": "x"})
    assert ungoverned.status_code == 422
    rows = await _restarts(client, "nobody")
    assert rows and rows[0]["outcome"] == "refused" and rows[0]["before"]["stage"] == "policy"


@pytest.mark.asyncio
async def test_complete_requires_a_cleared_lease(client, gate_env):
    lease = (await client.post("/api/restarts/leases",
                               json={"unit": "svc", "reason": "x"})).json()["lease"]
    resp = await client.post(f"/api/restarts/leases/{lease['id']}/complete",
                             json={"outcome": "success"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_release_records_an_aborted_attempt(client, gate_env):
    svc, _ = gate_env
    lease = await _cleared(client)
    resp = await client.post(f"/api/restarts/leases/{lease['id']}/release",
                             json={"reason": "changed my mind"})
    assert resp.json()["lease"]["state"] == "released"
    assert [r["outcome"] for r in await _restarts(client)] == ["authorized", "aborted"]
    assert svc.drain is None
    assert (await client.post("/api/restarts/leases",
                              json={"unit": "svc", "reason": "y"})).status_code == 201


@pytest.mark.asyncio
async def test_renew_extends_the_lease_and_reasserts_the_drain(client, gate_env, db_engine):
    """Finding 3b: the heartbeat keeps BOTH the lease and the service drain alive."""
    svc, _ = gate_env
    lease = await _cleared(client)
    calls_before = len(svc.drain_calls)
    renewed = await client.post(f"/api/restarts/leases/{lease['id']}/renew", json={})
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["drain"] == {"ok": True, "detail": "draining"}
    assert len(svc.drain_calls) == calls_before + 1
    assert svc.drain_calls[-1] == {"lease_id": lease["id"], "ttl_s": 120}
    assert renewed.json()["lease"]["expires_in_s"] >= 119
    await _expire(db_engine, lease["id"])
    gone = await client.post(f"/api/restarts/leases/{lease['id']}/renew", json={})
    assert gone.status_code == 410


@pytest.mark.asyncio
async def test_an_expired_lease_frees_the_unit_and_releases_the_drain(client, gate_env,
                                                                     db_engine):
    svc, _ = gate_env
    lease = await _cleared(client)
    assert svc.drain == lease["id"]
    await _expire(db_engine, lease["id"])

    view = (await client.get(f"/api/restarts/leases/{lease['id']}")).json()
    assert view["state"] == "expired" and not view["active"]
    assert svc.drain is None, "expiry releases the service's drain"
    assert [r["outcome"] for r in await _restarts(client)] == ["authorized", "unknown"]
    late = await client.post(f"/api/restarts/leases/{lease['id']}/complete",
                             json={"outcome": "success", "new_pid": 5})
    assert late.status_code == 200
    assert [r["outcome"] for r in await _restarts(client)] == ["authorized", "unknown",
                                                              "completed"]
    assert (await client.post("/api/restarts/leases",
                              json={"unit": "svc", "reason": "y"})).status_code == 201


@pytest.mark.asyncio
async def test_the_sweeper_settles_expired_leases_without_any_api_call(client, gate_env,
                                                                     db_engine):
    """Finding 5: no reliance on a later API call touching the lease."""
    svc, _ = gate_env
    lease = await _cleared(client)
    await _expire(db_engine, lease["id"])
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    assert await restart_leases.sweep_expired(factory) == 1
    assert svc.drain is None
    assert [r["outcome"] for r in await _restarts(client)] == ["authorized", "unknown"]
    assert await restart_leases.sweep_expired(factory) == 0, "idempotent"


@pytest.mark.asyncio
async def test_the_sweeper_task_runs_on_an_interval(gate_env, db_engine, monkeypatch, client):
    svc, _ = gate_env
    lease = await _cleared(client)
    await _expire(db_engine, lease["id"])
    monkeypatch.setenv("ATS_RESTART_SWEEP_S", "0.2")
    monkeypatch.setattr(restart_leases, "_sweeper", None)
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    task = restart_leases.start_sweeper(factory)
    try:
        for _ in range(50):
            if svc.drain is None:
                break
            await asyncio.sleep(0.05)
        assert svc.drain is None
    finally:
        task.cancel()
    assert [r["outcome"] for r in await _restarts(client)] == ["authorized", "unknown"]


# ── bootstrap (finding 7) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_grants_on_full_evidence_and_records_the_mode(client, gate_env):
    svc, _ = gate_env
    svc.middleware_loaded = False
    resp = await client.post("/api/restarts/leases",
                             json={"unit": "svc", "reason": "first load", "bootstrap": True})
    assert resp.status_code == 201, resp.text
    lease = resp.json()["lease"]
    assert lease["mode"] == "bootstrap"
    drained = await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={})
    assert drained.status_code == 200, drained.text
    assert svc.drain_calls == [], "bootstrap cannot drain (the middleware is not loaded)"
    rows = await _restarts(client)
    assert rows[0]["outcome"] == "authorized" and rows[0]["before"]["mode"] == "bootstrap"
    names = {p["name"] for p in rows[0]["before"]["probes"]}
    assert {"api-absent", "gpu", "bootstrap-jobs", "connections"} <= names


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,status,needle", [
    ("loaded", 409, "ALREADY loaded"),
    ("connections", 409, "ESTABLISHED"),
    ("jobs_busy", 409, "generating_shots=3"),
    ("jobs_garbage", 503, "non-empty object of counts"),
    ("gpu", 409, "GPU queue not empty"),
])
async def test_bootstrap_refuses_on_each_missing_condition(client, gate_env, fault, status,
                                                          needle):
    svc, _ = gate_env
    svc.middleware_loaded = fault == "loaded"
    if fault == "connections":
        svc.ss["tcp"] = "0 0 127.0.0.1:18401 127.0.0.1:51234\n"
    if fault == "jobs_busy":
        svc.bootstrap_jobs = {"generating_shots": 3}
    if fault == "jobs_garbage":
        svc.bootstrap_jobs = ["no"]
    if fault == "gpu":
        svc.queue = {"queue_running": [[0, "p1"]], "queue_pending": []}
    resp = await client.post("/api/restarts/leases",
                             json={"unit": "svc", "reason": "first", "bootstrap": True})
    assert resp.status_code == status, resp.text
    assert needle in resp.json()["detail"]
    assert (await _restarts(client))[0]["outcome"] == "refused"


@pytest.mark.asyncio
async def test_bootstrap_sql_evidence_fails_closed_when_the_db_is_unreachable():
    boot = rg.BootstrapGate(port=1, jobs_kind="sql",
                            jobs_dsn="postgresql://nobody@127.0.0.1:1/none",
                            jobs_queries=(("n", "SELECT 0"),), jobs_timeout_s=1)
    res = await rg._bootstrap_jobs(boot)
    assert res.status == "unavailable"


def test_bootstrap_sql_dsn_may_not_carry_a_password(tmp_path, monkeypatch):
    text = ('[restart_gate]\ngoverned_units = []\n' + COMPLETE.format(u="a")
            + '[restart_gate.units."a".bootstrap]\nport = 1\n'
              'jobs = { kind = "sql", dsn = "postgresql://u:secret@h/db", '
              'queries = { n = "SELECT 0" } }\n')
    path = tmp_path / "operator.toml"
    path.write_text(text)
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(path))
    assert "password" in rg.load_gate_config().error


# ── the safe-restart driver ─────────────────────────────────────────────────


class FakeAts:
    def __init__(self, acquire=201, drain=200, complete=200, renew=200):
        self.codes = {"acquire": acquire, "drain": drain, "complete": complete,
                      "renew": renew}
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if request.url.host == "svc.test":
            self.calls.append(("health", {}))
            return httpx.Response(200)
        name = path.rsplit("/", 1)[-1] if path != "/api/restarts/leases" else "acquire"
        self.calls.append((name, body))
        code = self.codes.get(name, 200)
        lease = {"id": "L1", "state": "x", "mode": "normal", "ttl_s": 30,
                 "expires_in_s": 100, "waivers": {}, "probes": [
                     {"name": "api", "status": "busy", "detail": "POST /w in flight"}]}
        if code >= 400:
            return httpx.Response(code, json={"detail": f"{name} refused", "lease": lease})
        return httpx.Response(code, json={
            "lease": lease, "outcome_restart_id": "R2", "drain": {"ok": True},
            "policy": {
                "unit": "svc", "scope": "user", "health_url": "http://svc.test/health",
                "drain_wait_s": 1, "verify_timeout_s": 5, "restart_timeout_s": 5,
                "waivers": {}}})

    def completes(self):
        return [b for n, b in self.calls if n == "complete"]


class FakeSystemd:
    def __init__(self, restart_rc=0, pid_after=200, state_after="active", on_restart=None):
        self.pid, self.state = 100, "active"
        self.restart_rc, self.pid_after, self.state_after = restart_rc, pid_after, state_after
        self.on_restart = on_restart
        self.argvs: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.argvs.append(argv)
        if "restart" in argv:
            if self.on_restart:
                self.on_restart()
            if self.restart_rc == 0:
                self.pid, self.state = self.pid_after, self.state_after
            return subprocess.CompletedProcess(argv, self.restart_rc, "", "boom")
        return subprocess.CompletedProcess(argv, 0, f"ActiveState={self.state}\nMainPID={self.pid}\n", "")


def _drive(ats, systemd, **kw):
    lines: list[str] = []
    ticks = iter(range(10_000))
    code = sr.run_safe_restart(
        "svc", "deploy", server="http://ats.test", runner=systemd,
        http=httpx.Client(transport=httpx.MockTransport(ats.handler)),
        out=lines.append, sleep=lambda s: None, clock=lambda: float(next(ticks)), **kw)
    return code, lines


def test_safe_restart_success_path():
    ats, systemd = FakeAts(), FakeSystemd()
    code, lines = _drive(ats, systemd)
    assert code == 0, lines
    assert ["systemctl", "--user", "restart", "svc"] in systemd.argvs
    complete = ats.completes()[0]
    assert complete["outcome"] == "success"
    assert (complete["old_pid"], complete["new_pid"]) == (100, 200)


def test_safe_restart_refused_lease_never_restarts():
    ats, systemd = FakeAts(acquire=409), FakeSystemd()
    code, lines = _drive(ats, systemd)
    assert code == sr.EXIT_BUSY
    assert not any("restart" in a for a in systemd.argvs)
    assert any("POST /w in flight" in line for line in lines)


def test_safe_restart_refused_drain_never_restarts():
    ats, systemd = FakeAts(drain=503), FakeSystemd()
    code, _ = _drive(ats, systemd)
    assert code == sr.EXIT_UNAVAILABLE
    assert not any("restart" in a for a in systemd.argvs)


def test_safe_restart_failed_restart_is_recorded_as_failed():
    ats, systemd = FakeAts(), FakeSystemd(restart_rc=1)
    code, _ = _drive(ats, systemd)
    assert code == sr.EXIT_FAILED
    assert ats.completes()[0]["outcome"] == "failed"


def test_safe_restart_same_pid_is_a_timeout_not_a_success():
    ats, systemd = FakeAts(), FakeSystemd(pid_after=100)
    code, _ = _drive(ats, systemd)
    assert code == sr.EXIT_FAILED
    assert ats.completes()[0]["outcome"] == "timeout"


def test_safe_restart_reports_an_unrecorded_outcome():
    ats, systemd = FakeAts(complete=500), FakeSystemd()
    code, _ = _drive(ats, systemd)
    assert code == sr.EXIT_UNRECORDED


def _raise(exc):
    def go():
        raise exc
    return go


def test_any_exception_after_authorization_reports_failed():
    """Finding 5: not only TimeoutExpired."""
    ats = FakeAts()
    code, lines = _drive(ats, FakeSystemd(on_restart=_raise(OSError("exec format error"))))
    assert code == sr.EXIT_FAILED, lines
    (complete,) = ats.completes()
    assert complete["outcome"] == "failed"
    assert "OSError: exec format error" in complete["after"]["exception"]


def test_a_signal_mid_restart_reports_before_exit():
    ats = FakeAts()
    code, _ = _drive(ats, FakeSystemd(on_restart=_raise(sr.Interrupted("signal 15"))))
    assert code == sr.EXIT_INTERRUPTED
    (complete,) = ats.completes()
    assert complete["outcome"] == "failed" and "signal 15" in complete["after"]["exception"]


def test_keyboard_interrupt_is_reported_and_reraised():
    ats = FakeAts()
    with pytest.raises(KeyboardInterrupt):
        _drive(ats, FakeSystemd(on_restart=_raise(KeyboardInterrupt())))
    assert ats.completes()[0]["outcome"] == "failed"


def test_a_lost_lease_before_the_restart_aborts_without_restarting(monkeypatch):
    ats, systemd = FakeAts(), FakeSystemd()

    def lost_start(self):
        self.lost = "HTTP 410: lease expired"
        return self

    monkeypatch.setattr(sr.Heartbeat, "start", lost_start)
    monkeypatch.setattr(sr.Heartbeat, "stop", lambda self: None)
    code, _ = _drive(ats, systemd)
    assert code == sr.EXIT_LOCAL
    assert not any("restart" in a for a in systemd.argvs)
    assert ats.completes()[0]["outcome"] == "aborted"


def test_the_heartbeat_renews_during_a_slow_restart():
    """Finding 3b: renewal runs THROUGH systemctl, not just before it."""
    import time as _time

    ats = FakeAts()
    code, _ = _drive(ats, FakeSystemd(on_restart=lambda: _time.sleep(0.35)),
                     heartbeat_interval=0.05)
    assert code == 0
    assert sum(1 for n, _ in ats.calls if n == "renew") >= 3
    assert ats.completes()[0]["after"]["heartbeat_renewals"] >= 3


def test_verification_budget_starts_after_a_slow_restart_returns():
    """Found by acceptance: a 25 s stop consumed the whole 10 s verify budget
    because the deadline was counted from before `systemctl restart`."""
    now = {"t": 0.0}

    def jump():
        now["t"] += 1000.0  # the restart itself took "forever"

    def clock():
        now["t"] += 0.1
        return now["t"]

    ats = FakeAts()
    code = sr.run_safe_restart(
        "svc", "slow", server="http://ats.test", runner=FakeSystemd(on_restart=jump),
        http=httpx.Client(transport=httpx.MockTransport(ats.handler)),
        out=lambda s: None, sleep=lambda s: None, clock=clock)
    assert code == 0
    assert ats.completes()[0]["outcome"] == "success"


def test_system_units_restart_through_noninteractive_sudo():
    assert sr.restart_argv("system", "anime-studio") == [
        "sudo", "-n", "systemctl", "restart", "anime-studio"]
