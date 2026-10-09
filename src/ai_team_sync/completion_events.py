"""Session-completion events for Echo Brain: contract v1 + a durable outbox (#3537).

Every completed session -- owner-completed or reaped -- produces exactly one
event, built here and nowhere else. The event is written to the
``completion_outbox`` table in the SAME transaction as the completion, so it is
durable before anything is sent. Delivery happens after the commit, by
``deliver_pending``: a 2xx from Echo means Echo has persisted it; anything else
is retried with the same ``event_id`` and backoff, and stays visible in
``health()`` until it lands. Session completion never waits on Echo.

The event carries the handoff itself, so Echo never needs this session row to
ingest it.

Events from an ephemeral (in-memory) ATS database are never delivered: a
session that lives only in a test process must not reach a live consumer.
That is how the ATS test suite sent 8,441 phantom completions to Echo.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.models import CompletionOutbox, Handoff, Session

logger = logging.getLogger(__name__)

EVENT_TYPE = "ats.session.completed"
EVENT_VERSION = 1
OWNER_COMPLETED = "owner_completed"
REAPED = "reaped"
DEFAULT_ECHO_URL = "http://localhost:8309"
RETRY_BASE_S = 30
RETRY_CAP_S = 3600
# Echo answers these for an event it will never accept; retrying cannot help.
_REJECTED = {409, 422}

_inflight: set[asyncio.Task] = set()


# ── contract ──────────────────────────────────────────────────────────────

def canonical(obj: Any) -> bytes:
    """The one serialisation both services hash. Echo's copy must match."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def handoff_content_sha256(handoff: dict[str, Any]) -> str:
    return sha256({k: handoff[k] for k in ("verdict", "blockers", "next_steps", "artifacts")})


