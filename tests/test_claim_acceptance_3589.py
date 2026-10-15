"""#3589 acceptance suite: the complete checker against adversarial evidence.

Each case builds a small, real world in a temporary directory -- a git
repository, Codex rollouts and Claude transcripts in their harness formats,
ATS rows, and the answers systemd, HTTP probes and the migration ledger give
-- then runs the production claim builders and ``decide()`` unchanged and
compares the verdict with what that evidence actually establishes.

The gate is ZERO FALSE VERIFIED: no claim may be decided VERIFIED when the
evidence does not establish it. Every other mismatch is reported too.

Run as tests, or print the full input/expected/actual table:
    python tests/test_claim_acceptance_3589.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import pytest

from ai_team_sync.console import evidence as ev
from ai_team_sync.console.evidence import DeployTarget, EvidenceConfig, Ledger
from ai_team_sync.console.verify import (
    CONTRADICTED,
    STALE,
    UNVERIFIED,
    VERIFIED,
    Claim,
    TicketVerifier,
    decide,
)

T0 = datetime(2026, 10, 15, 0, 40, tzinfo=timezone.utc)
DELEG = "11111111-2222-4333-8444-555555555555"
OTHER = "66666666-7777-4888-9999-000000000000"
CID = "c0ffee00"  # the ticket's Claude session (agent claude-code:c0ffee00)
STRANGER = "a1b2c3d4"  # a Claude session with no part in this ticket


@dataclass
class Case:
    id: str
    claim: str
    category: str
    inputs: str
    expected: str
    build: Callable[["World"], Claim]


CASES: list[Case] = []


def case(cid: str, claim: str, category: str, expected: str, inputs: str):
    def register(fn: Callable[["World"], Claim]) -> Callable[["World"], Claim]:
        CASES.append(Case(cid, claim, category, inputs, expected, fn))
        return fn

    return register


# ----------------------------------------------------------------------------- world


class World:
    """One case's evidence: real files on disk plus canned service answers."""

    def __init__(self, tmp: Path, stack: ExitStack):
        self.tmp, self.stack = tmp, stack
        self.repo = tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.delegations: list[dict] = []
        self.sessions: list[dict] = []
        self.handoffs: list[dict] = []
        self.verified_by: dict[str, Any] = {}
        self.task: dict[str, Any] = {"created_at": (T0 - timedelta(hours=1)).isoformat()}
        self.targets: list[DeployTarget] = []
        self.http: dict[str, dict] = {}
        self.units: dict[str, dict] = {}
        self.ledger: dict[str, Any] = {"state": "OBSERVED", "rows": []}
        self.claude_runs: list[ev.ToolRun] | None = None
        self.outbox: list[dict] = []  # ATS completion_outbox: event_id -> the handoff it carried
        stack.enter_context(mock.patch.object(ev, "http_json", self._http))
        stack.enter_context(mock.patch.object(ev, "unit_started", self._unit))
        stack.enter_context(mock.patch.object(ev, "psql_rows", lambda *args, **kwargs: self.ledger))

    # ---- services
    def _http(self, url: str, timeout: float = 5.0) -> dict:
        for key, answer in self.http.items():
            if key in url:
                return answer
        return {"state": "DISCONNECTED", "error": "ConnectError", "url": url}

    def _unit(self, unit: str, user: bool) -> dict:
        return self.units.get(unit, {"unit": unit, "state": "UNAVAILABLE"})

    def unit(self, name: str, *, state="active", pid="4242", started: datetime | None = None):
        self.units[name] = {
            "unit": name,
            "state": state,
            "main_pid": pid,
            "started_at": (
                started or datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
        }

    # ---- git
    def git(self, *args: str, env: dict | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
        ).stdout.strip()

    def commit(self, name: str, body: str, env: dict | None = None) -> str:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        self.git("add", name)
        self.git("commit", "-q", "-m", f"change {name}", env=env)
        return self.git("rev-parse", "HEAD")

    # ---- transcripts
    def rollout(
        self,
        name: str,
        packet: str,
        calls: list[tuple[str, str]],
        final: str | None = None,
        *,
        phases: list[tuple[str, str]] | None = None,
        started: datetime = T0 + timedelta(seconds=5),
        originator: str = "codex_exec",
        day: datetime = T0,
    ) -> Path:
        rows: list[dict] = [
            {
                "type": "session_meta",
                "timestamp": started.isoformat(),
                "payload": {
                    "timestamp": started.isoformat(),
                    "cli_version": "0.160.1",
                    "cwd": str(self.repo),
                    "originator": originator,
                },
            },
            {"type": "turn_context", "payload": {"model": "model-x"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": packet}],
                },
            },
        ]
        for i, (call, output) in enumerate(calls):
            rows += [
                {
                    "type": "response_item",
                    "timestamp": (started + timedelta(seconds=10 + i)).isoformat(),
                    "payload": {"type": "custom_tool_call", "call_id": f"c{i}", "input": call},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": f"c{i}",
                        "output": [{"type": "input_text", "text": output}],
                    },
                },
            ]
        messages = phases if phases is not None else ([(None, final)] if final is not None else [])
        for phase, text in messages:
            payload: dict[str, Any] = {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
            if phase:
                payload["phase"] = phase
            rows.append({"type": "response_item", "payload": payload})
        path = (
            self.tmp / ".codex/sessions" / f"{day.astimezone():%Y/%m/%d}" / f"rollout-{name}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return path

    def claude(self, cid: str, rows: list[dict], *, sub: bool = False) -> Path:
        base = self.tmp / ".claude/projects/p"
        path = base / f"{cid}-0000-0000-0000-000000000000.jsonl"
        if sub:
            path = base / f"{cid}-0000-0000-0000-000000000000" / "subagents" / "agent-x.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return path

    def session(
        self, cid: str = CID, *, start=T0 - timedelta(minutes=30), end=T0 + timedelta(hours=2)
    ):
        self.sessions.append(
            {
                "id": f"s-{cid}",
                "agent": f"claude-code:{cid}",
                "ticket_id": 1001,
                "started_at": start.isoformat(),
                "completed_at": end.isoformat(),
            }
        )

    # ---- checker
    def verifier(self) -> TicketVerifier:
        v = object.__new__(TicketVerifier)
        v.ticket_id = 1001
        v.config = EvidenceConfig(
            ats_db=str(self.tmp / "none.db"),
            receipt_url="http://intake/receipts/{session_id}",
            ledgers={"app": Ledger(db="app_db")},
            home=str(self.tmp),
            targets=tuple(self.targets),
        )
        v.ats = {
            "sessions": self.sessions,
            "delegations": self.delegations,
            "handoffs": self.handoffs,
            "decisions": [],
            "commits": [],
            "file_activity": [],
            "restarts": [],
            "completion_events": self.outbox,
        }
        v.sessions = {row["id"]: row for row in self.sessions}
        v.task_state = "OBSERVED"
        v.task = self.task
        v.verified_by = self.verified_by
        v.codex = {}
        v._claude_runs = self.claude_runs
        return v


def deleg(**over: Any) -> dict:
    row = {
        "id": DELEG,
        "parent_session_id": "p",
        "child_session_id": None,
        "delegated_worker": "codex",
        "delegating_worker": "claude-code",
        "mode": "VERIFY",
        "state": "closed",
        "resolved_binary": "/usr/bin/codex",
        "objective": "review the change",
        "result_summary": "PASS",
        "evidence": "{}",
        "created_at": T0.isoformat(),
        "closed_at": (T0 + timedelta(minutes=30)).isoformat(),
        "verdict_code": None,
        "structured_verdict": None,
    }
    row.update(over)
    return row


def run(test: str, summary: str, code: int = 0, at: datetime = T0) -> ev.ToolRun:
    return ev.ToolRun(
        at=at.isoformat(),
        commands=[test],
        exit_codes=[code],
        summaries=[summary] if summary else [],
        source="claude transcript",
        raw_ref="t#1",
    )


def one(claims: list[Claim], prefix: str) -> Claim:
    return next(c for c in claims if c.id.startswith(prefix))


# ============================================================================= reviews

PASS_FINAL = [("commentary", "Reading the diff."), ("final_answer", "No findings.\nVERDICT: PASS")]
DIFF = "Output:\ndiff --git a/a.py b/a.py\n@@ -1 +1 @@\n-1\n+2"


def review_world(
    w: World,
    calls: Callable[[str, str], list[tuple[str, str]]] | None = None,
    *,
    phases: list[tuple[str, str]] | None = PASS_FINAL,
    final: str | None = None,
    record: str = "PASS",
    packet_id: str = DELEG,
    claimed_head: str | None = None,
    rollout: bool = True,
) -> Claim:
    base = w.commit("a.py", "1")
    head = w.commit("a.py", "2")
    if rollout:
        made = (
            calls
            or (lambda b, h: [(f'await tools.exec_command({{cmd: "git diff {b} {h}"}})', DIFF)])
        )(base, head)
        w.rollout(
            "r",
            f"delegation: {packet_id}\nparent task: 1001",
            made,
            final,
            phases=phases if final is None else None,
        )
    w.delegations.append(
        deleg(
            verdict_code=record,
            structured_verdict=json.dumps(
                {
                    "verdict_code": record,
                    "reviewed": {
                        "repo": str(w.repo),
                        "base_sha": base,
                        "head_sha": claimed_head or head,
                    },
                }
            ),
        )
    )
    return one(w.verifier().review_claims(), "review:")


def exec_call(cmd: str) -> str:
    return f'await tools.exec_command({{cmd: "{cmd}"}})'


@case(
    "RV01",
    "review",
    "genuine",
    VERIFIED,
    "Bound transcript diffs exactly base..head (git diff output recorded); final_answer says VERDICT: PASS; record PASS",
)
def _(w):
    return review_world(w)


@case(
    "RV02",
    "review",
    "genuine",
    VERIFIED,
    "Same, legacy transcript without phase tags; final message VERDICT: PASS",
)
def _(w):
    return review_world(w, phases=None, final="No findings.\nVERDICT: PASS")


@case(
    "RV03",
    "review",
    "genuine",
    VERIFIED,
    "Diff piped to head; exit is the shell's but git's own diff output is recorded",
)
def _(w):
    return review_world(
        w, lambda b, h: [(exec_call(f"git diff {b} {h} | head -80"), '{"exit_code":0} ' + DIFF)]
    )


@case(
    "RV04",
    "review",
    "self-report only",
    UNVERIFIED,
    "Structured verdict PASS recorded by ats; no reviewer transcript exists",
)
def _(w):
    return review_world(w, rollout=False)


@case(
    "RV05",
    "review",
    "missing evidence",
    UNVERIFIED,
    "Bound transcript says PASS but only ran git status: never inspected the range",
)
def _(w):
    return review_world(w, lambda b, h: [(exec_call("git status"), '{"exit_code":0}')])


@case(
    "RV06",
    "review",
    "conflicting",
    CONTRADICTED,
    "Record says PASS; the reviewer's own final_answer says VERDICT: BLOCK",
)
def _(w):
    return review_world(w, phases=[("final_answer", "Finding 1.\nVERDICT: BLOCK")])


@case(
    "RV07",
    "review",
    "conflicting",
    CONTRADICTED,
    "Final answer quotes an example PASS ats-verdict block, then states VERDICT: BLOCK; record PASS",
)
def _(w):
    text = 'Example:\n```ats-verdict\n{"verdict_code":"PASS"}\n```\nVERDICT: BLOCK'
    return review_world(w, phases=[("final_answer", text)])


@case(
    "RV08",
    "review",
    "unfinished reviewer",
    UNVERIFIED,
    "Reviewer still running: diff ran, only commentary so far, one commentary line says VERDICT: PASS",
)
def _(w):
    return review_world(
        w, phases=[("commentary", "Diff looks fine."), ("commentary", "VERDICT: PASS likely")]
    )


@case(
    "RV09",
    "review",
    "unfinished reviewer",
    UNVERIFIED,
    "Reviewer still running: diff ran, no assistant message at all",
)
def _(w):
    return review_world(w, phases=[])


@case(
    "RV10",
    "review",
    "invalid/reversed range",
    UNVERIFIED,
    "git log --oneline HEAD..BASE (reversed), exit 0, empty output",
)
def _(w):
    return review_world(
        w, lambda b, h: [(exec_call(f"git log --oneline {h}..{b}"), '{"exit_code":0}')]
    )


@case(
    "RV11", "review", "invalid/reversed range", UNVERIFIED, "git diff HEAD BASE (reversed), exit 0"
)
def _(w):
    return review_world(w, lambda b, h: [(exec_call(f"git diff {h} {b}"), '{"exit_code":0}')])


@case(
    "RV12",
    "review",
    "invalid/reversed range",
    UNVERIFIED,
    "git diff BASE <other commit>: the claimed head was never named",
)
def _(w):
    other = "c" * 40
    return review_world(
        w, lambda b, h: [(exec_call(f"git diff {b} {other}"), '{"exit_code":0} ' + DIFF)]
    )


@case(
    "RV13",
    "review",
    "invalid/reversed range",
    UNVERIFIED,
    "printf BASE HEAD: names both ids, inspects nothing",
)
def _(w):
    return review_world(w, lambda b, h: [(exec_call(f"printf {b} {h}"), '{"exit_code":0}')])


@case(
    "RV14",
    "review",
    "invalid/reversed range",
    UNVERIFIED,
    "git show BASE HEAD: shows two commits, not the range between them",
)
def _(w):
    return review_world(
        w, lambda b, h: [(exec_call(f"git show {b} {h}"), '{"exit_code":0} commit ' + h)]
    )


@case(
    "RV15",
    "review",
    "invalid/reversed range",
    CONTRADICTED,
    "Record claims a head commit that does not exist in the repository",
)
def _(w):
    return review_world(w, claimed_head="d" * 40)


@case(
    "RV16",
    "review",
    "masked failure",
    UNVERIFIED,
    "git -C /nope diff BASE HEAD || true: shell exit 0, git printed fatal",
)
def _(w):
    return review_world(
        w,
        lambda b, h: [
            (
                exec_call(f"git -C /nope diff {b} {h} || true"),
                "{\"exit_code\":0} fatal: cannot change to '/nope'",
            )
        ],
    )


@case(
    "RV17",
    "review",
    "masked failure",
    UNVERIFIED,
    "git diff BASE HEAD | head: shell exit 0 from head, no git output",
)
def _(w):
    return review_world(
        w, lambda b, h: [(exec_call(f"git diff {b} {h} | head"), '{"exit_code":0}')]
    )


@case("RV18", "review", "masked failure", UNVERIFIED, "git diff BASE HEAD exit 128")
def _(w):
    return review_world(w, lambda b, h: [(exec_call(f"git diff {b} {h}"), '{"exit_code":128}')])


@case(
    "RV19",
    "review",
    "masked failure",
    UNVERIFIED,
    "One call runs the claimed diff (fatal) and another diff (output): pooled output",
)
def _(w):
    call = (
        'await Promise.all([tools.exec_command({cmd: "git diff {b} {h}"}), '
        'tools.exec_command({cmd: "git diff HEAD~1 HEAD"})])'
    )
    return review_world(
        w, lambda b, h: [(call.replace("{b}", b).replace("{h}", h), "fatal: bad object\n" + DIFF)]
    )


@case(
    "RV20",
    "review",
    "wrong session",
    UNVERIFIED,
    "Transcript diffs the range and says PASS, but its packet names a DIFFERENT delegation",
)
def _(w):
    return review_world(w, packet_id=OTHER)


@case(
    "RV21",
    "review",
    "wrong session",
    UNVERIFIED,
    "Diff ran in a different existing repository (workdir)",
)
def _(w):
    other = w.tmp / "other"
    other.mkdir()
    subprocess.run(["git", "-C", str(other), "init", "-q"], check=True)
    call = f'const wd="{other}";\nawait tools.exec_command({{cmd: "git diff {{b}} {{h}}", workdir: wd}})'
    return review_world(
        w, lambda b, h: [(call.replace("{b}", b).replace("{h}", h), '{"exit_code":0}')]
    )


@case(
    "RV22",
    "review",
    "wrong session",
    UNVERIFIED,
    "Rollout's opening prompt names no delegation; the id appears only in a later message",
)
def _(w):
    claim_world = review_world(w, packet_id=OTHER)
    path = next((w.tmp / ".codex/sessions").rglob("rollout-r.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[2]["payload"]["content"][0]["text"] = "Please review the repository."
    rows.insert(
        -1,
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"delegation: {DELEG}"}],
            },
        },
    )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    del claim_world
    return one(w.verifier().review_claims(), "review:")


