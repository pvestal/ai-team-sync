#!/usr/bin/env python3
"""De-duplicate scope_locks: one row per (session, anchor, lane) (#2765).

Before #2765, POST /api/locks inserted a new row on every claim, so one session
could hold one lane several times with contradictory modes. create_lock now
refuses that; this clears rows already written.

DRY RUN BY DEFAULT. Without --apply the database is opened read-only (SQLite
`PRAGMA query_only`, PostgreSQL `SET TRANSACTION READ ONLY`), the plan is printed
with a count, and the exit code is 1 when anything would be removed, else 0.

Per group of rows sharing a session, its repo_root and a lane (the pattern's
canonical form under that anchor), the KEEPER is the strongest row: live first
(unexpired, or exclusive with a live owner, as GET /api/locks counts it), then
exclusive, then authority-bearing, then the latest expiry. Every other row in
the group is removed, except authority-bearing rows, which this script never
removes (they are listed as retained).

    python scripts/dedupe_scope_locks.py [--database-url URL]          # dry run
    python scripts/dedupe_scope_locks.py [--database-url URL] --apply  # remove

--apply takes the same database writer lock lock acquisition takes, and plans
inside it, so it cannot race a concurrent claim.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import IO

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_team_sync.models import ScopeLock, Session
from ai_team_sync.routers.locks import (_aware, _take_database_writer_lock, lane_key,
                                        live_exclusive_owner)
from ai_team_sync.scope_paths import canonical_root


def _row(lock: ScopeLock, owner: Session, lane: str, live: bool) -> dict:
    return {"id": lock.id, "session_id": lock.session_id, "agent": owner.agent,
            "repo_root": owner.repo_root or "", "lane": lane, "pattern": lock.pattern,
            "mode": lock.mode, "authority_bearing": bool(lock.authority_bearing),
            "expires_at": _aware(lock.expires_at).isoformat(), "live": live}


def plan(rows: list[tuple[ScopeLock, Session]], now: datetime) -> dict:
    """Which rows to keep, remove and retain. Pure: reads nothing, writes nothing."""
    groups: dict[tuple[str, str, str], list[tuple[ScopeLock, Session]]] = defaultdict(list)
    for lock, owner in rows:
        anchor = canonical_root(owner.repo_root or "")
        groups[(lock.session_id, anchor, lane_key(lock.pattern, owner.repo_root))].append(
            (lock, owner))

    def is_live(lock: ScopeLock, owner: Session) -> bool:
        return _aware(lock.expires_at) > now or (
            lock.mode == "exclusive" and live_exclusive_owner(owner))

    report = {"groups": 0, "keep": [], "remove": [], "retained": []}
    for (_sid, _anchor, lane), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        report["groups"] += 1
        ranked = sorted(members, reverse=True, key=lambda m: (
            is_live(*m), m[0].mode == "exclusive", bool(m[0].authority_bearing),
            _aware(m[0].expires_at), m[0].id))
        keeper, rest = ranked[0], ranked[1:]
        report["keep"].append(_row(*keeper, lane, is_live(*keeper)))
        for lock, owner in rest:
            bucket = "retained" if lock.authority_bearing else "remove"
            report[bucket].append(_row(lock, owner, lane, is_live(lock, owner)))
    return report


async def _read_only(db: AsyncSession) -> None:
    conn = await db.connection()
    if conn.dialect.name == "postgresql":
        await conn.execute(text("SET TRANSACTION READ ONLY"))
    elif conn.dialect.name == "sqlite":
        await conn.exec_driver_sql("PRAGMA query_only = ON")


def _print(report: dict, apply: bool, out: IO[str]) -> None:
    head = "APPLY" if apply else "DRY RUN"
    verb = "removed" if apply else "would be removed"
    print(f"{head}: {report['groups']} duplicated lane(s); {len(report['remove'])} row(s) "
          f"{verb}; {len(report['retained'])} authority-bearing duplicate(s) retained.",
          file=out)

    def fmt(r: dict) -> str:
        return (f"{r['id']}  session={r['session_id']} ({r['agent']}) "
                f"repo_root={r['repo_root'] or '-'} lane={r['lane']!r} "
                f"pattern={r['pattern']!r} mode={r['mode']} "
                f"authority_bearing={r['authority_bearing']} live={r['live']} "
                f"expires={r['expires_at']}")

    for title, key in (("keep", "keep"), (verb, "remove"),
                       ("retained (authority-bearing, never removed)", "retained")):
        if report[key]:
            print(f"\n{title}:", file=out)
            for r in report[key]:
                print("  " + fmt(r), file=out)


async def run(database_url: str, apply: bool = False, out: IO[str] | None = None) -> dict:
    out = out if out is not None else sys.stdout
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            if apply:
                await _take_database_writer_lock(db)
            else:
                await _read_only(db)
            rows = (await db.execute(
                select(ScopeLock, Session).join(Session, ScopeLock.session_id == Session.id)
            )).all()
            report = plan([(lock, owner) for lock, owner in rows], datetime.now(timezone.utc))
            if apply and report["remove"]:
                doomed = {r["id"] for r in report["remove"]}
                for lock, _owner in rows:
                    if lock.id in doomed:
                        await db.delete(lock)
                await db.commit()
            else:
                await db.rollback()
    finally:
        await engine.dispose()
    _print(report, apply, out)
    return report


async def main_async(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", default=None,
                        help="SQLAlchemy async URL (default: the server's DATABASE_URL)")
    parser.add_argument("--apply", action="store_true",
                        help="remove the planned rows (default: read-only dry run)")
    args = parser.parse_args(argv)
    url = args.database_url
    if url is None:
        from ai_team_sync.config import settings
        url = settings.database_url
    report = await run(url, apply=args.apply)
    return 0 if args.apply or not report["remove"] else 1


def main() -> None:
    sys.exit(asyncio.run(main_async(sys.argv[1:])))


if __name__ == "__main__":
    main()
