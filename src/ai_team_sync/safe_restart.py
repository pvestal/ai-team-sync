"""`ats safe-restart <unit>`: the sanctioned way for an agent to restart a governed unit (#3580).

    acquire lease -> drain -> wait for clear -> systemctl restart -> verify -> complete

Refuses, and exits non-zero, on any failed or unavailable check. Every step
after the lease is granted ends by reporting to ATS -- /complete with the
outcome, or /release when the restart was never issued -- so an authorized
attempt never disappears from the record because this process gave up.

The two side effects are injectable for tests: `runner` executes argv lists
(systemctl show / restart), and `http` is an httpx.Client.

Exit codes: 0 restarted and verified; 1 restarted but failed/timed out
verification; 2 refused (busy); 3 refused (a check was unavailable / policy);
4 local error before the restart was issued; 5 restart happened but ATS did not
accept the outcome report.
"""

from __future__ import annotations

import subprocess
import time
from typing import Callable

import httpx

Runner = Callable[[list[str], float], subprocess.CompletedProcess]

EXIT_OK, EXIT_FAILED, EXIT_BUSY, EXIT_UNAVAILABLE, EXIT_LOCAL, EXIT_UNRECORDED = 0, 1, 2, 3, 4, 5


def default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _systemctl(scope: str) -> list[str]:
    return ["systemctl", "--user"] if scope == "user" else ["systemctl"]


def restart_argv(scope: str, unit: str) -> list[str]:
    # System units go through `sudo -n`: never prompt, fail instead. A password
    # prompt inside an agent's tool call would hang until the restart timeout.
    if scope == "user":
        return ["systemctl", "--user", "restart", unit]
    return ["sudo", "-n", "systemctl", "restart", unit]


def unit_state(runner: Runner, scope: str, unit: str) -> tuple[str, int]:
    """(ActiveState, MainPID). Raises RuntimeError when systemctl cannot say."""
    proc = runner([*_systemctl(scope), "show", "-p", "ActiveState", "-p", "MainPID", unit], 10)
    if proc.returncode != 0:
        raise RuntimeError(f"systemctl show failed rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    try:
        return values.get("ActiveState", ""), int(values.get("MainPID", "0") or 0)
    except ValueError as exc:
        raise RuntimeError(f"unreadable systemctl show output: {proc.stdout[:200]!r}") from exc


def _refusal_exit(status: int) -> int:
    return EXIT_BUSY if status == 409 else EXIT_UNAVAILABLE


def _say_refusal(out, what: str, resp: httpx.Response) -> None:
    try:
        body = resp.json()
    except ValueError:
        body = {"detail": resp.text}
    out(f"REFUSED ({what}, HTTP {resp.status_code}): {body.get('detail')}")
    lease = body.get("lease") or {}
    for probe in lease.get("probes") or []:
        out(f"  probe {probe.get('name')}: {probe.get('status')} -- {probe.get('detail')}")
    if lease.get("id"):
        out(f"  lease {lease['id']} recorded as {lease.get('state')}")


def run_safe_restart(unit: str, reason: str, *, server: str, session_id: str | None = None,
                     developer: str = "", runner: Runner = default_runner,
                     http: httpx.Client | None = None, out: Callable[[str], None] = print,
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic) -> int:
    own_client = http is None
    http = http or httpx.Client(timeout=30, trust_env=False)
    base = f"{server.rstrip('/')}/api/restarts/leases"
    try:
        payload = {"unit": unit, "reason": reason, "developer": developer}
        if session_id:
            payload["session_id"] = session_id
        try:
            resp = http.post(base, json=payload)
            if resp.status_code == 404 and session_id:
                # A reaped session must not lose the attempt record.
                payload.pop("session_id")
                resp = http.post(base, json=payload)
        except httpx.HTTPError as exc:
            out(f"REFUSED: ATS server unreachable at {server}: {exc}")
            return EXIT_UNAVAILABLE
        if resp.status_code != 201:
            _say_refusal(out, "lease", resp)
            return _refusal_exit(resp.status_code)
        granted = resp.json()
        lease_id, policy = granted["lease"]["id"], granted["policy"]
        unit, scope = policy["unit"], policy["scope"]
        out(f"lease {lease_id} granted for {unit} (expires in {granted['lease']['expires_in_s']}s)")

        def release(why: str) -> None:
            try:
                http.post(f"{base}/{lease_id}/release", json={"reason": why})
            except httpx.HTTPError as exc:
                out(f"WARNING: could not release lease {lease_id}: {exc} (it expires on its TTL)")

        try:
            _state, old_pid = unit_state(runner, scope, unit)
        except Exception as exc:  # noqa: BLE001
            out(f"REFUSED: cannot read {unit}'s current MainPID: {exc}")
            release(f"safe-restart could not read MainPID: {exc}")
            return EXIT_LOCAL

        try:
            resp = http.post(f"{base}/{lease_id}/drain", json={},
                             timeout=policy["drain_wait_s"] + 30)
        except httpx.HTTPError as exc:
            out(f"REFUSED: drain call failed: {exc}")
            release(f"drain call failed: {exc}")
            return EXIT_UNAVAILABLE
        if resp.status_code != 200:
            _say_refusal(out, "drain", resp)
            return _refusal_exit(resp.status_code)
        out(f"drained and clear; restarting {unit} (old MainPID {old_pid})")

        argv = restart_argv(scope, unit)
        after: dict = {"argv": argv}
        started = clock()
        try:
            proc = runner(argv, policy["restart_timeout_s"])
            after.update(restart_rc=proc.returncode, restart_stderr=(proc.stderr or "")[-500:])
            outcome = "success" if proc.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            after["restart_rc"] = None
            outcome = "timeout"

        new_pid = None
        if outcome == "success":
            outcome = "timeout"
            deadline = started + policy["verify_timeout_s"]
            active_seen = False
            while clock() < deadline:
                try:
                    state, pid = unit_state(runner, scope, unit)
                except Exception as exc:  # noqa: BLE001
                    after["verify_error"] = str(exc)
                    state, pid = "", 0
                after.update(active_state=state, main_pid=pid)
                if state == "failed":
                    outcome = "failed"
                    break
                if state == "active" and pid and pid != old_pid:
                    active_seen, new_pid = True, pid
                    if not policy.get("health_url"):
                        outcome = "success"
                        break
                    try:
                        health = http.get(policy["health_url"], timeout=5)
                        after["health_status"] = health.status_code
                        if 200 <= health.status_code < 300:
                            outcome = "success"
                            break
                    except httpx.HTTPError as exc:
                        after["health_error"] = f"{type(exc).__name__}: {exc}"
                sleep(0.5)
            after["new_process_seen"] = active_seen
        after["elapsed_s"] = round(clock() - started, 2)

        try:
            resp = http.post(f"{base}/{lease_id}/complete", json={
                "outcome": outcome, "old_pid": old_pid, "new_pid": new_pid, "after": after})
        except httpx.HTTPError as exc:
            resp = None
            out(f"ERROR: restart outcome '{outcome}' NOT recorded: {exc}")
        if resp is None or resp.status_code != 200:
            if resp is not None:
                out(f"ERROR: ATS refused the outcome report (HTTP {resp.status_code}): {resp.text[:300]}")
            return EXIT_UNRECORDED
        out(f"{unit}: {outcome} (MainPID {old_pid} -> {new_pid}); "
            f"recorded as {resp.json()['outcome_restart_id']}")
        return EXIT_OK if outcome == "success" else EXIT_FAILED
    finally:
        if own_client:
            http.close()
