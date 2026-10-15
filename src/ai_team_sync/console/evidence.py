"""Read-only evidence collectors for claim verification (#3589).

Every function here observes; none writes. Sources fall in two classes, and
the class decides what a source may prove (see verify.py):

* SELF_REPORTED -- written by the agents being checked: ATS rows (sessions,
  delegations, handoffs, decisions, commit records), ticket text and
  verified_by, agent messages.
* OBSERVED -- produced by something other than the claimant: git objects and
  ancestry, process start times from systemd, installed package bytes, a
  running service answering a probe, an intake service's own receipts, a
  migration ledger, and the transcripts the Claude Code and Codex harnesses
  write themselves (tool commands and their exit status, the operator's typed
  turns).

Site-specific locations (ticket service, receipts, ledgers, deploy targets)
come from a machine-local config file; see ``EvidenceConfig``.

ATS is read straight from its SQLite file opened with ``mode=ro``. The HTTP
surfaces cap or post-filter ticket history, withhold the fields a verdict
needs, and two of the GET routes change state (message claim, lease settle).
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx

from .sanitize import safe_text

_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_PYTEST_SUMMARY = re.compile(
    r"(?:=+ )?((?:\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)"
    r"(?:, )?)+)(?: in [\d.]+s)?"
)
_TEST_COMMAND = re.compile(r"\bpytest\b|-m pytest|\bpython3? tests/")
_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_PYTEST_INVOCATION = re.compile(
    r"(?:^|[;&|(]\s*|\s)(?:\S*/)?(?:pytest|py\.test)(?:\s|$)|python[\d.]*\s+-m\s+pytest\b"
)
_DISPLAY_ONLY = {
    "cat",
    "grep",
    "egrep",
    "rg",
    "tail",
    "head",
    "echo",
    "printf",
    "sed",
    "awk",
    "ls",
    "wc",
    "sort",
    "uniq",
    "cd",
    "true",
    "test",
    "[",
    "less",
    "nl",
    "jq",
    "cut",
    "tr",
}


def invokes_pytest(command: str) -> bool:
    """True when pytest is invoked as a command, not merely named in a string."""
    return bool(_PYTEST_INVOCATION.search(_QUOTED.sub("''", command or "")))


def display_only(command: str) -> bool:
    """True when every segment only prints existing data (cat a log, grep a file).

    Output printed this way was produced earlier by something the transcript
    does not show executing here; it is not an observed execution.
    """
    unquoted = _QUOTED.sub("''", command or "")
    segments = [seg.strip() for seg in re.split(r"&&|\|\||[;|\n]", unquoted) if seg.strip()]
    for segment in segments:
        words = [w for w in segment.split() if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
        if words and os.path.basename(words[0]) not in _DISPLAY_ONLY:
            return False
    return True


def parse_time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _read_toml(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


@dataclass(frozen=True)
class DeployTarget:
    """How code in one repository reaches a running process on this host."""

    repo: str
    units: tuple[tuple[str, bool], ...]  # (unit, is_user_unit)
    install_dir: str = ""  # installed package dir when deployed by install
    source_prefix: str = ""  # path of that package inside the repository
    probe_url: str = ""  # an HTTP endpoint the running service itself answers
    probe_reports_commit: bool = False  # the probe body's "commit" names the running build


@dataclass(frozen=True)
class Ledger:
    """A database migration ledger: one row per applied migration id."""

    db: str
    host: str = ""
    user: str = ""
    table: str = "schema_migrations"


@dataclass(frozen=True)
class EvidenceConfig:
    """Where the evidence lives on this machine.

    Nothing site-specific is built in. ``from_environment`` reads it from a
    machine-local file (``$ATS_VERIFY_CONFIG``, else
    ``~/.config/ai-team-sync/verify.toml``) that is never committed; without it
    no ticket service, receipts, ledgers or deploy targets are consulted.
    """

    ats_db: str
    home: str = str(Path.home())
    ticket_url: str = ""  # JSON ticket record; "{ticket}" is replaced by the id
    receipt_url: str = ""  # intake receipts for a session; "{session_id}" replaced
    ledgers: dict[str, Ledger] = field(default_factory=dict)  # deploy target name -> ledger
    targets: tuple[DeployTarget, ...] = ()

    @classmethod
    def from_environment(cls) -> "EvidenceConfig":
        home = str(Path.home())
        url = os.getenv("DATABASE_URL", "")
        db = url.split(":///", 1)[1] if url.startswith("sqlite") and ":///" in url else ""
        db = db or os.path.join(home, ".local/share/ai-team-sync/ats.db")
        path = os.getenv("ATS_VERIFY_CONFIG") or os.path.join(
            home, ".config/ai-team-sync/verify.toml"
        )
        return cls.from_mapping(_read_toml(path), ats_db=os.getenv("ATS_DB_PATH", db), home=home)

    @classmethod
    def from_mapping(cls, data: dict[str, Any], *, ats_db: str, home: str) -> "EvidenceConfig":
        targets = tuple(
            DeployTarget(
                repo=os.path.expanduser(str(t["repo"])),
                units=tuple(
                    (str(u["name"]), bool(u.get("user", False))) for u in t.get("units") or []
                ),
                install_dir=os.path.expanduser(str(t.get("install_dir", ""))),
                source_prefix=str(t.get("source_prefix", "")),
                probe_url=str(t.get("probe_url", "")),
                probe_reports_commit=bool(t.get("probe_reports_commit", False)),
            )
            for t in data.get("targets") or []
        )
        ledgers = {
            str(name): Ledger(
                db=str(spec["db"]),
                host=str(spec.get("host", "")),
                user=str(spec.get("user", "")),
                table=str(spec.get("table", "schema_migrations")),
            )
            for name, spec in (data.get("ledgers") or {}).items()
        }
        return cls(
            ats_db=ats_db,
            home=home,
            ticket_url=str(data.get("ticket_url", "")),
            receipt_url=str(data.get("receipt_url", "")),
            ledgers=ledgers,
            targets=targets,
        )

    def ticket_endpoint(self, ticket_id: int) -> str:
        return self.ticket_url.replace("{ticket}", str(ticket_id)) if self.ticket_url else ""

    def receipt_endpoint(self, session_id: str) -> str:
        return self.receipt_url.replace("{session_id}", session_id) if self.receipt_url else ""

    def target_for(self, repo: str) -> DeployTarget | None:
        repo = os.path.realpath(repo or "")
        for target in self.targets:
            if os.path.realpath(target.repo) == repo:
                return target
        return None


# --------------------------------------------------------------------------- ATS


class AtsRecords:
    """ATS rows for one ticket, read from the SQLite file in read-only mode."""

    def __init__(self, path: str):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def ticket(self, ticket_id: int) -> dict[str, Any]:
        """Sessions, delegations, handoffs, decisions and commits of one ticket.

        A delegated child session usually carries no ticket_id; it belongs to
        the ticket through its delegation's parent_task.
        """
        delegations = self._rows(
            "SELECT * FROM delegations WHERE parent_task = ? ORDER BY created_at",
            (str(ticket_id),),
        )
        direct = self._rows(
            "SELECT * FROM sessions WHERE ticket_id = ? OR task_id = ? ORDER BY started_at",
            (ticket_id, ticket_id),
        )
        ids = {row["id"] for row in direct}
        linked = {d["child_session_id"] for d in delegations if d["child_session_id"]}
        linked |= {d["parent_session_id"] for d in delegations}
        missing = sorted(linked - ids)
        extra = (
            self._rows(
                f"SELECT * FROM sessions WHERE id IN ({','.join('?' * len(missing))})",
                tuple(missing),
            )
            if missing
            else []
        )
        sessions = sorted([*direct, *extra], key=lambda row: str(row["started_at"]))
        sids = [row["id"] for row in sessions]
        marks = ",".join("?" * len(sids)) or "''"
        handoffs = self._rows(
            "SELECT h.*, m.id AS message_id, m.delivery_history, m.acknowledged_at, "
            "m.addressing_mode FROM handoffs h LEFT JOIN agent_messages m "
            "ON m.handoff_id = h.id WHERE h.ticket_id = ? ORDER BY h.created_at",
            (ticket_id,),
        )
        decisions = self._rows(
            f"SELECT * FROM decisions WHERE ticket_id = ? OR session_id IN ({marks}) "
            "ORDER BY created_at",
            (ticket_id, *sids),
        )
        commits = self._rows(
            f"SELECT * FROM commit_records WHERE session_id IN ({marks}) ORDER BY created_at",
            tuple(sids),
        )
        edits = self._rows(
            f"SELECT session_id, action, count(*) AS n, min(created_at) AS first, "
            f"max(created_at) AS last FROM file_activities WHERE session_id IN ({marks}) "
            "GROUP BY session_id, action",
            tuple(sids),
        )
        restarts = self._rows(
            f"SELECT * FROM service_restarts WHERE session_id IN ({marks}) ORDER BY created_at",
            tuple(sids),
        )
        return {
            "sessions": sessions,
            "delegations": delegations,
            "handoffs": handoffs,
            "decisions": decisions,
            "commits": commits,
            "file_activity": edits,
            "restarts": restarts,
            "completion_events": self._completion_events(sids),
        }

    def _completion_events(self, sids: list[str]) -> list[dict]:
        """ATS's record of each completion event it emitted and the handoff it carried.

        The event id is what the intake receipt names, so this row is what ties a
        receipt to one specific handoff.
        """
        if not sids:
            return []
        marks = ",".join("?" * len(sids))
        try:
            rows = self._rows(
                f"SELECT event_id, session_id, payload FROM completion_outbox "
                f"WHERE session_id IN ({marks})",
                tuple(sids),
            )
        except sqlite3.OperationalError:  # an ATS database older than the outbox
            return []
        events = []
        for row in rows:
            try:
                payload = json.loads(row.get("payload") or "{}")
            except ValueError:
                payload = {}
            handoff = payload.get("handoff") if isinstance(payload, dict) else None
            events.append(
                {
                    "event_id": row["event_id"],
                    "session_id": row["session_id"],
                    "handoff_id": handoff.get("handoff_id") if isinstance(handoff, dict) else None,
                }
            )
        return events


# --------------------------------------------------------------------------- git


def git(repo: str, *args: str, timeout: float = 10.0) -> tuple[int, str]:
    """Run one read-only git command with a fixed argv and no shell."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 128, type(exc).__name__
    return proc.returncode, proc.stdout


