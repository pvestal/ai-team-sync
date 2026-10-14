# Restart safety gate (#3580)

Operator ruling 2026-10-13. At 17:06:13 PDT a raw `sudo systemctl restart anime-studio`
ran while `POST /api/scenes/beats/899/story-render?dispatch=true` was in flight. uvicorn's
graceful shutdown happened to finish that request (200 at 17:06:15).

Three things were missing:

- Nothing let the restarter see the request.
- Nothing stopped new requests from arriving between "looks idle" and the restart.
- `service_restarts` has no row for it, or for the one at 16:16:38.

`routers/restarts.py` records restarts but does not gate them. The `record_restart` and
`preflight` MCP tools are advisory only.

This gate applies only to restarts made through it. The section below says exactly
which restarts those are.

## The service-control boundary

### What is enforced

| Mechanism | Where | What it stops |
|---|---|---|
| Required evidence per unit | `restart_gate.py` `_parse_unit` / `load_gate_config` | A weak or partial gate. Every governed unit must declare `inflight`, `jobs`, `gpu_queue` and `drain` evidence. A missing class makes the whole config invalid, so every unit is refused. The only way out of a class is an explicit `waive` entry with a written reason, which is shown in every lease response and recorded on the attempt row. |
| One active lease per unit, enforced by the database | `restart_lease_model.py` (UNIQUE `active_unit`) | Two callers both being cleared to restart the same unit. |
| Probes that fail closed | `restart_gate.py` `run_probe` / `run_probes` | A gated restart while requests are in flight, jobs are active, or a GPU queue is non-empty. Each of these also refuses: a probe that is down, times out, returns non-200 or malformed JSON; `active_jobs_ok=false`; empty job evidence (unless waived); and a service that cannot prove it is a single worker. |
| Drain inside the service | anime-studio `packages/core/inflight.py` | New mutating requests between the check and the restart: they get 503 with `Retry-After` and are not counted. The drain check and the in-flight increment happen in one event-loop step. The drain is mandatory unless waived. |
| Final round plus a check at commit time | `routers/restart_leases.py` `drain_lease` | Clearing a lease on stale evidence. A lease becomes `cleared` only after a complete, concurrent probe round shows all of: `draining` is true for this lease, `in_flight` is 0, jobs are 0, and GPU queues are empty. The clearing UPDATE also requires `state='draining' AND expires_at > now`, inside the same transaction as the attempt row. |
| Lease and drain renewal | `safe_restart.py` `Heartbeat`, `POST /leases/{id}/renew` | The lease or drain expiring under a slow `systemctl restart` or slow verification. Config load rejects any `lease_ttl_s` that is not greater than `drain_wait + restart + verify + 10s`. |
| Outcome on every exit path | `safe_restart.py`, plus `start_sweeper()` in `routers/restart_leases.py` | An authorized attempt with no outcome. The wrapper catches exceptions and SIGTERM/SIGINT and reports `failed` or `aborted`. For SIGKILL, the server's sweeper settles the expired lease as `unknown` and releases the drain within one interval (`ATS_RESTART_SWEEP_S`, default 15 s). |
| In-process GPU producers | `inflight.is_draining()`, plus `docs/patches/3580_gateway_drain_check.diff` (anime-studio) | New GPU submissions from inside the API while it drains. See the deploy notes. |
| `ats safe-restart` | `safe_restart.py`, `cli.py` | The full sequence: lease, drain, wait, restart, then verify that the unit is active with a new MainPID and its health URL answers. Records the outcome. |
| Bash PreToolUse hook | `hooks/pre_tool_use_restart_guard.py` | An agent's Claude Code `Bash` tool call that runs raw service control on a governed unit. |
| Evidence | `service_restarts` | One attempt row per attempt (`refused`, `authorized` or `aborted`) and one outcome row per authorized attempt (`completed`, `failed`, `timeout`, `aborted` or `unknown`). Both rows carry the mode and the waivers. |

### What cannot be enforced (and is not claimed)

- **The operator's own terminal**, and any root or sudo shell. `patrick` has
  `NOPASSWD: /usr/bin/systemctl restart *` and `stop *`, `/bin/kill`, `/usr/bin/pkill` and
  `killall *` (`sudo -n -l`). The OS lets this account restart any unit with no gate.
