"""The context packet a worker gets when it claims work.

Goal: LESS but BETTER context. Cheap local tokens are spent so the expensive
model starts with the useful part instead of reconstructing it from a giant
transcript. Three rules hold this together.

PROVENANCE IS CARRIED, NOT FLATTENED. A model-written "root cause was X" and an
operator ruling are both text; treating them alike is how an external memory
becomes an efficient way to remember a hallucination forever. Every item states
which it is, and nothing is promoted here — promotion needs an artifact.

EVERY LINE IS ATTRIBUTABLE. Each item carries a citation the reader can go and
check (`ats:decision/<id>`, `echo:mem/<id>`). A brief that cannot be checked is
worse than no brief.

RECALL IS BEST-EFFORT. Echo Brain or ollama being down degrades the packet and
never the claim. Coordination must not wedge real work.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ai_team_sync.models import Decision, Handoff, ScopeLock, Session

logger = logging.getLogger(__name__)

# Provenance, weakest promotion last. Nothing in this module ever moves an item
# UP a level: that takes a commit, a test, a measured result or your ruling.
OBSERVATION = "OBSERVATION"            # raw evidence: a lock, a restart, a commit
INFERRED = "INFERRED"                  # a model's interpretation, including mine
VERIFIED = "VERIFIED"                  # backed by an artifact recorded alongside it
OPERATOR_DECISION = "OPERATOR_DECISION"  # your ruling
WORKER_PROPOSAL = "WORKER_PROPOSAL"    # a worker-authored ATS decision, never operator auth
SEMANTIC_MEMORY = "SEMANTIC_MEMORY"    # ranked supplement, never execution authority

_AUTHORITY_RANK = {OPERATOR_DECISION: 0, VERIFIED: 1, OBSERVATION: 2,
                   INFERRED: 3, WORKER_PROPOSAL: 3, SEMANTIC_MEMORY: 4}

ECHO_URL = os.environ.get("ECHO_BRAIN_URL", "http://localhost:8309")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("ATS_BRIEF_EMBED_MODEL", "nomic-embed-text")


class TaskContextUnavailable(RuntimeError):
    """A named task has no validated exact structured authority."""

    def __init__(self, task_id: int, reason: str):
        self.task_id = task_id
        self.reason = reason
        super().__init__(
            f"exact structured context for task {task_id} unavailable: {reason}")


def _canonical_returned_task_id(value: Any) -> int | None:
    """Accept API integer ids and their unambiguous canonical JSON strings."""
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
        if parsed > 0 and value == str(parsed):
            return parsed
    return None


def _fetch_tower_task(task_id: str | int, *, timeout: float = 10.0
                      ) -> tuple[dict[str, Any] | None, str | None]:
    """(structured envelope, error) for one Tower task, from Echo Brain."""
    try:
        tid = int(str(task_id).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None, f"task id {task_id!r} is not numeric"

    import httpx  # lazy, matching this module's other network callers

    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{ECHO_URL}/api/tower-tasks/{tid}")
    except Exception as exc:  # noqa: BLE001
        return None, f"Echo Brain unreachable at {ECHO_URL} ({type(exc).__name__})"

    if r.status_code == 404:
        return None, f"no Tower task with id {tid}"
    if r.status_code >= 400:
        return None, f"Echo Brain returned HTTP {r.status_code} for task {tid}"

    try:
        env = r.json()
    except Exception:  # noqa: BLE001
        return None, f"Echo Brain returned a non-JSON envelope for task {tid}"
    if not isinstance(env, dict):
        return None, (f"task identity mismatch requested={tid} envelope=malformed "
                      "context=malformed (no structured task_context)")

    envelope_raw = env.get("id", None)
    envelope_id = _canonical_returned_task_id(envelope_raw)
    context = env.get("task_context")
    task = context.get("task") if isinstance(context, dict) else None
    context_raw = task.get("id", None) if isinstance(task, dict) else None
    context_id = _canonical_returned_task_id(context_raw)

    def shown(raw: Any, normalized: int | None) -> str:
        if raw is None:
            return "missing"
        if normalized is None:
            return f"malformed({raw!r})"
        return str(normalized)

    envelope_label = shown(envelope_raw, envelope_id)
    if not isinstance(context, dict) or not isinstance(task, dict):
        context_label = "malformed (no structured task_context)"
    else:
        context_label = shown(context_raw, context_id)
    if envelope_id != tid or context_id != tid:
        return None, (f"task identity mismatch requested={tid} "
                      f"envelope={envelope_label} context={context_label}")
    rulings = context.get("operator_rulings")
    if (not isinstance(rulings, dict)
            or not isinstance(rulings.get("current"), list)
            or not isinstance(rulings.get("history"), list)
            or not isinstance(context.get("verified_facts"), list)
            or not isinstance(context.get("requires_live_verification"), list)):
        return None, (f"malformed structured task_context requested={tid} "
                      f"envelope={envelope_label} context={context_label}")
    if "description" not in env:
        return None, f"envelope for task {tid} is missing its description field"
    return env, None


def fetch_tower_task_context(task_id: str | int, *, timeout: float = 10.0
                             ) -> tuple[dict[str, Any], str | None]:
    """Exact structured context for a Tower task; never semantic reconstruction."""
    env, error = _fetch_tower_task(task_id, timeout=timeout)
    if error:
        return {}, error
    context = (env or {}).get("task_context")
    if not isinstance(context, dict):
        return {}, f"envelope for task {task_id} has no structured task_context"
    return context, None


def fetch_tower_task_data(task_id: str | int, *, timeout: float = 10.0
                          ) -> tuple[dict[str, Any], str | None]:
    """Validated canonical envelope for one exact Tower task."""
    env, error = _fetch_tower_task(task_id, timeout=timeout)
    return (env or {}), error


def resolve_tower_task(objective: str, *, timeout: float = 10.0
                       ) -> tuple[dict[str, Any], str | None]:
    """Resolve free text through Echo's structured Tower-task resolver.

    ATS never promotes semantic recall into task identity.  The resolver may
    return one confident task, explicit ambiguity, or no candidates; only the
    first result is allowed to enter the exact task-context pipeline.
    """
    import httpx

    try:
        with httpx.Client(timeout=timeout) as c:
            response = c.post(f"{ECHO_URL}/api/tower-tasks/resolve",
                              json={"objective": objective, "limit": 5})
    except Exception as exc:  # noqa: BLE001
        return {}, f"Echo Brain task resolver unavailable ({type(exc).__name__})"
    if response.status_code >= 400:
        return {}, f"Echo Brain task resolver returned HTTP {response.status_code}"
    try:
        result = response.json()
    except Exception:  # noqa: BLE001
        return {}, "Echo Brain task resolver returned non-JSON"
    if not isinstance(result, dict) or result.get("status") not in {
            "resolved", "ambiguous", "unresolved"}:
        return {}, "Echo Brain task resolver returned a malformed result"
    if result.get("status") == "resolved" and _canonical_returned_task_id(
            result.get("task_id")) is None:
        return {}, "Echo Brain task resolver returned a malformed task id"
    if not isinstance(result.get("candidates", []), list):
        return {}, "Echo Brain task resolver returned malformed candidates"
    return result, None


_EXPLICIT_TASK_PATTERNS = (
    re.compile(r"^\s*#([1-9][0-9]*)\b", re.IGNORECASE),
    re.compile(r"\b(?:tower\s+)?(?:task|ticket)\s*#([1-9][0-9]*)\b",
               re.IGNORECASE),
    re.compile(r"\b(?:tower\s+)?(?:task|ticket)\s+id\s*#?([1-9][0-9]*)\b",
               re.IGNORECASE),
    re.compile(r"^\s*(?:continue|resume|work\s+on)\s+#([1-9][0-9]*)\b",
               re.IGNORECASE),
    re.compile(
        r"\b(?:status|state|details?|context)\s+(?:of|for)\s+#([1-9][0-9]*)\b",
        re.IGNORECASE,
    ),
)


def explicit_task_id(objective: str) -> int | None:
    """One explicit task/ticket identity, excluding incidental ``#`` refs."""
    ids = {int(match.group(1)) for pattern in _EXPLICIT_TASK_PATTERNS
           for match in pattern.finditer(objective or "")}
    if ids:
        ids.update(int(value) for value in re.findall(
            r"#([1-9][0-9]*)\b", objective or ""))
    return next(iter(ids)) if len(ids) == 1 else None