# ============================================================================= review coverage


def coverage_world(
    w: World, *, extra_commit: bool = False, verdict_phases=PASS_FINAL, record="PASS", rollout=True
):
    review_world(w, phases=verdict_phases, record=record, rollout=rollout)
    head = w.git("rev-parse", "HEAD")
    deployed = w.commit("m.sql", "rename") if extra_commit else head
    w.verified_by = {"deployed": {"svc": {"sha": deployed, "repo": str(w.repo)}}}
    return one(w.verifier().coverage_claims(), "coverage:")


@case(
    "CV01",
    "review coverage",
    "genuine",
    VERIFIED,
    "Deployed commit == head of a VERIFIED passing review",
)
def _(w):
    return coverage_world(w)


@case(
    "CV02",
    "review coverage",
    "self-report only",
    UNVERIFIED,
    "Deployed == reviewed head, but the review itself has no transcript",
)
def _(w):
    return coverage_world(w, rollout=False)


@case(
    "CV03",
    "review coverage",
    "stale",
    STALE,
    "Deployed commit adds a change after the VERIFIED passing review's head",
)
def _(w):
    return coverage_world(w, extra_commit=True)


@case(
    "CV04",
    "review coverage",
    "conflicting",
    CONTRADICTED,
    "The review of exactly the deployed commit is BLOCK (verified transcript)",
)
def _(w):
    return coverage_world(w, verdict_phases=[("final_answer", "VERDICT: BLOCK")], record="BLOCK")


