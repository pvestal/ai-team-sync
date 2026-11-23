# Remote read-only ATS MCP (#3760, #3787)

Lets a remote MCP client (claude.ai chat, including voice mode) READ ATS
context: `task_brief`, `get_decision_history`, `team_status`,
`get_session_details`, `list_all_locks`, `ats_version`; and, since #3787,
Echo task / question / outbox records, Anime Studio production ledgers, GPU
claims, governed executions and service state (see "Tower context tools"). It
has no write tools and cannot act as any session.

Not on this surface (operator ruling 2026-11-21): `preflight`, and semantic
memory recall inside `task_brief`. Both run Echo memory search, which refreshes
Echo's Redis result cache and bumps Qdrant access metadata on every call. They
return once Echo search takes a read-only flag (follow-up ticket). Until then
the remote brief carries ATS context, the ticket envelope and its rulings, but
no recalled memories.

## Tower context tools (#3787 Phase 1)

Thirteen more read tools, all annotated `readOnlyHint: true`,
`destructiveHint: false`, `openWorldHint: false`
(`src/ai_team_sync/mcp/remote_tower.py`):

| Tool | Source |
|---|---|
| `tower_task` | Echo `GET /api/tower-tasks/{id}` (SELECT-only envelope: description, relations, rulings, latest claim) |
| `tower_task_runs`, `open_tasks`, `operator_questions`, `notification_outbox` | `echo_brain.remote_read.*` views |
| `production_assignments`, `dispatch_holds`, `judge_queue`, `accepted_media`, `gpu_claims`, `governed_executions`, `video_submissions` | `anime_production.remote_read.*` views |
| `service_state` | fixed `systemctl show` / `git rev-parse` argv, Echo `/health`, ATS `/api/version`, allowlisted keys of `/etc/anime-studio/flags.env` and of tower-echo-brain's Environment |

Boundaries:

- **Anime Studio's HTTP API is never called.** Under
  `ANIME_STUDIO_AUTH_MODE=single_user` every request to :8401 is the configured
  operator, and several of its GET routes write (QC kicks, lazy `UPDATE shots`,
  the GPU arbiter sweep, filmstrip builds). The upstream allowlist has no
  Anime Studio origin, and a test pins that.
- **Origins are pinned in code** (ATS `http://127.0.0.1:8400`, Echo
  `http://127.0.0.1:8309`): the process refuses to start, and the transport
  refuses every request, if `ATS_SERVER_URL` / `ATS_REMOTE_ECHO_URL` name any
  other origin.
- **Echo** is reached for exactly `GET /api/tower-tasks/{int}` and
  `GET /health` (whose `database_user` is dropped). Not Echo's MCP tools: every
  Echo MCP call inserts a telemetry row.
- **Database:** role `tower_remote_ro` (deploy/sql/remote_read/00_roles.sql)
  can read only the `remote_read` views; the views are owned by a NOLOGIN
  role that holds SELECT on exactly the base tables they name, and each view
  is a column allowlist (no paths, prompts, tokens, Telegram ids or chat
  bodies; `echo_telegram_messages` and `telegram_inbox` are not exposed). The
  connector sends only the literal SELECTs in `NAMED_QUERIES`, each in a READ
  ONLY transaction, and before first use per database refuses a role that is
  elevated or has REPLICATION, inherits a role, can touch any relation or
  sequence outside `remote_read`, can write `remote_read`, can read anything in
  `remote_read` other than the exact reviewed views, can CREATE in a schema,
  can execute a state-writing function (large-object writers, any SECURITY
  DEFINER function outside pg_catalog), or is not read-only by default.
  The migrations grant each view by name (never `ALL TABLES IN SCHEMA`),
  refuse to run if `remote_read` already holds any unreviewed object, and
  revoke PUBLIC EXECUTE on the large-object writer functions (0 large objects
  and no code using them in either database, measured 2026-11-23; the
  rollback re-grants them).
