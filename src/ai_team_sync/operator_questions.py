"""#3774: ask the operator one question through Echo's Telegram, get the answer back here.

ask_operator (MCP) files the question with Echo for the CALLER'S OWN ATS session (the
process's own session, never a caller-supplied id). Echo answers with a per-question
delivery secret, kept with the question id in a private 0600 file under the ATS state dir.
The UserPromptSubmit inbox hook, only when it holds this session's capability and the file
lists open questions, reads each answer with that question's own secret, prints it into the
session's next turn, and only then marks it delivered with Echo and drops it from the file.

The answer is also a current operator ruling on the ticket, so task_brief shows it to
every session working that ticket; this hook only makes sure the asker hears it at once.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ECHO_URL = os.environ.get("ECHO_URL", "http://127.0.0.1:8309")
_SID = re.compile(r"^[0-9a-zA-Z-]{8,64}$")
_QID = re.compile(r"^[0-9a-f-]{36}$")
_TOKEN = re.compile(r"^[0-9a-f]{48,128}$")
TOKEN_HEADER = "X-Operator-Question-Token"


def _dir() -> Path:
    return Path(os.environ.get("ATS_STATE_DIR") or Path.home()) / ".ats_operator_questions"


def _path(session_id: str) -> Path:
    if not _SID.match(session_id or ""):
        raise ValueError("bad session id")
    return _dir() / f"{session_id}.json"


class QuestionFileCorrupt(ValueError):
    """The per-session file exists but cannot be parsed: never overwritten (its tokens are the
    only way back to those answers), never read as empty."""


def _parse(raw: str) -> dict[str, str]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise QuestionFileCorrupt("not an object")
    out = {}
    for q, t in data.items():
        if not (isinstance(q, str) and _QID.match(q) and isinstance(t, str) and _TOKEN.match(t)):
            raise QuestionFileCorrupt(f"bad entry {q!r}")
        out[q] = t
    return out


def _load(session_id: str) -> dict[str, str]:
    """{question_id: delivery_token}. Missing file -> {}; unreadable/corrupt -> raises."""
    try:
        raw = _path(session_id).read_text()
    except FileNotFoundError:
        return {}
    try:
        return _parse(raw)
    except ValueError as exc:
        raise QuestionFileCorrupt(str(exc)) from exc


def pending_ids(session_id: str) -> list[str]:
    return sorted(_load(session_id))


@contextmanager
def _locked(session_id: str):
    """One writer per session file: remember/forget are read-modify-write."""
    d = _dir()
    d.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(d / f"{_path(session_id).stem}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write(session_id: str, entries: dict[str, str]) -> None:
    p = _path(session_id)
    if not entries:
        p.unlink(missing_ok=True)
        return
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def remember(session_id: str, question_id: str, token: str) -> None:
    if not _QID.match(question_id or "") or not _TOKEN.match(token or ""):
        raise ValueError("bad question id or delivery token")
    with _locked(session_id):
        _write(session_id, {**_load(session_id), question_id: token})


def forget(session_id: str, question_id: str) -> None:
    with _locked(session_id):
        _write(session_id, {q: t for q, t in _load(session_id).items() if q != question_id})


def delivery_token(capability: str, session_id: str, tower_task_id: Any, question_key: Any) -> str:
    """Deterministic per-question secret: the same session asking the same question again (e.g.
    after a lost response) sends the same token, and Echo hands back the same question. Only a
    holder of this session's capability can compute it."""
    if not capability:
        raise ValueError("no session capability")
    msg = f"operator-question|{session_id}|{tower_task_id}|{question_key}".encode()
    return hmac.new(capability.encode(), msg, hashlib.sha256).hexdigest()


ASK_FIELDS = ("tower_task_id", "question_key", "question", "options", "context", "evidence",
              "moments_blocked", "info_only", "test")