def fetch_tower_task_envelope(task_id: str | int, *, timeout: float = 10.0
                              ) -> tuple[str, str | None]:
    """(rendered envelope, error) for one Tower task, from Echo Brain.

    Echo Brain owns Tower Tasks, so ATS asks rather than reaching into its
    database, the same way preflight is a thin wrapper over Echo's analysis.

    Returns ("", reason) on ANY failure -- unknown id, Echo down, bad shape --
    and never a partial envelope. The caller decides what a missing envelope
    means; for a delegation that named the task explicitly, it means refuse.
    """
    env, error = _fetch_tower_task(task_id, timeout=timeout)
    return (render_task_envelope(env), None) if env is not None else ("", error)


def render_task_context(context: dict[str, Any]) -> list[str]:
    """Human rendering of structured authority; records remain intact in JSON."""
    out: list[str] = []
    rulings = context.get("operator_rulings") or {}
    current = rulings.get("current") or []
    history = rulings.get("history") or []
    if current:
        out += ["", "  CURRENT OPERATOR RULINGS / PROHIBITIONS:"]
        out.append("    These reviewed structured records supersede conflicting legacy prose.")
        for d in current:
            marker = "PROHIBITION" if d.get("prohibition") else d.get("effect", "RULING")
            out.append(f"    [{marker}] {d.get('ruling') or '(text unavailable; follow citation)'}")
            out.append(f"      id={d.get('id')} operator={((d.get('author') or {}).get('name') or '?')} "
                       f"source={((d.get('source') or {}).get('citation') or '?')}")
    facts = context.get("verified_facts") or []
    if facts:
        out += ["", "  VERIFIED TASK FACTS:"]
        for fact in facts:
            out.append(f"    [VERIFIED] {json.dumps(fact.get('value'), default=str)}")
            out.append(f"      source={fact.get('citation') or '?'}")
    if history:
        out += ["", "  NON-CURRENT RULING HISTORY (NOT AUTHORITY):"]
        for d in history:
            out.append(f"    [{str(d.get('state') or 'historical').upper()}] "
                       f"{d.get('ruling') or '(text unavailable)'}")
            out.append(f"      id={d.get('id')} superseded_by={d.get('superseded_by') or []}")
    checks = context.get("requires_live_verification") or []
    if checks:
        out += ["", "  requires live verification: " + ", ".join(str(x) for x in checks)]
    return out


