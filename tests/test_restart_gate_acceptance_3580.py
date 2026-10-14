"""#3580 acceptance: real processes, disposable services, controlled fault injection.

NEVER PRODUCTION. Operator amendment 2026-10-13: no production restart during
acceptance. Everything here is created and destroyed by the test:
  * the governed unit is a transient `systemd-run --user` service
    (ats-p0-test-<random>) running tests/restart_gate_disposable_app.py, which
    mounts the REAL anime-studio middleware loaded from INFLIGHT_PY;
  * ATS is a real uvicorn ats-server on a free port with a temp SQLite file and
    a temp operator.toml (ATS_OPERATOR_CONFIG);
  * "ComfyUI" and every faulty probe target are in-process HTTP servers.
The restart itself is real: `ats safe-restart` runs `systemctl --user restart`
on the transient unit.

Opt-in: ATS_RESTART_ACCEPTANCE=1. Needs a systemd user manager and the
anime-studio #3580 inflight module (ANIME_STUDIO_INFLIGHT, default: the
feat/3580-inflight-drain worktree).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
DEFAULT_INFLIGHT = ("/home/patrick/.config/superpowers/worktrees/anime-studio/"
                    "feat-3580-inflight-drain/packages/core/inflight.py")
INFLIGHT_PY = os.environ.get("ANIME_STUDIO_INFLIGHT", DEFAULT_INFLIGHT)
TOKEN = "acceptance-" + uuid.uuid4().hex

pytestmark = [
    pytest.mark.skipif(os.environ.get("ATS_RESTART_ACCEPTANCE") != "1",
                       reason="opt-in: ATS_RESTART_ACCEPTANCE=1"),
    pytest.mark.skipif(not shutil.which("systemd-run"), reason="needs systemd-run"),
    pytest.mark.skipif(not os.path.exists(INFLIGHT_PY), reason=f"missing {INFLIGHT_PY}"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=0.5, trust_env=False)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise AssertionError(f"{url} never came up")


# ── fake / faulty probe targets ─────────────────────────────────────────────


class Fakes:
    def __init__(self):
        self.queue = {"queue_running": [], "queue_pending": []}

    def handler(self):
        fakes = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body: bytes, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/queue":
                    return self._send(200, json.dumps(fakes.queue).encode())
                if self.path == "/garbage":
                    return self._send(200, b"{not json", "text/plain")
                if self.path == "/slow":
                    time.sleep(3)
                    return self._send(200, b"{}")
                return self._send(404, b"{}")

        return H


@pytest.fixture(scope="module")
def fakes():
    f = Fakes()
    server = ThreadingHTTPServer(("127.0.0.1", 0), f.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    f.base = f"http://127.0.0.1:{server.server_address[1]}"
    yield f
    server.shutdown()


# ── the disposable governed unit ────────────────────────────────────────────


class Unit:
    def __init__(self, tmp: Path):
        self.name = f"ats-p0-test-{uuid.uuid4().hex[:8]}"
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.jobs_file = tmp / "jobs.json"
        self.jobs_file.write_text("{}")

    def start(self):
        subprocess.run([
            "systemd-run", "--user", f"--unit={self.name}", "--property=Restart=no",
            f"--setenv=INFLIGHT_PY={INFLIGHT_PY}", f"--setenv=ATS_DRAIN_TOKEN={TOKEN}",
            f"--setenv=JOBS_FILE={self.jobs_file}",
            sys.executable, str(HERE / "restart_gate_disposable_app.py"), str(self.port),
        ], check=True, capture_output=True, text=True)
        _wait_http(f"{self.base}/health")

    def main_pid(self) -> int:
        out = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value",
                              self.name], capture_output=True, text=True, check=True)
        return int(out.stdout.strip() or 0)

    def inflight(self) -> dict:
        return httpx.get(f"{self.base}/api/system/inflight", timeout=5, trust_env=False).json()

    def stop(self):
        subprocess.run(["systemctl", "--user", "stop", self.name], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", self.name], capture_output=True)


class Ats:
    def __init__(self, tmp: Path):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.config = tmp / "operator.toml"
        self.db = tmp / "ats.db"
        self.env = {**os.environ, "PYTHONPATH": str(SRC),
                    "DATABASE_URL": f"sqlite+aiosqlite:///{self.db}",
                    "ATS_OPERATOR_CONFIG": str(self.config), "ATS_DRAIN_TOKEN": TOKEN,
                    "ATS_SERVER_URL": self.base,
                    # Isolate session pointers: never attribute to a live session.
                    "ATS_STATE_DIR": str(tmp / "ats-state")}
        self.env.pop("ATS_COORDINATED_REPOS", None)
        self.proc = None

    def configure(self, unit: Unit, probes: list[dict], **extra):
        lines = [f'[restart_gate.units."{unit.name}"]', 'scope = "user"',
                 f'health_url = "{unit.base}/health"',
                 f'drain_url = "{unit.base}/api/system/drain"',
                 f'release_url = "{unit.base}/api/system/drain/release"',
                 'drain_wait_s = 30', 'verify_timeout_s = 30', 'lease_ttl_s = 120']
        lines += [f"{k} = {v}" for k, v in extra.items()]
        rendered = ", ".join(
            "{ " + ", ".join(f'{k} = {json.dumps(v)}' for k, v in p.items()) + " }"
            for p in probes)
        lines.append(f"probes = [ {rendered} ]")
        self.config.write_text("\n".join(lines) + "\n")

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "ai_team_sync.server:app", "--host",
             "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        _wait_http(f"{self.base}/health")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=15)

    def post(self, path, **kw):
        return httpx.post(f"{self.base}/api{path}", trust_env=False, timeout=60, **kw)

    def get(self, path, **kw):
        return httpx.get(f"{self.base}/api{path}", trust_env=False, timeout=30, **kw).json()

    def restarts(self, unit: Unit) -> list[dict]:
        rows = self.get("/restarts", params={"unit": unit.name, "limit": 100})
        return sorted(rows, key=lambda r: r["created_at"])

    def safe_restart(self, unit: Unit, reason="acceptance") -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ai_team_sync.cli", "safe-restart", unit.name,
             "--reason", reason, "--session-id", ""],
            env=self.env, capture_output=True, text=True, timeout=180)


@pytest.fixture
def rig(tmp_path, fakes):
    fakes.queue = {"queue_running": [], "queue_pending": []}
    unit, ats = Unit(tmp_path), Ats(tmp_path)
    unit.start()
    ats.configure(unit, [
        {"name": "api", "kind": "inflight", "url": f"{unit.base}/api/system/inflight"},
        {"name": "comfyui", "kind": "comfyui", "url": f"{fakes.base}/queue"},
    ])
    ats.start()
    try:
        yield unit, ats, fakes
    finally:
        ats.stop()
        unit.stop()


def _post_async(url: str, results: list):
    def go():
        try:
            results.append(httpx.post(url, timeout=60, trust_env=False))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)
    t = threading.Thread(target=go)
    t.start()
    return t


def _until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


# ── T2a ─────────────────────────────────────────────────────────────────────


def test_t2a_inflight_post_refuses_then_restart_proceeds(rig):
    unit, ats, _ = rig
    results: list = []
    t = _post_async(f"{unit.base}/work?seconds=10", results)
    _until(lambda: unit.inflight()["in_flight"] == 1)
    old_pid = unit.main_pid()

    refused = ats.safe_restart(unit, "T2a busy")
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "POST /work" in refused.stdout
    assert unit.main_pid() == old_pid, "a refused restart touches nothing"
    print("T2a refused:", refused.stdout.strip().splitlines()[0])

    t.join()
    assert results[0].status_code == 200, "the in-flight request completed untouched"
    ok = ats.safe_restart(unit, "T2a idle")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    new_pid = unit.main_pid()
    assert new_pid and new_pid != old_pid
    rows = ats.restarts(unit)
    assert [r["outcome"] for r in rows] == ["refused", "authorized", "completed"], rows
    assert "/work" in rows[0]["before"]["refusal"]
    assert (rows[2]["old_pid"], rows[2]["new_pid"]) == (old_pid, new_pid)
    print("T2a rows:", [(r["outcome"], r["old_pid"], r["new_pid"]) for r in rows])


# ── T2b ─────────────────────────────────────────────────────────────────────


def test_t2b_new_write_during_drain_is_refused_and_uncounted(rig):
    unit, ats, _ = rig
    lease = ats.post("/restarts/leases", json={"unit": unit.name, "reason": "T2b"})
    assert lease.status_code == 201, lease.text
    lease_id = lease.json()["lease"]["id"]

    old: list = []
    t_old = _post_async(f"{unit.base}/work?seconds=3", old)  # arrives after the grant
    _until(lambda: unit.inflight()["in_flight"] == 1)

    drained: list = []
    t_drain = threading.Thread(target=lambda: drained.append(
        ats.post(f"/restarts/leases/{lease_id}/drain", json={})))
    t_drain.start()
    _until(lambda: unit.inflight()["draining"])

    late = httpx.post(f"{unit.base}/write", timeout=10, trust_env=False)
    assert late.status_code == 503 and int(late.headers["retry-after"]) >= 1
    assert unit.inflight()["in_flight"] == 1, "the refused write was not counted"

    t_old.join()
    t_drain.join()
    assert old[0].status_code == 200
    assert drained[0].status_code == 200, drained[0].text
    assert drained[0].json()["lease"]["state"] == "cleared"
    snap = unit.inflight()
    assert snap["in_flight"] == 0 and snap["draining"]

    old_pid = unit.main_pid()
    subprocess.run(["systemctl", "--user", "restart", unit.name], check=True)
    _wait_http(f"{unit.base}/health")
    new_pid = unit.main_pid()
    assert new_pid != old_pid
    done = ats.post(f"/restarts/leases/{lease_id}/complete", json={
        "outcome": "success", "old_pid": old_pid, "new_pid": new_pid,
        "after": {"in_flight_at_restart": snap["in_flight"]}})
    assert done.status_code == 200, done.text
    assert httpx.post(f"{unit.base}/write", timeout=10, trust_env=False).status_code == 200
    print("T2b late write:", late.status_code, late.headers["retry-after"],
          "in_flight at restart:", snap["in_flight"])


# ── T2c ─────────────────────────────────────────────────────────────────────


def test_t2c_concurrent_lease_requests_one_winner(rig):
    unit, ats, _ = rig
    n = 8
    barrier = threading.Barrier(n)
    codes: list = []
    lock = threading.Lock()

    def contend(i):
        with httpx.Client(trust_env=False, timeout=60) as c:
            barrier.wait()
            r = c.post(f"{ats.base}/api/restarts/leases",
                       json={"unit": unit.name, "reason": f"T2c-{i}"})
            with lock:
                codes.append((r.status_code, r.json()))

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = sorted(c for c, _ in codes)
    assert statuses.count(201) == 1 and statuses.count(409) == n - 1, statuses
    winner = next(body for c, body in codes if c == 201)["lease"]["id"]
    assert all(winner in body["detail"] for c, body in codes if c == 409)
    print("T2c statuses:", statuses)
    ats.post(f"/restarts/leases/{winner}/release", json={"reason": "T2c done"})


# ── T2d ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fault", ["down", "timeout", "garbage"])
def test_t2d_unavailable_probe_refuses(rig, fault):
    unit, ats, fakes = rig
    url = {"down": f"http://127.0.0.1:{_free_port()}/api/system/inflight",
           "timeout": f"{fakes.base}/slow",
           "garbage": f"{fakes.base}/garbage"}[fault]
    ats.configure(unit, [
        {"name": "api", "kind": "inflight", "url": url, "timeout_s": 0.5},
        {"name": "comfyui", "kind": "comfyui", "url": f"{fakes.base}/queue"},
    ])
    old_pid = unit.main_pid()
    r = ats.safe_restart(unit, f"T2d {fault}")
    assert r.returncode == 3, r.stdout + r.stderr
    assert unit.main_pid() == old_pid
    assert not unit.inflight()["draining"], "a refused lease never drains"
    rows = ats.restarts(unit)
    assert [x["outcome"] for x in rows] == ["refused"]
    print(f"T2d {fault}:", rows[0]["before"]["refusal"])


def test_t2d_active_jobs_undeterminable_refuses(rig):
    unit, ats, _ = rig
    unit.jobs_file.write_text("[1, 2]")  # provider returns a non-object
    r = ats.safe_restart(unit, "T2d jobs")
    assert r.returncode == 3 and "non-object" in r.stdout, r.stdout
    unit.jobs_file.write_text('{"generating_shots": 2}')
    r = ats.safe_restart(unit, "T2d jobs busy")
    assert r.returncode == 2 and "generating_shots=2" in r.stdout, r.stdout


# ── T2e ─────────────────────────────────────────────────────────────────────


def test_t2e_gpu_queue_nonempty_refuses(rig):
    unit, ats, fakes = rig
    fakes.queue = {"queue_running": [[1, "prompt-abc", {}, {}, []]], "queue_pending": []}
    old_pid = unit.main_pid()
    r = ats.safe_restart(unit, "T2e")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "prompt-abc" in r.stdout
    assert unit.main_pid() == old_pid
    fakes.queue = {"queue_running": [], "queue_pending": []}
    print("T2e:", [line for line in r.stdout.splitlines() if "comfyui" in line])


# ── T2f ─────────────────────────────────────────────────────────────────────


def test_t2f_lease_expiry_releases_the_drain(rig):
    unit, ats, _ = rig
    lease = ats.post("/restarts/leases", json={"unit": unit.name, "reason": "T2f",
                                               "ttl_s": 10})
    assert lease.status_code == 201, lease.text
    lease_id = lease.json()["lease"]["id"]
    drained = ats.post(f"/restarts/leases/{lease_id}/drain", json={})
    assert drained.status_code == 200, drained.text
    assert httpx.post(f"{unit.base}/write", timeout=5, trust_env=False).status_code == 503
    # Nobody releases anything: the holder "died". The SERVICE's own TTL ends it.
    _until(lambda: httpx.post(f"{unit.base}/write", timeout=5,
                              trust_env=False).status_code == 200, timeout=20)
    assert not unit.inflight()["draining"]
    view = ats.get(f"/restarts/leases/{lease_id}")
    assert view["state"] == "expired"
    rows = ats.restarts(unit)
    assert [r["outcome"] for r in rows] == ["authorized", "unknown"]
    # And the unit can be leased again.
    again = ats.post("/restarts/leases", json={"unit": unit.name, "reason": "T2f again"})
    assert again.status_code == 201
    ats.post(f"/restarts/leases/{again.json()['lease']['id']}/release", json={})
