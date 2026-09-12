"""A delegated worker inherits the task's authority, and both workers get the same.

The gap this closes, measured 2026-09-12 (ATS session c42fec94): given Tower
task #2649, Claude and a delegated Codex child each obtained the acceptance
criteria by reading Postgres directly, because `list_open_tasks` omits
`description` and no other surface exposed it. ATS propagated its own decision
history perfectly and omitted the current task's definition of done.

Three properties are pinned here:

  1. THE ENVELOPE REACHES THE PACKET. Description, acceptance criteria and
     prohibitions appear in the text the child is launched with.
  2. BOTH WORKERS GET THE SAME AUTHORITY. Worker-independence is structural --
     the packet is built once and handed to build_launch as an opaque string --
     so this asserts the property rather than trusting the convention.
  3. A NAMED TASK WITH NO ENVELOPE FAILS CLOSED. Launching anyway is worse than
     refusing: the packet still names the task, so the child assumes the
     constraints came with it.
"""

from __future__ import annotations

import pytest

from ai_team_sync.briefs import render_task_envelope
from ai_team_sync.delegation_packet import build_child_packet
from ai_team_sync.delegation import READ_ONLY, VERIFY
from ai_team_sync.launch_spec import build_launch

# Shaped like the real #2649 envelope, including the clauses that matter:
# an ACCEPTANCE block and an explicit prohibition.
ENVELOPE = {
    "envelope_version": "1",
    "id": 2649,
    "task_key": "executor-related-tests-substring-match-p01",
    "project_id": 20,
    "project_name": "Echo Brain Agent Ensemble",
    "parent_id": None,
    "title": "_find_related_tests matches the module stem as a bare substring",
    "description": (
        "FILE: src/autonomous/workers/tower_task_executor_worker.py\n"
        "FUNCTION: _find_related_tests\n"
        "\n"
        "ACCEPTANCE:\n"
        '- for stem "cache", a file whose only match is "cached" is NOT selected\n'
        '- for stem "cache", tests/unit/test_cache.py IS selected\n'
        "\n"
        "Do not change MAX_TARGETED_TEST_FILES, do not change _run_targeted_tests, "
        "and do not touch any other function."
    ),
    "status": "pending",
    "gate": "none",
    "priority": 3,
    "recommendation": "",
    "notes": "",
    "blocked_by": [],
    "verified_by": None,
    "claim": None,
    "is_closed": False,
}

DELEGATION = {
    "id": "82fb4676-471a-4cb3-8d46-8187ef581d1c",
    "parent_task": "2649",
    "delegating_worker": "claude-code:6a76804f",
    "prohibitions": ["file_write", "git_commit", "parent_task_close"],
}

REPO = "/opt/tower-echo-brain"


def _packet(**over):
    kw = dict(mode=READ_ONLY, delegation=DELEGATION, objective="inspect one function",
              acceptance="a file:line citation",
              task_envelope_text=render_task_envelope(ENVELOPE),
              brief="PRIOR DECISIONS\n  [INFERRED] something older")
    kw.update(over)
    return build_child_packet(**kw)


# ── 1. the envelope reaches the packet ───────────────────────────────────────

def test_packet_carries_the_task_description_and_acceptance_criteria():
    packet = _packet()
    assert "TOWER TASK #2649" in packet
    assert "executor-related-tests-substring-match-p01" in packet
    assert "ACCEPTANCE:" in packet
    assert 'tests/unit/test_cache.py IS selected' in packet


def test_packet_carries_the_explicit_prohibitions():
    """The clause a worker is most likely to violate if it never sees it."""
    packet = _packet()
    assert "Do not change MAX_TARGETED_TEST_FILES" in packet
    assert "do not touch any other function" in packet


def test_task_authority_precedes_the_objective():
    """A worker that reads the objective first starts solving. The criteria it
    will be judged against must not arrive after it has chosen an approach."""
    packet = _packet()
    assert packet.index("TASK AUTHORITY") < packet.index("OBJECTIVE")