- **Redaction:** a value under any key naming a credential or personal id
  (token, secret, password, api_key, chat_id, user_id, telegram_*id, nonce,
  ...) is withheld whatever its type; the same keys written inside a string are
  withheld in every quoting form (bare `key: value`, `key=value`, Python repr,
  JSON, escaped JSON): a quoted value up to its real closing quote, an unquoted
  value to the end of its line (never cut at a space). Strong keys (password,
  passphrase, secret, credential, cookie, private_key, api_key) always
  withhold; the other keys (token, authorization, nonce, chat_id, user_id,
  signature) withhold after `=`, or after `:` when the first word is
  credential-shaped (an auth scheme, a digit, >=16 chars, or >=8 chars with
  `+/=_-~.`), so ruling prose like "operator authorization: Codex review" is
  kept. PEM private-key blocks are withheld whole; credential-shaped text (bearer/JWT/`sk-`/`ghp_`/Vault
  tokens, `password=` pairs) is replaced everywhere. Absolute paths are
  withheld from the leading `/` to the next delimiter, spaces and Unicode
  included (a path that may contain spaces cannot be cut at a space); Echo
  task text keeps repo citations as repo-relative labels (`echo:src/...`,
  `anime-studio:packages/...`) and HTTP routes (`/api/...`) as written.
  Hashes are never redacted.
