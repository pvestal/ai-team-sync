"""Run a reviewed commit's tests in the kernel sandbox, for a reviewer that cannot (#3656).

WHY THIS EXISTS
---------------
A Codex VERIFY child runs under `codex exec --sandbox read-only` (launch_spec.py).
Measured on #3559 round 4 and on #3651: that sandbox denies socket() for every
family and makes /var/tmp read-only, so `pytest-sandbox` (bwrap, private netns,
private PostgreSQL) cannot start inside it, and no wrapper change can fix that.
Every database-backed review therefore rested on the implementer's own test
numbers. Loosening the reviewer's sandbox is the rejected path (#3559 option B):
Codex itself would then reach production localhost services.

So the tests run OUTSIDE the reviewer, owned by ATS: before the child launches,
ATS checks out the exact commit under review into a throwaway worktree, runs the
declared pytest selection inside the existing kernel sandbox, and hands the raw
log, exit status and a manifest to the reviewer in its packet and to the
delegation record. The reviewer gets no network, no credentials and no new tool.

WHAT IS TRUSTED, AND WHAT IS NOT
--------------------------------
The commit under review is untrusted input. Everything it contains runs INSIDE
the sandbox. The only code that runs on the host with the caller's full
privileges is the sandbox launcher itself, so that launcher is never taken from
the reviewed tree: it comes from the sandbox repository's trusted ref (normally
`main`, already reviewed and integrated). In `sandbox` mode (the reviewed repo
owns the sandbox) the whole runner directory is overwritten from the trusted ref
inside the throwaway tree, and the manifest says whether the reviewed commit
changes those files (if it does, that change was NOT used). In `hosted` mode (a
repo with no sandbox of its own, e.g. one whose documented runner is only
`unshare -rn`) the commit is run inside a throwaway checkout of the sandbox
repository at the trusted ref, with a private `<prefix>_*` test database, the
same arrangement that repository's own sandbox scripts already use.

`unshare -rn` alone is not a database boundary: a filesystem Unix socket is
reachable from any network namespace that can see its path, and the caller's
credential files stay readable. Measured on Tower 2026-11-15: under
`unshare -rn` the host Postgres socket CONNECTED and ~/.pgpass was readable;
inside pytest-sandbox the socket was ENOENT and ~/.pgpass absent.

EVERY RUN PROVES ITS OWN BOUNDARY
---------------------------------
The first thing executed inside the sandbox, before any test, is an isolation
probe (PROBE_PROGRAM). It checks, in that same namespace instance: the network
namespace holds only `lo`; every host Postgres socket path (discovered from the
host's /proc/net/unix plus configured ones) refuses a connect; configured
credential files are unreadable or empty; a canary directory on the host
filesystem cannot be written. It prints one marker line carrying a per-run nonce
and only then execs the tests. A failed probe exits before any test runs and the
whole run is refused. The probe is corroboration: the guarantee is the trusted
launcher's bwrap invocation, which the reviewed commit cannot change.

Integrity limit, stated rather than hidden: code under test runs in the same
sandbox as the probe and can print anything. A run whose log carries the nonce
marker more than once is refused as ambiguous; a deliberately adversarial commit
could still fake test output, which is no different from editing its own tests,
and the reviewer sees the diff.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import signal
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

#: Bump when the run arrangement, the probe or the evidence shape changes.
RUNNER_VERSION = "1"

PROBE_MARKER = "ATS-ISOLATION-PROBE"
#: Probe exit status when the boundary is not what it must be. Chosen outside
#: pytest's 0-5 and the sandbox's own 2/3 so it cannot be mistaken for either.
PROBE_FAILED_EXIT = 97

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PYTEST_SUMMARY = re.compile(
    r"\b\d+ (passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)\b")
_FAILED_LINE = re.compile(r"^(FAILED|ERROR) (\S+)")


class ReviewRunError(RuntimeError):
    """The review tests cannot be run, or not inside a proven boundary.

    Raised instead of producing evidence. A test result from an unproven
    boundary, or from a tree that is not the commit under review, would make a
    review look better evidenced than it is.
    """


# ---------------------------------------------------------------------------
# Configuration: machine-local, never in source (the paths are a workstation's)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RepoProfile:
    root: str
    mode: str                     # "sandbox" | "hosted"
    python: str = ""              # hosted: interpreter that runs the tests
    test_database: str = ""       # hosted: private database created in the sandbox
    dsn_env: str = ""             # hosted: env var the tests read that DSN from


@dataclass(frozen=True)
class RunnerConfig:
    sandbox_repo: str
    trusted_ref: str
    runner_dir: str
    entrypoint: str
    repos: dict
    work_dir: str
    log_dir: str
    nice: int = 10
    probe_python: str = "/usr/bin/python3"
    db_sockets: tuple = ()
    secret_paths: tuple = ()
    source: str = ""

    def profile_for(self, root: str) -> RepoProfile:
        profile = self.repos.get(os.path.realpath(root))
        if profile is None:
            raise ReviewRunError(
                f"no review-runner profile for repository {root} in {self.source}: "
                f"refusing rather than guessing how to sandbox it. Profiles: "
                f"{', '.join(sorted(self.repos)) or '(none)'}.")
        return profile


def config_path() -> Path:
    override = (os.environ.get("ATS_REVIEW_RUNNER_CONFIG") or "").strip()
    return Path(override) if override else (
        Path.home() / ".config" / "ai-team-sync" / "review_runner.toml")


def _abs(value, key: str, source: str, *, resolve: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewRunError(f"{source}: {key} must be a non-empty path")
    path = os.path.expanduser(value.strip())
    if not os.path.isabs(path):
        raise ReviewRunError(f"{source}: {key} must be absolute, got {value!r}")
    # An interpreter is NOT resolved: a venv's bin/python is a symlink to the
    # base interpreter, and running the target would drop the venv.
    return os.path.realpath(path) if resolve else os.path.normpath(path)


def _rel(value, key: str, source: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewRunError(f"{source}: {key} must be a non-empty relative path")
    rel = value.strip().strip("/")
    if os.path.isabs(value) or ".." in Path(rel).parts:
        raise ReviewRunError(f"{source}: {key} must stay inside the repository, got {value!r}")
    return rel


def load_config(path: Path | None = None) -> RunnerConfig:
    path = Path(path) if path else config_path()
    source = str(path)
    if not path.exists():
        raise ReviewRunError(
            f"review runner not configured: {source} does not exist. See "
            f"docs/delegation.md ('ATS-run review tests').")
    try:
        doc = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReviewRunError(f"cannot read {source}: {exc}") from exc

    sandbox = doc.get("sandbox")
    if not isinstance(sandbox, dict):
        raise ReviewRunError(f"{source}: missing [sandbox] table")
    runner_dir = _rel(sandbox.get("runner_dir"), "sandbox.runner_dir", source)
    entrypoint = _rel(sandbox.get("entrypoint"), "sandbox.entrypoint", source)
    if not entrypoint.startswith(runner_dir + "/"):
        raise ReviewRunError(
            f"{source}: sandbox.entrypoint must live inside sandbox.runner_dir, so "
            f"every file the launcher runs is taken from the trusted ref")
    trusted_ref = sandbox.get("trusted_ref")
    if not isinstance(trusted_ref, str) or not trusted_ref.strip() \
            or trusted_ref.strip().startswith("-"):
        raise ReviewRunError(f"{source}: sandbox.trusted_ref must be a ref name")
    sandbox_repo = _abs(sandbox.get("repo"), "sandbox.repo", source)

    repos: dict[str, RepoProfile] = {}
    table = doc.get("repos")
    if not isinstance(table, dict) or not table:
        raise ReviewRunError(f"{source}: no [repos.\"<root>\"] profiles")
    for raw_root, spec in table.items():
        root = _abs(raw_root, f"repos.{raw_root!r}", source)
        if not isinstance(spec, dict):
            raise ReviewRunError(f"{source}: repos.{raw_root!r} must be a table")
        mode = spec.get("mode")
        if mode == "sandbox":
            if root != sandbox_repo:
                raise ReviewRunError(
                    f"{source}: repos.{raw_root!r} mode 'sandbox' is only for the "
                    f"sandbox repository itself ({sandbox_repo}); use 'hosted'")
            repos[root] = RepoProfile(root=root, mode=mode)
        elif mode == "hosted":
            python = _abs(spec.get("python"), f"repos.{raw_root!r}.python", source,
                          resolve=False)
            db, env = spec.get("test_database", ""), spec.get("dsn_env", "")
            if not (isinstance(db, str) and _IDENT.match(db)):
                raise ReviewRunError(f"{source}: repos.{raw_root!r}.test_database "
                                     f"must be a plain identifier")
            if not (isinstance(env, str) and _IDENT.match(env)):
                raise ReviewRunError(f"{source}: repos.{raw_root!r}.dsn_env must be "
                                     f"an environment variable name")
            repos[root] = RepoProfile(root=root, mode=mode, python=python,
                                      test_database=db, dsn_env=env)
        else:
            raise ReviewRunError(f"{source}: repos.{raw_root!r}.mode must be "
                                 f"'sandbox' or 'hosted', got {mode!r}")

    # The probe's interpreter is an absolute HOST path, never a PATH lookup inside
    # the sandbox: the launcher puts the tested tree's venv/bin first on PATH,
    # and a reviewed commit can commit a venv/bin/python3 that prints a fake PASS.
    probe_python = _abs(doc.get("probe_python", "/usr/bin/python3"), "probe_python",
                        source, resolve=False)
    if not os.access(probe_python, os.X_OK):
        raise ReviewRunError(f"{source}: probe_python {probe_python} is not executable")

    nice = doc.get("nice", 10)
    if not isinstance(nice, int) or not 0 <= nice <= 19:
        raise ReviewRunError(f"{source}: nice must be an integer 0-19")

    def _paths(key):
        values = doc.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ReviewRunError(f"{source}: {key} must be an array of paths")
        return tuple(_abs(v, key, source) for v in values)

    # Not resolved: run_review_tests checks with lstat that this exact path is a
    # directory this user owns, and resolving here would hide a planted symlink.
    work_dir = _abs(doc.get("work_dir", "/var/tmp/ats-review"), "work_dir", source,
                    resolve=False)
    for root in [sandbox_repo, *repos]:
        if work_dir == root or Path(work_dir).is_relative_to(root):
            raise ReviewRunError(f"{source}: work_dir {work_dir} is inside {root}")

    return RunnerConfig(
        sandbox_repo=sandbox_repo, trusted_ref=trusted_ref.strip(),
        runner_dir=runner_dir, entrypoint=entrypoint, repos=repos,
        work_dir=work_dir,
        log_dir=_abs(doc.get("log_dir", "~/.local/state/ai-team-sync/review-runs"),
                     "log_dir", source),
        nice=nice, probe_python=probe_python, db_sockets=_paths("db_sockets"),
        secret_paths=_paths("secret_paths"), source=source)


# ---------------------------------------------------------------------------
# The isolation probe: runs first, inside the same sandbox as the tests
# ---------------------------------------------------------------------------

# Run as `<probe_python> -I -c PROBE_PROGRAM <nonce> <params-json> -- <command...>`,
# probe_python an absolute host interpreter (config, default /usr/bin/python3).
# -I keeps the reviewed tree (the sandbox's cwd) and PYTHON* variables off
# sys.path, so a planted socket.py or json.py cannot answer for the probe. It
# execs the command only when every check passed.
PROBE_PROGRAM = r'''
import errno, json, os, socket, sys
nonce, params = sys.argv[1], json.loads(sys.argv[2])
if sys.argv[3:4] != ["--"] or len(sys.argv) < 5:
    sys.exit(96)
command = sys.argv[4:]
checks = []

def check(name, ok, detail):
    checks.append({"check": name, "ok": bool(ok), "detail": detail})

def err(exc):
    return errno.errorcode.get(exc.errno, str(exc.errno)) if exc.errno else type(exc).__name__

try:
    with open("/proc/net/dev") as fh:
        ifaces = sorted(line.split(":")[0].strip() for line in fh.read().splitlines()[2:] if ":" in line)
    check("network namespace holds only loopback", ifaces == ["lo"], ifaces)
except OSError as exc:
    check("network namespace holds only loopback", False, "unreadable: " + err(exc))

for path in params["db_sockets"]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(path)
        check("host database socket unreachable", False, path + ": CONNECTED")
    except OSError as exc:
        check("host database socket unreachable", True, path + ": " + err(exc))
    finally:
        s.close()

for path in params["secret_paths"]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(1)
        check("credential file unreadable or masked", not data,
              path + (": READABLE" if data else ": masked (empty)"))
    except OSError as exc:
        check("credential file unreadable or masked", True, path + ": " + err(exc))

canary = os.path.join(params["canary"], "probe-" + nonce)
try:
    fd = os.open(canary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    check("host filesystem read-only", False, params["canary"] + ": WRITABLE")
except OSError as exc:
    check("host filesystem read-only", exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM),
          params["canary"] + ": " + err(exc))

check("tree under test visible", os.path.isdir(params["tree"]), params["tree"])

ok = all(c["ok"] for c in checks)
print("ATS-ISOLATION-PROBE " + nonce + " " + json.dumps({"ok": ok, "checks": checks}), flush=True)
if not ok:
    sys.exit(97)
os.execvp(command[0], command)
'''

# Hosted mode: what runs after the probe, inside the sandbox. Arguments, never
# interpolation: $1 tree, $2 interpreter, $3 database, $4 DSN variable name,
# then the pytest selection. Mirrors the hosted repo's own sandbox scripts.
HOSTED_SCRIPT = r'''set -eu
tree=$1 py=$2 db=$3 dsn_env=$4; shift 4
export PGPASSWORD="$DB_PASSWORD"
createdb -h 127.0.0.1 -U "$DB_USER" "$db"
cd "$tree"
export "$dsn_env=postgresql://$DB_USER:$DB_PASSWORD@127.0.0.1/$db"
export HOME=/tmp
unset PYTHONPATH
exec "$py" -m pytest "$@"
'''


def host_db_sockets(extra: Sequence[str] = ()) -> list[str]:
    """Postgres sockets the HOST is listening on, plus configured ones.

    Discovered rather than hard-coded so a moved socket directory is still
    checked; configured ones cover a caller already inside another netns, where
    /proc/net/unix would not list the host's sockets.
    """
    found: list[str] = []
    try:
        with open("/proc/net/unix") as fh:
            for line in fh.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 8 and parts[-1].startswith("/") \
                        and "/.s.PGSQL." in parts[-1]:
                    found.append(parts[-1])
    except OSError:
        pass
    out: list[str] = []
    for path in [*found, *extra]:
        if path not in out:
            out.append(path)
    return out


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

@dataclass
class ReviewRun:
    run_id: str
    repo: str                  # common root of the reviewed repository
    lead_tree: str             # the tree the caller named (what the reviewer reads)
    sha: str
    subject: str
    selection: list
    mode: str
    lead_head: str
    lead_dirty: list
    sandbox_repo: str
    trusted_ref: str
    trusted_sha: str
    entrypoint_blob: str
    runner_differs: list       # sandbox mode: runner files the reviewed commit changes
    argv: list
    exit_code: int | None = None
    timed_out: bool = False
    probe: dict = field(default_factory=dict)
    summary: str = ""
    failed_ids: list = field(default_factory=list)
    log_path: str = ""
    log_sha256: str = ""
    log_bytes: int = 0
    duration_s: float = 0.0
    tail: str = ""

    @property
    def tests_ran(self) -> bool:
        return bool(self.probe.get("ok"))

    def status(self) -> str:
        if self.timed_out:
            return "TIMED OUT (the selection did not finish; results are partial)"
        if not self.tests_ran:
            return (f"NOT RUN: the sandbox exited {self.exit_code} before the isolation "
                    f"probe (sandbox start or scratch database build failed; the log "
                    f"shows why, and a migration in the reviewed commit can be the cause)")
        return {0: "PASSED (pytest exit 0)", 1: "TESTS FAILED (pytest exit 1)",
                5: "NO TESTS COLLECTED (pytest exit 5)"}.get(
            self.exit_code, f"pytest exit {self.exit_code}")

    def evidence(self) -> dict:
        return {
            "runner_version": RUNNER_VERSION, "run_id": self.run_id,
            "repo": self.repo, "lead_tree": self.lead_tree, "sha": self.sha,
            "selection": self.selection, "mode": self.mode,
            "lead_head": self.lead_head, "lead_dirty_files": self.lead_dirty[:50],
            "boundary": {"launcher_repo": self.sandbox_repo,
                         "trusted_ref": self.trusted_ref,
                         "trusted_sha": self.trusted_sha,
                         "entrypoint_blob": self.entrypoint_blob,
                         "reviewed_runner_differs": self.runner_differs},
            "probe": self.probe, "exit_code": self.exit_code,
            "timed_out": self.timed_out, "status": self.status(),
            "summary": self.summary, "failed_ids": self.failed_ids[:200],
            "log_path": self.log_path, "log_sha256": self.log_sha256,
            "log_bytes": self.log_bytes, "duration_s": round(self.duration_s, 1),
        }


# ---------------------------------------------------------------------------
# Git on the host: nothing the reviewed commit carries may make it run anything
# ---------------------------------------------------------------------------
# Checkout is the one step that touches reviewed content on the host, before any
# sandbox exists. Git runs programs at checkout (and at `status`) only through
# filter drivers: the commit's .gitattributes selects a driver, configuration
# names its program. On Tower the git-lfs driver is configured system- and
# user-wide with required=true, and a committed .lfsconfig can point git-lfs at
# any URL, so a reviewed commit could make host-side git-lfs fetch from a server
# of its choosing (Codex VERIFY 26b13c62). Two locks, either one sufficient:
#   1. GIT_ATTR_SOURCE = the empty tree, and core.attributesFile=/dev/null: the
#      commit's own .gitattributes (and a global attributes file) are not read,
#      so nothing selects a driver. (git >= 2.42; older git ignores it.)
#   2. Every configured filter driver is blanked for these invocations, so a
#      driver selected from any remaining source ($GIT_DIR/info/attributes) has
#      no program to run. required=false keeps that from being an error.
# Hooks are off for the same reason, and the caller's GIT_* environment
# (GIT_DIR, GIT_WORK_TREE, GIT_CONFIG_*...) is dropped so it cannot redirect
# these commands to another repository or inject configuration.
# Consequence: an LFS-tracked file is checked out as its pointer text.

_SAFE_FLAGS: dict[str, list[str]] = {}
_EMPTY_TREE: dict[str, str] = {}


def _git_env(repo: str | Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if repo is not None:
        key = os.path.realpath(str(repo))
        if key not in _EMPTY_TREE:
            _EMPTY_TREE[key] = subprocess.run(
                ["git", "-C", key, "hash-object", "-t", "tree", "/dev/null"],
                capture_output=True, text=True, env=env).stdout.strip()
        if _EMPTY_TREE[key]:
            env["GIT_ATTR_SOURCE"] = _EMPTY_TREE[key]
    return env


def _safe_flags(repo: str | Path) -> list[str]:
    key = os.path.realpath(str(repo))
    if key in _SAFE_FLAGS:
        return _SAFE_FLAGS[key]
    proc = subprocess.run(["git", "-C", key, "config", "--name-only", "--get-regexp",
                           r"^filter\."], capture_output=True, text=True, env=_git_env())
    names = sorted({m.group(1) for m in (re.match(r"^filter\.(.+)\.[^.]+$", line)
                                         for line in proc.stdout.splitlines()) if m})
    flags = ["-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
             "-c", "core.fsmonitor=false"]
    for name in names:
        if not re.match(r"^[A-Za-z0-9_.-]+$", name):
            raise ReviewRunError(f"filter driver name {name!r} configured for {key} cannot "
                                 f"be neutralized safely; refusing")
        for field_ in ("smudge", "clean", "process"):
            flags += ["-c", f"filter.{name}.{field_}="]
        flags += ["-c", f"filter.{name}.required=false"]
    _SAFE_FLAGS[key] = flags
    return flags


def _git(repo: str | Path, *args: str, check: bool = True, text: bool = True):
    proc = subprocess.run(["git", "-C", str(repo), *_safe_flags(repo), *args],
                          capture_output=True, text=text, env=_git_env(repo))
    if check and proc.returncode != 0:
        err = proc.stderr if text else proc.stderr.decode("utf-8", "replace")
        raise ReviewRunError(f"git {' '.join(args)} failed in {repo}: {err.strip()}")
    return proc.stdout


def _commit(repo: str | Path, rev: str) -> str:
    rev = (rev or "").strip()
    if not rev or rev.startswith("-"):
        raise ReviewRunError(f"not a revision: {rev!r}")
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()


def common_root(repo: str | Path) -> str:
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    if not common:
        raise ReviewRunError(f"{repo} is not a git repository")
    return os.path.realpath(str(Path(common).parent))


def _add_worktree(repo: str, path: Path, sha: str) -> None:
    # _git's flags: no hook (with a relative core.hooksPath a post-checkout hook
    # would come from the commit being checked out) and no filter program.
    _git(repo, "worktree", "add", "--detach", "--quiet", str(path), sha)
    head = _git(path, "rev-parse", "HEAD").strip()
    if head != sha:
        raise ReviewRunError(f"worktree {path} is at {head}, not {sha}")


def _remove_worktree(repo: str, path: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        subprocess.run(["chmod", "-R", "u+rwX", str(path)], capture_output=True)
        shutil.rmtree(path, ignore_errors=True)
    _git(repo, "worktree", "prune", check=False)


def _install_trusted_runner(cfg: RunnerConfig, trusted_sha: str, tree: Path) -> None:
    """Overwrite the runner directory in `tree` with the trusted ref's blobs."""
    listing = _git(cfg.sandbox_repo, "ls-tree", "-r", trusted_sha, "--", cfg.runner_dir)
    entries = [line.split(None, 3) for line in listing.splitlines() if line.strip()]
    if not any(e[3] == cfg.entrypoint for e in entries if len(e) == 4):
        raise ReviewRunError(f"{cfg.entrypoint} is not in {cfg.sandbox_repo}@"
                             f"{cfg.trusted_ref}: no trusted launcher to run")
    root = tree.resolve()
    for mode, kind, blob, rel in entries:
        if kind != "blob":
            continue
        dst = tree / rel
        # The reviewed commit controls every path in this tree. A symlinked
        # directory or file on the way would turn this host-side write into a
        # write outside the throwaway tree, so any symlink on the path refuses.
        probe = tree
        for part in Path(rel).parts:
            probe = probe / part
            if probe.is_symlink():
                raise ReviewRunError(f"reviewed tree has a symlink at "
                                     f"{probe.relative_to(tree)}; refusing to install "
                                     f"the trusted runner through it")
        if dst.exists() and not dst.is_file():
            raise ReviewRunError(f"reviewed tree has a non-file at {rel}; refusing")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.parent.resolve().is_relative_to(root):
            raise ReviewRunError(f"{rel} resolves outside the throwaway tree; refusing")
        data = _git(cfg.sandbox_repo, "cat-file", "blob", blob, text=False)
        dst.write_bytes(data)
        dst.chmod(0o755 if mode == "100755" else 0o644)