def git_commit(repo: str, ref: str) -> dict | None:
    """Full sha, commit time and subject, or None when the object is absent."""
    if not ref or not _SHA.match(ref.lower()):
        return None
    code, out = git(repo, "log", "-1", "--format=%H%x00%cI%x00%s", f"{ref}^{{commit}}", "--")
    if code != 0 or not out.strip():
        return None
    sha, when, subject = (out.strip().split("\x00") + ["", ""])[:3]
    return {"sha": sha, "committed_at": when, "subject": safe_text(subject, 200)}


def git_is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    code, _ = git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    return code == 0


def git_head(repo: str) -> str | None:
    code, out = git(repo, "rev-parse", "HEAD")
    return out.strip() if code == 0 else None


def git_changed_paths(repo: str, base: str, head: str) -> list[str]:
    code, out = git(repo, "diff", "--name-status", "-M", base, head)
    return [line for line in out.splitlines() if line.strip()] if code == 0 else []


def git_diff_text(repo: str, base: str, head: str, max_lines: int = 160) -> str:
    code, out = git(repo, "diff", "-M", "--unified=2", base, head)
    if code != 0:
        return ""
    lines = out.splitlines()
    clipped = lines[:max_lines]
    if len(lines) > max_lines:
        clipped.append(f"... {len(lines) - max_lines} more lines")
    return safe_text("\n".join(clipped), 20000)


