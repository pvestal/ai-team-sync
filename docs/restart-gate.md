# Restart safety gate (#3580)

Operator ruling 2026-10-13. At 17:06:13 PDT a raw `sudo systemctl restart anime-studio`
ran while `POST /api/scenes/beats/899/story-render?dispatch=true` was in flight. uvicorn's
graceful shutdown happened to finish that request (200 at 17:06:15). Nothing let the
restarter see the request, nothing stopped new requests arriving between "looks idle" and
the restart, and `service_restarts` has no row for it (or for 16:16:38).
`routers/restarts.py` records restarts but does not gate them. The `record_restart` and
`preflight` MCP tools are advisory only.

This gate covers restarts made through it, and nothing else. The first section below says
which restarts that is.

## The service-control boundary

### What is enforced

| Mechanism | Where | What it stops |
|---|---|---|
| Lease, one active per unit, enforced by the database | `routers/restart_leases.py`, `restart_lease_model.py` (UNIQUE `active_unit`) | Two callers both being cleared to restart the same unit. |
| Probes that fail closed | `restart_gate.py` `run_probe` / `unit_policy` | A gated restart while requests are in flight, active jobs exist, or a GPU queue is non-empty. A probe that is down, times out, returns non-200 or malformed JSON, or reports `active_jobs_ok=false` causes a refusal. So do an unreadable config and a governed unit with no probes. |
| Drain inside the service | anime-studio `packages/core/inflight.py` | New mutating requests between the check and the restart. Once the drain starts they get 503 with `Retry-After` and are not counted. The drain check and the in-flight increment run in one event-loop step. |
| `ats safe-restart` | `safe_restart.py`, `cli.py` | Runs lease, drain, wait for clear, restart, then verifies (active, new MainPID, health URL) and records the outcome. It exits non-zero on any failed check. |
| Bash PreToolUse hook | `hooks/pre_tool_use_restart_guard.py` | An agent's Claude Code `Bash` tool call that runs raw service control on a governed unit. |
| Evidence | `service_restarts` | Each attempt gets a row: `refused` (with the failing probe), `authorized` or `aborted`. Each authorized attempt also gets an outcome row: `completed`, `failed`, `timeout`, `aborted` or `unknown`. |

### What cannot be enforced (and is not claimed)

- **The operator's own terminal**, and any root or sudo shell. `patrick` has
  `NOPASSWD: /usr/bin/systemctl restart *` and `stop *` (`sudo -n -l`). The OS lets this
  account restart any unit with no gate.
- **The hook sees only one path.** That path is the command string an agent passes to
  Claude Code's Bash tool. The hook does not see:
  - commands inside a script file (`bash deploy.sh`)
  - Python `subprocess`
  - `systemctl` called from another tool or MCP server
  - D-Bus (`busctl`, `dbus-send`, `gdbus` to `org.freedesktop.systemd1`)
  - Codex or other agents without this hook
  - `xargs` or other indirection that builds the unit name at run time
  - `kill $(pgrep ...)`, because the PIDs are not known when the command is parsed
  - commands it cannot tokenize (allowed with a warning)
- **cron, systemd timers and watchdogs.** The `comfyui-nvidia-watchdog` and
  `comfyui-rocm-watchdog` timers (`/opt/anime-studio/scripts/comfyui_nvidia_watchdog.sh`)
  run `systemctl restart` on an enabled-but-inactive or failed ComfyUI. `tower-watchdog`
  reboots the host when sshd wedges.
- **systemd itself.** `Restart=on-failure` on `anime-studio` restarts it after a crash,
  as does an OOM kill or `systemd-oomd`.
- **Deploy scripts and humans** that do not call `ats safe-restart`.
- **Work the probes cannot see.** This includes `asyncio.create_task` work inside the API
  that is attached to no request. For units with no drain endpoint, ComfyUI can be sent
  new prompts between the last clear probe and the restart, because ComfyUI has no drain.

### What a real OS boundary would take

1. Remove the blanket `NOPASSWD: systemctl restart|stop *`, `kill`, `pkill` and
   `killall` grants from `patrick` in sudoers.
2. Add a polkit rule (`org.freedesktop.systemd1.manage-units`) that denies governed units
   to everyone except one dedicated account or a small setuid-free helper.
3. Make that helper the only restart path. It would take the lease itself before it calls
   systemd.

As long as the agent's account has passwordless sudo, an agent can get around any of this.
The gate only constrains agents that use the hook and the wrapper. A sudoers or polkit
change is the operator's decision and is not part of this branch.

## Lease flow

