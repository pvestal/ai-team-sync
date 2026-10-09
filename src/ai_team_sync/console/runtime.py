"""Runtime proof: which OS process is actually behind each ATS session label.

An ATS agent string ("codex:delegate", "claude-code:fff39404") is a claim the
agent made about itself. This module checks it against the process table,
read-only, from three joins that exist independently of the label:

* ``~/.ats_live_cid_<pid>`` — the SessionStart hook's record of which Claude
  session id a Claude host process is running (session_pointer, #2003).
* ``ATS_SESSION_ID=<uuid>`` in a process's argv or environment — how
  ``ats delegate`` hands a Codex worker its session.
* ``~/.claude/projects/*/<cid>/subagents/*.meta.json`` — which Claude subagent
  (agentType) issued a Bash command, matched against the spawned argv.

Only argv, exe path, ppid and start-time are read from /proc, plus the single
ATS_SESSION_ID key from environ. Nothing else from environ is retained.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

from .sanitize import safe_text

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ARGV_SESSION = re.compile(rf"ATS_SESSION_ID=\"?({_UUID})")
_SUBAGENT_WINDOW_S = 12 * 3600
_TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024
_GAP_WINDOW_S = 24 * 3600
INTERESTING = {"claude", "ats", "codex"}


@dataclass
class Proc:
    pid: int
    ppid: int
    argv: list[str]
    exe: str = ""
    starttime: str = ""
    session_marker: str | None = None
    runtime: str = "other"
    children: list[int] = field(default_factory=list)


def classify(argv: list[str], exe: str = "") -> str:
    """Runtime from the executable actually running, never from an agent label."""
    if not argv:
        return "other"
    head = os.path.basename(argv[0])
    second = os.path.basename(argv[1]) if len(argv) > 1 else ""
    # argv[0], not the exe: Claude Code re-executes its own binary for bundled
    # tools (ugrep), so the exe path alone does not mean an agent process.
    if head == "claude":
        return "claude"
    if head.startswith("codex") or "@openai/codex" in exe or "/.codex/" in exe:
        return "codex"
    if head == "node" and second == "codex":
        return "codex"
    if head == "ats" or (head.startswith("python") and second == "ats"):
        return "ats"
    if head in {"bash", "sh", "zsh", "dash"}:
        return "shell"
    if head == "timeout":
        return "wrapper"
    return "other"


def label_runtime(agent: str) -> str:
    prefix = (agent or "").split(":", 1)[0].lower()
    if prefix.startswith("claude"):
        return "claude"
    if prefix.startswith("codex"):
        return "codex"
    return "unknown"


def _read(path: Path, limit: int = 65536) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(limit)


def read_proc_table(proc_root: str = "/proc") -> dict[int, Proc]:
    """Snapshot of this user's readable processes. Unreadable rows are skipped."""
    table: dict[int, Proc] = {}
    root = Path(proc_root)
    try:
        entries = [entry for entry in os.listdir(root) if entry.isdigit()]
    except OSError:
        return table
    for entry in entries:
        base = root / entry
        try:
            stat = _read(base / "stat").decode(errors="replace")
            argv = [
                part.decode(errors="replace")
                for part in _read(base / "cmdline").split(b"\0")
                if part
            ]
        except OSError:
            continue
        after = stat.rsplit(")", 1)[-1].split()
        if len(after) < 20 or not argv:
            continue
        try:
            exe = os.readlink(base / "exe")
        except OSError:
            exe = ""
        proc = Proc(pid=int(entry), ppid=int(after[1]), argv=argv, exe=exe, starttime=after[19])
        match = _ARGV_SESSION.search(" ".join(argv))
        if match:
            proc.session_marker = match.group(1)
        else:
            try:
                for item in _read(base / "environ", 262144).split(b"\0"):
                    if item.startswith(b"ATS_SESSION_ID="):
                        value = item.split(b"=", 1)[1].decode(errors="replace").strip()
                        proc.session_marker = value if re.fullmatch(_UUID, value) else None
                        break
            except OSError:
                pass
        proc.runtime = classify(argv, exe)
        table[proc.pid] = proc
    for proc in table.values():
        if proc.ppid in table:
            table[proc.ppid].children.append(proc.pid)
    return table


