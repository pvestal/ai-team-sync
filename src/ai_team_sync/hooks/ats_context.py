#!/usr/bin/env python3
"""UserPromptSubmit hook: resolve governed work through ATS before the model.

The SessionStart hook establishes identity.  This hook supplies the missing
second half: as soon as a prompt exposes an exact task, a governed project name,
or a governed repository cwd, it anchors the session and obtains the ATS brief.
It requests ATS-only project context, then invokes any configured Echo hook as a
supplemental-memory stage in the same process so sibling-hook concurrency cannot
reverse their order. A governed prompt fails closed if ATS cannot provide
context, while unrelated generic prompts remain unscoped.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import replace
from typing import Any

from ai_team_sync import session_pointer as sp
from ai_team_sync.context_resolution import (
    RequestTarget,
    context_roots,
    governed_roots,
    is_subagent_handback,
    resolve_request_target,
)
from ai_team_sync.hooks.session_registration import RegistrationInput, lifecycle_session_key
from ai_team_sync.hooks.session_registration import ensure_session as ensure_registered_session
from ai_team_sync.operator_config import OperatorConfigError
from ai_team_sync.session_marker import AUTOREG_DESCRIPTION, is_autoregistered


class ContextResolutionError(RuntimeError):
    """A governed prompt could not obtain its mandatory ATS context."""


class AnchorRefused(ContextResolutionError):
    """The session may not move to the cwd's repository. Advisory, never a block."""


def _response_error(response: Any) -> str:
    try:
        body = response.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("error") or detail)
        return str(detail)
    except Exception:  # noqa: BLE001
        return str(getattr(response, "text", "request failed"))[:300]


def _session_id(cid: str) -> str:
    explicit = (os.environ.get("ATS_SESSION_ID") or "").strip()
    return explicit or (sp.resolve_pointer(cid, allow_global=False) or "")


def _token(session_id: str, cid: str) -> str:
    """This exact session's capability, or ''. A keyring that cannot be read
    safely is refused and reported, never read as another session's (#3593)."""
    try:
        return sp.load_approval_token(session_id, cid=cid) or ""
    except sp.CapabilityStoreError as exc:
        print(f"ATS: capability keyring refused ({exc}); acting without a capability.",
              file=sys.stderr)
        return ""


_is_subagent_handback = is_subagent_handback

# Discovery runs for every request that has something to search for (#3591):
# short work like "fix parser bug" included. Only a turn with no searchable
# word at all ("go ahead", "yes, ship it") skips it -- an empty query, not a
# length rule.
_CONTENT_WORD = __import__("re").compile(r"[a-z][a-z0-9_]{2,}")
_CHAT_WORDS = frozenset(
    "the and for you yes not this that with from are was can now then just please "
    "go ahead ok okay sure thanks thank good great looks fine ship it do does did".split())


def _content_words(prompt: str) -> set[str]:
    return {w for w in _CONTENT_WORD.findall(prompt.lower()) if w not in _CHAT_WORDS}


def _context_description(repo_root: str) -> str:
    # Keep the marker prefix so a later start_session still recognizes and
    # adopts this identity/context placeholder instead of leaving an orphan.
    return (
        f"{AUTOREG_DESCRIPTION}; context resolved to {repo_root}; " "file scope remains unclaimed"
    )


def _is_context_only_placeholder(session: dict[str, Any]) -> bool:
    """True when changing the anchor cannot move any claimed work.

    A worker whose cwd moves into another governed repository before it has
    claimed scope or recorded work may follow it there.  Once the row carries
    any authority or durable work, its repository remains fixed and the
    existing fail-closed path applies.
    """
    count_fields = ("lock_count", "decision_count", "commit_count")
    return (
        is_autoregistered(str(session.get("description") or ""))
        and not list(session.get("scope") or [])
        # These server-authored fields are mandatory evidence.  Treat an older
        # or partial response as unsafe instead of silently assuming zero work.
        and all(field in session for field in count_fields)
        and all(int(session[field]) == 0 for field in count_fields)
        and session.get("ticket_id") is None
    )


def _within(path: str, root: str) -> bool:
    current = os.path.realpath(path).rstrip("/")
    base = os.path.realpath(root).rstrip("/")
    return bool(current and base) and (current == base or current.startswith(base + "/"))


