"""Command-line entry point for tower-console."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .sources import ConsoleConfig, ReadOnlySources


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Tower agent activity console")
    parser.add_argument("--ats-url", help="ATS base URL (default: ATS_URL or localhost:8400)")
    parser.add_argument(
        "--echo-url", help="Echo Brain base URL (default: ECHO_BRAIN_URL or localhost:8309)"
    )
    parser.add_argument("--repo", default="", help="Repository for read-only git status")
    parser.add_argument("--snapshot", action="store_true", help="Print one sanitized JSON snapshot")
    return parser


async def _snapshot(config: ConsoleConfig) -> int:
    sources = ReadOnlySources(config)
    try:
        values = await asyncio.gather(
            sources.ats_sessions(),
            sources.ats_events(limit=50),
            sources.coverage(),
            sources.echo_summary(),
            sources.local_status(),
            return_exceptions=True,
        )
        labels = ("sessions", "events", "coverage", "echo", "system")
        snapshot: dict = {
            label: (
                {"state": "DISCONNECTED", "error": type(value).__name__}
                if isinstance(value, Exception)
                else value
            )
            for label, value in zip(labels, values, strict=True)
        }
        sessions = snapshot["sessions"].get("sessions", [])
        snapshot["runtime"] = await sources.runtime(sessions if isinstance(sessions, list) else [])
        print(json.dumps(snapshot, indent=2, default=str))
        return 0
    finally:
        await sources.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = ConsoleConfig.from_environment(args.repo)
    if args.ats_url or args.echo_url:
        config = ConsoleConfig(
            ats_url=(args.ats_url or config.ats_url).rstrip("/"),
            echo_url=(args.echo_url or config.echo_url).rstrip("/"),
            repo_root=config.repo_root,
        )
    if args.snapshot:
        return asyncio.run(_snapshot(config))
    try:
        from .app import TowerConsole
    except ImportError as exc:
        if exc.name == "textual" or (exc.name and exc.name.startswith("textual.")):
            print(
                "tower-console requires the optional console dependency:\n"
                "  pip install 'ai-team-sync[console]'",
                file=sys.stderr,
            )
            return 2
        raise
    TowerConsole(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