- **Per-client secrets:** the #3760 secret is client `claude-ai`; each further
  client has its own `<client>.token` (0600) in
  `~/.config/ai-team-sync/remote-mcp.clients.d/` (0700). Secrets must differ,
  may not start with `bearer` or contain whitespace (either would make two
  clients' accepted forms collide), and a credential that matches more than
  one client is refused (401), never attributed by order.
- **Rate limits:** per client, 60 tool calls/min and 30 database tool
  calls/min; over the limit is a tool error.
- **Audit:** one JSON line per call (exception: a client over its rate limit
  gets at most one `rate_limited` line per 10 s, and the next such line carries
  `suppressed_refusals`, the number of refused calls it stands for) to
  `$ATS_STATE_DIR/audit.jsonl` (0600,
  rotated once at 10 MiB) and the journal: time, client, tool, outcome, ms,
  output size, and arguments (integers/booleans and fixed-shape enum words
  verbatim only when the value is in that tool's fixed schema enum; any other
  string as length + sha256 prefix). An unlisted tool
  name is logged as `<unlisted>` with its length. Library log messages
  (mcp, uvicorn) are written as `<message withheld>`, and uvicorn's access log
  is off, because both echoed client text. Arguments are validated against
  each tool's schema by the connector itself, after the rate limit and inside
  the audited call (the SDK's own pre-callback validation is off), so a
  malformed call is limited and audited like any other.

Known differences, stated rather than hidden:

- `judge_queue` is DB-side membership mirroring Anime Studio 776e213d9's
  queue predicates (cited in the SQL). It omits the keyframe queue's
  legacy-linked arm (a Python-computed id list) and does not check files on
  disk or resolve missing video identities, which the Judge page does lazily
  (and with writes).
- `production_assignments` returns records, not Anime Studio's derived
  eligibility (out of Phase 1).
- Anime Studio exposes no loaded revision without operator authority;
  `service_state` reports its disk HEAD and unit start time instead.
- The role can still create a session-local TEMP table if a session turns its
  own read-only default off (PostgreSQL grants TEMPORARY to PUBLIC); the
  connector never sends such SQL. Optional hardening at release:
  `REVOKE TEMPORARY ON DATABASE <db> FROM PUBLIC` (affects every role relying
  on PUBLIC).

SQL sandbox (no production rows; schema-only dump of the base tables):

```bash
deploy/sql/remote_read/sandbox.sh /tmp/rr-sandbox      # prints the env line
REMOTE_READ_SANDBOX_HOST=<socket dir> pytest tests/test_remote_read_sql_3787.py
```

## Shape

```
claude.ai ──HTTPS──▶ Tailscale Funnel (tower.<tailnet>.ts.net:443, path /mcp)
                         │  (tailscaled, a system service; no router port opened)
                         ▼
             127.0.0.1:8402  ats-mcp-remote (user unit)
               1. BearerGate  — 401 unless `Authorization: Bearer <secret>`
               2. /mcp only   — every other path 404
               3. tool allowlist (6 ATS + 13 Tower read tools, #3787)
               4. upstream allowlist — only GET reads + POST /api/brief on
                  ats-server, and GET /api/tower-tasks/{id} + GET /health on
                  Echo, can leave the process; never Anime Studio
               5. remote_read views via role tower_remote_ro (#3787)
                         ▼
             ats-server 127.0.0.1:8400 · Echo 127.0.0.1:8309 · PostgreSQL
```

Code: `src/ai_team_sync/mcp/remote_readonly.py`. Guard tests:
`tests/test_mcp_remote_readonly.py` — `test_no_mutating_tool_on_the_remote_transport`
fails if any mutating tool is listed on this transport, and
`test_every_stdio_tool_is_classified` fails when a new stdio tool is added
without a read/mutating classification.

Bounds: request bodies over 64 KiB are refused (413, or the body is cut off
when chunked); upstream answers must be uncompressed (`Accept-Encoding:
identity`; any other content-encoding is refused) and over 2 MiB are refused
while streaming; tool
text is cut at 60,000 characters; uvicorn answers 503 beyond 16 concurrent
connections. `tests/test_mcp_remote_readonly.py` pins the exact upstream
allowlist and each tool's exact upstream requests as literals.

## Refusal log

Every 401 is logged to the `ats-mcp-remote` journal as one line, for example:

```
WARNING:ats_remote_mcp.auth:401 POST /mcp auth_headers=1 value_len=71 scheme=bearer credential_len=64 expected_len=64 len_match=True xff=anthropic funnel=yes
```

`auth_headers` is how many Authorization headers arrived (0 = the client sent
none); `value_len` is the raw header value's total length. `scheme` is the lowercased scheme name if it is one of bearer / basic /
digest / token / negotiate, `<none>` for a value without a scheme, otherwise
`<other>`. Lengths are of the credential only. `xff` classifies each
forwarded client address (up to three): `anthropic` if it is inside
one of Anthropic's published outbound ranges (160.79.104.0/21,
153.61.208.0/20), otherwise `v4` / `v6`, or
`<invalid>`. The address itself is never logged: it is client-chosen bits, and
a valid IPv6 address can carry 32 hex characters of anything. `funnel` says
whether Tailscale marked the request as Funnel traffic. Method
and path are logged only when they are a standard method and `/mcp`; anything
else is `<other>`. Every field is a number or a member of a fixed set, so no
client-sent text (and no credential) reaches the log.

```bash
journalctl --user -u ats-mcp-remote --since -15min | grep ats_remote_mcp.auth
```

## Secret

- Header: `Authorization: Bearer <secret>` (a standard header name that
  claude.ai custom connectors may send). Per RFC 7235 (`auth-scheme 1*SP
  token68`) the scheme is matched case-insensitively, one or more spaces may
  separate it from the secret, and spaces/tabs around the whole value are
  ignored. A tab, CR, LF, VT or FF inside the value is refused, and the secret
  itself must match exactly. The bare secret as the whole value (no scheme) is also
  accepted, because claude.ai's connector sends it that way.
- File: `~/.config/ai-team-sync/remote-mcp.token`, mode 0600, outside the repo.
  The server refuses to start if the file is missing, not 0600, or shorter
  than 32 characters.

### Rotate

```bash
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' \
  > ~/.config/ai-team-sync/remote-mcp.token.new
mv ~/.config/ai-team-sync/remote-mcp.token.new ~/.config/ai-team-sync/remote-mcp.token
systemctl --user restart ats-mcp-remote
```

Then paste the new value into the claude.ai connector (`Bearer <value>`).
The old value stops working at the restart.

## Public route (Tailscale Funnel)

One-time tailnet prerequisites (admin console): HTTPS certificates enabled
(DNS page) and the `funnel` node attribute granted to this node in the
policy file. Then:

```bash
sudo tailscale funnel --bg --set-path /mcp http://127.0.0.1:8402/mcp
tailscale funnel status
```

tailscaled persists the funnel config across reboots. Turn it off with
`sudo tailscale funnel --https=443 --set-path /mcp off`.

## Install / deploy

`scripts/deploy.sh` installs the `ats-mcp-remote` entry point with the rest of
the package. First install of the unit:

```bash
cp deploy/ats-mcp-remote.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now ats-mcp-remote
```

## Prove the gate

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<funnel-host>/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'      # → 401
```