def _hold_anchor(
    anchored: str, target: RequestTarget, cwd: str = ""
) -> tuple[RequestTarget, str]:
    """Prompt text never moves or blocks an anchored session (ruling 2026-10-14).

    A project named in the prompt is context routing: the anchor stays and the
    worker is told how to switch explicitly. Only the worker's physical
    location -- a cwd inside a different governed repository -- still goes to
    _anchor_session, which moves a placeholder and otherwise refuses; that
    refusal is advisory too (_location_advisory). Project authority itself is
    enforced where operations happen (edit, Bash, git and restart guards), not
    here.
    """
    # cwd_repo maps a linked worktree to its shared checkout; a session
    # anchored to the worktree itself is already where its cwd is.
    in_anchor = bool(cwd) and _within(cwd, anchored)
    if target.cwd_repo and target.cwd_repo != anchored and not in_anchor:
        return replace(target, repo_root=target.cwd_repo), ""
    held = replace(target, repo_root=anchored)
    others = [root for root in target.mentioned if root != anchored]
    if not others:
        return held, ""
    named = ", ".join(others)
    return held, (
        f"PROJECT MENTION (advisory, not a switch): the prompt names {named}. This "
        f"session stays anchored to {anchored}; prompt text never switches projects. "
        f"To work in {named}, start an ATS session there explicitly "
        f"(start_session with repo_root={others[0]} and its scope). Edits and Bash "
        "writes there need a lock held by that session. Agent commits and "
        "non-fast-forward merges there (where the ATS git hooks are installed; "
        "fast-forward merges and --no-verify are not checked) need the committing "
        "session's own live locks on those files and are refused on other sessions' "
        "conflicting locks."
    )


def _location_advisory(anchored: str, cwd_repo: str, refusal: str) -> str:
    """A cwd in another governed repository is briefed, never blocked (2026-10-22).

    Refusing the prompt protected nothing the operation guards do not already
    enforce, and it stranded operators in sessions they had not chosen to end.
    """
    return (
        f"LOCATION (advisory, not a switch): the cwd is in {cwd_repo} but this session "
        f"stays anchored to {anchored or '(no repository)'} ({refusal}). Context below is "
        f"for {cwd_repo}. To work there, start an ATS session there explicitly "
        f"(start_session with repo_root={cwd_repo} and its scope); edits, Bash writes "
        "and commits there need locks held by that session."
    )


async def _get_session(client: Any, server_url: str, session_id: str) -> dict[str, Any]:
    response = await client.get(f"{server_url}/api/sessions/{session_id}")
    if response.status_code != 200:
        raise ContextResolutionError(
            f"ATS session {session_id or '(missing)'} unavailable: "
            f"HTTP {response.status_code} {_response_error(response)}"
        )
    body = response.json()
    if body.get("status") == "completed" and body.get("auto_completed") is True:
        # A live prompt is itself proof that the inactivity reaper guessed
        # wrong. ATS already has a guarded resurrection path that restores the
        # same session and any still-available locks. Use it instead of either
        # blocking the operator's prompt or minting a duplicate identity.
        heartbeat = await client.post(f"{server_url}/api/sessions/{session_id}/heartbeat")
        if heartbeat.status_code == 200 and heartbeat.json().get("status") == "active":
            return heartbeat.json()
        raise ContextResolutionError(
            f"ATS could not resurrect auto-reaped session {session_id}: "
            f"HTTP {heartbeat.status_code} {_response_error(heartbeat)}"
        )
    if body.get("status") != "active":
        raise ContextResolutionError(
            f"ATS session {session_id} is {body.get('status')}, not active"
        )
    return body


async def _anchor_session(
    client: Any, server_url: str, session: dict[str, Any], repo_root: str, token: str
) -> dict[str, Any]:
    current = str(session.get("repo_root") or "").rstrip("/")
    requested = repo_root.rstrip("/")
    if current == requested:
        return session
    if current and not _is_context_only_placeholder(session):
        raise AnchorRefused("the session holds work there")
    response = await client.patch(
        f"{server_url}/api/sessions/{session['id']}",
        json={"repo_root": requested, "description": _context_description(requested)},
        headers={"X-ATS-Approval-Token": token} if token else {},
    )
    if 400 <= response.status_code < 500:
        # e.g. 409: the session holds locks under its current anchor.
        raise AnchorRefused(f"ATS kept it: {_one_line(_response_error(response), 160)}")
    if response.status_code != 200:
        raise ContextResolutionError(
            f"ATS refused project attachment for session {session['id']}: "
            f"HTTP {response.status_code} {_response_error(response)}"
        )
    anchored = response.json()
    if str(anchored.get("repo_root") or "").rstrip("/") != requested:
        raise ContextResolutionError(f"ATS did not persist project attachment {requested}")
    return anchored


