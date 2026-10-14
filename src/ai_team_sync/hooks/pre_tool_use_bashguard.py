#!/usr/bin/env python3
"""PreToolUse hook for Claude Code's Bash tool: a BEST-EFFORT write guard
(#3581 part B, defense in depth).

The edit guard (pre_tool_use_lockcheck) sees Edit/Write/MultiEdit/NotebookEdit.
A shell command reaches the same files without it -- `sed -i`, a redirect,
`git apply`, `git merge`. On 2026-10-13 a `git merge` changed five files another
live session had claimed. This hook reads the Bash command, extracts the paths
it can PROVE the command writes, and asks ATS about them before it runs.

Extracted (static parse, no execution of the command):
  sed -i / perl -i files; tee [-a] files; > >> >| &> &>> <> redirects;
  cp / mv / install / rsync / ln destinations (mv and rsync --remove-source-files
  sources too); truncate; rm / unlink; git apply / git am (paths from
  `git apply --numstat`); git checkout [<rev>] -- <paths>, git checkout/switch
  <branch>; git restore; git rm / git mv; git clean (via its own dry run);
  git merge | cherry-pick | revert | rebase | pull | stash [push|pop|apply] |
  reset --hard (changed paths by diffing against the target ref).

NOT extracted -- allowed with a warning, listed as residual in
docs/commit-guard.md: interpreters (python/node/ruby/perl -e/php ...), `bash -c`
/ `sh -c` / eval / source, xargs, find -exec/-delete, command substitution or
variables inside a target path, make/npm/any other program that writes files,
and anything this parser does not recognise. A Bash guard cannot see what an
arbitrary program writes; this hook never claims to.

Verdict per resolved target:
  * inside a GOVERNED repository (operator_config) or any of its worktrees:
      - another live session's lock (any mode) covers it      -> exit 2, holder named;
      - a target named directly on the command line must also be covered by the
        editing session's own live lock (the edit guard's claim_check);
        whole-tree git results are not claim-checked;
      - ATS unreachable                                       -> exit 2 (fail closed).
  * elsewhere: another session's EXCLUSIVE lock -> exit 2; advisory -> warning;
    ATS unreachable -> allow (the edit guard's legacy behaviour).

ATS_LOCKCHECK_BLOCK=0 / ATS_CLAIMCHECK=0 downgrade exactly as they do for the
edit guard. Wiring (docs only -- not installed by anything):
  "PreToolUse": [{ "matcher": "Bash", "hooks": [{ "type": "command",
     "command": "<ats-venv>/bin/python -m ai_team_sync.hooks.pre_tool_use_bashguard" }] }]
"""

from __future__ import annotations

import glob as _glob
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

# --- result types ---------------------------------------------------------------


@dataclass
class Target:
    path: str      # absolute, normalised
    direct: bool   # named literally on the command line (claim-checked)
    origin: str    # what produced it, for messages


@dataclass
class Analysis:
    targets: list[Target] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def add(self, path: str, direct: bool, origin: str) -> None:
        self.targets.append(Target(os.path.normpath(path), direct, origin))

    def miss(self, what: str) -> None:
        if what not in self.unresolved:
            self.unresolved.append(what)


# --- tokenizer --------------------------------------------------------------------

@dataclass
class Word:
    text: str
    dynamic: bool = False   # contained an unquoted/double-quoted $ or substitution
    glob: bool = False      # contained an unquoted * ? [


OPS = ("&>>", "<<<", "<<-", "&>", ">>", ">|", ">&", "<<", "<>", "<&", "&&", "||", "|&",
       ";;", ">", "<", "|", "&", ";", "(", ")")
SEPARATORS = {";", "&&", "||", "|", "|&", "&", ";;", "(", ")"}
WRITE_REDIRS = {">", ">>", ">|", "&>", "&>>", "<>"}
READ_REDIRS = {"<", "<<<", "<&"}
HEREDOCS = {"<<", "<<-"}


def _balanced(s: str, i: int, open_: str = "(", close: str = ")") -> int:
    """Index just past the `close` matching s[i] == open_ (quote-aware)."""
    depth, j, quote = 0, i, ""
    while j < len(s):
        c = s[j]
        if quote:
            if c == "\\" and quote == '"':
                j += 2
                continue
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "\\":
            j += 2
            continue
        elif c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return len(s)


