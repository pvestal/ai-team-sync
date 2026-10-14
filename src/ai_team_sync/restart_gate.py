"""Restart safety gate: per-unit policy, probes and drain calls (#3580).

routers/restarts.py RECORDS restarts. This module is the part that can REFUSE
one: it decides, from live evidence, whether a governed unit may be restarted
right now, and it asks the service to stop admitting new work while the caller
restarts it. The lease that makes the decision atomic lives in
routers/restart_leases.py; the caller that performs the restart is
safe_restart.py (`ats safe-restart`).

FAIL CLOSED. Every "cannot tell" answer is a refusal, never a pass:
  * the config is unreadable, or ANY governed unit lacks the evidence its class
    needs (a health-only or partial config is a config error, which refuses
    every unit -- not just the incomplete one);
  * a probe is down, times out, answers non-200 or JSON of the wrong shape;
  * the service cannot prove it is a single worker (the drain is per process);
  * active jobs are undeterminable or absent when job evidence is required.

REQUIRED EVIDENCE. Every governed unit must supply all four classes:
    inflight   -- an `inflight` probe (requests in flight + process identity)
    jobs       -- that probe's non-empty `active_jobs` counts
    gpu_queue  -- at least one `comfyui` probe
    drain      -- drain_url + release_url, so check and restart cannot race
The set is fixed. The ONLY opt-out is an explicit, reasoned waiver:
    waive = { drain = "ComfyUI has no drain endpoint; queue probe only" }
and every waiver is echoed in each lease response and recorded on the attempt
row, so a weaker gate is never silent.

POLICY SOURCE. operator.toml (operator_config.operator_config_path()); see
docs/restart-gate.md for a complete example.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ai_team_sync.operator_config import OperatorConfigError, load_operator_document

#: Governed when operator.toml names no governed_units. Each must be configured
#: (or the whole config is refused); the Bash hook guards these even when the
#: config cannot be read.
DEFAULT_GOVERNED_UNITS = (
    "anime-studio",
    "tower-echo-brain",
    "comfyui",
    "comfyui-rocm",
    "keyframe-blitz-worker",
)

EVIDENCE_CLASSES = ("inflight", "jobs", "gpu_queue", "drain")
PROBE_KINDS = ("inflight", "comfyui", "health")
#: Seconds a lease must outlast drain_wait + restart + verify, at minimum.
TTL_MARGIN_S = 10

_UNIT_KEYS = {
    "scope", "health_url", "drain_url", "release_url", "drain_token_file",
    "lease_ttl_s", "drain_wait_s", "verify_timeout_s", "restart_timeout_s",
    "probe_timeout_s", "probes", "waive", "bootstrap",
}
_PROBE_KEYS = {"name", "kind", "url", "timeout_s"}
_GATE_KEYS = {"governed_units", "units"}
_BOOTSTRAP_KEYS = {"port", "unix_socket", "jobs"}
_JOBS_KEYS = {"kind", "url", "dsn", "queries", "timeout_s"}

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
class BootstrapGate:
    """Evidence for the FIRST restart, the one that loads the middleware the
    normal gate depends on (so there is no inflight endpoint and no drain)."""
    port: int
    unix_socket: str = ""
    jobs_kind: str = ""          # "sql" | "http" | "" (only when jobs is waived)
    jobs_url: str = ""
    jobs_dsn: str = ""
    jobs_queries: tuple[tuple[str, str], ...] = ()
    jobs_timeout_s: float = 5.0


@dataclass(frozen=True)
class UnitGate:
    unit: str
    scope: str = "system"
    probes: tuple[Probe, ...] = ()
    health_url: str = ""
    drain_url: str = ""
    release_url: str = ""
    drain_token_file: str = ""
    lease_ttl_s: int = 360
    drain_wait_s: int = 60
    verify_timeout_s: int = 90
    restart_timeout_s: int = 150
    waivers: tuple[tuple[str, str], ...] = ()
    bootstrap: BootstrapGate | None = None

    @property
    def waived(self) -> dict[str, str]:
        return dict(self.waivers)

    def min_ttl_s(self) -> int:
        """A lease must outlive the whole sequence it authorizes."""
        return self.drain_wait_s + self.restart_timeout_s + self.verify_timeout_s + TTL_MARGIN_S

    def public(self) -> dict:
        """What a restarting caller needs; never the token, DSN or their location."""
        return {
            "unit": self.unit,
            "scope": self.scope,
            "health_url": self.health_url,
            "drains": bool(self.drain_url),
            "lease_ttl_s": self.lease_ttl_s,
            "drain_wait_s": self.drain_wait_s,
            "verify_timeout_s": self.verify_timeout_s,
            "restart_timeout_s": self.restart_timeout_s,
            "waivers": self.waived,
            "bootstrap_configured": self.bootstrap is not None,
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


def _timeout(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 30:
        raise OperatorConfigError(f"{where} must be within (0, 30]")
    return float(value)


def _parse_waivers(raw, where: str) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise OperatorConfigError(f"{where}.waive must be a table of class = \"reason\"")
    out = []
    for cls, reason in raw.items():
        if cls not in EVIDENCE_CLASSES:
            raise OperatorConfigError(f"{where}.waive: unknown evidence class {cls!r} "
                                      f"(classes: {EVIDENCE_CLASSES})")
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise OperatorConfigError(f"{where}.waive.{cls} needs a written reason "
                                      "(at least 10 characters)")
        out.append((cls, reason.strip()))
    return tuple(sorted(out))


def _parse_bootstrap(raw, where: str, waived: dict) -> BootstrapGate | None:
    if raw is None:
        return None
    bwhere = f"{where}.bootstrap"
    if not isinstance(raw, dict):
        raise OperatorConfigError(f"{bwhere} must be a table")
    unknown = set(raw) - _BOOTSTRAP_KEYS
    if unknown:
        raise OperatorConfigError(f"{bwhere}: unknown key(s) {sorted(unknown)}")
    port = _positive_int(raw.get("port"), f"{bwhere}.port", maximum=65535)
    sock = raw.get("unix_socket", "")
    if sock and (not isinstance(sock, str) or not os.path.isabs(sock)):
        raise OperatorConfigError(f"{bwhere}.unix_socket must be an absolute path")
    jobs = raw.get("jobs")
    if jobs is None:
        if "jobs" not in waived:
            raise OperatorConfigError(f"{bwhere}.jobs is required unless 'jobs' is waived")
        return BootstrapGate(port=port, unix_socket=sock)
    jwhere = f"{bwhere}.jobs"
    if not isinstance(jobs, dict):
        raise OperatorConfigError(f"{jwhere} must be a table")
    unknown = set(jobs) - _JOBS_KEYS
    if unknown:
        raise OperatorConfigError(f"{jwhere}: unknown key(s) {sorted(unknown)}")
    kind = jobs.get("kind")
    timeout = _timeout(jobs.get("timeout_s", 5), f"{jwhere}.timeout_s")
    if kind == "http":
        return BootstrapGate(port=port, unix_socket=sock, jobs_kind="http",
                             jobs_url=_http_url(jobs.get("url"), f"{jwhere}.url"),
                             jobs_timeout_s=timeout)
    if kind == "sql":
        dsn = jobs.get("dsn")
        if not isinstance(dsn, str) or not dsn.startswith(("postgresql://", "postgres://")):
            raise OperatorConfigError(f"{jwhere}.dsn must be a postgresql:// DSN "
                                      "(no password in it; ~/.pgpass supplies it)")
        if urlparse(dsn).password:
            raise OperatorConfigError(f"{jwhere}.dsn must not carry a password")
        queries = jobs.get("queries")
        if not isinstance(queries, dict) or not queries:
            raise OperatorConfigError(f"{jwhere}.queries must be a non-empty table name = SQL")
        for name, sql in queries.items():
            if not isinstance(sql, str) or not sql.strip().lower().startswith("select"):
                raise OperatorConfigError(f"{jwhere}.queries.{name} must be a SELECT")
        return BootstrapGate(port=port, unix_socket=sock, jobs_kind="sql", jobs_dsn=dsn,
                             jobs_queries=tuple(sorted(queries.items())),
                             jobs_timeout_s=timeout)
    raise OperatorConfigError(f"{jwhere}.kind must be 'sql' or 'http'")


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
    default_timeout = _timeout(table.get("probe_timeout_s", 3), f"{where}.probe_timeout_s")
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
        timeout = _timeout(raw.get("timeout_s", default_timeout), f"{pwhere}.timeout_s")
        url = _http_url(raw.get("url"), f"{pwhere}.url")
        probes.append(Probe(name=str(raw.get("name") or f"{kind}:{url}"), kind=kind,
                            url=url, timeout_s=timeout))
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

    waivers = _parse_waivers(table.get("waive"), where)
    waived = dict(waivers)
    kinds = {p.kind for p in probes}
    provided = set()
    if "inflight" in kinds:
        provided |= {"inflight", "jobs"}
    if "comfyui" in kinds:
        provided.add("gpu_queue")
    if drain_url:
        provided.add("drain")
    missing = [c for c in EVIDENCE_CLASSES if c not in provided and c not in waived]
    if missing:
        raise OperatorConfigError(
            f"{where} lacks required evidence {missing} (have {sorted(provided)}); "
            "add the probe/drain, or waive the class with a written reason")

    gate = UnitGate(
        unit=name,
        scope=scope,
        probes=tuple(probes),
        health_url=health_url,
        drain_url=drain_url,
        release_url=release_url,
        drain_token_file=token_file,
        lease_ttl_s=_positive_int(table.get("lease_ttl_s", 360), f"{where}.lease_ttl_s",
                                  maximum=MAX_TTL_S),
        drain_wait_s=_positive_int(table.get("drain_wait_s", 60), f"{where}.drain_wait_s",
                                   maximum=MAX_TTL_S),
        verify_timeout_s=_positive_int(table.get("verify_timeout_s", 90),
                                       f"{where}.verify_timeout_s", maximum=MAX_TTL_S),
        restart_timeout_s=_positive_int(table.get("restart_timeout_s", 150),
                                        f"{where}.restart_timeout_s", maximum=MAX_TTL_S),
        waivers=waivers,
        bootstrap=_parse_bootstrap(table.get("bootstrap"), where, waived),
    )
    if gate.lease_ttl_s <= gate.min_ttl_s():
        raise OperatorConfigError(
            f"{where}.lease_ttl_s={gate.lease_ttl_s} must exceed drain_wait_s + "
            f"restart_timeout_s + verify_timeout_s + {TTL_MARGIN_S} = {gate.min_ttl_s()}")
    return gate


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
        unconfigured = sorted(names - set(units))
        if unconfigured:
            raise OperatorConfigError(
                f"governed unit(s) {unconfigured} have no [restart_gate.units.\"<unit>\"] "
                "table; every governed unit must declare its evidence (or waive it)")
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


def _interpret_inflight(data, lease_id: str, expect_drain: bool,
                        require_jobs: bool = True) -> tuple[str, str]:
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
    process = data.get("process")
    if not isinstance(process, dict) or process.get("workers") != 1 \
            or isinstance(process.get("workers"), bool):
        return "unavailable", (f"cannot prove a single worker (process={process!r}); the "
                               "drain is per process, so a multi-worker or unidentified "
                               "service cannot be drained")
    if not jobs_ok or "error" in jobs:
        return "unavailable", f"active jobs could not be determined: {jobs.get('error', '?')}"
    bad = [k for k, v in jobs.items() if not _is_count(v)]
    if bad:
        return "unavailable", f"active_jobs values are not counts: {bad}"
    if require_jobs and not jobs:
        return "unavailable", "no active-job evidence (active_jobs is empty) and 'jobs' is not waived"
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
    return "clear", (f"in_flight=0, active_jobs={jobs}, pid={process.get('pid')}, "
                     f"draining={draining}")


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
                    expect_drain: bool = False, require_jobs: bool = True) -> ProbeResult:
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
        return result(*_interpret_inflight(data, lease_id, expect_drain, require_jobs))
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
    """One probe ROUND. Probes run concurrently; the caller checks the round
    finished inside the lease TTL before acting on it."""
    require_jobs = "jobs" not in gate.waived
    async with http_client() as client:
        return list(await asyncio.gather(*[
            run_probe(client, p, lease_id=lease_id, expect_drain=expect_drain,
                      require_jobs=require_jobs)
            for p in gate.probes]))


# ── bootstrap evidence (first load of the middleware) ───────────────────────


async def _run_ss(args: list[str]) -> tuple[int, str]:
    """Seam for tests. Returns (rc, stdout)."""
    proc = await asyncio.create_subprocess_exec(
        "ss", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), 5)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, ""
    return proc.returncode, out.decode(errors="replace")


async def _connections(boot: BootstrapGate) -> ProbeResult:
    name, url = "connections", f"tcp:{boot.port}"
    rc, out = await _run_ss(["-Htn", "state", "established", f"( sport = :{boot.port} )"])
    if rc != 0:
        return ProbeResult(name, "connections", url, "unavailable", f"ss failed rc={rc}")
    peers = [line.split()[-1] for line in out.splitlines() if line.strip()]
    if boot.unix_socket:
        rc, out = await _run_ss(["-Hxn", "state", "established"])
        if rc != 0:
            return ProbeResult(name, "connections", url, "unavailable", f"ss -x failed rc={rc}")
        peers += [f"unix:{boot.unix_socket}" for line in out.splitlines()
                  if boot.unix_socket in line.split()]
    if peers:
        return ProbeResult(name, "connections", url, "busy",
                           f"{len(peers)} ESTABLISHED client connection(s): {', '.join(peers[:5])}")
    return ProbeResult(name, "connections", url, "clear", "no established client connections")


async def _bootstrap_jobs(boot: BootstrapGate) -> ProbeResult:
    name = "bootstrap-jobs"
    if boot.jobs_kind == "http":
        try:
            async with http_client() as client:
                resp = await client.get(boot.jobs_url, timeout=boot.jobs_timeout_s)
            data = resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as exc:
            return ProbeResult(name, "jobs", boot.jobs_url, "unavailable",
                               f"{type(exc).__name__}: {exc}")
        counts = data
        where = boot.jobs_url
    else:
        where = "sql (read-only)"
        try:
            import asyncpg

            conn = await asyncpg.connect(boot.jobs_dsn, timeout=boot.jobs_timeout_s,
                                         server_settings={
                                             "default_transaction_read_only": "on",
                                             "statement_timeout": "3000"})
            try:
                async with conn.transaction(readonly=True):
                    counts = {n: await conn.fetchval(sql) for n, sql in boot.jobs_queries}
            finally:
                await conn.close()
        except Exception as exc:  # noqa: BLE001 -- any failure is "cannot tell"
            return ProbeResult(name, "jobs", where, "unavailable",
                               f"{type(exc).__name__}: {exc}")
    if not isinstance(counts, dict) or not counts \
            or any(not _is_count(v) for v in counts.values()):
        return ProbeResult(name, "jobs", where, "unavailable",
                           f"job evidence is not a non-empty object of counts: {counts!r}")
    active = {k: v for k, v in counts.items() if v}
    if active:
        return ProbeResult(name, "jobs", where, "busy", "active jobs: " + ", ".join(
            f"{k}={v}" for k, v in sorted(active.items())))
    return ProbeResult(name, "jobs", where, "clear", f"{counts}")


async def _middleware_absent(client: httpx.AsyncClient, probe: Probe) -> ProbeResult:
    def result(status, detail):
        return ProbeResult(f"{probe.name}-absent", "bootstrap", probe.url, status, detail)
    try:
        resp = await client.get(probe.url, timeout=probe.timeout_s)
    except httpx.HTTPError as exc:
        return result("unavailable", f"service unreachable: {type(exc).__name__}: {exc}")
    if resp.status_code == 404:
        return result("clear", "in-flight middleware not loaded (404): first load")
    if resp.status_code == 200:
        return result("busy", "the in-flight middleware is ALREADY loaded; bootstrap is "
                              "only for its first load -- use the normal (drained) path")
    return result("unavailable", f"HTTP {resp.status_code}")


async def run_bootstrap_checks(gate: UnitGate) -> list[ProbeResult]:
    """One bootstrap round: middleware absent, GPU queues empty, no active jobs,
    then (LAST, after this round's own HTTP connections are closed) no
    ESTABLISHED client connections to the unit."""
    boot = gate.bootstrap
    if boot is None:
        return [ProbeResult("bootstrap", "bootstrap", "", "unavailable",
                            "no [bootstrap] evidence configured for this unit")]
    inflight = [p for p in gate.probes if p.kind == "inflight"]
    gpu = [p for p in gate.probes if p.kind == "comfyui"]
    results: list[ProbeResult] = []
    async with http_client() as client:
        if not inflight:
            results.append(ProbeResult("bootstrap", "bootstrap", "", "unavailable",
                                       "no inflight probe to prove the middleware is absent"))
        results += await asyncio.gather(*[_middleware_absent(client, p) for p in inflight])
        if "gpu_queue" not in gate.waived:
            results += await asyncio.gather(*[run_probe(client, p) for p in gpu])
    if "jobs" not in gate.waived:
        results.append(await _bootstrap_jobs(boot))
    results.append(await _connections(boot))
    return results


# ── drain ───────────────────────────────────────────────────────────────────


def drain_token(gate: UnitGate) -> str:
    if gate.drain_token_file:
        try:
            return Path(gate.drain_token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get(DRAIN_TOKEN_ENV, "").strip()


async def request_drain(gate: UnitGate, lease_id: str, ttl_s: int) -> tuple[bool, str]:
    """Start or RENEW (same lease id) the service's drain for ttl_s seconds."""
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
