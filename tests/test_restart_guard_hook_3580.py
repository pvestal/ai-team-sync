"""The Bash restart guard (#3580): defense in depth, not the boundary.

T2g of the acceptance plan: the raw service-control spellings an agent actually
types against a governed unit are blocked and pointed at `ats safe-restart`;
read-only systemctl and the sanctioned wrapper pass. The hook is NOT claimed to
cover the operator's terminal, cron, timers, scripts or D-Bus -- see
docs/restart-gate.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from ai_team_sync.hooks import pre_tool_use_restart_guard as guard

GOVERNED = frozenset({"anime-studio", "comfyui", "comfyui-rocm", "tower-echo-brain"})
PROCS = [guard.UnitProcess("anime-studio", 4242,
                           "/opt/anime-studio/venv/bin/python /opt/anime-studio/server/app.py")]


def _blocked(cmd: str) -> list[str]:
    return guard.analyze(cmd, GOVERNED, pid_lookup=lambda units: PROCS).blocked


@pytest.mark.parametrize("cmd", [
    # the T2g matrix
    "sudo systemctl restart anime-studio",
    "systemctl restart anime-studio.service",
    "sudo -n systemctl try-restart anime-studio",
    'bash -c "systemctl stop anime-studio"',
    "env X=1 systemctl kill anime-studio",
    "service anime-studio restart",
    # more of the same class
    "sudo -u root -n systemctl reload-or-restart anime-studio",
    "systemctl --user restart anime-studio",
    "systemctl -q --no-block restart anime-studio",
    "timeout -s KILL 30 sudo systemctl restart anime-studio",
    "nice -n 5 nohup systemctl condrestart anime-studio",
    "cd /opt/anime-studio && git pull && sudo systemctl restart anime-studio",
    "git pull; sudo systemctl restart anime-studio",
    "echo go\nsudo systemctl restart anime-studio",
    "sudo bash -lc 'cd /x && systemctl restart anime-studio'",
    "sh -c 'sudo systemctl stop comfyui'",
    "echo $(sudo systemctl restart anime-studio)",
    "echo `systemctl restart anime-studio`",
    "eval sudo systemctl restart anime-studio",
    "env -S 'systemctl restart anime-studio'",
    "systemctl restart 'anime-*'",
    "systemctl restart nginx anime-studio",
    "sudo systemctl disable --now comfyui",
    "sudo systemctl mask --now comfyui-rocm",
    "/usr/bin/systemctl restart ANIME-STUDIO.service",
    "sudo kill 4242",
    "kill -9 4242",
    "sudo pkill -f server/app.py",
    "pkill -f anime-studio",
    "killall comfyui",
])
def test_raw_service_control_of_a_governed_unit_is_blocked(cmd):
    assert _blocked(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "ats safe-restart anime-studio --reason 'deploy 06aefa90'",
    "systemctl status anime-studio",
    "systemctl show -p MainPID --value anime-studio",
    "systemctl is-active anime-studio",
    "journalctl -u anime-studio -n 50",
    "sudo systemctl restart nginx",
    "systemctl --user restart ats-p0-test-123",
    "systemctl disable comfyui",          # no --now: does not stop it
    "kill 9999",                          # not a governed MainPID
    "pkill -f some-other-script.py",
    "echo 'systemctl restart anime-studio'",  # quoted text, not a command
    "grep -r 'systemctl restart' docs/",
    "git commit -m 'restart anime-studio safely'",
])
def test_reads_and_the_sanctioned_wrapper_pass(cmd):
    assert not _blocked(cmd), cmd


def test_unparseable_commands_are_allowed_with_a_warning():
    with pytest.raises(guard.Unparseable):
        guard.analyze("echo 'unterminated", GOVERNED)


def test_unresolvable_pids_warn_rather_than_guess():
    v = guard.analyze("kill 4242", GOVERNED, pid_lookup=lambda units: None)
    assert not v.blocked and v.warnings


COMPLETE_CONFIG = """
[restart_gate]
governed_units = ["anime-studio"]

[restart_gate.units."anime-studio"]
drain_url = "http://127.0.0.1:8401/api/system/drain"
release_url = "http://127.0.0.1:8401/api/system/drain/release"
probes = [
  { kind = "inflight", url = "http://127.0.0.1:8401/api/system/inflight" },
  { kind = "comfyui", url = "http://127.0.0.1:8188/queue" },
]
"""


def _run_hook(payload: dict, tmp_path) -> subprocess.CompletedProcess:
    cfg = tmp_path / "operator.toml"
    cfg.write_text(COMPLETE_CONFIG)
    env = {**os.environ, "ATS_OPERATOR_CONFIG": str(cfg),
           "PYTHONPATH": os.pathsep.join(sys.path)}
    return subprocess.run([sys.executable, "-m", "ai_team_sync.hooks.pre_tool_use_restart_guard"],
                          input=json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=30)


def test_hook_process_blocks_with_exit_2_and_names_the_wrapper(tmp_path):
    proc = _run_hook({"tool_name": "Bash",
                      "tool_input": {"command": "sudo systemctl restart anime-studio"}}, tmp_path)
    assert proc.returncode == 2
    assert "ats safe-restart" in proc.stderr


def test_hook_process_allows_other_tools_and_reads(tmp_path):
    assert _run_hook({"tool_name": "Edit", "tool_input": {}}, tmp_path).returncode == 0
    ok = _run_hook({"tool_name": "Bash",
                    "tool_input": {"command": "systemctl status anime-studio"}}, tmp_path)
    assert ok.returncode == 0 and ok.stdout == ""


def test_hook_process_warns_on_unparseable(tmp_path):
    proc = _run_hook({"tool_name": "Bash", "tool_input": {"command": "echo 'oops"}}, tmp_path)
    assert proc.returncode == 0
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "could not parse" in ctx


def test_a_config_warning_is_shown_only_on_service_control_commands(tmp_path):
    env = {**os.environ, "ATS_OPERATOR_CONFIG": str(tmp_path / "absent.toml"),
           "PYTHONPATH": os.pathsep.join(sys.path)}

    def run(cmd):
        return subprocess.run(
            [sys.executable, "-m", "ai_team_sync.hooks.pre_tool_use_restart_guard"],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
            capture_output=True, text=True, env=env, timeout=30)

    quiet = run("ls -la")
    assert quiet.returncode == 0 and quiet.stdout == ""
    noisy = run("systemctl status anime-studio")
    assert noisy.returncode == 0 and "config unreadable" in noisy.stdout


def test_hook_process_guards_the_defaults_when_config_is_broken(tmp_path):
    cfg = tmp_path / "operator.toml"
    cfg.write_text("[restart_gate]\nnonsense = 1\n")
    env = {**os.environ, "ATS_OPERATOR_CONFIG": str(cfg),
           "PYTHONPATH": os.pathsep.join(sys.path)}
    proc = subprocess.run(
        [sys.executable, "-m", "ai_team_sync.hooks.pre_tool_use_restart_guard"],
        input=json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "sudo systemctl restart comfyui"}}),
        capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 2, "an unreadable config still guards the default units"
