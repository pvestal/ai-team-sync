"""Claim verification: CLAIM -> OBSERVED EVIDENCE -> COMPARISON -> VERDICT (#3589).

A claim is something an agent or an agent-written record asserts: a
deployment, a review verdict, a test result, a delegation, a completion, an
operator acceptance. Each claim carries the evidence found for and against
it, and one verdict with a precise meaning:

VERIFIED      OBSERVED evidence meets the claim type's own requirements
              (each builder states them and supports only when every required
              part is seen) and nothing contradicts it.
CONTRADICTED  an OBSERVED source -- or the claimant's own record -- is
              inconsistent with the claim.
STALE         the claim held for an earlier state, but the thing it describes
              has changed since (code changed after review, a checkout moved
              past the deployed commit, a process predates its install).
UNVERIFIED    nothing observed supports it: only self-reported records do, or
              the needed evidence is not instrumented. ``missing`` says which.

Precedence: CONTRADICTED > STALE > VERIFIED > UNVERIFIED. A self-reported
record never makes a claim VERIFIED; agent text is evidence only against
itself (``admission``).
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import evidence as ev
from .evidence import EvidenceConfig, parse_time, iso
from .sanitize import safe_text

VERIFIED = "VERIFIED"
CONTRADICTED = "CONTRADICTED"
STALE = "STALE"
UNVERIFIED = "UNVERIFIED"
OBSERVED = "OBSERVED"
SELF_REPORTED = "SELF_REPORTED"

_PASSING = {"PASS", "PASS_WITH_CITATION"}

LEGEND = {
    VERIFIED: "Independently observed evidence meets this claim type's own requirements -- "
    "stated on each claim, often several parts that must ALL be observed -- and nothing "
    "observed contradicts it or shows it went stale. Operator acceptance is never VERIFIED.",
    CONTRADICTED: "An independently observed source, or the claimant's own record, is "
    "inconsistent with the claim.",
    STALE: "The claim held for an earlier state, but what it describes has changed since.",
    UNVERIFIED: "Nothing independently observed supports it: only self-reported records do, "
    "or the evidence is not instrumented.",
    OBSERVED: "Produced by something other than the claimant: git, systemd, installed files, "
    "a running service, intake receipts, a migration ledger, harness-written transcripts.",
    SELF_REPORTED: "Written by the agents being checked: ATS rows, ticket text, verified_by, "
    "agent messages.",
}


@dataclass
class Evidence:
    source: str
    kind: str  # OBSERVED | SELF_REPORTED
    relation: str  # supports | contradicts | stale | context
    summary: str
    at: str | None = None
    ref: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    admission: bool = False


@dataclass
class Claim:
    id: str
    kind: str
    title: str
    statement: str
    claimant: str
    made_at: str | None
    source_record: str
    evidence: list[Evidence] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    meaning: str = ""
    requirements: list[dict[str, Any]] = field(default_factory=list)
    verdict: str = UNVERIFIED
    basis: str = ""

    def add(self, *items: Evidence) -> None:
        self.evidence.extend(items)


def decide(claim: Claim) -> Claim:
    """Apply the verdict rule. The only place a verdict is assigned."""
    against = [
        e
        for e in claim.evidence
        if e.relation == "contradicts" and (e.kind == OBSERVED or e.admission)
    ]
    stale = [e for e in claim.evidence if e.relation == "stale" and e.kind == OBSERVED]
    support = [e for e in claim.evidence if e.relation == "supports" and e.kind == OBSERVED]
    if against:
        claim.verdict, chosen = CONTRADICTED, against
    elif stale:
        claim.verdict, chosen = STALE, stale
    elif support:
        claim.verdict, chosen = VERIFIED, support
    else:
        claim.verdict, chosen = UNVERIFIED, []
    if chosen:
        claim.basis = " | ".join(e.summary for e in chosen[:3])
    else:
        claim.basis = (
            "; ".join(claim.missing[:3])
            if claim.missing
            else "Only self-reported records support this claim."
        )
    return claim


# --------------------------------------------------------------------------- helpers


def _short(sha: str | None) -> str:
    return (sha or "")[:8]


def _session_windows(sessions: list[dict], slack_min: int = 5) -> list[tuple[datetime, datetime]]:
    windows = []
    for row in sessions:
        start = parse_time(row.get("started_at"))
        end = parse_time(row.get("completed_at")) or datetime.now(timezone.utc)
        if start:
            windows.append(
                (start - timedelta(minutes=slack_min), end + timedelta(minutes=slack_min))
            )
    return windows


def _jsonish(value: Any, fallback: Any) -> Any:
    import json

    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _stems(text: str) -> set[str]:
    words = re.findall(r"[a-z]{4,}", text.lower())
    out = set()
    for word in words:
        for suffix in ("ing", "ed", "es", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
        out.add(word)
    return out - {"with", "that", "this", "from", "into", "than", "when", "only", "none"}


_NEGATION = re.compile(
    r"\bnot (?:done|implemented|yet|run|replayed|deployed)\b|\bdeferred\b|\bskipped\b",
    re.I,
)


# Marks a candidate acceptance turn for the operator's eye; it never decides a verdict.
_NEGATING = re.compile(r"\b(?:not|no|never|reject\w*|redo|don't|isn't|won't)\b", re.I)


def acceptance_criteria(description: str) -> list[str]:
    match = re.search(r"ACCEPTANCE:\s*(.+?)(?:\n\s*\n|\Z)", description or "", re.S)
    if not match:
        return []
    body = " ".join(match.group(1).split())
    return [part.strip(" .") for part in re.split(r";\s*", body) if part.strip(" .")]


_COUNT_PATTERNS = (
    ("passed", re.compile(r"(\d+) passed")),
    ("passed", re.compile(r"(\d+)P\b")),
    ("passed", re.compile(r"\b(\d+)/\1\b")),
    ("failed", re.compile(r"(\d+) (?:failed|failures?)")),
    ("failed", re.compile(r"(\d+)F\b")),
    ("error", re.compile(r"(\d+) errors?\b")),
)


def claimed_counts(text: str) -> set[tuple[str, int]]:
    """Every (outcome, number) a test claim asserts, e.g. {("passed", 44)}.

    "0 failed" is not a count a run prints; it is what a run WITHOUT failures means.
    """
    found: set[tuple[str, int]] = set()
    for outcome, pattern in _COUNT_PATTERNS:
        found.update((outcome, int(n)) for n in pattern.findall(text or ""))
    return {(o, n) for o, n in found if n or o == "passed"}


def claimed_test_files(text: str) -> set[str]:
    return set(re.findall(r"\btest_[\w]+\.py\b", text or ""))


def observed_counts(run: ev.ToolRun) -> set[tuple[str, int]]:
    found: set[tuple[str, int]] = set()
    for summary in run.summaries:
        for outcome, pattern in (
            ("passed", r"(\d+) passed"),
            ("failed", r"(\d+) failed"),
            ("error", r"(\d+) errors?\b"),
        ):
            found.update((outcome, int(n)) for n in re.findall(pattern, summary))
    return found


def run_outcome(run: ev.ToolRun, counts: set[tuple[str, int]]) -> tuple[str, str]:
    """("ok" | "fail" | "incomplete", why) for one run, judged against the claim.

    Failures the claim itself states ("12 failed, 1912 passed") are consistent with
    it; failures it omits are not. pytest exits 1 when tests failed; 2-5 mean the run
    never completed (interrupted, internal or usage error, nothing collected), which
    shows nothing either way.
    """
    observed = observed_counts(run)
    unclaimed = sorted(
        f"{n} {o}" for o, n in observed if o != "passed" and n and (o, n) not in counts
    )
    if unclaimed:
        return "fail", ", ".join(unclaimed) + " not stated in the claim"
    nonzero = [code for code in run.exit_codes if code != 0]
    if not nonzero:
        return "ok", ""
    codes = ",".join(str(c) for c in run.exit_codes)
    if all(code == 1 for code in nonzero):
        if any(o != "passed" and n for o, n in observed):
            return "ok", ""
        return "fail", f"exit {codes} with no failure in its summary"
    return "incomplete", f"exit {codes}"


def passed_cleanly(run: ev.ToolRun, counts: set[tuple[str, int]]) -> bool:
    """An executed run that printed passing tests and is consistent with the claim."""
    return (
        run.executed
        and run_outcome(run, counts)[0] == "ok"
        and any(o == "passed" and n for o, n in observed_counts(run))
    )


def run_matches(run: ev.ToolRun, counts: set[tuple[str, int]], files: set[str]) -> set:
    """The claimed counts this run printed (empty when it ran a different file)."""
    if files and not any(name in command for command in run.commands for name in files):
        return set()
    hit = counts & observed_counts(run)
    # "2 failed" alone matches unrelated suites by coincidence; when the claim
    # states pass counts, a relevant run must print one of them.
    if any(outcome == "passed" for outcome, _ in counts) and not any(
        outcome == "passed" for outcome, _ in hit
    ):
        return set()
    return hit


def _run_line(run: ev.ToolRun) -> str:
    codes = ",".join(str(c) for c in run.exit_codes) or "unrecorded"
    summary = "; ".join(run.summaries) or "no pytest summary line"
    command = (run.test_commands or run.commands)[0]
    return f"{command[:160]} -> exit {codes}; {summary}"


def _run_detail(run: ev.ToolRun) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "commands": run.commands,
        "exit_codes": run.exit_codes,
        "summaries": run.summaries,
        "recorded_by": run.source,
        "ref": run.raw_ref,
    }
    if run.cwd:
        detail["workdir"] = run.cwd
    return detail


def _require_every_part(claim: Claim, parts: list[str]) -> None:
    """A claim with several parts (a deployment's files, units and probe; a migration's
    file and ledger) is established only when every part is: one supported part must
    not read as the whole. Missing parts turn support into context."""
    supported = {
        e.detail.get("part") for e in claim.evidence if e.relation == "supports" and e.detail
    }
    lacking = [part for part in parts if part not in supported]
    if not lacking:
        return
    for e in claim.evidence:
        if e.relation == "supports":
            e.relation = "context"
    claim.missing.append("Not every required part was observed: " + ", ".join(lacking))


def _range_command(run: ev.ToolRun, base: str, head: str) -> str:
    return next(
        (c for c in run.commands if ev.git_range_command(c, base, head)),
        run.commands[0],
    )


def _range_inspection(run: ev.ToolRun, repo: str, base: str | None, head: str) -> str | None:
    """None when ``run`` is a successful git inspection of exactly base..head in ``repo``;
    otherwise why it does not count ('' when it does not even name the range)."""
    if not base:
        return ""
    found = [r for c in run.commands if (r := ev.git_range_command(c, base, head)) is not None]
    if not found:
        named = any(s.startswith(base[:7]) for s in run.ranges) and any(
            s.startswith(head[:7]) for s in run.ranges
        )
        return "not a git diff/log of base..head in that order" if named else ""
    git_dir, exit_is_gits = found[0]
    if run.exit_codes and any(code != 0 for code in run.exit_codes):
        return f"exit {','.join(str(c) for c in run.exit_codes)}"
    if run.git_fatal:
        return "git reported a fatal error"
    if not run.git_output:
        if not run.exit_codes:
            return "no exit code and no git output recorded"
        if not exit_is_gits:
            return "the exit code is the shell's, not git's (|| or a pipe), and no git output"
    place = git_dir or run.cwd or ""
    if os.path.isabs(place) and ev.same_repository(place, repo) is False:
        return f"ran in {place}, not {repo}"
    return None


# --------------------------------------------------------------------------- builder


class TicketVerifier:
    """Build every claim for one ticket from live, read-only sources."""

    def __init__(self, ticket_id: int, config: EvidenceConfig):
        self.ticket_id = ticket_id
        self.config = config
        self.ats = ev.AtsRecords(config.ats_db).ticket(ticket_id)
        self.sessions = {row["id"]: row for row in self.ats["sessions"]}
        endpoint = config.ticket_endpoint(ticket_id)
        task: dict[str, Any] = ev.http_json(endpoint) if endpoint else {"state": "UNCONFIGURED"}
        self.task_state = task["state"] if task.get("status") == 200 else "UNAVAILABLE"
        self.task: dict[str, Any] = task.get("body") or {} if task.get("status") == 200 else {}
        self.verified_by: dict[str, Any] = _jsonish(self.task.get("verified_by"), {}) or {}
        self.codex: dict[str, list[ev.CodexTranscript]] = {}
        self._claude_runs: list[ev.ToolRun] | None = None

    # ------------------------------------------------------------ transcripts

    def codex_transcripts(self, delegation: dict) -> list[ev.CodexTranscript]:
        key = delegation["id"]
        if key not in self.codex:
            paths = ev.find_codex_transcript(
                self.config.home,
                key,
                parse_time(delegation.get("created_at")),
                parse_time(delegation.get("closed_at")),
            )
            self.codex[key] = [ev.read_codex_transcript(path) for path in paths]
        return self.codex[key]

    def claude_runs(self) -> list[ev.ToolRun]:
        if self._claude_runs is None:
            runs: list[ev.ToolRun] = []
            by_agent: dict[str, list[dict]] = {}
            for row in self.sessions.values():
                by_agent.setdefault(row.get("agent") or "", []).append(row)
            for agent, rows in by_agent.items():
                main, subs = ev.claude_transcripts(self.config.home, agent)
                windows = _session_windows(rows)
                for path in [*main, *subs]:
                    runs += ev.claude_test_runs(path, windows)
            self._claude_runs = runs
        return self._claude_runs

    def all_test_runs(self) -> list[ev.ToolRun]:
        runs = list(self.claude_runs())
        for delegation in self.ats["delegations"]:
            for transcript in self.codex_transcripts(delegation):
                runs += [run for run in transcript.runs if run.is_test or run.summaries]
        return sorted(runs, key=lambda run: run.at)

    # ------------------------------------------------------------ claims

    def delegation_claims(self) -> list[Claim]:
        claims = []
        for d in self.ats["delegations"]:
            parent = self.sessions.get(d["parent_session_id"], {})
            child = self.sessions.get(d.get("child_session_id") or "", {})
            result = d.get("result_summary") or ""
            claim = Claim(
                id=f"delegation:{d['id']}",
                kind="delegation",
                title=f"{d['delegated_worker']} {d['mode']} delegation {_short(d['id'])}",
                statement=(
                    f"{parent.get('agent') or d['delegating_worker']} delegated a {d['mode']} task "
                    f"to {d['delegated_worker']} (resolved binary "
                    f"{d.get('resolved_binary') or 'none recorded'}); state '{d['state']}'. "
                    f"Objective: {safe_text(d.get('objective'), 300)}"
                ),
                claimant=f"ats delegate in session {_short(d['parent_session_id'])} "
                f"({parent.get('agent', '?')})",
                made_at=iso(parse_time(d.get("created_at"))),
                source_record=f"ats.db delegations/{d['id']}",
                meaning="VERIFIED means the delegated process's own transcript names this "
                "delegation, was written by the claimed runtime inside the delegation window, "
                "ran at least one command and returned a final message. It does not mean the "
                "objective was achieved: review and test claims judge that.",
            )
            created = parse_time(d.get("created_at"))
            closed = parse_time(d.get("closed_at"))
            transcripts = (
                self.codex_transcripts(d) if str(d["delegated_worker"]).startswith("codex") else []
            )
            for t in transcripts:
                started = parse_time(t.started_at)
                lag = (started - created).total_seconds() if started and created else None
                in_window = (
                    started is not None
                    and created is not None
                    and created - timedelta(minutes=1)
                    <= started
                    <= (closed or datetime.now(timezone.utc)) + timedelta(minutes=1)
                )
                runtime_ok = (t.originator or "").startswith("codex")
                commands = sum(len(r.commands) for r in t.runs)
                worked = commands > 0 and bool(t.final_message.strip())
                if in_window and runtime_ok and not worked:
                    claim.missing.append(
                        f"Codex's transcript is bound but shows no recorded work ({commands} "
                        "commands" + ("" if t.final_message.strip() else ", no final message") + ")"
                    )
                claim.add(
                    Evidence(
                        source="codex transcript",
                        kind=OBSERVED,
                        relation=(
                            "contradicts"
                            if not (in_window and runtime_ok)
                            else "supports" if worked else "context"
                        ),
                        summary=(
                            f"Codex's own transcript names this delegation in its first packet; "
                            f"originator {t.originator}, model {t.model}, CLI {t.cli_version}, "
                            f"started {t.started_at}"
                            + (
                                f" ({lag:+.0f}s after the delegation was recorded)"
                                if lag is not None
                                else ""
                            )
                            + ("" if in_window else " -- OUTSIDE the delegation window")
                            + f"; {commands} command(s) recorded"
                        ),
                        at=t.started_at,
                        ref=t.path,
                        detail={
                            "cwd": t.cwd,
                            "packet_parent_task": t.packet_task,
                            "commands_recorded": commands,
                            "final_message": t.final_message,
                        },
                    )
                )
            if not transcripts:
                claim.missing.append(
                    "No harness transcript names this delegation, so which process did the "
                    "work is unobserved"
                    + (
                        ""
                        if str(d["delegated_worker"]).startswith("codex")
                        else " (Claude delegate transcripts are not bound yet)"
                    )
                )
            if d["state"] in ("returned", "closed") and (
                not result.strip() or result.strip().startswith("(no result")
            ):
                claim.add(
                    Evidence(
                        source="ats record",
                        kind=SELF_REPORTED,
                        relation="contradicts",
                        admission=True,
                        summary=f"Recorded as '{d['state']}' with launch evidence "
                        f"{safe_text(d.get('evidence'), 120)}, but the returned result is "
                        f"'{safe_text(result, 60)}': the delegation produced no output",
                        at=iso(closed),
                        ref=f"delegations/{d['id']}.result_summary",
                    )
                )
            if child and child.get("ticket_id") is None:
                claim.add(
                    Evidence(
                        source="ats record",
                        kind=SELF_REPORTED,
                        relation="context",
                        summary=f"Child session {_short(child['id'])} carries no ticket; it is "
                        f"attributed to #{self.ticket_id} only through delegations.parent_task",
                    )
                )
            if d["state"] == "returned":
                claim.add(
                    Evidence(
                        source="ats record",
                        kind=SELF_REPORTED,
                        relation="context",
                        summary="The owner never closed this delegation (no accept/reject recorded)",
                    )
                )
            elif d.get("verdict"):
                claim.add(
                    Evidence(
                        source="ats record",
                        kind=SELF_REPORTED,
                        relation="context",
                        summary=f"Owner's disposition: {safe_text(d['verdict'], 300)}",
                        at=iso(closed),
                    )
                )
            claims.append(claim)
        return claims

    def review_claims(self) -> list[Claim]:
        claims = []
        owner_text = {
            key: str(value)
            for key, value in (self.verified_by.get("reviews") or {}).items()
            if isinstance(value, str)
        }
        for d in self.ats["delegations"]:
            sv = _jsonish(d.get("structured_verdict"), {}) or {}
            reviewed = sv.get("reviewed") or {}
            repo, base, head = (
                reviewed.get("repo"),
                reviewed.get("base_sha"),
                reviewed.get("head_sha"),
            )
            code = sv.get("verdict_code") or d.get("verdict_code")
            if not (repo and head and code):
                continue
            mentions = [f"{k}: {v}" for k, v in owner_text.items() if _short(d["id"]) in v]
            claim = Claim(
                id=f"review:{d['id']}",
                kind="review",
                title=f"Review {_short(d['id'])}: {code} on {os.path.basename(repo)} "
                f"{_short(base)}..{_short(head)}",
                statement=f"Structured verdict {code} on {repo} {base}..{head}."
                + (f"\nReason: {safe_text(sv.get('reason'), 400)}" if sv.get("reason") else "")
                + ("\nOwner's account: " + " | ".join(mentions) if mentions else ""),
                claimant=f"{d.get('delegated_worker', '?')} delegate (structured verdict recorded by "
                f"ats delegate)",
                made_at=iso(parse_time(d.get("closed_at"))),
                source_record=f"ats.db delegations/{d['id']}.structured_verdict",
                meaning="VERIFIED means the claimed process examined exactly this commit range "
                "and its own last message states this verdict. It does not mean the code is "
                "correct; read the reviewer's evidence below.",
            )
            transcripts = self.codex_transcripts(d)
            if not transcripts:
                claim.missing.append(
                    "No reviewer transcript is bound to this delegation, so what it examined is "
                    "unobserved"
                )
            # A review is VERIFIED only when ONE bound transcript shows both halves: a
            # successful git inspection of exactly base..head in this repository, and an
            # overall verdict equal to the record.
            complete = False
            for t in transcripts:
                ranged, rejected = [], []
                for run in t.runs:
                    why = _range_inspection(run, repo, base, head)
                    if why is None:
                        ranged.append(run)
                    elif why:
                        rejected.append((run, why))
                overall = t.final_verdict
                both = bool(ranged) and overall == code
                complete = complete or both
                if overall is not None and overall != code:
                    claim.add(
                        Evidence(
                            source="codex transcript",
                            kind=OBSERVED,
                            relation="contradicts",
                            summary=f"The reviewer's own overall verdict is {overall}, not the "
                            f"recorded {code}",
                            ref=t.path,
                            detail={"final_message": t.final_message},
                        )
                    )
                elif overall is None:
                    claim.missing.append(
                        "The reviewer's last message states no overall verdict (no ats-verdict "
                        "block, VERDICT line, or leading verdict)"
                    )
                else:
                    claim.add(
                        Evidence(
                            source="codex transcript",
                            kind=OBSERVED,
                            relation="supports" if both else "context",
                            summary=f"The reviewer's own overall verdict is {overall}",
                            ref=t.path,
                            detail={"final_message": t.final_message},
                        )
                    )
                if ranged:
                    claim.add(
                        Evidence(
                            source="codex transcript",
                            kind=OBSERVED,
                            relation="supports" if both else "context",
                            summary=f"Reviewer ran {len(ranged)} successful git inspection(s) of "
                            f"exactly {_short(base)}..{_short(head)}; first: "
                            f"{_range_command(ranged[0], base or '', head)[:140]}",
                            at=ranged[0].at,
                            ref=ranged[0].raw_ref,
                            detail={"runs": [_run_detail(r) for r in ranged]},
                        )
                    )
                else:
                    claim.missing.append(
                        "No successful git inspection of exactly the reviewed base..head in this "
                        "repository appears in the reviewer's transcript"
                    )
                for run, why in rejected:
                    claim.add(
                        Evidence(
                            source="codex transcript",
                            kind=OBSERVED,
                            relation="context",
                            summary=f"Names the reviewed range but does not count ({why}): "
                            f"{run.commands[0][:140]}",
                            at=run.at,
                            ref=run.raw_ref,
                            detail=_run_detail(run),
                        )
                    )
                for run in [r for r in t.runs if r.is_test]:  # pytest actually invoked
                    skipped = any("skipped" in s for s in run.summaries)
                    claim.add(
                        Evidence(
                            source="codex transcript",
                            kind=OBSERVED,
                            relation="context",
                            summary="Reviewer test run: "
                            + _run_line(run)
                            + (" (skipped tests did NOT execute)" if skipped else ""),
                            at=run.at,
                            ref=run.raw_ref,
                            detail=_run_detail(run),
                        )
                    )
            if transcripts and not complete:
                claim.missing.append(
                    "No single bound transcript shows both the exact range and a matching "
                    "overall verdict"
                )
            for label, ref in (("base", base), ("head", head)):
                found = ev.git_commit(repo, ref) if ref else None
                claim.add(
                    Evidence(
                        source="git",
                        kind=OBSERVED,
                        # Existence alone proves nothing was reviewed; absence disproves it.
                        relation="context" if found else "contradicts",
                        summary=f"Reviewed {label} {_short(ref)} "
                        + (f"exists: {found['subject']}" if found else "is NOT in the repository"),
                        at=found["committed_at"] if found else None,
                        ref=f"{repo}@{ref}",
                    )
                )
            claims.append(claim)
        return claims

    def deployed_targets(self) -> dict[str, dict]:
        deployed = self.verified_by.get("deployed") or {}
        return {k: v for k, v in deployed.items() if isinstance(v, dict) and v.get("sha")}

    def deployment_claims(self) -> list[Claim]:
        claims = []
        closer = self.verified_by.get("authority") or {}
        for name, spec in self.deployed_targets().items():
            repo, sha = spec.get("repo", ""), spec["sha"]
            claim = Claim(
                id=f"deploy:{name}",
                kind="deployment",
                title=f"{name} deployed at {_short(sha)}",
                statement=f"{name}: deployed {sha} from {repo}. Running: "
                f"{safe_text(spec.get('running'), 300)}. Commits: {', '.join(spec.get('commits') or [])}",
                claimant="ticket verified_by (written when the ticket was closed)",
                made_at=self.verified_by.get("verified_at") or closer.get("checked_at"),
                source_record=f"ticket/{self.ticket_id}.verified_by.deployed.{name}",
                meaning="VERIFIED needs every part observed: the installed package files (code "
                "and package data) or the clean live checkout match the commit; each listed "
                "service is active with a live PID and started after that code was in place; and "
                "the running service answered its probe, naming this build where it reports one. "
                "It does not test the deployed feature's behavior. The commit merely existing in "
                "git never verifies a deployment.",
            )
            commit = ev.git_commit(repo, sha)
            claim.add(
                Evidence(
                    source="git",
                    kind=OBSERVED,
                    # The commit existing says nothing about what runs; absence disproves.
                    relation="context" if commit else "contradicts",
                    summary=f"Commit {_short(sha)} "
                    + (
                        f"exists ({commit['committed_at']}): {commit['subject']}"
                        if commit
                        else "is NOT in the repository"
                    ),
                    at=commit["committed_at"] if commit else None,
                    ref=f"{repo}@{sha}",
                )
            )
            listed = [c for c in spec.get("commits") or [] if isinstance(c, str)]
            if listed and commit:
                bad = [
                    c
                    for c in listed
                    if not (ev.git_commit(repo, c) and ev.git_is_ancestor(repo, c, sha))
                ]
                claim.add(
                    Evidence(
                        source="git",
                        kind=OBSERVED,
                        relation="contradicts" if bad else "context",
                        summary=(
                            f"{len(listed) - len(bad)}/{len(listed)} listed commits exist and are "
                            f"ancestors of {_short(sha)}"
                            + (f"; not: {', '.join(bad)}" if bad else "")
                        ),
                        ref=repo,
                    )
                )
            target = self.config.target_for(repo)
            if target is None:
                claim.missing.append(
                    f"No runtime evidence: {repo} has no known install or service on this host"
                )
            elif commit:
                if target.install_dir:
                    self._installed_evidence(claim, target, commit)
                else:
                    self._checkout_evidence(claim, target, commit)
                self._probe_evidence(claim, target, commit)
                _require_every_part(
                    claim, ["code", *(f"unit:{unit}" for unit, _ in target.units), "probe"]
                )
            claims.append(claim)
            migration = spec.get("migration")
            if isinstance(migration, dict) and migration.get("id"):
                claims.append(self._migration_claim(name, repo, sha, migration))
        return claims

    def _installed_evidence(self, claim: Claim, target: ev.DeployTarget, commit: dict) -> None:
        match = ev.installed_matches_commit(target, commit["sha"])
        if match["state"] != "OBSERVED":
            claim.missing.append(f"Installed package not comparable: {match.get('reason')}")
            return
        clean = not match["differ"] and not match["missing"]
        relation = "supports" if clean else "contradicts"
        if not clean:
            head = ev.git_head(target.repo)
            if head and head != commit["sha"]:
                later = ev.installed_matches_commit(target, head)
                if not later.get("differ") and not later.get("missing"):
                    relation = "stale"
        claim.add(
            Evidence(
                source="installed files",
                kind=OBSERVED,
                relation=relation,
                summary=f"{match['same']} installed package files (code and package data) are "
                f"byte-identical to {_short(commit['sha'])}"
                + (
                    f"; differ: {len(match['differ'])}; missing: {len(match['missing'])}"
                    if not clean
                    else ""
                )
                + (" (the install now matches a later commit)" if relation == "stale" else ""),
                at=match.get("newest_install_mtime"),
                ref=match["install_dir"],
                detail={"differ": match["differ"], "missing": match["missing"], "part": "code"},
            )
        )
        installed_at = parse_time(match.get("newest_install_mtime"))
        for unit, user in target.units:
            self._unit_evidence(claim, unit, user, installed_at, "the installed files were written")

    def _unit_evidence(
        self, claim: Claim, unit: str, user: bool, landed: datetime | None, landed_what: str
    ) -> None:
        """One service: running (active, live PID) and started after the code landed."""
        state = ev.unit_started(unit, user)
        ref = f"systemctl {'--user ' if user else ''}show {unit}"
        pid = str(state.get("main_pid") or "0")
        if state.get("state") == "UNAVAILABLE":
            claim.missing.append(f"{unit}: systemd state unreadable")
            return
        if state.get("state") != "active" or pid in ("", "0"):
            claim.add(
                Evidence(
                    source="systemd",
                    kind=OBSERVED,
                    relation="contradicts",
                    summary=f"{unit} is {state.get('state')} (pid {pid}): it is not running",
                    ref=ref,
                    detail={"part": f"unit:{unit}"},
                )
            )
            return
        started = parse_time(state.get("started_at"))
        if not started or not landed:
            claim.missing.append(
                f"{unit}: "
                + ("start time unavailable" if not started else f"when {landed_what} is unknown")
            )
            return
        fresh = started >= landed
        claim.add(
            Evidence(
                source="systemd",
                kind=OBSERVED,
                relation="supports" if fresh else "stale",
                summary=f"{unit} (active, pid {pid}) started {iso(started)}, "
                + ("after" if fresh else "BEFORE")
                + f" {landed_what} ({iso(landed)})"
                + ("" if fresh else "; whether it loads the changed files is not checked"),
                at=iso(started),
                ref=ref,
                detail={"part": f"unit:{unit}"},
            )
        )

    def _probe_evidence(self, claim: Claim, target: ev.DeployTarget, commit: dict) -> None:
        """Ask the running service itself: does it answer, and which build is it?"""
        if not target.probe_url:
            claim.missing.append(
                f"No runtime probe is configured for {target.repo}: whether the running service "
                "answers, and which build it runs, is unobserved"
            )
            return
        url, sha = target.probe_url, commit["sha"]
        answer = ev.http_json(url)
        body: dict[str, Any] = answer["body"] if isinstance(answer.get("body"), dict) else {}
        if answer.get("state") != "OBSERVED" or answer.get("status") != 200:
            claim.add(
                Evidence(
                    source="running service",
                    kind=OBSERVED,
                    relation="contradicts",
                    summary=f"{url} did not answer "
                    f"({answer.get('status') or answer.get('error') or answer.get('state')}): "
                    "the service is not serving",
                    ref=url,
                    detail={"part": "probe"},
                )
            )
            return
        if not target.probe_reports_commit:
            claim.add(
                Evidence(
                    source="running service",
                    kind=OBSERVED,
                    relation="supports",
                    summary=f"{url} answered 200 -- liveness only; it does not name the running "
                    "revision, which rests on the start-time comparison above",
                    ref=url,
                    detail={"part": "probe", "body": body},
                )
            )
            return
        running = str(body.get("commit") or "")
        package = str(body.get("package_path") or "")
        if not running:
            relation, summary = "context", f"{url} answered but reported no build commit"
            claim.missing.append("The running service did not report which build it runs")
        elif sha.startswith(running):
            relation, summary = "supports", f"The running service reports build {running}"
        elif ev.git_is_ancestor(target.repo, sha, running):
            relation = "stale"
            summary = f"The running service reports a later build {running}"
        else:
            relation = "contradicts"
            summary = f"The running service reports build {running}, not {_short(sha)}"
        if (
            relation == "supports"
            and package
            and target.install_dir
            and os.path.realpath(package) != os.path.realpath(target.install_dir)
        ):
            relation = "contradicts"
            summary += f", but imports from {package}, not the install compared above"
        elif relation == "supports" and target.install_dir and not package:
            relation = "context"
            summary += ", but reports no package_path, so it is not bound to the install above"
            claim.missing.append(
                "The running service did not report its package_path: which installed files it "
                "runs is unobserved"
            )
        claim.add(
            Evidence(
                source="running service",
                kind=OBSERVED,
                relation=relation,
                summary=summary
                + (f" (pid {body['pid']})" if body.get("pid") else "")
                + ("; installed from a DIRTY tree" if body.get("dirty") else ""),
                ref=url,
                detail={"part": "probe", "body": body},
            )
        )

    def _checkout_evidence(self, claim: Claim, target: ev.DeployTarget, commit: dict) -> None:
        head = ev.git_head(target.repo)
        if not head:
            claim.missing.append("Checkout HEAD unreadable")
            return
        dirty = ev.git_dirty_tracked(target.repo)
        if head == commit["sha"]:
            claim.add(
                Evidence(
                    source="git",
                    kind=OBSERVED,
                    # Uncommitted edits mean the checkout matches no commit exactly.
                    relation="context" if dirty else "supports",
                    summary=f"Live checkout HEAD is exactly {_short(head)}"
                    + (", but tracked files are modified" if dirty else ""),
                    ref=f"{target.repo} HEAD",
                    detail={"part": "code"},
                )
            )
        elif ev.git_is_ancestor(target.repo, commit["sha"], head):
            claim.add(
                Evidence(
                    source="git",
                    kind=OBSERVED,
                    relation="stale",
                    summary=f"Live checkout has moved past {_short(commit['sha'])} to "
                    f"{_short(head)}; the running code now includes later commits",
                    ref=f"{target.repo} HEAD",
                    detail={"changed": ev.git_changed_paths(target.repo, commit["sha"], head)},
                )
            )
        else:
            claim.add(
                Evidence(
                    source="git",
                    kind=OBSERVED,
                    relation="contradicts",
                    summary=f"Live checkout HEAD {_short(head)} does not contain {_short(commit['sha'])}",
                    ref=f"{target.repo} HEAD",
                )
            )
        if dirty:
            claim.add(
                Evidence(
                    source="git",
                    kind=OBSERVED,
                    relation="context",
                    summary=f"{len(dirty)} tracked file(s) modified in the live checkout",
                    detail={"paths": dirty},
                )
            )
        # The code is on disk from when HEAD moved there (the reflog), which can be
        # later than the commit's own date: a merge lands an older commit.
        head_commit = ev.git_commit(target.repo, head)
        committed = parse_time(head_commit["committed_at"]) if head_commit else None
        moved = ev.git_head_moved_at(target.repo)
        landed = max(moved, committed) if moved and committed else None
        for unit, user in target.units:
            self._unit_evidence(claim, unit, user, landed, f"the checkout moved to {_short(head)}")

    def _migration_claim(self, name: str, repo: str, sha: str, migration: dict) -> Claim:
        mig_id = str(migration.get("id"))
        claim = Claim(
            id=f"migration:{name}:{mig_id}",
            kind="deployment",
            title=f"{name} migration {mig_id} applied",
            statement=f"Migration {mig_id} ({migration.get('file')}) applied at "
            f"{migration.get('applied_at')} via {migration.get('applied_via')}; source sha256 "
            f"prefix {migration.get('source_sha256_prefix')}",
            claimant="ticket verified_by (written when the ticket was closed)",
            made_at=self.verified_by.get("verified_at"),
            source_record=f"ticket/{self.ticket_id}.verified_by.deployed.{name}.migration",
            meaning="VERIFIED means BOTH: the migration file at the deployed commit hashes as "
            "claimed, AND the database's migration ledger records the id. Either one alone stays "
            "UNVERIFIED. A ledger row says the migration was recorded as applied; it does not "
            "show that the migration's intended behavior works.",
        )
        prefix = str(migration.get("source_sha256_prefix") or "")
        if migration.get("file"):
            digest = ev.git_blob_sha256(repo, sha, str(migration["file"]))
            if digest is None:
                claim.add(
                    Evidence(
                        source="git",
                        kind=OBSERVED,
                        relation="contradicts",
                        summary=f"{migration['file']} does not exist at {_short(sha)}",
                    )
                )
            elif prefix:
                claim.add(
                    Evidence(
                        source="git",
                        kind=OBSERVED,
                        relation="supports" if digest.startswith(prefix) else "contradicts",
                        summary=f"sha256 of {migration['file']} at {_short(sha)} is "
                        f"{digest[:12]} (claimed prefix {prefix})",
                        ref=f"{repo}@{sha}:{migration['file']}",
                        detail={"part": "file"},
                    )
                )
            else:
                claim.missing.append("The claim gives no file hash to check the migration file")
        else:
            claim.missing.append("The claim names no migration file")
        ledger = self.config.ledgers.get(name)
        if ledger is None:
            claim.missing.append(
                f"No migration ledger is configured for target '{name}': whether it was applied "
                "is unobserved"
            )
        elif re.fullmatch(r"[0-9A-Za-z_-]{1,40}", mig_id) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]{0,62}", ledger.table
        ):
            rows = ev.psql_rows(
                ledger.db,
                f"SELECT id, applied_at FROM {ledger.table} WHERE id = '{mig_id}'",
                host=ledger.host,
                user=ledger.user,
            )
            if rows["state"] != "OBSERVED":
                claim.missing.append(f"Migration ledger unreadable: {rows.get('error')}")
            else:
                found = rows["rows"]
                claim.add(
                    Evidence(
                        source="migration ledger",
                        kind=OBSERVED,
                        relation="supports" if found else "contradicts",
                        summary=(
                            f"{ledger.table} records {mig_id} applied {found[0][1]}"
                            if found
                            else f"{ledger.table} has NO row for {mig_id}"
                        ),
                        ref=f"{ledger.db}.{ledger.table}",
                        detail={"part": "ledger"},
                    )
                )
        _require_every_part(claim, ["file", "ledger"])
        return claim

    def coverage_claims(self, review_verdicts: dict[str, str] | None = None) -> list[Claim]:
        """``review_verdicts`` maps delegation id -> its decided review verdict."""
        if review_verdicts is None:
            review_verdicts = {
                c.id.split(":", 1)[1]: decide(c).verdict for c in self.review_claims()
            }
        claims = []
        reviews = []
        for d in self.ats["delegations"]:
            sv = _jsonish(d.get("structured_verdict"), {}) or {}
            reviewed = sv.get("reviewed") or {}
            if reviewed.get("repo") and reviewed.get("head_sha"):
                reviews.append((d, reviewed))
        post = (self.verified_by.get("reviews") or {}).get("post_review_change")
        for name, spec in self.deployed_targets().items():
            repo, sha = spec.get("repo", ""), spec["sha"]
            mine = [
                (d, r) for d, r in reviews if os.path.realpath(r["repo"]) == os.path.realpath(repo)
            ]
            claim = Claim(
                id=f"coverage:{name}",
                kind="review coverage",
                title=f"{name}: deployed code {_short(sha)} was the code reviewed",
                statement=f"The {name} code deployed at {sha} passed independent review."
                + (f"\nOwner's account of post-review changes: {post}" if post else ""),
                claimant="ticket verified_by.reviews (written when the ticket was closed)",
                made_at=self.verified_by.get("verified_at"),
                source_record=f"ticket/{self.ticket_id}.verified_by.reviews",
                meaning="VERIFIED means a PASSING review, itself VERIFIED, examined exactly the "
                "deployed commit. STALE means the deployed commit descends from a passing reviewed "
                "head but code changed after that review; the diff below is the unreviewed change.",
            )
            if not mine:
                claim.missing.append(f"No structured review of {repo} is recorded for this ticket")
            # Only a PASSING review can cover code, and it supports only when its own
            # claim is VERIFIED; a BLOCK at the deployed head is the record against itself.
            passing = [(d, r) for d, r in mine if (d.get("verdict_code") or "") in _PASSING]
            covering = [(d, r) for d, r in passing if r["head_sha"] == sha]
            ancestors = [
                (d, r)
                for d, r in passing
                if r["head_sha"] != sha and ev.git_is_ancestor(repo, r["head_sha"], sha)
            ]
            for d, r in covering:
                verified = review_verdicts.get(d["id"]) == VERIFIED
                claim.add(
                    Evidence(
                        source="git",
                        kind=OBSERVED,
                        relation="supports" if verified else "context",
                        summary=f"Passing review {_short(d['id'])} examined head "
                        f"{_short(r['head_sha'])}, which is exactly the deployed commit",
                        ref=f"{repo}@{sha}",
                    )
                )
                if not verified:
                    claim.missing.append(
                        f"Review {_short(d['id'])} covers the deployed commit, but that review's "
                        f"own claim is {review_verdicts.get(d['id'], UNVERIFIED)}"
                    )
            blocked_at_head = [
                (d, r) for d, r in mine if r["head_sha"] == sha and (d, r) not in passing
            ]
            if blocked_at_head and not covering:
                d, r = blocked_at_head[-1]
                claim.add(
                    Evidence(
                        source="ats record",
                        kind=SELF_REPORTED,
                        relation="contradicts",
                        admission=True,
                        summary=f"The review of exactly the deployed commit ({_short(d['id'])}) "
                        f"returned {d.get('verdict_code') or 'no passing verdict'}, and no "
                        f"passing review covers it",
                        ref=f"delegations/{d['id']}",
                    )
                )
            if not covering and ancestors:
                latest = max(
                    ancestors,
                    key=lambda item: len(ev.git_changed_paths(repo, item[1]["head_sha"], sha)) * -1,
                )
                d, r = latest
                changed = ev.git_changed_paths(repo, r["head_sha"], sha)
                held = review_verdicts.get(d["id"]) == VERIFIED
                if not held:
                    claim.missing.append(
                        f"The last passing review ({_short(d['id'])}) is itself "
                        f"{review_verdicts.get(d['id'], UNVERIFIED)}, so even the earlier state is "
                        f"unproven"
                    )
                claim.add(
                    Evidence(
                        source="git",
                        kind=OBSERVED,
                        relation="stale" if held else "context",
                        summary=f"Deployed {_short(sha)} differs from the last passing reviewed "
                        f"head {_short(r['head_sha'])} (review {_short(d['id'])}) in "
                        f"{len(changed)} path(s) that no review examined",
                        ref=f"{repo} {r['head_sha']}..{sha}",
                        detail={
                            "changed": changed,
                            "diff": ev.git_diff_text(repo, r["head_sha"], sha),
                        },
                    )
                )
            if passing and not covering and not ancestors:
                claim.add(
                    Evidence(
                        source="git",
                        kind=OBSERVED,
                        relation="contradicts",
                        summary="No passing reviewed head is the deployed commit or one of its "
                        "ancestors",
                        ref=repo,
                    )
                )
            for d, r in mine:
                claim.add(
                    Evidence(
                        source="ats record",
                        kind=SELF_REPORTED,
                        relation="context",
                        summary=f"Review {_short(d['id'])} range {_short(r.get('base_sha'))}.."
                        f"{_short(r['head_sha'])}: {d.get('verdict_code') or '?'}",
                    )
                )
            claims.append(claim)
        return claims

    def test_claims(self) -> list[Claim]:
        claims = []
        runs = self.all_test_runs()
        tests = self.verified_by.get("tests") or {}
        for key, text in tests.items() if isinstance(tests, dict) else []:
            text = str(text)
            counts = claimed_counts(text)
            files = claimed_test_files(text)
            claim = Claim(
                id=f"test:{key}",
                kind="test result",
                title=f"Test claim '{key}'",
                statement=safe_text(text, 600),
                claimant="ticket verified_by.tests (written when the ticket was closed)",
                made_at=self.verified_by.get("verified_at"),
                source_record=f"ticket/{self.ticket_id}.verified_by.tests.{key}",
                meaning="VERIFIED means harness-recorded executions in this ticket's sessions "
                "printed EVERY count the claim states (and ran the named test file, when one is "
                "named), with no failure the claim omits, and no later run of those tests failed. "
                "Output an agent merely printed from a file is shown, but cannot verify.",
            )
            if not counts:
                claim.missing.append("The claim states no test count that an execution could match")
            executed: list[tuple[ev.ToolRun, set]] = []
            printed: list[tuple[ev.ToolRun, set]] = []
            failing: list[tuple[ev.ToolRun, str]] = []
            unfinished: list[tuple[ev.ToolRun, str]] = []
            clean_passes: list[ev.ToolRun] = []
            for run in runs:
                hit = run_matches(run, counts, files) if counts else set()
                names_file = bool(files) and any(n in c for c in run.commands for n in files)
                if not (hit or names_file):
                    continue
                outcome, why = run_outcome(run, counts) if run.executed else ("ok", "")
                if outcome == "fail":
                    failing.append((run, why))
                elif outcome == "incomplete":
                    unfinished.append((run, why))
                else:
                    if passed_cleanly(run, counts):
                        clean_passes.append(run)
                    if hit:
                        (executed if run.executed else printed).append((run, hit))
            # A failure stands unless a clean passing run of the same tests came after it
            # (a red run before the fix is history, not a contradiction).
            # Chronology is per named file: a later pass of test_a does not repair a
            # newer failure of test_b. A claim naming no file is one group.
            epoch = datetime.min.replace(tzinfo=timezone.utc)

            def groups(run: ev.ToolRun) -> list[str]:
                return [n for n in sorted(files) if any(n in c for c in run.commands)] or ["*"]

            last_pass: dict[str, datetime] = {}
            for r in clean_passes:
                for key in groups(r):
                    last_pass[key] = max(last_pass.get(key, epoch), parse_time(r.at) or epoch)
            broken = False  # some file's failure came after that file's last clean pass
            for run, why in failing:
                when = parse_time(run.at) or epoch
                later = any(k not in last_pass or when > last_pass[k] for k in groups(run))
                broken = broken or later
                claim.add(
                    Evidence(
                        source=run.source,
                        kind=OBSERVED,
                        relation="contradicts" if later else "context",
                        summary=(
                            f"A recorded run of these tests failed ({why}): "
                            if later
                            else f"An earlier run failed ({why}); a later run passed: "
                        )
                        + _run_line(run),
                        at=run.at,
                        ref=run.raw_ref,
                        detail=_run_detail(run),
                    )
                )
            for run, why in unfinished:
                claim.add(
                    Evidence(
                        source=run.source,
                        kind=OBSERVED,
                        relation="context",
                        summary=f"A run that did not complete ({why}) shows nothing either way: "
                        + _run_line(run),
                        at=run.at,
                        ref=run.raw_ref,
                        detail=_run_detail(run),
                    )
                )
            listed = {id(r) for r, _ in [*failing, *unfinished, *executed, *printed]}
            covered = set().union(*(hit for _, hit in executed)) if executed else set()
            ran_files = {
                name for name in files for run, _ in executed for c in run.commands if name in c
            }
            complete = bool(counts) and covered == counts and ran_files == files
            for run, hit in executed:
                claim.add(
                    Evidence(
                        source=run.source,
                        kind=OBSERVED,
                        relation="supports" if complete and not broken else "context",
                        summary=(
                            "Earlier passing run, superseded by the later failure: "
                            if broken
                            else "Recorded execution: " if complete else "Partial match: "
                        )
                        + _run_line(run),
                        at=run.at,
                        ref=run.raw_ref,
                        detail={**_run_detail(run), "matched": sorted(f"{n} {o}" for o, n in hit)},
                    )
                )
            for run, hit in printed:
                claim.add(
                    Evidence(
                        source=run.source,
                        kind=OBSERVED,
                        relation="context",
                        summary="Printed from earlier output, not executed in this call: "
                        + _run_line(run),
                        at=run.at,
                        ref=run.raw_ref,
                        detail={**_run_detail(run), "matched": sorted(f"{n} {o}" for o, n in hit)},
                    )
                )
            lacking = sorted(f"{n} {o}" for o, n in counts - covered)
            if counts and lacking:
                claim.missing.append(
                    "No harness-recorded execution in this ticket's sessions printed "
                    + ", ".join(lacking)
                    + (f" for {', '.join(sorted(files))}" if files else "")
                    + (" (the matching output was only printed from a file)" if printed else "")
                )
            unrun = sorted(files - ran_files)
            if counts and unrun:
                claim.missing.append(
                    "No matching execution ran the named file(s): " + ", ".join(unrun)
                )
            if files and not executed and not printed:
                for run in [
                    r
                    for r in runs
                    if id(r) not in listed and any(n in c for c in r.commands for n in files)
                ]:
                    claim.add(
                        Evidence(
                            source=run.source,
                            kind=OBSERVED,
                            relation="context",
                            summary="Recorded run of the named file: " + _run_line(run),
                            at=run.at,
                            ref=run.raw_ref,
                            detail=_run_detail(run),
                        )
                    )
            claims.append(claim)
        return claims

    def completion_claim(self) -> list[Claim]:
        if not self.task:
            return []
        status = self.task.get("status")
        claim = Claim(
            id=f"completion:{self.ticket_id}",
            kind="completion",
            title=f"Ticket #{self.ticket_id} is complete",
            statement=f"Status '{status}'; verified_at {self.verified_by.get('verified_at')}. "
            f"Acceptance criteria are listed below.",
            claimant="Ticket record (status + verified_by, written by the closing session)",
            made_at=self.verified_by.get("verified_at") or self.task.get("updated_at"),
            source_record=f"ticket/{self.ticket_id}",
            meaning="CONTRADICTED here means the closing record itself admits an acceptance "
            "criterion was not met. Criteria without a probe stay UNVERIFIED.",
        )
        criteria = acceptance_criteria(self.task.get("description") or "")
        residuals = [str(r) for r in self.verified_by.get("residuals") or []]
        for criterion in criteria:
            entry = {
                "criterion": criterion,
                "status": UNVERIFIED,
                "evidence": "no independent probe",
            }
            for residual in residuals:
                shared = _stems(criterion) & _stems(residual)
                if _NEGATION.search(residual) and len(shared) >= 2:
                    entry = {
                        "criterion": criterion,
                        "status": CONTRADICTED,
                        "evidence": f"closing record: '{residual}' (linked by shared words: "
                        f"{', '.join(sorted(shared))})",
                    }
                    claim.add(
                        Evidence(
                            source="ticket",
                            kind=SELF_REPORTED,
                            relation="contradicts",
                            admission=True,
                            summary=f"Criterion '{criterion}' -- the closing record itself says: "
                            f"'{residual}'",
                            ref=f"ticket/{self.ticket_id}.verified_by.residuals",
                            detail={"linked_by": sorted(shared)},
                        )
                    )
            claim.requirements.append(entry)
        if not criteria:
            claim.missing.append("The ticket states no parseable ACCEPTANCE criteria")
        unprobed = sum(1 for r in claim.requirements if r["status"] == UNVERIFIED)
        if unprobed:
            claim.missing.append(f"{unprobed} acceptance criteria have no independent probe")
        return [claim]

    def acceptance_claim(self) -> list[Claim]:
        accepted = self.verified_by.get("accepted_by")
        if not accepted:
            return []
        claim = Claim(
            id=f"acceptance:{self.ticket_id}",
            kind="operator acceptance",
            title="Operator acceptance -- inspect the candidate turn",
            statement=str(accepted),
            claimant="ticket verified_by.accepted_by (written by the closing session)",
            made_at=self.verified_by.get("verified_at"),
            source_record=f"ticket/{self.ticket_id}.verified_by.accepted_by",
            meaning="Operator acceptance is never verified automatically: matching quoted text "
            "cannot tell your decision from a negation, a quote, or agent text you pasted. "
            "Candidate turns from this ticket's sessions are shown for you to read; the verdict "
            "stays UNVERIFIED.",
        )
        claim.missing.append(
            "Operator acceptance is never verified automatically: read the candidate turn(s) "
            "below and decide"
        )
        quoted = re.findall(r"'([^']{6,})'|\"([^\"]{6,})\"", str(accepted))
        needle = next((a or b for a, b in quoted), "")
        authority = self.verified_by.get("authority") or {}
        if authority:
            claim.add(
                Evidence(
                    source="ticket",
                    kind=SELF_REPORTED,
                    relation="context",
                    summary=f"Close stamp: source {authority.get('source')}, caller_uid "
                    f"{authority.get('caller_uid')}. The operator and every agent run as the same "
                    f"uid, so this stamp cannot tell who closed the ticket",
                    at=authority.get("checked_at"),
                )
            )
        if not needle:
            claim.missing.append("No quoted decision text to look for")
            return [claim]
        start = parse_time(self.task.get("created_at")) or datetime.now(timezone.utc) - timedelta(
            days=30
        )
        end = (
            parse_time(self.verified_by.get("verified_at")) or datetime.now(timezone.utc)
        ) + timedelta(days=1)
        hits = []
        seen: set[str] = set()
        for row in self.sessions.values():
            main, _ = ev.claude_transcripts(self.config.home, row.get("agent") or "")
            for path in main:
                if path in seen:
                    continue
                seen.add(path)
                hits += ev.operator_turns(path, start, end, needle)
        for hit in hits:
            notes = []
            if hit["excerpt"].lstrip().startswith("<pasted_content"):
                notes.append("pasted text, whose author the transcript does not record")
            if _NEGATING.search(hit["excerpt"]):
                notes.append("contains negating words")
            claim.add(
                Evidence(
                    source="claude transcript",
                    kind=OBSERVED,
                    relation="context",
                    summary=f"Candidate operator turn at {hit['at']} contains '{needle}'"
                    + (f" ({'; '.join(notes)})" if notes else "")
                    + " -- for your inspection, not proof of approval",
                    at=hit["at"],
                    ref=hit["ref"],
                    detail={"excerpt": hit["excerpt"]},
                )
            )
        if not hits:
            claim.missing.append(
                f"No operator-typed turn containing '{needle}' was found in this ticket's Claude "
                "sessions"
            )
        return [claim]

    # ------------------------------------------------------------ graph

    def graph(self, claims: dict[str, Claim]) -> dict[str, Any]:
        nodes = []
        for row in self.sessions.values():
            agent = row.get("agent") or ""
            observed = None
            delegation = next(
                (d for d in self.ats["delegations"] if d.get("child_session_id") == row["id"]), None
            )
            if delegation:
                found = self.codex.get(delegation["id"]) or []
                if found:
                    observed = f"codex transcript: {found[0].originator}, model {found[0].model}"
            elif agent.startswith("claude-code:"):
                main, _ = ev.claude_transcripts(self.config.home, agent)
                if main:
                    observed = "claude transcript present"
            nodes.append(
                {
                    "id": row["id"],
                    "agent": agent,
                    "started_at": iso(parse_time(row.get("started_at"))),
                    "completed_at": iso(parse_time(row.get("completed_at"))),
                    "ticket_link": (
                        "direct" if row.get("ticket_id") or row.get("task_id") else "via delegation"
                    ),
                    "observed_runtime": observed or "UNOBSERVED",
                    "summary": safe_text(row.get("description"), 160),
                }
            )
        edges = []
        for d in self.ats["delegations"]:
            claim = claims.get(f"delegation:{d['id']}")
            edges.append(
                {
                    "kind": "delegation",
                    "from": d["parent_session_id"],
                    "to": d.get("child_session_id"),
                    "label": f"{d['mode']} {d.get('verdict_code') or d['state']}",
                    "claim_id": claim.id if claim else None,
                    "verdict": claim.verdict if claim else None,
                    "at": iso(parse_time(d.get("created_at"))),
                }
            )
        for h in self.ats["handoffs"]:
            history = _jsonish(h.get("delivery_history"), []) or []
            matched, _ = self._handoff_receipt(h)
            receipt = {
                "stored": bool(matched and matched.get("stored")),
                "job_status": (matched or {}).get("job_status"),
                "finished_at": (matched or {}).get("finished_at"),
            }
            claim = claims.get(f"handoff:{h['id']}")
            edges.append(
                {
                    "kind": "handoff",
                    "from": h["source_session_id"],
                    "to": (
                        None
                        if h.get("addressing_mode") == "ticket"
                        else h.get("recipient_session_id")
                    ),
                    "mailbox": (
                        f"ticket #{self.ticket_id}"
                        if h.get("addressing_mode") == "ticket"
                        else None
                    ),
                    "label": safe_text(h.get("verdict"), 120),
                    "at": iso(parse_time(h.get("created_at"))),
                    "delivered": [
                        {
                            "action": e.get("action"),
                            "to": e.get("recipient_session_id"),
                            "reason": e.get("reason"),
                            "at": e.get("at"),
                        }
                        for e in history
                        if isinstance(e, dict)
                    ],
                    "acknowledged_at": iso(parse_time(h.get("acknowledged_at"))),
                    "intake_receipt": receipt,
                    "claim_id": claim.id if claim else None,
                    "verdict": claim.verdict if claim else None,
                }
            )
        return {"nodes": nodes, "edges": edges}

    def _receipts(self, session_id: str) -> list[dict[str, Any]]:
        cache: dict[str, list[dict[str, Any]]] = self.__dict__.setdefault("_receipt_cache", {})
        if session_id not in cache:
            cache[session_id] = self._intake_receipts(session_id)
        return cache[session_id]

    def _handoff_receipt(self, h: dict) -> tuple[dict[str, Any] | None, str]:
        """The intake receipt for THIS handoff, or (None, why not).

        The intake receipt names the ATS completion event it ingested; ATS's outbox
        records which handoff that event carried. Only that shared identifier ties a
        receipt to one handoff: session and time alone are an ambiguous correlation.
        """
        receipts = self._receipts(h["source_session_id"])
        if not receipts:
            return None, "No intake receipt exists for this session"
        carried = {
            e["event_id"]: e.get("handoff_id") for e in self.ats.get("completion_events") or []
        }
        for receipt in receipts:
            if receipt.get("event_id") and carried.get(receipt["event_id"]) == h["id"]:
                return receipt, ""
        return None, (
            "The intake service has a receipt for this session, but no ATS completion event ties it to this "
            "specific handoff; an ambiguous correlation is not proof"
        )

    def handoff_claims(self) -> list[Claim]:
        claims = []
        for h in self.ats["handoffs"]:
            source = self.sessions.get(h["source_session_id"], {})
            claim = Claim(
                id=f"handoff:{h['id']}",
                kind="handoff delivery",
                title=f"Handoff from {source.get('agent') or _short(h['source_session_id'])} "
                f"was received: {safe_text(h.get('verdict'), 60)}",
                statement=f"Verdict: {safe_text(h.get('verdict'), 400)}\nBlockers: "
                f"{'; '.join(_jsonish(h.get('blockers'), []) or []) or 'none'}\nNext steps: "
                f"{'; '.join(_jsonish(h.get('next_steps'), []) or []) or 'none'}",
                claimant=f"session {_short(h['source_session_id'])} ({source.get('agent', '?')})",
                made_at=iso(parse_time(h.get("created_at"))),
                source_record=f"ats.db handoffs/{h['id']}",
                meaning="VERIFIED means the intake service's own receipt says it stored the handoff "
                "carried by a specific ATS completion event, and ATS's outbox records that this "
                "event carried THIS handoff. It says nothing about whether the handoff's content "
                "is true; the deployment, review and test claims check that.",
            )
            receipt, why_not = self._handoff_receipt(h)
            if receipt and receipt.get("stored"):
                claim.add(
                    Evidence(
                        source="intake receipt",
                        kind=OBSERVED,
                        relation="supports",
                        summary=f"The intake service stored the handoff carried by completion event "
                        f"{receipt.get('event_id')}, which ATS records as carrying this handoff; "
                        f"intake job {receipt.get('job_status')} at {receipt.get('finished_at')}",
                        at=receipt.get("finished_at"),
                        ref=self.config.receipt_endpoint(h["source_session_id"]),
                        detail={"event_id": receipt.get("event_id"), "handoff_id": h["id"]},
                    )
                )
            elif receipt:
                claim.missing.append(
                    "The intake receipt for this handoff's completion event says it was not stored"
                )
            else:
                claim.missing.append(why_not)
            history = _jsonish(h.get("delivery_history"), []) or []
            addressed = (
                f"ticket #{self.ticket_id} mailbox"
                if h.get("addressing_mode") == "ticket"
                else f"session {_short(h.get('recipient_session_id'))}"
            )
            claim.add(
                Evidence(
                    source="ats record",
                    kind=SELF_REPORTED,
                    relation="context",
                    summary=f"Written to {addressed}; {len(history)} later assignment(s); "
                    + (
                        f"acknowledged {iso(parse_time(h.get('acknowledged_at')))}"
                        if h.get("acknowledged_at")
                        else "never acknowledged"
                    ),
                    detail={"delivery_history": history},
                )
            )
            claims.append(claim)
        return claims

    def _intake_receipts(self, session_id: str) -> list[dict[str, Any]]:
        """Every intake receipt for a session's ATS completion events."""
        endpoint = self.config.receipt_endpoint(session_id)
        if not endpoint:
            return []
        probe = ev.http_json(endpoint)
        body = probe.get("body") or {}
        jobs = body.get("queued") or [] if isinstance(body, dict) else []
        receipts = []
        for job in jobs:
            if job.get("trigger_source") != "ats_completion_event":
                continue
            result = _jsonish(job.get("result"), {}) or {}
            receipts.append(
                {
                    "stored": result.get("handoff") == "stored",
                    "job_status": job.get("status"),
                    "finished_at": job.get("finished_at"),
                    "event_id": result.get("event_id"),
                }
            )
        return receipts

    # ------------------------------------------------------------ report

    def report(self) -> dict[str, Any]:
        reviews = [decide(claim) for claim in self.review_claims()]
        review_verdicts = {claim.id.split(":", 1)[1]: claim.verdict for claim in reviews}
        claims = [
            *self.completion_claim(),
            *self.acceptance_claim(),
            *self.deployment_claims(),
            *self.coverage_claims(review_verdicts),
            *reviews,
            *self.delegation_claims(),
            *self.test_claims(),
            *self.handoff_claims(),
        ]
        for claim in claims:
            decide(claim)
        by_id = {claim.id: claim for claim in claims}
        counts: dict[str, int] = {}
        for claim in claims:
            counts[claim.verdict] = counts.get(claim.verdict, 0) + 1
        return {
            "ticket": {
                "id": self.ticket_id,
                "title": safe_text(self.task.get("title"), 300),
                "status": self.task.get("status"),
                "gate": self.task.get("gate"),
                "source": self.task_state,
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "legend": LEGEND,
            "counts": counts,
            "claims": [asdict(claim) for claim in claims],
            "graph": self.graph(by_id),
            "records": {key: len(value) for key, value in self.ats.items()},
        }


def ticket_report(ticket_id: int, config: EvidenceConfig | None = None) -> dict[str, Any]:
    return TicketVerifier(ticket_id, config or EvidenceConfig.from_environment()).report()
