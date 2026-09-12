"""CLI entry point for ai-team-sync (ats command)."""

from __future__ import annotations

import json
import os
import subprocess

import click
import httpx

DEFAULT_SERVER = "http://localhost:8400"


def _repo_root() -> str | None:
    """Get git repo root, or None if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _load_team_config() -> dict:
    """Load .ai-team-sync.toml from repo root if it exists."""
    from ai_team_sync.config import load_team_config
    root = _repo_root()
    if root:
        from pathlib import Path
        return load_team_config(Path(root))
    return {}


def _server_url() -> str:
    env = os.environ.get("ATS_SERVER_URL")
    if env:
        return env
    team = _load_team_config()
    return team.get("server", {}).get("url", DEFAULT_SERVER)


def _get_developer() -> str:
    """Get developer name from env, team config, or git config."""
    name = os.environ.get("ATS_DEVELOPER")
    if name:
        return name
    team = _load_team_config()
    toml_name = team.get("developer", {}).get("name")
    if toml_name:
        return toml_name
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
    """Detect which AI agent is active from environment hints.

    Resolution order:
      1. ATS_AGENT explicit override — the reliable path for ANY agent
         (e.g. ATS_AGENT=codex, ATS_AGENT=ollama:qwen2.5-coder). Set this in
         the agent's environment when it lacks a stable auto-detect signature.
      2. Known auto-detected env signatures.
      3. "unknown".
    """
    # #2517: this was a rotted copy — it missed CLAUDECODE (no underscore),
    # so `ats session start` from a Claude Code shell registered 'unknown'.
    # One detection now lives in session_pointer; keep this name for callers.
    from ai_team_sync import session_pointer as sp
    return sp.detect_agent()


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
@click.option("--exclusive", is_flag=True, help="Create exclusive locks (block overlapping sessions)")
def session_start(scope, desc, agent, no_lock, exclusive):
    """Start a new working session and announce scope to the team."""
    lock_mode = "exclusive" if exclusive else "advisory"

    try:
        # #2517: the label MUST carry the live Claude session token — the lock
        # guard attributes sessions by matching the hook cid against it. A bare
        # label here created rows the guard read as "another ACTIVE session",
        # self-blocking the caller and worsening on every re-register.
        from ai_team_sync import session_pointer as sp
        resp = _api("post", "/sessions", json={
            "developer": _get_developer(),
            "agent": sp.agent_label(agent) if agent else sp.agent_label(),
            "scope": list(scope),
            "description": desc,
            "branch": _get_branch(),
            "auto_lock": not no_lock,
            "lock_mode": lock_mode,
        })
    except SystemExit:
        # Error already printed by _api
        raise
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
@click.option("--session-id", default="", help=(
    "The exact session to complete. Authoritative: refused if this process's own "
    "pointer names a different LIVE session. Omit for the legacy pointer path."))
def session_complete(summary, session_id):
    """Complete a session, release locks, notify team.

    Parity with the MCP tool: targeting is part of the contract, not a CLI
    convenience, so a worker can name the session it means from either surface.
    """
    from ai_team_sync import session_pointer as sp
    from ai_team_sync.session_target import resolve_completion_target

    pointer_id, pointer_source = sp.resolve_pointer_source()
    live = None
    if session_id and pointer_id and pointer_id != session_id:
        try:
            with httpx.Client(timeout=10) as c:
                r = c.get(f"{_server_url()}/api/sessions/{pointer_id}")
            live = r.status_code == 200 and r.json().get("status") == "active"
        except Exception:  # noqa: BLE001 — unknown stays LIVE, fail closed
            live = None

    target = resolve_completion_target(
        explicit_id=session_id or None, pointer_id=pointer_id,
        pointer_source=pointer_source, pointer_names_live_session=live)
    if not target.ok:
        click.echo(target.refusal, err=True)
        raise SystemExit(1)
    if target.conflict:
        click.echo(f"note: {target.conflict}", err=True)
    sid = target.session_id

    if summary is None:
        summary = click.prompt("Session summary (what did you accomplish?)", default="")

    resp = _api("patch", f"/sessions/{sid}",
                json={"status": "completed", "summary": summary})
    after = resp.json()
    # Only drop the pointer when it was OUR session; clearing it after completing
    # some other row would strand this process's own id.
    if not session_id or session_id == pointer_id:
        _clear_active_session()
    click.echo(f"Session completed: {after.get('id')} "
               f"(agent {after.get('agent')}, status {after.get('status')}, "
               f"completed_at {after.get('completed_at')}). Locks released.")


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
            why = f' — "{r["reason"]}"' if r.get("reason") else ""
            click.echo(f"  [{icon}] {r['path']} — locked by {r['developer']} (pattern: {r['pattern']}){why}")
        else:
            click.echo(f"  [ok] {r['path']}")

    if any_locked:
        raise SystemExit(1)


@lock.command("add")
@click.argument("pattern")
@click.option("--mode", type=click.Choice(["advisory", "exclusive"]), default="advisory",
              help="advisory (warn on overlap) or exclusive (block overlap)")
@click.option("--reason", default="", help="Why this path is locked (shown to anyone it blocks)")
def lock_add(pattern, mode, reason):
    """Add a scope lock to the current session.

    PATTERN must be a path glob (e.g. 'src/**', 'pkg/foo.py'), not prose — put the
    description in --reason. The server rejects prose patterns.
    """
    sid = _load_active_session()
    if not sid:
        click.echo("No active session. Start one first with: ats session start", err=True)
        raise SystemExit(1)
    resp = _api("post", "/locks", json={
        "session_id": sid, "pattern": pattern, "mode": mode, "reason": reason,
    })
    lock = resp.json()
    why = f" — {reason}" if reason else ""
    click.echo(f"Locked {lock['pattern']} ({lock['mode']}){why}")


@lock.command("list")
def lock_list():
    """List all active scope locks."""
    resp = _api("get", "/locks")
    locks = resp.json()

    if not locks:
        click.echo("No active locks.")
        return

    for l in locks:
        why = f"  reason: {l['reason']}" if l.get("reason") else ""
        click.echo(f"  {l['pattern']} ({l['mode']}) — {l.get('developer', '?')}  expires: {l['expires_at']}{why}")


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
@click.option("--all", "show_all", is_flag=True,
              help="Show the whole team's decision log, not just the active session's")
def decision_list(show_all):
    """List recent decisions.

    Defaults to the active session's decisions; pass --all to read the
    team-wide log (what other agents have decided) regardless of your session.
    """
    sid = None if show_all else _load_active_session()
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


# --- Hooks management ---

@cli.group()
def hooks():
    """Manage git hooks for ai-team-sync."""
    pass


@hooks.command("install")
@click.option("--force", is_flag=True, help="Overwrite existing hooks")
def hooks_install(force):
    """Install ai-team-sync git hooks in the current repository."""
    import shutil
    from pathlib import Path

    # Find git hooks directory
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=True,
        )
        git_dir = Path(result.stdout.strip())
    except subprocess.CalledProcessError:
        click.echo("Error: Not in a git repository", err=True)
        raise SystemExit(1)

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)

    # Find source hooks (installed with package)
    package_dir = Path(__file__).parent
    source_hooks_dir = package_dir / "hooks"

    # Hook mapping: source file -> git hook name
    hooks_to_install = {
        "pre_commit.py": "pre-commit",
        "prepare_commit_msg.py": "prepare-commit-msg",
        "post_checkout.py": "post-checkout",
    }

    installed = []
    skipped = []

    for source_file, hook_name in hooks_to_install.items():
        source = source_hooks_dir / source_file
        dest = hooks_dir / hook_name

        if not source.exists():
            click.echo(f"Warning: {source_file} not found, skipping", err=True)
            continue

        if dest.exists() and not force:
            skipped.append(hook_name)
            continue

        shutil.copy(source, dest)
        dest.chmod(0o755)  # Make executable
        installed.append(hook_name)

    if installed:
        click.echo(f"Installed hooks: {', '.join(installed)}")
    if skipped:
        click.echo(f"Skipped (already exist): {', '.join(skipped)}")
        click.echo("Use --force to overwrite existing hooks")

    if installed or skipped:
        click.echo("\nGit hooks are now active for this repository.")


@hooks.command("uninstall")
def hooks_uninstall():
    """Remove ai-team-sync git hooks from the current repository."""
    from pathlib import Path

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=True,
        )
        git_dir = Path(result.stdout.strip())
    except subprocess.CalledProcessError:
        click.echo("Error: Not in a git repository", err=True)
        raise SystemExit(1)

    hooks_dir = git_dir / "hooks"
    hook_names = ["pre-commit", "prepare-commit-msg", "post-checkout"]

    removed = []
    for hook_name in hook_names:
        hook_file = hooks_dir / hook_name
        if hook_file.exists():
            # Check if it's our hook (contains ai-team-sync marker)
            try:
                with open(hook_file) as f:
                    content = f.read()
                if "ai-team-sync" in content:
                    hook_file.unlink()
                    removed.append(hook_name)
            except Exception:
                pass

    if removed:
        click.echo(f"Removed hooks: {', '.join(removed)}")
    else:
        click.echo("No ai-team-sync hooks found.")


# --- Session file helpers ---

def _session_file() -> str:
    # Through session_pointer so $ATS_STATE_DIR isolates a delegated child.
    try:
        from ai_team_sync import session_pointer as sp
        return str(sp.global_pointer_path())
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".ats_session")


def _save_active_session(session_id: str):
    with open(_session_file(), "w") as f:
        f.write(session_id)
    # #2517: the global file names whichever session wrote it last, so with two
    # concurrent sessions `ats status` flapped between "no active session" and
    # someone else's id. The per-cid pointer (session_pointer) is authoritative
    # when a Claude session id is known; the global file stays for hookless use.
    try:
        from ai_team_sync import session_pointer as sp
        sp.save_pointer(session_id)
    except Exception:
        pass


def _load_active_session() -> str | None:
    try:
        from ai_team_sync import session_pointer as sp
        sid = sp.resolve_pointer()
        if sid:
            return sid
    except Exception:
        pass
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
    # #2517: also drop the per-cid pointer, or `ats status` keeps resolving the
    # session that was just completed (the pointer outliving the row is the
    # same stale-identity class the save/load wiring fixed).
    try:
        from ai_team_sync import session_pointer as sp
        sp.clear_pointer()
    except Exception:
        pass


@cli.command()
def init():
    """Set up ai-team-sync for this repo. Creates .ai-team-sync.toml and validates the server."""
    root = _repo_root()
    if not root:
        click.echo("Not inside a git repository. Run this from your project root.", err=True)
        raise SystemExit(1)

    click.echo("ai-team-sync setup\n")

    # Gather settings
    server_url = click.prompt("Server URL", default=DEFAULT_SERVER)
    git_name = "unknown"
    try:
        result = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, check=True
        )
        git_name = result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    dev_name = click.prompt("Your name", default=git_name)
    lock_mode = click.prompt("Default lock mode", default="advisory", type=click.Choice(["advisory", "exclusive"], case_sensitive=False))

    # Write config
    config_path = os.path.join(root, ".ai-team-sync.toml")
    toml_content = f"""# ai-team-sync team configuration
