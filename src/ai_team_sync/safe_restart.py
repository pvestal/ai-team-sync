"""`ats safe-restart <unit>`: the sanctioned way for an agent to restart a governed unit (#3580).

    acquire lease -> drain -> wait for clear -> systemctl restart -> verify -> complete

Refuses, and exits non-zero, on any failed or unavailable check.

ONCE AUTHORIZED, AN OUTCOME IS ALWAYS RECORDED. Everything after the drain
clears runs inside try/except BaseException/finally: any exception, Ctrl-C or
SIGTERM (handlers installed for the duration) reports 'aborted' (restart not yet
issued) or 'failed' (issued) with the exception before this process exits. A
SIGKILL cannot be caught; for that, the ATS server's expiry sweeper settles the
lease as 'unknown' and releases the drain within one sweep interval after the
lease expires.

HEARTBEAT. From authorization until the outcome is reported, a background
thread renews the lease AND the service drain every ttl/3, so neither expires
under a slow `systemctl restart` or verification. A renewal that fails BEFORE
the restart is issued aborts the restart.

--bootstrap is the first restart only -- the one that LOADS the in-flight
middleware the normal path depends on. It is gated on different evidence
(middleware absent, GPU queues empty, no active jobs, no ESTABLISHED client
connections to the unit) and cannot drain; see docs/restart-gate.md.

The side effects are injectable for tests: `runner` executes argv lists
(systemctl show / restart), and `http` is an httpx.Client.

Exit codes: 0 restarted and verified; 1 restarted but failed/timed out
verification; 2 refused (busy); 3 refused (a check was unavailable / policy);
4 local error before the restart was issued; 5 restart happened but ATS did not
accept the outcome report; 6 interrupted (signal) -- outcome reported.
"""

from __future__ import annotations

import signal
import subprocess
import threading
import time
from typing import Callable

import httpx

Runner = Callable[[list[str], float], subprocess.CompletedProcess]

(EXIT_OK, EXIT_FAILED, EXIT_BUSY, EXIT_UNAVAILABLE, EXIT_LOCAL, EXIT_UNRECORDED,
 EXIT_INTERRUPTED) = 0, 1, 2, 3, 4, 5, 6


class Interrupted(BaseException):
    """Raised by our SIGTERM/SIGINT handler so the finally path reports."""


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
    if lease.get("waivers"):
        out(f"  waived evidence: {lease['waivers']}")
    if lease.get("id"):
        out(f"  lease {lease['id']} recorded as {lease.get('state')}")


class Heartbeat:
    """Renews lease + drain every `interval` seconds until stopped."""

    def __init__(self, http: httpx.Client, url: str, interval: float,
                 out: Callable[[str], None]):
        self.http, self.url, self.interval, self.out = http, url, interval, out
        self.stop_event = threading.Event()
        self.renewals = 0
        self.lost: str = ""
        self.thread = threading.Thread(target=self._run, name="lease-heartbeat", daemon=True)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                resp = self.http.post(self.url, json={})
            except httpx.HTTPError as exc:
                self.out(f"WARNING: lease renewal failed: {exc}")
                continue
            if resp.status_code == 200:
                self.renewals += 1
                drain = (resp.json() or {}).get("drain")
                if drain and not drain.get("ok"):
                    self.out(f"note: drain re-assertion: {drain.get('detail')}")
            else:
                self.lost = f"HTTP {resp.status_code}: {resp.text[:200]}"
                self.out(f"WARNING: lease renewal refused ({self.lost})")
                return

    def start(self) -> "Heartbeat":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=10)


def _install_signal_handlers() -> dict:
    """Turn SIGTERM/SIGINT into Interrupted in the main thread; returns the
    previous handlers. A no-op off the main thread (signal module rule)."""
    if threading.current_thread() is not threading.main_thread():
        return {}

    def handler(signum, frame):
        raise Interrupted(f"signal {signum}")

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, handler)
    return previous


def _restore_signal_handlers(previous: dict) -> None:
    for sig, prev in previous.items():
        signal.signal(sig, prev)