def read_live_cids(home: str, procs: dict[int, Proc]) -> dict[int, str]:
    """Claude host pid -> live Claude session id, dropping recycled pids."""
    found: dict[int, str] = {}
    try:
        names = os.listdir(home)
    except OSError:
        return found
    for name in names:
        if not name.startswith(".ats_live_cid_"):
            continue
        try:
            record = json.loads(_read(Path(home) / name))
            pid = int(record.get("pid") or name.rsplit("_", 1)[-1])
        except (OSError, ValueError, TypeError):
            continue
        proc = procs.get(pid)
        if not proc or proc.runtime != "claude":
            continue
        recorded = str(record.get("starttime") or "")
        if recorded and recorded != proc.starttime:
            continue
        cid = str(record.get("cid") or "").strip()
        if cid:
            found[pid] = cid
    return found


def read_subagent_commands(home: str, cids: list[str]) -> list[dict]:
    """Bash commands issued by Claude subagents of the given live sessions.

    Returns only agentType, description and the command string used for argv
    matching. Transcript content is never surfaced.
    """
    out: list[dict] = []
    projects = Path(home) / ".claude" / "projects"
    now = time.time()
    for cid in cids:
        for sub_dir in projects.glob(f"*/{cid}/subagents"):
            for meta_path in sub_dir.glob("*.meta.json"):
                transcript = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
                try:
                    if now - transcript.stat().st_mtime > _SUBAGENT_WINDOW_S:
                        continue
                    meta = json.loads(_read(meta_path))
                    size = transcript.stat().st_size
                    with open(transcript, "rb") as fh:
                        fh.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
                        tail = fh.read().decode(errors="replace")
                except (OSError, ValueError):
                    continue
                for line in tail.splitlines():
                    if '"Bash"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    content = (row.get("message") or {}).get("content")
                    for block in content if isinstance(content, list) else []:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("name") == "Bash"
                        ):
                            command = str((block.get("input") or {}).get("command") or "")
                            if command:
                                out.append(
                                    {
                                        "cid": cid,
                                        "agent_type": safe_text(meta.get("agentType"), 60),
                                        "description": safe_text(meta.get("description"), 120),
                                        "command": " ".join(command.split()),
                                    }
                                )
    return out


