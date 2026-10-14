"""`ats lock preflight-deploy` (#3581): before deploying placement, list live
locks it makes repo-wide or broad, or that no edit guard can honour — reading
the database read-only and changing no lock.

Modeled on the live board of 2026-10-13: a session anchored at the main
checkout holding an absolute '<worktree>/**' (which places as the whole
repository), and a session anchored in another repository holding absolute
paths into this one.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession

from ai_team_sync.cli import cli
from ai_team_sync.models import Base, ScopeLock, Session
from tests.test_ownership_enforcement_3581 import BASE_FILES, _git, _govern, _make_repo


def _digest(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_preflight_lists_hazards_without_writing(tmp_path, monkeypatch):
    main = _make_repo(tmp_path / "main", BASE_FILES)
    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "side", str(worktree))
    wt = str(worktree.resolve())
    other = _make_repo(tmp_path / "other", ("README.md",))
    _govern(monkeypatch, tmp_path, main)

    db = tmp_path / "ats.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    later = datetime.now(timezone.utc) + timedelta(hours=4)
    with OrmSession(engine) as orm:
        def session(agent, root, status="active"):
            s = Session(developer="p", agent=agent, repo_root=root, status=status,
                        creator_uid=os.getuid())
            orm.add(s)
            orm.flush()
            return s.id

        wide = session("claude-code:298b649f", main)
        coordinator = session("claude-code:709f9ecf", other)
        ordinary = session("claude-code:ordinary", main)
        ended = session("claude-code:ended", main, status="completed")
        for sid, pattern in [
                (wide, f"{wt}/**"),
                (coordinator, f"{wt}/docs/a.md"),
                (coordinator, "/nowhere/at/all/**"),
                (ordinary, "frontend/src/**/Gone.vue"),
                (ordinary, "docs/a.md"),
                (ended, f"{wt}/**")]:
            orm.add(ScopeLock(session_id=sid, pattern=pattern, mode="advisory",
                              expires_at=later))
        orm.commit()
    engine.dispose()
    before = _digest(db)

    result = CliRunner().invoke(cli, ["lock", "preflight-deploy", "--db", str(db)])

    assert result.exit_code == 1, result.output
    out = result.output
    assert "5 live lock(s)" in out and "4 hazard(s)" in out
    assert f"[repo_wide] session {wide}" in out and f"as '**'" in out and "is governed" in out
    assert f"[other_repository] session {coordinator}" in out and "as 'docs/a.md'" in out
    assert f"[unplaceable] session {coordinator}" in out
    assert f"[matches_no_file] session {ordinary}" in out
    assert out.count(f"session {ordinary}") == 1  # its 'docs/a.md' is not a hazard
    assert ended not in out
    assert _digest(db) == before, "the preflight must not write the database"


def test_preflight_is_clean_when_nothing_is_hazardous(tmp_path, monkeypatch):
    main = _make_repo(tmp_path / "main", BASE_FILES)
    _govern(monkeypatch, tmp_path, main)
    db = tmp_path / "ats.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    with OrmSession(engine) as orm:
        s = Session(developer="p", agent="claude-code:x", repo_root=main, status="active")
        orm.add(s)
        orm.flush()
        orm.add(ScopeLock(session_id=s.id, pattern="docs/a.md", mode="exclusive"))
        orm.commit()
    engine.dispose()

    result = CliRunner().invoke(cli, ["lock", "preflight-deploy", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "1 live lock(s)" in result.output and "0 hazard(s)" in result.output
