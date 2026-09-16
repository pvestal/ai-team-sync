"""Which SESSION is making this request (#2757).

A reader asked "which live locks cover this path" needs no caller: coverage is a
namespace fact and does not depend on who asks (docs/lock-readers.md). A reader
asked "are YOU blocked" does, and ATS had no way to answer, so it told sessions
their own claims blocked them.

HOW A CALLER IS RESOLVED. Only by identifying itself, never by inference:

1. an explicit `session_id` in the request body;
2. the `X-ATS-Session-Id` header;
3. the `X-ATS-Agent` header, when this account has exactly one active session
   under that label.

Each is validated against the #2741 boundary the same way liveness validates its
headers -- `cross_account` against the kernel's owner of the requesting socket --
so naming another ACCOUNT's session resolves nothing.

WHAT WAS TRIED AND REJECTED: "this uid has exactly one live session, so that
must be the caller." It is not sound. A uid owning one session is a coincidence,
not proof that THIS request came from it; a bare `git commit` hook, the CLI, or
any other process of the same account owns no session at all and would have been
handed someone else's. Caught by tests 3, 7 and 8b of test_lock_readers_lexical
and by test_mcp_pre_commit_check, which all went green-when-they-should-be-red:
a real caller was told a foreign exclusive lock did not block it. Unresolved
never guesses, because a wrong guess drops a foreign lock from a verdict, which
is the one failure this must not have.

THE GRANULARITY IS THE ACCOUNT, and this file does not pretend otherwise. Within
one OS account a caller can name a sibling session and have that session's locks
left out of ITS OWN verdict. That is the boundary #2741 established ("it does not
stop other code running as the bound account itself") and the blast radius is
only the caller's own answer: no lock is released, no claim is taken, no mutation
is granted. Cross-account, the claim is refused outright.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_team_sync.models import ScopeLock, Session

#: Shown to a caller whose verdict may therefore include its own locks.
UNRESOLVED_NOTE = ("caller identity unresolved ({reason}) - your own locks may "
                   "be included above; pass session_id to exclude them")


@dataclass(frozen=True)
class CallerSession:
    """The resolved caller, or the reason there isn't one."""

    session_id: str | None = None
    reason: str = ""

    @property
    def unresolved(self) -> bool:
        return self.session_id is None

    def owns(self, lock: ScopeLock) -> bool:
        """Is this lock the caller's own? False for every lock when unresolved,
        which is what makes the unresolved path conservative."""
        return self.session_id is not None and lock.session_id == self.session_id

    def note(self) -> str:
        return UNRESOLVED_NOTE.format(reason=self.reason)


async def _owned(db: AsyncSession, peer_uid: int | None, sid: str) -> "CallerSession":
    from ai_team_sync.routers.locks import cross_account

    session = (await db.execute(
        select(Session).where(Session.id == sid))).scalar_one_or_none()
    if session is None:
        return CallerSession(reason=f"session {sid} is not known to this server")
    if cross_account(peer_uid, session, any_status=True):
        return CallerSession(reason=f"session {sid} belongs to another OS account")
    return CallerSession(session_id=session.id)


async def resolve_caller_session(
    db: AsyncSession, *, request, session_id: str = ""
) -> CallerSession:
    """The session behind this request, by the #2741 boundary.

    Both lock readers call this one function: a verdict must not mean different
    things depending on which endpoint rendered it.
    """
    from ai_team_sync import peer_identity
    from ai_team_sync.liveness import AGENT_HEADER, SESSION_HEADER
    from ai_team_sync.routers.locks import cross_account

    peer_uid = peer_identity.peer_uid_for_request(request)
    headers = getattr(request, "headers", {}) or {}

    claimed = (session_id or "").strip() or (headers.get(SESSION_HEADER) or "").strip()
    if claimed:
        return await _owned(db, peer_uid, claimed)

    agent = (headers.get(AGENT_HEADER) or "").strip()
    if not agent:
        return CallerSession(reason="the request named no session")

    mine = [s for s in (await db.execute(
        select(Session).where(Session.status == "active", Session.agent == agent)
    )).scalars().all() if not cross_account(peer_uid, s)]
    if len(mine) == 1:
        return CallerSession(session_id=mine[0].id)
    if not mine:
        return CallerSession(reason=f"no active session of this account is labelled {agent!r}")
    # One agent routinely holds several sessions, one per repo (liveness.py).
    return CallerSession(
        reason=f"{agent!r} has {len(mine)} active sessions here; pass session_id")