@case(
    "CV05",
    "review coverage",
    "unfinished reviewer",
    UNVERIFIED,
    "Passing record at the deployed head, but the reviewer is still running (commentary only)",
)
def _(w):
    return coverage_world(w, verdict_phases=[("commentary", "VERDICT: PASS soon")])


@case(
    "CV06",
    "review coverage",
    "missing evidence",
    UNVERIFIED,
    "Deployment claimed, no structured review of that repository recorded",
)
def _(w):
    sha = w.commit("a.py", "1")
    w.verified_by = {"deployed": {"svc": {"sha": sha, "repo": str(w.repo)}}}
    return one(w.verifier().coverage_claims(), "coverage:")


# ============================================================================= deployments (installed package)


def install_world(
    w: World,
    *,
    unit_state="active",
    pid="4242",
    started_delta=timedelta(minutes=5),
    probe: dict | None = None,
    probe_status=200,
    installed_html="<p>v1</p>\n",
    drop_html=False,
    probe_url="http://127.0.0.1:1/api/version",
    target=True,
    later_commit=False,
) -> Claim:
    sha = w.commit("src/pkg/a.py", "print(1)\n")
    w.commit("src/pkg/page.html", "<p>v1</p>\n")
    sha = w.git("rev-parse", "HEAD")
    later = w.commit("src/pkg/a.py", "print(2)\n") if later_commit else None
    install = w.tmp / "site/pkg"
    install.mkdir(parents=True)
    (install / "a.py").write_text("print(2)\n" if later_commit else "print(1)\n")
    if not drop_html:
        (install / "page.html").write_text(installed_html)
    w.unit("svc", state=unit_state, pid=pid, started=datetime.now(timezone.utc) + started_delta)
    body = (
        probe
        if probe is not None
        else {"commit": sha[:7], "pid": int(pid or 0), "package_path": str(install)}
    )
    w.http["/api/version"] = {"state": "OBSERVED", "status": probe_status, "body": body}
    if target:
        w.targets.append(
            DeployTarget(
                repo=str(w.repo),
                units=(("svc", True),),
                install_dir=str(install),
                source_prefix="src/pkg",
                probe_url=probe_url,
                probe_reports_commit=bool(probe_url),
            )
        )
    w.verified_by = {"deployed": {"svc": {"sha": sha, "repo": str(w.repo)}}}
    del later
    return one(w.verifier().deployment_claims(), "deploy:")


