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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ai_team_sync.models import Decision, ScopeLock, Session

logger = logging.getLogger(__name__)

# Provenance, weakest promotion last. Nothing in this module ever moves an item
# UP a level: that takes a commit, a test, a measured result or your ruling.
OBSERVATION = "OBSERVATION"            # raw evidence: a lock, a restart, a commit
INFERRED = "INFERRED"                  # a model's interpretation, including mine
VERIFIED = "VERIFIED"                  # backed by an artifact recorded alongside it
OPERATOR_DECISION = "OPERATOR_DECISION"  # your ruling

_AUTHORITY_RANK = {OPERATOR_DECISION: 0, VERIFIED: 1, OBSERVATION: 2, INFERRED: 3}

ECHO_URL = os.environ.get("ECHO_BRAIN_URL", "http://localhost:8309")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("ATS_BRIEF_EMBED_MODEL", "nomic-embed-text")


def fetch_tower_task_envelope(task_id: str | int, *, timeout: float = 10.0
                              ) -> tuple[str, str | None]:
    """(rendered envelope, error) for one Tower task, from Echo Brain.

    Echo Brain owns Tower Tasks, so ATS asks rather than reaching into its
    database, the same way preflight is a thin wrapper over Echo's analysis.

    Returns ("", reason) on ANY failure -- unknown id, Echo down, bad shape --
    and never a partial envelope. The caller decides what a missing envelope
    means; for a delegation that named the task explicitly, it means refuse.
    """
    try:
        tid = int(str(task_id).strip().lstrip("#"))
    except (TypeError, ValueError):
        return "", f"task id {task_id!r} is not numeric"

    import httpx  # lazy, matching this module's other network callers

    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{ECHO_URL}/api/tower-tasks/{tid}")
    except Exception as exc:  # noqa: BLE001
        return "", f"Echo Brain unreachable at {ECHO_URL} ({type(exc).__name__})"

    if r.status_code == 404:
        return "", f"no Tower task with id {tid}"
    if r.status_code >= 400:
        return "", f"Echo Brain returned HTTP {r.status_code} for task {tid}"

    try:
        env = r.json()
    except Exception:  # noqa: BLE001
        return "", f"Echo Brain returned a non-JSON envelope for task {tid}"
    if not isinstance(env, dict) or "description" not in env:
        return "", f"envelope for task {tid} is missing its description field"

    return render_task_envelope(env), None


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
        out.append(f"  verified_by: {_json.dumps(env['verified_by'], default=str)}")

    if env.get("recommendation"):
        out += _lines("RECOMMENDATION / OPERATOR RULING ON THIS TASK:",
                      env["recommendation"])

    out += ["",
            "  DESCRIPTION — the task's authority: the required change, the",
            "  acceptance criteria, and the prohibited approaches. Binding.",
            ""]
    out += [f"    {ln}" for ln in (env.get("description") or "(empty)").splitlines()]

    if env.get("notes"):
        out += _lines("NOTES:", env["notes"])
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
                "citation": self.citation, "score": round(self.score, 4)}


def classify_memory(result: dict[str, Any]) -> str:
    """Provenance of one Echo Brain hit, read from the store, never guessed.

    Echo stamps `payload.trust`; 'operator_memory' is the operator's own
    standing rule. Anything else, including an absent marker, is INFERRED. The
    asymmetry is deliberate: an unmarked memory must never be promoted by
    default, because the default is what gets applied silently forever.
    """
    payload = result.get("payload") or {}
    trust = str(payload.get("trust") or "").lower()
    if trust in ("operator_memory", "operator", "operator_decision"):
        return OPERATOR_DECISION
    if trust in ("verified", "artifact"):
        return VERIFIED
    return INFERRED


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


def recall_memories(objective: str, limit: int = 12) -> list[dict[str, Any]]:
    """Echo Brain hybrid search. Synchronous by design so it can be swapped in
    tests and run off the event loop via a thread."""
    import httpx

    resp = httpx.post(f"{ECHO_URL}/api/echo/memory/search",
                      json={"query": objective, "limit": limit}, timeout=10)
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
                      limit: int = 8) -> dict[str, Any]:
    """Assemble the packet. ATS state is authoritative and local; Echo Brain
    recall is additive and may be absent."""
    scope = scope or []

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
    ]

    decision_rows = (await db.execute(
        select(Decision).options(selectinload(Decision.session))
        .order_by(Decision.created_at.desc()).limit(60)
    )).scalars().all()
    decisions = [
        BriefItem(
            INFERRED,
            f"{d.title} — chose: {d.chosen}" + (f" (because {d.reasoning})" if d.reasoning else ""),
            f"ats:decision/{d.id} by {d.session.agent}",
        )
        for d in decision_rows
        if d.session and _same_repo(d.session.repo_root, repo_root)
    ]

    prior_rows = (await db.execute(
        select(Session).options(selectinload(Session.commits))
        .where(Session.status == "completed")
        .order_by(Session.completed_at.desc()).limit(40)
    )).scalars().all()
    prior_work = [
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
    recall_status = "skipped"
    if recall:
        try:
            results = await asyncio.to_thread(recall_memories, objective, 12)
            memories = [
                BriefItem(classify_memory(r), str(r.get("content") or ""),
                          _memory_citation(r), float(r.get("score") or 0.0))
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

    packet = {
        "preflight_recommended": recommended,
        "preflight_reason": why,
        "objective": objective,
        "repo_root": repo_root,
        "scope": scope,
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
    lines = [f"TASK BRIEF — {packet['objective']}"]
    if packet.get("repo_root"):
        lines.append(f"repo: {packet['repo_root']}")
    if packet.get("scope"):
        lines.append("scope: " + ", ".join(packet["scope"]))

    sections = [
        ("BLOCKERS NOW", "blockers"),
        ("PRIOR DECISIONS", "decisions"),
        ("PRIOR WORK IN THIS SCOPE", "prior_work"),
        ("RECALL", "recall"),
    ]
    for title, key in sections:
        items = packet.get(key) or []
        if not items:
            continue
        lines += ["", title]
        for i in items:
            lines.append(f"  [{i['provenance']}] {i['text']}")
            lines.append(f"      ↳ {i['citation']}")

    if packet.get("preflight_recommended"):
        lines += ["", "PREFLIGHT RECOMMENDED before you spend real work",
                  f"  {packet.get('preflight_reason')}",
                  "  Call the `preflight` tool with what you are about to do. "
                  "This hint is a cheap trigger, not an answer."]

    lines += ["", f"recall: {packet['recall_status']}",
              "Provenance is carried, not merged: INFERRED is somebody's reading, "
              "OPERATOR_DECISION is a ruling. Check a citation before you rely on it."]
    return "\n".join(lines)