def _parse_log(text: str, nonce: str) -> tuple[dict, str, list]:
    markers = [line for line in text.splitlines()
               if line.startswith(f"{PROBE_MARKER} {nonce} ")]
    probe: dict = {}
    if len(markers) > 1:
        raise ReviewRunError(
            f"the run's log carries the isolation-probe marker {len(markers)} times; "
            f"code inside the sandbox printed it. Refusing ambiguous evidence.")
    if markers:
        try:
            probe = json.loads(markers[0].split(" ", 2)[2])
        except (ValueError, IndexError) as exc:
            raise ReviewRunError(f"unparseable isolation-probe line: {exc}") from exc
    summary = ""
    failed: list[str] = []
    for line in text.splitlines():
        m = _FAILED_LINE.match(line)
        if m and m.group(2) not in failed:
            failed.append(m.group(2))
        if _PYTEST_SUMMARY.search(line) and " in " in line:
            summary = line.strip().strip("=").strip()
    return probe, summary, failed


def _tail(text: str, lines: int = 150, limit: int = 12000) -> str:
    out = "\n".join(text.splitlines()[-lines:])
    return out[-limit:]


def _minimal_env() -> dict:
    # The launcher clears the environment inside the sandbox, so this is only
    # what the host-side launcher sees. Fixed rather than inherited so that a
    # caller's SANDBOX_CLONE_DUMP, SANDBOX_PGLOG_OUT or PATH cannot change what
    # this run is.
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()),
            "USER": os.environ.get("USER", ""), "LANG": "C.UTF-8", "TERM": "dumb"}