def run_safe_restart(unit: str, reason: str, *, server: str, session_id: str | None = None,
                     developer: str = "", bootstrap: bool = False,
                     runner: Runner = default_runner,
                     http: httpx.Client | None = None, out: Callable[[str], None] = print,
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic,
                     heartbeat_interval: float | None = None) -> int:
    own_client = http is None
    http = http or httpx.Client(timeout=30, trust_env=False)
    base = f"{server.rstrip('/')}/api/restarts/leases"
    try:
        payload = {"unit": unit, "reason": reason, "developer": developer,
                   "bootstrap": bootstrap}
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
        lease, policy = granted["lease"], granted["policy"]
        lease_id = lease["id"]
        unit, scope = policy["unit"], policy["scope"]
        out(f"lease {lease_id} granted for {unit} ({lease['mode']}, "
            f"expires in {lease['expires_in_s']}s)")
        if policy.get("waivers"):
            out(f"WAIVED evidence for {unit}: {policy['waivers']}")

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
        out(f"cleared; restarting {unit} (old MainPID {old_pid})")

        # ── AUTHORIZED. From here an outcome is always reported. ──────────
        after: dict = {"argv": restart_argv(scope, unit)}
        state = {"reported": False, "issued": False, "new_pid": None}
        interval = heartbeat_interval or max(1.0, lease["ttl_s"] / 3)
        beat = Heartbeat(http, f"{base}/{lease_id}/renew", interval, out).start()
        previous = _install_signal_handlers()
        started = clock()

        def report(outcome: str) -> int:
            after["elapsed_s"] = round(clock() - started, 2)
            after["heartbeat_renewals"] = beat.renewals
            try:
                resp = http.post(f"{base}/{lease_id}/complete", json={
                    "outcome": outcome, "old_pid": old_pid, "new_pid": state["new_pid"],
                    "after": after})
            except httpx.HTTPError as exc:
                out(f"ERROR: restart outcome '{outcome}' NOT recorded: {exc}")
                return EXIT_UNRECORDED
            state["reported"] = True
            if resp.status_code != 200:
                out(f"ERROR: ATS refused the outcome report (HTTP {resp.status_code}): {resp.text[:300]}")
                return EXIT_UNRECORDED
            out(f"{unit}: {outcome} (MainPID {old_pid} -> {state['new_pid']}); "
                f"recorded as {resp.json()['outcome_restart_id']}")
            return EXIT_OK if outcome == "success" else EXIT_FAILED

        try:
            if beat.lost:
                raise RuntimeError(f"lease lost before the restart was issued: {beat.lost}")
            state["issued"] = True
            try:
                proc = runner(after["argv"], policy["restart_timeout_s"])
                after.update(restart_rc=proc.returncode,
                             restart_stderr=(proc.stderr or "")[-500:])
                outcome = "success" if proc.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                after["restart_rc"] = None
                outcome = "timeout"

            if outcome == "success":
                outcome = "timeout"
                # Verification gets its own budget, counted from when systemctl
                # returned -- not from before a (possibly slow) stop/start.
                deadline = clock() + policy["verify_timeout_s"]
                active_seen = False
                while clock() < deadline:
                    try:
                        unit_active, pid = unit_state(runner, scope, unit)
                    except Exception as exc:  # noqa: BLE001
                        after["verify_error"] = str(exc)
                        unit_active, pid = "", 0
                    after.update(active_state=unit_active, main_pid=pid)
                    if unit_active == "failed":
                        outcome = "failed"
                        break
                    if unit_active == "active" and pid and pid != old_pid:
                        active_seen, state["new_pid"] = True, pid
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
            return report(outcome)
        except BaseException as exc:
            if not state["reported"]:
                after["exception"] = f"{type(exc).__name__}: {exc}"
                after["note"] = ("interrupted after the restart was issued; the unit's "
                                 "real state is not known" if state["issued"]
                                 else "interrupted before the restart was issued")
                report("failed" if state["issued"] else "aborted")
            if isinstance(exc, Interrupted):
                out(f"interrupted ({exc}); outcome reported")
                return EXIT_INTERRUPTED
            if isinstance(exc, Exception):
                out(f"ERROR: {type(exc).__name__}: {exc}; outcome reported")
                return EXIT_FAILED if state["issued"] else EXIT_LOCAL
            raise
        finally:
            beat.stop()
            _restore_signal_handlers(previous)
    finally:
        if own_client:
            http.close()