def tokenize(s: str, subs: list[str]) -> list:
    """Shell-ish tokens: Word or ("op", str). Command/process substitutions are
    appended to `subs` so the caller can analyse them as commands too."""
    toks: list = []
    cur: list[str] = []
    in_word = False
    dyn = False
    glb = False
    pending_heredocs: list[tuple[str, bool]] = []
    expect_heredoc_word: str | None = None
    i, n = 0, len(s)

    def end_word():
        nonlocal cur, in_word, dyn, glb, expect_heredoc_word
        if in_word:
            word = Word("".join(cur), dyn, glb)
            if expect_heredoc_word is not None:
                pending_heredocs.append((word.text, expect_heredoc_word == "<<-"))
                expect_heredoc_word = None
            toks.append(word)
        cur, in_word, dyn, glb = [], False, False, False

    def take_subst(j: int) -> int:
        nonlocal dyn, in_word
        end = _balanced(s, j)
        subs.append(s[j + 1:end - 1])
        dyn = True
        in_word = True
        cur.append("\x00")
        return end

    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                i += 2
                continue
            if i + 1 < n:
                cur.append(s[i + 1])
                in_word = True
            i += 2
            continue
        if c == "'":
            end = s.find("'", i + 1)
            end = n if end < 0 else end
            cur.append(s[i + 1:end])
            in_word = True
            i = end + 1
            continue
        if c == '"':
            j = i + 1
            in_word = True
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n:
                    cur.append(s[j + 1])
                    j += 2
                    continue
                if s[j] == "$" and j + 1 < n and s[j + 1] == "(":
                    j = take_subst(j + 1)
                    continue
                if s[j] == "`":
                    end = s.find("`", j + 1)
                    end = n if end < 0 else end
                    subs.append(s[j + 1:end])
                    dyn = True
                    cur.append("\x00")
                    j = end + 1
                    continue
                if s[j] == "$" and j + 1 < n and (s[j + 1].isalnum() or s[j + 1] in "_{@*#?$!-"):
                    dyn = True
                cur.append(s[j])
                j += 1
            i = j + 1
            continue
        if c == "$":
            if i + 1 < n and s[i + 1] == "(":
                if s.startswith("$((", i):
                    end = _balanced(s, i + 1)
                    cur.append(s[i:end])
                    in_word, dyn = True, True
                    i = end
                    continue
                i = take_subst(i + 1)
                continue
            if i + 1 < n and (s[i + 1].isalnum() or s[i + 1] in "_{@*#?$!-"):
                dyn = True
            cur.append(c)
            in_word = True
            i += 1
            continue
        if c == "`":
            end = s.find("`", i + 1)
            end = n if end < 0 else end
            subs.append(s[i + 1:end])
            cur.append("\x00")
            in_word, dyn = True, True
            i = end + 1
            continue
        if c in " \t":
            end_word()
            i += 1
            continue
        if c == "\n":
            end_word()
            toks.append(("op", ";"))
            i += 1
            # Drop heredoc bodies: their text is data, not commands.
            for delim, strip_tabs in pending_heredocs:
                while i < n:
                    eol = s.find("\n", i)
                    eol = n if eol < 0 else eol
                    line = s[i:eol]
                    i = eol + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
            pending_heredocs.clear()
            continue
        if c == "#" and not in_word:
            eol = s.find("\n", i)
            i = n if eol < 0 else eol
            continue
        if c in "<>" and i + 1 < n and s[i + 1] == "(":
            # process substitution <(cmd) / >(cmd)
            end_word()
            i = take_subst(i + 1)
            end_word()
            continue
        if c in ";&|<>()":
            # An all-digit word glued to a redirect is its fd number (2>err).
            if c in "<>" and in_word and "".join(cur).isdigit() and not dyn:
                cur, in_word = [], False
            end_word()
            for op in OPS:
                if s.startswith(op, i):
                    break
            if op in HEREDOCS:
                expect_heredoc_word = op
            toks.append(("op", op))
            i += len(op)
            continue
        if c in "*?[":
            glb = True
        cur.append(c)
        in_word = True
        i += 1
    end_word()
    return toks


def split_commands(toks: list) -> list[list]:
    cmds, cur = [], []
    for t in toks:
        if isinstance(t, tuple) and t[1] in SEPARATORS:
            if cur:
                cmds.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        cmds.append(cur)
    return cmds


# --- per-command analysis ------------------------------------------------------------

INTERPRETERS = re.compile(
    r"^(python[0-9.]*|pypy[0-9.]*|node|nodejs|deno|bun|ruby|php|lua|luajit|Rscript|"
    r"osascript|awk|gawk|mawk|nawk|tclsh|jshell|groovy|julia)$")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
WRAPPERS = {"command", "builtin", "nohup", "time", "exec", "noglob", "chronic",
            # shell keywords that precede a command in a compound statement
            "do", "then", "else", "elif", "if", "while", "until", "!", "{", "}"}
DEST_CMDS = {"cp", "mv", "install", "rsync", "ln"}


class Ctx:
    def __init__(self, cwd: str | None):
        self.cwd = cwd  # None = unknown (a cd we could not follow)
        self.stdin: str | None = None


def _abs(ctx: Ctx, text: str) -> str | None:
    if text.startswith("~"):
        text = os.path.expanduser(text)
    if os.path.isabs(text):
        return os.path.normpath(text)
    if ctx.cwd is None:
        return None
    return os.path.normpath(os.path.join(ctx.cwd, text))


def _expand(ctx: Ctx, w: Word, a: Analysis, origin: str) -> list[str]:
    """Absolute paths a word names, or [] (and a recorded miss) when it cannot
    be known statically."""
    if w.dynamic:
        a.miss(f"{origin}: target '{w.text.replace(chr(0), '$(...)')}' uses a variable "
               f"or command substitution")
        return []
    p = _abs(ctx, w.text)
    if p is None:
        a.miss(f"{origin}: relative target '{w.text}' after a cd this guard could not follow")
        return []
    if w.glob:
        hits = sorted(_glob.glob(p))
        return hits or [p]
    return [p]


def _files_under(path: str, cap: int = 5000) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(path):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for f in filenames:
            out.append(os.path.join(dirpath, f))
            if len(out) >= cap:
                return out
    return out


def _strip_prefix(argv: list[Word], a: Analysis) -> list[Word]:
    """Drop env assignments and transparent wrappers (sudo, env, timeout ...)."""
    i = 0
    while i < len(argv):
        t = argv[i].text
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
            continue
        if t in WRAPPERS:
            i += 1
            continue
        if t == "sudo":
            i += 1
            while i < len(argv) and argv[i].text.startswith("-"):
                i += 2 if argv[i].text in ("-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t") else 1
            continue
        if t == "env":
            i += 1
            while i < len(argv) and (argv[i].text.startswith("-")
                                     or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[i].text)):
                i += 2 if argv[i].text in ("-u", "-C", "-S") else 1
            continue
        if t in ("nice", "ionice", "stdbuf", "timeout", "flock"):
            i += 1
            while i < len(argv) and argv[i].text.startswith("-"):
                i += 2 if argv[i].text in ("-n", "-c", "-s", "-k", "-w") else 1
            if t in ("timeout", "flock") and i < len(argv):
                i += 1  # duration / lock file
            continue
        break
    return argv[i:]


def _nonopts(args: list[Word], with_value: set[str] = frozenset()) -> list[Word]:
    out, i, dashdash = [], 0, False
    while i < len(args):
        t = args[i].text
        if dashdash or not t.startswith("-") or t == "-":
            out.append(args[i])
        elif t == "--":
            dashdash = True
        elif t in with_value:
            i += 1
        i += 1
    return out


