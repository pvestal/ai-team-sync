# Context and preflight

Two advisory features that help a worker start informed. Neither blocks
anything; both exist so an agent can see what is already known before spending
effort.

## Brief on claim

`start_session` returns a context packet along with the session, and
`task_brief` fetches one explicitly for any objective. A brief gathers:

- **Blockers now** — live locks overlapping your scope, and who holds them.
- **Prior decisions** — decisions recorded against this repo.
- **Prior work in this scope** — what earlier sessions reported doing.
- **Recall** — related material from a memory service, if one is configured.

### Every line carries provenance and a citation

Items are labelled by where they came from and how much weight they carry:

| Class | Means |
|---|---|
| `OPERATOR_DECISION` | a human ruling |
| `VERIFIED` | backed by an artifact recorded alongside it, such as a commit |
| `OBSERVATION` | raw evidence: a lock, a restart |
| `INFERRED` | somebody's reading of something, including a model's |

Provenance is carried, never merged. An unmarked item stays `INFERRED`: nothing
is promoted because it sounds confident, and a pending draft must remain
distinguishable from a ruling.

Every line also names something you can open. A brief you cannot check is worse
than no brief.

### It is advisory

A brief is context, not instruction. Recall is best-effort: if the memory
service is unreachable the brief still returns its live blockers and decisions
and says that recall was unavailable — absent is reported as absent, never as
"nothing found".

### The preflight hint

A brief may carry `preflight_recommended: true` with a short reason, when the
material it already gathered contains a recorded prohibition or a failed
approach in scope. It is a deterministic trigger computed from what the brief
already has: no extra query, no embedding, no model.

`false` means the cheap trigger found nothing obvious. It does **not** mean a
preflight would return CLEAR.

## Preflight

`preflight` answers one question before you spend real work: **has this exact
action already been tried, and what happened?**

Give it what you are about to do — the objective, the operation type, the repo,
the scope, and any structured specifics — and it returns a disposition:

| Disposition | Means |
|---|---|
| `ALREADY_COMPLETED` | the Tower task you named is finished; the work may already be shipped |
| `CLEAR` | prior material exists and none of it records this action failing |
| `CAUTION` | something relevant exists but does not settle it, or a prior failure is offset by a cited change since |
| `STRONG_WARNING` | this was already attempted and failed, or a ruling applies, and nothing relevant has changed |
| `INSUFFICIENT_EVIDENCE` | nothing inspectable was found; treat the work as new |

### Live task state is read first, and outranks history

Pass `task_id` and preflight fetches that Tower task's canonical envelope
**before** it gathers any evidence. The live row can then pre-empt the
historical reading:

- **closed** (completed, skipped, cancelled) → `ALREADY_COMPLETED`, carrying
  `verified_by` so you can check the closure rather than take it on trust. A
  distinct disposition rather than a warning, because nothing is prohibited: the
  work is simply already done, and `CAUTION` would bury the one fact that makes
  it unnecessary. Closed outranks the task's own gate and claim, since the first
  question is whether the work is needed at all.
- **claimed** by a live run → `CAUTION` naming the run, executor and lease. A
  terminal run is history, not ownership, and an expired or unparseable lease is
  treated as expired so a stale claim cannot park work forever.
- **gated** (`decision`, `upstream`, `gpu`, `aesthetic`, `verdict`, `verify`) →
  `CAUTION` naming the gate. `none` is the only value that means ready.
- **unknown id** → fails closed, persisting nothing. An id that resolves to
  nothing means the caller and the board disagree about what work exists, and
  answering `CLEAR` to that answers a question nobody asked.

Live authority wins when it and recall disagree, but the conflict is **shown,
never silently resolved**: the historical disposition and its reasoning are
preserved verbatim under `HISTORY ALSO SAYS`. A stale memory cannot quietly
win, and a wrong row stays visible rather than becoming authoritative.

Without `task_id` this section does not apply and history owns the answer.

### `CLEAR` does not mean the work is needed

It means nothing on record says this action failed. It is not a statement that
the work is outstanding, still wanted, or not already shipped. Measured
2026-09-12: Tower #2649 was closed with `verified_by` naming a commit already on
the deployed HEAD, and an identical objective minutes later still returned
`CLEAR` — because the task id was accepted but the task row was never read. That
is what the live-state check above now prevents, *when a task id is given*.

### Authority must cover the FINAL effective operation

Where downstream layers mutate a request after preflight, authority has to reach
the operation that is actually performed, not the intent that was checked
upstream. A preflight that passed on one request proves nothing about a
different request the production path assembled afterwards.

For Anime Studio that means the assembled generation contract immediately before
GPU submission: the prompt, the method, the identity bindings, the framing and
pose contract, and the relevant generation parameters. Upstream preflight
remains useful, and is not sufficient proof that the final submission respected
authority.

### Evidence authority is a class, not a score

Evidence is ordered by the kind of thing it is, never by a blended number:

1. operator decisions
2. verified or adjudicated engineering evidence
3. pending or inferred facts
4. prior session findings
5. semantic similarity

A similar-sounding memory cannot outrank an explicit ruling however similar it
reads. Semantic ranking orders items *within* its own class and nowhere else.

### A prior failure is not a permanent veto

Preflight checks whether the thing that failed has been touched since it failed,
and cites the change. "We tried it once" should not freeze a system forever, so
a prior failure plus a cited material change is `CAUTION` and an argument for
retrying, not a refusal.

### It is advisory, and unavailable is not CLEAR

Preflight changes nothing: no task created or closed, no file edited, no work
claimed, no job submitted. The caller decides what to do with a warning.

If the backing service is unreachable the tool says so explicitly and does
**not** return `CLEAR`. Answering "go ahead" because the thing that would have
warned you is down is the worst available default.

### Warnings are citation-backed

Every material warning names something openable — a fact id, a decision, a
session, a commit. Evidence that cannot be inspected is discarded rather than
shown, because an unsourceable warning is indistinguishable from a guess.

## Example

```
start_session(scope=["src/scheduler/**"], repo_root="/srv/my-repo", description="...")
# → brief, possibly with preflight_recommended: true

preflight(
  objective       = "retry the batch reprocessing path for oversized payloads",
  operation_type  = "test",
  repo_root       = "/srv/my-repo",
  scope           = ["src/scheduler/**"],
)
# → STRONG_WARNING, with the citations behind it
```