@case(
    "DP01",
    "deployment",
    "genuine",
    VERIFIED,
    "All package files match; unit active pid>0 started after install; probe reports this build and the install path",
)
def _(w):
    return install_world(w)


@case(
    "DP02",
    "deployment",
    "self-report only",
    UNVERIFIED,
    "verified_by says deployed; no install or service known for the repo",
)
def _(w):
    return install_world(w, target=False)


@case(
    "DP03",
    "deployment",
    "missing evidence",
    UNVERIFIED,
    "Files and unit fine; no runtime probe configured",
)
def _(w):
    return install_world(w, probe_url="")


@case(
    "DP04",
    "deployment",
    "installed differs from running",
    CONTRADICTED,
    "Installed page.html differs from the commit (non-Python package data)",
)
def _(w):
    return install_world(w, installed_html="<p>old</p>\n")


@case(
    "DP05",
    "deployment",
    "installed differs from running",
    CONTRADICTED,
    "page.html missing from the install",
)
def _(w):
    return install_world(w, drop_html=True)


@case(
    "DP06",
    "deployment",
    "installed differs from running",
    CONTRADICTED,
    "Files match; the running service reports a different, unrelated build",
)
def _(w):
    return install_world(w, probe={"commit": "0000000", "pid": 4242})


@case(
    "DP07",
    "deployment",
    "installed differs from running",
    CONTRADICTED,
    "Running service imports from another directory than the install compared",
)
def _(w):
    return install_world(w, probe={"commit": "x", "pid": 4242, "package_path": "/elsewhere"})


@case(
    "DP08",
    "deployment",
    "missing evidence",
    UNVERIFIED,
    "Probe reports the right build but no package_path",
)
def _(w):
    install_world(w, probe={})
    sha = w.verified_by["deployed"]["svc"]["sha"]
    w.http["/api/version"] = {
        "state": "OBSERVED",
        "status": 200,
        "body": {"commit": sha[:7], "pid": 4242},
    }
    return one(w.verifier().deployment_claims(), "deploy:")


@case("DP09", "deployment", "conflicting", CONTRADICTED, "Unit inactive, pid 0")
def _(w):
    return install_world(w, unit_state="inactive", pid="0")


@case(
    "DP10",
    "deployment",
    "stale",
    STALE,
    "Unit started BEFORE the installed files were written: process runs older code",
)
def _(w):
    return install_world(w, started_delta=timedelta(hours=-1))


@case(
    "DP11", "deployment", "stale", STALE, "Install now matches a LATER commit than the one claimed"
)
def _(w):
    return install_world(w, later_commit=True)


@case("DP12", "deployment", "conflicting", CONTRADICTED, "Running service probe answers 500")
def _(w):
    return install_world(w, probe_status=500)


# ============================================================================= deployments (live checkout)


def checkout_world(w: World, *, move=None, dirty=False, health=200, started=None) -> Claim:
    sha = w.commit("a.py", "1")
    if move == "later":
        w.commit("b.py", "later")
    elif move == "diverged":
        w.git("checkout", "-q", "--orphan", "other")
        w.commit("z.py", "unrelated")
    if dirty:
        (w.repo / "a.py").write_text("edited in place")
    w.unit("svc", started=started or datetime.now(timezone.utc) + timedelta(minutes=5))
    w.http["/health"] = {"state": "OBSERVED", "status": health, "body": {"status": "healthy"}}
    w.targets.append(
        DeployTarget(
            repo=str(w.repo), units=(("svc", False),), probe_url="http://127.0.0.1:1/health"
        )
    )
    w.verified_by = {"deployed": {"svc": {"sha": sha, "repo": str(w.repo)}}}
    return one(w.verifier().deployment_claims(), "deploy:")


@case(
    "DK01",
    "deployment",
    "genuine",
    VERIFIED,
    "Checkout HEAD == commit, clean; unit active, started after HEAD moved; health answers",
)
def _(w):
    return checkout_world(w)


