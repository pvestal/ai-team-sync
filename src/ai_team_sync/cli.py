"""CLI entry point for ai-team-sync (ats command)."""

from __future__ import annotations

import json
import os
import subprocess

import click
import httpx

DEFAULT_SERVER = "http://localhost:8400"


def _server_url() -> str:
    return os.environ.get("ATS_SERVER_URL", DEFAULT_SERVER)


def _get_developer() -> str:
    """Get developer name from git config or environment."""
    name = os.environ.get("ATS_DEVELOPER")
    if name:
        return name
    try:
        result = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def _get_branch() -> str:
    """Get current git branch."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _detect_agent() -> str:
    """Detect which AI agent is active from environment hints."""
    if os.environ.get("CLAUDE_CODE"):
        return "claude-code"
    if os.environ.get("CURSOR_SESSION"):
        return "cursor"
    if os.environ.get("COPILOT_WORKSPACE"):
        return "copilot-workspace"
    return "unknown"


def _api(method: str, path: str, **kwargs) -> httpx.Response:
    url = f"{_server_url()}/api{path}"
    with httpx.Client(timeout=10) as client:
        resp = getattr(client, method)(url, **kwargs)
        if resp.status_code >= 400:
            click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
            raise SystemExit(1)
        return resp


@click.group()
def cli():
    """ai-team-sync: Change management for AI-assisted teams."""
    pass


# --- Session commands ---

@cli.group()
def session():
    """Manage working sessions."""
    pass


@session.command("start")
@click.option("--scope", "-s", multiple=True, required=True, help="Scope glob pattern (repeatable)")
@click.option("--desc", "-d", default="", help="Description of what you're working on")
@click.option("--agent", "-a", default=None, help="Agent name (auto-detected if omitted)")
@click.option("--no-lock", is_flag=True, help="Don't auto-create scope locks")
def session_start(scope, desc, agent, no_lock):
    """Start a new working session and announce scope to the team."""
    resp = _api("post", "/sessions", json={
        "developer": _get_developer(),
        "agent": agent or _detect_agent(),
        "scope": list(scope),
        "description": desc,
        "branch": _get_branch(),
        "auto_lock": not no_lock,
    })
    data = resp.json()
    click.echo(f"Session started: {data['id']}")
    click.echo(f"  Developer: {data['developer']}")
    click.echo(f"  Scope: {', '.join(data['scope'])}")
    click.echo(f"  Branch: {data['branch']}")
    if data.get("lock_count"):
        click.echo(f"  Locks created: {data['lock_count']}")

    # Save session ID for other commands
    _save_active_session(data["id"])


@session.command("pause")
def session_pause():
    """Pause the current session (keeps locks)."""
    sid = _load_active_session()
    if not sid:
        click.echo("No active session. Start one with: ats session start", err=True)
        raise SystemExit(1)
    _api("patch", f"/sessions/{sid}", json={"status": "paused"})
    click.echo(f"Session {sid[:8]}... paused (locks retained)")


@session.command("complete")
@click.option("--summary", "-m", default=None, help="Session summary")
def session_complete(summary):
    """Complete the current session, release locks, notify team."""
    sid = _load_active_session()
    if not sid:
        click.echo("No active session.", err=True)
        raise SystemExit(1)

    if summary is None:
        summary = click.prompt("Session summary (what did you accomplish?)", default="")

    _api("patch", f"/sessions/{sid}", json={"status": "completed", "summary": summary})
    _clear_active_session()
    click.echo(f"Session {sid[:8]}... completed. Locks released, team notified.")


@session.command("list")
@click.option("--all", "show_all", is_flag=True, help="Show all sessions, not just active")
def session_list(show_all):
    """List active sessions across the team."""
    params = {} if show_all else {"status": "active"}
    resp = _api("get", "/sessions", params=params)
    sessions = resp.json()

    if not sessions:
        click.echo("No active sessions.")
        return

    for s in sessions:
        scope = ", ".join(s["scope"]) if s["scope"] else "no scope"
        status_icon = {"active": "*", "paused": "||", "completed": "ok"}.get(s["status"], "?")
        click.echo(f"  [{status_icon}] {s['developer']} ({s['agent']}) — {scope}")
        if s["description"]:
            click.echo(f"      {s['description']}")
        click.echo(f"      branch: {s['branch']}  locks: {s['lock_count']}  decisions: {s['decision_count']}")


# --- Lock commands ---

@cli.group()
def lock():
    """Manage scope locks."""
    pass


@lock.command("check")
@click.argument("paths", nargs=-1, required=True)
def lock_check(paths):
    """Check if paths conflict with any active locks."""
    resp = _api("post", "/locks/check", json={"paths": list(paths)})
    results = resp.json()

    any_locked = False
    for r in results:
        if r["locked"]:
            any_locked = True
            icon = "BLOCKED" if r["mode"] == "exclusive" else "WARNING"
            click.echo(f"  [{icon}] {r['path']} — locked by {r['developer']} (pattern: {r['pattern']})")
        else:
            click.echo(f"  [ok] {r['path']}")

    if any_locked:
        raise SystemExit(1)


@lock.command("list")
def lock_list():
    """List all active scope locks."""
    resp = _api("get", "/locks")
    locks = resp.json()

    if not locks:
        click.echo("No active locks.")
        return

    for l in locks:
        click.echo(f"  {l['pattern']} ({l['mode']}) — {l.get('developer', '?')}  expires: {l['expires_at']}")


# --- Decision commands ---

@cli.group()
def decision():
    """Log design decisions."""
    pass


@decision.command("log")
@click.argument("title")
@click.option("--chosen", "-c", required=True, help="What was chosen")
@click.option("--rejected", "-r", default=None, help="What was rejected")
@click.option("--reason", default="", help="Why this choice was made")
@click.option("--files", "-f", multiple=True, help="Affected files")
def decision_log(title, chosen, rejected, reason, files):
    """Log a design decision made during the current session."""
    sid = _load_active_session()
    if not sid:
        click.echo("No active session. Start one first.", err=True)
        raise SystemExit(1)

    _api("post", "/decisions", json={
        "session_id": sid,
        "title": title,
        "chosen": chosen,
        "rejected": rejected,
        "reasoning": reason,
        "files": list(files),
    })
    click.echo(f"Decision logged: {title}")


@decision.command("list")
def decision_list():
    """List recent decisions."""
    sid = _load_active_session()
    params = {"session_id": sid} if sid else {}
    resp = _api("get", "/decisions", params=params)
    decisions = resp.json()

    if not decisions:
        click.echo("No decisions logged.")
        return

    for d in decisions:
        click.echo(f"  {d['title']}")
        click.echo(f"    Chose: {d['chosen']}")
        if d.get("rejected"):
            click.echo(f"    Rejected: {d['rejected']}")
        if d.get("reasoning"):
            click.echo(f"    Why: {d['reasoning']}")


# --- Status commands ---

@cli.command()
def status():
    """Show your active session and any conflicts."""
    sid = _load_active_session()
    if not sid:
        click.echo("No active session. Start one with: ats session start -s 'src/**' -d 'description'")
        return

    resp = _api("get", f"/sessions/{sid}")
    s = resp.json()
    click.echo(f"Active session: {s['id'][:8]}...")
    click.echo(f"  Scope: {', '.join(s['scope'])}")
    click.echo(f"  Branch: {s['branch']}")
    click.echo(f"  Locks: {s['lock_count']}  Decisions: {s['decision_count']}  Commits: {s['commit_count']}")


@cli.command()
def team():
    """Show all active sessions across the team."""
    resp = _api("get", "/sessions", params={"status": "active"})
    sessions = resp.json()

    if not sessions:
        click.echo("No one is currently working.")
        return

    click.echo(f"{len(sessions)} active session(s):\n")
    for s in sessions:
        scope = ", ".join(s["scope"]) if s["scope"] else "no scope"
        click.echo(f"  {s['developer']} ({s['agent']})")
        click.echo(f"    Scope: {scope}")
        click.echo(f"    Branch: {s['branch']}")
        click.echo(f"    Locks: {s['lock_count']}  Decisions: {s['decision_count']}")
        click.echo()


# --- Session file helpers ---

def _session_file() -> str:
    return os.path.join(os.path.expanduser("~"), ".ats_session")


def _save_active_session(session_id: str):
    with open(_session_file(), "w") as f:
        f.write(session_id)


def _load_active_session() -> str | None:
    try:
        with open(_session_file()) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def _clear_active_session():
    try:
        os.remove(_session_file())
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    cli()