async def _brief(
    client: Any, server_url: str, session_id: str, token: str, prompt: str, target: RequestTarget
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "objective": prompt,
        "repo_root": target.repo_root,
        "scope": [],
        # The chained Echo prompt hook is the supplemental-memory stage. Exact
        # task context may still be fetched through ATS because ATS owns
        # the authority packet and validates its identity.
        "recall": False,
        "limit": 6,
        "session_id": session_id,
        # Identity is never guessed for an unnumbered request; it gets advisory
        # overlap candidates instead (#3591).
        "resolve_task": target.task_id is not None,
        "task_ids": list(target.task_ids),
        "overlaps": _wants_overlaps(prompt, target),
    }
    if target.task_id is not None:
        body["task_id"] = target.task_id
    response = await client.post(
        f"{server_url}/api/brief",
        json=body,
        headers={"X-ATS-Approval-Token": token} if token else {},
        timeout=25,
    )
    if response.status_code != 200:
        raise ContextResolutionError(
            f"ATS brief refused: HTTP {response.status_code} " f"{_response_error(response)}"
        )
    packet = response.json()
    if not str(packet.get("rendered") or "").strip():
        raise ContextResolutionError("ATS returned an empty context brief")
    # Naming a ticket in a prompt is a read, not a claim (D3): the brief never
    # binds the session, and an unbound session still receives the context.
    return packet


async def _delegated_child_context(
    client: Any, server_url: str, delegation_id: str
) -> str:
    """Context for a delegated child, whose packet already carries it.

    The parent rendered the authoritative task envelope into the child's
    packet. Re-resolving here tried to anchor/bind a session the child may not
    mutate (VERIFY/READ_ONLY) or that the packet's text pointed at another
    governed project, and the blocked prompt left the child with no task.
    Delegation and session ids are public, so the proof is the child's own
    approval token, which only the launching parent saw; ATS also checks the
    delegation is open, its lease unexpired and the child session active.
    """
    child = (os.environ.get("ATS_SESSION_ID") or "").strip()
    # The token comes from the supervisor's private 0600 capability file, the
    # same proof start_session uses -- never from env, which Codex forwards as
    # world-readable -c arguments.
    capability = sp.load_delegated_capability()
    token = capability[1] if capability and capability[0] == child else ""
    if not child or not token:
        raise ContextResolutionError(
            f"ATS_DELEGATION={delegation_id} is set without this child's private "
            "capability token; refusing to skip ATS-first resolution on ids alone"
        )
    response = await client.post(
        f"{server_url}/api/delegations/{delegation_id}/verify-child",
        json={"child_session_id": child},
        headers={"X-ATS-Approval-Token": token},
    )
    if response.status_code != 200:
        raise ContextResolutionError(
            f"ATS did not confirm this process as the live child of delegation "
            f"{delegation_id}: HTTP {response.status_code} {_response_error(response)}"
        )
    body = response.json()
    return (
        "ATS-FIRST CONTEXT RESOLUTION (delegated child)\n"
        f"session: {child}\n"
        f"delegation: {delegation_id} ({body.get('mode') or '?'}), confirmed live by ATS\n"
        "Your authoritative task context, acceptance criteria and prohibitions "
        "are in the delegation packet below; ATS does not re-resolve them."
    )


def _wants_overlaps(prompt: str, target: RequestTarget) -> bool:
    if target.reason == "subagent_handback":
        return False
    if target.reason in ("governed_cwd", "context_cwd"):
        return bool(_content_words(prompt))
    return True