def git_blob_sha256(repo: str, rev: str, path: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "show", f"{rev}:{path}"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return hashlib.sha256(proc.stdout).hexdigest() if proc.returncode == 0 else None


def git_dirty_tracked(repo: str) -> list[str]:
    code, out = git(repo, "status", "--porcelain", "--untracked-files=no")
    return [line for line in out.splitlines() if line.strip()] if code == 0 else []


def git_head_moved_at(repo: str) -> datetime | None:
    """When the checkout's HEAD last moved (its reflog), not when the commit was written.

    A commit written at 08:58 and merged at 09:02 is on disk only from 09:02.
    """
    code, out = git(repo, "log", "-g", "-1", "--format=%gd", "--date=unix", "HEAD")
    match = re.search(r"@\{(\d+)\}", out) if code == 0 else None
    return datetime.fromtimestamp(int(match.group(1)), timezone.utc) if match else None


def installed_matches_commit(target: DeployTarget, sha: str) -> dict[str, Any]:
    """Compare every file of the package at ``sha`` (code and package data) with the install."""
    code, out = git(target.repo, "ls-tree", "-r", "--name-only", sha, target.source_prefix)
    if code != 0:
        return {"state": "UNAVAILABLE", "reason": "commit tree not readable"}
    same, differ, missing = 0, [], []
    newest = 0.0
    for rel in (line for line in out.splitlines() if line.strip()):
        installed = Path(target.install_dir) / rel[len(target.source_prefix) + 1 :]
        if not installed.exists():
            missing.append(rel)
            continue
        newest = max(newest, installed.stat().st_mtime)
        expected = git_blob_sha256(target.repo, sha, rel)
        actual = hashlib.sha256(installed.read_bytes()).hexdigest()
        if expected == actual:
            same += 1
        else:
            differ.append(rel)
    return {
        "state": "OBSERVED",
        "same": same,
        "differ": differ,
        "missing": missing,
        "install_dir": target.install_dir,
        "newest_install_mtime": (
            iso(datetime.fromtimestamp(newest, timezone.utc)) if newest else None
        ),
    }


# --------------------------------------------------------------------------- systemd


def unit_started(unit: str, user: bool) -> dict[str, Any]:
    argv = ["systemctl", *(["--user"] if user else []), "show", unit]
    argv += ["-p", "ActiveState", "-p", "ActiveEnterTimestamp", "-p", "MainPID"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"unit": unit, "state": "UNAVAILABLE", "reason": type(exc).__name__}
    props = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    started = None
    raw = props.get("ActiveEnterTimestamp", "")
    if raw:
        try:
            started = datetime.strptime(raw.rsplit(" ", 1)[0], "%a %Y-%m-%d %H:%M:%S").astimezone()
        except ValueError:
            started = None
    return {
        "unit": unit,
        "user": user,
        "state": props.get("ActiveState", "unknown"),
        "main_pid": props.get("MainPID"),
        "started_at": iso(started),
    }


# --------------------------------------------------------------------------- HTTP / DB


def http_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    try:
        response = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        return {"state": "DISCONNECTED", "error": type(exc).__name__, "url": url}
    try:
        body = response.json()
    except ValueError:
        body = None
    return {"state": "OBSERVED", "status": response.status_code, "body": body, "url": url}


def psql_rows(
    db: str, sql: str, *, host: str = "", user: str = "", timeout: float = 8.0
) -> dict[str, Any]:
    """One read-only query through psql (credentials from ~/.pgpass or the environment)."""
    script = (
        "SET default_transaction_read_only = on; SET statement_timeout = '5s'; "
        f"SET lock_timeout = '1s'; {sql}"
    )
    argv = ["psql", "-X", "-q", "-At", "-F", "\t"]
    argv += [*(["-h", host] if host else []), *(["-U", user] if user else []), "-d", db]
    try:
        proc = subprocess.run(
            [*argv, "-c", script], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"state": "UNAVAILABLE", "error": type(exc).__name__}
    if proc.returncode != 0:
        return {"state": "UNAVAILABLE", "error": safe_text(proc.stderr, 200)}
    rows = [line.split("\t") for line in proc.stdout.splitlines() if line and line != "SET"]
    return {"state": "OBSERVED", "rows": rows}


# --------------------------------------------------------------------------- transcripts


@dataclass
class ToolRun:
    """One harness-recorded tool execution."""

    at: str
    commands: list[str]
    exit_codes: list[int]
    summaries: list[str]
    source: str
    raw_ref: str
    ranges: list[str] = field(default_factory=list)
    cwd: str | None = None  # the working directory the call named, when it is literal
    git_output: bool = False  # the result contains git's own diff or log output
    git_fatal: bool = False  # the result contains a git "fatal:" error

    @property
    def test_commands(self) -> list[str]:
        return [c for c in self.commands if invokes_pytest(c)]

    @property
    def is_test(self) -> bool:
        return bool(self.test_commands)

    @property
    def executed(self) -> bool:
        """The call ran a test itself rather than only printing earlier output."""
        return self.is_test and not all(display_only(c) for c in self.commands)


def _jsonl(path: str) -> Iterator[dict]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _summaries(text: str) -> list[str]:
    found = []
    for match in _PYTEST_SUMMARY.finditer(text or ""):
        value = match.group(1).strip().rstrip(",")
        if value and value not in found:
            found.append(value)
    return found[-3:]


@dataclass
class CodexTranscript:
    path: str
    delegation_id: str
    started_at: str | None
    model: str | None
    cli_version: str | None
    cwd: str | None
    originator: str | None
    packet_task: str | None
    final_message: str
    runs: list[ToolRun]
    final_verdict: str | None = None


_PACKET_DELEGATION = re.compile(r"^delegation:\s*([0-9a-f-]{36})", re.M)
_PACKET_TASK = re.compile(r"^parent task:\s*(\S+)", re.M)


_TURN_STARTED = {"function_call", "custom_tool_call", "local_shell_call", "reasoning"}


def _codex_packet(path: str) -> tuple[str | None, str | None]:
    """The delegation id and parent task named in a rollout's INITIAL prompt turn.

    Only user messages before the model's first reasoning, tool call or reply
    count. A delegation named in a later message (a follow-up, a pasted record)
    is not the rollout's own packet.
    """
    for row in _jsonl(path):
        payload = row.get("payload") or {}
        if row.get("type") != "response_item":
            continue
        kind = payload.get("type")
        if kind in _TURN_STARTED or (kind == "message" and payload.get("role") == "assistant"):
            break
        if kind == "message" and payload.get("role") == "user":
            for part in payload.get("content") or []:
                text = part.get("text", "") if isinstance(part, dict) else ""
                match = _PACKET_DELEGATION.search(text)
                if match:
                    task = _PACKET_TASK.search(text)
                    return match.group(1), task.group(1) if task else None
    return None, None


_ATS_VERDICT_BLOCK = re.compile(r"```ats-verdict\s*(\{.*?\})\s*```", re.S)
_VERDICT_LINE = re.compile(r"^\W*VERDICT\s*[=:]\s*\**\s*([A-Z_]+)", re.M | re.I)
_LEADING_VERDICT = re.compile(r"^\W*(PASS_WITH_CITATION|PASS|BLOCK)\b")


def overall_verdict(message: str) -> str | None:
    """A reviewer's OVERALL verdict, or None when it states none.

    The verdict stated LAST wins, whether an ats-verdict block or a VERDICT
    line: findings, quoted examples and earlier rounds come before the final
    word. Only a message with neither falls back to a verdict it opens with.
    """
    text = message or ""
    stated: list[tuple[int, str]] = []
    for match in _ATS_VERDICT_BLOCK.finditer(text):
        try:
            code = json.loads(match.group(1)).get("verdict_code")
        except (ValueError, AttributeError):
            code = None
        if code:
            stated.append((match.start(), str(code).upper()))
    stated += [(m.start(), m.group(1).upper()) for m in _VERDICT_LINE.finditer(text)]
    if stated:
        return max(stated)[1]
    lead = _LEADING_VERDICT.match(text)
    return lead.group(1) if lead else None


_GIT_RANGE = re.compile(
    r"(?:^|[;&|(]\s*)git\s+(?:-C\s+(\S+)\s+)?(?:-c\s+\S+\s+)*(diff|log|rev-list)\b([^;&|]*)"
)


def git_range_command(command: str, base: str, head: str) -> tuple[str, bool] | None:
    """(``git -C`` directory or '', whether the shell's exit code is git's own) when
    ``command`` runs ``git diff``/``log``/``rev-list`` over base then head, in that
    order; None otherwise.

    A command that merely prints both ids (echo, printf, a JS string) inspects
    nothing, and head..base is a different range from base..head. After ``||``, a
    pipe or ``;`` the exit code belongs to another command.
    """
    for match in _GIT_RANGE.finditer(command or ""):
        ids = [t for t in re.split(r"\s+|\.{2,3}", match.group(3)) if _SHA.fullmatch(t)]
        at_base = next((i for i, t in enumerate(ids) if base.startswith(t)), None)
        at_head = next((i for i, t in enumerate(ids) if head.startswith(t)), None)
        if at_base is not None and at_head is not None and at_base < at_head:
            rest = command[match.end() :]
            return match.group(1) or "", not re.match(r"\s*(?:\|\|?|;)", rest)
    return None


def same_repository(path: str, repo: str) -> bool | None:
    """Whether ``path`` is ``repo`` or one of its worktrees; None when it no longer exists."""
    if not path or not os.path.isdir(path):
        return None
    ours = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    theirs = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if ours[0] != 0 or theirs[0] != 0:
        return False
    return os.path.realpath(ours[1].strip()) == os.path.realpath(theirs[1].strip())


def find_codex_transcript(
    home: str, delegation_id: str, created: datetime | None, closed: datetime | None
) -> list[str]:
    """Rollouts whose OWN first packet names this delegation.

    A plain text search is not a binding: a later reviewer that merely reads
    an earlier session id would match. Dates bracket the delegation window
    (rollout directories are local dates).
    """
    if created is None:
        return []
    start = (created - timedelta(days=1)).astimezone()
    # An open delegation may still be running: search up to today.
    end = (closed + timedelta(days=1)).astimezone() if closed else datetime.now().astimezone()
    day = start.date()
    paths: list[str] = []
    while day <= end.date():
        paths += glob.glob(
            os.path.join(home, ".codex/sessions", f"{day:%Y/%m/%d}", "rollout-*.jsonl")
        )
        day += timedelta(days=1)
    return sorted(p for p in paths if _codex_packet(p)[0] == delegation_id)


def read_codex_transcript(path: str) -> CodexTranscript:
    meta: dict = {}
    model = None
    final = ""
    phased = False
    calls: dict[str, dict] = {}
    order: list[str] = []
    for row in _jsonl(path):
        kind = row.get("type")
        payload = row.get("payload") or {}
        if kind == "session_meta":
            meta = payload
        elif kind == "turn_context" and not model:
            model = payload.get("model")
        elif kind == "response_item":
            ptype = payload.get("type")
            if ptype in ("custom_tool_call", "function_call"):
                call_id = payload.get("call_id") or str(len(order))
                text = payload.get("input") or payload.get("arguments") or ""
                calls[call_id] = {"at": row.get("timestamp"), "input": str(text), "output": ""}
                order.append(call_id)
            elif ptype in ("custom_tool_call_output", "function_call_output"):
                call = calls.get(payload.get("call_id") or "")
                if call is not None:
                    out = payload.get("output")
                    if isinstance(out, list):
                        out = " ".join(p.get("text", "") for p in out if isinstance(p, dict))
                    call["output"] = str(out or "")
            elif ptype == "message" and payload.get("role") == "assistant":
                text = " ".join(
                    p.get("text", "") for p in payload.get("content") or [] if isinstance(p, dict)
                )
                # Codex tags each reply commentary (mid-run) or final_answer. Once
                # replies carry a phase, only the final_answer is the final message:
                # a reviewer that is still running has said nothing final.
                if "phase" in payload:
                    if not phased:
                        phased, final = True, ""
                    if payload.get("phase") == "final_answer" and text.strip():
                        final = text
                elif text.strip() and not phased:
                    final = text
    delegation, task = _codex_packet(path)
    runs = []
    for call_id in order:
        call = calls[call_id]
        commands = _codex_commands(call["input"])
        if not commands:
            continue
        codes = [int(c) for c in re.findall(r'exit_code\\?"?\s*[:=]\s*(-?\d+)', call["output"])]
        codes += [int(c) for c in re.findall(r"Exit code:?\s*(-?\d+)", call["output"])]
        runs.append(
            ToolRun(
                at=str(call["at"] or ""),
                commands=commands,
                exit_codes=codes,
                summaries=_summaries(call["output"]),
                source="codex transcript",
                raw_ref=f"{path}#{call_id}",
                ranges=_shas(call["input"]),
                cwd=_codex_workdir(call["input"]),
                git_output=bool(_GIT_OUTPUT.search(call["output"])),
                git_fatal="fatal: " in call["output"],
            )
        )
    return CodexTranscript(
        path=path,
        delegation_id=delegation or "",
        started_at=meta.get("timestamp"),
        model=model,
        cli_version=meta.get("cli_version"),
        cwd=meta.get("cwd"),
        originator=meta.get("originator"),
        packet_task=task,
        final_message=_display_message(final),
        runs=runs,
        final_verdict=overall_verdict(final),
    )


_DISPLAY_KEEP = 8000


def _display_message(text: str) -> str:
    """The reviewer's last message for display: whole when it fits, else its opening
    (findings) and its close (verdict) with the cut stated where it happened."""
    text = safe_text(text, len(text or "") + 1)
    if len(text) <= 2 * _DISPLAY_KEEP:
        return text
    omitted = len(text) - 2 * _DISPLAY_KEEP
    return (
        text[:_DISPLAY_KEEP]
        + f"\n\n[... {omitted} characters omitted here; the full message is in the transcript ...]\n\n"
        + text[-_DISPLAY_KEEP:]
    )


_GIT_OUTPUT = re.compile(r"diff --git a/|@@ -\d+(?:,\d+)? \+\d+|\bcommit [0-9a-f]{40}\b")
_WORKDIR = re.compile(r'\b(?:workdir|cwd)\s*:\s*(?:"((?:[^"\\]|\\.)*)"|`([^`]*)`|(\w+))')


def _codex_workdir(text: str) -> str | None:
    """The literal working directory a Codex tool call named, resolving one constant."""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("workdir"), str):
        return str(parsed["workdir"])
    match = _WORKDIR.search(text or "")
    if not match:
        return None
    if match.group(3):
        const = re.search(
            rf'\b(?:const|let|var)\s+{re.escape(match.group(3))}\s*=\s*["`]([^"`]*)["`]', text
        )
        return const.group(1) if const else None
    return match.group(1) if match.group(1) is not None else match.group(2)