@case(
    "DK02",
    "deployment",
    "stale",
    STALE,
    "Checkout has moved past the claimed commit to a later one",
)
def _(w):
    return checkout_world(w, move="later")


@case(
    "DK03",
    "deployment",
    "stale",
    STALE,
    "Commit written 2h ago, merged now; unit started 1h ago (after the commit date, before the merge)",
)
def _(w):
    w.commit("a.py", "1")
    w.git("checkout", "-q", "-b", "feat")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    sha = w.commit("a.py", "2", env={"GIT_COMMITTER_DATE": old, "GIT_AUTHOR_DATE": old})
    w.git("checkout", "-q", "-")
    w.git("merge", "-q", "--ff-only", "feat")
    w.unit("svc", started=datetime.now(timezone.utc) - timedelta(hours=1))
    w.http["/health"] = {"state": "OBSERVED", "status": 200, "body": {}}
    w.targets.append(
        DeployTarget(
            repo=str(w.repo), units=(("svc", False),), probe_url="http://127.0.0.1:1/health"
        )
    )
    w.verified_by = {"deployed": {"svc": {"sha": sha, "repo": str(w.repo)}}}
    return one(w.verifier().deployment_claims(), "deploy:")


@case(
    "DK04",
    "deployment",
    "conflicting",
    CONTRADICTED,
    "Checkout HEAD is on an unrelated history that does not contain the commit",
)
def _(w):
    return checkout_world(w, move="diverged")


@case(
    "DK05",
    "deployment",
    "installed differs from running",
    UNVERIFIED,
    "HEAD == commit but a tracked file is modified in place",
)
def _(w):
    return checkout_world(w, dirty=True)


@case("DK06", "deployment", "conflicting", CONTRADICTED, "Health endpoint does not answer 200")
def _(w):
    return checkout_world(w, health=503)


# ============================================================================= migrations


def migration_world(w: World, *, prefix_ok=True, ledger="row", name="app") -> Claim:
    w.commit("migrations/094_x.sql", "CREATE TABLE x();\n")
    sha = w.git("rev-parse", "HEAD")
    import hashlib

    digest = hashlib.sha256(b"CREATE TABLE x();\n").hexdigest()
    w.ledger = (
        {"state": "OBSERVED", "rows": [["094", "2026-10-15 01:00"]]}
        if ledger == "row"
        else (
            {"state": "OBSERVED", "rows": []}
            if ledger == "none"
            else {"state": "UNAVAILABLE", "error": "connection refused"}
        )
    )
    w.verified_by = {
        "deployed": {
            name: {
                "sha": sha,
                "repo": str(w.repo),
                "migration": {
                    "id": "094",
                    "file": "migrations/094_x.sql",
                    "source_sha256_prefix": digest[:12] if prefix_ok else "deadbeef0000",
                },
            }
        }
    }
    return one(w.verifier().deployment_claims(), "migration:")


@case(
    "MG01",
    "migration",
    "genuine",
    VERIFIED,
    "File at the commit hashes as claimed; schema_migrations has the row",
)
def _(w):
    return migration_world(w)


@case(
    "MG02",
    "migration",
    "conflicting",
    CONTRADICTED,
    "File hashes as claimed; schema_migrations has NO row",
)
def _(w):
    return migration_world(w, ledger="none")


@case(
    "MG03",
    "migration",
    "conflicting",
    CONTRADICTED,
    "File hash differs from the claimed prefix; ledger has the row",
)
def _(w):
    return migration_world(w, prefix_ok=False)


@case(
    "MG04",
    "migration",
    "missing evidence",
    UNVERIFIED,
    "File hashes as claimed; migration ledger unreadable (applied is unobserved)",
)
def _(w):
    return migration_world(w, ledger="unreadable")


@case(
    "MG05",
    "migration",
    "missing evidence",
    UNVERIFIED,
    "Target with no configured ledger: file hashes as claimed; no ledger is consulted",
)
def _(w):
    return migration_world(w, name="other")


# Added with the bounded fix (operator ruling: ledger evidence IN ADDITION TO the file).
@case(
    "MG06",
    "migration",
    "missing evidence",
    UNVERIFIED,
    "Ledger has the row, but the claim gives no file hash to check the migration file against",
)
def _(w):
    claim = migration_world(w)
    del claim
    w.verified_by["deployed"]["app"]["migration"]["source_sha256_prefix"] = ""
    return one(w.verifier().deployment_claims(), "migration:")


# ============================================================================= test results


def results_world(w: World, claim: str, runs: list[ev.ToolRun]) -> Claim:
    w.verified_by = {"tests": {"t": claim}}
    w.claude_runs = runs
    return one(w.verifier().test_claims(), "test:")


@case(
    "TS01",
    "test result",
    "genuine",
    VERIFIED,
    "Claim 'test_a.py 5 passed'; recorded execution of test_a.py printed 5 passed, exit 0",
)
def _(w):
    return results_world(w, "test_a.py 5 passed", [run("pytest -q tests/test_a.py", "5 passed")])


@case(
    "TS02",
    "test result",
    "self-report only",
    UNVERIFIED,
    "Claim '44 passed'; no recorded execution at all",
)
def _(w):
    return results_world(w, "44 passed", [])


@case(
    "TS03",
    "test result",
    "self-report only",
    UNVERIFIED,
    "Claim '44 passed'; the only '44 passed' is a cat of a log file",
)
def _(w):
    return results_world(w, "44 passed", [run("cat runs/out.txt", "44 passed")])


@case(
    "TS04",
    "test result",
    "conflicting",
    CONTRADICTED,
    "Claim 'tests PASS: 34 passed'; run printed '1 failed, 34 passed', exit 1",
)
def _(w):
    return results_world(
        w, "tests PASS: 34 passed", [run("pytest -q tests/test_a.py", "1 failed, 34 passed", 1)]
    )


@case(
    "TS05",
    "test result",
    "conflicting",
    CONTRADICTED,
    "Claim '34 passed'; run exit 1 with no failure printed",
)
def _(w):
    return results_world(w, "34 passed", [run("pytest -q tests/test_a.py", "34 passed", 1)])