def run_review_tests(lead_tree: str, rev: str, selection: Sequence[str], *,
                     config: RunnerConfig | None = None, timeout_s: int = 1800,
                     out: Callable[[str], None] = lambda _m: None,
                     run_id: str | None = None) -> ReviewRun:
    """Check out `rev` of `lead_tree`'s repository and run `selection` in the sandbox.

    Raises ReviewRunError when the run cannot be made trustworthy (no profile,
    unresolvable commit, worktree failure, failed or ambiguous isolation probe).
    A run that completed with failing tests, or whose sandbox could not build its
    scratch database, is RETURNED: that is review evidence, not an outage.
    """
    selection = [str(a) for a in selection]
    if not selection:
        raise ReviewRunError("an empty test selection: name the tests to run")
    cfg = config or load_config()
    repo = common_root(lead_tree)
    profile = cfg.profile_for(repo)
    sha = _commit(repo, rev)
    if not sha:
        raise ReviewRunError(f"{rev!r} does not name a commit in {repo}")
    trusted_sha = _commit(cfg.sandbox_repo, cfg.trusted_ref)
    if not trusted_sha:
        raise ReviewRunError(f"trusted ref {cfg.trusted_ref!r} does not resolve in "
                             f"{cfg.sandbox_repo}")
    entrypoint_blob = _git(cfg.sandbox_repo, "rev-parse",
                           f"{trusted_sha}:{cfg.entrypoint}").strip()
    subject = _git(repo, "log", "-1", "--format=%s", sha).strip()
    lead_head = _git(lead_tree, "rev-parse", "HEAD").strip()
    lead_dirty = [line[3:] for line in _git(
        lead_tree, "status", "--porcelain", "--untracked-files=no").splitlines() if line]

    run_id = run_id or secrets.token_hex(6)
    nonce = secrets.token_hex(16)
    base = Path(cfg.work_dir)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    # work_dir normally lives in a world-writable directory (/var/tmp), where
    # another account could have planted it as a symlink or a directory of its
    # own; checkouts would then land wherever that points.
    st = os.lstat(base)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise ReviewRunError(f"work_dir {base} is not a directory owned by this user; "
                             f"refusing to check anything out there")
    run_dir = base / run_id
    run_dir.mkdir(mode=0o700)
    tree = run_dir / "tree"
    canary = run_dir / "canary"
    canary.mkdir(mode=0o700)
    log_dir = Path(cfg.log_dir) / run_id
    log_dir.mkdir(mode=0o700, parents=True)
    log_path = log_dir / ("review-tests-" + sha[:12] + ".log")

    sandbox_tree: Path | None = None
    run = ReviewRun(
        run_id=run_id, repo=repo, lead_tree=os.path.realpath(lead_tree), sha=sha,
        subject=subject, selection=selection, mode=profile.mode, lead_head=lead_head,
        lead_dirty=lead_dirty, sandbox_repo=cfg.sandbox_repo,
        trusted_ref=cfg.trusted_ref, trusted_sha=trusted_sha,
        entrypoint_blob=entrypoint_blob, runner_differs=[], argv=[])
    try:
        _add_worktree(repo, tree, sha)
        if profile.mode == "sandbox":
            run.runner_differs = [p for p in _git(
                repo, "diff", "--name-only", trusted_sha, sha, "--",
                cfg.runner_dir).splitlines() if p]
            _install_trusted_runner(cfg, trusted_sha, tree)
            launcher_root = tree
        else:
            sandbox_tree = run_dir / "sandbox"
            _add_worktree(cfg.sandbox_repo, sandbox_tree, trusted_sha)
            launcher_root = sandbox_tree

        params = {"db_sockets": host_db_sockets(cfg.db_sockets),
                  "secret_paths": list(cfg.secret_paths),
                  "canary": str(canary), "tree": str(tree)}
        if profile.mode == "sandbox":
            command = ["python", "-m", "pytest", *selection]
        else:
            command = ["bash", "-c", HOSTED_SCRIPT, "bash", str(tree), profile.python,
                       profile.test_database, profile.dsn_env, *selection]
        run.argv = ["nice", "-n", str(cfg.nice), str(launcher_root / cfg.entrypoint),
                    "--exec", cfg.probe_python, "-I", "-c", PROBE_PROGRAM, nonce,
                    json.dumps(params), "--", *command]

        out(f"review tests: {profile.mode} run of {sha[:12]} ({' '.join(selection)}) "
            f"in {run_dir}; log {log_path}")
        started = time.monotonic()
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(run.argv, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=_minimal_env(),
                                    cwd=str(run_dir), start_new_session=True)
            try:
                run.exit_code = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                run.timed_out = True
                for sig, grace in ((signal.SIGTERM, 15), (signal.SIGKILL, 15)):
                    try:
                        os.killpg(proc.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        proc.wait(timeout=grace)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                run.exit_code = proc.returncode
        run.duration_s = time.monotonic() - started
    finally:
        if sandbox_tree is not None:
            _remove_worktree(cfg.sandbox_repo, sandbox_tree)
        _remove_worktree(repo, tree)
        shutil.rmtree(run_dir, ignore_errors=True)

    data = log_path.read_bytes() if log_path.exists() else b""
    text = data.decode("utf-8", "replace")
    run.log_path, run.log_bytes = str(log_path), len(data)
    run.log_sha256 = hashlib.sha256(data).hexdigest()
    run.tail = _tail(text)
    try:
        run.probe, run.summary, run.failed_ids = _parse_log(text, nonce)
        if run.probe and not run.probe.get("ok"):
            failed = [f"{c['check']}: {c['detail']}" for c in run.probe.get("checks", [])
                      if not c.get("ok")]
            raise ReviewRunError(
                "ISOLATION PROBE FAILED inside the sandbox; no test ran. "
                + "; ".join(failed) + f". Log: {log_path}")
        if run.probe and not run.probe.get("checks"):
            raise ReviewRunError(f"isolation probe reported no checks; log {log_path}")
    except ReviewRunError as exc:
        exc.run = run          # the log path and what was attempted, for the caller
        raise
    return run


# ---------------------------------------------------------------------------
# What the reviewer and the record receive
# ---------------------------------------------------------------------------

@dataclass
class ReviewTests:
    head: ReviewRun
    base: ReviewRun | None = None

    def comparison(self) -> dict:
        if self.base is None or not (self.head.tests_ran and self.base.tests_ran):
            return {}
        head, base = set(self.head.failed_ids), set(self.base.failed_ids)
        return {"new_failures": sorted(head - base), "fixed": sorted(base - head),
                "failing_in_both": sorted(head & base)}

    def evidence(self) -> dict:
        ev = {"head": self.head.evidence()}
        if self.base is not None:
            ev["base"] = self.base.evidence()
            ev["comparison"] = self.comparison()
        return ev

    def packet_text(self) -> str:
        lines = [
            "=" * 62,
            "ATS REVIEW TESTS — run by ATS before this review, NOT by the implementer",
            "and not by you (#3656). Your sandbox cannot run database tests; these",
            "were run on the exact commit below, in a fresh checkout, inside the",
            "kernel sandbox. Cite them as 'ATS review-tests run <id>'.",
            "=" * 62,
        ]
        for label, run in (("HEAD (under review)", self.head), ("BASE", self.base)):
            if run is None:
                continue
            lines += _render_run(label, run)
        comp = self.comparison()
        if comp:
            lines += ["", "HEAD vs BASE (same selection):",
                      f"  new failures   : {', '.join(comp['new_failures']) or '(none)'}",
                      f"  fixed          : {', '.join(comp['fixed']) or '(none)'}",
                      f"  failing in both: {len(comp['failing_in_both'])}"]
        lines += [
            "",
            "What this establishes: the selection's outcome at that commit inside a",
            "boundary with no route to production databases (probe above). What it",
            "does not: tests outside the selection, uncommitted lead-tree changes, or",
            "live-service behaviour. Name a further selection in your verdict if this",
            "one does not cover the change.",
            "=" * 62,
        ]
        return "\n".join(lines)


def _render_run(label: str, run: ReviewRun) -> list:
    probe = "; ".join(f"{c['check']} [{c['detail']}]" for c in run.probe.get("checks", []))
    head_note = ("MATCHES the tested commit" if run.lead_head == run.sha
                 else f"{run.lead_head[:12]} — DIFFERS from the tested commit")
    lines = [
        "", f"--- {label}: run {run.run_id} ---",
        f"repository        : {run.repo}",
        f"tested commit     : {run.sha}  ({run.subject})",
        f"lead tree         : {run.lead_tree}  HEAD {head_note}",
    ]
    if run.lead_dirty:
        lines.append(f"  !! lead tree has {len(run.lead_dirty)} uncommitted tracked change(s) "
                     f"NOT included in this run: {', '.join(run.lead_dirty[:10])}")
    lines += [
        f"selection         : pytest {shlex.join(run.selection)}",
        f"mode              : {run.mode}",
        f"launcher          : {run.entrypoint_blob[:12]} from {run.sandbox_repo}@"
        f"{run.trusted_ref} ({run.trusted_sha[:12]}), never from the reviewed tree",
    ]
    if run.mode == "sandbox":
        lines.append("reviewed runner   : " + (
            "identical to the trusted ref" if not run.runner_differs else
            "DIFFERS (" + ", ".join(run.runner_differs) + ") — that change was NOT used"))
    lines += [
        f"isolation probe   : {'PASS' if run.tests_ran else 'did not run'}"
        + (f" — {probe}" if probe else ""),
        f"result            : {run.status()}",
        f"summary           : {run.summary or '(no pytest summary line)'}",
    ]
    if run.failed_ids:
        lines.append(f"failed/error ids  : {len(run.failed_ids)}")
        lines += [f"  {i}" for i in run.failed_ids[:60]]
        if len(run.failed_ids) > 60:
            lines.append(f"  ... {len(run.failed_ids) - 60} more in the log")
    lines += [
        f"full log          : {run.log_path} (sha256 {run.log_sha256[:16]}, "
        f"{run.log_bytes} bytes, {run.duration_s:.0f}s)",
        "log tail:", *("  | " + line for line in run.tail.splitlines()),
    ]
    return lines


def run_for_review(lead_tree: str, rev: str, selection: Sequence[str], *,
                   base_rev: str | None = None, timeout_s: int = 1800,
                   config: RunnerConfig | None = None,
                   out: Callable[[str], None] = lambda _m: None) -> ReviewTests:
    cfg = config or load_config()
    head = run_review_tests(lead_tree, rev, selection, config=cfg,
                            timeout_s=timeout_s, out=out)
    base = None
    if base_rev:
        base = run_review_tests(lead_tree, base_rev, selection, config=cfg,
                                timeout_s=timeout_s, out=out)
    return ReviewTests(head=head, base=base)
