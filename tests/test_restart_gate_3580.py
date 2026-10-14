"""Restart safety gate (#3580): policy, probes, leases, and the safe-restart driver.

Operator ruling 2026-10-13: governed restarts are REFUSED while requests, jobs or
GPU queues are in flight; the check and the restart cannot race (lease + drain);
a failed or unavailable check refuses; attempts AND outcomes are recorded.

These are in-process tests: probes and drain calls go through an
httpx.MockTransport standing in for the service. The same flows against real
processes (a disposable systemd --user service carrying the anime-studio
middleware, a real ats-server, truly concurrent clients) are in
test_restart_gate_acceptance_3580.py.
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

[restart_gate.units."svc"]
scope = "user"
health_url = "http://svc.test/health"
drain_url = "http://svc.test/api/system/drain"
release_url = "http://svc.test/api/system/drain/release"
lease_ttl_s = 120
drain_wait_s = 2
probes = [
  { name = "api", kind = "inflight", url = "http://svc.test/api/system/inflight" },
  { name = "gpu", kind = "comfyui", url = "http://gpu.test/queue" },
]
"""


class FakeService:
    """The anime-studio inflight contract + a ComfyUI /queue, in memory."""

    def __init__(self):
        self.in_flight: list[dict] = []
        self.jobs: dict = {"generating_shots": 0}
        self.jobs_ok = True
        self.queue = {"queue_running": [], "queue_pending": []}
        self.drain: str | None = None
        self.drain_calls: list[dict] = []
        self.release_calls: list[dict] = []
        self.inflight_override = None  # callable(request) -> httpx.Response
        self.clear_on_drain = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "gpu.test":
            return httpx.Response(200, json=self.queue)
        if path == "/api/system/inflight":
            if self.inflight_override:
                return self.inflight_override(request)
            return httpx.Response(200, json={
                "in_flight": len(self.in_flight), "routes": self.in_flight,
                "oldest_age_s": 0.0, "draining": self.drain is not None,
                "drain": {"lease_id": self.drain} if self.drain else None,
                "active_jobs": self.jobs, "active_jobs_ok": self.jobs_ok})
        if path == "/api/system/drain":
            body = json.loads(request.content)
            self.drain_calls.append(body)
            assert request.headers.get("X-ATS-Drain-Token") == "t0ken"
            self.drain = body["lease_id"]
            if self.clear_on_drain:
                self.in_flight = []
            return httpx.Response(200, json={"draining": True})
        if path == "/api/system/drain/release":
            body = json.loads(request.content)
            self.release_calls.append(body)
            if self.drain == body["lease_id"]:
                self.drain = None
            return httpx.Response(200, json={"draining": False})
        return httpx.Response(404)


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    cfg = tmp_path / "operator.toml"
    cfg.write_text(UNIT_TOML)
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(cfg))
    monkeypatch.delenv("ATS_COORDINATED_REPOS", raising=False)
    monkeypatch.setenv("ATS_DRAIN_TOKEN", "t0ken")
    monkeypatch.setattr(restart_leases, "POLL_INTERVAL_S", 0.05)
    svc = FakeService()
    monkeypatch.setattr(rg, "http_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(svc.handler)))
    return svc, cfg


async def _restarts(client, unit="svc"):
    rows = (await client.get("/api/restarts", params={"unit": unit, "limit": 50})).json()
    return sorted(rows, key=lambda r: r["created_at"])


# ── policy ──────────────────────────────────────────────────────────────────


def test_governed_repositories_still_load_with_a_restart_gate_table(gate_env):
    assert governed_repositories() == ["/opt/anime-studio"]


def test_no_config_governs_the_defaults_and_refuses_them(tmp_path, monkeypatch):
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(tmp_path / "absent.toml"))
    cfg = rg.load_gate_config()
    assert not cfg.error and "anime-studio" in cfg.governed
    gate, status, why = rg.unit_policy(cfg, "anime-studio.service")
    assert gate is None and status == 503 and "no probes configured" in why
    assert rg.unit_policy(cfg, "some-other-unit")[1] == 422


