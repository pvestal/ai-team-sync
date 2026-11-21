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

import json
import os
import re
from pathlib import Path
from typing import Any

ECHO_URL = os.environ.get("ECHO_URL", "http://127.0.0.1:8309")
_SID = re.compile(r"^[0-9a-zA-Z-]{8,64}$")
_QID = re.compile(r"^[0-9a-f-]{36}$")
_TOKEN = re.compile(r"^[0-9a-f]{32,128}$")
TOKEN_HEADER = "X-Operator-Question-Token"


def _dir() -> Path:
    return Path(os.environ.get("ATS_STATE_DIR") or Path.home()) / ".ats_operator_questions"


def _path(session_id: str) -> Path:
    if not _SID.match(session_id or ""):
        raise ValueError("bad session id")
    return _dir() / f"{session_id}.json"


def _load(session_id: str) -> dict[str, str]:
    """{question_id: delivery_token}. The token is Echo's per-question secret, returned only
    to the asker at ask time; Echo requires it to read or mark that answer."""
    try:
        data = json.loads(_path(session_id).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {q: t for q, t in data.items()
            if isinstance(q, str) and _QID.match(q) and isinstance(t, str) and _TOKEN.match(t)}


def pending_ids(session_id: str) -> list[str]:
    return sorted(_load(session_id))


def _write(session_id: str, entries: dict[str, str]) -> None:
    d = _dir()
    d.mkdir(mode=0o700, exist_ok=True)
    p = _path(session_id)
    if not entries:
        p.unlink(missing_ok=True)
        return
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(entries, f)
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def remember(session_id: str, question_id: str, token: str) -> None:
    if not _QID.match(question_id or "") or not _TOKEN.match(token or ""):
        raise ValueError("bad question id or delivery token")
    _write(session_id, {**_load(session_id), question_id: token})


def forget(session_id: str, question_id: str) -> None:
    _write(session_id, {q: t for q, t in _load(session_id).items() if q != question_id})


ASK_FIELDS = ("tower_task_id", "question_key", "question", "options", "context", "evidence",
              "moments_blocked", "info_only", "test")


def ask_payload(session_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    body = {k: arguments[k] for k in ASK_FIELDS if k in arguments}
    body["ats_session_id"] = session_id
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
        return ("⛔ Not asked: the operator already ruled on this question "
                f"({detail.get('decided_at')}): {detail.get('ruling')}")
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
