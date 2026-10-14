#!/usr/bin/env python3
"""PreToolUse(Bash) hook: refuse raw service control of GOVERNED units (#3580).

DEFENSE IN DEPTH, NOT THE BOUNDARY. This hook sees only the command string an
agent hands to Claude Code's Bash tool. It cannot see the operator's terminal,
cron, timers, deploy scripts, other agents' tools, a script file that runs
systemctl internally, or anything that reaches systemd over D-Bus. The user it
runs as has passwordless sudo, so nothing here is an OS-level control. What it
does do is stop the one path that caused the 2026-10-13 incident -- an agent
typing `sudo systemctl restart anime-studio` -- and point at the gated path,
`ats safe-restart`. See docs/restart-gate.md for the full boundary.

Blocked when the target is a governed unit (restart_gate.load_gate_config):
  * systemctl restart|stop|kill|try-restart|reload-or-restart|
    try-reload-or-restart|condrestart|force-reload, and disable|mask --now,
    including --user, a .service suffix, and globs that match a governed unit
  * service <unit> restart|stop|try-restart|condrestart|force-reload
  * kill <pid> where pid is a governed unit's MainPID
  * pkill/killall whose pattern names a governed unit, or (pkill -f) matches its
    MainPID's command line
through the wrappers sudo, doas, env, nice, ionice, timeout, nohup, exec,
command, time, stdbuf, setsid, and through `sh|bash|zsh|dash -c "..."` and
`eval`, after splitting on ; && || | & and newlines, and inside $(...)/backticks.

Unparseable input (shlex cannot tokenize it) is ALLOWED with a warning in the
agent's context: a hook that wedges on odd quoting would be disabled, and this
hook is not the boundary anyway.

Wire (~/.claude/settings.json) -- shown in docs only, never installed by ATS:
  "PreToolUse": [{ "matcher": "Bash", "hooks": [{ "type": "command",
      "command": "<ats-venv>/bin/python -m ai_team_sync.hooks.pre_tool_use_restart_guard" }] }]
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ai_team_sync.restart_gate import load_gate_config, normalize_unit

CONTROL_VERBS = {
    "restart", "stop", "kill", "try-restart", "reload-or-restart",
    "try-reload-or-restart", "condrestart", "force-reload",
}
NOW_VERBS = {"disable", "mask"}  # stop the unit only with --now
SERVICE_VERBS = {"restart", "stop", "try-restart", "condrestart", "force-reload"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
OPERATORS = {";", "&&", "||", "|", "&", "|&", ";;", "(", ")", "$(", "`"}

# Options that consume the following word, per wrapper.
_SUDO_ARG_OPTS = {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "-R"}
_ENV_ARG_OPTS = {"-u", "-C", "--unset", "--chdir"}
_NICE_ARG_OPTS = {"-n", "--adjustment"}
_IONICE_ARG_OPTS = {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"}
_TIMEOUT_ARG_OPTS = {"-s", "-k", "--signal", "--kill-after"}
_STDBUF_ARG_OPTS = {"-i", "-o", "-e"}
_SYSTEMCTL_ARG_OPTS = {"-H", "--host", "-M", "--machine", "-s", "--signal", "--kill-whom",
                       "--kill-value", "-p", "--property", "-t", "--type", "--state",
                       "--job-mode", "-n", "--lines", "-o", "--output", "--root",
                       "--what", "--timestamp", "--check-inhibitors", "--when"}
_PKILL_ARG_OPTS = {"-g", "-G", "-P", "-s", "-t", "-u", "-U", "--signal", "--ns", "--nslist",
                   "-F", "--pidfile", "--group", "--euid", "--uid", "--parent", "--session",
                   "--terminal", "--pgroup"}


class Unparseable(ValueError):
    pass


@dataclass
class Verdict:
    blocked: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class UnitProcess:
    unit: str
    pid: int
    cmdline: str


PidLookup = Callable[[Iterable[str]], list[UnitProcess]]


def _tokens(command: str) -> list[str]:
    # Newlines separate commands; command substitution opens a nested command.
    text = command.replace("\r", "\n").replace("\n", " ; ")
    text = text.replace("$(", " $( ").replace("`", " ` ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as exc:
        raise Unparseable(str(exc)) from exc


def _segments(tokens: list[str]) -> list[list[str]]:
    out, cur = [], []
    for tok in tokens:
        if tok in OPERATORS or (tok and set(tok) <= set(";&|()")):
            if cur:
                out.append(cur)
            cur = []
            continue
        if tok in ("$", "$("):
            continue
        cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _skip_opts(words: list[str], i: int, arg_opts: set[str]) -> int:
    while i < len(words):
        w = words[i]
        if w == "--":
            return i + 1
        if not w.startswith("-") or w == "-":
            return i
        if w in arg_opts:
            i += 2
            continue
        i += 1
    return i


_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _unwrap(words: list[str]) -> tuple[list[str], list[str]]:
    """(the real command words, nested scripts to analyze separately)."""
    nested: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        base = os.path.basename(w)
        if _ASSIGN.match(w):
            i += 1
        elif base in ("sudo", "doas"):
            i = _skip_opts(words, i + 1, _SUDO_ARG_OPTS)
        elif base == "env":
            j = i + 1
            while j < len(words):
                if words[j] in ("-S", "--split-string") and j + 1 < len(words):
                    nested.append(words[j + 1])
                    j += 2
                elif words[j] in _ENV_ARG_OPTS:
                    j += 2
                elif words[j].startswith("-") or _ASSIGN.match(words[j]):
                    j += 1
                else:
                    break
            i = j
        elif base == "nice":
            i = _skip_opts(words, i + 1, _NICE_ARG_OPTS)
        elif base == "ionice":
            i = _skip_opts(words, i + 1, _IONICE_ARG_OPTS)
        elif base == "timeout":
            i = _skip_opts(words, i + 1, _TIMEOUT_ARG_OPTS) + 1  # then the DURATION
        elif base == "stdbuf":
            i = _skip_opts(words, i + 1, _STDBUF_ARG_OPTS)
        elif base in ("nohup", "exec", "command", "time", "setsid", "builtin"):
            i = _skip_opts(words, i + 1, set())
        elif base in SHELLS:
            j = i + 1
            while j < len(words) and words[j].startswith("-"):
                if "c" in words[j].lstrip("-") and not words[j].startswith("--"):
                    if j + 1 < len(words):
                        nested.append(words[j + 1])
                    return [], nested
                j += 1
            return [], nested  # `bash script.sh`: not visible to this hook
        elif base == "eval":
            nested.append(" ".join(words[i + 1:]))
            return [], nested
        else:
            return words[i:], nested
    return [], nested


def _matches(target: str, governed: frozenset[str]) -> list[str]:
    name = normalize_unit(target.strip("'\""))
    if not name:
        return []
    if any(c in name for c in "*?["):
        return sorted(u for u in governed if fnmatch.fnmatch(u, name))
    return [name] if name in governed else []


def _systemctl(args: list[str], governed: frozenset[str]) -> list[str]:
    i = _skip_opts(args, 0, _SYSTEMCTL_ARG_OPTS)
    if i >= len(args):
        return []
    verb = args[i]
    rest = args[i + 1:]
    flags = {a for a in args if a.startswith("-")}
    if verb not in CONTROL_VERBS and not (verb in NOW_VERBS and "--now" in flags):
        return []
    hits = []
    j = 0
    while j < len(rest):
        a = rest[j]
        if a in _SYSTEMCTL_ARG_OPTS:
            j += 2
            continue
        if not a.startswith("-"):
            hits += [f"systemctl {verb} {u}" for u in _matches(a, governed)]
        j += 1
    return hits


def _service(args: list[str], governed: frozenset[str]) -> list[str]:
    words = [a for a in args if not a.startswith("-")]
    if len(words) >= 2 and words[1] in SERVICE_VERBS:
        return [f"service {u} {words[1]}" for u in _matches(words[0], governed)]
    return []


def _kill(base: str, args: list[str], governed: frozenset[str],
          pid_lookup: PidLookup, verdict: Verdict) -> list[str]:
    if base == "kill":
        pids = []
        for a in args:
            if a.lstrip("-").isdigit() and not a.startswith("-"):
                pids.append(int(a))
        if not pids:
            return []
        procs = pid_lookup(governed)
        if procs is None:
            verdict.warnings.append("could not resolve governed units' MainPIDs for `kill`")
            return []
        return [f"kill {p.pid} ({p.unit} MainPID)" for p in procs if p.pid in pids]
    # pkill / killall
    full = "-f" in args or "--full" in args or any(
        a.startswith("-") and not a.startswith("--") and "f" in a[1:] and base == "pkill"
        for a in args)
    patterns = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _PKILL_ARG_OPTS:
            i += 2
            continue
        if not a.startswith("-"):
            patterns.append(a)
        i += 1
    hits = []
    for pat in patterns:
        lowered = pat.lower()
        for unit in sorted(governed):
            if unit in lowered or (base == "killall" and normalize_unit(pat) == unit):
                hits.append(f"{base} {pat} (names {unit})")
    if full and patterns:
        procs = pid_lookup(governed)
        if procs is None:
            verdict.warnings.append("could not resolve governed units' command lines for `pkill -f`")
        else:
            for pat in patterns:
                for p in procs:
                    try:
                        if p.cmdline and re.search(pat, p.cmdline):
                            hits.append(f"{base} -f {pat} (matches {p.unit} MainPID {p.pid})")
                    except re.error:
                        if pat in p.cmdline:
                            hits.append(f"{base} -f {pat} (matches {p.unit} MainPID {p.pid})")
    return hits


def analyze(command: str, governed: frozenset[str], pid_lookup: PidLookup | None = None,
            _depth: int = 0) -> Verdict:
    verdict = Verdict()
    if _depth > 5:
        verdict.warnings.append("nesting too deep to analyze")
        return verdict
    pid_lookup = pid_lookup or systemd_main_pids
    tokens = _tokens(command)  # may raise Unparseable
    for words in _segments(tokens):
        words = [w.strip("`") for w in words if w.strip("`")]
        real, nested = _unwrap(words)
        for script in nested:
            sub = analyze(script, governed, pid_lookup, _depth + 1)
            verdict.blocked += sub.blocked
            verdict.warnings += sub.warnings
        if not real:
            continue
        base = os.path.basename(real[0])
        args = real[1:]
        if base == "systemctl":
            verdict.blocked += _systemctl(args, governed)
        elif base == "service":
            verdict.blocked += _service(args, governed)
        elif base in ("kill", "pkill", "killall"):
            verdict.blocked += _kill(base, args, governed, pid_lookup, verdict)
    return verdict


def systemd_main_pids(units: Iterable[str]) -> list[UnitProcess] | None:
    """MainPID + cmdline of each governed unit, system and user managers. None
    when systemctl cannot answer at all."""
    units = sorted(units)
    found: list[UnitProcess] = []
    answered = False
    for scope in ([], ["--user"]):
        try:
            proc = subprocess.run(
                ["systemctl", *scope, "show", "-p", "Id", "-p", "MainPID",
                 *[f"{u}.service" for u in units]],
                capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        answered = answered or proc.returncode == 0
        for block in proc.stdout.strip().split("\n\n"):
            vals = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            try:
                pid = int(vals.get("MainPID", "0") or 0)
            except ValueError:
                continue
            if pid <= 0:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
            except OSError:
                cmdline = ""
            found.append(UnitProcess(normalize_unit(vals.get("Id", "")), pid, cmdline))
    return found if answered else None


def _governed() -> tuple[frozenset[str], str]:
    cfg = load_gate_config()
    return cfg.governed, cfg.error


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if payload.get("tool_name") != "Bash":
        sys.exit(0)
    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        sys.exit(0)
    governed, config_error = _governed()
    warnings = []
    # The config warning only accompanies commands that could control a service;
    # repeating it on every `ls` would train agents to ignore the guard.
    if config_error and re.search(r"\b(systemctl|service|kill|pkill|killall)\b", command):
        warnings.append(f"ATS RESTART GUARD: restart_gate config unreadable ({config_error}); "
                        f"guarding the default governed units {sorted(governed)}")
    try:
        verdict = analyze(command, governed)
    except Unparseable as exc:
        verdict = Verdict(warnings=[f"ATS RESTART GUARD: could not parse this command ({exc}); "
                                    "it was NOT checked for service control of governed units"])
    if verdict.blocked:
        print("ATS RESTART GUARD: raw service control of a GOVERNED unit is refused:\n"
              + "\n".join(f"  - {b}" for b in verdict.blocked)
              + "\nUse `ats safe-restart <unit> --reason \"...\"` -- it refuses while requests, "
                "jobs or GPU queues are in flight, drains the service, restarts it, verifies "
                "it, and records the attempt and the outcome (#3580). If the gate refuses, "
                "the refusal names what is in flight; wait for it or ask the operator.",
              file=sys.stderr)
        sys.exit(2)
    warnings += [w if w.startswith("ATS RESTART GUARD") else f"ATS RESTART GUARD: {w}"
                 for w in verdict.warnings]
    if warnings:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "\n".join(warnings),
        }}))
    sys.exit(0)


if __name__ == "__main__":
    main()
