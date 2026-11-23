"""Read-only Tower context for the remote MCP (#3787 Phase 1).

Echo task envelopes come from Echo's own SELECT-only route over loopback, through
remote_readonly's guarded transport. Everything else Echo- or Anime-Studio-side
comes from ``remote_read`` SQL views (deploy/sql/remote_read/) read by the
dedicated ``tower_remote_ro`` role:

* Anime Studio's HTTP API is never called: in single_user auth mode every
  request to it is the operator, and several of its GET routes write.
* Every SQL statement is a literal in ``NAMED_QUERIES``; a caller supplies only
  typed, validated parameters. No identifier, clause or fragment is built from
  caller input.
* Each query runs in a READ ONLY transaction, on a role whose session default is
  read-only, and the connector refuses to use a connection whose role could
  touch anything outside ``remote_read`` (``_verify_role``).
* ``service_state`` runs a fixed set of argv lists (systemctl show / git
  rev-parse) and reads one config file through a key allowlist. No caller input
  reaches either.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp.types import TextContent, Tool, ToolAnnotations

DB_FILE = Path(os.environ.get(
    "ATS_REMOTE_MCP_DB_FILE",
    str(Path.home() / ".config" / "ai-team-sync" / "remote-ro-db.json")))
DB_ROLE = "tower_remote_ro"
DATABASES = {"echo": "echo_brain", "anime": "anime_production"}
AS_FLAGS_FILE = Path("/etc/anime-studio/flags.env")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)

# ---- redaction ------------------------------------------------------------------

_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bhvs\.[A-Za-z0-9_-]{20,}"),                  # Vault tokens
)
# key/value forms are handled by _scrub_kv below (one rule, every quoting form).
# The KEY half of a key/value credential; the value's extent is decided by
# _scrub_kv (review r5: a regex value stopped at whitespace, so a quoted
# passphrase or an escaped private key leaked after its first word).
_KV_KEY = re.compile(
    r"(?i)[\"'\\]*\b[a-z0-9_-]*?(?P<key>token|secret|password|passwd|passphrase|api[_-]?key|"
    r"credential|cookie|authorization|bearer|nonce|chat_id|user_id|private_key|signature)"
    r"[a-z0-9_-]*[\"'\\]*\s*[:=]\s*")
# A PEM private key anywhere, raw or with escaped newlines; unterminated = to the end.
_PEM_PRIVATE = re.compile(r"-----BEGIN[^-\n]*PRIVATE KEY-----.*?(?:-----END[^-\n]*PRIVATE KEY-----|\Z)",
                          re.S)


def _scrub_kv(text: str) -> str:
    """Withhold the whole value after every credential key.

    A value opened by a quote (", ' or an escaped form like \\") runs to the
    close at the same escaping level; an unquoted value runs to the end of the line
    (a real newline or a literal backslash-n). Over-withholding the rest of a
    line is the accepted cost: a value is never cut at a space."""
    out, i = [], 0
    for m in _KV_KEY.finditer(text):
        if m.start() < i:
            continue
        j = m.end()
        q = re.match(r"(\\*)([\"'])", text[j:])
        if q:
            opener, quote, run0 = q.group(0), q.group(2), len(q.group(1))
            k, close_at = j + len(opener), len(text)
            # The close is the same quote character preceded by exactly the
            # opener's backslash run (or, for a plain opener, an even run).
            # A longer run is an escaped quote INSIDE the value (review r6:
            # \\\" inside \" ... \" was taken as the close). Unclosed: to
            # the end of the text.
            while True:
                k = text.find(quote, k)
                if k < 0:
                    break
                run = 0
                while k - run - 1 >= j and text[k - run - 1] == "\\":
                    run += 1
                if run == run0 or (run0 == 0 and run % 2 == 0):
                    close_at = k - run
                    break
                k += 1
            out.append(text[i:j] + opener + "<redacted>")
            i = close_at
        else:
            # Every credential key withholds its value to the end of the line
            # (review r6: a prose exception for "weak" keys let short tokens and
            # alphabetic ids through). Cost, measured on live envelopes: the rest
            # of a prose line that happens to read "authorization: ...".
            ends = [e for e in (text.find("\n", j), text.find("\\n", j)) if e >= 0]
            out.append(text[i:j] + "<redacted>")
            i = min(ends) if ends else len(text)
    out.append(text[i:])
    return "".join(out)


# A credential written as JSON text inside a string column ("token": "...").
_SECRET_JSON = re.compile(
    r'(?i)("(?:[a-z0-9_]*?)(?:token|secret|password|passwd|api[_-]?key|credential|cookie|'
    r'authorization|chat_id|user_id|telegram_[a-z_]*id)"\s*:\s*)("[^"]*"|-?[0-9]+)')
# A dict KEY naming a credential or a personal identifier: its value is
# withheld whatever its type (#3787 review r1: nested {"token": ...} and
# integer telegram_chat_id survived a text-only scrub).
_SENSITIVE_KEY = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|credential|cookie|authorization|"
    r"bearer|nonce|chat_id|user_id|from_user|telegram_[a-z_]*id|session_fingerprint|"
    r"private_key|signature)")
# An absolute path starts at a '/' not preceded by a word character, '.', ':',
# '/' or '~' (so URLs and dates are not paths) and RUNS TO THE NEXT DELIMITER,
# spaces and any Unicode included (review r3: "/data private/x", "/dataé/x"; a
# real file name may contain spaces). A run containing a further '/' is
# withheld whole. Over-withholding trailing words is the accepted cost.
_PATH_DELIMS = "'\"\n\r,;)\\]}<>"  # regex character-class body: \] is a literal ]
_ABS_RUN = re.compile(r"(?<![\w.:/~])/[^\s/" + _PATH_DELIMS + r"][^" + _PATH_DELIMS + r"]*")
# Echo task text cites HTTP routes (/api/..., /health, /mcp); they are not files.
_ROUTE_TOKEN = re.compile(r"(?<![\w.:/~])/(?:api/|mcp\b|mcp-|health\b)[^\s" + _PATH_DELIMS + r"]*")
_JSON_START = re.compile(r"[\[{]")
# A sensitive key in text whose value is a container that did not parse as
# JSON: everything from the key to the end of the string is withheld.
_SECRET_JSON_CONTAINER = re.compile(
    r'(?is)"[a-z0-9_]*?(?:token|secret|password|passwd|api[_-]?key|credential|cookie|'
    r'authorization|chat_id|user_id|telegram_[a-z_]*id|nonce|private_key|signature)"\s*:\s*[\[{].*')
# Repo citations in Echo task text stay readable as repo-relative labels;
# every other absolute path is withheld.
_REPO_ROOTS = (("/opt/tower-echo-brain/", "echo:"), ("/opt/anime-studio/", "anime-studio:"),
               ("/home/patrick/code/ai-team-sync/", "ai-team-sync:"))


_REPO_TOKEN = re.compile(
    r"(?<![\w.:/~])(" + "|".join(re.escape(r) for r, _ in _REPO_ROOTS) + r")([^\s" + _PATH_DELIMS + r"]*)")


def _scrub_paths(text: str, repos: bool) -> str:
    """Withhold absolute paths. ``repos``: keep repo citations as labels and
    HTTP routes as written (Echo task text); every other path is withheld."""
    if "/" not in text:
        return text
    text = text.replace("\x00", "")  # the placeholder below can never come from input
    keep: list[str] = []

    def stash(value: str) -> str:
        keep.append(value)
        return f"\x00{len(keep) - 1}\x00"

    if repos:
        def repo(m: re.Match) -> str:
            rest = m.group(2)
            if ".." in rest.split("/") or rest.startswith("."):
                return m.group(0)  # left for the general rule below
            return stash(dict(_REPO_ROOTS)[m.group(1)] + rest)
        text = _REPO_TOKEN.sub(repo, text)
        text = _ROUTE_TOKEN.sub(lambda m: stash(m.group(0)), text)
    text = _ABS_RUN.sub(lambda m: "<path>" if "/" in m.group(0)[1:] else m.group(0), text)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


def _scrub_embedded_json(text: str, paths: bool) -> str:
    """JSON objects/arrays written inside a text value are scrubbed structurally
    (review r2: {"token": ["..."]} and {"credential": {...}} survived a scalar
    regex), then re-serialised in place."""
    if "{" not in text and "[" not in text:
        return text
    dec = json.JSONDecoder()
    out, i = [], 0
    while True:
        m = _JSON_START.search(text, i)
        if not m:
            out.append(text[i:])
            return "".join(out)
        try:
            obj, end = dec.raw_decode(text, m.start())
        except ValueError:
            out.append(text[i:m.start() + 1])
            i = m.start() + 1
            continue
        out.append(text[i:m.start()])
        out.append(json.dumps(scrub(obj, paths=paths), sort_keys=True)
                   if isinstance(obj, (dict, list)) else text[m.start():end])
        i = end


def scrub(value: Any, *, paths: bool = True) -> Any:
    """Withhold credentials and personal ids (by key, by JSON text, by shape)
    and absolute filesystem paths, recursively. ``paths=True`` (production
    rows, where a path names a media file) withholds every absolute path;
    ``paths=False`` (Echo task text) keeps repo paths as repo-relative labels
    and withholds all others."""
    if isinstance(value, str):
        value = _scrub_embedded_json(value, paths)
        value = _SECRET_JSON.sub(lambda m: m.group(1) + '"<redacted>"', value)
        value = _SECRET_JSON_CONTAINER.sub('"<redacted>": "<redacted>"', value)
        value = _PEM_PRIVATE.sub("<redacted private key>", value)
        value = _scrub_kv(value)
        for pat in _SECRET_PATTERNS:
            value = pat.sub("<redacted>", value)
        return _scrub_paths(value, repos=not paths)
    if isinstance(value, dict):
        return {k: ("<redacted>" if isinstance(k, str) and _SENSITIVE_KEY.search(k)
                    and v not in (None, "", [], {}) else scrub(v, paths=paths))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, paths=paths) for v in value]
    return value


def _json(rows: Any, paths: bool = True) -> str:
    return json.dumps(scrub(rows, paths=paths), indent=1, sort_keys=True, default=str)


# ---- argument validation -----------------------------------------------------------

class BadArgument(ValueError):
    """A caller argument failed validation; reported as a tool error."""


_WORD = re.compile(r"[a-z][a-z0-9_]{0,31}")


def _int(a: dict, key: str, *, lo: int = 1, hi: int = 2**31 - 1,
         default: int | None = None) -> int | None:
    v = a.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise BadArgument(f"{key} must be an integer in [{lo}, {hi}]")
    return v


def _limit(a: dict, hi: int, default: int = 30) -> int:
    v = a.get("limit", default)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise BadArgument("limit must be a positive integer")
    return min(v, hi)


def _word(a: dict, key: str, allowed: frozenset[str] | None = None) -> str | None:
    v = a.get(key)
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not _WORD.fullmatch(v) or (allowed and v not in allowed):
        raise BadArgument(f"{key} must be one of {sorted(allowed)}" if allowed
                          else f"{key} must be a lowercase word")
    return v


def _uuid(a: dict, key: str, required: bool = False) -> uuid.UUID | None:
    v = a.get(key)
    if v is None or v == "":
        if required:
            raise BadArgument(f"{key} is required")
        return None
    try:
        return uuid.UUID(str(v))
    except ValueError:
        raise BadArgument(f"{key} must be a UUID") from None


def _since(a: dict) -> dt.datetime | None:
    v = a.get("since")
    if v is None or v == "":
        return None
    if not isinstance(v, str) or len(v) > 40:
        raise BadArgument("since must be an ISO-8601 timestamp")
    try:
        t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise BadArgument("since must be an ISO-8601 timestamp") from None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _bool(a: dict, key: str, default: bool) -> bool:
    v = a.get(key, default)
    if not isinstance(v, bool):
        raise BadArgument(f"{key} must be a boolean")
    return v


# ---- the SQL: every statement the connector can send ---------------------------------
#
# (database key, SQL). Parameters are positional and typed. Every relation is a
# remote_read view (tests/test_mcp_remote_tower_3787.py pins this).

NAMED_QUERIES: dict[str, tuple[str, str]] = {
    "tower_task_runs": ("echo", """
        SELECT * FROM remote_read.tower_task_runs
        WHERE task_id = $1 ORDER BY id DESC LIMIT $2"""),
    "open_tasks": ("echo", """
        SELECT * FROM remote_read.tower_tasks
        WHERE status = ANY($1::text[])
          AND ($2::int IS NULL OR project_id = $2)
          AND ($3::text IS NULL OR gate = $3)
        ORDER BY priority ASC NULLS LAST, updated_at DESC LIMIT $4"""),
    "operator_questions": ("echo", """
        SELECT * FROM remote_read.operator_questions
        WHERE ($1::int IS NULL OR tower_task_id = $1)
          AND ($2::text IS NULL OR state = $2)
          AND ($3::timestamptz IS NULL OR created_at >= $3)
        ORDER BY created_at DESC LIMIT $4"""),
    "notification_outbox": ("echo", """
        SELECT * FROM remote_read.notification_outbox
        WHERE ($1::text IS NULL OR status = $1)
          AND ($2::timestamptz IS NULL OR created_at >= $2)
        ORDER BY created_at DESC LIMIT $3"""),
    "production_assignments": ("anime", """
        SELECT * FROM remote_read.production_assignments
        WHERE ($1::text IS NULL OR status = $1)
        ORDER BY updated_at DESC NULLS LAST LIMIT $2"""),
    "assignment": ("anime", """
        SELECT * FROM remote_read.production_assignments WHERE id = $1"""),
    "assignment_items": ("anime", """
        SELECT * FROM remote_read.production_assignment_items
        WHERE assignment_id = $1 ORDER BY created_at LIMIT 500"""),
    "assignment_submissions": ("anime", """
        SELECT * FROM remote_read.production_assignment_submissions
        WHERE assignment_id = $1 ORDER BY id DESC LIMIT 200"""),
    "assignment_stop_choices": ("anime", """
        SELECT * FROM remote_read.production_assignment_stop_choices
        WHERE stopped_assignment_id = $1 ORDER BY created_at DESC LIMIT 50"""),
    "dispatch_holds": ("anime", """
        SELECT * FROM remote_read.dispatch_holds
        WHERE (NOT $1::bool OR active)
          AND ($2::text IS NULL OR scope_kind = $2)
        ORDER BY placed_at DESC LIMIT $3"""),
    "judge_queue": ("anime", """
        SELECT * FROM remote_read.judge_queue
        WHERE ($1::text IS NULL OR lane = $1)
          AND ($2::int IS NULL OR project_id = $2)
        ORDER BY created_at DESC NULLS LAST LIMIT $3"""),
    "accepted_media": ("anime", """
        SELECT * FROM remote_read.accepted_media
        WHERE ($1::int IS NULL OR project_id = $1)
          AND ($2::timestamptz IS NULL OR reviewed_at >= $2)
        ORDER BY reviewed_at DESC NULLS LAST, video_rendered_at DESC NULLS LAST
        LIMIT $3"""),
    "gpu_claims": ("anime", """
        SELECT * FROM remote_read.gpu_claims
        WHERE (NOT $1::bool OR active)
        ORDER BY claimed_at DESC LIMIT $2"""),
    "governed_executions": ("anime", """
        SELECT * FROM remote_read.render_admission_executions
        WHERE ($1::uuid IS NULL OR shot_id = $1)
          AND ($2::text IS NULL OR outcome = $2)
          AND ($3::timestamptz IS NULL OR created_at >= $3)
        ORDER BY created_at DESC LIMIT $4"""),
    "execution_refusals": ("anime", """
        SELECT * FROM remote_read.render_admission_refusals
        WHERE ($1::uuid IS NULL OR shot_id = $1)
          AND ($2::timestamptz IS NULL OR recorded_at >= $2)
        ORDER BY recorded_at DESC LIMIT $3"""),
    "video_submissions": ("anime", """
        SELECT * FROM remote_read.video_submissions
        WHERE ($1::uuid IS NULL OR shot_id = $1)
          AND ($2::text IS NULL OR outcome = $2)
          AND ($3::timestamptz IS NULL OR created_at >= $3)
        ORDER BY created_at DESC LIMIT $4"""),
}

# Run once per (process, database) before any query: the role must be the
# dedicated one, without superuser/bypass, a member of no role, and unable to
# touch any relation outside remote_read (catalogs aside, which PUBLIC reads).
_ROLE_CHECK_SQL = """
SELECT current_user AS who,
       (SELECT rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb
          FROM pg_roles WHERE rolname = current_user) AS elevated,
       (SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
         WHERE r.rolname = current_user) AS memberships,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'v', 'm', 'p', 'f')
           AND n.nspname NOT IN ('pg_catalog', 'information_schema')
           AND n.nspname <> 'remote_read'
           AND has_table_privilege(current_user, c.oid,
                 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')) AS outside,
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'remote_read'
           AND has_table_privilege(current_user, c.oid,
                 'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')) AS writable,
       (SELECT count(*) FROM pg_namespace n
         WHERE has_schema_privilege(current_user, n.oid, 'CREATE')) AS create_schemas,
       (SELECT rolreplication FROM pg_roles WHERE rolname = current_user) AS replication,
       (SELECT count(*) FROM pg_class c
         WHERE c.relkind = 'S'
           AND has_sequence_privilege(current_user, c.oid, 'USAGE, SELECT, UPDATE')) AS sequences,
       -- Functions that could persist state when called: large-object writers
       -- (PUBLIC by default) and any SECURITY DEFINER function outside pg_catalog.
       (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE ((n.nspname = 'pg_catalog' AND p.proname IN
                   ('lo_creat', 'lo_create', 'lo_from_bytea', 'lo_put', 'lowrite',
                    'lo_truncate', 'lo_truncate64', 'lo_unlink', 'lo_open', 'lo_import',
                    'lo_export', 'be_lo_put', 'be_lo_from_bytea'))
                OR (p.prosecdef AND n.nspname <> 'pg_catalog'))
           AND has_function_privilege(current_user, p.oid, 'EXECUTE')) AS writer_functions,
       (SELECT coalesce(array_agg(c.relname::text ORDER BY c.relname), '{}')
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'remote_read'
           AND has_table_privilege(current_user, c.oid, 'SELECT')) AS readable_views,
       current_setting('transaction_read_only') AS ro