@case(
    "TS06",
    "test result",
    "mixed test history",
    CONTRADICTED,
    "test_a.py: 5 passed at t0, then '1 failed, 4 passed' at t1",
)
def _(w):
    return results_world(
        w,
        "test_a.py 5 passed",
        [
            run("pytest -q tests/test_a.py", "5 passed"),
            run("pytest -q tests/test_a.py", "1 failed, 4 passed", 1, T0 + timedelta(minutes=1)),
        ],
    )


@case(
    "TS07",
    "test result",
    "mixed test history",
    VERIFIED,
    "test_a.py: red run '1 failed, 4 passed' at t0, then 5 passed at t1",
)
def _(w):
    return results_world(
        w,
        "test_a.py 5 passed",
        [
            run("pytest -q tests/test_a.py", "1 failed, 4 passed", 1),
            run("pytest -q tests/test_a.py", "5 passed", 0, T0 + timedelta(minutes=1)),
        ],
    )


@case(
    "TS08",
    "test result",
    "mixed test history",
    CONTRADICTED,
    "test_b pass, test_b fail, then test_a pass (claim names both files)",
)
def _(w):
    return results_world(
        w,
        "test_a.py 5 passed; test_b.py 5 passed",
        [
            run("pytest -q tests/test_b.py", "5 passed"),
            run("pytest -q tests/test_b.py", "1 failed, 4 passed", 1, T0 + timedelta(minutes=1)),
            run("pytest -q tests/test_a.py", "5 passed", 0, T0 + timedelta(minutes=2)),
        ],
    )


@case(
    "TS09",
    "test result",
    "mixed test history",
    UNVERIFIED,
    "Only run of the named file exited 4 (never collected)",
)
def _(w):
    return results_world(w, "test_a.py 5 passed", [run("pytest -q tests/test_a.py", "", 4)])


@case(
    "TS10",
    "test result",
    "genuine",
    VERIFIED,
    "Claim '12 failed, 1912 passed'; run printed exactly that, exit 1",
)
def _(w):
    return results_world(
        w, "12 failed, 1912 passed", [run("pytest -q", "12 failed, 1912 passed", 1)]
    )


@case(
    "TS11",
    "test result",
    "missing evidence",
    UNVERIFIED,
    "Claim 'branch 1885 passed vs main 1857 passed'; only the branch count was executed",
)
def _(w):
    return results_world(
        w, "branch 1885 passed vs main 1857 passed", [run("pytest -q", "1885 passed")]
    )


@case(
    "TS12",
    "test result",
    "missing evidence",
    UNVERIFIED,
    "Claim names test_a.py and test_b.py; only test_a.py ran",
)
def _(w):
    return results_world(
        w, "test_a.py 5 passed; test_b.py 5 passed", [run("pytest -q tests/test_a.py", "5 passed")]
    )


def claude_bash(cid: str, command: str, output: str, at: datetime, *, tid="tu1", is_error=False):
    return [
        {
            "type": "assistant",
            "timestamp": at.isoformat(),
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "id": tid, "input": {"command": command}}
                ]
            },
        },
        {
            "type": "user",
            "timestamp": at.isoformat(),
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": output,
                        "is_error": is_error,
                    }
                ]
            },
        },
    ]


@case(
    "TS13",
    "test result",
    "genuine",
    VERIFIED,
    "Matching run recorded in the ticket session's own Claude transcript, inside its window",
)
def _(w):
    w.session()
    w.claude(CID, claude_bash(CID, "pytest -q tests/test_a.py", "5 passed in 0.2s", T0))
    w.verified_by = {"tests": {"t": "test_a.py 5 passed"}}
    return one(w.verifier().test_claims(), "test:")


@case(
    "TS14",
    "test result",
    "wrong session",
    UNVERIFIED,
    "Matching run exists only in the transcript of a Claude session with no part in this ticket",
)
def _(w):
    w.session()
    w.claude(CID, [])
    w.claude(STRANGER, claude_bash(STRANGER, "pytest -q tests/test_a.py", "5 passed in 0.2s", T0))
    w.verified_by = {"tests": {"t": "test_a.py 5 passed"}}
    return one(w.verifier().test_claims(), "test:")


@case(
    "TS15",
    "test result",
    "wrong session",
    UNVERIFIED,
    "Matching run in the ticket session's transcript but a day before that session started",
)
def _(w):
    w.session()
    w.claude(
        CID,
        claude_bash(CID, "pytest -q tests/test_a.py", "5 passed in 0.2s", T0 - timedelta(days=1)),
    )
    w.verified_by = {"tests": {"t": "test_a.py 5 passed"}}
    return one(w.verifier().test_claims(), "test:")


# ============================================================================= delegations

WORK = [(exec_call("git status"), '{"exit_code":0}')]


def delegation_world(w: World, *, calls=WORK, phases=PASS_FINAL, rollout=True, **row) -> Claim:
    if rollout:
        w.rollout(
            "d",
            f"delegation: {row.pop('packet', DELEG)}\nparent task: 1001",
            calls,
            phases=phases,
            started=row.pop("started", T0 + timedelta(seconds=5)),
            originator=row.pop("originator", "codex_exec"),
        )
    w.delegations.append(deleg(**row))
    return one(w.verifier().delegation_claims(), "delegation:")


@case(
    "DL01",
    "delegation",
    "genuine",
    VERIFIED,
    "Codex transcript bound by its own packet, started 5s after the record, ran commands, gave a final_answer",
)
def _(w):
    return delegation_world(w)


@case(
    "DL02",
    "delegation",
    "self-report only",
    UNVERIFIED,
    "ATS says closed with result 'done'; no transcript names the delegation",
)
def _(w):
    return delegation_world(w, rollout=False, result_summary="done")


@case(
    "DL03",
    "delegation",
    "unfinished reviewer",
    UNVERIFIED,
    "Bound transcript, commands ran, only commentary so far",
)
def _(w):
    return delegation_world(w, phases=[("commentary", "working")], state="open", closed_at=None)


@case(
    "DL04",
    "delegation",
    "missing evidence",
    UNVERIFIED,
    "Bound transcript with a final answer but zero commands recorded",
)
def _(w):
    return delegation_world(w, calls=[])