def iso_utc(ts: datetime) -> str:
    """SQLite hands back naive datetimes; every one ATS writes is UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="microseconds")


def event_id_for(session_id: str, completed_at: str) -> str:
    """Stable per logical completion: a retry reuses it, a second completion of
    a resurrected session gets its own."""
    return f"{EVENT_TYPE}/v{EVENT_VERSION}/{session_id}/{completed_at}"


def _json_list(text: str | None) -> list[str]:
    value = json.loads(text or "[]")
    return [str(v) for v in value] if isinstance(value, list) else []


def handoff_payload(handoff: Handoff) -> dict[str, Any]:
    body = {
        "handoff_id": handoff.id,
        "ticket_id": handoff.ticket_id,
        "verdict": handoff.verdict,
        "blockers": _json_list(handoff.blockers),
        "next_steps": _json_list(handoff.next_steps),
        "artifacts": _json_list(handoff.artifacts),
        "created_at": iso_utc(handoff.created_at),
    }
    body["content_sha256"] = handoff_content_sha256(body)
    return body


def _source() -> dict[str, Any]:
    from ai_team_sync.build_info import identity
    ident = identity("rest")
    return {"service": "ai-team-sync", "version": ident.get("version", "unknown"),
            "commit": ident.get("commit", "unknown")}


def build_event(session: Session, handoff: Handoff | None, reason: str,
                *, source: dict[str, Any] | None = None) -> dict[str, Any]:
    if reason not in (OWNER_COMPLETED, REAPED):
        raise ValueError(f"unknown completion reason {reason!r}")
    if session.completed_at is None:
        raise ValueError(f"session {session.id} has no completed_at")
    completed_at = iso_utc(session.completed_at)
    return {
        "event_type": EVENT_TYPE,
        "event_version": EVENT_VERSION,
        "event_id": event_id_for(session.id, completed_at),
        "session_id": session.id,
        "ticket_id": session.ticket_id,
        "task_id": session.task_id,
        "completed_at": completed_at,
        "completion_reason": reason,
        "agent": session.agent or "",
        "repo_root": session.repo_root or "",
        "summary": session.summary or "",
        "handoff": handoff_payload(handoff) if handoff is not None else None,
        "source": source if source is not None else _source(),
    }


# ── outbox ────────────────────────────────────────────────────────────────

def emission_enabled() -> bool:
    return os.environ.get("ATS_EMIT_COMPLETION", "1") != "0"


def echo_url() -> str:
    return os.environ.get("ECHO_BRAIN_URL", DEFAULT_ECHO_URL).rstrip("/")


def ephemeral_database() -> bool:
    """True when this ATS keeps its state in memory (a test process)."""
    from ai_team_sync.config import settings
    url = settings.database_url
    return url.startswith("sqlite") and (url.endswith("://") or ":memory:" in url)


async def record_completion(db: AsyncSession, session: Session, reason: str) -> str | None:
    """Add this completion's event to the outbox, inside the caller's transaction.

    Call it after the completion fields (and any handoff) are set and before the
    commit, so the event and the state it describes land together or not at all.
    Returns the event_id, or None when emission is switched off.
    """
    if not emission_enabled():
        return None
    handoff = (await db.execute(
        select(Handoff).where(Handoff.source_session_id == session.id))).scalar_one_or_none()
    event = build_event(session, handoff, reason)
    if await db.get(CompletionOutbox, event["event_id"]) is None:
        db.add(CompletionOutbox(
            event_id=event["event_id"], session_id=session.id, completion_reason=reason,
            has_handoff=handoff is not None, payload=json.dumps(event),
            payload_sha256=sha256(event)))
    return event["event_id"]


async def _post(url: str, payload: dict[str, Any]) -> int:
    """The one network call. Tests replace it; nothing else sends."""
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
        return resp.status_code


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(RETRY_BASE_S * 2 ** max(attempts - 1, 0), RETRY_CAP_S))


async def deliver_pending(db: AsyncSession, *, limit: int = 50) -> dict[str, int]:
    """Send every due, undelivered event once. Commits per event."""
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(CompletionOutbox)
        .where(CompletionOutbox.delivered_at.is_(None), CompletionOutbox.rejected_at.is_(None),
               CompletionOutbox.suppressed_reason.is_(None),
               CompletionOutbox.next_attempt_at <= now)
        .order_by(CompletionOutbox.created_at).limit(limit))).scalars().all()
    out = {"due": len(rows), "delivered": 0, "failed": 0, "rejected": 0, "suppressed": 0}
    if not rows:
        return out
    if ephemeral_database():
        for row in rows:
            row.suppressed_reason = "ephemeral ATS database: session cannot be read back"
        out["suppressed"] = len(rows)
        await db.commit()
        return out

    url = f"{echo_url()}/api/ats/session-completed"
    for row in rows:
        row.attempts += 1
        row.last_attempt_at = datetime.now(timezone.utc)
        try:
            status = await _post(url, json.loads(row.payload))
        except Exception as exc:  # noqa: BLE001 -- Echo down is a retry, not an error
            status, error = None, f"{type(exc).__name__}: {exc}"
        else:
            error = f"HTTP {status}"
        if status is not None and 200 <= status < 300:
            row.delivered_at = row.last_attempt_at
            row.last_error = None
            out["delivered"] += 1
        elif status in _REJECTED:
            row.rejected_at = row.last_attempt_at
            row.last_error = error
            out["rejected"] += 1
            logger.error("completion event %s REJECTED by Echo (%s); not retried",
                         row.event_id, error)
        else:
            row.last_error = error[:2000]
            row.next_attempt_at = row.last_attempt_at + _backoff(row.attempts)
            out["failed"] += 1
            logger.warning("completion event %s not delivered (attempt %d, %s); retry at %s",
                           row.event_id, row.attempts, error, row.next_attempt_at.isoformat())
        await db.commit()
    return out


def kick() -> None:
    """Deliver now rather than at the next loop tick. Never raises, never blocks.

    The task is held in a module set until it finishes: an unreferenced task can
    be garbage-collected mid-flight, which is one way the old emitter lost events.
    """
    if not emission_enabled() or ephemeral_database():
        return

    async def _run() -> None:
        from ai_team_sync.database import async_session
        try:
            async with async_session() as db:
                await deliver_pending(db)
        except Exception as exc:  # noqa: BLE001 -- the loop retries
            logger.warning("completion delivery kick failed: %s", exc)

    try:
        task = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        return
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


async def health(db: AsyncSession) -> dict[str, Any]:
    """Outbox counters: what was emitted, what landed, what is stuck and why."""
    O = CompletionOutbox
    pending = (O.delivered_at.is_(None) & O.rejected_at.is_(None) & O.suppressed_reason.is_(None))
    row = (await db.execute(select(
        func.count(),
        func.count().filter(O.delivered_at.is_not(None)),
        func.count().filter(pending),
        func.count().filter(pending & (O.attempts > 0)),
        func.count().filter(O.rejected_at.is_not(None)),
        func.count().filter(O.suppressed_reason.is_not(None)),
        func.count().filter(O.has_handoff.is_(True)),
        func.count().filter(O.completion_reason == REAPED),
        func.coalesce(func.sum(O.attempts), 0) - func.count().filter(O.attempts > 0),
        func.min(O.created_at).filter(pending),
    ))).one()
    oldest = row[9]
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    last_error = (await db.execute(
        select(O.event_id, O.last_error, O.last_attempt_at)
        .where(pending, O.last_error.is_not(None))
        .order_by(O.last_attempt_at.desc()).limit(1))).first()
    return {
        "emitted": row[0], "delivered": row[1], "pending": row[2],
        "delivery_failing": row[3], "rejected": row[4], "suppressed": row[5],
        "with_handoff": row[6], "reaped": row[7], "retries": int(row[8]),
        "oldest_pending_s": (int((datetime.now(timezone.utc) - oldest).total_seconds())
                             if oldest else None),
        "last_error": ({"event_id": last_error[0], "error": last_error[1],
                        "at": iso_utc(last_error[2])} if last_error else None),
        "echo_url": echo_url(), "emission_enabled": emission_enabled(),
    }


# ── historical replay ─────────────────────────────────────────────────────

async def replay_plan(db: AsyncSession, *, handoffs_only: bool = True) -> list[dict[str, Any]]:
    """Events for completed sessions that never had one in the outbox.

    Built by the same build_event as live completions, so a replayed event is
    the event the session would have produced. Deterministic: rerunning finds
    nothing new once the rows exist.
    """
    q = select(Session).where(Session.status == "completed", Session.completed_at.is_not(None))
    if handoffs_only:
        q = q.where(Session.id.in_(select(Handoff.source_session_id)))
    out = []
    for s in (await db.execute(q.order_by(Session.completed_at))).scalars().all():
        handoff = (await db.execute(
            select(Handoff).where(Handoff.source_session_id == s.id))).scalar_one_or_none()
        event = build_event(s, handoff, REAPED if s.auto_completed else OWNER_COMPLETED)
        if await db.get(CompletionOutbox, event["event_id"]) is None:
            out.append(event)
    return out


async def replay_apply(db: AsyncSession, events: list[dict[str, Any]]) -> int:
    for event in events:
        db.add(CompletionOutbox(
            event_id=event["event_id"], session_id=event["session_id"],
            completion_reason=event["completion_reason"],
            has_handoff=event["handoff"] is not None, payload=json.dumps(event),
            payload_sha256=sha256(event), replayed=True))
    await db.commit()
    return len(events)


def main(argv: list[str] | None = None) -> int:
    """python -m ai_team_sync.completion_events replay [--apply] [--all-sessions]"""
    import argparse
    p = argparse.ArgumentParser(prog="ai_team_sync.completion_events")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="queue v1 events for completed sessions that never had one")
    r.add_argument("--apply", action="store_true", help="write outbox rows (default: dry run)")
    r.add_argument("--all-sessions", action="store_true",
                   help="include sessions without a handoff (default: handoffs only)")
    sub.add_parser("health", help="print outbox counters")
    args = p.parse_args(argv)

    async def run() -> int:
        from ai_team_sync.database import async_session, init_db
        await init_db()
        async with async_session() as db:
            if args.cmd == "health":
                print(json.dumps(await health(db), indent=2))
                return 0
            events = await replay_plan(db, handoffs_only=not args.all_sessions)
            print(json.dumps({
                "mode": "apply" if args.apply else "dry-run",
                "events": len(events),
                "with_handoff": sum(1 for e in events if e["handoff"]),
                "reaped": sum(1 for e in events if e["completion_reason"] == REAPED),
                "first": events[0]["event_id"] if events else None,
                "last": events[-1]["event_id"] if events else None,
            }, indent=2))
            if args.apply:
                print(f"queued {await replay_apply(db, events)} event(s); the running "
                      f"server's delivery loop sends them")
            return 0

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