def _sed(ctx, args, a):
    inplace = False
    script_given = False
    files, i = [], 0
    while i < len(args):
        t = args[i].text
        if t in ("-e", "--expression", "-f", "--file"):
            script_given = True
            i += 2
            continue
        if t.startswith("--expression=") or t.startswith("--file="):
            script_given = True
        elif t == "--in-place" or t.startswith("--in-place=") or t.startswith("-i"):
            inplace = True
        elif t.startswith("-") and not t.startswith("--") and len(t) > 1:
            if "i" in t[1:] and not set(t[1:t.index("i") + 1]) & {"e", "f", "l"}:
                inplace = True
            if t.endswith("e") or t.endswith("f"):
                script_given = True
                i += 2
                continue
        elif t.startswith("-"):
            pass
        else:
            files.append(args[i])
        i += 1
    if not inplace:
        return
    if not script_given and files:
        files = files[1:]  # first operand is the script
    for w in files:
        for p in _expand(ctx, w, a, "sed -i"):
            a.add(p, True, "sed -i")


def _perl(ctx, args, a):
    inplace, script_given, files, i = False, False, [], 0
    while i < len(args):
        t = args[i].text
        if t.startswith("-") and not t.startswith("--") and len(t) > 1 and not files:
            body = t[1:]
            if "i" in body:
                inplace = True
            if "e" in body or "E" in body:
                script_given = True
                if body.endswith(("e", "E")):
                    i += 2
                    continue
            i += 1
            continue
        files.append(args[i])
        i += 1
    if not inplace:
        if script_given:
            a.miss("perl -e: an interpreter's file writes are invisible to this guard")
        return
    if not script_given and files:
        files = files[1:]
    for w in files:
        for p in _expand(ctx, w, a, "perl -i"):
            a.add(p, True, "perl -i")


# Options that consume a value, per command (#3581 review finding 2). A value
# mistaken for an operand moves the destination, so these lists matter.
_SHORT_VALUED = {"cp": "tS", "mv": "tS", "ln": "tS", "install": "tSmog", "rsync": "efTBM"}
_LONG_VALUED = {
    "cp": {"--target-directory", "--suffix"},
    "mv": {"--target-directory", "--suffix"},
    "ln": {"--target-directory", "--suffix"},
    "install": {"--target-directory", "--suffix", "--mode", "--owner", "--group",
                "--strip-program"},
    "rsync": {"--rsh", "--exclude", "--include", "--filter", "--exclude-from",
              "--include-from", "--files-from", "--log-file", "--log-file-format",
              "--rsync-path", "--chmod", "--chown", "--temp-dir", "--partial-dir",
              "--backup-dir", "--suffix", "--compare-dest", "--copy-dest", "--link-dest",
              "--port", "--password-file", "--out-format", "--max-size", "--min-size",
              "--timeout", "--contimeout", "--bwlimit", "--modify-window", "--max-delete",
              "--usermap", "--groupmap", "--info", "--debug", "--iconv", "--protocol",
              "--address", "--sockopts", "--outbuf", "--remote-option", "--skip-compress",
              "--block-size", "--checksum-choice", "--compress-choice", "--compress-level",
              "--write-batch", "--only-write-batch", "--read-batch", "--max-alloc"},
}


def _parse_dest_opts(name: str, args: list[Word]):
    """(flags, target_dir Word | None, operands) for cp/mv/ln/install/rsync.

    Handles clusters (`-rt DIR`), attached values (`-tDIR`, `--target-directory=DIR`)
    and `--`. rsync has no target-directory option (its -t is --times)."""
    short_valued = _SHORT_VALUED[name]
    long_valued = _LONG_VALUED[name]
    flags: set[str] = set()
    target: Word | None = None
    ops: list[Word] = []
    i, dashdash = 0, False
    while i < len(args):
        w = args[i]
        t = w.text
        if dashdash or not t.startswith("-") or t == "-":
            ops.append(w)
            i += 1
            continue
        if t == "--":
            dashdash = True
            i += 1
            continue
        if t.startswith("--"):
            opt, eq, val = t.partition("=")
            if opt in long_valued:
                if eq:
                    value = Word(val, w.dynamic, w.glob)
                    i += 1
                else:
                    value = args[i + 1] if i + 1 < len(args) else None
                    i += 2
                if opt == "--target-directory":
                    target = value
                continue
            flags.add(opt)
            i += 1
            continue
        body, j, consumed = t[1:], 0, 1
        while j < len(body):
            ch = body[j]
            if ch in short_valued:
                rest = body[j + 1:]
                if rest:
                    value = Word(rest, w.dynamic, w.glob)
                else:
                    value = args[i + 1] if i + 1 < len(args) else None
                    consumed = 2
                if ch == "t" and name != "rsync":
                    target = value
                break
            flags.add("-" + ch)
            j += 1
        i += consumed
    return flags, target, ops


def _spelling(ctx: Ctx, w: Word, p: str) -> str:
    """How the shell would hand path `p` (an expansion of word `w`) to the
    program -- what --parents / --relative replicate under the destination."""
    if w.glob:
        return p if os.path.isabs(os.path.expanduser(w.text)) else os.path.relpath(p, ctx.cwd)
    return os.path.expanduser(w.text) if w.text.startswith("~") else w.text


