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
under a slow `systemctl restart` or verification. A refused renewal, a lease
that comes back inactive, or (drain units) a failed drain re-assertion marks
the heartbeat LOST.

PRE-ISSUE RE-CHECK. Immediately before `systemctl restart`, preissue_check()
re-reads the lease (cleared, active, >= 5 s left) and, for drain units, asks the
service whether it is draining for THIS lease with nothing in flight. A LOST
heartbeat or a failed re-check aborts with a recorded 'aborted' outcome (exit 3).
The window that remains -- between that last read and systemctl's exec -- is
stated in preissue_check().

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
    """Renews lease + drain every `interval` seconds until stopped.

    LOST means the authorization can no longer be relied on:
      * the renewal was refused (non-200), or it returned a lease that is not
        active -- the thread stops;
      * on a unit whose drain is required, the drain re-assertion did not
        succeed -- recorded as `drain_lost`; the lease keeps being renewed,
        because once systemctl has stopped the old process a failed
        re-assertion is expected, and the lease still has to cover the outcome.
    The wrapper refuses to ISSUE the restart if either is set; after issue they
    are recorded with the outcome.
    """

    def __init__(self, http: httpx.Client, url: str, interval: float,
                 out: Callable[[str], None], drain_required: bool = False):
        self.http, self.url, self.interval, self.out = http, url, interval, out
        self.drain_required = drain_required
        self.stop_event = threading.Event()
        self.renewals = 0
        self.lost: str = ""
        self.drain_lost: str = ""
        self.thread = threading.Thread(target=self._run, name="lease-heartbeat", daemon=True)

    def beat_once(self) -> None:
        try:
            resp = self.http.post(self.url, json={})
        except httpx.HTTPError as exc:
            self.out(f"WARNING: lease renewal failed: {exc}")
            return
        try:
            body = resp.json() if resp.status_code == 200 else {}
        except ValueError:
            body = {}
        lease = body.get("lease") if isinstance(body, dict) else None
        if resp.status_code != 200 or not isinstance(lease, dict) or not lease.get("active"):
            state = lease.get("state") if isinstance(lease, dict) else None
            self.lost = (f"HTTP {resp.status_code}, lease state {state!r}: {resp.text[:200]}")
            self.out(f"WARNING: lease LOST ({self.lost})")
            self.stop_event.set()
            return
        self.renewals += 1
        drain = body.get("drain")
        if self.drain_required and not (isinstance(drain, dict) and drain.get("ok")):
            self.drain_lost = f"drain re-assertion failed: {drain!r}"
            self.out(f"note: {self.drain_lost}")

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            self.beat_once()

    def lost_reason(self) -> str:
        return self.lost or self.drain_lost

    def start(self) -> "Heartbeat":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=10)


class LeaseLost(Exception):
    """The authorization is gone (or unprovable) before the restart is issued."""


#: The lease must have at least this long left when the restart is issued. It
#: covers the gap between the pre-issue re-check and systemctl's exec, during
#: which nothing renews it; the heartbeat takes over after that.
PREISSUE_MIN_REMAINING_S = 5.0


def preissue_check(http: httpx.Client, base: str, lease_id: str, policy: dict,
                   drain_required: bool) -> str:
    """Re-prove the authorization immediately before systemctl. '' when it holds.

    1. ATS: the lease is 'cleared', active, and has PREISSUE_MIN_REMAINING_S left.
    2. Drain units: the service itself reports draining == THIS lease and no
       request in flight.
    What it cannot close: the time between the last of these reads and the exec
    of `systemctl restart` (process spawn; milliseconds). No lock spans that
    boundary -- systemd knows nothing of ATS leases -- so a release of this
    lease's drain, or an expiry, landing inside it is not detected.
    """
    try:
        resp = http.get(f"{base}/{lease_id}", timeout=10)
        lease = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as exc:
        return f"lease unreadable: {exc}"
    if not isinstance(lease, dict) or lease.get("state") != "cleared" or not lease.get("active"):
        return f"lease is no longer cleared and active: {lease.get('state') if isinstance(lease, dict) else lease!r}"
    if (lease.get("expires_in_s") or 0) < PREISSUE_MIN_REMAINING_S:
        return f"lease expires in {lease.get('expires_in_s')}s (< {PREISSUE_MIN_REMAINING_S}s)"
    if drain_required:
        urls = [p.get("url") for p in policy.get("probes") or [] if p.get("kind") == "inflight"]
        if not urls:
            return "no inflight probe to confirm the service is draining"
        for url in urls:
            try:
                snap = http.get(url, timeout=5).json()
            except (httpx.HTTPError, ValueError) as exc:
                return f"cannot confirm the drain at {url}: {exc}"
            drain = snap.get("drain") if isinstance(snap, dict) else None
            if not isinstance(snap, dict) or snap.get("draining") is not True \
                    or not isinstance(drain, dict) or drain.get("lease_id") != lease_id:
                return (f"service at {url} is not draining for lease {lease_id} "
                        f"(draining={snap.get('draining') if isinstance(snap, dict) else '?'}, "
                        f"holder={drain.get('lease_id') if isinstance(drain, dict) else None})")
            if snap.get("in_flight") != 0:
                return f"service at {url} reports {snap.get('in_flight')} request(s) in flight"
    return ""


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
        drain_required = (lease.get("mode") == "normal" and bool(policy.get("drains"))
                          and "drain" not in (policy.get("waivers") or {}))
        beat = Heartbeat(http, f"{base}/{lease_id}/renew", interval, out,
                         drain_required=drain_required).start()
        previous = _install_signal_handlers()
        started = clock()

        def report(outcome: str) -> int:
            after["elapsed_s"] = round(clock() - started, 2)
            after["heartbeat_renewals"] = beat.renewals
            if beat.lost_reason():
                after["heartbeat_lost"] = beat.lost_reason()
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
            # Re-prove the authorization at the last moment before issuing.
            lost = beat.lost_reason() or preissue_check(http, base, lease_id, policy,
                                                         drain_required)
            if lost:
                raise LeaseLost(lost)
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
            if isinstance(exc, LeaseLost):
                out(f"REFUSED before issuing the restart: {exc}; recorded as aborted")
                return EXIT_UNAVAILABLE
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