_SHA_TOKEN = re.compile(r"\b[0-9a-f]{7,40}\b")


def _shas(text: str) -> list[str]:
    return sorted({token for token in _SHA_TOKEN.findall(text) if re.search(r"[a-f]", token)})


def _codex_commands(text: str) -> list[str]:
    """Shell commands inside one Codex tool call (JS exec wrapper or JSON args)."""
    commands: list[str] = []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        value = parsed.get("cmd") or parsed.get("command")
        if isinstance(value, list):
            value = " ".join(str(v) for v in value)
        if value:
            commands.append(str(value))
    for match in re.finditer(r'(?:cmd|command)\s*:\s*(`(?:[^`\\]|\\.)*`|"(?:[^"\\]|\\.)*")', text):
        commands.append(match.group(1)[1:-1])
    for match in re.finditer(r'\[\s*"[\w-]+"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]', text):
        commands.append(match.group(1))
    cleaned = []
    for command in commands:
        command = " ".join(command.replace("\\n", " ").split())
        if command and command not in cleaned:
            cleaned.append(safe_text(command, 400))
    return cleaned


def claude_transcripts(home: str, agent: str) -> tuple[list[str], list[str]]:
    """(main transcripts, subagent transcripts) for an agent label claude-code:<cid8>."""
    match = re.fullmatch(r"claude-code:([0-9a-f]{8})", agent or "")
    if not match:
        return [], []
    prefix = match.group(1)
    main = sorted(glob.glob(os.path.join(home, ".claude/projects/*", f"{prefix}*.jsonl")))
    subs: list[str] = []
    for path in main:
        subs += sorted(glob.glob(os.path.join(path[: -len(".jsonl")], "subagents", "*.jsonl")))
    return main, subs


