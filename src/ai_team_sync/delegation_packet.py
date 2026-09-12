"""The packet a delegated child receives: its objective, its limits, its authority.

WHY THIS IS ITS OWN MODULE
--------------------------
Packet assembly used to be an inline join inside `cli.delegate`, wrapped in
four HTTP calls, so the only way to find out what a child actually received was
to read the argv of a running process. It was tested by nobody. The one thing
this system most needs to be sure of -- that a worker was told the constraints
before it acted -- was the one thing with no test.

Pulled out here so the packet is a value that can be asserted on, and so the
same text demonstrably reaches every worker. `build_launch` takes the packet as
an opaque string, so Claude and Codex receive byte-identical authority; that is
a property a test can hold, not a convention to remember.

TWO KINDS OF AUTHORITY, DELIBERATELY NOT MERGED
-----------------------------------------------
  TOWER TASK ENVELOPE — what THIS task is and what done means. Current, binding,
      fetched fresh per delegation from Echo Brain.
  BRIEF — what has been tried, decided and ruled BEFORE. Historical, carrying
      its own provenance per line (OBSERVATION / INFERRED / VERIFIED /
      OPERATOR_DECISION).

They are additive and are rendered as separate blocks. Collapsing them would
let a prior session's reading be mistaken for the current task's definition of
done, which is the exact confusion the provenance labels exist to prevent.

The envelope is placed BEFORE the objective on purpose: a worker that reads the
objective first will start solving, and the acceptance criteria it must be
judged against should not arrive after it has already chosen an approach.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


class MissingTaskAuthority(RuntimeError):
    """A task was named by explicit id and its envelope could not be fetched.

    Fails the delegation rather than launching. A child that was told it is
    working task #2649 but never received #2649's acceptance criteria will
    reconstruct them, and reconstructed authority is the failure this whole
    path exists to end. Launching without it would be silently worse than
    launching without a task id at all, because the packet still NAMES the task
    and the child reasonably assumes the constraints came with it.
    """


def build_child_packet(*, mode: str, delegation: Mapping[str, Any],
                       objective: str, acceptance: str,
                       scope: Sequence[str] = (),
                       task_envelope_text: str = "",
                       brief: str = "") -> str:
    """The full packet text for a delegated child.

    Worker-independent by construction: nothing here branches on who will run
    it. That is what makes "Claude and Codex receive the same authority" a
    structural property rather than a promise.
    """
    parent_task = delegation.get("parent_task") or ""
    lines: list[str] = [
        f"DELEGATED TASK — mode {mode}",
        f"delegation: {delegation.get('id')}",
        f"parent task: {parent_task or '(none)'} — owned by "
        f"{delegation.get('delegating_worker')}, NOT by you.",
    ]

    if task_envelope_text:
        lines += [
            "",
            "=" * 60,
            "TASK AUTHORITY — the current, binding definition of this task.",
            "This is not background. Your work is judged against the acceptance",
            "criteria below, and the prohibitions below are not negotiable.",
            "=" * 60,
            task_envelope_text,
            "=" * 60,
        ]

    lines += [
        "",
        "OBJECTIVE", f"  {objective}",
        "", "ACCEPTANCE — your answer is judged against this", f"  {acceptance}",
        "", "SCOPE", "  " + (", ".join(scope) if scope else "(none — claim nothing)"),
        "", "YOU MAY NOT",
        *[f"  - {p}" for p in (delegation.get("prohibitions") or [])],
        "", "Return findings with file:line citations. Do not report success you "
        "have not demonstrated; the parent verifies your claims independently.",
    ]

    if task_envelope_text:
        lines += [
            "",
            "If the TASK AUTHORITY above and this OBJECTIVE disagree, say so "
            "plainly and stop; do not silently pick one.",
        ]

    lines += ["", "-" * 60, brief]
    return "\n".join(lines)