async def resolve_prompt_context(
    server_url: str,
    client: Any,
    payload: dict[str, Any],
    *,
    agent: str | None = None,
) -> str | None:
    """Return injected ATS context, None for a genuinely generic prompt."""
    prompt = str(payload.get("prompt") or "").strip()
    cwd = str(payload.get("cwd") or os.getcwd())
    roots = governed_roots()
    target = resolve_request_target(prompt, cwd=cwd, governed_roots=roots)
    if target is None:
        return None

    delegation_id = (os.environ.get("ATS_DELEGATION") or "").strip()
    if delegation_id:
        return await _delegated_child_context(client, server_url, delegation_id)

    cid = str(
        payload.get("session_id")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("ATS_SESSION")
        or ""
    )
    base_agent = agent or ("claude-code" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "codex")
    cid_key = lifecycle_session_key(base_agent, cid)
    session_id = _session_id(cid_key)
    # SessionStart may run before Codex MCP startup and before local ATS is
    # reachable. A governed turn is the mandatory retry boundary: register via
    # local REST now, before asking ATS for authoritative context. Generic turns
    # return above and remain deliberately unscoped.
    explicit_session = bool((os.environ.get("ATS_SESSION_ID") or "").strip())
    if cid and not explicit_session:
        registered = await ensure_registered_session(
            server_url,
            client,
            RegistrationInput(
                lifecycle_session_id=cid,
                agent=base_agent,
                cwd=cwd,
                hook_event_name=str(payload.get("hook_event_name") or "UserPromptSubmit"),
                model=str(payload.get("model") or ""),
                source=str(payload.get("source") or ""),
            ),
        )
        if registered:
            session_id = registered
    if not session_id:
        raise ContextResolutionError(
            "no ATS session identity; SessionStart auto-registration did not complete"
        )
    token = _token(session_id, cid_key)
    session = await _get_session(client, server_url, session_id)

    anchored = str(session.get("repo_root") or "").rstrip("/")
    advisory = ""
    if target.reason == "subagent_handback":
        # A subagent's report names whatever it touched. It never anchors a
        # session (#3591); an already anchored session keeps its context.
        if not anchored:
            return None
        target = RequestTarget(None, anchored, "subagent_handback")
    elif anchored:
        target, advisory = _hold_anchor(anchored, target, cwd)

    # Context-only repositories are briefed, never anchored: anchoring is how a
    # session acquires a governed project, and these are not governed (#3591).
    context_only = bool(target.repo_root) and target.repo_root in set(context_roots())
    held_elsewhere = False
    if target.repo_root and not context_only:
        try:
            session = await _anchor_session(client, server_url, session, target.repo_root, token)
        except AnchorRefused as exc:
            held_elsewhere = True
            advisory = _location_advisory(anchored, target.repo_root, str(exc))

    packet = await _brief(client, server_url, session_id, token, prompt, target)

    header = (
        "ATS-FIRST CONTEXT RESOLUTION (automatic; obtained before worker answer)\n"
        f"session: {session_id}\n"
        f"trigger: {target.reason}\n"
        f"resolved repo: {target.repo_root or '(task context only)'}"
        + (" (context only: not governed, session not anchored)" if context_only else "")
        + (" (cwd context only: session not moved)" if held_elsewhere else "")
        + "\n"
        + (f"{advisory}\n" if advisory else "")
        + "Source precedence: ATS owns coordination/scope/authority/task context; "
        "Echo is supplemental; Git/DB/live services verify mutable facts.\n\n"
    )
    digest = "\n".join(_priority_digest(packet, target.repo_root, prompt=prompt)) + "\n\n"
    rendered = _within_budget(_short_echo(str(packet["rendered"]).strip()),
                              _BRIEF_BUDGET - len(header) - len(digest))
    return header + digest + rendered


# Claude Code shows a hook's whole output inline only up to ~10 KB; past that
# it shows the first 2 KB and saves the rest to a file the agent may never
# open. Codex truncates by tokens near the same size. The ATS note therefore
# stays under this budget (leaving room for the supplement), and its first
# lines carry what controls the work. The digest has its own cap so the full
# brief always keeps some room after it.
_BRIEF_BUDGET = 7000
_DIGEST_BUDGET = 4500
_RULINGS_SHOWN = 6
_NAMED_FULL = 12
_BRANCHES_SHOWN = 3
_DIGEST_ITEMS = 5