def _dest_cmd(ctx, name, args, a):
    """Destinations of cp / mv / install / rsync / ln.

    Forms resolved: DEST as a file or directory; -t/--target-directory (also
    in clusters and attached); -T/--no-target-directory; cp --parents and
    rsync -R/--relative (the source path as spelled is replicated under the
    destination; rsync's '/./' marker is honoured); install -D (destination is
    the file path, leading directories created). Anything that cannot be placed
    with certainty is reported as unresolved instead of being guessed (#3581
    review finding 2: `cp --parents src/x backup/` writes backup/src/x, which
    the first version resolved to backup/x)."""
    flags, target_dir, ops = _parse_dest_opts(name, args)
    if name == "install" and ({"-d", "--directory"} & flags):
        return  # creates directories only
    if name == "rsync" and "--files-from" in {t.text.partition("=")[0] for t in args}:
        a.miss("rsync --files-from: the file list is not resolved")
        return
    if target_dir is not None:
        sources, dest = ops, target_dir
    else:
        if len(ops) < 2:
            if ops:
                a.miss(f"{name}: could not identify a destination")
            return
        sources, dest = ops[:-1], ops[-1]
    if name == "rsync" and re.match(r"^[^/]*:", dest.text):
        return  # remote destination
    if name == "rsync" and any(re.match(r"^[^/]*:", s.text) for s in sources):
        a.miss("rsync from a remote source: the files written are not resolved")
        return
    dest_paths = _expand(ctx, dest, a, name)
    if not dest_paths:
        return
    if len(dest_paths) > 1:
        a.miss(f"{name}: destination '{dest.text}' is a glob with several matches")
        return
    dest_p = dest_paths[0]

    no_target = bool({"-T", "--no-target-directory"} & flags) and name != "rsync"
    parents = name == "cp" and "--parents" in flags
    relative = name == "rsync" and bool({"-R", "--relative"} & flags) \
        and "--no-relative" not in flags
    install_d = name == "install" and "-D" in flags
    if name == "rsync":
        recursive = bool({"-r", "-a", "--recursive", "--archive"} & flags)
    elif name == "cp":
        recursive = bool({"-r", "-R", "-a", "--recursive", "--archive"} & flags)
    else:
        recursive = name == "mv"

    if no_target and target_dir is not None:
        a.miss(f"{name}: both -t and -T given")
        return
    if (parents or relative) and no_target:
        a.miss(f"{name}: --parents/--relative with -T is not resolved")
        return
    if install_d and target_dir is None:
        if len(sources) > 1 or os.path.isdir(dest_p) or dest.text.endswith("/"):
            a.miss("install -D into a directory: the placement is not resolved with certainty")
            return
    if parents or relative:
        into_dir = True
    elif no_target or install_d and target_dir is None:
        into_dir = False
    else:
        into_dir = (target_dir is not None or os.path.isdir(dest_p)
                    or dest.text.endswith("/") or len(sources) > 1)
    remove_sources = name == "mv" or (name == "rsync" and "--remove-source-files" in flags)

    def base_for(src_word: Word, sp: str) -> str | None:
        if parents or relative:
            spelled = _spelling(ctx, src_word, sp)
            if relative and "/./" in spelled:
                spelled = spelled.split("/./", 1)[1]
            spelled = spelled.lstrip("/") if spelled.startswith("/") else spelled
            norm = os.path.normpath(spelled)
            if norm in (".", "") or norm.startswith("..") or "/../" in f"/{spelled}/":
                a.miss(f"{name} {'--parents' if parents else '-R'}: source "
                       f"'{src_word.text}' contains '..'; placement not resolved")
                return None
            return os.path.join(dest_p, norm)
        if name == "rsync" and os.path.isdir(sp):
            # rsync treats a directory source's destination as a directory;
            # a trailing slash copies the CONTENTS.
            return dest_p if src_word.text.endswith("/") else \
                os.path.join(dest_p, os.path.basename(sp.rstrip("/")))
        if into_dir:
            return os.path.join(dest_p, os.path.basename(sp.rstrip("/")))
        return dest_p

    for src in sources:
        for sp in _expand(ctx, src, a, f"{name} source"):
            base = base_for(src, sp)
            if base is None:
                continue
            if os.path.isdir(sp):
                if not recursive:
                    continue  # cp/rsync without -r skip a directory
                for f in _files_under(sp):
                    a.add(os.path.join(base, os.path.relpath(f, sp)), False, name)
                    if remove_sources:
                        a.add(f, False, f"{name} source")
                continue
            a.add(base, True, name)
            if remove_sources:
                a.add(sp, True, f"{name} source")


def _rm(ctx, name, args, a):
    texts = [w.text for w in args]
    recursive = any(t in ("-r", "-R", "--recursive") or
                    (t.startswith("-") and not t.startswith("--") and set(t[1:]) & {"r", "R"})
                    for t in texts)
    for w in _nonopts(args):
        for p in _expand(ctx, w, a, name):
            if os.path.isdir(p) and recursive:
                for f in _files_under(p):
                    a.add(f, False, f"{name} -r")
            else:
                a.add(p, True, name)


def _truncate(ctx, args, a):
    for w in _nonopts(args, {"-s", "--size", "-r", "--reference"}):
        for p in _expand(ctx, w, a, "truncate"):
            a.add(p, True, "truncate")


def _tee(ctx, args, a):
    for w in _nonopts(args):
        for p in _expand(ctx, w, a, "tee"):
            a.add(p, True, "tee")


# --- git ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: str) -> list[str] | None:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                             timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.split("\0") if p]


def _toplevel(cwd: str) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _is_commit(ref: str, cwd: str) -> bool:
    return _run_git(["rev-parse", "--verify", "--quiet", "-z", f"{ref}^{{commit}}"], cwd) is not None


def _top_paths(top: str, rels: list[str], a: Analysis, origin: str) -> None:
    for r in rels:
        a.add(os.path.join(top, r), False, origin)


def _diff(cwd: str, top: str, spec: list[str], a: Analysis, origin: str) -> bool:
    rels = _run_git(["diff", "--name-only", "--no-renames", "-z", *spec], cwd)
    if rels is None:
        return False
    _top_paths(top, rels, a, origin)
    return True


def _working_changes(cwd, top, a, origin):
    _diff(cwd, top, ["HEAD"], a, origin)


def _pathspecs(cwd: str, specs: list[Word], rev: str | None, a: Analysis, origin: str,
               direct_if_literal: bool = True) -> None:
    """Files a pathspec names in the index (and in `rev`), relative to cwd."""
    for w in specs:
        if w.dynamic:
            a.miss(f"{origin}: pathspec '{w.text}' uses a variable or substitution")
            continue
        found = set(_run_git(["ls-files", "-z", "--", w.text], cwd) or [])
        if rev:
            found.update(_run_git(["ls-tree", "-r", "--name-only", "-z", rev, "--", w.text], cwd) or [])
        if found:
            literal = len(found) == 1 and os.path.normpath(next(iter(found))) == os.path.normpath(w.text)
            for f in found:
                a.add(os.path.join(cwd, f), direct_if_literal and literal, origin)
        else:
            a.add(os.path.join(cwd, w.text), direct_if_literal, origin)


