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
import shlex
import subprocess
import sys
from typing import Any

from ai_team_sync import session_pointer as sp
from ai_team_sync.context_resolution import (
    RequestTarget,
    governed_roots,
    resolve_request_target,
)
from ai_team_sync.hooks.session_registration import RegistrationInput, lifecycle_session_key
from ai_team_sync.hooks.session_registration import ensure_session as ensure_registered_session
from ai_team_sync.session_marker import AUTOREG_DESCRIPTION


class ContextResolutionError(RuntimeError):
    """A governed prompt could not obtain its mandatory ATS context."""


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
    return sp.load_approval_token(session_id, cid=cid) or ""


def _context_description(repo_root: str) -> str:
    # Keep the marker prefix so a later start_session still recognizes and
    # adopts this identity/context placeholder instead of leaving an orphan.
    return (
        f"{AUTOREG_DESCRIPTION}; context resolved to {repo_root}; " "file scope remains unclaimed"
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
    if current:
        raise ContextResolutionError(
            f"ATS session {session['id']} is already anchored to {current}; "
            f"start a new session before switching governed project to {requested}"
        )
    response = await client.patch(
        f"{server_url}/api/sessions/{session['id']}",
        json={"repo_root": requested, "description": _context_description(requested)},
        headers={"X-ATS-Approval-Token": token} if token else {},
    )
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
        "resolve_task": target.task_id is not None,
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
    if target.task_id is not None:
        linkage = (packet.get("session_linkage") or {}).get("status")
        if linkage not in {"bound", "already_bound"}:
            raise ContextResolutionError(
                f"ATS resolved task #{target.task_id} but did not attach it to "
                f"session {session_id} (linkage={linkage or 'missing'})"
            )
    return packet


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

    if target.repo_root:
        session = await _anchor_session(client, server_url, session, target.repo_root, token)

    packet = await _brief(client, server_url, session_id, token, prompt, target)

    rendered = str(packet["rendered"]).strip()
    return (
        "ATS-FIRST CONTEXT RESOLUTION (automatic; obtained before worker answer)\n"
        f"session: {session_id}\n"
        f"trigger: {target.reason}\n"
        f"resolved repo: {target.repo_root or '(task context only)'}\n"
        "Source precedence: ATS owns coordination/scope/authority/task context; "
        "Echo is supplemental; Git/DB/live services verify mutable facts.\n\n" + rendered
    )


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
    if resolve_request_target(prompt, cwd=cwd) is None:
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
