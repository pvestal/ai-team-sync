"""Restart safety gate: per-unit policy, probes and drain calls (#3580).

routers/restarts.py RECORDS restarts. This module is the part that can REFUSE
one: it decides, from live evidence, whether a governed unit may be restarted
right now, and it asks the service to stop admitting new work while the caller
restarts it. The lease that makes the decision atomic lives in
routers/restart_leases.py; the caller that performs the restart is
safe_restart.py (`ats safe-restart`).

FAIL CLOSED. Every "cannot tell" answer -- the config is unreadable, the unit is
governed but has no probes, a probe is down, times out, answers non-200 or
answers JSON of the wrong shape -- is a refusal, never a pass. A gate that
passes on missing evidence is the 2026-10-13 incident with extra steps.

POLICY SOURCE. operator.toml (operator_config.operator_config_path()):

    [restart_gate]
    governed_units = ["anime-studio", "tower-echo-brain", ...]  # optional; replaces the defaults

    [restart_gate.units."anime-studio"]
    scope = "system"                       # or "user" (systemctl --user)
    health_url = "http://127.0.0.1:8401/api/system/health"
    drain_url = "http://127.0.0.1:8401/api/system/drain"
    release_url = "http://127.0.0.1:8401/api/system/drain/release"
    drain_token_file = "/home/patrick/.config/ai-team-sync/drain_token"
    lease_ttl_s = 180
    drain_wait_s = 60
    verify_timeout_s = 90
    probes = [
      { name = "api", kind = "inflight", url = "http://127.0.0.1:8401/api/system/inflight" },
      { name = "comfyui-nvidia", kind = "comfyui", url = "http://127.0.0.1:8188/queue" },
      { name = "comfyui-rocm", kind = "comfyui", url = "http://127.0.0.1:8189/queue" },
    ]

The units governed with NO config are refused, not waved through: being on the
governed list is what makes a unit the gate's business, and having probes is what
makes the gate able to answer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ai_team_sync.operator_config import OperatorConfigError, load_operator_document

#: Governed when operator.toml says nothing. These are the production units an
#: agent session on this host has restarted or been asked about; the Bash hook
#: blocks raw service control of them even when the config cannot be read.
DEFAULT_GOVERNED_UNITS = (
    "anime-studio",
    "tower-echo-brain",
    "comfyui",
    "comfyui-rocm",
    "keyframe-blitz-worker",
)

PROBE_KINDS = ("inflight", "comfyui", "health")
_UNIT_KEYS = {
    "scope", "health_url", "drain_url", "release_url", "drain_token_file",
    "lease_ttl_s", "drain_wait_s", "verify_timeout_s", "restart_timeout_s",
    "probe_timeout_s", "probes",
}
_PROBE_KEYS = {"name", "kind", "url", "timeout_s"}
_GATE_KEYS = {"governed_units", "units"}

DRAIN_TOKEN_ENV = "ATS_DRAIN_TOKEN"
DRAIN_TOKEN_HEADER = "X-ATS-Drain-Token"
MAX_TTL_S = 900


def normalize_unit(unit: str) -> str:
    # Same rule as schemas.normalize_unit; duplicated here so the Bash hook can
    # import this module without pulling in pydantic schemas.
    return (unit or "").strip().lower().removesuffix(".service").strip()


@dataclass(frozen=True)
class Probe:
    name: str
    kind: str
    url: str
    timeout_s: float = 3.0


@dataclass(frozen=True)
class UnitGate:
    unit: str
    scope: str = "system"
    probes: tuple[Probe, ...] = ()
    health_url: str = ""
    drain_url: str = ""
    release_url: str = ""
    drain_token_file: str = ""
    lease_ttl_s: int = 180
    drain_wait_s: int = 60
    verify_timeout_s: int = 90
    restart_timeout_s: int = 150

    def public(self) -> dict:
        """What a restarting caller needs; never the token or its location."""
        return {
            "unit": self.unit,
            "scope": self.scope,
            "health_url": self.health_url,
            "drains": bool(self.drain_url),
            "drain_wait_s": self.drain_wait_s,
            "verify_timeout_s": self.verify_timeout_s,
            "restart_timeout_s": self.restart_timeout_s,
            "probes": [{"name": p.name, "kind": p.kind, "url": p.url} for p in self.probes],
        }


@dataclass
class GateConfig:
    governed: frozenset[str] = frozenset(DEFAULT_GOVERNED_UNITS)
    units: dict[str, UnitGate] = field(default_factory=dict)
    error: str = ""


def _http_url(value, where: str) -> str:
    if not isinstance(value, str) or urlparse(value).scheme not in ("http", "https") \
            or not urlparse(value).netloc:
        raise OperatorConfigError(f"{where} must be an http(s) URL, got {value!r}")
    return value


def _positive_int(value, where: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 \
            or (maximum is not None and value > maximum):
        bound = f" (<= {maximum})" if maximum else ""
        raise OperatorConfigError(f"{where} must be a positive integer{bound}, got {value!r}")
    return value


def _parse_unit(name: str, table) -> UnitGate:
    where = f"restart_gate.units.{name!r}"
    if not isinstance(table, dict):
        raise OperatorConfigError(f"{where} must be a table")
    unknown = set(table) - _UNIT_KEYS
    if unknown:
        raise OperatorConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    scope = table.get("scope", "system")
    if scope not in ("system", "user"):
        raise OperatorConfigError(f"{where}.scope must be 'system' or 'user'")
    raw_probes = table.get("probes")
    if not isinstance(raw_probes, list) or not raw_probes:
        raise OperatorConfigError(f"{where}.probes must be a non-empty array")
    default_timeout = table.get("probe_timeout_s", 3)
    if isinstance(default_timeout, bool) or not isinstance(default_timeout, (int, float)) \
            or not 0 < default_timeout <= 30:
        raise OperatorConfigError(f"{where}.probe_timeout_s must be within (0, 30]")
    probes = []
    for i, raw in enumerate(raw_probes):
        pwhere = f"{where}.probes[{i}]"
        if not isinstance(raw, dict):
            raise OperatorConfigError(f"{pwhere} must be a table")
        unknown = set(raw) - _PROBE_KEYS
        if unknown:
            raise OperatorConfigError(f"{pwhere}: unknown key(s) {sorted(unknown)}")
        kind = raw.get("kind", "inflight")
        if kind not in PROBE_KINDS:
            raise OperatorConfigError(f"{pwhere}.kind must be one of {PROBE_KINDS}")
        timeout = raw.get("timeout_s", default_timeout)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not 0 < timeout <= 30:
            raise OperatorConfigError(f"{pwhere}.timeout_s must be within (0, 30]")
        url = _http_url(raw.get("url"), f"{pwhere}.url")
        probes.append(Probe(name=str(raw.get("name") or f"{kind}:{url}"), kind=kind,
                            url=url, timeout_s=float(timeout)))
    drain_url = table.get("drain_url", "")
    release_url = table.get("release_url", "")
    if bool(drain_url) != bool(release_url):
        raise OperatorConfigError(f"{where}: drain_url and release_url go together")
    if drain_url:
        _http_url(drain_url, f"{where}.drain_url")
        _http_url(release_url, f"{where}.release_url")
    health_url = table.get("health_url", "")
    if health_url:
        _http_url(health_url, f"{where}.health_url")
    token_file = table.get("drain_token_file", "")
    if token_file and (not isinstance(token_file, str) or not os.path.isabs(token_file)):
        raise OperatorConfigError(f"{where}.drain_token_file must be an absolute path")
    return UnitGate(
        unit=name,
        scope=scope,
        probes=tuple(probes),
        health_url=health_url,
        drain_url=drain_url,
        release_url=release_url,
        drain_token_file=token_file,
        lease_ttl_s=_positive_int(table.get("lease_ttl_s", 180), f"{where}.lease_ttl_s",
                                  maximum=MAX_TTL_S),
        drain_wait_s=_positive_int(table.get("drain_wait_s", 60), f"{where}.drain_wait_s",
                                   maximum=MAX_TTL_S),
        verify_timeout_s=_positive_int(table.get("verify_timeout_s", 90),
                                       f"{where}.verify_timeout_s", maximum=MAX_TTL_S),
        restart_timeout_s=_positive_int(table.get("restart_timeout_s", 150),
                                        f"{where}.restart_timeout_s", maximum=MAX_TTL_S),
    )


def load_gate_config() -> GateConfig:
    """Never raises. A config that cannot be trusted comes back with `error` set
    and the DEFAULT governed set, so every caller fails closed on the units that
    matter most instead of discovering the problem as an exception."""
    try:
        document = load_operator_document()
        gate = document.get("restart_gate", {})
        if not isinstance(gate, dict):
            raise OperatorConfigError("[restart_gate] must be a table")
        unknown = set(gate) - _GATE_KEYS
        if unknown:
            raise OperatorConfigError(f"restart_gate: unknown key(s) {sorted(unknown)}")
        governed = gate.get("governed_units", list(DEFAULT_GOVERNED_UNITS))
        if not isinstance(governed, list) or any(not isinstance(u, str) for u in governed):
            raise OperatorConfigError("restart_gate.governed_units must be an array of unit names")
        units_table = gate.get("units", {})
        if not isinstance(units_table, dict):
            raise OperatorConfigError("restart_gate.units must be a table")
        units = {normalize_unit(name): _parse_unit(normalize_unit(name), table)
                 for name, table in units_table.items()}
        # A unit with probes configured is governed by definition.
        names = {normalize_unit(u) for u in governed} | set(units)
        names.discard("")
        return GateConfig(governed=frozenset(names), units=units)
    except OperatorConfigError as exc:
        return GateConfig(error=str(exc))


def unit_policy(cfg: GateConfig, unit: str) -> tuple[UnitGate | None, int, str]:
    """(gate, http_status, refusal). gate is None exactly when refused."""
    unit = normalize_unit(unit)
    if cfg.error:
        return None, 503, f"restart gate config is unavailable, refusing (fail closed): {cfg.error}"
    if unit in cfg.units:
        return cfg.units[unit], 200, ""
    if unit in cfg.governed:
        return None, 503, (f"{unit} is governed but has no probes configured in "
                           f"[restart_gate.units.\"{unit}\"]; refusing (fail closed)")
    return None, 422, (f"{unit} is not governed by the restart gate; ats safe-restart "
                       f"handles governed units only")


# ── probes ──────────────────────────────────────────────────────────────────


@dataclass
class ProbeResult:
    name: str
    kind: str
    url: str
    status: str  # clear | busy | unavailable
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "url": self.url,
                "status": self.status, "detail": self.detail}


def http_client() -> httpx.AsyncClient:
    """Seam for tests (httpx.MockTransport). Production: a plain client; the
    probe URLs are loopback, so no proxy may intercept them."""
    return httpx.AsyncClient(trust_env=False)


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _interpret_inflight(data, lease_id: str, expect_drain: bool) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "unavailable", "response is not a JSON object"
    in_flight = data.get("in_flight")
    draining = data.get("draining")
    jobs = data.get("active_jobs")
    jobs_ok = data.get("active_jobs_ok")
    if not _is_count(in_flight) or not isinstance(draining, bool) \
            or not isinstance(jobs, dict) or not isinstance(jobs_ok, bool):
        return "unavailable", ("response lacks in_flight:int, draining:bool, "
                               "active_jobs:object, active_jobs_ok:bool")
    if not jobs_ok or "error" in jobs:
        return "unavailable", f"active jobs could not be determined: {jobs.get('error', '?')}"
    bad = [k for k, v in jobs.items() if not _is_count(v)]
    if bad:
        return "unavailable", f"active_jobs values are not counts: {bad}"
    drain = data.get("drain") if isinstance(data.get("drain"), dict) else {}
    holder = str(drain.get("lease_id") or "")
    if expect_drain and (not draining or holder != lease_id):
        return "unavailable", (f"the service is not draining for lease {lease_id} "
                               f"(draining={draining}, holder={holder or 'none'}); "
                               "the drain expired, was released, or the process restarted")
    busy = []
    if draining and holder != lease_id:
        busy.append(f"already draining for another lease {holder or '?'}")
    if in_flight:
        routes = data.get("routes") if isinstance(data.get("routes"), list) else []
        named = ", ".join(
            f"{r.get('method', '?')} {r.get('path', '?')} ({r.get('age_s', '?')}s)"
            for r in routes[:5] if isinstance(r, dict))
        busy.append(f"{in_flight} request(s) in flight: {named or 'unnamed'}")
    active = {k: v for k, v in jobs.items() if v}
    if active:
        busy.append("active jobs: " + ", ".join(f"{k}={v}" for k, v in sorted(active.items())))
    if busy:
        return "busy", "; ".join(busy)
    return "clear", f"in_flight=0, active_jobs={jobs}"


def _interpret_comfyui(data) -> tuple[str, str]:
    if not isinstance(data, dict):
        return "unavailable", "response is not a JSON object"
    running, pending = data.get("queue_running"), data.get("queue_pending")
    if not isinstance(running, list) or not isinstance(pending, list):
        return "unavailable", "response lacks queue_running/queue_pending arrays"
    if running or pending:
        ids = []
        for item in (running + pending)[:5]:
            if isinstance(item, list) and len(item) > 1:
                ids.append(str(item[1]))
        return "busy", (f"GPU queue not empty: running={len(running)} pending={len(pending)}"
                        + (f" (prompt ids {', '.join(ids)})" if ids else ""))
    return "clear", "queue empty"


async def run_probe(client: httpx.AsyncClient, probe: Probe, *, lease_id: str = "",
                    expect_drain: bool = False) -> ProbeResult:
    def result(status: str, detail: str) -> ProbeResult:
        return ProbeResult(probe.name, probe.kind, probe.url, status, detail)

    try:
        resp = await client.get(probe.url, timeout=probe.timeout_s)
    except httpx.TimeoutException:
        return result("unavailable", f"timed out after {probe.timeout_s}s")
    except httpx.HTTPError as exc:
        return result("unavailable", f"unreachable: {type(exc).__name__}: {exc}")
    if probe.kind == "health":
        if 200 <= resp.status_code < 300:
            return result("clear", f"HTTP {resp.status_code}")
        return result("unavailable", f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        return result("unavailable", f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError):
        return result("unavailable", f"malformed JSON: {resp.text[:120]!r}")
    if probe.kind == "inflight":
        return result(*_interpret_inflight(data, lease_id, expect_drain))
    return result(*_interpret_comfyui(data))


def overall(results: list[ProbeResult]) -> str:
    statuses = {r.status for r in results}
    if not results or "unavailable" in statuses:
        return "unavailable"
    if "busy" in statuses:
        return "busy"
    return "clear"


def summarize(results: list[ProbeResult]) -> str:
    return "; ".join(f"{r.name} {r.status.upper()}: {r.detail}"
                     for r in results if r.status != "clear") or "all probes clear"


async def run_probes(gate: UnitGate, *, lease_id: str = "",
                     expect_drain: bool = False) -> list[ProbeResult]:
    async with http_client() as client:
        return [await run_probe(client, p, lease_id=lease_id, expect_drain=expect_drain)
                for p in gate.probes]


# ── drain ───────────────────────────────────────────────────────────────────


def drain_token(gate: UnitGate) -> str:
    if gate.drain_token_file:
        try:
            return Path(gate.drain_token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get(DRAIN_TOKEN_ENV, "").strip()


async def request_drain(gate: UnitGate, lease_id: str, ttl_s: int) -> tuple[bool, str]:
    token = drain_token(gate)
    if not token:
        return False, "no drain token available to the ATS server (drain_token_file / ATS_DRAIN_TOKEN)"
    try:
        async with http_client() as client:
            resp = await client.post(gate.drain_url, timeout=5,
                                     json={"lease_id": lease_id, "ttl_s": ttl_s},
                                     headers={DRAIN_TOKEN_HEADER: token})
    except httpx.HTTPError as exc:
        return False, f"drain request failed: {type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return False, f"drain refused: HTTP {resp.status_code}: {resp.text[:200]}"
    return True, "draining"


async def release_drain(gate: UnitGate, lease_id: str) -> tuple[bool, str]:
    """Best effort. A drain that cannot be released still expires on its TTL."""
    if not gate.release_url:
        return True, "unit has no drain"
    token = drain_token(gate)
    try:
        async with http_client() as client:
            resp = await client.post(gate.release_url, timeout=5,
                                     json={"lease_id": lease_id},
                                     headers={DRAIN_TOKEN_HEADER: token})
    except httpx.HTTPError as exc:
        return False, f"release failed: {type(exc).__name__}: {exc}"
    return resp.status_code == 200, f"HTTP {resp.status_code}"
