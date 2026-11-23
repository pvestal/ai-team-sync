"""Per-client credentials, audit log and rate limits for the remote read-only MCP (#3787).

Kept apart from remote_readonly.py so the bearer gate and tool dispatch stay
readable. Nothing here talks to ATS, Echo or a database: the audit log is a
local file plus the journal, never a database row (a row would be a write
caused by a remote read).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIN_TOKEN_LEN = 32
CLIENT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

# ---- per-client credentials ------------------------------------------------------


def _read_private_token(path: Path) -> str:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):  # a symlink is refused too (lstat)
        raise RuntimeError(f"{path} is not a regular file")
    if st.st_mode & 0o077:
        raise RuntimeError(f"{path} must be mode 0600 (is {oct(st.st_mode & 0o777)})")
    token = path.read_text().strip()
    if len(token) < MIN_TOKEN_LEN:
        raise RuntimeError(f"{path} holds a token shorter than {MIN_TOKEN_LEN} chars")
    return token


def load_clients(primary_file: Path, primary_client: str,
                 clients_dir: Path | None) -> dict[str, str]:
    """{client_id: secret}. The primary file is the #3760 token (behaviour kept).

    Extra clients, one secret per client, live in ``clients_dir`` as
    ``<client_id>.token`` (0600) inside a 0700 directory. A missing directory
    means "no extra clients"; anything malformed refuses startup.
    """
    if not CLIENT_ID_RE.fullmatch(primary_client):
        raise RuntimeError(f"bad primary client id {primary_client!r}")
    clients = {primary_client: _read_private_token(primary_file)}
    if clients_dir is not None and clients_dir.exists():
        st = clients_dir.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o077:
            raise RuntimeError(f"{clients_dir} must be a 0700 directory")
        for p in sorted(clients_dir.iterdir()):
            if p.suffix != ".token":
                raise RuntimeError(f"unexpected file in {clients_dir}: {p.name}")
            cid = p.stem
            if not CLIENT_ID_RE.fullmatch(cid) or cid in clients:
                raise RuntimeError(f"bad or duplicate client id {cid!r} in {clients_dir}")
            clients[cid] = _read_private_token(p)
    if len(set(clients.values())) != len(clients):
        raise RuntimeError("two remote clients share one secret; give each its own")
    return clients


# ---- rate limits -----------------------------------------------------------------


@dataclass
class _Bucket:
    tokens: float
    stamp: float


@dataclass
class RateLimiter:
    """Token bucket per (client, class). ``allow`` never blocks."""

    per_minute: dict[str, int] = field(default_factory=lambda: {"call": 60, "db": 30})
    clock: Any = time.monotonic
    _buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _refusal_logged: dict[str, float] = field(default_factory=dict)
    _suppressed: dict[str, int] = field(default_factory=dict)

    def should_record_refusal(self, client: str, every_s: float = 10.0) -> tuple[bool, int]:
        """(record this refusal?, refusals suppressed since the last recorded one).

        At most one rate-limit refusal per client per ``every_s`` becomes an
        audit line, so a client over its limit cannot flood the audit log or
        journal; the next recorded line carries the count of the ones it
        stood for, so every refused call is still accounted for (review r3)."""
        now = self.clock()
        with self._lock:
            last = self._refusal_logged.get(client)
            if last is not None and now - last < every_s:
                self._suppressed[client] = self._suppressed.get(client, 0) + 1
                return False, 0
            self._refusal_logged[client] = now
            return True, self._suppressed.pop(client, 0)

    def allow(self, client: str, cls: str) -> bool:
        cap = self.per_minute[cls]
        now = self.clock()
        with self._lock:
            b = self._buckets.get((client, cls))
            if b is None:
                b = self._buckets[(client, cls)] = _Bucket(float(cap), now)
            b.tokens = min(float(cap), b.tokens + (now - b.stamp) * cap / 60.0)
            b.stamp = now
            if b.tokens < 1.0:
                return False
            b.tokens -= 1.0
            return True


# ---- audit log ---------------------------------------------------------------------

audit_log = logging.getLogger("ats_remote_mcp.audit")
MAX_AUDIT_BYTES = 10 * 1024 * 1024


def describe_args(args: dict[str, Any],
                  enums: dict[str, frozenset[str]] | None = None,
                  known_keys: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Arguments as they may be logged: never client free text.

    Only keys the tool's schema declares (``known_keys``) are named; any other
    key is client text (review r2) and is counted, never named. Values:
    ints/bools verbatim; a string verbatim only when it is a member of that
    tool's FIXED enum for that key (``enums``); every other string, and
    anything nested, as its length and a short sha256 prefix.
    """
    enums = enums or {}
    out: dict[str, Any] = {}
    items = list((args or {}).items())
    unknown = sum(1 for k, _ in items if k not in known_keys)
    if unknown:
        out["<unknown_keys>"] = unknown
    for k, v in [(k, v) for k, v in items if k in known_keys][:16]:
        key = k
        if isinstance(v, bool) or (isinstance(v, int) and abs(v) < 2**53):
            out[key] = v
        elif isinstance(v, str) and v in enums.get(key, ()):
            out[key] = v
        else:
            raw = json.dumps(v, sort_keys=True, default=str).encode()
            out[key] = {"len": len(raw), "sha256": hashlib.sha256(raw).hexdigest()[:12]}
    return out


class AuditLog:
    """One JSON line per tool call: to a local file (rotated once) and the journal."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry},
                          sort_keys=True, default=str)
        audit_log.info(line)
        if self.path is None:
            return
        with self._lock:
            try:
                if self.path.exists() and self.path.stat().st_size > MAX_AUDIT_BYTES:
                    self.path.replace(self.path.with_suffix(".jsonl.1"))
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write(line + "\n")
            except OSError as exc:  # the journal line above still exists
                audit_log.warning("audit file write failed: %s", type(exc).__name__)