def render_task_envelope(env: dict[str, Any]) -> str:
    """The envelope as a prompt block.

    Mirrors Echo Brain's own render_envelope so a packet reads the same whether
    the text was rendered here or there. Kept local rather than fetched as
    pre-rendered text so ATS controls what a CHILD sees, and so a caller that
    wants the structured fields is not forced through a string.
    """
    import json as _json

    def _lines(label: str, body: str) -> list[str]:
        return ["", f"  {label}", *[f"    {ln}" for ln in body.splitlines()]]

    out = [
        f"TOWER TASK #{env.get('id')} — {env.get('task_key')}",
        f"  project : {env.get('project_name') or env.get('project_id')}",
        f"  title   : {env.get('title') or ''}",
        f"  status  : {env.get('status')}   gate: {env.get('gate')}"
        f"   priority: {env.get('priority') if env.get('priority') is not None else '?'}",
    ]
    if env.get("parent_id"):
        out.append(f"  parent  : #{env['parent_id']}")
    if env.get("blocked_by"):
        out.append("  blocked_by: " + ", ".join(str(b) for b in env["blocked_by"]))

    relations = env.get("relations") or {}
    children = relations.get("children") or []
    residual_ids = set(relations.get("active_residuals") or [])
    if residual_ids:
        out += ["", "  ACTIVE RESIDUALS (from current child status):",
                "    " + ", ".join(f"#{task_id}" for task_id in sorted(residual_ids))]
    if children:
        out += ["", "  LINKED CHILD TASKS (current Tower relations):"]
        for child in children:
            residual = " ACTIVE RESIDUAL" if child.get("id") in residual_ids else ""
            out.append(
                f"    #{child.get('id')} [{child.get('status')}]"
                f"{residual} {child.get('title') or child.get('task_key') or ''}")
            if child.get("verified_by"):
                evidence_label = ("deployed/live evidence" if child.get("status") in {
                    "completed", "skipped", "cancelled"}
                    else "historical closure evidence (task is currently open)")
                out.append(f"      {evidence_label}: "
                           + _json.dumps(child["verified_by"], default=str))
    dependencies = relations.get("dependencies") or []
    if dependencies:
        out += ["", "  LINKED BLOCKERS / DEPENDENCIES:"]
        for dep in dependencies:
            out.append(f"    #{dep.get('id')} [{dep.get('status')}] "
                       f"{dep.get('title') or dep.get('task_key') or ''}")
    successors = relations.get("successors") or []
    if successors:
        out += ["", "  SUCCESSOR TASKS:"]
        for successor in successors:
            out.append(f"    #{successor.get('id')} [{successor.get('status')}] "
                       f"{successor.get('title') or successor.get('task_key') or ''}")
    successor_references = relations.get("successor_references") or []
    if successor_references:
        out += ["", "  EXACT SUCCESSOR REFERENCES (NOT STRUCTURED AUTHORITY):"]
        for successor in successor_references:
            out.append(f"    #{successor.get('id')} [{successor.get('status')}] "
                       f"{successor.get('title') or successor.get('task_key') or ''}")
            out.append(f"      source={successor.get('citation')} authority="
                       "EXACT_TASK_REFERENCE current_authority=false")

    claim = env.get("claim") or None
    if claim:
        out.append(f"  claim   : run {claim.get('run_id')} state={claim.get('state')} "
                   f"executor={claim.get('executor')} "
                   f"lease_expires={claim.get('lease_expires_at')}")

    if env.get("is_closed"):
        out += ["",
                "  *** THIS TASK IS ALREADY CLOSED. The work may already be shipped.",
                "      Verify before doing anything. Closure evidence:",
                f"      {_json.dumps(env.get('verified_by'), default=str)}"]
    elif env.get("verified_by"):
        out.append("  historical closure evidence (task is currently open; "
                   "not current VERIFIED): "
                   + _json.dumps(env["verified_by"], default=str))

    if isinstance(env.get("task_context"), dict):
        out += render_task_context(env["task_context"])

    if env.get("recommendation"):
        out += _lines("TASK RECOMMENDATION — LEGACY PROSE; OPERATOR IDENTITY NOT AUTHENTICATED:",
                      env["recommendation"])

    out += ["",
            "  DESCRIPTION — the task's authority: the required change, the",
            "  acceptance criteria, and the prohibited approaches. Binding.",
            ""]
    out += [f"    {ln}" for ln in (env.get("description") or "(empty)").splitlines()]

    if env.get("notes"):
        out += _lines("HISTORICAL / LEGACY NOTES (NOT CURRENT AUTHORITY):",
                      env["notes"])
    return "\n".join(out)


