"""#2814: a deeply nested reaped_locks journal is an invalid_journal refusal, not a 500.

json.loads raises RecursionError (a RuntimeError, not a ValueError) on valid JSON
nested ~10k levels deep. Both journal parses guarded only (TypeError, ValueError),
so the heartbeat -- the hottest endpoint in ATS -- answered HTTP 500 on every
attempt for that session, making it unrecoverable through the API.
"""

from __future__ import annotations

import json

import pytest

from ai_team_sync.background_tasks import auto_complete_stale_sessions
from ai_team_sync.routers import sessions as sessions_router

from .test_resurrection_restores_locks_2760 import (
    _all_locks, _heartbeat, _journal_of, _set_journal, _silent_session)

DEPTH = 100_000


def _deep(depth: int = DEPTH) -> str:
    return "[" * depth + "]" * depth


def test_the_fixture_really_raises_recursionerror_not_valueerror():
    with pytest.raises(RecursionError):
        json.loads(_deep())


@pytest.mark.parametrize("journal", [
    _deep(),                                                   # the whole envelope
    '{"repo_root": "/srv/echo-2760", "locks": ' + _deep() + "}",  # nested inside it
])
async def test_a_deeply_nested_journal_is_refused_as_invalid_journal(
        client, db_session, journal):
    sess = await _silent_session(db_session, scope=["src/**"],
                                 locks=[("src/**", "advisory")])
    sid = sess.id
    assert await auto_complete_stale_sessions(db_session) == 1
    await _set_journal(db_session, sid, journal)

    resp = await _heartbeat(client, sid)
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["status"] == "active"
    assert body["restoration_reason"] == "invalid_journal"
    # The lanes cannot be named from unreadable JSON: nothing recorded, nothing erased.
    assert body["locks_restored"] == []
    assert await _all_locks(db_session) == [], "an unreadable journal minted a lock"
    assert await _journal_of(db_session, sid) == "", "the claimed journal is spent"

    again = await _heartbeat(client, sid)
    assert again.status_code == 200, "the session stays recoverable through the API"


def test_the_tolerant_readers_never_raise_on_deep_nesting():
    deep = _deep()
    assert sessions_router._journalled_patterns(deep) == []
    assert sessions_router._parse_unresolved(deep) == []
    # The PATCH guard reads unreadable as "carries authority" (fail closed).
    assert sessions_router.journal_names_locks(deep) is True
