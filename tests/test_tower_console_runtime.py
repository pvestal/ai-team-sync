"""Runtime proof: the console must show which process is behind an ATS label.

The fixture is the 2026-10-09 #3531 review chain: a Claude host ran the
adversarial-reviewer subagent, whose Bash call ran `ats delegate`, which
launched the Codex CLI carrying ATS session f881c22c.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ai_team_sync.console import runtime

HOST_CID = "fff39404-a7bb-4476-8794-5c099491e606"
DELEGATE = "f881c22c-acfd-42cf-870d-768df3439645"
TWIN = "cd4ca392-8c92-4ee6-862e-ab8bd38e6c93"
OWNER = "35898fbc-250d-493d-af2f-9614e9ce22b8"
CMD = (
    "timeout 590 ats delegate --worker codex --mode VERIFY "
    "--repo /wt/fix-3531-named-cast --task 3531 -o Adversarial review"
)


def _proc(
    root: Path, pid: int, ppid: int, argv: list[str], environ: dict | None = None, start="100"
):
    base = root / str(pid)
    base.mkdir(parents=True)
    fields = ["S", str(ppid)] + ["0"] * 17 + [start, "0"]
    (base / "stat").write_text(f"{pid} ({os.path.basename(argv[0])}) " + " ".join(fields))
    (base / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")
    env = environ or {}
    (base / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()))


def _fixture(tmp_path: Path, *, recycled: bool = False) -> tuple[str, str]:
    proc = tmp_path / "proc"
    home = tmp_path / "home"
    home.mkdir()
    _proc(proc, 3302789, 1, ["claude"], start="94634969")
    _proc(proc, 3388845, 3302789, ["ugrep", "-iE", "x"])  # Claude's bundled tool
    _proc(
        proc,
        3369987,
        3302789,
        ["/bin/bash", "-c", f"source snap.sh && {{ eval '{CMD}' }}"],
    )
    _proc(proc, 3369988, 3369987, CMD.split())
    _proc(
        proc,
        3369990,
        3369988,
        ["/venv/bin/python", "/home/p/.local/bin/ats", "delegate", "--worker", "codex"],
        environ={"OPENAI_API_KEY": "sk-should-never-be-read-out", "PATH": "/bin"},
    )
    _proc(
        proc,
        3370117,
        3369990,
        [
            "node",
            "/usr/bin/codex",
            "exec",
            "-c",
            f'mcp_servers.ai-team-sync.env.ATS_SESSION_ID="{DELEGATE}"',
        ],
    )
    _proc(
        proc,
        3370128,
        3370117,
        [
            "/usr/lib/node_modules/@openai/codex/bin/codex",
            "exec",
            "-c",
            f'x.ATS_SESSION_ID="{DELEGATE}"',
        ],
    )
    (home / ".ats_live_cid_3302789").write_text(
        json.dumps(
            {"cid": HOST_CID, "pid": "3302789", "starttime": "1" if recycled else "94634969"}
        )
    )
    sub = home / ".claude" / "projects" / "-home-p-Documents" / HOST_CID / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.meta.json").write_text(
        json.dumps(
            {"agentType": "adversarial-reviewer", "description": "Codex review of #3531 diff"}
        )
    )
    tool_use = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": CMD}}]},
    }
    (sub / "agent-a.jsonl").write_text(json.dumps(tool_use) + "\n")
    return str(proc), str(home)


def _sessions(now: str = "2026-10-09T15:19:50+00:00") -> list[dict]:
    return [
        {
            "id": OWNER,
            "agent": "claude-code:fff39404",
            "status": "active",
            "task_id": 3531,
            "effective_mode": "DIRECT",
            "parent_session_id": None,
            "started_at": now,
        },
        {
            "id": DELEGATE,
            "agent": "codex:delegate",
            "status": "active",
            "task_id": None,
            "effective_mode": "VERIFY",
            "parent_session_id": OWNER,
            "delegation_id": "6e78a07b",
            "started_at": now,
        },
        {
            "id": TWIN,
            "agent": "codex:delegate",
            "status": "completed",
            "task_id": 3531,
            "effective_mode": "DIRECT",
            "parent_session_id": None,
            "delegation_id": None,
            "started_at": "2026-10-09T15:20:06+00:00",
        },
    ]


def _view(tmp_path, sessions=None, **kwargs):
    proc, home = _fixture(tmp_path, **kwargs)
    procs = runtime.read_proc_table(proc)
    live = runtime.read_live_cids(home, procs)
    commands = runtime.read_subagent_commands(home, sorted(set(live.values())))
    now = time.mktime(time.strptime("2026-10-09 16:00", "%Y-%m-%d %H:%M"))
    return procs, runtime.build_runtime_view(
        sessions or _sessions(), procs, live, commands, now=now
    )


def test_classifies_by_running_executable_not_label():
    assert runtime.classify(["node", "/usr/bin/codex", "exec"]) == "codex"
    assert runtime.classify(["/venv/bin/python", "/x/bin/ats", "delegate"]) == "ats"
    assert runtime.classify(["claude"]) == "claude"
    # Claude Code re-executes its own binary for bundled tools.
    assert runtime.classify(["ugrep", "-iE"], "/home/p/.local/share/claude/versions/2.1") == "other"
    assert runtime.label_runtime("codex:delegate") == "codex"
    assert runtime.label_runtime("claude-code:fff39404") == "claude"


def test_reconstructs_the_3531_review_chain(tmp_path):
    _, view = _view(tmp_path)
    assert view["verdicts"][OWNER]["state"] == "VERIFIED"
    assert view["verdicts"][OWNER]["pid"] == 3302789
    assert view["verdicts"][DELEGATE] == {
        "state": "VERIFIED",
        "pid": 3370117,
        "label_runtime": "codex",
        "process_runtime": "codex",
        "evidence": "ATS_SESSION_ID on process",
    }
    text = "\n".join(line for line, _ in runtime.render_lines(view))
    assert "CLAUDE pid 3302789  ⇐ claude-code:fff39404 35898fbc #3531 DIRECT ✓" in text
    assert "via Claude subagent:adversarial-reviewer (in-process, no pid)" in text
    assert "ATS pid 3369990" in text
    assert "CODEX pid 3370117  ⇐ codex:delegate f881c22c VERIFY ✓" in text
    assert "CODEX pid 3370128" in text
    assert "3388845" not in text  # bundled ugrep is not an agent
    order = [
        text.index(s)
        for s in ("CLAUDE pid", "subagent:adversarial", "ATS pid", "CODEX pid 3370117")
    ]
    assert order == sorted(order)


def test_flags_the_unlinked_delegate_twin(tmp_path):
    _, view = _view(tmp_path)
    assert [gap["id"] for gap in view["lineage_gaps"]] == [TWIN]
    text = "\n".join(line for line, _ in runtime.render_lines(view))
    assert "LINEAGE GAPS 1 in 24h" in text
    assert "codex:delegate cd4ca392 #3531" in text


def test_label_that_disagrees_with_the_process_is_a_mismatch(tmp_path):
    sessions = _sessions()
    sessions[1]["agent"] = "claude-code:reviewer"  # claims Claude; process is Codex
    _, view = _view(tmp_path, sessions)
    assert view["verdicts"][DELEGATE]["state"] == "MISMATCH"
    lines = runtime.render_lines(view)
    assert any("LABEL≠PROCESS" in line and style == "bold red" for line, style in lines)


def test_recycled_host_pid_is_not_attributed(tmp_path):
    _, view = _view(tmp_path, recycled=True)
    assert view["verdicts"][OWNER]["state"] == "NO_PROCESS"


def test_environ_contributes_only_the_session_key(tmp_path):
    procs, view = _view(tmp_path)
    blob = json.dumps(view, default=str) + repr(procs)
    assert "sk-should-never-be-read-out" not in blob
    assert "OPENAI_API_KEY" not in blob


def test_missing_proc_root_is_unavailable(tmp_path):
    assert (
        runtime.runtime_snapshot([], home=str(tmp_path), proc_root=str(tmp_path / "nope"))["state"]
        == "UNAVAILABLE"
    )


def test_claude_host_without_active_session_says_so(tmp_path):
    _, view = _view(tmp_path, [_sessions()[2]])
    text = "\n".join(line for line, _ in runtime.render_lines(view))
    assert "claude-code:fff39404 (no active ATS session)" in text
