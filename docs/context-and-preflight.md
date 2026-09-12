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
| `CLEAR` | prior material exists and none of it records this action failing |
| `CAUTION` | something relevant exists but does not settle it, or a prior failure is offset by a cited change since |
| `STRONG_WARNING` | this was already attempted and failed, or a ruling applies, and nothing relevant has changed |
| `INSUFFICIENT_EVIDENCE` | nothing inspectable was found; treat the work as new |

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
