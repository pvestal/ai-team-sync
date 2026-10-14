"""Structured VERIFY verdicts (#3585).

A review verdict used to be free text in ``Delegation.result_summary``; nothing
could tell a BLOCK from a PASS without reading prose. A VERIFY return now
carries one structured verdict, and the verdict that counts is DERIVED from its
items rather than taken from the reviewer's headline:

- any BLOCK item with material severity        -> BLOCK
- only nonmaterial BLOCK items                 -> PASS_WITH_CITATION
- no BLOCK items                               -> PASS or PASS_WITH_CITATION as reported
- the child produced no usable verdict         -> NO_RESULT (never a pass)

Severity follows operational impact, not where the defect sits. An item whose
``claim_category`` is security, authorization, data_integrity or
enforcement_coverage is material even when the defect is only in docs or
advisory text, so it may not be filed as nonmaterial (operator refinement,
2026-10-14). Only ``other`` may be nonmaterial.

Everything here is parsing and validation of structured fields; nothing
classifies natural language.
"""

from __future__ import annotations

import json
import re
from typing import Any

PASS = "PASS"
PASS_WITH_CITATION = "PASS_WITH_CITATION"
BLOCK = "BLOCK"
NO_RESULT = "NO_RESULT"
VERDICT_CODES = (PASS, PASS_WITH_CITATION, BLOCK, NO_RESULT)

MATERIAL_SAFETY = "material_safety"
MATERIAL_CORRECTNESS = "material_correctness"
NONMATERIAL = "nonmaterial"
SEVERITIES = (MATERIAL_SAFETY, MATERIAL_CORRECTNESS, NONMATERIAL)
MATERIAL = (MATERIAL_SAFETY, MATERIAL_CORRECTNESS)

CLAIM_CATEGORIES = ("security", "authorization", "data_integrity",
                    "enforcement_coverage", "behavior", "other")

ITEM_STATUSES = (PASS, BLOCK)

