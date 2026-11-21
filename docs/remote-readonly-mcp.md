# Remote read-only ATS MCP (#3760)

Lets a remote MCP client (claude.ai chat, including voice mode) READ ATS
context: `task_brief`, `preflight`, `get_decision_history`, `team_status`,
`get_session_details`, `list_all_locks`, `ats_version`. It has no write tools
and cannot act as any session.

## Shape

```
claude.ai ──HTTPS──▶ Tailscale Funnel (tower.<tailnet>.ts.net:443, path /mcp)
                         │  (tailscaled, a system service; no router port opened)
                         ▼
             127.0.0.1:8402  ats-mcp-remote (user unit)
               1. BearerGate  — 401 unless `Authorization: Bearer <secret>`
               2. /mcp only   — every other path 404
               3. tool allowlist (7 read tools)
               4. upstream allowlist — only GET reads + POST /api/brief (ATS)
                  and POST /api/preflight (Echo) can leave the process
                         ▼
             ats-server 127.0.0.1:8400 / Echo Brain 127.0.0.1:8309
```

Code: `src/ai_team_sync/mcp/remote_readonly.py`. Guard tests:
`tests/test_mcp_remote_readonly.py` — `test_no_mutating_tool_on_the_remote_transport`
fails if any mutating tool is listed on this transport, and
`test_every_stdio_tool_is_classified` fails when a new stdio tool is added
without a read/mutating classification.

`preflight` is advisory but Echo records each request as a `preflight_request`
row (its id is in the answer). That is an audit log entry, not state any
agent acts on.

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