```
ats safe-restart <unit> --reason "..."
  POST /api/restarts/leases {unit, reason, session_id}
      UNIQUE(active_unit) insert          -> 409 + 'refused' row if already leased
      probes (inflight, comfyui, health)  -> 409 busy / 503 unavailable + 'refused' row
      -> 201 {lease, policy}
  systemctl show MainPID                  (old pid)
  POST /api/restarts/leases/{id}/drain
      service POST /api/system/drain {lease_id, ttl_s = lease remaining}
      poll probes (expect_drain) until all clear, bounded by drain_wait_s
      -> 200 cleared + 'authorized' row  |  409/503 + 'refused' row + drain released
  sudo -n systemctl restart <unit>        (systemctl --user for scope = "user")
  verify ActiveState=active, MainPID changed, health_url 2xx (bounded)
  POST /api/restarts/leases/{id}/complete {outcome, old_pid, new_pid, after}
      -> outcome row 'completed' | 'failed' | 'timeout'; drain released
```

- **Expiry.** A live lease past its TTL is settled lazily: on the next acquire for that
  unit, or on GET or any call on that lease. If it had cleared, it writes `unknown`.
  Otherwise it writes `refused`. In both cases it releases the drain. The service does not
  depend on that: its drain carries the same TTL and lifts itself (acceptance T2f).
- **Release.** `POST /api/restarts/leases/{id}/release` gives up a lease without
  restarting and records `aborted`.

Exit codes of `ats safe-restart`:

| Code | Meaning |
|---|---|
| 0 | Restarted and verified |
| 1 | Restarted, but it failed or timed out |
| 2 | Refused because something is busy |
| 3 | Refused because a check was unavailable, or by policy |
| 4 | Local error before the restart was issued |
| 5 | Restarted, but the outcome was not recorded |

## Configuration (`~/.config/ai-team-sync/operator.toml`)

**Deploy order matters.** An ATS install older than this branch rejects any unknown
top-level key in `operator.toml`. Every hook that reads `governance.repositories` would
then fail. Upgrade every ATS install, including the pipx copy that hooks run from, before
you add `[restart_gate]`.

```toml
[restart_gate]
# Optional. Replaces the defaults: anime-studio, tower-echo-brain, comfyui,
# comfyui-rocm, keyframe-blitz-worker. A configured unit is always governed.
# governed_units = ["anime-studio", "tower-echo-brain", "comfyui", "comfyui-rocm", "keyframe-blitz-worker"]

[restart_gate.units."anime-studio"]
scope = "system"
health_url = "http://127.0.0.1:8401/api/system/health"
drain_url = "http://127.0.0.1:8401/api/system/drain"
release_url = "http://127.0.0.1:8401/api/system/drain/release"
drain_token_file = "/home/patrick/.config/ai-team-sync/drain_token"   # 0600, same value as anime-studio's ATS_DRAIN_TOKEN
lease_ttl_s = 180
drain_wait_s = 60
verify_timeout_s = 120
probes = [
  { name = "api", kind = "inflight", url = "http://127.0.0.1:8401/api/system/inflight" },
  { name = "comfyui-nvidia", kind = "comfyui", url = "http://127.0.0.1:8188/queue" },
  { name = "comfyui-rocm", kind = "comfyui", url = "http://127.0.0.1:8189/queue" },
]

[restart_gate.units."comfyui"]
scope = "system"
health_url = "http://127.0.0.1:8188/system_stats"
probes = [ { name = "queue", kind = "comfyui", url = "http://127.0.0.1:8188/queue" } ]
```

Things to know about this config:

- Units with no drain endpoint, such as `comfyui`, are gated on probes only. See the
  residual gaps above.
- `tower-echo-brain` and `keyframe-blitz-worker` have no in-flight endpoint. Until someone
  writes probes for them, they stay governed and every lease for them is refused. Agents
  cannot restart them; the operator can.
- Probe kinds:
  - `inflight`: the anime-studio contract `{in_flight, routes, draining, drain, active_jobs, active_jobs_ok}`.
  - `comfyui`: `{queue_running: [], queue_pending: []}`.
  - `health`: any 2xx.

## Hook wiring (docs only: ATS does not edit `~/.claude/settings.json`)

```json
"PreToolUse": [
  { "matcher": "Bash",
    "hooks": [{ "type": "command",
      "command": "/home/patrick/.local/share/pipx/venvs/ai-team-sync/bin/python -m ai_team_sync.hooks.pre_tool_use_restart_guard" }] }
]
```

Behaviour:

- **Blocked:** exit 2, with stderr naming `ats safe-restart`.
- **Unparseable:** exit 0, with a warning added as `additionalContext`.
- **Unreadable config:** the default governed units are still guarded.

## Tests

- `tests/test_restart_gate_3580.py`: policy, probe interpretation, leases over REST
  (including the concurrent race on a file database), and the safe-restart driver with
  fake systemd.
- `tests/test_restart_guard_hook_3580.py`: the T2g matrix and the hook process protocol.
- `tests/test_restart_gate_acceptance_3580.py` (opt-in, `ATS_RESTART_ACCEPTANCE=1`):
  T2a–T2f against a transient `systemd-run --user` unit that mounts the real anime-studio
  middleware, a real ats-server on a temp DB, and fault-injection HTTP servers. It never
  touches a production unit.