def _git_apply_paths(cwd: str, top: str | None, patch_args: list[str], patch: str,
                     a: Analysis, origin: str) -> None:
    try:
        with open(patch, "rb") as fh:
            data = fh.read()
    except OSError:
        a.miss(f"{origin}: cannot read patch '{patch}'")
        return
    try:
        out = subprocess.run(["git", "apply", "--numstat", "-z", *patch_args],
                             cwd=cwd, input=data, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        a.miss(f"{origin}: `git apply --numstat` failed on '{patch}'")
        return
    if out.returncode != 0:
        a.miss(f"{origin}: `git apply --numstat` rejected '{patch}' "
               f"({out.stderr.decode(errors='replace').strip()[:120]})")
        return
    base = top or cwd
    fields = out.stdout.decode(errors="replace").split("\0")
    k = 0
    while k < len(fields):
        entry = fields[k]
        if not entry:
            k += 1
            continue
        parts = entry.split("\t")
        if len(parts) >= 3 and parts[2]:
            a.add(os.path.join(base, parts[2]), False, origin)
            k += 1
        elif len(parts) >= 3:  # rename/copy: '' then src, dst
            for p in fields[k + 1:k + 3]:
                if p:
                    a.add(os.path.join(base, p), False, origin)
            k += 3
        else:
            k += 1


def _commit_paths(cwd, top, commits, a, origin, mainline=None):
    for c in commits:
        if ".." in c:
            revs = _run_git(["rev-list", "-z", c], cwd)
            if revs is None:
                a.miss(f"{origin}: cannot resolve range '{c}'")
                continue
            revs = [r.strip() for r in revs if r.strip()]
        else:
            revs = [c]
        for r in revs:
            spec = [f"{r}^{mainline}", r] if mainline else [f"{r}^", r]
            if not _diff(cwd, top, spec, a, origin):
                # root commit or bad ref
                rels = _run_git(["diff-tree", "-r", "--root", "--no-commit-id", "--name-only",
                                 "--no-renames", "-z", r], cwd)
                if rels is None:
                    a.miss(f"{origin}: cannot resolve commit '{r}'")
                else:
                    _top_paths(top, rels, a, origin)


GIT_GLOBAL_WITH_VALUE = {"-c", "--namespace", "--exec-path", "--config-env"}


def _git(ctx: Ctx, args: list[Word], a: Analysis) -> None:
    cwd = ctx.cwd
    i = 0
    while i < len(args) and args[i].text.startswith("-"):
        t = args[i].text
        if t == "-C" and i + 1 < len(args):
            w = args[i + 1]
            if w.dynamic or cwd is None and not os.path.isabs(w.text):
                a.miss("git -C with a directory this guard cannot resolve")
                return
            cwd = os.path.normpath(os.path.join(cwd or "/", os.path.expanduser(w.text)))
            i += 2
            continue
        if t.startswith("--git-dir") or t.startswith("--work-tree"):
            a.miss("git --git-dir/--work-tree: target tree not resolved")
            return
        i += 2 if t in GIT_GLOBAL_WITH_VALUE else 1
    if i >= len(args) or cwd is None:
        return
    sub, rest = args[i].text, args[i + 1:]
    texts = [w.text for w in rest]
    origin = f"git {sub}"
    writers = {"apply", "am", "checkout", "switch", "restore", "merge", "cherry-pick", "revert",
               "rebase", "pull", "stash", "reset", "rm", "mv", "clean"}
    if sub not in writers:
        return
    top = _toplevel(cwd) if os.path.isdir(cwd) else None
    if top is None and sub != "apply":
        return  # not in a repository: nothing governed to protect

    def control_flags() -> bool:
        if "--abort" in texts:
            _working_changes(cwd, top, a, f"{origin} --abort")
            if sub in ("rebase", "am"):
                a.miss(f"{origin} --abort: restoring the pre-{sub} branch is not resolved")
            return True
        for flag in ("--continue", "--skip", "--quit", "--edit-todo"):
            if flag in texts:
                if sub in ("rebase", "cherry-pick", "revert", "am") and flag != "--quit":
                    a.miss(f"{origin} {flag}: commits still to be applied are not resolved")
                return True
        return False

    if sub in ("apply", "am"):
        if sub == "apply" and any(t in ("--stat", "--numstat", "--summary", "--check")
                                  for t in texts) and "--apply" not in texts:
            return
        if sub == "am" and control_flags():
            return
        passthru, patches, k = [], [], 0
        while k < len(rest):
            t = rest[k].text
            if t.startswith("-p") or t.startswith("--directory=") or t.startswith("--include=") \
                    or t.startswith("--exclude="):
                passthru.append(t)
            elif t in ("--directory", "--include", "--exclude", "-p") and k + 1 < len(rest):
                passthru.extend([t, rest[k + 1].text])
                k += 1
            elif t in ("-C", "--whitespace", "--build-fake-ancestor", "-S", "--patch-format"):
                k += 1
            elif t == "-":
                patches.append(None)
            elif not t.startswith("-"):
                patches.append(rest[k])
            k += 1
        if sub == "am":
            passthru = [p for p in passthru if p.startswith("-p") or p.startswith("--directory")]
        if not patches:
            patches = [None]
        for w in patches:
            if w is None:
                if ctx.stdin:
                    _git_apply_paths(cwd, top, passthru, ctx.stdin, a, origin)
                else:
                    a.miss(f"{origin}: patch read from a pipe/stdin is not resolved")
                continue
            for p in _expand(Ctx(cwd), w, a, origin):
                _git_apply_paths(cwd, top, passthru, p, a, origin)
        return

    if sub in ("checkout", "switch"):
        if "--" in texts:
            k = texts.index("--")
            before = [w for w in rest[:k] if not w.text.startswith("-")]
            rev = before[0].text if before else None
            _pathspecs(cwd, rest[k + 1:], rev, a, f"git {sub} -- paths")
            return
        newb = None
        ops, k = [], 0
        while k < len(rest):
            t = rest[k].text
            if t in ("-b", "-B", "-c", "-C", "--orphan"):
                newb = rest[k + 1].text if k + 1 < len(rest) else ""
                k += 2
                continue
            if t in ("--conflict", "--pathspec-from-file"):
                k += 2
                continue
            if not t.startswith("-"):
                ops.append(rest[k])
            k += 1
        if newb is not None:
            if ops and _is_commit(ops[0].text, cwd):
                _diff(cwd, top, ["HEAD", ops[0].text], a, f"git {sub} {ops[0].text}")
            return
        if not ops:
            return
        if _is_commit(ops[0].text, cwd):
            if len(ops) == 1:
                _diff(cwd, top, ["HEAD", ops[0].text], a, f"git {sub} {ops[0].text}")
            else:
                _pathspecs(cwd, ops[1:], ops[0].text, a, f"git {sub} <rev> paths")
            return
        if sub == "checkout":
            _pathspecs(cwd, ops, None, a, "git checkout paths")
        else:
            a.miss(f"git switch {ops[0].text}: not a resolvable commit")
        return

    if sub == "restore":
        source, specs, k = None, [], 0
        while k < len(rest):
            t = rest[k].text
            if t in ("-s", "--source") and k + 1 < len(rest):
                source = rest[k + 1].text
                k += 2
                continue
            if t.startswith("--source="):
                source = t.split("=", 1)[1]
            elif t == "--pathspec-from-file" or t.startswith("--pathspec-from-file="):
                a.miss("git restore --pathspec-from-file is not resolved")
            elif t == "--" or t.startswith("-"):
                pass
            else:
                specs.append(rest[k])
            k += 1
        if ("--staged" in texts or "-S" in texts) and not ("--worktree" in texts or "-W" in texts):
            return  # index only; the commit guard sees the result
        _pathspecs(cwd, specs, source, a, "git restore")
        return

    if sub in ("rm", "mv"):
        if "--cached" in texts:
            return
        ops = _nonopts(rest)
        if sub == "mv" and len(ops) >= 2:
            _pathspecs(cwd, ops[:-1], None, a, "git mv source")
            dest = ops[-1]
            for p in _expand(Ctx(cwd), dest, a, "git mv"):
                if os.path.isdir(p):
                    for s in ops[:-1]:
                        a.add(os.path.join(p, os.path.basename(s.text)), True, "git mv")
                else:
                    a.add(p, True, "git mv")
            return
        _pathspecs(cwd, ops, None, a, f"git {sub}")
        return

    if sub == "clean":
        if any(t in ("-n", "--dry-run") for t in texts):
            return
        keep = [t for t in texts if t not in ("-f", "--force", "-i", "--interactive")
                and not re.match(r"^-[fdxXq]+$", t)]
        flags = [t for t in texts if re.match(r"^-[fdxXq]+$", t)]
        flags = ["-" + "".join(c for f in flags for c in f[1:] if c in "dxX")] if flags else []
        flags = [f for f in flags if f != "-"]
        lines = None
        try:
            out = subprocess.run(["git", "clean", "-n", *flags, *keep], cwd=cwd,
                                 capture_output=True, text=True, timeout=20)
            if out.returncode == 0:
                lines = out.stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            pass
        if lines is None:
            a.miss("git clean: dry run failed; removals not resolved")
            return
        for line in lines:
            if line.startswith("Would remove "):
                a.add(os.path.join(cwd, line[len("Would remove "):]), False, "git clean")
        return

    if sub == "stash":
        verb = rest[0].text if rest and not rest[0].text.startswith("-") else "push"
        if verb in ("list", "show", "drop", "clear", "create", "store"):
            return
        if verb in ("pop", "apply"):
            refs = [w.text for w in rest[1:] if not w.text.startswith("-")]
            ref = refs[0] if refs else "stash@{0}"
            if refs and refs[0].isdigit():
                ref = f"stash@{{{refs[0]}}}"
            if not _diff(cwd, top, [f"{ref}^1", ref], a, f"git stash {verb}"):
                a.miss(f"git stash {verb}: cannot resolve '{ref}'")
            untracked = _run_git(["ls-tree", "-r", "--name-only", "-z", f"{ref}^3"], cwd)
            if untracked:
                _top_paths(top, untracked, a, f"git stash {verb} (untracked)")
            return
        if verb in ("push", "save"):
            _working_changes(cwd, top, a, "git stash")
            if any(t in ("-u", "--include-untracked", "-a", "--all") for t in texts):
                others = _run_git(["ls-files", "--others", "--exclude-standard", "-z"], top)
                _top_paths(top, others or [], a, "git stash -u")
            return
        a.miss(f"git stash {verb}: not resolved")
        return

    if sub == "reset":
        mode = next((t for t in texts if t in ("--hard", "--merge", "--keep")), None)
        if mode is None:
            return  # soft/mixed: index only
        refs = [t for t in texts if not t.startswith("-")]
        _working_changes(cwd, top, a, f"git reset {mode}")
        if refs:
            if not _diff(cwd, top, ["HEAD", refs[0]], a, f"git reset {mode} {refs[0]}"):
                a.miss(f"git reset {mode}: cannot resolve '{refs[0]}'")
        return

    if control_flags():
        return

    with_value = {"merge": {"-m", "-s", "-X", "-F", "--strategy", "--strategy-option", "--file",
                            "--into-name", "--cleanup", "-S"},
                  "cherry-pick": {"-m", "--mainline", "-X", "--strategy", "--strategy-option",
                                  "--cleanup", "-S"},
                  "revert": {"-m", "--mainline", "-X", "--strategy", "--strategy-option",
                             "--cleanup", "-S"},
                  "rebase": {"--onto", "-x", "--exec", "-s", "-X", "--strategy",
                             "--strategy-option", "-C", "--whitespace", "-S"},
                  "pull": {"-s", "-X", "--strategy", "--strategy-option", "--depth",
                           "--upload-pack", "-j", "--jobs", "--recurse-submodules", "-S"},
                  }[sub]
    ops, onto, mainline, k = [], None, None, 0
    while k < len(rest):
        t = rest[k].text
        if t in with_value:
            if t == "--onto" and k + 1 < len(rest):
                onto = rest[k + 1].text
            if t in ("-m", "--mainline") and sub in ("cherry-pick", "revert") and k + 1 < len(rest):
                mainline = rest[k + 1].text
            k += 2
            continue
        if t.startswith("--onto="):
            onto = t.split("=", 1)[1]
        elif not t.startswith("-"):
            ops.append(rest[k].text)
        k += 1

    if sub in ("cherry-pick", "revert"):
        if not ops:
            a.miss(f"git {sub}: no commit named")
        _commit_paths(cwd, top, ops, a, f"git {sub}", mainline)
        return

    refs: list[str]
    if sub == "merge":
        refs = ops or ["@{u}"]
    elif sub == "rebase":
        refs = [onto] if onto else (ops[:1] or ["@{u}"])
        if len(ops) >= 2 and _is_commit(ops[-1], cwd):
            _diff(cwd, top, ["HEAD", ops[-1]], a, "git rebase <branch> (checkout)")
    else:  # pull
        if len(ops) >= 2:
            refs = [f"{ops[0]}/{r.split(':')[0]}" for r in ops[1:]]
        elif len(ops) == 1:
            refs = ["@{u}"]
        else:
            refs = ["@{u}"]
        a.miss("git pull: commits fetched by this pull beyond the local tracking ref "
               "are not visible before it runs")
    for ref in refs:
        if not _is_commit(ref, cwd):
            a.miss(f"{origin}: cannot resolve '{ref}' to diff against")
            continue
        _diff(cwd, top, [f"HEAD...{ref}"], a, f"{origin} {ref}")


# --- driver ------------------------------------------------------------------------

def analyze(command: str, cwd: str | None, _depth: int = 0) -> Analysis:
    a = Analysis()
    subs: list[str] = []
    toks = tokenize(command, subs)
    ctx = Ctx(cwd)
    for cmd in split_commands(toks):
        ctx.stdin = None
        argv: list[Word] = []
        k = 0
        while k < len(cmd):
            t = cmd[k]
            if isinstance(t, tuple):
                op = t[1]
                nxt = cmd[k + 1] if k + 1 < len(cmd) and isinstance(cmd[k + 1], Word) else None
                if op in WRITE_REDIRS and nxt is not None:
                    for p in _expand(ctx, nxt, a, f"redirect {op}"):
                        a.add(p, True, f"redirect {op}")
                    k += 2
                    continue
                if op == ">&" and nxt is not None:
                    if not (nxt.text.isdigit() or nxt.text == "-"):
                        for p in _expand(ctx, nxt, a, "redirect >&"):
                            a.add(p, True, "redirect >&")
                    k += 2
                    continue
                if op in READ_REDIRS and nxt is not None:
                    if op == "<" and not nxt.dynamic:
                        ctx.stdin = _abs(ctx, nxt.text)
                    k += 2
                    continue
                if op in HEREDOCS:
                    k += 2
                    continue
                k += 1
                continue
            argv.append(t)
            k += 1
        argv = _strip_prefix(argv, a)
        if not argv:
            continue
        name = os.path.basename(argv[0].text)
        args = argv[1:]
        if argv[0].dynamic:
            a.miss("a command name built from a variable or substitution")
        elif name in ("cd", "pushd"):
            target = next((w for w in args if not w.text.startswith("-")), None)
            if target is None:
                ctx.cwd = os.path.expanduser("~")
            elif target.dynamic or target.text == "-":
                ctx.cwd = None
            else:
                ctx.cwd = _abs(ctx, target.text)
        elif name == "sed":
            _sed(ctx, args, a)
        elif name == "perl":
            _perl(ctx, args, a)
        elif name == "tee":
            _tee(ctx, args, a)
        elif name in DEST_CMDS:
            _dest_cmd(ctx, name, args, a)
        elif name == "truncate":
            _truncate(ctx, args, a)
        elif name in ("rm", "unlink", "shred"):
            _rm(ctx, name, args, a)
        elif name == "git":
            _git(ctx, args, a)
        elif name in SHELLS:
            if any(w.text == "-c" or (w.text.startswith("-") and "c" in w.text[1:]
                                      and not w.text.startswith("--")) for w in args):
                a.miss(f"{name} -c: the inner command is not analysed")
            elif args:
                a.miss(f"{name} script: a script's writes are invisible to this guard")
        elif name in ("eval", "source", "."):
            a.miss(f"{name}: not analysed")
        elif name == "xargs":
            a.miss("xargs: arguments come from stdin and are not resolved")
        elif name == "find" and any(w.text in ("-exec", "-execdir", "-delete", "-ok", "-okdir",
                                              "-fprint", "-fprintf", "-fls") for w in args):
            a.miss("find -exec/-delete: targets are not resolved")
        elif INTERPRETERS.match(name):
            a.miss(f"{name}: an interpreter's file writes are invisible to this guard")
    if _depth < 3:
        for sub in subs:
            inner = analyze(sub, cwd, _depth + 1)
            a.targets.extend(inner.targets)
            for m in inner.unresolved:
                a.miss(m)
    return a


# --- verdict -------------------------------------------------------------------------

def _repo_for(path: str, cache: dict) -> tuple[str, str] | None:
    from ai_team_sync.hooks import commit_guard as cg

    d = os.path.dirname(path) if not os.path.isdir(path) else path
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    if d not in cache:
        cache[d] = cg.repo_roots(d)
    return cache[d]


def group_targets(targets: list[Target]) -> dict[str, list[tuple[str, Target]]]:
    """canonical repo root -> [(repo-relative path, target)]; targets outside any
    git work tree, or inside .git/, are dropped."""
    cache: dict = {}
    groups: dict[str, list[tuple[str, Target]]] = {}
    for t in targets:
        if t.path.startswith(("/dev/", "/proc/", "/sys/")):
            continue
        roots = _repo_for(t.path, cache)
        if roots is None:
            continue
        top, root = roots
        real = os.path.realpath(t.path)
        rel = os.path.relpath(real, top)
        if rel == "." or rel.startswith("..") or rel == ".git" or rel.startswith(".git/"):
            continue
        groups.setdefault(root, [])
        if all(r != rel for r, _ in groups[root]):
            groups[root].append((rel, t))
    return groups


def _fetch_sessions_and_locks() -> tuple[list, list]:
    import httpx

    from ai_team_sync.hooks import commit_guard as cg

    with httpx.Client(timeout=3) as client:
        s = client.get(f"{cg.server_url()}/api/sessions")
        s.raise_for_status()
        lk = client.get(f"{cg.server_url()}/api/locks")
        lk.raise_for_status()
    sessions, locks = s.json(), lk.json()
    sessions = sessions if isinstance(sessions, list) else sessions.get("sessions", [])
    locks = locks if isinstance(locks, list) else locks.get("locks", [])
    return sessions, locks


@dataclass
class Decision:
    code: int = 0
    blocks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)   # lock warnings -> agent context
    residual: list[str] = field(default_factory=list)   # unresolved constructs -> stderr