def test_envelope_and_brief_stay_separate_blocks():
    """Prior-session reading must never be mistaken for current task authority."""
    packet = _packet()
    assert "TASK AUTHORITY" in packet
    assert "PRIOR DECISIONS" in packet
    assert packet.index("TASK AUTHORITY") < packet.index("PRIOR DECISIONS")


def test_a_conflict_between_authority_and_objective_must_be_surfaced():
    packet = _packet()
    assert "disagree, say so" in packet


def test_a_closed_task_warns_before_any_work():
    """The #2649 shape: shipped code, ticket still open. A worker must see the
    closure evidence before it starts re-implementing."""
    closed = dict(ENVELOPE, status="completed", is_closed=True,
                  verified_by={"commit": "128483f5", "tests": "21 passed"})
    packet = _packet(task_envelope_text=render_task_envelope(closed))
    assert "ALREADY CLOSED" in packet
    assert "128483f5" in packet


def test_an_operator_ruling_on_the_task_reaches_the_child():
    ruled = dict(ENVELOPE, recommendation="OPERATOR DECISION: do not implement Option D.")
    packet = _packet(task_envelope_text=render_task_envelope(ruled))
    assert "OPERATOR RULING" in packet
    assert "do not implement Option D" in packet


# ── 2. both workers receive the SAME authority ───────────────────────────────

@pytest.mark.parametrize("worker,binary", [("claude-code", "/usr/local/bin/claude"),
                                           ("codex", "/usr/bin/codex")])
def test_every_worker_is_launched_with_the_same_task_authority(worker, binary):
    """Worker-independence, asserted rather than assumed: the packet is built
    once and passed to build_launch as an opaque string."""
    packet = _packet()
    which = lambda name: {"claude": "/usr/local/bin/claude",
                          "codex": "/usr/bin/codex"}.get(name)
    launch = build_launch(worker, READ_ONLY, packet, repo=REPO, which=which)

    assert launch.resolved_binary == binary
    delivered = [a for a in launch.argv if "TOWER TASK #2649" in a]
    assert len(delivered) == 1, "the packet must reach the child exactly once"
    assert "ACCEPTANCE:" in delivered[0]
    assert "Do not change MAX_TARGETED_TEST_FILES" in delivered[0]


def test_claude_and_codex_receive_byte_identical_packets():
    packet = _packet()
    which = lambda name: {"claude": "/c", "codex": "/x"}.get(name)
    c = build_launch("claude-code", VERIFY, packet, repo=REPO, which=which)
    x = build_launch("codex", VERIFY, packet, repo=REPO, which=which)

    c_packet = [a for a in c.argv if a.startswith("DELEGATED TASK")]
    x_packet = [a for a in x.argv if a.startswith("DELEGATED TASK")]
    assert c_packet == x_packet != []


# ── 3. no authority means no launch ──────────────────────────────────────────

def test_without_a_task_the_packet_has_no_authority_block_and_no_conflict_clause():
    """Delegating with no --task is legitimate and must not grow a phantom
    'TASK AUTHORITY' header with nothing under it."""
    packet = _packet(task_envelope_text="",
                     delegation=dict(DELEGATION, parent_task=""))
    assert "TASK AUTHORITY" not in packet
    assert "disagree, say so" not in packet
    assert "OBJECTIVE" in packet


def test_fetch_returns_no_partial_envelope_on_failure():
    """The fetch contract: ("", reason), never half an envelope."""
    from ai_team_sync.briefs import fetch_tower_task_envelope

    text, err = fetch_tower_task_envelope("not-a-number")
    assert text == "" and err and "numeric" in err

    # Echo unreachable: a bad base URL must degrade to a refusal, not an exception.
    import ai_team_sync.briefs as briefs
    original = briefs.ECHO_URL
    briefs.ECHO_URL = "http://127.0.0.1:9"        # discard port, nothing listens
    try:
        text, err = fetch_tower_task_envelope(2649, timeout=0.25)
    finally:
        briefs.ECHO_URL = original
    assert text == "" and err and "unreachable" in err