- **Anything outside the hook's single path.** The hook sees only the command string an
  agent passes to Claude Code's Bash tool. It does not see:
  - commands inside a script file (`bash deploy.sh`)
  - Python `subprocess`
  - `systemctl` called by another tool or an MCP server
  - D-Bus calls (`busctl`, `dbus-send` or `gdbus` to `org.freedesktop.systemd1`)
  - Codex, or other agents that don't run this hook
  - `xargs`
  - `kill $(pgrep ...)`
  - commands it cannot tokenize (these are allowed, with a warning)
- **cron, systemd timers and watchdogs.**
  - `comfyui-nvidia-watchdog` and `comfyui-rocm-watchdog` run `systemctl restart` on an
    enabled-but-inactive or failed ComfyUI.
  - `tower-watchdog` reboots the host when sshd wedges.
- **systemd itself.** `Restart=on-failure` restarts `anime-studio` after a crash, and so do
  OOM kills and `systemd-oomd`.
- **Deploy scripts and people** who don't use `ats safe-restart`.
- **Direct ComfyUI submitters outside the API's GPU gateway**, such as other processes,
  scripts and the ComfyUI UI. The GPU-queue probe sees their work but cannot stop it from
  being submitted after the last probe round.
- **The single-worker proof** is the service's own report from `/proc` (parent process and
  sibling workers). It does not cross-check against the unit's MainPID.

### What a real OS boundary would take

1. Remove the blanket `NOPASSWD: systemctl restart|stop *`, `kill`, `pkill` and `killall`
   grants for `patrick` from sudoers.
2. Add a polkit rule (`org.freedesktop.systemd1.manage-units`) that denies governed units
   to everyone except one dedicated account or a small setuid-free helper.
3. Make that helper the only restart path. It would take the lease itself.

As long as the agent's account has passwordless sudo, nothing here stops it. The gate
constrains agents that use the hook and the wrapper. Changing sudoers or polkit is the
operator's decision.

## Lease flow (normal)

```
ats safe-restart <unit> --reason "..."
  POST /api/restarts/leases              UNIQUE insert -> probes (concurrent round)
      -> 201 {lease (mode, waivers), policy (waivers)} | 409/503 + 'refused' row
  POST /api/restarts/leases/{id}/drain   service drain (ttl = lease remaining) ->
      rounds until a FINAL round is clear AND shows draining for THIS lease ->
      conditional UPDATE (state='draining' AND expires_at > now) + 'authorized' row
  -- authorized: heartbeat renews lease + drain every ttl/3; signals report --
  sudo -n systemctl restart <unit>       (systemctl --user for scope = "user")
  verify: active, new MainPID, health 2xx  (own budget, starting when systemctl returns)
  POST /api/restarts/leases/{id}/complete {outcome} -> outcome row; drain released
```

The new process is not draining, so writes resume there at once. A renewal sent during
the restart may fail to reach the old process while it stops. That is reported and is not
fatal.

## Bootstrap: the ONLY sanctioned first-load path

The first restart after this ships is the one that loads the middleware. Before it, there
is no `/api/system/inflight` and no drain. For that restart, use
`ats safe-restart <unit> --bootstrap --reason "..."`. It takes a lease in `bootstrap` mode
and refuses unless all of these hold:

1. **The middleware is absent.** The inflight URL returns 404. If it returns 200, the
   attempt is refused and the normal path must be used. Bootstrap therefore works once.
2. **GPU queues are empty.** These are the unit's `comfyui` probes.
3. **No jobs are active.** This comes from `[...bootstrap].jobs`: either
   `kind = "sql"`, which runs read-only counts over a DSN with no password (`~/.pgpass`
   supplies it), or `kind = "http"`.
   - No GET endpoint in anime-studio today can serve here.
     `/api/system/orchestrator/health` (`orchestrator.get_orchestrator_health`) swallows
     DB errors as `generating = 0` and ignores `video_generating`, so it would pass when it
     should fail.
   - The SQL connection runs with `default_transaction_read_only=on` inside a read-only
     transaction.
4. **No ESTABLISHED client connections** to the unit's TCP port, or to its unix socket if
   configured (`ss`). This check runs last, after the gate's own connections have closed.

Then the wrapper restarts the unit, verifies it, and records the attempt and outcome with
`mode = "bootstrap"`.

Bootstrap cannot drain. A request can still arrive between the final round and
`systemctl restart`. That window is the cost of the one ungated-by-drain restart, and the
attempt records it as such.

## Configuration (`~/.config/ai-team-sync/operator.toml`)

### Deploy order

1. Upgrade every ATS install before adding `[restart_gate]`. That includes the pipx copy
   the hooks run from. An older ATS rejects the unknown top-level key, and every
   governance hook would break.
