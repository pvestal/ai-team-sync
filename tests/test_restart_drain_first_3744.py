"""#3744: drain first, then wait -- a busy service can still be restarted safely.

Measured 2026-11-15 19:30-20:25 PT: 177 `ats safe-restart tower-echo-brain`
attempts were refused, every one at stage 'probe' (the ACQUIRE round), because
continuous MCP traffic and worker cycles meant the probe never saw a moment with
zero requests and zero jobs in flight. The drain that would have stopped new
admissions was never started.

Contract under test:
  * On a unit whose drain is configured and not waived, requests and jobs in
    flight at acquire read DRAINABLE: the lease is granted, /drain stops new
    admissions and waits (bounded) for that work, and the restart is authorized
    only by a final round with in_flight == 0 and every job count 0.
  * If the bound expires, the refusal names the requests/jobs that held it and
    the drain is released.
  * Everything that refused at acquire for any other reason still does:
    unknown evidence, a busy GPU queue, a drain held by another lease, and any
    unit whose drain is waived (nothing would stop new work there).

Reuses the in-process FakeService fixture of the #3580 tests.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ai_team_sync import restart_gate as rg

from .test_restart_gate_3580 import UNIT_TOML, _restarts, _write_config, gate_env  # noqa: F401

ROUTE = {"method": "POST", "path": "/mcp", "age_s": 0.4}


async def _acquire(client, reason="deploy"):
    return await client.post("/api/restarts/leases", json={"unit": "svc", "reason": reason})


# ── probe reading ───────────────────────────────────────────────────────────


def _snap(**over):
    base = {"in_flight": 0, "routes": [], "draining": False, "drain": None,
            "active_jobs": {"worker:x": 0}, "active_jobs_ok": True,
            "process": {"pid": 1, "workers": 1}}
    return {**base, **over}


def test_drain_first_reads_work_in_flight_as_drainable_and_names_it():
    status, detail = rg._interpret_inflight(
        _snap(in_flight=1, routes=[ROUTE], active_jobs={"worker:ingest": 1}),
        "L1", expect_drain=False, drain_first=True)
    assert status == "drainable"
    assert "POST /mcp" in detail and "worker:ingest=1" in detail


def test_without_drain_first_the_same_snapshot_is_still_busy():
    status, _ = rg._interpret_inflight(_snap(in_flight=1, routes=[ROUTE]), "L1",
                                       expect_drain=False, drain_first=False)
    assert status == "busy"


def test_drain_first_never_turns_unknown_evidence_or_a_foreign_drain_into_drainable():
    foreign = rg._interpret_inflight(
        _snap(in_flight=1, routes=[ROUTE], draining=True, drain={"lease_id": "OTHER"}),
        "L1", expect_drain=False, drain_first=True)
    assert foreign[0] == "busy" and "OTHER" in foreign[1]
    jobs_unknown = rg._interpret_inflight(_snap(active_jobs_ok=False, active_jobs={"error": "x"}),
                                          "L1", expect_drain=False, drain_first=True)
    assert jobs_unknown[0] == "unavailable"
    multi = rg._interpret_inflight(_snap(process={"pid": 1, "workers": 2}), "L1",
                                   expect_drain=False, drain_first=True)
    assert multi[0] == "unavailable"


def test_overall_orders_drainable_below_busy_and_unavailable():
    def r(status):
        return rg.ProbeResult("p", "inflight", "u", status, "")
    assert rg.overall([r("clear"), r("drainable")]) == "drainable"
    assert rg.overall([r("drainable"), r("busy")]) == "busy"
    assert rg.overall([r("drainable"), r("unavailable")]) == "unavailable"


# ── lease flow ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_busy_drain_unit_is_granted_drained_and_cleared_once_work_finishes(
        client, gate_env):
    svc, _ = gate_env
    svc.in_flight = [ROUTE]
    svc.jobs = {"generating_shots": 1}

    resp = await _acquire(client)
    assert resp.status_code == 201, resp.text
    lease = resp.json()["lease"]
    api = next(p for p in lease["probes"] if p["name"] == "api")
    assert api["status"] == "drainable" and "POST /mcp" in api["detail"]
    assert svc.drain_calls == [], "granting does not drain; /drain does"

    async def work_finishes():
        while not svc.drain:  # the drain starts BEFORE the work is gone
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        assert svc.in_flight, "work was still in flight when the drain began"
        svc.in_flight, svc.jobs = [], {"generating_shots": 0}

    task = asyncio.create_task(work_finishes())
    drained = await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={})
    await task
    assert drained.status_code == 200, drained.text
    assert drained.json()["lease"]["state"] == "cleared"
    assert svc.drain_calls[0]["lease_id"] == lease["id"]

    rows = await _restarts(client)
    assert [r["outcome"] for r in rows] == ["authorized"]
    before = rows[0]["before"]
    assert before["stage"] == "cleared" and before["drained"] is True
    at_grant = {p["name"]: p for p in before["at_grant"]}
    assert at_grant["api"]["status"] == "drainable"
    assert "POST /mcp" in at_grant["api"]["detail"]
    assert before["drain_wait_s"] >= 0.2
    final = {p["name"]: p for p in before["probes"]}
    assert final["api"]["status"] == "clear", "authorized only on a clear final round"


@pytest.mark.asyncio
async def test_work_that_outlasts_the_bound_refuses_names_it_and_releases_the_drain(
        client, gate_env):
    svc, _ = gate_env
    svc.in_flight = [{"method": "POST", "path": "/api/echo/session/save", "age_s": 1.0}]
    svc.jobs = {"worker:domain_ingestor": 1}
    lease = (await _acquire(client)).json()["lease"]

    resp = await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={"wait_s": 0})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "drain did not clear" in detail
    assert "POST /api/echo/session/save" in detail and "worker:domain_ingestor=1" in detail
    assert resp.json()["lease"]["state"] == "refused"
    assert svc.release_calls[-1]["lease_id"] == lease["id"] and svc.drain is None
    rows = await _restarts(client)
    assert [r["outcome"] for r in rows] == ["refused"]
    assert rows[0]["before"]["stage"] == "drain"


@pytest.mark.asyncio
async def test_a_busy_gpu_queue_still_refuses_at_acquire_without_draining(client, gate_env):
    svc, _ = gate_env
    svc.in_flight = [ROUTE]  # drainable on its own ...
    svc.queue = {"queue_running": [[0, "p-1"]], "queue_pending": []}  # ... but not this
    resp = await _acquire(client)
    assert resp.status_code == 409, resp.text
    assert "GPU queue not empty" in resp.json()["detail"]
    rows = await _restarts(client)
    assert [r["outcome"] for r in rows] == ["refused"]
    assert rows[0]["before"]["stage"] == "probe"
    assert svc.drain_calls == []


@pytest.mark.asyncio
async def test_a_drain_held_by_another_lease_still_refuses_at_acquire(client, gate_env):
    svc, _ = gate_env
    svc.drain = "someone-elses-lease"
    resp = await _acquire(client)
    assert resp.status_code == 409 and "someone-elses-lease" in resp.json()["detail"]
    assert svc.drain_calls == []


@pytest.mark.asyncio
async def test_unknown_evidence_still_refuses_at_acquire(client, gate_env):
    svc, _ = gate_env
    svc.in_flight = [ROUTE]
    svc.jobs_ok = False
    resp = await _acquire(client)
    assert resp.status_code == 503, resp.text
    assert "could not be determined" in resp.json()["detail"]
    assert svc.drain_calls == []


@pytest.mark.asyncio
async def test_a_unit_whose_drain_is_waived_keeps_wait_for_idle(client, gate_env):
    svc, cfg = gate_env
    _write_config(cfg, UNIT_TOML.replace(
        'drain_url = "http://svc.test/api/system/drain"\n'
        'release_url = "http://svc.test/api/system/drain/release"\n',
        'waive = { drain = "test unit: no drain endpoint on purpose" }\n'))
    svc.in_flight = [ROUTE]
    resp = await _acquire(client)
    assert resp.status_code == 409, "nothing would stop new work, so busy still refuses"
    assert "POST /mcp" in resp.json()["detail"]
    rows = await _restarts(client)
    assert rows[0]["before"]["stage"] == "probe"


@pytest.mark.asyncio
async def test_bootstrap_is_unchanged_by_drain_first(client, gate_env):
    svc, _ = gate_env
    svc.middleware_loaded = False
    svc.bootstrap_jobs = {"generating_shots": 1}
    resp = await client.post("/api/restarts/leases",
                             json={"unit": "svc", "reason": "b", "bootstrap": True})
    assert resp.status_code == 409 and "generating_shots=1" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_new_work_arriving_after_the_grant_is_waited_for_too(client, gate_env):
    """The grant is not a promise that the work seen is the only work: anything
    the service admitted before the drain started is counted and waited for."""
    svc, _ = gate_env
    lease = (await _acquire(client)).json()["lease"]
    svc.in_flight = [ROUTE]  # admitted after the grant, before the drain

    async def finish():
        await asyncio.sleep(0.25)
        svc.in_flight = []

    task = asyncio.create_task(finish())
    resp = await client.post(f"/api/restarts/leases/{lease['id']}/drain", json={})
    await task
    assert resp.status_code == 200 and resp.json()["lease"]["state"] == "cleared"


def test_drains_first_only_for_normal_leases_on_units_with_a_live_drain():
    gate = rg.UnitGate(unit="u", drain_url="http://x/d", release_url="http://x/r")
    assert rg.drains_first(gate, bootstrap=False) is True
    assert rg.drains_first(gate, bootstrap=True) is False
    assert rg.drains_first(rg.UnitGate(unit="u"), bootstrap=False) is False
    waived = rg.UnitGate(unit="u", drain_url="http://x/d", release_url="http://x/r",
                         waivers=(("drain", "no drain endpoint here"),))
    assert rg.drains_first(waived, bootstrap=False) is False


def test_the_wrapper_reports_what_it_is_draining_first():
    """safe_restart prints the at-grant drainable work, so the operator's
    terminal shows what the drain is waiting for."""
    from ai_team_sync import safe_restart as sr

    lines: list[str] = []
    lease = {"id": "L", "mode": "normal", "expires_in_s": 100, "ttl_s": 100,
             "probes": [{"name": "api", "status": "drainable", "detail": "1 request(s) in flight: POST /mcp"}]}
    policy = {"unit": "svc", "scope": "user", "drain_wait_s": 1, "waivers": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/restarts/leases":
            return httpx.Response(201, json={"lease": lease, "policy": policy})
        if request.url.path.endswith("/drain"):
            return httpx.Response(409, json={"detail": "drain did not clear within 1s: x"})
        return httpx.Response(200, json={})

    class Proc:
        returncode, stdout, stderr = 0, "ActiveState=active\nMainPID=7\n", ""

    rc = sr.run_safe_restart("svc", "r", server="http://ats.test",
                             runner=lambda argv, t: Proc(),
                             http=httpx.Client(transport=httpx.MockTransport(handler)),
                             out=lines.append)
    assert rc == sr.EXIT_BUSY
    assert any("draining first -- api: 1 request(s) in flight: POST /mcp" in line
               for line in lines), lines


# ── cooldown: a retry loop cannot keep a busy service drained ──────────────


@pytest.mark.asyncio
async def test_after_a_drain_times_out_the_unit_is_not_drained_again_during_cooldown(
        client, gate_env):
    svc, _ = gate_env
    svc.jobs = {"worker:domain_ingestor": 1}  # outlasts every drain
    first = (await _acquire(client)).json()["lease"]
    timed_out = await client.post(f"/api/restarts/leases/{first['id']}/drain",
                                  json={"wait_s": 0})
    assert timed_out.status_code == 409 and "drain did not clear" in timed_out.json()["detail"]
    drains_so_far = len(svc.drain_calls)

    retry = await _acquire(client, reason="retry loop")
    assert retry.status_code == 409, retry.text
    detail = retry.json()["detail"]
    assert detail.startswith("cooldown:") and first["id"] in detail
    assert "worker:domain_ingestor=1" in detail, "names what is still holding it"
    assert len(svc.drain_calls) == drains_so_far, "a cooling unit is never drained"
    rows = await _restarts(client)
    assert rows[-1]["before"]["stage"] == "cooldown"

    svc.jobs = {"worker:domain_ingestor": 0}  # idle: the old wait-for-idle grant
    idle = await _acquire(client, reason="idle moment")
    assert idle.status_code == 201, idle.text


@pytest.mark.asyncio
async def test_drain_first_resumes_once_the_cooldown_has_passed(client, gate_env, db_engine):
    from sqlalchemy import update
    from ai_team_sync.restart_lease_model import RestartLease

    svc, _ = gate_env
    svc.jobs = {"worker:domain_ingestor": 1}
    first = (await _acquire(client)).json()["lease"]
    await client.post(f"/api/restarts/leases/{first['id']}/drain", json={"wait_s": 0})
    async with db_engine.begin() as conn:  # the timeout was long ago
        await conn.execute(update(RestartLease).where(RestartLease.id == first["id"])
                           .values(finished_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    again = await _acquire(client, reason="after cooldown")
    assert again.status_code == 201, again.text
    api = next(p for p in again.json()["lease"]["probes"] if p["name"] == "api")
    assert api["status"] == "drainable"


@pytest.mark.asyncio
async def test_other_refusals_start_no_cooldown(client, gate_env):
    svc, _ = gate_env
    svc.queue = {"queue_running": [[0, "p-1"]], "queue_pending": []}
    assert (await _acquire(client)).status_code == 409  # GPU busy: refused at probe
    svc.queue = {"queue_running": [], "queue_pending": []}
    svc.in_flight = [ROUTE]
    assert (await _acquire(client)).status_code == 201, "no drain timed out, so no cooldown"