@case(
    "DL05",
    "delegation",
    "conflicting",
    CONTRADICTED,
    "Recorded 'returned' but result is '(no result: )'",
)
def _(w):
    return delegation_world(
        w, rollout=False, state="returned", result_summary="(no result: )", closed_at=None
    )


@case(
    "DL06",
    "delegation",
    "conflicting",
    CONTRADICTED,
    "Bound transcript was written by a different runtime (originator claude_code)",
)
def _(w):
    return delegation_world(w, originator="claude_code")


@case(
    "DL07",
    "delegation",
    "conflicting",
    CONTRADICTED,
    "Bound transcript started 3 hours after the delegation was closed",
)
def _(w):
    return delegation_world(w, started=T0 + timedelta(hours=3, minutes=30))


@case(
    "DL08",
    "delegation",
    "wrong session",
    UNVERIFIED,
    "Transcript did the work but its packet names a different delegation",
)
def _(w):
    return delegation_world(w, packet=OTHER)


@case(
    "DL09",
    "delegation",
    "self-report only",
    UNVERIFIED,
    "Delegation to a Claude worker (no transcript binding exists for Claude delegates)",
)
def _(w):
    return delegation_world(w, rollout=False, delegated_worker="claude", result_summary="done")


# ============================================================================= handoffs


def event_id(sid: str, at: datetime) -> str:
    return f"ats.session.completed/v1/{sid}/{at.isoformat()}"


def outbox(w: World, sid: str, at: datetime, handoff_id: str | None) -> None:
    """ATS's own record of the completion event it emitted and the handoff it carried."""
    w.outbox.append({"event_id": event_id(sid, at), "session_id": sid, "handoff_id": handoff_id})


def receipt(sid: str, *, stored=True, trigger="ats_completion_event", at=T0) -> dict:
    result = {"handoff": "stored" if stored else "skipped", "event_id": event_id(sid, at)}
    return {
        "state": "OBSERVED",
        "status": 200,
        "body": {
            "queued": [
                {
                    "trigger_source": trigger,
                    "status": "done",
                    "finished_at": at.isoformat(),
                    "result": json.dumps(result),
                }
            ]
        },
    }


def handoff(hid: str, sid: str = "src", at: datetime = T0) -> dict:
    return {
        "id": hid,
        "source_session_id": sid,
        "verdict": "DONE",
        "blockers": "[]",
        "next_steps": "[]",
        "addressing_mode": "ticket",
        "recipient_session_id": None,
        "delivery_history": "[]",
        "created_at": at.isoformat(),
        "acknowledged_at": (at + timedelta(minutes=1)).isoformat(),
    }


@case(
    "HO01",
    "handoff delivery",
    "genuine",
    VERIFIED,
    "The intake service's receipt says it stored the handoff carried by event E; ATS's outbox records that E carried THIS handoff",
)
def _(w):
    w.handoffs.append(handoff("h1"))
    outbox(w, "src", T0, "h1")
    w.http["/receipts/src"] = receipt("src")
    return one(w.verifier().handoff_claims(), "handoff:h1")


@case(
    "HO02",
    "handoff delivery",
    "self-report only",
    UNVERIFIED,
    "Handoff written and acknowledged in ATS; the intake service has no receipt",
)
def _(w):
    w.handoffs.append(handoff("h1"))
    w.http["/receipts/src"] = {"state": "OBSERVED", "status": 200, "body": {"queued": []}}
    return one(w.verifier().handoff_claims(), "handoff:h1")


@case(
    "HO03",
    "handoff delivery",
    "conflicting",
    UNVERIFIED,
    "Intake receipt exists but says the handoff was NOT stored",
)
def _(w):
    w.handoffs.append(handoff("h1"))
    outbox(w, "src", T0, "h1")
    w.http["/receipts/src"] = receipt("src", stored=False)
    return one(w.verifier().handoff_claims(), "handoff:h1")


@case("HO04", "handoff delivery", "missing evidence", UNVERIFIED, "Intake service unreachable")
def _(w):
    w.handoffs.append(handoff("h1"))
    return one(w.verifier().handoff_claims(), "handoff:h1")


@case(
    "HO05",
    "handoff delivery",
    "wrong session",
    UNVERIFIED,
    "Session wrote two handoffs 3h apart; the intake receipt is for the completion event at the second; this is the FIRST",
)
def _(w):
    w.handoffs += [handoff("h1", at=T0), handoff("h2", at=T0 + timedelta(hours=3))]
    outbox(w, "src", T0 + timedelta(hours=3), "h2")
    w.http["/receipts/src"] = receipt("src", at=T0 + timedelta(hours=3))
    return one(w.verifier().handoff_claims(), "handoff:h1")


# Added with the bounded fix (operator ruling: ambiguous correlation stays UNVERIFIED).
@case(
    "HO06",
    "handoff delivery",
    "wrong session",
    UNVERIFIED,
    "The intake service stored a handoff for this session, created at the same second, but ATS has no outbox record tying that event to this handoff",
)
def _(w):
    w.handoffs.append(handoff("h1"))
    w.http["/receipts/src"] = receipt("src")
    return one(w.verifier().handoff_claims(), "handoff:h1")


# ============================================================================= operator acceptance


def acceptance_world(w: World, accepted: str, rows: list[dict], *, cid=CID, sub=False) -> Claim:
    w.session()
    w.claude(CID, [])
    if rows:
        w.claude(cid, rows, sub=sub)
    w.verified_by = {
        "accepted_by": accepted,
        "verified_at": (T0 + timedelta(hours=2)).isoformat(),
        "authority": {"source": "operator_mcp", "caller_uid": 1000},
    }
    return one(w.verifier().acceptance_claim(), "acceptance:")


def typed(text: str, at: datetime = T0 + timedelta(hours=1), **extra) -> dict:
    return {
        "type": "user",
        "timestamp": at.isoformat(),
        "uuid": "u1",
        "message": {"content": text},
        **extra,
    }


PHRASE = "#1001 ACCEPTED / RELEASE APPROVED"


