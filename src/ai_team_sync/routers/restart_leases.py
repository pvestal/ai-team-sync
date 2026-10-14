"""Restart leases: the part of #3580 that can REFUSE a governed restart.

routers/restarts.py records restarts after the fact and refuses nothing. A lease
is the authorization a governed restart is made under:

    POST /api/restarts/leases                   acquire: one active lease per unit
                                                (DB-enforced), then probe; refused
                                                if anything is in flight or unknown
    POST /api/restarts/leases/{id}/drain        ask the service to stop admitting
                                                writes, poll until in-flight work,
                                                active jobs and GPU queues are all
                                                empty -> 'cleared' (bounded wait)
    POST /api/restarts/leases/{id}/renew        heartbeat: extend the lease AND
                                                re-assert the service drain
    POST /api/restarts/leases/{id}/complete     the caller restarted; record outcome
    POST /api/restarts/leases/{id}/release      abandon without restarting
    GET  /api/restarts/leases[/{id}]            history / one lease

EVIDENCE. Every decision lands in service_restarts, refusals included:
  * ATTEMPT row, written once when the attempt is decided -- outcome 'refused'
    (with the failing probe in before_state) or 'authorized' (drain cleared);
    'aborted' if the holder released it before clearing.
  * OUTCOME row, written by /complete -- 'completed' | 'failed' | 'timeout' |
    'aborted' -- or by expiry: 'unknown' (the lease expired with no report; a
    restart may or may not have happened).
Both carry the lease mode and the unit's waivers. Rows are never rewritten.

EXPIRY IS EAGER. start_sweeper() (wired into the server lifespan) settles every
expired live lease on an interval -- writing 'unknown' for an authorized one and
releasing the service drain -- so a wrapper killed with SIGKILL still leaves an
outcome, without waiting for anyone to call the API again.

WHAT THIS DOES NOT DO. It does not perform the restart (safe_restart.py does),
and nothing here can stop a restart made without a lease. See
docs/restart-gate.md for the service-control boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync import restart_gate as rg
from ai_team_sync.database import get_db
from ai_team_sync.models import ServiceRestart, Session
from ai_team_sync.restart_lease_model import ACTIVE_STATES, RestartLease

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/restarts/leases", tags=["restarts"])

#: Seconds between probe rounds while waiting for a drain to clear. A module
#: attribute so tests can shorten it.
POLL_INTERVAL_S = 0.5
#: Seconds between expiry sweeps (env ATS_RESTART_SWEEP_S overrides).
DEFAULT_SWEEP_S = 15.0


class LeaseAcquire(BaseModel):
    unit: str
    reason: str
    session_id: str | None = None
    developer: str = ""
    ttl_s: int | None = Field(default=None, ge=1, le=rg.MAX_TTL_S)
    bootstrap: bool = False

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        v = rg.normalize_unit(v)
        if not v:
            raise ValueError("unit must be a systemd unit name")
        return v

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("a governed restart needs a reason")
        return v


class LeaseDrain(BaseModel):
    wait_s: int | None = Field(default=None, ge=0, le=rg.MAX_TTL_S)


class LeaseComplete(BaseModel):
    outcome: Literal["success", "failed", "timeout", "aborted"]
    old_pid: int | None = None
    new_pid: int | None = None
    after: dict = Field(default_factory=dict)


class LeaseRelease(BaseModel):
    reason: str = ""


_OUTCOME_ROW = {"success": "completed", "failed": "failed", "timeout": "timeout",
                "aborted": "aborted"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite hands back naive datetimes; values are written as UTC.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _remaining_s(lease: RestartLease) -> float:
    return (_aware(lease.expires_at) - _now()).total_seconds()


def _loads(raw: str, default):
    try:
        value = json.loads(raw or "")
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _view(lease: RestartLease) -> dict:
    iso = lambda d: _aware(d).isoformat() if d else None  # noqa: E731
    return {
        "id": lease.id,
        "unit": lease.unit,
        "state": lease.state,
        "mode": lease.mode,
        "waivers": _loads(lease.waivers, {}),
        "active": lease.state in ACTIVE_STATES,
        "session_id": lease.session_id,
        "developer": lease.developer,
        "reason": lease.reason,
        "ttl_s": lease.ttl_s,
        "refusal": lease.refusal,
        "probes": _loads(lease.probes, []),
        "attempt_restart_id": lease.attempt_restart_id,
        "outcome_restart_id": lease.outcome_restart_id,
        "created_at": iso(lease.created_at),
        "expires_at": iso(lease.expires_at),
        "expires_in_s": round(_remaining_s(lease), 1),
        "cleared_at": iso(lease.cleared_at),
        "finished_at": iso(lease.finished_at),
    }


def _new_service_restart(lease: RestartLease, outcome: str, before: dict,
                         after: dict | None = None, old_pid: int | None = None,
                         new_pid: int | None = None) -> ServiceRestart:
    return ServiceRestart(
        unit=lease.unit,
        session_id=lease.session_id,
        developer=lease.developer,
        reason=lease.reason,
        outcome=outcome,
        old_pid=old_pid,
        new_pid=new_pid,
        before_state=json.dumps({"lease_id": lease.id, "mode": lease.mode,
                                 "waivers": _loads(lease.waivers, {}), **before}),
        after_state=json.dumps(after or {}),
    )


async def _record(db: AsyncSession, lease: RestartLease, outcome: str, before: dict,
                  **kw) -> ServiceRestart:
    row = _new_service_restart(lease, outcome, before, **kw)
    db.add(row)
    await db.flush()
    return row


def _finish(lease: RestartLease, state: str) -> None:
    lease.state = state
    lease.active_unit = None
    lease.finished_at = _now()


def _gate_for(unit: str) -> rg.UnitGate | None:
    gate, _status, _why = rg.unit_policy(rg.load_gate_config(), unit)
    return gate


async def _release_quietly(lease: RestartLease) -> None:
    if lease.mode != "normal":
        return
    gate = _gate_for(lease.unit)
    if gate is not None and gate.release_url:
        await rg.release_drain(gate, lease.id)


async def _settle_expired(db: AsyncSession, lease: RestartLease) -> bool:
    """Expire a live lease past its TTL, writing what is known. True if expired now.

    The DATABASE decides, not the in-memory row: a /renew can commit an extension
    after the caller (the sweeper, say) loaded this lease as due. So expiry is a
    conditional UPDATE -- id, still active, expires_at <= now -- and the record,
    the cleared active_unit and the drain release happen only if it matched.
    The in-memory check below is just a cheap filter for leases that are not due.
    """
    if lease.state not in ACTIVE_STATES or _remaining_s(lease) > 0:
        return False
    now = _now()
    moved = await db.execute(
        update(RestartLease)
        .where(RestartLease.id == lease.id, RestartLease.state.in_(ACTIVE_STATES),
               RestartLease.expires_at <= now)
        .values(state="expired", active_unit=None, finished_at=now)
        .execution_options(synchronize_session=False))
    if moved.rowcount != 1:
        await db.rollback()
        await db.refresh(lease)  # renewed, completed or released meanwhile
        return False
    await db.refresh(lease)  # the row as this transaction now holds it
    if lease.cleared_at is not None:
        if lease.outcome_restart_id is None:
            row = await _record(db, lease, "unknown", {
                "attempt_id": lease.attempt_restart_id,
                "note": "lease expired with no completion report; a restart may or "
                        "may not have happened"})
            lease.outcome_restart_id = row.id
    elif lease.attempt_restart_id is None:
        lease.refusal = lease.refusal or f"lease expired in state {lease.state} before the drain cleared"
        row = await _record(db, lease, "refused", {"stage": "expired", "refusal": lease.refusal})
        lease.attempt_restart_id = row.id
    await db.commit()
    await _release_quietly(lease)
    return True


async def _refuse_new(db: AsyncSession, body: LeaseAcquire, developer: str, status: int,
                      refusal: str, stage: str, extra: dict | None = None,
                      gate: rg.UnitGate | None = None) -> JSONResponse:
    """A refusal before any lease held the unit: still a lease row + attempt row."""
    lease = RestartLease(unit=body.unit, active_unit=None, state="refused",
                         mode="bootstrap" if body.bootstrap else "normal",
                         waivers=json.dumps(gate.waived if gate else {}),
                         session_id=body.session_id, developer=developer, reason=body.reason,
                         ttl_s=body.ttl_s or 0, refusal=refusal, expires_at=_now(),
                         finished_at=_now())
    db.add(lease)
    await db.flush()
    row = await _record(db, lease, "refused", {"stage": stage, "refusal": refusal, **(extra or {})})
    lease.attempt_restart_id = row.id
    await db.commit()
    return JSONResponse(status_code=status, content={"detail": refusal, "lease": _view(lease)})


async def _refuse_lease(db: AsyncSession, lease: RestartLease, status: int, refusal: str,
                        results: list[rg.ProbeResult], stage: str) -> JSONResponse:
    await db.refresh(lease)
    if lease.state in ACTIVE_STATES:
        lease.refusal = refusal
        if results:
            lease.probes = json.dumps([r.as_dict() for r in results])
        if lease.attempt_restart_id is None:
            row = await _record(db, lease, "refused", {
                "stage": stage, "refusal": refusal,
                "probes": [r.as_dict() for r in results]})
            lease.attempt_restart_id = row.id
        _finish(lease, "refused")
        await db.commit()
    await _release_quietly(lease)
    return JSONResponse(status_code=status, content={"detail": refusal, "lease": _view(lease)})


async def _load(db: AsyncSession, lease_id: str) -> RestartLease:
    lease = await db.get(RestartLease, lease_id)
    if lease is None:
        raise HTTPException(404, "Lease not found")
    return lease


async def _round(gate: rg.UnitGate, lease: RestartLease, expect_drain: bool):
    if lease.mode == "bootstrap":
        return await rg.run_bootstrap_checks(gate)
    return await rg.run_probes(gate, lease_id=lease.id, expect_drain=expect_drain)


@router.post("", status_code=201)
async def acquire_lease(body: LeaseAcquire, db: AsyncSession = Depends(get_db)):
    developer = body.developer
    if body.session_id:
        session = await db.get(Session, body.session_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        developer = developer or session.developer

    gate, status, refusal = rg.unit_policy(rg.load_gate_config(), body.unit)
    if gate is None:
        return await _refuse_new(db, body, developer, status, refusal, "policy")
    if body.bootstrap and gate.bootstrap is None:
        return await _refuse_new(db, body, developer, 503,
                                 f"{body.unit} has no [bootstrap] evidence configured",
                                 "policy", gate=gate)
    ttl = body.ttl_s or gate.lease_ttl_s
    if ttl <= gate.min_ttl_s():
        return await _refuse_new(
            db, body, developer, 422,
            f"ttl_s={ttl} must exceed {gate.min_ttl_s()} (drain_wait + restart + verify + "
            f"{rg.TTL_MARGIN_S}s) for {body.unit}", "policy", gate=gate)

    holder = (await db.execute(
        select(RestartLease).where(RestartLease.active_unit == body.unit))).scalar_one_or_none()
    if holder is not None:
        await _settle_expired(db, holder)

    lease = RestartLease(unit=body.unit, active_unit=body.unit, state="probing",
                         mode="bootstrap" if body.bootstrap else "normal",
                         waivers=json.dumps(gate.waived),
                         session_id=body.session_id, developer=developer, reason=body.reason,
                         ttl_s=ttl, expires_at=_now() + timedelta(seconds=ttl))
    db.add(lease)
    try:
        await db.commit()
    except IntegrityError:
        # The UNIQUE(active_unit) constraint is the arbiter: exactly one INSERT
        # for a unit commits while a lease is live, however close the race.
        await db.rollback()
        holder = (await db.execute(
            select(RestartLease).where(RestartLease.active_unit == body.unit))).scalar_one_or_none()
        who = (f"lease {holder.id} ({holder.state}, developer {holder.developer or '?'}, "
               f"session {holder.session_id or '-'}, expires in {round(_remaining_s(holder))}s): "
               f"{holder.reason}") if holder else "a concurrent lease"
        return await _refuse_new(db, body, developer, 409,
                                 f"{body.unit} is already leased by {who}", "lease",
                                 {"holder": holder.id if holder else None}, gate=gate)

    results = await _round(gate, lease, expect_drain=False)
    verdict = rg.overall(results)
    if verdict != "clear":
        return await _refuse_lease(db, lease, 503 if verdict == "unavailable" else 409,
                                   rg.summarize(results), results, "probe")
    await db.refresh(lease)
    if lease.state != "probing":
        raise HTTPException(409, f"lease left 'probing' concurrently (now {lease.state})")
    lease.state = "granted"
    lease.probes = json.dumps([r.as_dict() for r in results])
    await db.commit()
    return JSONResponse(status_code=201, content={"lease": _view(lease), "policy": gate.public()})


@router.post("/{lease_id}/drain")
async def drain_lease(lease_id: str, body: LeaseDrain | None = None,
                      db: AsyncSession = Depends(get_db)):
    body = body or LeaseDrain()
    lease = await _load(db, lease_id)
    if await _settle_expired(db, lease):
        return JSONResponse(status_code=410, content={"detail": "lease expired",
                                                      "lease": _view(lease)})
    if lease.state == "cleared":
        gate = _gate_for(lease.unit)
        return {"lease": _view(lease), "policy": gate.public() if gate else None}
    if lease.state != "granted":
        raise HTTPException(409, f"lease is {lease.state}; only a granted lease can drain")

    gate, status, refusal = rg.unit_policy(rg.load_gate_config(), lease.unit)
    if gate is None:
        return await _refuse_lease(db, lease, status, refusal, [], "policy")

    moved = await db.execute(update(RestartLease)
                             .where(RestartLease.id == lease.id, RestartLease.state == "granted")
                             .values(state="draining"))
    await db.commit()
    if moved.rowcount != 1:
        raise HTTPException(409, "lease changed state concurrently")
    await db.refresh(lease)

    drains = lease.mode == "normal" and "drain" not in gate.waived
    remaining = _remaining_s(lease)
    if drains:
        ok, why = await rg.request_drain(gate, lease.id,
                                         max(1, min(rg.MAX_TTL_S, math.ceil(remaining))))
        if not ok:
            return await _refuse_lease(db, lease, 503, why, [], "drain")

    wait = min(body.wait_s if body.wait_s is not None else gate.drain_wait_s,
               max(0.0, remaining - 1))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        # A FINAL round: every probe (run concurrently) must say clear, and the
        # in-flight probe must show the drain held by THIS lease.
        results = await _round(gate, lease, expect_drain=drains)
        verdict = rg.overall(results)
        if verdict == "clear":
            break
        if verdict == "unavailable":
            return await _refuse_lease(db, lease, 503, rg.summarize(results), results, "drain")
        if loop.time() >= deadline:
            return await _refuse_lease(
                db, lease, 409, f"drain did not clear within {wait:.0f}s: {rg.summarize(results)}",
                results, "drain")
        await asyncio.sleep(POLL_INTERVAL_S)

    # Clear ONLY if the lease is still draining AND unexpired at commit time:
    # the condition is evaluated inside the same transaction as the attempt row.
    probes = [r.as_dict() for r in results]
    row = _new_service_restart(lease, "authorized", {
        "stage": "cleared", "drained": drains, "probes": probes})
    db.add(row)
    await db.flush()
    cleared = await db.execute(
        update(RestartLease)
        .where(RestartLease.id == lease.id, RestartLease.state == "draining",
               RestartLease.expires_at > _now())
        .values(state="cleared", cleared_at=_now(), attempt_restart_id=row.id,
                probes=json.dumps(probes))
        # The row is re-read below; never evaluate the time predicate in Python
        # (SQLite returns naive datetimes).
        .execution_options(synchronize_session=False))
    if cleared.rowcount != 1:
        await db.rollback()
        await db.refresh(lease)
        if await _settle_expired(db, lease):
            return JSONResponse(status_code=410, content={
                "detail": "lease expired before the clear could be committed",
                "lease": _view(lease)})
        return await _refuse_lease(db, lease, 409,
                                   f"lease left 'draining' while waiting (now {lease.state})",
                                   results, "drain")
    await db.commit()
    await db.refresh(lease)
    return {"lease": _view(lease), "policy": gate.public()}


@router.post("/{lease_id}/renew")
async def renew_lease(lease_id: str, db: AsyncSession = Depends(get_db)):
    """Heartbeat from the restarting caller: push the expiry out by the lease
    TTL and re-assert the service drain (same lease id refreshes its TTL). A
    failed drain re-assertion is reported, not fatal: once systemctl has
    stopped the old process there is nothing left to drain."""
    lease = await _load(db, lease_id)
    if await _settle_expired(db, lease):
        return JSONResponse(status_code=410, content={"detail": "lease expired",
                                                      "lease": _view(lease)})
    if lease.state not in ("granted", "draining", "cleared"):
        raise HTTPException(409, f"lease is {lease.state}; nothing to renew")
    new_expiry = _now() + timedelta(seconds=lease.ttl_s)
    renewed = await db.execute(
        update(RestartLease)
        .where(RestartLease.id == lease.id, RestartLease.state.in_(("granted", "draining", "cleared")),
               RestartLease.expires_at > _now())
        .values(expires_at=new_expiry)
        .execution_options(synchronize_session=False))
    await db.commit()

    async def still_live() -> bool:
        # Re-read AFTER commit: 200 means the lease is live now, not that an
        # UPDATE matched at some earlier instant.
        await db.refresh(lease)
        return lease.state in ACTIVE_STATES and _remaining_s(lease) > 0

    if renewed.rowcount != 1 or not await still_live():
        await _settle_expired(db, lease)
        return JSONResponse(status_code=410, content={"detail": "lease expired or finished",
                                                      "lease": _view(lease)})
    drain = None
    gate = _gate_for(lease.unit)
    if lease.mode == "normal" and gate is not None and gate.drain_url \
            and "drain" not in gate.waived and lease.state in ("draining", "cleared"):
        ok, why = await rg.request_drain(gate, lease.id, min(rg.MAX_TTL_S, lease.ttl_s))
        drain = {"ok": ok, "detail": why}
        # The re-assertion is a network round trip; the lease can be settled
        # (sweeper, release, complete) while it runs. A drain re-asserted for a
        # lease that is no longer live must not be left standing.
        if not await still_live():
            await _release_quietly(lease)
            return JSONResponse(status_code=410, content={
                "detail": "lease ended while the drain was being re-asserted",
                "lease": _view(lease), "drain": drain})
    return {"lease": _view(lease), "drain": drain}


@router.post("/{lease_id}/complete")
async def complete_lease(lease_id: str, body: LeaseComplete,
                         db: AsyncSession = Depends(get_db)):
    lease = await _load(db, lease_id)
    await _settle_expired(db, lease)
    # A lease that cleared and then expired before the caller reported still
    # takes the real outcome: the 'unknown' row stays as history beside it.
    if lease.state == "completed":
        raise HTTPException(409, "lease already has an outcome")
    if not (lease.state == "cleared" or (lease.state == "expired" and lease.cleared_at)):
        raise HTTPException(409, f"lease is {lease.state}; only a cleared lease records an outcome")
    row = await _record(db, lease, _OUTCOME_ROW[body.outcome], {
        "attempt_id": lease.attempt_restart_id,
        "superseded_unknown": lease.outcome_restart_id}, after=body.after,
        old_pid=body.old_pid, new_pid=body.new_pid)
    lease.outcome_restart_id = row.id
    _finish(lease, "completed")
    await db.commit()
    # A failed restart leaves the OLD process up and still draining.
    await _release_quietly(lease)
    return {"lease": _view(lease), "outcome_restart_id": row.id, "outcome": row.outcome}


@router.post("/{lease_id}/release")
async def release_lease(lease_id: str, body: LeaseRelease | None = None,
                        db: AsyncSession = Depends(get_db)):
    body = body or LeaseRelease()
    lease = await _load(db, lease_id)
    if await _settle_expired(db, lease) or lease.state not in ACTIVE_STATES:
        return {"lease": _view(lease)}
    note = {"note": body.reason or "released by the holder without restarting"}
    if lease.cleared_at is not None:
        row = await _record(db, lease, "aborted", {"attempt_id": lease.attempt_restart_id, **note})
        lease.outcome_restart_id = row.id
    else:
        row = await _record(db, lease, "aborted", {"stage": lease.state, **note})
        lease.attempt_restart_id = row.id
    _finish(lease, "released")
    await db.commit()
    await _release_quietly(lease)
    return {"lease": _view(lease)}


@router.get("")
async def list_leases(unit: str = Query(""), active: bool = Query(False),
                      limit: int = Query(20, ge=1, le=200),
                      db: AsyncSession = Depends(get_db)):
    stmt = select(RestartLease).order_by(RestartLease.created_at.desc()).limit(limit)
    if unit:
        stmt = stmt.where(RestartLease.unit == rg.normalize_unit(unit))
    leases = (await db.execute(stmt)).scalars().all()
    for lease in leases:
        await _settle_expired(db, lease)
    if active:
        leases = [lease for lease in leases if lease.state in ACTIVE_STATES]
    return [_view(lease) for lease in leases]


@router.get("/{lease_id}")
async def get_lease(lease_id: str, db: AsyncSession = Depends(get_db)):
    lease = await _load(db, lease_id)
    await _settle_expired(db, lease)
    return _view(lease)


# ── eager expiry ────────────────────────────────────────────────────────────


async def sweep_expired(session_factory) -> int:
    """Settle every live lease past its TTL. Returns how many were settled."""
    settled = 0
    async with session_factory() as db:
        due = (await db.execute(
            select(RestartLease.id).where(RestartLease.active_unit.is_not(None),
                                          RestartLease.expires_at <= _now()))).scalars().all()
    # One session per lease: a settlement that loses its race rolls back, and a
    # rollback must not expire the other rows this sweep still has to read.
    for lease_id in due:
        async with session_factory() as db:
            try:
                lease = await db.get(RestartLease, lease_id)
                if lease is not None and await _settle_expired(db, lease):
                    settled += 1
            except Exception:  # noqa: BLE001 -- one bad row must not stop the sweep
                await db.rollback()
                logger.exception("restart lease sweep failed for %s", lease_id)
    return settled


_sweeper: asyncio.Task | None = None


def sweep_interval_s() -> float:
    try:
        return max(0.2, float(os.environ.get("ATS_RESTART_SWEEP_S", DEFAULT_SWEEP_S)))
    except ValueError:
        return DEFAULT_SWEEP_S


async def _sweep_forever(session_factory, interval: float) -> None:
    while True:
        try:
            n = await sweep_expired(session_factory)
            if n:
                logger.warning("restart lease sweeper settled %d expired lease(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("restart lease sweep failed")
        await asyncio.sleep(interval)


def start_sweeper(session_factory=None) -> asyncio.Task:
    """Started from the server lifespan. Idempotent per event loop."""
    global _sweeper
    if _sweeper is not None and not _sweeper.done():
        return _sweeper
    if session_factory is None:
        from ai_team_sync.database import async_session as session_factory
    _sweeper = asyncio.get_running_loop().create_task(
        _sweep_forever(session_factory, sweep_interval_s()), name="restart-lease-sweeper")
    return _sweeper