2. Every governed unit must be configured, or the whole gate refuses. The default
   governed set is `anime-studio`, `tower-echo-brain`, `comfyui`, `comfyui-rocm` and
   `keyframe-blitz-worker`. Either configure all five, with waivers where an evidence
   class does not exist, or set `governed_units` explicitly. Units you leave out are also
   not guarded by the hook.
3. A missing config makes every lease fail closed. The hook then guards the default set,
   and warns only on commands that look like service control.

```toml
[restart_gate]
governed_units = ["anime-studio", "comfyui", "comfyui-rocm"]

[restart_gate.units."anime-studio"]
scope = "system"
health_url = "http://127.0.0.1:8401/api/system/health"
drain_url = "http://127.0.0.1:8401/api/system/drain"
release_url = "http://127.0.0.1:8401/api/system/drain/release"
drain_token_file = "/home/patrick/.config/ai-team-sync/drain_token"   # 0600; same value as anime-studio's ATS_DRAIN_TOKEN
lease_ttl_s = 360          # must exceed drain_wait_s + restart_timeout_s + verify_timeout_s + 10
drain_wait_s = 60
restart_timeout_s = 150
verify_timeout_s = 120
probes = [
  { name = "api", kind = "inflight", url = "http://127.0.0.1:8401/api/system/inflight" },
  { name = "comfyui-nvidia", kind = "comfyui", url = "http://127.0.0.1:8188/queue" },
  { name = "comfyui-rocm", kind = "comfyui", url = "http://127.0.0.1:8189/queue" },
]

[restart_gate.units."anime-studio".bootstrap]
port = 8401
unix_socket = "/run/anime-studio/operator-ui.sock"   # the operator UI listener, if in use
jobs = { kind = "sql", dsn = "postgresql://patrick@127.0.0.1:5432/anime_production", queries = {
  generating_shots = "SELECT count(*) FROM shots WHERE status IN ('generating', 'video_generating')",
  claimed_blitz_jobs = "SELECT count(*) FROM keyframe_blitz_queue WHERE status = 'claimed'",
  open_regenerate_dispatches = "SELECT count(*) FROM regenerate_dispatch_requests WHERE finalized_at IS NULL" } }

[restart_gate.units."comfyui"]
scope = "system"
health_url = "http://127.0.0.1:8188/system_stats"
lease_ttl_s = 200
drain_wait_s = 30
restart_timeout_s = 90
verify_timeout_s = 60
waive = { inflight = "ComfyUI serves no in-flight endpoint; its queue is the work",
          jobs = "ComfyUI's jobs ARE its queue, covered by the queue probe",
          drain = "ComfyUI has no drain; a prompt may be queued after the last probe" }
probes = [ { name = "queue", kind = "comfyui", url = "http://127.0.0.1:8188/queue" } ]
```

Check the `unix_socket` path against `OPERATOR_UI_SOCKET` in anime-studio
`packages/core/config.py` before you use it.

## Hook wiring (docs only: ATS does not edit `~/.claude/settings.json`)

```json
"PreToolUse": [
  { "matcher": "Bash",
    "hooks": [{ "type": "command",
      "command": "/home/patrick/.local/share/pipx/venvs/ai-team-sync/bin/python -m ai_team_sync.hooks.pre_tool_use_restart_guard" }] }
]
```

## Tests

- `tests/test_restart_gate_3580.py`
  - config policy, including partial configs, waivers and TTL validation
  - probe interpretation, including the single-worker check and empty job evidence
  - concurrent rounds
  - leases, including the file-DB race and a lease that expires mid-drain and is refused
    at commit
  - renew, and the sweeper (once and on an interval)
  - bootstrap
  - the wrapper's exception, signal, lost-lease and heartbeat paths
- `tests/test_restart_guard_hook_3580.py`: the hook matrix and its process protocol.
- `tests/test_restart_gate_acceptance_3580.py` (opt-in, `ATS_RESTART_ACCEPTANCE=1`).
  Runs against transient `systemd-run --user` units that carry the real anime-studio
  middleware, with a real ats-server and a 1 s sweeper:
  - T2a–T2f
  - partial config
  - multi-worker refusal (uvicorn `--workers 2`)
  - heartbeat through a 25 s stop
  - SIGKILL mid-restart settled by the sweeper
  - SIGTERM mid-restart reported
  - bootstrap first load: refused for a held connection, refused for an active job,
    granted, then refused once the middleware is loaded
