"""#3774: ask the operator one question through Echo's Telegram, get the answer back here.

ask_operator (MCP) files the question with Echo for the CALLER'S OWN ATS session (the
process's own session, never a caller-supplied id), and records the question id in a
private per-session list under the ATS state directory. The UserPromptSubmit inbox hook
reads that list: only while it is non-empty does it ask Echo for answered questions, so a
session that never asked pays nothing. Each answer is printed into the session's next turn,
then marked delivered with Echo and dropped from the list.

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


def _dir() -> Path:
    return Path(os.environ.get("ATS_STATE_DIR") or Path.home()) / ".ats_operator_questions"


def _path(session_id: str) -> Path:
    if not _SID.match(session_id or ""):
        raise ValueError("bad session id")
    return _dir() / f"{session_id}.json"


def pending_ids(session_id: str) -> list[str]:
    try:
        data = json.loads(_path(session_id).read_text())
    except (OSError, ValueError):
        return []
    return [q for q in data if isinstance(q, str) and _QID.match(q)] if isinstance(data, list) else []


def _write(session_id: str, ids: list[str]) -> None:
    d = _dir()
    d.mkdir(mode=0o700, exist_ok=True)
    p = _path(session_id)
    if not ids:
        p.unlink(missing_ok=True)
        return
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(set(ids))))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def remember(session_id: str, question_id: str) -> None:
    _write(session_id, pending_ids(session_id) + [question_id])


def forget(session_id: str, question_id: str) -> None:
    _write(session_id, [q for q in pending_ids(session_id) if q != question_id])


ASK_FIELDS = ("tower_task_id", "question_key", "question", "options", "context", "evidence",
              "moments_blocked", "info_only", "test")


def ask_payload(session_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    body = {k: arguments[k] for k in ASK_FIELDS if k in arguments}
    body["ats_session_id"] = session_id
    return body


def format_ask_result(status_code: int, data: Any) -> str:
    if status_code == 200 and isinstance(data, dict) and data.get("ok"):
        sent = data.get("sent") or {}
        if data["delivery"] == "now":
            where = ("sent to the operator's Telegram now" if sent.get("ok")
                     else f"NOT sent yet ({sent.get('error')}: {sent.get('why')}); it stays queued")
        else:
            where = "queued for the operator's morning digest (blocks fewer than 10 moments)"
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


def surface_answers(session_id: str, client) -> str | None:
    """For the inbox hook (sync httpx client). Prints nothing and calls nothing when this
    session has no open question. An answer is marked delivered only after it was
    formatted for printing; a failure leaves it for the next prompt."""
    ids = pending_ids(session_id)
    if not ids:
        return None
    r = client.get(f"{ECHO_URL}/api/operator-questions/answers", params={"ats_session_id": session_id})
    if r.status_code != 200:
        return None
    answers = [a for a in (r.json() or {}).get("answers") or [] if a.get("question_id") in ids]
    note = format_answers(answers)
    for a in answers:
        d = client.post(f"{ECHO_URL}/api/operator-questions/{a['question_id']}/delivered",
                        json={"ats_session_id": session_id})
        if d.status_code == 200:
            forget(session_id, a["question_id"])
    return note