def _in_windows(at: datetime | None, windows: list[tuple[datetime, datetime]]) -> bool:
    return at is not None and any(start <= at <= end for start, end in windows)


def claude_test_runs(path: str, windows: list[tuple[datetime, datetime]]) -> list[ToolRun]:
    """Bash test executions recorded by the Claude Code harness inside the windows."""
    pending: dict[str, tuple[str, str]] = {}
    runs: list[ToolRun] = []
    for row in _jsonl(path):
        content = (row.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                command = str((block.get("input") or {}).get("command") or "")
                when = parse_time(row.get("timestamp"))
                if _in_windows(when, windows) and (
                    invokes_pytest(command) or _TEST_COMMAND.search(command)
                ):
                    pending[block.get("id", "")] = (str(row.get("timestamp")), command)
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                at, command = pending.pop(block["tool_use_id"])
                body = block.get("content")
                text = (
                    body
                    if isinstance(body, str)
                    else " ".join(p.get("text", "") for p in body or [] if isinstance(p, dict))
                )
                code_match = re.match(r"\s*Exit code (\d+)", text or "")
                code = (
                    int(code_match.group(1)) if code_match else (1 if block.get("is_error") else 0)
                )
                runs.append(
                    ToolRun(
                        at=at,
                        commands=[safe_text(" ".join(command.split()), 400)],
                        exit_codes=[code],
                        summaries=_summaries(text),
                        source="claude transcript",
                        raw_ref=f"{path}#{block['tool_use_id']}",
                    )
                )
    return runs


def operator_turns(path: str, start: datetime, end: datetime, needle: str) -> list[dict]:
    """Typed operator turns in a MAIN transcript containing ``needle``.

    Subagent transcripts are excluded: their "user" turns are prompts written
    by the parent agent. Harness-injected turns (meta, command wrappers,
    notifications) are excluded too.
    """
    wanted = " ".join(needle.lower().split())
    found = []
    for row in _jsonl(path):
        if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
            continue
        content = (row.get("message") or {}).get("content")
        if isinstance(content, list):
            if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
                continue
            content = " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue
        text = content.strip()
        if text.startswith("<") and not text.startswith("<pasted_content"):
            continue
        when = parse_time(row.get("timestamp"))
        if when is None or not (start <= when <= end):
            continue
        flat = " ".join(text.lower().split())
        index = flat.find(wanted)
        if index >= 0:
            excerpt = " ".join(text.split())[max(0, index - 80) : index + len(wanted) + 160]
            found.append(
                {
                    "at": iso(when),
                    "excerpt": safe_text(excerpt, 400),
                    "ref": f"{path}#{row.get('uuid')}",
                }
            )
    return found