@pytest.mark.parametrize("bad", [
    '[restart_gate]\nbogus = 1\n',
    '[restart_gate.units."x"]\nprobes = []\n',
    '[restart_gate.units."x"]\nprobes = [{ url = "ftp://x/q" }]\n',
    '[restart_gate.units."x"]\nprobes = [{ url = "http://x/q", kind = "magic" }]\n',
    '[restart_gate.units."x"]\nprobes = [{ url = "http://x/q" }]\nsurprise = true\n',
    '[restart_gate.units."x"]\nprobes = [{ url = "http://x/q" }]\ndrain_url = "http://x/d"\n',
    'not toml at all [[[',
])
def test_an_untrustworthy_config_refuses_every_unit(tmp_path, monkeypatch, bad):
    path = tmp_path / "operator.toml"
    path.write_text(bad)
    monkeypatch.setenv("ATS_OPERATOR_CONFIG", str(path))
    cfg = rg.load_gate_config()
    assert cfg.error
    assert cfg.governed == frozenset(rg.DEFAULT_GOVERNED_UNITS), "fail closed on the defaults"
    gate, status, why = rg.unit_policy(cfg, "x")
    assert gate is None and status == 503 and "fail closed" in why


# ── probe interpretation ────────────────────────────────────────────────────


def _probe(kind="inflight"):
    return rg.Probe(name="p", kind=kind, url="http://p.test/x", timeout_s=0.2)


async def _run(kind, handler, **kw):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await rg.run_probe(c, _probe(kind), **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("response,status", [
    (lambda r: httpx.Response(200, text="<html>garbage"), "unavailable"),
    (lambda r: httpx.Response(200, json=[1, 2]), "unavailable"),
    (lambda r: httpx.Response(200, json={"in_flight": "0"}), "unavailable"),
    (lambda r: httpx.Response(500, text="boom"), "unavailable"),
    (lambda r: httpx.Response(200, json={"in_flight": 0, "draining": False,
                                         "active_jobs": {"error": "db down"},
                                         "active_jobs_ok": False}), "unavailable"),
    (lambda r: httpx.Response(200, json={"in_flight": 0, "draining": False,
                                         "active_jobs": {"x": "many"},
                                         "active_jobs_ok": True}), "unavailable"),
    (lambda r: httpx.Response(200, json={"in_flight": 0, "draining": False,
                                         "active_jobs": {"generating_shots": 2},
                                         "active_jobs_ok": True}), "busy"),
    (lambda r: httpx.Response(200, json={"in_flight": 0, "draining": False,
                                         "active_jobs": {}, "active_jobs_ok": True}), "clear"),
])
async def test_inflight_probe_reads_only_a_well_formed_zero_as_clear(response, status):
    assert (await _run("inflight", response)).status == status


@pytest.mark.asyncio
async def test_a_busy_probe_names_the_request():
    res = await _run("inflight", lambda r: httpx.Response(200, json={
        "in_flight": 1, "draining": False, "active_jobs": {}, "active_jobs_ok": True,
        "routes": [{"method": "POST", "path": "/api/scenes/beats/899/story-render",
                    "age_s": 4.2}]}))
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
    res = await _run("inflight", lambda r: httpx.Response(200, json={
        "in_flight": 0, "draining": False, "active_jobs": {}, "active_jobs_ok": True}),
        lease_id="L1", expect_drain=True)
    assert res.status == "unavailable" and "not draining for lease L1" in res.detail


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


@pytest.mark.asyncio
async def test_policy_refusals_are_recorded(client, gate_env, monkeypatch):
    ungoverned = await client.post("/api/restarts/leases",
                                   json={"unit": "nobody", "reason": "x"})
    assert ungoverned.status_code == 422
    governed = await client.post("/api/restarts/leases",
                                 json={"unit": "anime-studio", "reason": "x"})
    assert governed.status_code == 503 and "no probes configured" in governed.json()["detail"]
    rows = await _restarts(client, "anime-studio")
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
    lease = (await client.post("/api/restarts/leases",
                               json={"unit": "svc", "reason": "x"})).json()["lease"]
    await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={})
    resp = await client.post(f"/api/restarts/leases/{lease['id']}/release",
                             json={"reason": "changed my mind"})
    assert resp.json()["lease"]["state"] == "released"
    assert [r["outcome"] for r in await _restarts(client)] == ["authorized", "aborted"]
    assert svc.drain is None
    # The unit is free again.
    assert (await client.post("/api/restarts/leases",
                              json={"unit": "svc", "reason": "y"})).status_code == 201