# The only retrieval paths the note may advertise. Both take ONE ticket:
# task_brief has no ticket list and no overlap flag, so it cannot reproduce a
# multi-ticket brief (Codex review e99660fb), but for one task_id it returns
# that ticket's current operator rulings first. get_tower_task returns the
# ticket record (description, status, gate), not the structured rulings.
_GET_TICKET = "get_tower_task(task_id=N)"
_GET_RULINGS = "task_brief(objective=..., task_id=N)"


def _one_line(text: Any, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def _owner(item: dict) -> str:
    owner = item.get("live_owner") or {}
    return f" | live owner: {owner['agent']}" if owner.get("agent") else ""


def _rulings_of(packet: dict) -> list[dict]:
    """Every current operator ruling the packet carries, once, prohibitions
    first. Only the structured operator_rulings fields (the reviewed store);
    never recommendation or notes prose."""
    found: dict[tuple, dict] = {}

    def add(tid: Any, r: dict) -> None:
        text = str(r.get("ruling") or "")
        if text:
            # Keyed by ticket id as text: the branch census keys tickets by
            # JSON object keys (strings), the task context by int.
            found.setdefault((str(tid), str(r.get("id") or text)), {
                "ticket": str(tid), "id": str(r.get("id") or ""), "text": text,
                "prohibition": bool(r.get("prohibition"))})

    for r in ((packet.get("task_context") or {}).get("operator_rulings") or {}).get("current") or []:
        add(packet.get("task_id"), r)
    for card in packet.get("named_tasks") or []:
        for r in card.get("operator_rulings") or []:
            add(card.get("id"), r)
    for b in packet.get("active_branches") or []:
        for tid, rs in (b.get("ticket_rulings") or {}).items():
            for r in rs:
                add(tid, r)
    for c in packet.get("overlaps") or []:
        for r in c.get("operator_rulings") or []:
            add(c.get("id"), r)
    return sorted(found.values(), key=lambda r: not r["prohibition"])


def _unchecked_ruling_tickets(packet: dict) -> list[int]:
    """Tickets whose rulings the brief never looked up, so "no ruling shown" is
    not "no ruling": named tickets it could not fetch, and branch tickets past
    the server's lookup cap (same selection as briefs._existing_work)."""
    from ai_team_sync.briefs import _BRANCH_TICKET_RULINGS_CAP

    named = packet.get("named_tasks") or []
    known = ({int(c["id"]) for c in packet.get("overlaps") or []}
             | {int(c["id"]) for c in named if not c.get("error")})
    wanted = [int(t) for b in packet.get("active_branches") or []
              for t in b.get("tickets") or [] if int(t) not in known]
    past_cap = list(dict.fromkeys(wanted))[_BRANCH_TICKET_RULINGS_CAP:]
    return list(dict.fromkeys([int(c["id"]) for c in named if c.get("error")] + past_cap))


def _rulings_call(ticket: Any) -> str:
    return _GET_RULINGS.replace("task_id=N", f"task_id={ticket}")


# Same shape the branch census reads as a ticket mention (active_work).
_TICKET_LIKE = re.compile(r"(?<![\w/#])#(\d{3,5})\b")


def _possible_references(prompt: str, packet: dict) -> list[str]:
    """Ticket-like "#3528" mentions the request did not name as tickets.

    A bare number counts as a ticket only when the text says so (briefs.
    explicit_task_ids), so "...to finish #3528 (related: #2945)?" resolves to
    no ticket and none of its rulings are fetched. These are listed, never
    looked up and never anchored: the agent retrieves them by id."""
    resolved = {str(packet.get("task_id"))} | {str(c.get("id")) for c in packet.get("named_tasks") or []}
    return [t for t in dict.fromkeys(_TICKET_LIKE.findall(prompt or "")) if t not in resolved]


def _priority_digest(packet: dict, repo_root: str = "", *, prompt: str = "") -> list[str]:
    """What controls this work, from the packet's structured fields, inside
    _DIGEST_BUDGET: binding rulings, every named ticket with gate and owner,
    unfinished branches, overlap candidates -- and every item not shown in full,
    by identifier, with a retrieval path that works. When even the identifiers
    do not fit, it says CONTEXT INCOMPLETE and how to reach the rest."""
    from ai_team_sync.active_work import MAX_BRANCHES_REPORTED, default_branch

    named = packet.get("named_tasks") or []
    overlaps = packet.get("overlaps") or []
    branches = packet.get("active_branches") or []
    envelope = packet.get("task_envelope") or {}
    rulings = _rulings_of(packet)
    unchecked = _unchecked_ruling_tickets(packet)
    named_ids = {str(c.get("id")) for c in named}
    repo = repo_root or "<repo>"

    # (priority, seq, section, line, item). When the budget binds, the highest
    # priority number goes first: unshown rulings outrank named-ticket lines
    # because the request itself carries its ticket numbers.
    entries: list[tuple] = []

    def add(priority: int, section: str, line: str, item: tuple = ("other", None)) -> None:
        entries.append((priority, len(entries), section, line, item))

    for r in rulings[:_RULINGS_SHOWN]:
        kind = "PROHIBITION" if r["prohibition"] else "RULING"
        rid = f" (ruling id {r['id'][:8]})" if r["id"] else ""
        add(1, "rulings", f"    [{kind} #{r['ticket']}] {_one_line(r['text'], 220)}{rid}",
            ("ruling", r["ticket"]))
    if unchecked:
        add(2, "unchecked", "  rulings NOT CHECKED for " + ", ".join(f"#{t}" for t in unchecked)
            + f" (this brief did not look them up) — check each with {_GET_RULINGS}",
            ("ruling", None))
    for ref in _possible_references(prompt, packet):
        add(2, "references", f"    #{ref} → {_rulings_call(ref)} | "
            f"{_GET_TICKET.replace('task_id=N', 'task_id=' + ref)}", ("reference", ref))
    for r in rulings[_RULINGS_SHOWN:]:
        kind = "prohibition" if r["prohibition"] else "ruling"
        ident = r["id"][:8] if r["id"] else f'"{_one_line(r["text"], 60)}"'
        add(3, "omitted", f"    {kind} {ident} on #{r['ticket']} → {_rulings_call(r['ticket'])}",
            ("ruling", r["ticket"]))

    for i, card in enumerate(named):
        if card.get("error"):
            line = f"    #{card['id']} UNAVAILABLE: {_one_line(card['error'], 120)}"
        else:
            title = f" {_one_line(card.get('title'), 90)}" if i < _NAMED_FULL else ""
            line = (f"    #{card['id']} [{card.get('status')}, gate={card.get('gate')}]"
                    f"{title}{_owner(card)}")
        add(4, "named", line, ("named", card.get("id")))
    if not named and envelope.get("id"):
        add(4, "ticket", f"  ticket: #{envelope['id']} [{envelope.get('status')}, "
            f"gate={envelope.get('gate')}] {_one_line(envelope.get('title'), 90)}",
            ("named", envelope["id"]))

    branch_status = str(packet.get("active_branches_status") or "")
    if branch_status and branch_status != "not requested" and not branch_status.startswith("ok"):
        add(5, "branch_status", f"  unfinished-branch census UNAVAILABLE ({_one_line(branch_status, 140)})"
            " — this is NOT 'no unfinished branches'")
    status = str(packet.get("overlaps_status") or "")
    if status and status != "not requested" and not status.startswith("ok"):
        add(5, "overlap_status", f"  existing-work lookup UNAVAILABLE ({_one_line(status, 140)}) — "
            "this is NOT 'no related work'")

    tickets = {packet.get("task_id"), *(c.get("id") for c in named)} - {None}
    relevant = [b for b in branches if b.get("files_in_scope") or set(b.get("tickets") or []) & tickets]
    others = [b for b in branches if b not in relevant]
    for n, b in enumerate(relevant + others):
        state = "CONFLICTS with default" if b.get("conflict") == "conflicts_with_default" else "merges cleanly"
        on = ", ".join(f"#{t}" for t in b.get("tickets") or []) or "no ticket"
        if n < _BRANCHES_SHOWN and b in relevant:
            add(7, "branches", f"    {b['branch']} ({on}) {b.get('unique_commits')} commit(s), {state}"
                + (f"; in scope: {', '.join(b['files_in_scope'][:3])}" if b.get("files_in_scope") else "")
                + _owner(b), ("branch", b["branch"]))
        else:
            add(9, "omitted", f"    unfinished branch {b['branch']} ({on}, {state}){_owner(b)} → "
                f"git -C {repo} log --oneline -10 {b['branch']}", ("branch", b["branch"]))
    if len(branches) >= MAX_BRANCHES_REPORTED and repo_root:
        add(9, "omitted", f"    the branch census lists at most {MAX_BRANCHES_REPORTED}; every unmerged "
            f"branch → git -C {repo} for-each-ref --no-merged={default_branch(repo_root) or 'main'} "
            "refs/heads", ("branch", None))

    for n, c in enumerate(overlaps):
        closed = " closed" if c.get("closed") else ""
        if n < _DIGEST_ITEMS:
            add(8, "overlaps", f"    #{c['id']} [{c.get('status')}{closed}] "
                f"{_one_line(c.get('title'), 80)}{_owner(c)}", ("overlap", c["id"]))
        else:
            add(6, "omitted", f"    advisory overlap #{c['id']} [{c.get('status')}{closed}]{_owner(c)} → "
                f"{_GET_TICKET.replace('task_id=N', 'task_id=' + str(c['id']))}", ("overlap", c["id"]))

    omitted_total = sum(1 for e in entries if e[2] == "omitted")
    headers = {
        "rulings": f"  operator rulings and prohibitions (binding where they apply; {len(rulings)} current):",
        "references": "  POSSIBLE TICKET REFERENCES — mentioned in your request but NOT resolved as tickets, "
                      "so their rulings were NOT looked up (write \"ticket #N\" to resolve one):",
        "named": f"  named tickets (all {len(named)}):",
        "branches": f"  unfinished branches on this work (informational, never blocking; "
                    f"{len(relevant)} relevant):",
        "overlaps": f"  existing work that may overlap (advisory; similarity is not proof; "
                    f"{len(overlaps)} returned):",
        "omitted": f"  NOT SHOWN IN FULL ABOVE — {omitted_total} item(s); each identified, "
                   "with how to retrieve it:",
    }
    order = ["rulings", "unchecked", "references", "named", "ticket", "branch_status", "branches",
             "overlap_status", "overlaps", "omitted"]
    retrieve = ["  retrieve (verified tools; each takes ONE ticket):",
                f"    a ticket's record: {_GET_TICKET}  [Echo Brain MCP]",
                f"    a ticket's current operator rulings: {_GET_RULINGS}  "
                "[ATS MCP; takes ONE task_id, never a ticket list]"]
    if branches:
        retrieve.append(f"    a branch: git -C {repo} log --oneline -10 <branch>")

    def incomplete(dropped: list[tuple]) -> list[str]:
        if not dropped:
            return []
        dropped = sorted(dropped, key=lambda e: e[1])
        kinds: dict[str, int] = {}
        for e in dropped:
            kinds[e[4][0]] = kinds.get(e[4][0], 0) + 1
        out = [f"  CONTEXT INCOMPLETE — {len(dropped)} item(s) did not fit inline ("
               + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
               + "). This note does NOT establish that every controlling decision is shown."]
        ruled = [e[4][1] for e in dropped if e[4][0] == "ruling"]
        if ruled:
            others_ = list(dict.fromkeys(t for t in ruled if t and t not in named_ids))
            out.append("    rulings not listed are on "
                       + (", ".join(f"#{t}" for t in others_) + " and " if others_ else "")
                       + f"tickets in your request → for each, and every ticket number in your "
                       f"request: {_GET_RULINGS}")
        if any(e[4][0] == "reference" for e in dropped):
            out.append("    possible ticket references not listed: every #N in your request → "
                       f"{_GET_RULINGS}")
        if any(e[4][0] == "named" for e in dropped):
            out.append(f"    named tickets not listed: every ticket number in your request → {_GET_TICKET}")
        lost = [str(e[4][1]) for e in dropped if e[4][0] == "overlap"]
        if lost:
            out.append("    overlap candidates not listed: " + ", ".join(f"#{t}" for t in lost)
                       + f" → {_GET_TICKET}")
        if any(e[4][0] == "branch" for e in dropped):
            out.append(f"    unfinished branches not listed → git -C {repo} for-each-ref "
                       f"--no-merged={(default_branch(repo_root) if repo_root else '') or 'main'} refs/heads")
        return out

    def render(kept: list[tuple], dropped: list[tuple]) -> list[str]:
        lines = ["PRIORITY CONTEXT — controlling records first; details follow below"]
        for section in order:
            rows = [e[3] for e in kept if e[2] == section]
            if rows:
                lines += ([headers[section]] if section in headers else []) + rows
        return lines + incomplete(dropped) + retrieve

    kept = sorted(entries, key=lambda e: e[1])
    dropped: list[tuple] = []
    while kept and len("\n".join(render(kept, dropped))) > _DIGEST_BUDGET:
        worst = max(kept, key=lambda e: (e[0], e[1]))
        kept.remove(worst)
        dropped.append(worst)
    return render(kept, dropped)


def _short_echo(rendered: str) -> str:
    """The brief's first line repeats the whole request; the agent already has it."""
    first, sep, rest = rendered.partition("\n")
    if first.startswith("TASK BRIEF") and len(first) > 200:
        first = first[:199] + "…"
    return first + sep + rest


def _within_budget(rendered: str, room: int) -> str:
    if len(rendered) <= room:
        return rendered
    note_room = 260
    cut = rendered.rfind("\n", 0, max(0, room - note_room))
    shown = rendered[:cut if cut > 0 else max(0, room - note_room)]
    return (shown + f"\n[brief truncated here: {len(rendered) - len(shown)} more characters "
            "not shown. PRIORITY CONTEXT above identifies every controlling item, or says "
            f"CONTEXT INCOMPLETE. Per ticket: {_GET_TICKET} for the record, {_GET_RULINGS} "
            "for its current rulings.]")


def _run_supplement(command: str, timeout: int, raw_payload: str) -> None:
    if not command:
        return
    try:
        result = subprocess.run(
            shlex.split(command),
            input=raw_payload,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ATS supplemental context unavailable ({type(exc).__name__})", file=sys.stderr)
        return
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.returncode and result.stderr:
        print(result.stderr, file=sys.stderr, end=("" if result.stderr.endswith("\n") else "\n"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=("codex", "claude-code"), default=None)
    parser.add_argument("--supplement-command", default="")
    parser.add_argument("--supplement-timeout", type=int, default=90)
    args = parser.parse_args(argv)
    raw_payload = sys.stdin.read()
    try:
        payload = json.loads(raw_payload)
    except Exception:
        payload = {}
    prompt = str(payload.get("prompt") or "")
    cwd = str(payload.get("cwd") or os.getcwd())
    # Decide before opening a socket. Generic conversation must remain generic.
    # A bad operator config must block (exit 2): exit 1 lets the prompt through.
    try:
        target = resolve_request_target(prompt, cwd=cwd)
    except OperatorConfigError as exc:
        print(f"ATS-FIRST blocked: operator config is invalid: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if target is None:
        _run_supplement(args.supplement_command, args.supplement_timeout, raw_payload)
        raise SystemExit(0)

    server = os.environ.get("ATS_SERVER_URL", "http://localhost:8400")

    async def _run() -> str | None:
        import httpx

        async with httpx.AsyncClient(timeout=25) as client:
            return await resolve_prompt_context(server, client, payload, agent=args.agent)

    try:
        note = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        # A delegated child's claim is verified or blocked, never best-effort:
        # packets routinely name several tickets, and an unconfirmed delegation
        # must not slip through as "added context" (#3536 review round 1).
        if target.best_effort and not (os.environ.get("ATS_DELEGATION") or "").strip():
            # Context added by #3591 never blocks a prompt the pre-#3591 hook
            # let through; it reports what is missing instead.
            print(f"ATS context unavailable for this request ({type(exc).__name__}: "
                  f"{exc}); continuing without it.", file=sys.stderr)
            _run_supplement(args.supplement_command, args.supplement_timeout, raw_payload)
            raise SystemExit(0)
        print(
            "ATS-FIRST context resolution failed for governed work: "
            f"{exc}. The prompt was blocked before the worker could substitute "
            "Echo/Git/DB for ATS coordination context.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if note:
        print(note)
    # The supplement starts only after ATS succeeded. Keeping it in this same
    # process avoids Claude's parallel hook execution reordering the sources.
    _run_supplement(args.supplement_command, args.supplement_timeout, raw_payload)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
