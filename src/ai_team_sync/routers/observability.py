"""Bounded, read-only projections for the Tower Agent Console.

This module deliberately projects existing ATS records.  It does not create an
event log, infer conversations, or expose message bodies.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, asc, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.database import get_db
from ai_team_sync.delegation import effective_authority, prohibitions_for
from ai_team_sync.models import (
    AgentMessage,
    AuthorityCheck,
    CommitRecord,
    Decision,
    Delegation,
    FileActivity,
    Handoff,
    OverrideRequest,
    ServiceRestart,
    Session,
)
from ai_team_sync.workers import Authority, registry

router = APIRouter(prefix="/observability", tags=["observability"])

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRETS = (
    re.compile(r"(?i)\b(?:sk-|xox[baprs]-|gh[pousr]_)[a-z0-9_-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|redis|https?)://[^\s/:]+:[^\s/@]+@[^\s]+"),
    re.compile(r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----"),
)
_MAX_TEXT = 500
_PER_SOURCE_MAX = 500


def _authority_dict(value: Authority) -> dict[str, Any]:
    return {
        "edit": value.edit,
        "commit": value.commit,
        "land": value.land,
        "task_close": value.task_close,
    }


def _project_authority(session: Session, delegations: list[Delegation]) -> dict[str, Any]:
    """Bulk-safe equivalent of the read-only portion of authority_for_session."""
    reg = registry()
    worker, bound, _ = reg.resolve_for_session(session)
    base = _authority_dict(worker.authority)
    effective = base
    grantable = bound and session.status == "active" and reg.config_error is None
    delegation_info = None
    pointed = [row for row in delegations if row.child_session_id == session.id]
    recorded = session.delegation_id
    chosen = None
    legacy = False
    inconsistent = False
    if recorded or pointed:
        if recorded and len(pointed) == 1 and pointed[0].id == recorded:
            chosen = pointed[0]
        elif not recorded and len(pointed) == 1:
            chosen = pointed[0]
            legacy = True
        else:
            inconsistent = True
    if inconsistent:
        effective = _authority_dict(Authority())
        grantable = False
    elif chosen is not None:
        effective = _authority_dict(effective_authority(worker, chosen.mode))
        grantable = grantable and not legacy
        delegation_info = {
            "delegation_id": chosen.id,
            "mode": chosen.mode,
            "state": chosen.state,
            "parent_owner_session_id": chosen.parent_session_id,
        }
    return {
        "delegation": delegation_info,
        "base_authority": base,
        "effective_authority": effective,
        "grantable": grantable,
        "prohibitions": prohibitions_for(chosen.mode) if chosen is not None else [],
        "linkage": "INCONSISTENT" if inconsistent else "OBSERVED",
    }


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    """Make a display-safe summary; this is not a raw-record endpoint."""
    if value is None:
        return ""
    clean = _CONTROL.sub("", str(value)).replace("\x1b", "")
    for pattern in _SECRETS:
        clean = pattern.sub("[REDACTED]", clean)
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _stamp(value: datetime) -> str:
    return _aware(value).isoformat()


def _cursor_key(event: dict[str, Any]) -> tuple[str, str]:
    return event["timestamp"], event["id"]


def _encode_cursor(key: tuple[str, str]) -> str:
    raw = json.dumps(list(key), separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        datetime.fromisoformat(value[0])
        return str(value[0]), str(value[1])
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, detail="invalid observability cursor") from exc


def _party(session_id: str | None, sessions: dict[str, Session], agent: str = "") -> dict | None:
    if not session_id and not agent:
        return None
    row = sessions.get(session_id or "")
    return {
        "session_id": session_id,
        "agent": _text(agent or (row.agent if row else ""), 100) or None,
    }


def _event(
    *,
    event_id: str,
    timestamp: datetime,
    event_type: str,
    summary: str,
    source_record: str,
    session_id: str | None = None,
    task_id: int | None = None,
    status: str | None = None,
    source: dict | None = None,
    target: dict | None = None,
    detail: dict | None = None,
    integrity: str = "OBSERVED",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "timestamp": _stamp(timestamp),
        "event_type": event_type,
        "integrity": integrity,
        "session_id": session_id,
        "task_id": task_id,
        "source": source,
        "target": target,
        "summary": _text(summary),
        "status": _text(status, 80) if status is not None else None,
        "source_record": source_record,
        "detail": detail or {},
    }


def _collapsed_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Default view exposes metadata and counts, never stored free text."""
    safe_keys = {
        "repo_root",
        "scope_count",
        "auto_completed",
        "kind",
        "addressing_mode",
        "content",
        "mode",
        "lease_expires_at",
        "action",
        "worker",
        "path",
        "conflicting_pattern",
        "unit",
        "old_pid",
        "new_pid",
        "commit_hash",
    }
    collapsed: dict[str, Any] = {"expansion": "AVAILABLE"}
    for key, value in detail.items():
        if isinstance(value, list):
            collapsed[f"{key}_count"] = len(value)
        elif key in safe_keys:
            collapsed[key] = value
    return collapsed