"""

# The exact views the role may read in each database (deploy/sql/remote_read/).
EXPECTED_VIEWS = {
    "echo": frozenset({"tower_tasks", "tower_task_runs", "operator_questions",
                       "notification_outbox"}),
    "anime": frozenset({"production_assignments", "production_assignment_items",
                        "production_assignment_submissions",
                        "production_assignment_stop_choices", "dispatch_holds", "judge_queue",
                        "accepted_media", "gpu_claims", "render_admission_executions",
                        "render_admission_refusals", "video_submissions"}),
}


class NotConfigured(RuntimeError):
    """The read-only database credential is not installed on this host."""


class RoleRefused(RuntimeError):
    """The configured role can do more than read remote_read views."""


def load_db_config(path: Path = DB_FILE) -> dict[str, Any]:
    try:
        st = path.lstat()
    except FileNotFoundError:
        raise NotConfigured("read-only database credential not installed") from None
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise NotConfigured(f"{path.name} must be a regular 0600 file")
    cfg = json.loads(path.read_text())
    if cfg.get("user") != DB_ROLE:
        raise NotConfigured(f"database user must be {DB_ROLE!r}")
    host = str(cfg.get("host") or "")
    if host.startswith("/"):
        pass  # unix socket directory
    else:
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        except ValueError:
            raise NotConfigured("database host must be loopback or a unix socket") from None
    port = int(cfg.get("port") or 5432)
    if not str(cfg.get("password") or ""):
        raise NotConfigured("database password missing")
    return {"host": host, "port": port, "user": DB_ROLE, "password": str(cfg["password"])}


_verified: set[str] = set()


async def _connect(db_key: str):
    import asyncpg
    cfg = load_db_config(DB_FILE)
    return await asyncpg.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
        database=DATABASES[db_key], timeout=5, command_timeout=5,
        server_settings={"default_transaction_read_only": "on",
                         "statement_timeout": "3000",
                         "application_name": "ats-mcp-remote"})


# Tests replace this with a connector to a sandbox cluster or a fake.
connect: Callable[[str], Awaitable[Any]] = _connect


async def _verify_role(conn, db_key: str) -> None:
    if db_key in _verified:
        return
    async with conn.transaction(readonly=True):
        r = await conn.fetchrow(_ROLE_CHECK_SQL)
    problems = []
    if r["who"] != DB_ROLE:
        problems.append("wrong role")
    if r["elevated"]:
        problems.append("elevated role attributes")
    if r["memberships"]:
        problems.append("role inherits other roles")
    if r["outside"]:
        problems.append(f"{r['outside']} relation(s) outside remote_read are accessible")
    if r["writable"]:
        problems.append("remote_read is writable")
    if r["create_schemas"]:
        problems.append("role can CREATE in a schema")
    if r["replication"]:
        problems.append("role has REPLICATION")
    if r["sequences"]:
        problems.append(f"{r['sequences']} sequence(s) usable")
    if r["writer_functions"]:
        problems.append(f"{r['writer_functions']} state-writing function(s) executable")
    if set(r["readable_views"]) != EXPECTED_VIEWS[db_key]:
        problems.append("readable remote_read set differs from the reviewed views")
    if r["ro"] != "on":
        problems.append("session is not read-only")
    if problems:
        raise RoleRefused("; ".join(problems))
    _verified.add(db_key)


async def run_query(name: str, *params: Any) -> list[dict[str, Any]]:
    db_key, sql = NAMED_QUERIES[name]
    conn = await connect(db_key)
    try:
        await _verify_role(conn, db_key)
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


# ---- service state (fixed commands, fixed file) ----------------------------------

SYSTEM_UNITS = (
    "tower-echo-brain.service", "anime-studio.service", "tower-auth.service",
    "comfyui.service", "comfyui-rocm.service", "ollama.service",
    "ollama-embed.service", "ollama-rocm.service",
    "comfyui-nvidia-watchdog.service", "comfyui-rocm-watchdog.service",
    "tower-story-render-feeder.timer", "tower-story-render-feeder.service",
    "tower-autodispatch.timer", "tower-autodispatch.service",
    "tower-clip-ref-drain.timer", "tower-scene-review-drain.timer",
    "tower-vision-drain.timer", "tower-keyframe-replan-drain.timer",
    "tower-inflight-drain.timer",
)
USER_UNITS = ("ats-server.service", "ats-mcp-remote.service")
REPOS = {"echo": "/opt/tower-echo-brain", "anime_studio": "/opt/anime-studio"}
UNIT_PROPS = "ActiveState,SubState,UnitFileState,ActiveEnterTimestamp"

# Only these keys are ever read out of a config source, and only a value that
# matches _SAFE_VALUE is shown; anything else is shown as <redacted>.
AS_FLAG_KEYS = frozenset({
    "ANIME_STUDIO_AUTH_MODE", "ANIME_STUDIO_OPERATOR_MODE",
    "ANIME_STUDIO_OPERATOR_REQUEST_APPROVAL", "ANIME_STUDIO_OPERATOR_REQUEST_MAX_CANDIDATES",
    "ANIME_STUDIO_OPERATOR_REQUEST_MAX_PER_HOUR", "ANIME_STUDIO_PERSONAL_START_IMAGES",
    "H3_VIDEO_ENABLED", "H3_LICENSE_CLEARED", "DASIWA_AUTO_POSTPROCESS",
    "FLUX_MULTICHAR_AUTOROUTE", "FLUX_HIRES", "IDENTITY_FALLBACK_ENABLED",
    "C2_CONTROLNET_ENABLED", "CROSSFAMILY_VIDEO_FILTER_ENABLED",
    "ALLOW_CONTROLNET_SOLO_CHAR", "BLITZ_GPU_FAILOVER", "SKIP_FACE_DETAILER",
})
ECHO_FLAG_KEYS = frozenset({
    "TOWER_EXECUTOR_MAX_EXECUTE", "TOWER_EXECUTOR_START_PAUSED", "AUTONOMY_OVERNIGHT_ONLY",
    "AUTO_APPLY_PAUSE_ON_FAIL", "ENABLE_FILE_OPS", "ENABLE_SYSTEM_COMMANDS",
    "ENABLE_CODE_EXECUTION", "ENABLE_IMAGE_GENERATION", "PROMPT_POISON_AUDIT_ENABLED",
    "DATA_INTEGRITY_AUDIT_ENABLED", "CHARACTER_PROFILE_AUTOFILL_ENABLED",
    "MEDIA_S2_SUPPLEMENT",
})
_SAFE_VALUE = re.compile(r"(?i)(true|false|on|off|yes|no|[0-9]{1,6}|[a-z][a-z0-9_]{0,31})")


def _fixed_commands() -> list[tuple[str, list[str]]]:
    cmds = [(f"unit:{u}", ["systemctl", "show", u, "-p", UNIT_PROPS]) for u in SYSTEM_UNITS]
    cmds += [(f"user-unit:{u}", ["systemctl", "--user", "show", u, "-p", UNIT_PROPS])
             for u in USER_UNITS]
    cmds += [(f"disk-head:{k}", ["git", "-C", p, "rev-parse", "--short=12", "HEAD"])
             for k, p in REPOS.items()]
    cmds.append(("echo-env", ["systemctl", "show", "tower-echo-brain.service",
                              "-p", "Environment"]))
    return cmds


FIXED_COMMANDS = tuple((label, tuple(argv)) for label, argv in _fixed_commands())


async def _run_fixed(argv: tuple[str, ...]) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C",
             "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", ""),
             "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
             "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(Path.home())})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return out.decode("utf-8", "replace")[:20000]


# Tests replace this; production never does.
run_fixed: Callable[[tuple[str, ...]], Awaitable[str]] = _run_fixed


def _flag_values(pairs: dict[str, str], keys: frozenset[str]) -> dict[str, str]:
    return {k: (v if _SAFE_VALUE.fullmatch(v) else "<redacted>")
            for k, v in sorted(pairs.items()) if k in keys}


def parse_env_file(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*", line)
        if m:
            out[m.group(1)] = m.group(2).strip("'\"")
    return out


def parse_systemd_environment(text: str) -> dict[str, str]:
    body = text.partition("Environment=")[2].strip()
    out = {}
    for m in re.finditer(r'"([A-Z0-9_]+)=([^"]*)"|([A-Z0-9_]+)=(\S*)', body):
        k, v = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        out[k] = v
    return out


def read_as_flags(path: Path | None = None) -> dict[str, str]:
    try:
        text = (path or AS_FLAGS_FILE).read_text()
        return _flag_values(parse_env_file(text), AS_FLAG_KEYS)
    except OSError as exc:
        return {"<unreadable>": type(exc).__name__}


# ---- tool catalog -----------------------------------------------------------------

_OPEN = ("pending", "in_progress", "blocked")
GATES = frozenset({"none", "decision", "gpu", "aesthetic", "verdict", "upstream", "verify"})
LANES = frozenset({"video", "keyframe"})
OUTBOX_STATUSES = frozenset({"delivered", "failed", "pending"})
_SINCE = {"type": "string", "format": "date-time"}


def _tool(name: str, description: str, props: dict, required: list[str] | None = None) -> Tool:
    # additionalProperties false (review r2): an unknown argument is refused by
    # the SDK's schema validation before any handler or audit sees it.
    return Tool(name=name, description="Read-only. " + description, annotations=READ_ONLY,
                inputSchema={"type": "object", "properties": props,
                             "additionalProperties": False,
                             **({"required": required} if required else {})})


def _lim(hi: int, default: int = 30) -> dict:
    return {"type": "integer", "minimum": 1, "maximum": hi, "default": default}


TOWER_TOOLS: list[Tool] = [
    _tool("tower_task",
          "One Tower task's full envelope from Echo: description (its authority), "
          "status, gate, notes, relations/dependencies, blocked_by, verified_by, latest "
          "executor claim, and the task's current operator rulings.",
          {"task_id": {"type": "integer", "minimum": 1}}, ["task_id"]),
    _tool("tower_task_runs", "Executor runs recorded for one Tower task, newest first.",
          {"task_id": {"type": "integer", "minimum": 1}, "limit": _lim(50, 10)}, ["task_id"]),
    _tool("open_tasks",
          "Open Tower tasks (pending / in_progress / blocked), by priority. Board view: "
          "no description; use tower_task for a task's authority.",
          {"project_id": {"type": "integer", "minimum": 1},
           "status": {"type": "string", "enum": list(_OPEN)},
           "gate": {"type": "string", "enum": sorted(GATES)}, "limit": _lim(100)}),
    _tool("operator_questions",
          "Questions Echo/agents asked the operator over Telegram and his answers: "
          "lifecycle (sent / delivered / answered) and the chosen option index.",
          {"task_id": {"type": "integer", "minimum": 1}, "state": {"type": "string"},
           "since": _SINCE, "limit": _lim(50)}),
    _tool("notification_outbox",
          "Echo's notification outbox (pushes sent to the operator): title, body "
          "excerpt, channels, delivery status and time. No Telegram inbox, no personal chat.",
          {"status": {"type": "string", "enum": sorted(OUTBOX_STATUSES)}, "since": _SINCE,
           "limit": _lim(50)}),
    _tool("production_assignments",
          "Anime Studio production assignments (records only; not Anime Studio's "
          "derived eligibility). Pass assignment_id for one assignment with its items, "
          "submissions and stop choices.",
          {"assignment_id": {"type": "string", "format": "uuid"},
           "status": {"type": "string"}, "limit": _lim(50)}),
    _tool("dispatch_holds", "Dispatch holds on beats/shots/projects and why.",
          {"active_only": {"type": "boolean", "default": True},
           "scope_kind": {"type": "string"}, "limit": _lim(100)}),
    _tool("judge_queue",
          "Shots awaiting an operator verdict (video lane / keyframe lane): metadata and "
          "artifact hashes, no media, no prompts. machine_review_status is auto-QC, never a "
          "verdict. DB-side membership; see the connector doc for known differences "
          "from the Judge page.",
          {"lane": {"type": "string", "enum": sorted(LANES)},
           "project_id": {"type": "integer", "minimum": 1}, "limit": _lim(200, 50)}),
    _tool("accepted_media",
          "Approved shot clips (records and artifact hashes, no media). operator_verdict "
          "shows whether the approval is the operator's.",
          {"project_id": {"type": "integer", "minimum": 1}, "since": _SINCE,
           "limit": _lim(200, 50)}),
    _tool("gpu_claims", "GPU claim registry: who holds which card, since when, for what.",
          {"active_only": {"type": "boolean", "default": True}, "limit": _lim(100)}),
    _tool("governed_executions",
          "Governed render executions (admission ledger) and recorded refusals, "
          "optionally for one shot.",
          {"shot_id": {"type": "string", "format": "uuid"}, "outcome": {"type": "string"},
           "since": _SINCE, "limit": _lim(100)}),
    _tool("video_submissions", "Durable video submission records, optionally for one shot.",
          {"shot_id": {"type": "string", "format": "uuid"}, "outcome": {"type": "string"},
           "since": _SINCE, "limit": _lim(100)}),
    _tool("service_state",
          "Running revisions (Echo loaded revision, ATS build, disk HEADs), unit and "
          "worker-timer states, and allowlisted configuration flags (incl. auth/operator "
          "mode). Fixed probes only.", {}),
]

TOWER_TOOL_NAMES = frozenset(t.name for t in TOWER_TOOLS)
# Per tool, the string arguments whose value is checked against a FIXED enum
# (the tool's own inputSchema enum): only those values may be logged verbatim
# (#3787 review r1: a free lowercase word in state/outcome/scope_kind is
# client text and is logged as length + hash).
AUDIT_ENUMS: dict[str, dict[str, frozenset[str]]] = {
    t.name: {k: frozenset(v["enum"]) for k, v in t.inputSchema.get("properties", {}).items()
             if isinstance(v, dict) and "enum" in v}
    for t in TOWER_TOOLS}


# ---- handlers ----------------------------------------------------------------------

def _out(title: str, rows: Any, paths: bool = True) -> list[TextContent]:
    n = len(rows) if isinstance(rows, list) else 1
    return [TextContent(type="text", text=f"{title}: {n} row(s)\n{_json(rows, paths)}")]


async def h_tower_task(c, a, echo_url: str):
    tid = _int(a, "task_id")
    if tid is None:
        raise BadArgument("task_id is required")
    r = await c.get(f"{echo_url}/api/tower-tasks/{tid}")
    if r.status_code == 404:
        return [TextContent(type="text", text=f"No Tower task {tid}.")]
    r.raise_for_status()
    return _out(f"tower task #{tid}", r.json(), paths=False)


async def h_tower_task_runs(c, a, echo_url: str):
    tid = _int(a, "task_id")
    if tid is None:
        raise BadArgument("task_id is required")
    return _out("task runs", await run_query("tower_task_runs", tid, _limit(a, 50, 10)),
                paths=False)


async def h_open_tasks(c, a, echo_url: str):
    status = _word(a, "status", frozenset(_OPEN))
    return _out("open tasks", await run_query(
        "open_tasks", [status] if status else list(_OPEN), _int(a, "project_id"),
        _word(a, "gate", GATES), _limit(a, 100)), paths=False)


async def h_operator_questions(c, a, echo_url: str):
    return _out("operator questions", await run_query(
        "operator_questions", _int(a, "task_id"), _word(a, "state"), _since(a),
        _limit(a, 50)))


async def h_notification_outbox(c, a, echo_url: str):
    return _out("outbox", await run_query(
        "notification_outbox", _word(a, "status", OUTBOX_STATUSES), _since(a), _limit(a, 50)))


async def h_production_assignments(c, a, echo_url: str):
    aid = _uuid(a, "assignment_id")
    if aid is None:
        return _out("assignments", await run_query(
            "production_assignments", _word(a, "status"), _limit(a, 50)))
    head = await run_query("assignment", aid)
    if not head:
        return [TextContent(type="text", text=f"No assignment {aid}.")]
    return _out("assignment", {
        "assignment": head[0],
        "items": await run_query("assignment_items", aid),
        "submissions": await run_query("assignment_submissions", aid),
        "stop_choices": await run_query("assignment_stop_choices", aid)})


async def h_dispatch_holds(c, a, echo_url: str):
    return _out("dispatch holds", await run_query(
        "dispatch_holds", _bool(a, "active_only", True), _word(a, "scope_kind"),
        _limit(a, 100)))


async def h_judge_queue(c, a, echo_url: str):
    return _out("judge queue", await run_query(
        "judge_queue", _word(a, "lane", LANES), _int(a, "project_id"), _limit(a, 200, 50)))


async def h_accepted_media(c, a, echo_url: str):
    return _out("accepted media", await run_query(
        "accepted_media", _int(a, "project_id"), _since(a), _limit(a, 200, 50)))


async def h_gpu_claims(c, a, echo_url: str):
    return _out("gpu claims", await run_query(
        "gpu_claims", _bool(a, "active_only", True), _limit(a, 100)))


async def h_governed_executions(c, a, echo_url: str):
    shot, since, lim = _uuid(a, "shot_id"), _since(a), _limit(a, 100)
    return _out("governed executions", {
        "executions": await run_query("governed_executions", shot, _word(a, "outcome"),
                                      since, lim),
        "refusals": await run_query("execution_refusals", shot, since, lim)})


async def h_video_submissions(c, a, echo_url: str):
    return _out("video submissions", await run_query(
        "video_submissions", _uuid(a, "shot_id"), _word(a, "outcome"), _since(a),
        _limit(a, 100)))


def _parse_show(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


async def h_service_state(c, a, echo_url: str, ats_url: str):
    state: dict[str, Any] = {"units": {}, "disk_head": {}, "flags": {}}
    for label, argv in FIXED_COMMANDS:
        out = await run_fixed(argv)
        kind, _, name = label.partition(":")
        if kind in ("unit", "user-unit"):
            props = _parse_show(out)
            state["units"][name] = {k: props.get(k, "") for k in UNIT_PROPS.split(",")}
        elif kind == "disk-head":
            head = out.strip()
            state["disk_head"][name] = head if re.fullmatch(r"[0-9a-f]{7,40}", head) else None
        elif label == "echo-env":
            state["flags"]["echo"] = _flag_values(parse_systemd_environment(out),
                                                  ECHO_FLAG_KEYS)
    state["flags"]["anime_studio"] = read_as_flags()
    try:
        r = await c.get(f"{echo_url}/health", timeout=5)
        h = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        state["echo_health"] = {"http": r.status_code, "status": h.get("status"),
                                "loaded_revision": h.get("loaded_revision")}
    except Exception as exc:  # noqa: BLE001 -- reported, never raised
        if type(exc).__name__ in ("UpstreamRefused", "UpstreamTooLarge"):
            raise
        state["echo_health"] = {"error": type(exc).__name__}
    try:
        r = await c.get(f"{ats_url}/api/version", timeout=5)
        v = r.json()
        state["ats"] = {k: v.get(k) for k in ("component", "commit", "dirty", "built_at",
                                              "version", "revision_source")}
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ in ("UpstreamRefused", "UpstreamTooLarge"):
            raise
        state["ats"] = {"error": type(exc).__name__}
    state["notes"] = [
        "anime_studio has no loaded-revision route reachable without operator authority; "
        "disk_head plus the unit's ActiveEnterTimestamp is reported instead",
        "flags are allowlisted keys only; any value that is not a plain word/number "
        "is shown as <redacted>",
    ]
    return _out("service state", state)


TOWER_HANDLERS = {
    "tower_task": h_tower_task,
    "tower_task_runs": h_tower_task_runs,
    "open_tasks": h_open_tasks,
    "operator_questions": h_operator_questions,
    "notification_outbox": h_notification_outbox,
    "production_assignments": h_production_assignments,
    "dispatch_holds": h_dispatch_holds,
    "judge_queue": h_judge_queue,
    "accepted_media": h_accepted_media,
    "gpu_claims": h_gpu_claims,
    "governed_executions": h_governed_executions,
    "video_submissions": h_video_submissions,
    "service_state": h_service_state,
}
assert set(TOWER_HANDLERS) == TOWER_TOOL_NAMES
DB_TOOLS = TOWER_TOOL_NAMES - {"tower_task", "service_state"}