def _epoch(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def _host_claude(pid: int, procs: dict[int, Proc]) -> int | None:
    seen = set()
    while pid in procs and pid not in seen:
        seen.add(pid)
        if procs[pid].runtime == "claude":
            return pid
        pid = procs[pid].ppid
    return None


def _match_subagent(proc: Proc, host_cid: str | None, commands: list[dict]) -> dict | None:
    """Which subagent Bash call spawned this process, by command text.

    A wrapper's argv ("timeout 590 ats delegate ...") is a prefix of the
    command; a ``bash -c`` hop instead carries the command inside its script.
    Both directions are checked on a 60-character prefix.
    """
    if not host_cid:
        return None
    argv = [os.path.basename(proc.argv[0]), *proc.argv[1:]]
    hay = " ".join(" ".join(argv).split())
    if len(hay) < 12:
        return None
    for row in commands:
        if row["cid"] != host_cid:
            continue
        command = row["command"]
        if hay[:60] in command or (len(command) >= 12 and command[:60] in hay):
            return row
    return None


def build_runtime_view(
    sessions: list[dict],
    procs: dict[int, Proc],
    live_cids: dict[int, str],
    subagent_commands: list[dict] | None = None,
    now: float | None = None,
) -> dict:
    """Join ATS sessions to processes and return a renderable verdict tree."""
    commands = subagent_commands or []
    now = time.time() if now is None else now
    active = [row for row in sessions if row.get("status") == "active"]
    by_id = {row.get("id"): row for row in sessions}

    claude_session: dict[int, dict] = {}
    for pid, cid in live_cids.items():
        agent = f"claude-code:{cid[:8]}"
        match = next((row for row in active if row.get("agent") == agent), None)
        claude_session[pid] = match or {"id": None, "agent": agent, "cid": cid}

    attributions: dict[int, dict] = {}
    for pid, proc in procs.items():
        if proc.session_marker and proc.runtime in INTERESTING:
            # The outermost marked process of a chain is the attribution point;
            # its codex children inherit the marker and are rendered beneath it.
            parent = procs.get(proc.ppid)
            if parent and parent.session_marker == proc.session_marker:
                continue
            row = by_id.get(proc.session_marker) or {
                "id": proc.session_marker,
                "agent": "UNREGISTERED",
            }
            attributions[pid] = row
    for pid, row in claude_session.items():
        attributions[pid] = row

    verdicts: dict[str, dict] = {}
    for pid, row in attributions.items():
        sid = row.get("id")
        if not sid:
            continue
        claimed = label_runtime(str(row.get("agent")))
        actual = procs[pid].runtime
        verdicts[sid] = {
            "state": "VERIFIED" if claimed == actual else "MISMATCH",
            "pid": pid,
            "label_runtime": claimed,
            "process_runtime": actual,
            "evidence": (
                f"~/.ats_live_cid_{pid}" if pid in live_cids else "ATS_SESSION_ID on process"
            ),
        }
    for row in active:
        if row.get("id") not in verdicts:
            verdicts[row["id"]] = {
                "state": "NO_PROCESS",
                "label_runtime": label_runtime(str(row.get("agent"))),
                "evidence": "no live process carries this session",
            }

    def node(pid: int, via: list[str]) -> dict:
        proc = procs[pid]
        host = _host_claude(pid, procs)
        sub = (
            _match_subagent(proc, live_cids.get(host) if host else None, commands)
            if proc.runtime == "ats" and not any(hop.startswith("subagent:") for hop in via)
            else None
        )
        session = attributions.get(pid)
        return {
            "pid": pid,
            "runtime": proc.runtime,
            "argv": safe_text(" ".join(proc.argv), 140),
            "via": via,
            "session": (
                {
                    "id": session.get("id"),
                    "agent": session.get("agent"),
                    "task_id": session.get("task_id"),
                    "mode": session.get("effective_mode"),
                    "parent": session.get("parent_session_id"),
                    "verdict": verdicts.get(session.get("id") or "", {}).get("state"),
                }
                if session
                else None
            ),
            "subagent": (
                {"agent_type": sub["agent_type"], "description": sub["description"]}
                if sub
                else None
            ),
            "children": collect(pid),
        }

    def collect(pid: int) -> list[dict]:
        """Interesting descendants, collapsing shell/wrapper hops into ``via``."""
        out: list[dict] = []
        stack = [(child, []) for child in procs[pid].children]
        while stack:
            child, via = stack.pop()
            proc = procs[child]
            if proc.runtime in INTERESTING:
                out.append(node(child, via))
            elif proc.runtime in {"shell", "wrapper"}:
                hop = os.path.basename(proc.argv[0])
                if not any(item.startswith("subagent:") for item in via):
                    sub = _match_subagent(
                        proc, live_cids.get(_host_claude(child, procs) or -1), commands
                    )
                    if sub:
                        hop = f"subagent:{sub['agent_type']}"
                stack.extend((grand, via + [hop]) for grand in procs[child].children)
        return sorted(out, key=lambda item: item["pid"])

    roots = sorted(live_cids)
    for pid, proc in procs.items():
        if proc.runtime == "codex" and proc.session_marker and _host_claude(pid, procs) is None:
            parent = procs.get(proc.ppid)
            if not (parent and parent.session_marker == proc.session_marker):
                roots.append(pid)
    tree = [node(pid, []) for pid in roots]

    # A delegate is spawned by `ats delegate`; one with no parent and no
    # delegation is a second registration the delegation cannot account for.
    # Operator-launched controllers are legitimately parentless and excluded.
    cutoff = now - _GAP_WINDOW_S
    gaps = [
        {
            "id": row.get("id"),
            "agent": row.get("agent"),
            "task_id": row.get("task_id"),
            "started_at": row.get("started_at"),
            "reason": "delegate session not linked to any delegation or parent",
        }
        for row in sessions
        if row.get("agent") == "codex:delegate"
        and not row.get("parent_session_id")
        and not row.get("delegation_id")
        and _epoch(row.get("started_at")) >= cutoff
    ]
    return {"state": "OBSERVED", "tree": tree, "verdicts": verdicts, "lineage_gaps": gaps[:20]}


def runtime_snapshot(
    sessions: list[dict], home: str | None = None, proc_root: str = "/proc"
) -> dict:
    home = home or str(Path.home())
    procs = read_proc_table(proc_root)
    if not procs:
        return {"state": "UNAVAILABLE", "tree": [], "verdicts": {}, "lineage_gaps": []}
    live = read_live_cids(home, procs)
    commands = read_subagent_commands(home, sorted(set(live.values())))
    return build_runtime_view(sessions, procs, live, commands)


def render_lines(view: dict, selected_session: str | None = None) -> list[tuple[str, str]]:
    """(text, style) lines for the RUNTIME CHAIN pane."""
    colors = {"claude": "magenta", "codex": "cyan", "ats": "blue"}
    lines: list[tuple[str, str]] = []

    def walk(item: dict, depth: int) -> None:
        pad = "  " * depth + ("└ " if depth else "")
        hops = [hop for hop in item["via"] if hop.startswith("subagent:")]
        if item.get("subagent"):
            hops.append(f"subagent:{item['subagent']['agent_type']}")
        for hop in hops[:1]:
            lines.append((f"{'  ' * depth}┊ via Claude {hop} (in-process, no pid)", "magenta"))
        label = f"{pad}{item['runtime'].upper()} pid {item['pid']}"
        session = item.get("session")
        if session:
            if session.get("id"):
                mark = {"VERIFIED": "✓", "MISMATCH": "✗ LABEL≠PROCESS"}.get(session["verdict"], "?")
                task = f" #{session['task_id']}" if session.get("task_id") else ""
                mode = f" {session['mode']}" if session.get("mode") else ""
                label += f"  ⇐ {session['agent']} {session['id'][:8]}{task}{mode} {mark}"
            else:
                label += f"  ⇐ {session['agent']} (no active ATS session)"
        style = colors.get(item["runtime"], "white")
        if session and selected_session and session.get("id") == selected_session:
            style = f"bold reverse {style}"
        if session and session.get("verdict") == "MISMATCH":
            style = "bold red"
        lines.append((label, style))
        if item["runtime"] in {"ats"} or (
            session is None and item["runtime"] == "codex" and depth < 3
        ):
            lines.append((f"{'  ' * (depth + 1)}{item['argv'][:110]}", "bright_black"))
        for child in item["children"]:
            walk(child, depth + 1)

    for root in view.get("tree", []):
        walk(root, 0)
    if not lines:
        lines.append(("no live Claude/Codex processes", "bright_black"))
    gaps = view.get("lineage_gaps", [])
    if gaps:
        lines.append(
            (f"LINEAGE GAPS {len(gaps)} in 24h: delegate sessions with no parent", "yellow")
        )
    for gap in sorted(gaps, key=lambda row: str(row.get("started_at") or ""), reverse=True)[:3]:
        task = f" #{gap['task_id']}" if gap.get("task_id") else ""
        lines.append((f"  {gap['agent']} {str(gap['id'])[:8]}{task}", "yellow"))
    return lines