@dataclass
class BriefItem:
    provenance: str
    text: str
    citation: str
    score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"provenance": self.provenance, "text": self.text,
                "citation": self.citation, "score": round(self.score, 4),
                "meta": self.meta}


def classify_memory(result: dict[str, Any]) -> str:
    """Provenance of one Echo Brain hit, read from the store, never guessed.

    Semantic retrieval cannot prove current task authority. Even an
    operator-curated source may be old, revoked, or about another task; exact
    structured decisions are the only OPERATOR_DECISION path.
    """
    # A vector payload can report its source standing, but it cannot prove that
    # a ruling is current, task-bound, human-reviewed, or not superseded. Exact
    # structured context owns those claims. Keep the trust marker in metadata;
    # the semantic hit itself remains supplemental.
    return SEMANTIC_MEMORY


def _embed(texts: list[str]) -> list[list[float]]:
    """Local embeddings. nomic-embed-text is the resident model; this must not
    load anything else. Residency and the RAM floor are infrastructure
    constraints, not something a brief gets to override by asking."""
    import httpx

    resp = httpx.post(f"{OLLAMA_URL}/api/embed",
                      json={"model": EMBED_MODEL, "input": texts}, timeout=8)
    resp.raise_for_status()
    return resp.json()["embeddings"]


def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
    a, b = list(a), list(b)
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def rerank_by_similarity(objective: str, items: list[BriefItem]) -> list[BriefItem]:
    """Order recall against the objective using the local embedding model.

    This is the cheap half of the pipeline: the ranking a hybrid search returns
    is about the query string, not about what this worker is actually here to
    do. Any failure keeps the original order — a worse-ordered brief beats a
    failed claim.
    """
    if not items:
        return items
    try:
        vectors = _embed([objective] + [i.text for i in items])
        target, rest = vectors[0], vectors[1:]
        for item, vec in zip(items, rest):
            item.score = _cosine(target, vec)
        return sorted(items, key=lambda i: -i.score)
    except Exception as exc:  # noqa: BLE001
        logger.info("brief rerank skipped (%s); keeping source order", exc)
        return items


def compress(items: list[BriefItem], max_items: int = 8,
             max_chars: int = 400) -> list[BriefItem]:
    """Dedup, rank by authority then similarity, and budget.

    Compression here is deterministic on purpose. A generative summariser would
    be a second place for a model to invent a fact, and it would have to load a
    model the residency policy keeps evicted. Long items are CUT, never
    reworded, so the citation still leads to the whole thing.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[BriefItem] = []
    for item in items:
        key = (" ".join(item.text.split())[:160].lower(), item.citation)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    unique.sort(key=lambda i: (_AUTHORITY_RANK.get(i.provenance, 9), -i.score))

    out: list[BriefItem] = []
    for item in unique[:max_items]:
        text = " ".join(item.text.split())
        if len(text) > max_chars:
            text = text[:max_chars] + "..."
        out.append(BriefItem(item.provenance, text, item.citation, item.score, item.meta))
    return out


def recall_memories(objective: str, limit: int = 12, *, task_id: int | None = None,
                    task_key: str | None = None, project_id: int | None = None,
                    repo_root: str = "") -> list[dict[str, Any]]:
    """Echo Brain hybrid search. Synchronous by design so it can be swapped in
    tests and run off the event loop via a thread."""
    import httpx

    body: dict[str, Any] = {"query": objective, "limit": limit}
    if task_id is not None:
        body["task_id"] = task_id
    if task_key:
        body["task_key"] = task_key
    if project_id is not None:
        body["project_id"] = project_id
    if repo_root:
        body["repo_root"] = repo_root
    resp = httpx.post(f"{ECHO_URL}/api/echo/memory/search", json=body, timeout=10)
    resp.raise_for_status()
    return resp.json().get("results") or []


def _memory_citation(result: dict[str, Any]) -> str:
    """Something the reader can actually go and look at.

    Echo returns an empty `id` for hybrid hits and a collection name as
    `source`, so the obvious fallbacks produce 'echo:mem/qdrant/echo_memory' on
    every line — present, uniform, and useless. The file path is the checkable
    handle when there is one; otherwise a content digest at least distinguishes
    two hits and can be searched for verbatim.
    """
    payload = result.get("payload") or {}
    # A memory that states its own citation outranks anything derived. Clerk
    # rows carry 'project_facts/<id> · ats:session/<id>', which is the whole
    # provenance chain; falling through to a content digest threw that away.
    if payload.get("citation"):
        return str(payload["citation"])
    for key in ("file_path", "path", "url", "source_file"):
        if payload.get(key):
            return f"echo:{payload[key]}"
    if result.get("id"):
        return f"echo:mem/{result['id']}"
    digest = hashlib.sha1(str(result.get("content") or "").encode()).hexdigest()[:12]
    return f"echo:sha1/{digest}"


def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; the comparison must not explode on that."""
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def _scope_of(session: Session) -> list[str]:
    try:
        return json.loads(session.scope or "[]")
    except Exception:  # noqa: BLE001
        return []