def ask_payload(session_id: str, arguments: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    body = {k: arguments[k] for k in ASK_FIELDS if k in arguments}
    body["ats_session_id"] = session_id
    if token:
        body["delivery_token"] = token
    return body


def format_ask_result(status_code: int, data: Any, *, info_only: bool = False) -> str:
    if status_code == 200 and isinstance(data, dict) and data.get("ok"):
        sent = data.get("sent") or {}
        if data["delivery"] == "now":
            where = ("sent to the operator's Telegram now" if sent.get("ok")
                     else f"NOT sent yet ({sent.get('error')}: {sent.get('why')}); it stays queued")
        else:
            where = "queued for the operator's morning digest (blocks fewer than 10 moments)"
        if info_only:
            return (f"✅ Notice {data['question_id']} filed: {where}. It is information only: no "
                    "buttons, nothing is recorded, and no answer comes back.")
        return (f"✅ Question {data['question_id']} filed: {where}. The operator's tap is recorded "
                "as a ruling on the ticket and appears in this session's next turn.")
    detail = data.get("detail", data) if isinstance(data, dict) else data
    if isinstance(detail, dict) and detail.get("error") == "ALREADY_RULED":
        # Never the ruling's text, even if a response carried it: the ruling is read through
        # task_brief for the ticket, where its provenance is shown. Echo's `why` says so.
        return ("⛔ Not asked: the operator already ruled on this question "
                f"({detail.get('decided_at')}). {detail.get('why') or 'Read it with task_brief for this ticket.'}")
    if isinstance(detail, dict) and detail.get("error"):
        return f"❌ Not asked ({detail['error']}): {detail.get('why')}"
    return f"❌ Not asked (HTTP {status_code}): {str(detail)[:300]}"


def format_answers(answers: list[dict]) -> str | None:
    if not answers:
        return None
    lines = [f"OPERATOR RULING{'S' if len(answers) > 1 else ''} via Telegram "
             f"({len(answers)}), recorded on the ticket as current operator authority:"]
    for a in answers:
        lines.append(f"- #{a['tower_task_id']} {a['question_key']}: \"{a['question']}\" -> "
                     f"{a['answer']}" + (f" ({a['ruling']})" if a.get("ruling") else "")
                     + f" [answered {a['answered_at']}, decision {a['decision_id']}]")
    lines.append("Act on it; do not ask this question again (ask_operator refuses a re-ask).")
    return "\n".join(lines)


def collect_answers(session_id: str, client) -> list[dict]:
    """For the inbox hook (sync httpx client). Calls nothing when this session has no open
    question. Each question is read with its own delivery token, so a session can read only
    the questions it filed. Nothing is acknowledged here: the hook prints first, then calls
    acknowledge()."""
    out = []
    for qid, token in _load(session_id).items():
        r = client.get(f"{ECHO_URL}/api/operator-questions/{qid}/answer", headers={TOKEN_HEADER: token})
        if r.status_code != 200:
            continue
        a = r.json() or {}
        if a.get("answered") and a.get("question_id") == qid:
            out.append(a)
    return out


def acknowledge(session_id: str, answers: list[dict], client) -> list[str]:
    """After the note was printed and flushed: mark each answer delivered with Echo and drop it
    from the list. A failed mark keeps it, so the next prompt shows it again (at-least-once)."""
    tokens, done = _load(session_id), []
    for a in answers:
        qid = a["question_id"]
        if qid not in tokens:
            continue
        r = client.post(f"{ECHO_URL}/api/operator-questions/{qid}/delivered", headers={TOKEN_HEADER: tokens[qid]})
        if r.status_code == 200:
            forget(session_id, qid)
            done.append(qid)
    return done


def surface_for_prompt(session_id: str, capability: str, *, out=None, client=None) -> bool:
    """The one prompt-time path, shared by the Claude (override_inbox) and Codex (ats_context)
    hooks: only with this session's capability; read each answer with its own token; print and
    flush; only then acknowledge. Returns whether a note was printed. Raises on failure (the
    callers are fail-open hooks)."""
    if not capability or not session_id or not pending_ids(session_id):
        return False
    out = out or sys.stdout
    if client is None:
        import httpx
        with httpx.Client(timeout=2) as c:
            return surface_for_prompt(session_id, capability, out=out, client=c)
    answers = collect_answers(session_id, client)
    note = format_answers(answers)
    if not note:
        return False
    print(note, file=out)
    out.flush()
    acknowledge(session_id, answers, client)
    return True