# Commit this file so your team shares the same settings.

[server]
url = "{server_url}"

[developer]
name = "{dev_name}"

[locks]
default_mode = "{lock_mode}"
"""
    with open(config_path, "w") as f:
        f.write(toml_content)
    click.echo(f"\nWrote {config_path}")

    # Validate server connection
    click.echo(f"\nConnecting to {server_url}...")
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(f"{server_url}/health")
            if resp.status_code == 200:
                click.echo("  Server is running.\n")
            else:
                click.echo(f"  Server returned {resp.status_code} — check the URL.\n", err=True)
    except httpx.ConnectError:
        click.echo("  Could not connect. Start the server with: ats-server\n", err=True)
    except Exception as e:
        click.echo(f"  Connection failed: {e}\n", err=True)

    # Show quick start
    click.echo("Ready! Here's how to use it:\n")
    click.echo("  # Tell your team what you're working on")
    click.echo("  ats session start -s 'src/auth/**' -d 'Refactoring auth module'")
    click.echo()
    click.echo("  # See what everyone is doing")
    click.echo("  ats team")
    click.echo()
    click.echo("  # Check if a file is locked by a teammate")
    click.echo("  ats lock check src/auth/login.py")
    click.echo()
    click.echo("  # Log why the AI chose a particular approach")
    click.echo("  ats decision log 'Chose X over Y' -c 'X' -r 'Y' --reason 'because...'")
    click.echo()
    click.echo("  # Finish up — releases locks, notifies team")
    click.echo("  ats session complete -m 'Done, auth refactored'")
    click.echo()
    click.echo(f"  # Web dashboard (if server is running)")
    click.echo(f"  open {server_url}/dashboard")


# ---------------------------------------------------------------------------
# delegate — bounded worker-to-worker work, through ATS rather than a raw shell
# ---------------------------------------------------------------------------

@cli.command("delegate")
@click.option("--task", "parent_task", default="", help="Parent task id this serves, e.g. 2654")
@click.option("--parent-session", default="", help="Delegating session id (default: your current one)")
@click.option("--worker", default="claude-code", help="Worker to delegate to")
@click.option("--mode", type=click.Choice(["READ_ONLY", "IMPLEMENT", "VERIFY"]),
              default="READ_ONLY", help="Authority envelope granted to the child")
@click.option("--scope", "-s", multiple=True, help="Scope glob (repeatable); IMPLEMENT only")
@click.option("--repo", default="", help="Absolute repo root")
@click.option("--objective", "-o", required=True, help="The ONE question or change")
@click.option("--acceptance", "-a", required=True, help="What a satisfactory answer must contain")
@click.option("--lease-minutes", default=30, show_default=True)
@click.option("--dry-run", is_flag=True, help="Create the record and print the child packet; launch nothing")
def delegate(parent_task, parent_session, worker, mode, scope, repo, objective,
             acceptance, lease_minutes, dry_run):
    """Delegate a bounded subproblem to another worker.

    The delegating worker keeps ownership throughout: this creates a CHILD
    record, never a handoff. The child is launched with the mode's own harness
    restrictions and receives a freshly built packet — the parent's conversation
    is deliberately NOT inherited, so one worker's intermediate reasoning cannot
    contaminate the next, and the exchange stays reproducible from the record.
    """
    import sys
    from ai_team_sync.launch_spec import (SPEC_VERSION, RoutingFailure,
                                          build_launch, validate_launchable)
    from ai_team_sync.briefs import fetch_tower_task_envelope
    from ai_team_sync.delegation_packet import build_child_packet

    server = _server_url()
    parent_session = parent_session or (_load_active_session() or "")
    if not parent_session:
        click.echo("No parent session. Run `ats session start` first.", err=True)
        sys.exit(1)
    repo = repo or (_repo_root() or "")

    # Resolve the launcher BEFORE creating any record. Two reasons, both learned
    # the hard way. A worker that cannot be launched must leave no delegation row
    # and no child session behind for a spawn that never happened. And the binary
    # is resolved HERE, in the parent, because that is the one identity value the
    # child cannot influence -- child_env force-sets ATS_AGENT, so the child's own
    # answer to "who are you" is just the parent's label read back.
    try:
        _spec, resolved_binary = validate_launchable(worker, mode, repo=repo)
    except RoutingFailure as exc:
        click.echo(f"delegation refused (routing failure): {exc}", err=True)
        sys.exit(3)

    # The CURRENT task's authority, fetched before anything is created so a
    # missing envelope costs no records. A delegation that NAMES a task and then
    # launches without its acceptance criteria is worse than one with no task at
    # all: the packet still says "parent task: 2649", so the child reasonably
    # assumes the constraints arrived with it and reconstructs them when they
    # did not. That reconstruction is the failure this whole path exists to end.
    task_envelope_text = ""
    if parent_task:
        task_envelope_text, envelope_error = fetch_tower_task_envelope(parent_task)
        if envelope_error:
            click.echo(
                f"delegation refused (no task authority): task {parent_task!r} was "
                f"named explicitly but its envelope could not be fetched — "
                f"{envelope_error}. Refusing to launch a child that would have to "
                f"reconstruct the acceptance criteria. Drop --task to delegate "
                f"without task authority, or fix the id / Echo Brain.", err=True)
            sys.exit(4)

    with httpx.Client(timeout=30) as c:
        resp = c.post(f"{server}/api/delegations", json={
            "parent_session_id": parent_session, "parent_task": parent_task,
            "delegated_worker": worker, "mode": mode, "repo_root": repo,
            "scope": list(scope), "objective": objective, "acceptance": acceptance,
            "lease_minutes": lease_minutes,
            # Truth about the spawn, sent so the SERVER can re-derive whether
            # this record is allowed to claim `worker` at all.
            "resolved_binary": resolved_binary,
            "launch_spec_version": SPEC_VERSION,
        })
        if resp.status_code >= 400:
            click.echo(f"delegation refused: {resp.text}", err=True)
            sys.exit(2)
        d = resp.json()

        child = c.post(f"{server}/api/sessions", json={
            "developer": _get_developer(), "agent": f"{worker}:delegate",
            # A READ_ONLY/VERIFY child claims nothing; the server refuses it anyway.
            "scope": list(scope) if mode == "IMPLEMENT" else [],
            "description": f"delegated {mode} for task {parent_task or '-'}: {objective}",
            "repo_root": repo, "delegation_id": d["id"],
        })
        if child.status_code >= 400:
            click.echo(f"child session refused: {child.text}", err=True)
            sys.exit(2)
        child_id = child.json()["id"]

        brief = ""
        try:
            b = c.post(f"{server}/api/brief", json={
                "objective": objective, "repo_root": repo,
                "scope": list(scope), "limit": 6}, timeout=40)
            brief = (b.json() or {}).get("rendered", "")
        except Exception as exc:  # noqa: BLE001
            brief = f"(no brief: {type(exc).__name__})"

    packet = build_child_packet(
        mode=mode, delegation=d, objective=objective, acceptance=acceptance,
        scope=list(scope), task_envelope_text=task_envelope_text, brief=brief)

    if dry_run:
        click.echo(json.dumps({"delegation": d, "child_session": child_id,
                               "packet": packet}, indent=2))
        return

    # THE defect this replaced: `argv = ["claude", "-p", packet, ...]` ran Claude
    # for every worker, so `--worker codex` produced a Claude run recorded as a
    # Codex review (proven live 2026-09-12, delegations 82fb4676 / 5c04aa74).
    # build_launch picks the binary AND the enforcement flags together, because
    # swapping only the binary would hand Claude's --disallowedTools to Codex,
    # where they mean nothing and READ_ONLY would decay to a promise.
    # The child's environment is computed BEFORE the command line, because for
    # some workers it IS part of the command line: Codex starts its MCP servers
    # from its own config, whose env block replaces rather than extends what it
    # inherits, so the isolation vars have to ride in as -c overrides.
    from ai_team_sync.delegation import child_env as _child_env
    env = _child_env(dict(os.environ), delegation_id=d["id"],
                     child_session_id=child_id, worker=worker)

    launch = build_launch(worker, mode, packet, repo=repo, child_env=env)
    argv = launch.argv
    click.echo(f"launching {worker} via {launch.resolved_binary} "
               f"({mode}, lease {lease_minutes}m)...", err=True)
    try:
        # stdin closed: the child is not interactive, and left open the harness
        # waits on it before starting.
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=lease_minutes * 60, env=env,
                              stdin=subprocess.DEVNULL)
        output, failure = proc.stdout.strip(), (proc.returncode != 0)
    except subprocess.TimeoutExpired:
        output, failure = "", True
        click.echo("child exceeded its lease", err=True)

    with httpx.Client(timeout=30) as c:
        ret = c.post(f"{server}/api/delegations/{d['id']}/return", json={
            "result_summary": output[:20000],
            "evidence": {"exit_ok": not failure, "child_session_id": child_id},
            "actor_session_id": child_id,
        })
        c.patch(f"{server}/api/sessions/{child_id}",
                json={"status": "completed",
                      "summary": f"delegated {mode}: {objective[:120]}"})

    click.echo(json.dumps({
        "delegation_id": d["id"],
        "state": (ret.json().get("state") if ret.status_code < 400 else "return_refused"),
        "mode": mode,
        # Requested identity and the binary that actually ran, never collapsed
        # into one "worker" field a reader would have to trust.
        "requested_worker": worker,
        "resolved_binary": launch.resolved_binary,
        "launch_spec_version": launch.spec_version,
        "parent_still_owns": d["parent_owner_session_id"],
        "child_session_id": child_id,
        "result": output,
        "verify_before_accepting": d["acceptance"],
    }, indent=2))



if __name__ == "__main__":
    cli()