def _same_repo(row_root: str, repo_root: str) -> bool:
    """Unanchored ('') matches everything — the legacy rule used everywhere else."""
    a, b = (row_root or "").rstrip("/"), (repo_root or "").rstrip("/")
    return not a or not b or a == b


def _overlaps(pattern: str, scope: list[str]) -> bool:
    if not scope:
        return True
    return any(fnmatch(p, pattern) or fnmatch(pattern, p) or p == pattern for p in scope)


# Phrasings that mark a standing prohibition. Used ONLY to raise a cheap hint,
# never to assign authority — an agent's wording cannot make its own note a
# ruling, which is the mistake preflight had to unlearn.
_PROHIBITION = ("do not ", "don't ", "must not", "never ", "stand down",
                "prohibited", "do NOT")


def preflight_hint(decisions: list[BriefItem], recall: list[BriefItem]
                   ) -> tuple[bool, str | None]:
    """Should this worker run a preflight before doing real work?

    Deterministic and free: it reads material the brief ALREADY gathered and
    runs no query, no embedding and no model. It is a TRIGGER, not an analysis —
    the analysis lives in Echo Brain's preflight, which does the deterministic
    matching, change-since check and authority ordering this cannot.

    False means the cheap trigger found nothing obvious. It does NOT mean a
    preflight would come back CLEAR.
    """
    for item in recall:
        if item.provenance == OPERATOR_DECISION:
            return True, f"an operator ruling is in scope ({item.citation})"

    for item in decisions + recall:
        text = (item.text or "").lower()
        if any(p in text for p in _PROHIBITION):
            return True, f"prior work records a prohibition ({item.citation})"
        if "failed_approach" in text or "fails_how" in text:
            return True, f"a failed approach is already recorded ({item.citation})"
    return False, None


