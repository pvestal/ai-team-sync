"""Ticket roles and the owner-routed ticket mailbox (#3585).

A session bound to a ticket has exactly one role on it:

  owner        the one accountable session for the ticket. At most one live
               (active or paused) owner per ticket per OS account, enforced by a
               partial unique index plus the acquisition guard. Only the owner
               claims or receives the ticket's GENERAL mailbox.

Ownership is per OS account because accounts are ATS's trust boundary (#2741):
mail never crosses accounts, so an owner in one account must not be able to
stop another account's sessions from owning, and receiving, their own ticket
mail. Every interactive agent on a single-operator host runs as one account,
so there it means exactly one owner per ticket.
  contributor  works on the ticket under an explicit, non-empty file scope.
               WRITE (IMPLEMENT) delegation children are contributors.
  reviewer     independent review; claims no scope. VERIFY and READ_ONLY
               delegation children are reviewers.

Routing changes ticket-mode mail ONLY (addressing_mode='ticket': handoffs and
messages sent to "the next session on ticket N"). Mail addressed to an exact
session, addressed decisions, ticket events and delegation traffic keep their
exact addressee whatever that addressee's role (operator refinement,
2026-10-14). Nothing here drops or reroutes them.
"""

from __future__ import annotations

import json

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.delegation import IMPLEMENT
from ai_team_sync.message_lifecycle import append_delivery_event
from ai_team_sync.models import AgentMessage, Handoff, Session

OWNER = "owner"
CONTRIBUTOR = "contributor"
REVIEWER = "reviewer"
ROLES = (OWNER, CONTRIBUTOR, REVIEWER)
# A paused owner still owns: pausing must not let a second session take over.
LIVE_STATUSES = ("active", "paused")
OWNER_INDEX = "ux_sessions_live_ticket_owner"