@case(
    "AC01",
    "operator acceptance",
    "genuine",
    # Operator ruling 2026-10-15: acceptance is never verified automatically; the
    # candidate turn is shown for the operator's own inspection (was VERIFIED).
    UNVERIFIED,
    "Operator-typed turn in the ticket session contains the quoted decision (shown as a candidate)",
)
def _(w):
    return acceptance_world(
        w, f"operator '{PHRASE}'", [typed(f"OPERATOR DECISION -- {PHRASE}. Close it.")]
    )


@case(
    "AC02",
    "operator acceptance",
    "self-report only",
    UNVERIFIED,
    "verified_by.accepted_by quotes a decision; no transcript turn contains it",
)
def _(w):
    return acceptance_world(w, f"operator '{PHRASE}'", [typed("unrelated text")])


@case(
    "AC03",
    "operator acceptance",
    "self-report only",
    UNVERIFIED,
    "Phrase appears only in meta, tool_result and notification rows",
)
def _(w):
    rows = [
        typed(PHRASE, isMeta=True),
        {
            "type": "user",
            "timestamp": (T0 + timedelta(hours=1)).isoformat(),
            "message": {"content": [{"type": "tool_result", "content": PHRASE}]},
        },
        typed(f"<task-notification>{PHRASE}</task-notification>"),
    ]
    return acceptance_world(w, f"operator '{PHRASE}'", rows)


@case(
    "AC04",
    "operator acceptance",
    "wrong session",
    UNVERIFIED,
    "Phrase appears only in a subagent transcript (a prompt the parent agent wrote)",
)
def _(w):
    return acceptance_world(w, f"operator '{PHRASE}'", [typed(PHRASE)], sub=True)


@case(
    "AC05",
    "operator acceptance",
    "wrong session",
    UNVERIFIED,
    "Phrase typed in a Claude session that has no part in this ticket",
)
def _(w):
    return acceptance_world(w, f"operator '{PHRASE}'", [typed(PHRASE)], cid=STRANGER)


@case(
    "AC06",
    "operator acceptance",
    "stale",
    UNVERIFIED,
    "Phrase typed 3 days before the ticket existed",
)
def _(w):
    return acceptance_world(w, f"operator '{PHRASE}'", [typed(PHRASE, at=T0 - timedelta(days=3))])


@case(
    "AC07",
    "operator acceptance",
    "conflicting",
    UNVERIFIED,
    "Record quotes 'approved'; the operator actually typed 'this is NOT approved, redo it'",
)
def _(w):
    return acceptance_world(w, "operator said 'approved'", [typed("this is NOT approved, redo it")])


@case(
    "AC08",
    "operator acceptance",
    "self-report only",
    UNVERIFIED,
    "Quoted decision appears only inside an agent's report the operator pasted back for review",
)
def _(w):
    report = f'<pasted_content id="x">Agent report: status DONE. Operator decision: {PHRASE}.</pasted_content>'
    return acceptance_world(w, f"operator '{PHRASE}'", [typed(report)])


@case(
    "AC09",
    "operator acceptance",
    "missing evidence",
    UNVERIFIED,
    "accepted_by has no quoted decision text to look for",
)
def _(w):
    return acceptance_world(w, "operator accepted", [typed("ok ship it")])


# ============================================================================= completion


def completion_world(w: World, description: str, residuals: list[str]) -> Claim:
    w.task = {"status": "completed", "description": description, "created_at": T0.isoformat()}
    w.verified_by = {"verified_at": T0.isoformat(), "residuals": residuals}
    return one(w.verifier().completion_claim(), "completion:")


ACCEPT = "ACCEPTANCE: 188 archived records replayed; reaper emits the v1 contract; health exposes failed events"


@case(
    "CP01",
    "completion",
    "conflicting",
    CONTRADICTED,
    "Closing record's residuals say 'replay of 258 archived records not done'",
)
def _(w):
    return completion_world(
        w, ACCEPT, ["replay of 258 archived records not done (separate decision)"]
    )


@case(
    "CP02",
    "completion",
    "self-report only",
    UNVERIFIED,
    "Status completed, no residuals: criteria have no independent probe",
)
def _(w):
    return completion_world(w, ACCEPT, [])


@case("CP03", "completion", "missing evidence", UNVERIFIED, "Ticket states no ACCEPTANCE section")
def _(w):
    return completion_world(w, "Make it better.", [])


@case(
    "CP04",
    "completion",
    "self-report only",
    UNVERIFIED,
    "Residual negation about something no criterion names",
)
def _(w):
    return completion_world(w, ACCEPT, ["dashboard polish deferred"])


# ============================================================================= runner


def evaluate(c: Case, tmp: Path) -> Claim:
    with ExitStack() as stack:
        return decide(c.build(World(tmp, stack)))


def false_verified(c: Case, verdict: str) -> bool:
    return verdict == VERIFIED and c.expected != VERIFIED


@pytest.mark.parametrize("c", CASES, ids=[c.id for c in CASES])
def test_acceptance_case(c: Case, tmp_path: Path):
    claim = evaluate(c, tmp_path)
    assert not false_verified(c, claim.verdict), f"FALSE VERIFIED: {claim.basis}"
    assert claim.verdict == c.expected, f"{claim.verdict}: {claim.basis} | missing={claim.missing}"


def table() -> list[dict]:
    rows = []
    for c in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                claim = evaluate(c, Path(tmp))
                actual, basis = claim.verdict, claim.basis
            except Exception as exc:  # a crash is reported, never a verdict
                actual, basis = "ERROR", f"{type(exc).__name__}: {exc}"
        rows.append(
            {
                "id": c.id,
                "claim": c.claim,
                "category": c.category,
                "inputs": c.inputs,
                "expected": c.expected,
                "actual": actual,
                "match": actual == c.expected,
                "false_verified": false_verified(c, actual),
                "basis": basis,
            }
        )
    return rows


if __name__ == "__main__":
    rows = table()
    json.dump(rows, sys.stdout if len(sys.argv) < 2 else open(sys.argv[1], "w"), indent=1)
    bad = [r for r in rows if r["false_verified"]]
    print(
        f"\n{len(rows)} cases; {sum(r['match'] for r in rows)} match; "
        f"{len(bad)} FALSE VERIFIED: {[r['id'] for r in bad]}",
        file=sys.stderr,
    )