async def build_brief(db: AsyncSession, *, objective: str, repo_root: str = "",
                      scope: list[str] | None = None, recall: bool = True,
                      limit: int = 8, caller_session_id: str | None = None,
                      caller_identity_unresolved: bool = False,
                      task_id: int | None = None,
                      render_task_context: bool = True,
                      resolve_task: bool = True) -> dict[str, Any]:
    """Assemble the packet. ATS state is authoritative and local; Echo Brain
    recall is additive and may be absent.

    `caller_session_id` is the session this brief is FOR, already validated
    against the #2741 identity boundary by the caller of this function. Its own
    locks are not blockers (#2757) -- start_session creates a session's locks and
    then builds its brief, so without this every session was handed its own
    brand-new claims under BLOCKERS NOW. Unresolved (None) keeps every lock, the
    same conservative rule pre-commit-check applies."""
    scope = scope or []

    # A named task is an authority claim, not a recall hint. Resolve and
    # validate it before reading local proposals, locks, or semantic memory so
    # no partial/degraded packet can be mistaken for task authority.
    task_resolution: dict[str, Any]
    explicit_from_objective = (
        explicit_task_id(objective) if task_id is None and resolve_task else None)
    if task_id is not None:
        task_resolution = {"status": "resolved", "method": "explicit_parameter",
                           "task_id": task_id, "confidence": 1.0, "candidates": []}
    elif explicit_from_objective is not None:
        task_id = explicit_from_objective
        task_resolution = {"status": "resolved", "method": "explicit_objective_id",
                           "task_id": task_id, "confidence": 1.0, "candidates": []}
    elif resolve_task:
        try:
            task_resolution, resolution_error = await asyncio.to_thread(
                resolve_tower_task, objective)
        except Exception as exc:  # noqa: BLE001
            task_resolution, resolution_error = {}, (
                f"task resolution failed ({type(exc).__name__})")
        if resolution_error:
            task_resolution = {"status": "unavailable", "method": "structured_resolver",
                               "task_id": None, "confidence": 0.0, "candidates": [],
                               "reason": resolution_error}
        else:
            task_resolution = {"method": "structured_resolver", **task_resolution}
            if task_resolution.get("status") == "resolved":
                task_id = int(task_resolution["task_id"])
    else:
        task_resolution = {
            "status": "project_scoped", "method": "repo_root",
            "task_id": None, "confidence": 1.0, "candidates": [],
        }

    ambiguous = task_resolution.get("status") == "ambiguous"
    task_envelope: dict[str, Any] = {}
    task_context: dict[str, Any] = {}
    task_context_status = "not requested"
    if task_id is not None:
        try:
            task_envelope, task_error = await asyncio.to_thread(
                fetch_tower_task_data, task_id)
        except Exception as exc:  # noqa: BLE001
            raise TaskContextUnavailable(
                task_id, f"task context lookup failed ({type(exc).__name__})") from exc
        task_context = task_envelope.get("task_context", {}) \
            if isinstance(task_envelope, dict) else {}
        if task_error or not isinstance(task_context, dict) or not task_context:
            raise TaskContextUnavailable(
                task_id, task_error or "empty structured task_context")
        task_context_status = "ok"

    # A lock is live while its session is active and it has not expired; there
    # is no released_at column, release is the session completing.
    now = datetime.now(timezone.utc)
    locks = (await db.execute(
        select(ScopeLock).options(selectinload(ScopeLock.session))
    )).scalars().all()
    blockers = [
        BriefItem(
            OBSERVATION,
            f"'{lk.pattern}' is claimed by {lk.session.agent} ({lk.mode}): "
            f"{lk.session.description or 'no description'}",
            f"ats:lock/{lk.id}",
        )
        for lk in locks
        if lk.session and lk.session.status == "active"
        and _aware(lk.expires_at) > now
        and _same_repo(lk.session.repo_root, repo_root)
        and _overlaps(lk.pattern, scope)
        and not (caller_session_id and lk.session_id == caller_session_id)
    ]

    decision_query = select(Decision).options(selectinload(Decision.session))
    if task_id is not None:
        decision_query = decision_query.where(Decision.ticket_id == task_id)
    decision_rows = (await db.execute(
        decision_query.order_by(Decision.created_at.desc()).limit(60)
    )).scalars().all()
    decisions = [] if ambiguous else [
        BriefItem(
            WORKER_PROPOSAL,
            f"{d.title} — chose: {d.chosen}" + (f" (because {d.reasoning})" if d.reasoning else ""),
            f"ats:decision/{d.id} by {d.session.agent}",
            meta={"category": WORKER_PROPOSAL, "decision_id": d.id,
                  "ticket_id": d.ticket_id, "author": d.session.agent,
                  "authenticated_operator": False,
                  "created_at": d.created_at.isoformat() if d.created_at else None},
        )
        for d in decision_rows
        if d.session and _same_repo(d.session.repo_root, repo_root)
    ]

    prior_rows = (await db.execute(
        select(Session).options(selectinload(Session.commits))
        .where(Session.status == "completed")
        .where(Session.ticket_id == task_id if task_id is not None else True)
        .order_by(Session.completed_at.desc()).limit(40)
    )).scalars().all()
    prior_work = [] if ambiguous else [
        BriefItem(
            VERIFIED if s.commits else INFERRED,
            f"{s.agent}: {s.summary}",
            f"ats:session/{s.id}" + (f" ({len(s.commits)} commit(s))" if s.commits else ""),
        )
        for s in prior_rows
        if s.summary and _same_repo(s.repo_root, repo_root)
        and any(_overlaps(p, scope) for p in _scope_of(s) or [""])
    ]

    memories: list[BriefItem] = []
    recall_status = "suppressed (ambiguous Tower task)" if ambiguous else "skipped"
    if recall and not ambiguous:
        try:
            results = await asyncio.to_thread(
                recall_memories, objective, 12, task_id=task_id,
                repo_root=repo_root)
            memories = [
                BriefItem(classify_memory(r), str(r.get("content") or ""),
                          _memory_citation(r), float(r.get("score") or 0.0),
                          {"payload": r.get("payload") or r.get("metadata") or {},
                           "task_scoped": task_id is not None})
                for r in results
            ]
            recall_status = f"ok ({len(memories)} candidate(s))"
        except Exception as exc:  # noqa: BLE001 — never fail the claim
            recall_status = f"unavailable ({type(exc).__name__})"
            logger.info("brief recall unavailable: %s", exc)

    # One batched rerank across every section. Repo-filtering is not relevance:
    # without this the top decisions were whatever happened most recently in the
    # repo, which is how a brief becomes noise the worker learns to skip.
    ranked = await asyncio.to_thread(
        rerank_by_similarity, objective, decisions + prior_work + memories)
    order = {id(i): n for n, i in enumerate(ranked)}
    for bucket in (decisions, prior_work, memories):
        bucket.sort(key=lambda i: order.get(id(i), 10_000))

    recommended, why = preflight_hint(decisions, memories)

    latest_handoff: dict[str, Any] | None = None
    recent_handoffs: list[dict[str, Any]] = []
    if task_id is not None:
        handoff = (await db.execute(
            select(Handoff).where(Handoff.ticket_id == task_id)
            .order_by(Handoff.created_at.desc(), Handoff.id.desc()).limit(1)
        )).scalar_one_or_none()
        if handoff is not None:
            def _json_list(value: str) -> list[Any]:
                try:
                    parsed = json.loads(value or "[]")
                    return parsed if isinstance(parsed, list) else []
                except Exception:  # noqa: BLE001
                    return []
            task_updated_at: datetime | None = None
            try:
                raw_updated = task_envelope.get("updated_at")
                if raw_updated:
                    task_updated_at = datetime.fromisoformat(
                        str(raw_updated).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                task_updated_at = None
            handoff_created_at = _aware(handoff.created_at) if handoff.created_at else None
            superseded = bool(task_updated_at and handoff_created_at
                              and _aware(task_updated_at) > handoff_created_at)
            latest_handoff = {
                "id": handoff.id,
                "ticket_id": handoff.ticket_id,
                "source_session_id": handoff.source_session_id,
                "verdict": handoff.verdict,
                "blockers": _json_list(handoff.blockers),
                "next_steps": _json_list(handoff.next_steps),
                "artifacts": _json_list(handoff.artifacts),
                "created_at": (handoff.created_at.isoformat()
                               if handoff.created_at else None),
                "authority_state": "superseded" if superseded else "current",
                "superseded_by": "tower_task.updated_at" if superseded else None,
            }
    elif not resolve_task and repo_root:
        # A broad project status has no one authoritative ticket handoff. Carry
        # the recent repo-scoped continuation records separately and label them
        # below Tower/operator authority instead of pretending one is current.
        handoff_rows = (await db.execute(
            select(Handoff, Session)
            .join(Session, Handoff.source_session_id == Session.id)
            .where(Session.repo_root == repo_root.rstrip("/"))
            .order_by(Handoff.created_at.desc(), Handoff.id.desc())
            .limit(limit)
        )).all()
        for handoff, source in handoff_rows:
            def _json_list(value: str) -> list[Any]:
                try:
                    parsed = json.loads(value or "[]")
                    return parsed if isinstance(parsed, list) else []
                except Exception:  # noqa: BLE001
                    return []

            recent_handoffs.append({
                "id": handoff.id,
                "ticket_id": handoff.ticket_id,
                "source_session_id": handoff.source_session_id,
                "source_agent": source.agent,
                "verdict": handoff.verdict,
                "blockers": _json_list(handoff.blockers),
                "next_steps": _json_list(handoff.next_steps),
                "artifacts": _json_list(handoff.artifacts),
                "created_at": (handoff.created_at.isoformat()
                               if handoff.created_at else None),
            })

    packet = {
        "preflight_recommended": recommended,
        "preflight_reason": why,
        "objective": objective,
        "repo_root": repo_root,
        "scope": scope,
        "caller_session_id": caller_session_id,
        "caller_identity_unresolved": caller_identity_unresolved,
        "task_id": task_id,
        "task_resolution": task_resolution,
        "task_envelope": task_envelope,
        "task_context": task_context,
        "task_context_status": task_context_status,
        "latest_handoff": latest_handoff,
        "recent_handoffs": recent_handoffs,
        "render_task_context": render_task_context,
        "blockers": [i.as_dict() for i in compress(blockers, max_items=limit)],
        "decisions": [i.as_dict() for i in compress(decisions, max_items=limit)],
        "prior_work": [i.as_dict() for i in compress(prior_work, max_items=limit)],
        "recall": [i.as_dict() for i in compress(memories, max_items=limit)],
        "recall_status": recall_status,
    }
    packet["rendered"] = render(packet)
    return packet


def render(packet: dict[str, Any]) -> str:
    """Text form, for injection straight into a worker's turn."""
    task_id = packet.get("task_id")
    envelope = packet.get("task_envelope") or {}
    context = packet.get("task_context") or {}
    if task_id is not None and (not isinstance(envelope, dict) or not envelope
                                or not isinstance(context, dict) or not context):
        raise TaskContextUnavailable(
            task_id, str(packet.get("task_context_status") or
                         "missing structured task_context"))

    lines = [f"TASK BRIEF — {packet['objective']}"]
    if packet.get("repo_root"):
        lines.append(f"repo: {packet['repo_root']}")
    if packet.get("scope"):
        lines.append("scope: " + ", ".join(packet["scope"]))

    resolution = packet.get("task_resolution") or {}
    if resolution.get("status") == "project_scoped":
        lines += ["", "PROJECT / REPOSITORY CONTEXT — no exact task authority selected",
                  "  ATS coordination below is repo-scoped. Current task-specific operator "
                  "rulings require an exact ticket; do not infer them from project history."]
    elif resolution.get("status") == "ambiguous":
        lines += ["", "AMBIGUOUS TOWER TASK — no task authority was selected",
                  "  Name an exact ticket; semantic memories are suppressed until then."]
        for candidate in resolution.get("candidates") or []:
            lines.append(f"  #{candidate.get('id')} score={candidate.get('score')} "
                         f"{candidate.get('title') or candidate.get('task_key') or ''}")
    elif resolution.get("status") == "unavailable":
        lines += ["", "TOWER TASK RESOLUTION UNAVAILABLE — no task authority selected",
                  f"  {resolution.get('reason') or 'structured resolver unavailable'}"]

    if envelope and packet.get("render_task_context", True):
        lines += ["", "EXACT TASK-SCOPED CONTEXT — authoritative structured records",
                  render_task_envelope(envelope)]
    elif packet.get("task_id") and packet.get("render_task_context", True):
        lines += ["", "EXACT TASK-SCOPED CONTEXT UNAVAILABLE",
                  f"  {packet.get('task_context_status')}",
                  "  Do not infer current rulings or prohibitions from semantic recall."]

    handoff = packet.get("latest_handoff") or None
    if handoff:
        handoff_title = (
            "LATEST TASK HANDOFF — SUPERSEDED BY NEWER TOWER STATE"
            if handoff.get("authority_state") == "superseded"
            else ("LATEST CURRENT TASK HANDOFF (WORKER CONTINUATION; BELOW "
                  "TOWER / OPERATOR AUTHORITY)"))
        lines += ["", handoff_title,
                  f"  verdict: {handoff.get('verdict')}",
                  f"  source: ats:handoff/{handoff.get('id')} from session "
                  f"{handoff.get('source_session_id')} at {handoff.get('created_at')}"]
        if handoff.get("authority_state") == "superseded":
            lines.append("  Historical continuation context only; current Tower "
                         "identity/status/scope/relations above take precedence.")
        if handoff.get("blockers"):
            lines.append("  blockers: " + ", ".join(map(str, handoff["blockers"])))
        if handoff.get("next_steps"):
            lines.append("  next steps: " + ", ".join(map(str, handoff["next_steps"])))
        if handoff.get("artifacts"):
            lines.append("  artifacts: " + ", ".join(map(str, handoff["artifacts"])))

    project_handoffs = packet.get("recent_handoffs") or []
    if project_handoffs:
        lines += ["", "RECENT REPOSITORY HANDOFFS — continuation context, not task authority"]
        for row in project_handoffs:
            lines.append(f"  [#{row.get('ticket_id')}] {row.get('verdict')}")
            lines.append(f"      source=ats:handoff/{row.get('id')} from "
                         f"{row.get('source_agent')} at {row.get('created_at')}")
            if row.get("blockers"):
                lines.append("      blockers: " + ", ".join(map(str, row["blockers"])))
            if row.get("next_steps"):
                lines.append("      next steps: " + ", ".join(map(str, row["next_steps"])))

    recall_title = ("TASK-SCOPED SEMANTIC SUPPLEMENT" if packet.get("task_id")
                    else "UNSCOPED SEMANTIC RECALL")
    prior = packet.get("prior_work") or []
    sections = [
        ("VERIFIED PRIOR WORK", "prior_work",
         [i for i in prior if i.get("provenance") == VERIFIED]),
        ("BLOCKERS NOW", "blockers", packet.get("blockers") or []),
        ("OBSERVATIONS / INFERENCES FROM PRIOR WORK", "prior_work",
         [i for i in prior if i.get("provenance") != VERIFIED]),
        ("WORKER PROPOSALS", "decisions", packet.get("decisions") or []),
        (recall_title, "recall", packet.get("recall") or []),
    ]
    for title, key, items in sections:
        if not items:
            continue
        lines += ["", title]
        for i in items:
            lines.append(f"  [{i['provenance']}] {i['text']}")
            lines.append(f"      ↳ {i['citation']}")
        if key == "blockers" and packet.get("caller_identity_unresolved"):
            lines.append("  (caller identity unresolved — your own locks may be "
                         "listed above; pass session_id to exclude them)")

    if packet.get("preflight_recommended"):
        lines += ["", "PREFLIGHT RECOMMENDED before you spend real work",
                  f"  {packet.get('preflight_reason')}",
                  "  Call the `preflight` tool with what you are about to do. "
                  "This hint is a cheap trigger, not an answer."]

    lines += ["", f"recall: {packet['recall_status']}",
              "Provenance is carried, not merged: INFERRED is somebody's reading, "
              "OPERATOR_DECISION is a ruling. Check a citation before you rely on it."]
    return "\n".join(lines)