async def _recent(
    db: AsyncSession,
    model: type[Any],
    timestamps: tuple[Any, ...],
    event_prefix: str,
    limit: int,
    *,
    after_key: tuple[str, str] | None,
    cutoff: datetime,
) -> tuple[list[Any], bool]:
    """Read each lifecycle timestamp in bounded, cursor-safe pages."""
    by_id: dict[str, Any] = {}
    truncated = False
    after = datetime.fromisoformat(after_key[0]) if after_key else None
    lower_bound = max(after, cutoff) if after is not None else cutoff
    for timestamp in timestamps:
        order = asc(timestamp) if after is not None else desc(timestamp)
        id_order = asc(model.id) if after is not None else desc(model.id)
        boundary: Any = timestamp >= lower_bound
        if after_key and after is not None and after >= cutoff:
            cursor_event_id = after_key[1]
            cursor_prefix = cursor_event_id.split(":", 1)[0]
            if event_prefix < cursor_prefix:
                boundary = timestamp > after
            elif event_prefix == cursor_prefix:
                raw_id = cursor_event_id[len(event_prefix) + 1 :].rsplit(":", 1)[0]
                boundary = or_(
                    timestamp > after,
                    and_(timestamp == after, model.id >= raw_id),
                )
        rows: Any = await db.execute(
            select(model)
            .where(timestamp.is_not(None), boundary)
            .order_by(order, id_order)
            .limit(limit + 1)
        )
        found = list(rows.scalars().all())
        truncated = truncated or len(found) > limit
        for row in found[:limit]:
            by_id[row.id] = row
    return list(by_id.values()), truncated