# The fenced block a VERIFY child ends its answer with. The supervisor takes the
# LAST such block, so quoting the contract earlier in an answer is harmless.
FENCE = "ats-verdict"
_BLOCK_RE = re.compile(r"```" + re.escape(FENCE) + r"[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


class VerdictError(ValueError):
    """A structured verdict that cannot be stored as submitted."""


def _text(value: Any, field: str, *, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise VerdictError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise VerdictError(f"{field} is required")
    return value


def _sha(value: Any, field: str, *, required: bool) -> str:
    value = _text(value, field, required=required).lower()
    if value and not _SHA_RE.match(value):
        raise VerdictError(f"{field} must be a hex commit id")
    return value


def _item(raw: Any, index: int) -> dict:
    if not isinstance(raw, dict):
        raise VerdictError(f"items[{index}] must be an object")
    item_id = _text(raw.get("item_id"), f"items[{index}].item_id")
    status = _text(raw.get("status"), f"items[{index}].status").upper()
    if status not in ITEM_STATUSES:
        raise VerdictError(f"items[{index}].status must be one of {ITEM_STATUSES}")
    severity = _text(raw.get("severity"), f"items[{index}].severity").lower()
    if severity not in SEVERITIES:
        raise VerdictError(f"items[{index}].severity must be one of {SEVERITIES}")
    category = _text(raw.get("claim_category"), f"items[{index}].claim_category").lower()
    if category not in CLAIM_CATEGORIES:
        raise VerdictError(f"items[{index}].claim_category must be one of {CLAIM_CATEGORIES}")
    if severity == NONMATERIAL and category != "other":
        raise VerdictError(
            f"items[{index}] ({item_id}): claim_category {category} is material by rule; "
            f"an inaccurate {category} claim cannot be filed as nonmaterial")
    return {"item_id": item_id, "status": status, "severity": severity,
            "claim_category": category,
            "citation": _text(raw.get("citation"), f"items[{index}].citation",
                              required=status == BLOCK)}


def derive(reported: str, items: list[dict]) -> str:
    """The verdict that counts, from the items."""
    if reported == NO_RESULT:
        return NO_RESULT
    blocking = [i for i in items if i["status"] == BLOCK]
    if any(i["severity"] in MATERIAL for i in blocking):
        return BLOCK
    if blocking or reported == PASS_WITH_CITATION:
        return PASS_WITH_CITATION
    return PASS


def normalize(raw: Any) -> dict:
    """Validate a submitted verdict and return the stored form.

    Raises VerdictError when the verdict cannot be stored as submitted.
    """
    if not isinstance(raw, dict):
        raise VerdictError("structured_verdict must be an object")
    reported = _text(raw.get("verdict_code"), "verdict_code").upper()
    if reported not in VERDICT_CODES:
        raise VerdictError(f"verdict_code must be one of {VERDICT_CODES}")
    raw_items = raw.get("items") or []
    if not isinstance(raw_items, list):
        raise VerdictError("items must be a list")
    items = [_item(item, n) for n, item in enumerate(raw_items)]
    ids = [i["item_id"] for i in items]
    if len(ids) != len(set(ids)):
        raise VerdictError("item_id values must be unique")

    reviewed_raw = raw.get("reviewed") or {}
    if not isinstance(reviewed_raw, dict):
        raise VerdictError("reviewed must be an object")
    if reported == NO_RESULT:
        if items:
            raise VerdictError("a NO_RESULT verdict carries no items")
        reviewed = {"repo": _text(reviewed_raw.get("repo"), "reviewed.repo", required=False),
                    "base_sha": _sha(reviewed_raw.get("base_sha"), "reviewed.base_sha",
                                     required=False),
                    "head_sha": _sha(reviewed_raw.get("head_sha"), "reviewed.head_sha",
                                     required=False)}
    else:
        reviewed = {"repo": _text(reviewed_raw.get("repo"), "reviewed.repo"),
                    "base_sha": _sha(reviewed_raw.get("base_sha"), "reviewed.base_sha",
                                     required=False),
                    "head_sha": _sha(reviewed_raw.get("head_sha"), "reviewed.head_sha",
                                     required=True)}
        if reported == BLOCK and not any(i["status"] == BLOCK for i in items):
            raise VerdictError("a BLOCK verdict must name at least one BLOCK item")

    effective = derive(reported, items)
    return {
        "verdict_code": reported,
        "effective_verdict": effective,
        "mismatch": reported != effective,
        "reviewed": reviewed,
        "items": items,
        "reason": _text(raw.get("reason"), "reason", required=False),
    }


def no_result(reason: str) -> dict:
    """The stored form for a child that produced no usable verdict."""
    return {"verdict_code": NO_RESULT, "reason": reason[:2000], "items": [],
            "reviewed": {}}


def from_child_output(output: str, *, launch_error: str | None = None,
                      exit_ok: bool = True) -> dict:
    """Build the submission for a VERIFY child's run.

    The LAST fenced ``ats-verdict`` block is the verdict. A run that produced
    none, or one that does not validate, becomes NO_RESULT with the reason, so
    an empty or failed review is recorded as exactly that and never as success.
    """
    if launch_error:
        return no_result(f"child did not complete: {launch_error}")
    blocks = _BLOCK_RE.findall(output or "")
    if not blocks:
        reason = "no ats-verdict block in the child's output"
        if not exit_ok:
            reason += " (child exited nonzero)"
        return no_result(reason)
    try:
        parsed = json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        return no_result(f"ats-verdict block is not valid JSON: {exc}")
    try:
        normalize(parsed)
    except VerdictError as exc:
        return no_result(f"ats-verdict block refused: {exc}")
    return parsed


CONTRACT_TEXT = f"""VERDICT OUTPUT CONTRACT (required for VERIFY; #3585)
End your answer with exactly one fenced block tagged {FENCE} containing JSON:

```{FENCE}
{{"verdict_code": "PASS | PASS_WITH_CITATION | BLOCK",
  "reviewed": {{"repo": "<path>", "base_sha": "<sha or empty>", "head_sha": "<sha>"}},
  "items": [{{"item_id": "1", "status": "PASS | BLOCK",
             "severity": "material_safety | material_correctness | nonmaterial",
             "claim_category": "security | authorization | data_integrity | enforcement_coverage | behavior | other",
             "citation": "file:line, test, or command with output"}}]}}
```

Rules: severity follows operational impact. material_safety = a safety,
authority, security or data-loss behavior is wrong or unenforced;
material_correctness = runtime behavior or a test outcome is wrong;
nonmaterial = wording with no behavior change. An inaccurate claim about
security, authorization, data integrity or enforcement coverage is material
even if it is only in docs, comments or messages; only claim_category "other"
may be nonmaterial. Every BLOCK item needs a citation. The verdict that counts
is derived from the items. A missing or invalid block is recorded as NO_RESULT."""
