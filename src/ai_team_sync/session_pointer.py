"""Per-Claude-session ATS pointer files — the Gap 3 fix.

Background: the MCP server persisted "the active session id" to a single global
file (~/.ats_session). Two concurrent Claude sessions on one machine clobbered
each other's pointer — last start_session wins — so the heartbeat hook and the
MCP verbs that read it (complete_session, log_decision, request_override) could
act on the WRONG session.

Fix: every Claude session has a stable id in $CLAUDE_CODE_SESSION_ID, present in
every hook's env and in the stdio MCP server's env. We persist that session's ATS
session id to a per-session file keyed by it, so concurrent sessions never
overwrite each other. The legacy global file is still written for back-compat,
but the per-session file always wins on resolve.

Resolution order (resolve_pointer):
  1. $ATS_SESSION_ID  — explicit operator override, always wins.
  2. ~/.ats_session_<client id>  — this session's own pointer, keyed by the
     FULL client id (#3593 r2); a pre-r2 ~/.ats_session_<cid8> is read only
     when no full-id file exists (see read_session_pointer).
  3. ~/.ats_session  — legacy global (single-session fallback).

State dir is $ATS_STATE_DIR if set (tests), else $HOME.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

GLOBAL_FILE_NAME = ".ats_session"
APPROVAL_TOKEN_PREFIX = ".ats_approval_"

# Live-cid handoff (Tower #2003). A Claude process spawns the stdio MCP server
# ONCE; /clear (and resume/compact) then rotates CLAUDE_CODE_SESSION_ID for every
# newly-spawned hook while the long-lived MCP child keeps its spawn-time value.
# The two disagree for the rest of that process's life, which made the lock guard
# block its own session. The owning Claude PID is the one key both sides can
# still agree on, so the SessionStart hook publishes the live cid under it.
LIVE_CID_PREFIX = ".ats_live_cid_"
_LIVE_CID_MAX_AGE_S = 7 * 24 * 3600


def _state_dir() -> Path:
    return Path(os.environ.get("ATS_STATE_DIR") or Path.home())


def env_claude_session_id() -> str | None:
    """This PROCESS's CLAUDE_CODE_SESSION_ID, straight from the environment.

    Correct for anything spawned per-turn (hooks). NOT correct for the stdio MCP
    server, which is spawned once and keeps a pre-/clear value — use
    claude_session_id() there. The SessionStart hook must publish from THIS
    function, or it would republish whatever it read back from its own record.
    """
    cid = (os.environ.get("CLAUDE_CODE_SESSION_ID")
           or os.environ.get("ATS_SESSION") or "").strip()
    return cid or None


def host_pid() -> str:
    """The owning Claude process id — the identifier a hook and the stdio MCP
    subprocess still agree on after /clear rotates CLAUDE_CODE_SESSION_ID.

    Hooks carry it in $CLAUDE_PID. The MCP subprocess does not, but Claude Code
    spawns it directly, so os.getppid() is the same number (verified on tower:
    ats-mcp pid 1891887 -> ppid 1891656 == CLAUDE_PID of the owning session).
    """
    return (os.environ.get("CLAUDE_PID") or "").strip() or str(os.getppid())


def _proc_starttime(pid: str) -> str | None:
    """Host process start-time, to tell a live pid from a recycled one.

    Linux only (field 22 of /proc/<pid>/stat, read after the comm field so a
    process name containing ')' cannot shift the offsets). Returns None where
    /proc is unavailable — then publish records no start-time and the recycle
    check is skipped rather than always firing.
    """
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as fh:
            data = fh.read()
        return data.rsplit(")", 1)[1].split()[19]
    except Exception:
        return None


def live_cid_path(pid: str | None = None) -> Path:
    return _state_dir() / f"{LIVE_CID_PREFIX}{pid or host_pid()}"


def publish_live_cid(cid: str, pid: str | None = None) -> None:
    """Record the LIVE Claude session id for this Claude process.

    Called by the SessionStart hook, which is respawned on /clear and therefore
    always holds the current cid. Best-effort: never raises into a hook."""
    cid = (cid or "").strip()
    if not cid:
        return
    pid = str(pid or host_pid())
    try:
        _state_dir().mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        live_cid_path(pid).write_text(json.dumps(
            {"cid": cid, "pid": pid, "starttime": _proc_starttime(pid) or ""}))
    except Exception:
        pass


@contextmanager
def lifecycle_registration_lock(cid: str):
    """Serialize one client's pointer/create/capability sequence.

    The database unique index is the cross-process backstop; this lock also
    ensures a local concurrent hook observes the winner's saved capability
    instead of racing it and creating an unusable sibling.
    """
    digest = hashlib.sha256(cid.encode("utf-8")).hexdigest()
    path = _state_dir() / f".ats_registration_{digest}"
    _state_dir().mkdir(parents=True, exist_ok=True)
    with _store_lock(path):
        yield


def live_claude_session_id() -> str | None:
    """The cid most recently published by a hook of THIS Claude process.

    Ignored when the record is older than a week, or when the host pid's
    start-time no longer matches what was recorded — a recycled pid must not let
    a new session inherit a dead one's identity.
    """
    pid = host_pid()
    path = live_cid_path(pid)
    try:
        record = json.loads(path.read_text())
    except Exception:
        return None
    try:
        if time.time() - path.stat().st_mtime > _LIVE_CID_MAX_AGE_S:
            return None
    except Exception:
        pass
    recorded = str(record.get("starttime") or "")
    if recorded and _proc_starttime(pid) != recorded:
        return None  # pid recycled, or the host process is gone
    return str(record.get("cid") or "").strip() or None


def claude_session_id() -> str | None:
    """The host agent's per-session id, as every ATS caller should see it.

    Prefers the cid a hook of this Claude process published, because the stdio
    MCP server's own environment goes stale at the first /clear (#2003). Falls
    back to this process's environment when nothing was published, so agents
    without the SessionStart hook behave exactly as before.
    """
    return live_claude_session_id() or env_claude_session_id()


def detect_agent() -> str:
    """Detect which AI agent is active. The ONE detection (#2517).

    Honors ATS_AGENT (explicit override) first, then known env signatures.
    NOTE: Claude Code sets CLAUDECODE (no underscore) — the old
    CLAUDE_CODE-only check returned 'unknown' (#1556). cli._detect_agent and
    mcp.server.detect_agent delegate here; before #2517 the CLI carried a
    rotted copy that still missed CLAUDECODE, so `ats session start` from a
    Claude Code shell registered as 'unknown'.
    """
    explicit = (os.environ.get("ATS_AGENT") or "").strip()
    if explicit:
        return explicit
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE"):
        return "claude-code"
    if any(key.startswith("CODEX") for key in os.environ):
        return "codex"
    if os.environ.get("CURSOR_SESSION"):
        return "cursor"
    if os.environ.get("COPILOT_WORKSPACE"):
        return "copilot-workspace"
    return "unknown"


def agent_label(base: str | None = None, cid: str | None = None) -> str:
    """Per-session agent identity: '<base>:<cid8>' (#1556/#2003/#2517).

    THE label the lock guard attributes sessions by: it string-matches the
    hook payload's live cid against this. Every registrar (SessionStart
    autostart, the stdio MCP, the CLI) must build it HERE — a registrar that
    writes a bare base label creates a session its own creator can never be
    matched to, so the guard names the caller's scope as "another ACTIVE
    session's scope" and every re-register adds another blocker (#2517).

    base defaults to detect_agent(); cid defaults to claude_session_id().
    Falls back to the bare base when no cid is known ('unknown' never gets a
    suffix — a token on an unidentified agent reads as identity it isn't).
    """
    base = (base or detect_agent()).strip() or "unknown"

    # A DELEGATED child takes no cid suffix. The wrapper creates its row as
    # '<worker>:delegate' before the process exists, so appending a cid here
    # would produce '<worker>:delegate:<cid8>' and the child would fail the
    # ownership check on its OWN row (observed by an independent Codex run:
    # process reported claude-code while the row said claude-code:delegate).
    # Its identity is the delegation binding, which is exact; the cid adds
    # nothing a delegation id does not already pin down.
    if (os.environ.get("ATS_DELEGATION") or "").strip():
        return base

    cid = (cid if cid is not None else (claude_session_id() or "")).strip()
    return f"{base}:{cid[:8]}" if (cid and base != "unknown") else base


def global_pointer_path() -> Path:
    return _state_dir() / GLOBAL_FILE_NAME


# ── client keys (#3593 review r2, blocker 2) ─────────────────────────────────
#
# Per-client files used to be keyed by cid[:8]. Two DISTINCT Claude sessions
# whose ids share eight characters then shared one pointer and one capability
# file. Every file is now keyed by the FULL client id; the eight-character
# names are only ever READ, as a migration path for files written by older
# code (see legacy_session_pointer_path / _load_legacy_approval).
_SAFE_CLIENT_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def client_key(cid: str) -> str:
    """The filename-safe key for a client id: the id itself when it is a plain
    token (every Claude session id is a UUID), else a digest, so a hostile or
    odd id can never name a path outside the state directory."""
    cid = (cid or "").strip()
    if _SAFE_CLIENT_KEY.fullmatch(cid):
        return cid
    return "h-" + hashlib.sha256(cid.encode("utf-8")).hexdigest()[:40]


def session_pointer_path(cid: str) -> Path:
    """THIS client's pointer, keyed by its full id."""
    return _state_dir() / f"{GLOBAL_FILE_NAME}_{client_key(cid)}"


def legacy_session_pointer_path(cid: str) -> Path | None:
    """The pre-r2 '<GLOBAL_FILE_NAME>_<cid8>' pointer, or None when it is the
    same file as session_pointer_path (an id of eight characters or fewer)."""
    cid = (cid or "").strip()
    legacy = _state_dir() / f"{GLOBAL_FILE_NAME}_{cid[:8]}"
    return None if not cid or legacy == session_pointer_path(cid) else legacy


def read_session_pointer(cid: str) -> str | None:
    """The session this client's pointer names, or None.

    The full-id file is authoritative whenever it EXISTS, even empty: an empty
    file is the tombstone clear_pointer leaves, so a stale legacy pointer cannot
    resurface after this client completed its session. Only when no full-id
    file exists is the legacy eight-character file read -- the rollout path for
    sessions whose pointer older code wrote. The pointer is a DEFAULT SELECTOR
    only: it never carries a capability."""
    if not cid:
        return None
    try:
        return session_pointer_path(cid).read_text().strip() or None
    except FileNotFoundError:
        pass
    except Exception:
        return None
    legacy = legacy_session_pointer_path(cid)
    if legacy is None:
        return None
    try:
        return legacy.read_text().strip() or None
    except Exception:
        return None


# ── the capability keyring (#3593) ───────────────────────────────────────────
#
# One Claude session -- and every subagent it runs, which share its id and its
# one stdio MCP process -- may hold several ATS sessions at once: two
# repositories, two tickets, a parent and its registered subagents. Each keeps
# its OWN capability here. The file used to hold one pair written with O_TRUNC,
# so a second start_session destroyed the first session's capability and that
# session could no longer be completed (2026-10-15, 2026-10-22).
#
# Layout:  <state>/.ats_capabilities/            0700, owned by this user
#          <state>/.ats_capabilities/<key>.json  0600, one per FULL client id
#          <state>/.ats_capabilities/<key>.lock  flock for read-modify-write
#
#   {"version": 2, "client_id": "<full client id>",
#    "sessions": {"<session id>": {"token": "...", "state": "open",
#                                   "parent": "<session id>" | null,
#                                   "saved_at": <epoch>, "state_at": <epoch>}}}
#
# `state` is what THIS client learned from its own results: "open" when the
# capability was minted, "completed" after its own successful completion,
# "revoked" when the server refused the capability itself. It is bookkeeping
# for eviction and default selection, never authority: the server decides
# every lifecycle question, and a "completed" entry is never made "open" again.
#
# A completed entry's token is KEPT: a completed sender may still readdress its
# unread direct message after turnover (routers/messages.readdress_message),
# and that authority is exactly its own capability.
CAPABILITY_DIR_NAME = ".ats_capabilities"
CAPABILITY_STORE_VERSION = 2
CAP_OPEN, CAP_COMPLETED, CAP_REVOKED = "open", "completed", "revoked"
_CAP_STATES = frozenset({CAP_OPEN, CAP_COMPLETED, CAP_REVOKED})
# Eviction (review r2, blocker 1): past this many entries the OLDEST NON-OPEN
# entry is dropped. An open entry is never evicted: when every entry is open
# the keyring grows instead. Refusing would not help -- the server has already
# created the row by the time its capability arrives, so a refused save is
# exactly the orphan this ticket exists to stop -- and the store is per FULL
# client id, so its growth is bounded by what one Claude session itself opens.
APPROVAL_TOKEN_MAX = 32
_LOCK_TIMEOUT_S = 10.0


class CapabilityStoreError(RuntimeError):
    """The keyring could not be read or updated SAFELY. Never swallowed into
    'no capabilities': a malformed store is refused, not overwritten, and an
    update that cannot take the lock does not run unlocked."""


def _capability_dir(create: bool) -> Path:
    d = _state_dir() / CAPABILITY_DIR_NAME
    if create:
        try:
            _state_dir().mkdir(parents=True, exist_ok=True)
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise CapabilityStoreError(f"cannot create {d}: {exc}") from exc
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        if create:
            raise CapabilityStoreError(f"{d} vanished while being created")
        return d
    except OSError as exc:
        raise CapabilityStoreError(f"cannot stat {d}: {exc}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise CapabilityStoreError(f"{d} is not a directory (symlink or file); refusing it")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise CapabilityStoreError(f"{d} is owned by uid {st.st_uid}, not this user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        try:
            os.chmod(d, 0o700)
        except OSError as exc:
            raise CapabilityStoreError(f"cannot make {d} private: {exc}") from exc
    return d


def capability_store_path(cid: str) -> Path:
    return _state_dir() / CAPABILITY_DIR_NAME / f"{client_key(cid)}.json"


def _check_private_file(fd: int, name: str, *, tighten: bool) -> None:
    """The open file must be a regular file owned by this user and private.
    Checked on the DESCRIPTOR (opened with O_NOFOLLOW), so it cannot be swapped
    between the check and the use. A loose store is refused (this code only ever
    writes 0600, so a readable store is not ours to trust); a loose lock file,
    which holds nothing, is tightened when `tighten`."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise CapabilityStoreError(f"{name} is not a regular file; refusing it")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise CapabilityStoreError(f"{name} is owned by uid {st.st_uid}, not this user; refusing it")
    if stat.S_IMODE(st.st_mode) & 0o077:
        if not tighten:
            raise CapabilityStoreError(f"{name} is readable by others (mode "
                                       f"{stat.S_IMODE(st.st_mode):o}); refusing it")
        os.fchmod(fd, 0o600)


def _open_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    """os.open that refuses a symlink at the final component."""
    try:
        return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise CapabilityStoreError(f"{path.name} is a symlink; refusing it") from exc
        raise


def _read_store(path: Path, cid: str) -> dict[str, dict]:
    """{session_id: entry}, oldest first. {} only when the file does not exist.
    Anything else that is not a well-formed store FOR THIS CLIENT raises,
    including a store reached through a symlinked or foreign-owned directory, a
    symlinked store file, a foreign-owned one, or one readable by others."""
    _capability_dir(create=False)
    try:
        fd = _open_nofollow(path, os.O_RDONLY)
    except FileNotFoundError:
        return {}
    except CapabilityStoreError:
        raise
    except OSError as exc:
        raise CapabilityStoreError(f"cannot read {path.name}: {exc}") from exc
    try:
        _check_private_file(fd, path.name, tighten=False)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            raw = stream.read()
    except OSError as exc:
        raise CapabilityStoreError(f"cannot read {path.name}: {exc}") from exc
    finally:
        if fd != -1:
            os.close(fd)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CapabilityStoreError(f"{path.name} is malformed (not JSON); refusing it") from exc
    if not isinstance(data, dict) or data.get("version") != CAPABILITY_STORE_VERSION:
        raise CapabilityStoreError(f"{path.name} is not a version-{CAPABILITY_STORE_VERSION} "
                                   "capability store; refusing it")
    if data.get("client_id") != cid:
        raise CapabilityStoreError(f"{path.name} names another client id; refusing it")
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        raise CapabilityStoreError(f"{path.name} has no session map; refusing it")
    out: dict[str, dict] = {}
    for sid, entry in sessions.items():
        if not (isinstance(sid, str) and sid and isinstance(entry, dict)
                and isinstance(entry.get("token"), str) and entry["token"]
                and entry.get("state") in _CAP_STATES
                and (entry.get("parent") is None or isinstance(entry.get("parent"), str))):
            raise CapabilityStoreError(f"{path.name} has a malformed entry; refusing it")
        out[sid] = entry
    return out


def _write_store(path: Path, cid: str, entries: dict[str, dict]) -> None:
    """Atomically replace the store: a 0600 temp file in the same directory,
    fsync, rename over the old file, fsync the directory. On any failure the
    previous store is left exactly as it was and the error is raised."""
    d = path.parent
    try:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=d)
    except OSError as exc:
        raise CapabilityStoreError(f"cannot write in {d}: {exc}") from exc
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": CAPABILITY_STORE_VERSION, "client_id": cid,
                       "sessions": entries}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(exc, Exception):
            raise CapabilityStoreError(f"could not update {path.name}: {exc}") from exc
        raise
    try:
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # the rename is done; a directory fsync is durability, not atomicity


@contextmanager
def _store_lock(path: Path, timeout: float = _LOCK_TIMEOUT_S):
    """Exclusive flock for one client's read-modify-write (the MCP server and
    the hooks of one Claude session all write it). A lock that cannot be taken
    is an ERROR: the update never proceeds unlocked."""
    try:
        import fcntl
    except ImportError as exc:  # non-POSIX
        raise CapabilityStoreError("no flock on this platform; refusing an unlocked "
                                   "capability update") from exc
    lock_path = path.with_suffix(".lock")
    try:
        fd = _open_nofollow(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except CapabilityStoreError:
        raise
    except OSError as exc:
        raise CapabilityStoreError(f"cannot open {lock_path.name}: {exc}") from exc
    try:
        try:
            _check_private_file(fd, lock_path.name, tighten=True)
        except OSError as exc:
            raise CapabilityStoreError(f"cannot secure {lock_path.name}: {exc}") from exc
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CapabilityStoreError(
                        f"timed out after {timeout:.0f}s waiting for {lock_path.name}")
                time.sleep(0.01)
            except OSError as exc:
                raise CapabilityStoreError(f"flock on {lock_path.name} failed: {exc}") from exc
        yield
    finally:
        os.close(fd)  # closing releases the flock


def _evict(entries: dict[str, dict]) -> None:
    """Drop the oldest NON-open entries past APPROVAL_TOKEN_MAX. Never an open
    one: with every entry open, the keyring grows (see APPROVAL_TOKEN_MAX)."""
    while len(entries) > APPROVAL_TOKEN_MAX:
        victim = next((sid for sid, e in entries.items() if e.get("state") != CAP_OPEN), None)
        if victim is None:
            return
        entries.pop(victim)


def save_approval_token(session_id: str, token: str, cid: str | None = None, *,
                        parent_session_id: str | None = None) -> None:
    """Add `session_id`'s capability to this client's keyring as an OPEN entry.

    Never replaces or invalidates another session's entry. Raises
    CapabilityStoreError when the keyring cannot be updated safely (malformed
    store, lock not taken, write failed): the caller decides how loud to be, and
    nothing is overwritten on the way. A no-op without a client id or token."""
    cid = (cid or claude_session_id() or "").strip()
    if not cid or not session_id or not token:
        return
    _capability_dir(create=True)
    path = capability_store_path(cid)
    with _store_lock(path):
        entries = _read_store(path, cid)
        entries.pop(session_id, None)
        now = time.time()
        entries[session_id] = {"token": token, "state": CAP_OPEN,
                               "parent": parent_session_id or None,
                               "saved_at": now, "state_at": now}
        _evict(entries)
        _write_store(path, cid, entries)


def mark_capability_state(session_id: str, state: str, cid: str | None = None) -> bool:
    """Record what this client learned about `session_id` (completed/revoked).

    Monotone: an entry that is no longer open never becomes open again, and a
    revoked one stays revoked. Returns whether an entry was changed. Raises
    CapabilityStoreError like save_approval_token."""
    if state not in (CAP_COMPLETED, CAP_REVOKED):
        raise ValueError(f"not a terminal capability state: {state!r}")
    cid = (cid or claude_session_id() or "").strip()
    if not cid or not session_id:
        return False
    path = capability_store_path(cid)
    if not path.exists():
        return False
    _capability_dir(create=True)
    with _store_lock(path):
        entries = _read_store(path, cid)
        entry = entries.get(session_id)
        if entry is None or entry["state"] == state or entry["state"] == CAP_REVOKED:
            return False
        entry["state"], entry["state_at"] = state, time.time()
        _evict(entries)
        _write_store(path, cid, entries)
        return True


def capability_entries(cid: str | None = None) -> dict[str, dict]:
    """{session_id: {'state', 'parent'}} held in this client's keyring -- no
    tokens. Raises CapabilityStoreError for a malformed store."""
    cid = (cid or claude_session_id() or "").strip()
    if not cid:
        return {}
    entries = _read_store(capability_store_path(cid), cid)
    return {sid: {"state": e["state"], "parent": e.get("parent")}
            for sid, e in entries.items()}


def _load_legacy_approval(session_id: str, cid: str) -> str:
    """Migration read of the pre-r2 '.ats_approval_<cid8>' file.

    Used only when the session association is VERIFIABLE FROM THE FILE ITSELF:
    the file must pair exactly `session_id` with a token -- either as its
    top-level {'session_id','token'} (pre-#3593) or inside its 'sessions' map
    (the never-deployed first #3593 shape) -- and, if it names a client id at
    all, that id must be ours. The pointer is never consulted: a token is never
    attributed to whichever session the pointer happens to name. The file is
    never written or deleted here (a running pre-r2 process may still use it)."""
    path = _state_dir() / f"{APPROVAL_TOKEN_PREFIX}{cid[:8]}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    named = data.get("client_id", data.get("cid"))
    if named is not None and named != cid:
        return ""
    if data.get("session_id") == session_id and isinstance(data.get("token"), str):
        return data["token"]
    sessions = data.get("sessions")
    if isinstance(sessions, dict):
        token = sessions.get(session_id)
        if isinstance(token, str):
            return token
    return ""


def load_approval_token(session_id: str, cid: str | None = None, *,
                        allow_delegated: bool = True) -> str:
    """The capability for EXACTLY `session_id`, or ''. Never another row's.

    Order: this client's keyring (a revoked entry answers ''), then the legacy
    eight-character file under the rule in _load_legacy_approval, then -- only
    when ``allow_delegated`` is true -- a delegated child's process-scoped
    capability file. Request-bound callers disable that last fallback. Raises
    CapabilityStoreError for a malformed keyring rather than reading it as
    empty."""
    cid = (cid or claude_session_id() or "").strip()
    if not session_id:
        return ""
    if cid:
        entry = _read_store(capability_store_path(cid), cid).get(session_id)
        if entry is not None:
            return "" if entry["state"] == CAP_REVOKED else entry["token"]
        token = _load_legacy_approval(session_id, cid)
        if token:
            return token
    if allow_delegated:
        delegated = load_delegated_capability()
        return delegated[1] if delegated and delegated[0] == session_id else ""
    return ""


def legacy_capability_sessions(cid: str) -> set[str]:
    """Session ids the legacy eight-character file itself pairs with a token."""
    path = _state_dir() / f"{APPROVAL_TOKEN_PREFIX}{(cid or '')[:8]}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    named = data.get("client_id", data.get("cid"))
    if named is not None and named != cid:
        return set()
    out = set()
    if isinstance(data.get("session_id"), str) and isinstance(data.get("token"), str) \
            and data["session_id"] and data["token"]:
        out.add(data["session_id"])
    if isinstance(data.get("sessions"), dict):
        out |= {s for s, t in data["sessions"].items() if isinstance(s, str) and isinstance(t, str)
                and s and t}
    return out


def held_capability_sessions(cid: str | None = None) -> set[str]:
    """Sessions whose OWN capability this client holds: keyring entries that
    are not revoked, plus the pairs the legacy eight-character file itself
    names (_load_legacy_approval's rule). Raises CapabilityStoreError for a
    malformed keyring."""
    cid = (cid or claude_session_id() or "").strip()
    if not cid:
        return set()
    entries = _read_store(capability_store_path(cid), cid)
    held = {sid for sid, e in entries.items() if e["state"] != CAP_REVOKED}
    return held | (legacy_capability_sessions(cid) - set(entries))


# A delegated child's capability for the row its supervisor registered. It is a
# file in the child's private ATS_STATE_DIR, not an environment variable,
# because Codex forwards its MCP env as `-c` argv and /proc/<pid>/cmdline is
# world-readable. The supervisor writes it before spawn and deletes it after
# finalization.
DELEGATED_CAPABILITY_FILE = ".ats_delegated_capability"


def save_delegated_capability(state_dir: Path, session_id: str, token: str) -> None:
    if not session_id or not token:
        return
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(state_dir / DELEGATED_CAPABILITY_FILE,
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"session_id": session_id, "token": token}, stream)
    except Exception:
        pass  # a missing capability fails closed in the child


def load_delegated_capability() -> tuple[str, str] | None:
    """(session_id, token) handed to this delegated child, or None."""
    if not (os.environ.get("ATS_DELEGATION") or "").strip():
        return None
    try:
        data = json.loads((_state_dir() / DELEGATED_CAPABILITY_FILE).read_text(encoding="utf-8"))
    except Exception:
        return None
    sid, token = str(data.get("session_id") or ""), str(data.get("token") or "")
    return (sid, token) if sid and token else None


def delete_delegated_capability(state_dir: Path) -> None:
    try:
        (state_dir / DELEGATED_CAPABILITY_FILE).unlink(missing_ok=True)
    except Exception:
        pass


def save_pointer(session_id: str, cid: str | None = None, *,
                 write_global: bool = True) -> None:
    """Record the ATS session id for this client.

    Hooks and legacy callers write both the authoritative per-client pointer
    and the back-compat global file.  A request-bound MCP caller passes
    ``write_global=False`` because another client can observe that shared file.
    Best-effort: never raises into a hook/turn.
    """
    cid = cid or claude_session_id()
    sd = _state_dir()
    try:
        sd.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        if write_global:
            global_pointer_path().write_text(session_id)
    except Exception:
        pass
    if cid:
        try:
            session_pointer_path(cid).write_text(session_id)
        except Exception:
            pass


def resolve_pointer(cid: str | None = None, allow_global: bool = True) -> str | None:
    """Resolve THIS session's ATS session id. See module docstring for order.

    allow_global=False drops step 3 (the legacy shared file). Callers that
    ADOPT the resolved row must pass False: the global file names whichever
    session wrote it last, so a new session would inherit that row's agent
    label, scope and description.
    """
    env = (os.environ.get("ATS_SESSION_ID") or "").strip()
    if env:
        return env
    cid = cid or claude_session_id()
    if cid:
        content = read_session_pointer(cid)
        if content:
            return content
    if not allow_global:
        return None
    try:
        content = global_pointer_path().read_text().strip()
        return content or None
    except Exception:
        return None


def resolve_pointer_source(cid: str | None = None) -> tuple[str | None, str]:
    """(session_id, WHERE it came from) — the provenance a mutation must check.

    resolve_pointer() answers "which session", which is enough for a read. A
    mutation also needs "can this id have been written by somebody else", and
    the three sources differ:

      env          $ATS_SESSION_ID — set by whoever launched this process.
      per_session  ~/.ats_session_<client id> — keyed to THIS Claude session.
      global       ~/.ats_session — SHARED. Names whichever session wrote it
                   last, across every agent on the box.

    Proven live 2026-09-11: a delegated Claude's SessionStart overwrote the
    global file, a Codex parent (no cid, so no per-session pointer) resolved
    through it, and its complete_session completed the child's row while
    reporting success. Mutations must refuse 'global'.
    """
    env = (os.environ.get("ATS_SESSION_ID") or "").strip()
    if env:
        return env, "env"

    cid = cid or claude_session_id()
    if cid:
        content = read_session_pointer(cid)
        if content:
            return content, "per_session"

    try:
        content = global_pointer_path().read_text().strip()
        if content:
            return content, "global"
    except Exception:
        pass
    return None, "none"


def clear_pointer(cid: str | None = None) -> None:
    """Drop this session's per-session pointer (on complete_session). Leaves the
    global file alone — another session may legitimately own it."""
    cid = cid or claude_session_id()
    if not cid:
        return
    legacy = legacy_session_pointer_path(cid)
    try:
        if legacy is not None and legacy.exists():
            # Tombstone, not unlink: with the full-id file gone, a legacy
            # eight-character pointer written by older code would resurface
            # and name a session this client already left (read_session_pointer).
            session_pointer_path(cid).write_text("")
        else:
            session_pointer_path(cid).unlink()
    except Exception:
        pass