@router.get("/sessions")
async def projected_sessions(
    limit: int = Query(100, ge=1, le=200),
    status: str | None = Query(None, max_length=20),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    query = select(Session)
    if status:
        query = query.where(Session.status == status)
    result = await db.execute(query.order_by(desc(Session.started_at)).limit(limit))
    sessions = list(result.scalars().all())
    ids = [row.id for row in sessions]

    delegations: list[Delegation] = []
    lineage_truncated = False
    if ids:
        lineage_limit = min(500, limit * 4)
        found = await db.execute(
            select(Delegation)
            .where((Delegation.parent_session_id.in_(ids)) | (Delegation.child_session_id.in_(ids)))
            .order_by(desc(Delegation.created_at), asc(Delegation.id))
            .limit(lineage_limit + 1)
        )
        lineage_rows = list(found.scalars().all())
        lineage_truncated = len(lineage_rows) > lineage_limit
        delegations = lineage_rows[:lineage_limit]
    parent_by_child = {
        d.child_session_id: d.parent_session_id for d in delegations if d.child_session_id
    }
    children: dict[str, list[str]] = {}
    for delegation in delegations:
        if delegation.child_session_id:
            children.setdefault(delegation.parent_session_id, []).append(
                delegation.child_session_id
            )

    projected = []
    for row in sessions:
        authority = _project_authority(row, delegations)
        scope = _json(row.scope, [])
        heartbeat = _stamp(row.last_heartbeat) if row.last_heartbeat else None
        task_id = row.task_id if row.task_id is not None else row.ticket_id
        delegation_info = authority.get("delegation")
        projected.append(
            {
                "id": row.id,
                "agent": _text(row.agent, 100),
                "developer": _text(row.developer, 100),
                "status": row.status,
                "description": "WITHHELD_BY_DEFAULT",
                "repo_root": _text(row.repo_root, 1024),
                "branch": _text(row.branch, 255),
                "scope": [_text(item, 500) for item in scope] if isinstance(scope, list) else [],
                "task_id": task_id,
                "ticket_id": row.ticket_id,
                "parent_session_id": parent_by_child.get(row.id),
                "child_session_ids": sorted(children.get(row.id, [])),
                "delegation_id": row.delegation_id,
                "delegation": delegation_info,
                "effective_mode": (delegation_info.get("mode") if delegation_info else "DIRECT"),
                "effective_authority": authority["effective_authority"],
                "authority_grantable": authority["grantable"],
                "started_at": _stamp(row.started_at),
                "completed_at": _stamp(row.completed_at) if row.completed_at else None,
                "last_heartbeat": heartbeat,
                "ats_health": {
                    "registered": True,
                    "scoped": bool(scope),
                    "task": task_id is not None,
                    "brief": "UNINSTRUMENTED",
                    "client_connection": "UNINSTRUMENTED",
                    "last_event": heartbeat or _stamp(row.started_at),
                },
            }
        )
    return {
        "sessions": projected,
        "count": len(projected),
        "bounded": True,
        "lineage_limit": min(500, limit * 4),
        "lineage_truncated": lineage_truncated,
    }


@router.get("/events")
async def projected_events(
    limit: int = Query(200, ge=1, le=500),
    cursor: str | None = Query(None, max_length=2000),
    session_id: str | None = Query(None, max_length=36),
    task_id: int | None = None,
    event_id: str | None = Query(None, max_length=200),
    include_detail: bool = False,
    history_hours: int = Query(168, ge=1, le=720),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return a recent bootstrap or records strictly after an opaque cursor."""
    if include_detail and not event_id:
        raise HTTPException(422, detail="include_detail requires one exact event_id")
    cursor_key = _decode_cursor(cursor) if cursor else None
    cursor_time = datetime.fromisoformat(cursor_key[0]) if cursor_key else None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=history_hours)
    per_source = _PER_SOURCE_MAX if event_id else min(_PER_SOURCE_MAX, max(100, limit * 2))
    models = (
        (Session, (Session.started_at, Session.completed_at), "session"),
        (AgentMessage, (AgentMessage.created_at, AgentMessage.acknowledged_at), "message"),
        (Handoff, (Handoff.created_at,), "handoff"),
        (Delegation, (Delegation.created_at, Delegation.closed_at), "delegation"),
        (Decision, (Decision.created_at,), "decision"),
        (AuthorityCheck, (AuthorityCheck.created_at,), "authority"),
        (FileActivity, (FileActivity.created_at,), "file_activity"),
        (
            OverrideRequest,
            (OverrideRequest.created_at, OverrideRequest.responded_at),
            "override",
        ),
        (ServiceRestart, (ServiceRestart.created_at,), "restart"),
        (CommitRecord, (CommitRecord.created_at,), "commit"),
    )
    loaded = {
        model: await _recent(
            db,
            model,
            timestamps,
            event_prefix,
            per_source,
            after_key=cursor_key,
            cutoff=cutoff,
        )
        for model, timestamps, event_prefix in models
    }
    batches = {model: result[0] for model, result in loaded.items()}
    source_truncated = any(result[1] for result in loaded.values())
    session_rows = batches[Session]
    referenced = {row.id for row in session_rows}
    for row in batches[AgentMessage]:
        referenced.add(row.sender_session_id)
        if row.recipient_session_id:
            referenced.add(row.recipient_session_id)
    for row in batches[Handoff]:
        referenced.add(row.source_session_id)
        if row.recipient_session_id:
            referenced.add(row.recipient_session_id)
    for row in batches[Delegation]:
        referenced.add(row.parent_session_id)
        if row.child_session_id:
            referenced.add(row.child_session_id)
    for row in batches[Decision]:
        referenced.add(row.session_id)
        if row.recipient_session_id:
            referenced.add(row.recipient_session_id)
    for model in (AuthorityCheck, FileActivity, CommitRecord):
        referenced.update(row.session_id for row in batches[model])
    for row in batches[OverrideRequest]:
        referenced.add(row.requester_session_id)
        referenced.add(row.owner_session_id)
    referenced.update(row.session_id for row in batches[ServiceRestart] if row.session_id)
    if referenced:
        extra = await db.execute(select(Session).where(Session.id.in_(referenced)))
        session_rows = list(
            {row.id: row for row in [*session_rows, *extra.scalars().all()]}.values()
        )
    sessions = {row.id: row for row in session_rows}

    events: list[dict[str, Any]] = []
    for row in batches[Session]:
        events.append(
            _event(
                event_id=f"session:{row.id}:started",
                timestamp=row.started_at,
                event_type="ATS",
                summary="ATS session registered",
                source_record=f"sessions:{row.id}",
                session_id=row.id,
                task_id=row.task_id if row.task_id is not None else row.ticket_id,
                status=row.status,
                source=_party(row.id, sessions),
                detail={
                    "repo_root": _text(row.repo_root, 1024),
                    "scope_count": len(_json(row.scope, [])),
                },
            )
        )
        if row.completed_at:
            events.append(
                _event(
                    event_id=f"session:{row.id}:completed",
                    timestamp=row.completed_at,
                    event_type="ATS",
                    summary="ATS session completed",
                    source_record=f"sessions:{row.id}",
                    session_id=row.id,
                    task_id=row.task_id if row.task_id is not None else row.ticket_id,
                    status="completed",
                    source=_party(row.id, sessions),
                    detail={"auto_completed": row.auto_completed},
                )
            )
    for row in batches[AgentMessage]:
        events.append(
            _event(
                event_id=f"message:{row.id}:created",
                timestamp=row.created_at,
                event_type="HANDOFF",
                summary=f"Agent-visible {row.kind} (contents withheld)",
                source_record=f"agent_messages:{row.id}",
                session_id=row.sender_session_id,
                task_id=row.ticket_id,
                status="acknowledged" if row.acknowledged_at else "pending",
                source=_party(row.sender_session_id, sessions, row.sender_agent),
                target=_party(row.recipient_session_id, sessions, row.recipient_agent),
                detail={
                    "kind": _text(row.kind, 20),
                    "addressing_mode": _text(row.addressing_mode, 20),
                    "content": "WITHHELD",
                },
            )
        )
        if row.acknowledged_at:
            events.append(
                _event(
                    event_id=f"message:{row.id}:acknowledged",
                    timestamp=row.acknowledged_at,
                    event_type="HANDOFF",
                    summary="Agent-visible message acknowledged (contents withheld)",
                    source_record=f"agent_messages:{row.id}",
                    session_id=row.recipient_session_id,
                    task_id=row.ticket_id,
                    status="acknowledged",
                    source=_party(row.recipient_session_id, sessions, row.recipient_agent),
                    target=_party(row.sender_session_id, sessions, row.sender_agent),
                    detail={"kind": _text(row.kind, 20), "content": "WITHHELD"},
                )
            )
    for row in batches[Handoff]:
        blockers = [_text(x) for x in _json(row.blockers, [])]
        events.append(
            _event(
                event_id=f"handoff:{row.id}:created",
                timestamp=row.created_at,
                event_type="HANDOFF",
                summary="Structured handoff",
                source_record=f"handoffs:{row.id}",
                session_id=row.source_session_id,
                task_id=row.ticket_id,
                status=row.verdict,
                source=_party(row.source_session_id, sessions),
                target=_party(row.recipient_session_id, sessions),
                detail={
                    "blockers": blockers,
                    "next_steps": [_text(x) for x in _json(row.next_steps, [])],
                    "artifacts": [_text(x) for x in _json(row.artifacts, [])],
                },
            )
        )
        if blockers or row.verdict.upper() == "BLOCK":
            events.append(
                _event(
                    event_id=f"handoff:{row.id}:blockers",
                    timestamp=row.created_at,
                    event_type="BLOCKER",
                    summary=f"Structured handoff blocker ({len(blockers)})",
                    source_record=f"handoffs:{row.id}",
                    session_id=row.source_session_id,
                    task_id=row.ticket_id,
                    status=row.verdict,
                    source=_party(row.source_session_id, sessions),
                    target=_party(row.recipient_session_id, sessions),
                    detail={"blockers": blockers},
                )
            )
    for row in batches[Delegation]:
        events.append(
            _event(
                event_id=f"delegation:{row.id}:created",
                timestamp=row.created_at,
                event_type="TASK",
                summary="Bounded delegation recorded",
                source_record=f"delegations:{row.id}",
                session_id=row.parent_session_id,
                status=row.state,
                source=_party(row.parent_session_id, sessions),
                target=_party(row.child_session_id, sessions) if row.child_session_id else None,
                detail={
                    "mode": row.mode,
                    "objective": _text(row.objective),
                    "requested_worker": _text(row.delegated_worker, 100),
                    "resolved_binary": _text(row.resolved_binary, 1024) or "UNAVAILABLE",
                    "lease_expires_at": _stamp(row.lease_expires_at),
                },
            )
        )
        if row.closed_at:
            events.append(
                _event(
                    event_id=f"delegation:{row.id}:closed",
                    timestamp=row.closed_at,
                    event_type="REVIEW",
                    summary="Delegation result returned",
                    source_record=f"delegations:{row.id}",
                    session_id=row.child_session_id,
                    status=row.verdict or row.state,
                    source=_party(row.child_session_id, sessions),
                    target=_party(row.parent_session_id, sessions),
                    detail={"result_summary": _text(row.result_summary), "mode": row.mode},
                )
            )
    for row in batches[Decision]:
        events.append(
            _event(
                event_id=f"decision:{row.id}:created",
                timestamp=row.created_at,
                event_type="RULING",
                summary="ATS decision recorded",
                source_record=f"decisions:{row.id}",
                session_id=row.session_id,
                task_id=row.ticket_id,
                status="recorded",
                source=_party(row.session_id, sessions),
                target=_party(row.recipient_session_id, sessions),
                detail={
                    "title": _text(row.title),
                    "chosen": _text(row.chosen),
                    "reasoning": _text(row.reasoning),
                    "files": [_text(x, 1024) for x in _json(row.files, [])],
                },
            )
        )
    for row in batches[AuthorityCheck]:
        events.append(
            _event(
                event_id=f"authority:{row.id}:checked",
                timestamp=row.created_at,
                event_type="RULING",
                summary=f"Authority check: {_text(row.action, 40)}",
                source_record=f"authority_checks:{row.id}",
                session_id=row.session_id,
                task_id=row.task_id,
                status="allowed" if row.allowed else "denied",
                source=_party(row.session_id, sessions, row.agent),
                detail={
                    "action": row.action,
                    "worker": _text(row.worker, 100),
                    "reasons": [_text(x) for x in _json(row.reasons, [])],
                    "evidence_keys": [_text(x, 100) for x in _json(row.evidence_keys, [])],
                },
            )
        )
    for row in batches[FileActivity]:
        events.append(
            _event(
                event_id=f"file_activity:{row.id}:created",
                timestamp=row.created_at,
                event_type="TOOL",
                summary=f"File {row.action}: {_text(row.path, 1024)}",
                source_record=f"file_activities:{row.id}",
                session_id=row.session_id,
                status="observed",
                source=_party(row.session_id, sessions, row.agent),
                detail={
                    "action": row.action,
                    "path": _text(row.path, 1024),
                    "repo_root": _text(row.repo_root, 1024),
                },
                integrity="PARTIAL",
            )
        )
    for row in batches[OverrideRequest]:
        events.append(
            _event(
                event_id=f"override:{row.id}:requested",
                timestamp=row.created_at,
                event_type="HANDOFF",
                summary="Scope override requested",
                source_record=f"override_requests:{row.id}",
                session_id=row.requester_session_id,
                status="requested",
                source=_party(row.requester_session_id, sessions),
                target=_party(row.owner_session_id, sessions),
                detail={
                    "conflicting_pattern": _text(row.conflicting_pattern, 500),
                    "justification": _text(row.justification),
                },
            )
        )
        if row.responded_at:
            events.append(
                _event(
                    event_id=f"override:{row.id}:responded",
                    timestamp=row.responded_at,
                    event_type="RULING",
                    summary="Scope override response",
                    source_record=f"override_requests:{row.id}",
                    session_id=row.owner_session_id,
                    status=row.status,
                    source=_party(row.owner_session_id, sessions),
                    target=_party(row.requester_session_id, sessions),
                    detail={"response": _text(row.response_message)},
                )
            )
    for row in batches[ServiceRestart]:
        events.append(
            _event(
                event_id=f"restart:{row.id}:created",
                timestamp=row.created_at,
                event_type="SYSTEM",
                summary=f"Shared service restart recorded: {_text(row.unit, 100)}",
                source_record=f"service_restarts:{row.id}",
                session_id=row.session_id,
                status=row.outcome,
                source=_party(row.session_id, sessions),
                detail={"unit": row.unit, "old_pid": row.old_pid, "new_pid": row.new_pid},
            )
        )
    for row in batches[CommitRecord]:
        events.append(
            _event(
                event_id=f"commit:{row.id}:created",
                timestamp=row.created_at,
                event_type="TOOL",
                summary=f"Git commit {_text(row.commit_hash, 12)}",
                source_record=f"commit_records:{row.id}",
                session_id=row.session_id,
                status="recorded",
                source=_party(row.session_id, sessions),
                detail={"commit_hash": _text(row.commit_hash, 40), "message": _text(row.message)},
                integrity="PARTIAL",
            )
        )

    if session_id:
        events = [
            e
            for e in events
            if e["session_id"] == session_id
            or (e["source"] and e["source"].get("session_id") == session_id)
            or (e["target"] and e["target"].get("session_id") == session_id)
        ]
    if task_id is not None:
        events = [e for e in events if e["task_id"] == task_id]
    if event_id:
        events = [e for e in events if e["id"] == event_id]
    lower_key = _stamp(max(cursor_time, cutoff) if cursor_time else cutoff)
    events = [event for event in events if event["timestamp"] >= lower_key]
    events.sort(key=_cursor_key)
    if cursor_key:
        events = [event for event in events if _cursor_key(event) > cursor_key]
    else:
        events = events[-limit:]
    events = events[:limit]
    if not include_detail:
        for event in events:
            event["detail"] = _collapsed_detail(event["detail"])
    next_cursor = _encode_cursor(_cursor_key(events[-1])) if events else cursor
    return {
        "events": events,
        "count": len(events),
        "next_cursor": next_cursor,
        "bounded": True,
        "source_truncated": source_truncated,
        "history_window_hours": history_hours,
        "detail_mode": "expanded" if include_detail else "collapsed",
    }


@router.get("/coverage")
async def observability_coverage() -> dict[str, Any]:
    event_types = {
        "USER": ("UNINSTRUMENTED", "User prompts are not persisted by ATS."),
        "CLAUDE": ("PARTIAL", "Registered sessions and explicit ATS operations only."),
        "CODEX": ("PARTIAL", "Registered sessions and explicit ATS operations only."),
        "ATS": ("OBSERVED", "Sessions and coordination records are projected directly."),
        "ECHO": ("PARTIAL", "Read-only preflight and telemetry APIs; no memory contents."),
        "TOOL": ("PARTIAL", "ATS file activity, commits, and aggregate Echo MCP telemetry."),
        "HANDOFF": ("OBSERVED", "Explicit ATS messages, handoffs, and overrides."),
        "REVIEW": ("PARTIAL", "Structured returned delegations and recorded handoffs."),
        "RULING": ("OBSERVED", "ATS decisions, authority checks, and override responses."),
        "BLOCKER": ("PARTIAL", "Structured handoff blockers only."),
        "TASK": (
            "PARTIAL",
            "ATS delegations/task links plus current Tower task envelope when available.",
        ),
        "SYSTEM": ("PARTIAL", "Recorded restarts and point-in-time read-only probes."),
    }
    return {
        "event_types": {
            name: {"state": state, "detail": detail}
            for name, (state, detail) in event_types.items()
        },
        "exchange_boundaries": {
            "agent_visible_message_contents": "WITHHELD",
            "model_reasoning": "UNINSTRUMENTED",
            "unreported_tool_calls": "UNINSTRUMENTED",
            "causal_links_without_explicit_records": "UNAVAILABLE",
        },
    }
