# Remote read-only ATS MCP (#3760)

Lets a remote MCP client (claude.ai chat, including voice mode) READ ATS
context: `task_brief`, `get_decision_history`, `team_status`,
`get_session_details`, `list_all_locks`, `ats_version`. It has no write tools
and cannot act as any session.

Not on this surface (operator ruling 2026-11-21): `preflight`, and semantic
memory recall inside `task_brief`. Both run Echo memory search, which refreshes
Echo's Redis result cache and bumps Qdrant access metadata on every call. They
return once Echo search takes a read-only flag (follow-up ticket). Until then
the remote brief carries ATS context, the ticket envelope and its rulings, but
no recalled memories.

## Shape

```
claude.ai ──HTTPS──▶ Tailscale Funnel (tower.<tailnet>.ts.net:443, path /mcp)
                         │  (tailscaled, a system service; no router port opened)
                         ▼
             127.0.0.1:8402  ats-mcp-remote (user unit)
               1. BearerGate  — 401 unless `Authorization: Bearer <secret>`
               2. /mcp only   — every other path 404
               3. tool allowlist (6 read tools)
               4. upstream allowlist — only GET reads + POST /api/brief on
                  ats-server can leave the process; nothing goes to Echo
                  directly (the brief's own Echo lookups are reads)
                         ▼
             ats-server 127.0.0.1:8400
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

## Secret

- Header: `Authorization: Bearer <secret>` (a standard header name that
  claude.ai custom connectors may send).
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