def decide(command: str, cwd: str | None, claude_sid: str) -> Decision:
    """The verdict for one Bash command."""
    from ai_team_sync.hooks import commit_guard as cg
    from ai_team_sync.hooks.pre_tool_use_lockcheck import claim_check
    from ai_team_sync.operator_config import OperatorConfigError

    analysis = analyze(command, cwd)
    result = Decision(residual=[f"ATS BASH GUARD: not checked -- {m}"
                                for m in analysis.unresolved])
    warnings = result.warnings
    groups = group_targets(analysis.targets)
    if not groups:
        return result

    # Same resolution as the git hooks (#3581 review finding 1): this Claude
    # session's pointer wins; a contradicting $ATS_SESSION_ID is refused; an
    # env-only identity is trusted only once ATS confirms it (identity_problem).
    ident = cg.resolve_identity(claude_sid or None)
    if ident.conflict:
        result.code = 2
        result.blocks.append(f"ATS BASH GUARD: {ident.conflict}")
        return result
    my_sid = ident.session_id
    problem: str | None = None  # None = not yet validated
    cid8 = (claude_sid or "")[:8]
    blocks: list[str] = []
    claim_ctx: tuple[list, list] | None = None
    block_locks = os.environ.get("ATS_LOCKCHECK_BLOCK", "2") != "0"
    block_claims = os.environ.get("ATS_CLAIMCHECK", "1") != "0"

    for root, items in sorted(groups.items()):
        try:
            is_governed = cg.governed(root)
        except OperatorConfigError as exc:
            result.code = 2
            result.blocks.append(f"ATS BASH GUARD: operator config is invalid: {exc}")
            return result
        rels = [r for r, _ in items]
        try:
            results = cg.check_paths(rels, root, my_sid, timeout=3)
            if my_sid and problem is None:
                problem = cg.identity_problem(ident, results, timeout=3)
            if my_sid and problem:
                if ident.self_asserted and is_governed:
                    blocks.append(
                        f"ATS BASH GUARD: the acting identity came from $ATS_SESSION_ID "
                        f"(no pointer for this Claude session) and is not valid: {problem}. "
                        f"Refusing writes in the governed repository {root}.")
                    continue
                results = cg.check_paths(rels, root, None, timeout=3)
        except cg.GuardUnavailable as exc:
            if is_governed:
                blocks.append(
                    f"ATS BASH GUARD: this command writes {len(rels)} path(s) in the governed "
                    f"repository {root} (e.g. '{rels[0]}') and ATS at {cg.server_url()} could "
                    f"not be asked ({exc}). Failing closed.")
            continue
        eff_sid = None if problem else my_sid
        holders = cg.foreign_holders(results, eff_sid, cid8)
        by_path = {}
        for h in holders:
            by_path.setdefault(h.path, []).append(h)
        origin = {r: t.origin for r, t in items}
        for rel, hs in sorted(by_path.items()):
            hard = [h for h in hs if is_governed or h.mode == "exclusive"]
            soft = [h for h in hs if h not in hard]
            for h in hard:
                msg = (f"ATS BASH GUARD: `{origin.get(rel, 'write')}` would change '{rel}' in "
                       f"{root}, covered by another session's LIVE {h.mode.upper()} lock "
                       f"'{h.pattern}' held by {h.agent} (session {h.session_id})"
                       + (f' -- "{h.reason}"' if h.reason else "")
                       + ". Coordinate with the holder or request an override.")
                (blocks if block_locks else warnings).append(msg)
            for h in soft:
                warnings.append(f"ATS BASH GUARD: '{rel}' is covered by another session's LIVE "
                                f"ADVISORY lock '{h.pattern}' held by {h.agent}.")
        if is_governed:
            direct = [r for r, t in items if t.direct]
            if direct:
                if claim_ctx is None:
                    try:
                        claim_ctx = _fetch_sessions_and_locks()
                    except Exception as exc:
                        blocks.append(f"ATS BASH GUARD: cannot read ATS sessions/locks to "
                                      f"check your claim on '{direct[0]}' ({exc}). Failing closed.")
                        continue
                sessions, locks = claim_ctx
                for rel in direct:
                    ok, reason = claim_check(rel, root, eff_sid, cid8, sessions, locks)
                    if not ok:
                        (blocks if block_claims else warnings).append(
                            f"ATS CLAIM GUARD (bash `{origin.get(rel)}`): {reason}")
    result.blocks = blocks
    result.code = 2 if blocks else 0
    return result


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if payload.get("tool_name") != "Bash":
        sys.exit(0)
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not command.strip():
        sys.exit(0)
    cwd = payload.get("cwd") or os.getcwd()
    try:
        d = decide(command, cwd, str(payload.get("session_id") or ""))
    except Exception as exc:  # a parser bug must not wedge every shell command
        print(f"ATS BASH GUARD: internal error, command NOT checked: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(0)
    for line in d.residual + d.warnings:
        print(line, file=sys.stderr)
    if d.code:
        print("\n".join(d.blocks), file=sys.stderr)
        sys.exit(d.code)
    if d.warnings:
        # Lock warnings reach the agent the way the edit guard's do. Residual
        # notes stay on stderr: every `python ...` would otherwise add context.
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "\n".join(d.warnings),
        }}))
    sys.exit(0)


if __name__ == "__main__":
    main()