async def _expire(db_engine, lease_id):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        await s.execute(update(RestartLease).where(RestartLease.id == lease_id).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await s.commit()


@pytest.mark.asyncio
async def test_an_expired_lease_frees_the_unit_and_releases_the_drain(client, gate_env,
                                                                     db_engine):
    svc, _ = gate_env
    lease = (await client.post("/api/restarts/leases",
                               json={"unit": "svc", "reason": "x"})).json()["lease"]
    await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={})
    assert svc.drain == lease["id"]
    await _expire(db_engine, lease["id"])

    view = (await client.get(f"/api/restarts/leases/{lease['id']}")).json()
    assert view["state"] == "expired" and not view["active"]
    assert svc.drain is None, "expiry releases the service's drain"
    assert [r["outcome"] for r in await _restarts(client)] == ["authorized", "unknown"]
    # A late report still lands, beside the 'unknown' row rather than over it.
    late = await client.post(f"/api/restarts/leases/{lease['id']}/complete",
                             json={"outcome": "success", "new_pid": 5})
    assert late.status_code == 200
    assert [r["outcome"] for r in await _restarts(client)] == ["authorized", "unknown",
                                                              "completed"]
    assert (await client.post("/api/restarts/leases",
                              json={"unit": "svc", "reason": "y"})).status_code == 201


# ── the safe-restart driver ─────────────────────────────────────────────────


class FakeAts:
    def __init__(self, acquire=201, drain=200, complete=200):
        self.codes = {"acquire": acquire, "drain": drain, "complete": complete}
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
        lease = {"id": "L1", "state": "x", "expires_in_s": 100, "probes": [
            {"name": "api", "status": "busy", "detail": "POST /w in flight"}]}
        if code >= 400:
            return httpx.Response(code, json={"detail": f"{name} refused", "lease": lease})
        return httpx.Response(code, json={
            "lease": lease, "outcome_restart_id": "R2", "policy": {
                "unit": "svc", "scope": "user", "health_url": "http://svc.test/health",
                "drain_wait_s": 1, "verify_timeout_s": 5, "restart_timeout_s": 5}})


class FakeSystemd:
    def __init__(self, restart_rc=0, pid_after=200, state_after="active"):
        self.pid, self.state = 100, "active"
        self.restart_rc, self.pid_after, self.state_after = restart_rc, pid_after, state_after
        self.argvs: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.argvs.append(argv)
        if "restart" in argv:
            if self.restart_rc == 0:
                self.pid, self.state = self.pid_after, self.state_after
            return subprocess.CompletedProcess(argv, self.restart_rc, "", "boom")
        return subprocess.CompletedProcess(argv, 0, f"ActiveState={self.state}\nMainPID={self.pid}\n", "")


def _drive(ats, systemd):
    lines: list[str] = []
    ticks = iter(range(10_000))
    code = sr.run_safe_restart(
        "svc", "deploy", server="http://ats.test", runner=systemd,
        http=httpx.Client(transport=httpx.MockTransport(ats.handler)),
        out=lines.append, sleep=lambda s: None, clock=lambda: float(next(ticks)))
    return code, lines


def test_safe_restart_success_path():
    ats, systemd = FakeAts(), FakeSystemd()
    code, lines = _drive(ats, systemd)
    assert code == 0, lines
    assert ["systemctl", "--user", "restart", "svc"] in systemd.argvs
    complete = [b for n, b in ats.calls if n == "complete"][0]
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
    assert [b for n, b in ats.calls if n == "complete"][0]["outcome"] == "failed"


def test_safe_restart_same_pid_is_a_timeout_not_a_success():
    ats, systemd = FakeAts(), FakeSystemd(pid_after=100)
    code, _ = _drive(ats, systemd)
    assert code == sr.EXIT_FAILED
    assert [b for n, b in ats.calls if n == "complete"][0]["outcome"] == "timeout"


def test_safe_restart_reports_an_unrecorded_outcome():
    ats, systemd = FakeAts(complete=500), FakeSystemd()
    code, _ = _drive(ats, systemd)
    assert code == sr.EXIT_UNRECORDED


def test_system_units_restart_through_noninteractive_sudo():
    assert sr.restart_argv("system", "anime-studio") == [
        "sudo", "-n", "systemctl", "restart", "anime-studio"]