class RoleRefused(Exception):
    def __init__(self, status: int, error: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.detail = {"error": error, "message": message, **extra}


def is_owner_conflict(exc: Exception) -> bool:
    """True when an IntegrityError came from the one-live-owner index.

    PostgreSQL names the index; SQLite names only the columns
    ("UNIQUE constraint failed: sessions.ticket_id, sessions.creator_uid").
    """
    text = str(getattr(exc, "orig", exc))
    return OWNER_INDEX in text or (
        "UNIQUE" in text.upper()
        and "sessions.ticket_id" in text and "sessions.creator_uid" in text)


def owner_conflict_detail(ticket_id: int | None, owner: Session | None = None) -> dict:
    """The 409 body for an ownership conflict. Names only the ticket's live
    owner (session id and agent label), the same fields resolve_role gives."""
    detail = {"error": "ticket_owned",
              "message": (f"ticket #{ticket_id} gained a live owner while this request ran; "
                          f"retry (a returning owner then comes back as a non-owner) or "
                          f"start as a scoped contributor or a reviewer"),
              "ticket_id": ticket_id}
    if owner is not None:
        detail.update(owner_session_id=owner.id, owner_agent=owner.agent)
    return detail


def role_for_child(mode: str) -> str:
    return CONTRIBUTOR if mode == IMPLEMENT else REVIEWER


async def live_owner(db: AsyncSession, ticket_id: int | None, creator_uid: int | None, *,
                     exclude_id: str | None = None) -> Session | None:
    """The live owner of ticket_id within one OS account (None = unidentified)."""
    if ticket_id is None:
        return None
    account = (Session.creator_uid.is_(None) if creator_uid is None
               else Session.creator_uid == creator_uid)
    query = select(Session).where(
        Session.ticket_id == ticket_id,
        Session.ticket_role == OWNER,
        Session.status.in_(LIVE_STATUSES),
        account)
    if exclude_id:
        query = query.where(Session.id != exclude_id)
    return (await db.execute(query.limit(1))).scalars().first()


async def resolve_role(db: AsyncSession, *, ticket_id: int | None, requested: str | None,
                       delegation_mode: str | None, scope: list[str],
                       creator_uid: int | None) -> tuple[str | None, str | None]:
    """Return (role, live owner id) for a session about to be created.

    Must run inside the acquisition guard so the owner check and the insert are
    one serialized step; the partial unique index is the backstop.
    """
    if ticket_id is None:
        if requested:
            raise RoleRefused(422, "role_requires_ticket",
                              "ticket_role is meaningful only with ticket_id")
        return None, None

    owner = await live_owner(db, ticket_id, creator_uid)
    owner_id = owner.id if owner else None

    if delegation_mode is not None:
        role = role_for_child(delegation_mode)
        if requested and requested != role:
            raise RoleRefused(409, "role_fixed_by_delegation",
                              f"a {delegation_mode} delegation child is a {role} on its ticket; "
                              f"it cannot be {requested}", role=role)
    elif requested == OWNER:
        if owner is not None:
            raise RoleRefused(409, "ticket_owned",
                              f"ticket #{ticket_id} already has a live owner "
                              f"({owner.agent}, session {owner.id}); start as a scoped "
                              f"contributor or a reviewer, or have the owner hand off",
                              ticket_id=ticket_id, owner_session_id=owner.id,
                              owner_agent=owner.agent)
        role = OWNER
    elif requested in (CONTRIBUTOR, REVIEWER):
        role = requested
    elif requested:
        raise RoleRefused(422, "bad_ticket_role", f"ticket_role must be one of {ROLES}")
    elif owner is None:
        # Compatibility for callers that predate roles: the first live session owns.
        role = OWNER
    else:
        # Later ones join under their declared files, or, claiming nothing, as
        # reviewers; either way they are told who owns. Every contributor is scoped.
        role = CONTRIBUTOR if scope else REVIEWER

    if role == REVIEWER and scope:
        raise RoleRefused(422, "reviewer_claims_nothing",
                          "a reviewer claims no files; start without scope")
    if role == CONTRIBUTOR and not scope:
        raise RoleRefused(422, "contributor_needs_scope",
                          "a contributor works under an explicit file scope; name the files "
                          "(for an IMPLEMENT delegation: --scope)")
    return role, owner_id


async def yield_ownership_if_taken(db: AsyncSession, session: Session) -> bool:
    """A reaped owner coming back finds the ticket owned by someone else.

    It returns as a contributor instead of breaking the one-owner rule. Called
    before the status flips back to a live state. Returns True when demoted.
    """
    if session.ticket_role != OWNER:
        return False
    if await live_owner(db, session.ticket_id, session.creator_uid,
                        exclude_id=session.id) is None:
        return False
    # Scoped -> contributor, unscoped -> reviewer, as at creation.
    session.ticket_role = CONTRIBUTOR if json.loads(session.scope or "[]") else REVIEWER
    await db.flush()
    # The general mail it still holds belongs to the live owner (operator
    # refinement 1: contributors and reviewers never hold it).
    await reconcile_ticket_mail(db, session.ticket_id, session.creator_uid,
                                holder_id=session.id)
    return True


async def route_ticket_message(db: AsyncSession, message: AgentMessage,
                               sender: Session) -> Session | None:
    """Address one ticket-mode message to the live owner at send time.

    Same-account rule as the deferred claim: an owner receives only mail its
    own account's sessions sent. With no eligible owner the message stays in
    the ticket pool, which the next owner claims at start.
    """
    if (message.addressing_mode != "ticket" or message.ticket_id is None
            or message.recipient_session_id is not None or sender.creator_uid is None):
        return None
    owner = await live_owner(db, message.ticket_id, sender.creator_uid, exclude_id=sender.id)
    if owner is None:
        return None
    message.recipient_session_id = owner.id
    message.recipient_agent = owner.agent
    append_delivery_event(message, "assigned", owner.id, "owner_route")
    if message.handoff_id:
        handoff = await db.get(Handoff, message.handoff_id)
        if handoff is not None:
            handoff.recipient_session_id = owner.id
    return owner


async def reconcile_ticket_mail(db: AsyncSession, ticket_id: int | None,
                                account_uid: int | None, *,
                                holder_id: str | None = None) -> int:
    """Move unread general ticket mail off sessions that may not hold it.

    Holders that may not: a live contributor or reviewer (e.g. a demoted
    owner), and an explicitly completed session (a send that raced the owner's
    completion was addressed to it after its release ran). The mail goes to the
    live owner of the sender's account, or back to the pool. Reaped sessions
    keep theirs (reaping is reversible; releasing it is a later slice), and rows
    that predate roles (ticket_role NULL) are left as they were.

    Called after commits on both sides of every race: ticket-mode send,
    owner completion, and in-transaction on demotion, where `holder_id` names
    the demoted session (it is still marked reaped at that moment).
    Returns the rows moved.
    """
    if ticket_id is None or account_uid is None:
        return 0
    same_account = select(Session.id).where(Session.creator_uid == account_uid)
    may_not_hold = ((Session.status.in_(LIVE_STATUSES)
                     & Session.ticket_role.in_((CONTRIBUTOR, REVIEWER)))
                    | ((Session.status == "completed") & ~Session.auto_completed
                       & Session.ticket_role.is_not(None)))
    if holder_id:
        may_not_hold = may_not_hold | (Session.id == holder_id)
    rows = (await db.execute(
        select(AgentMessage, Session).join(Session, Session.id == AgentMessage.recipient_session_id)
        .where(AgentMessage.ticket_id == ticket_id,
               AgentMessage.addressing_mode == "ticket",
               AgentMessage.acknowledged_at.is_(None),
               AgentMessage.sender_session_id.in_(same_account),
               may_not_hold)
    )).all()
    owner = await live_owner(db, ticket_id, account_uid)
    moved = 0
    for message, holder in rows:
        target = owner.id if owner is not None and owner.id != message.sender_session_id else None
        result = await db.execute(update(AgentMessage).where(
            AgentMessage.id == message.id,
            AgentMessage.recipient_session_id == holder.id,
            AgentMessage.acknowledged_at.is_(None),
        ).values(recipient_session_id=target,
                 recipient_agent=owner.agent if target else "")
          .execution_options(synchronize_session=False))
        if not result.rowcount:
            continue
        reason = ("not_owner" if holder.id == holder_id or holder.status != "completed"
                  else "recipient_completed")
        append_delivery_event(message, "released", holder.id, reason)
        if target:
            append_delivery_event(message, "assigned", target, "owner_route")
        message.recipient_session_id = target
        message.recipient_agent = owner.agent if target else ""
        if message.handoff_id:
            handoff = await db.get(Handoff, message.handoff_id)
            if handoff is not None:
                handoff.recipient_session_id = target
        moved += 1
    return moved


async def claim_pool(db: AsyncSession, session: Session) -> int:
    """The live owner takes ticket-pool mail left unaddressed.

    Covers mail written while no owner was live, and a send that raced the
    owner's own start. Never runs for contributors or reviewers.
    """
    if (session.ticket_role != OWNER or session.ticket_id is None
            or session.creator_uid is None or session.status not in LIVE_STATUSES):
        return 0
    same_account = select(Session.id).where(Session.creator_uid == session.creator_uid)
    queued = (await db.execute(select(AgentMessage).where(
        AgentMessage.ticket_id == session.ticket_id,
        AgentMessage.addressing_mode == "ticket",
        AgentMessage.acknowledged_at.is_(None),
        AgentMessage.recipient_session_id.is_(None),
        AgentMessage.sender_session_id != session.id,
        AgentMessage.sender_session_id.in_(same_account),
    ))).scalars().all()
    claimed = 0
    for row in queued:
        # Compare-and-set on every selection condition: a concurrent readdress,
        # ack or claim between the SELECT and this UPDATE makes it a no-op.
        result = await db.execute(update(AgentMessage).where(
            AgentMessage.id == row.id,
            AgentMessage.ticket_id == session.ticket_id,
            AgentMessage.addressing_mode == "ticket",
            AgentMessage.acknowledged_at.is_(None),
            AgentMessage.recipient_session_id.is_(None),
            AgentMessage.sender_session_id.in_(same_account),
        ).values(recipient_session_id=session.id, recipient_agent=session.agent)
          .execution_options(synchronize_session=False))
        if not result.rowcount:
            continue
        row.recipient_session_id = session.id
        row.recipient_agent = session.agent
        append_delivery_event(row, "assigned", session.id, "ticket_claim")
        if row.handoff_id:
            handoff = await db.get(Handoff, row.handoff_id)
            if handoff is not None:
                handoff.recipient_session_id = session.id
        claimed += 1
    return claimed
