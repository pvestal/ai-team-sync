# Delegation

One worker hands a bounded subproblem to another and gets a result back. The
rule everything else follows from:

> **Delegation is not a handoff.** If you own a task and delegate part of it,
> you still own the task — while the child works, and after it returns.
> Ownership moves only by an explicit handoff, never as a side effect of a
> child finishing.

## The shape of it

```
parent session  ──delegate──▶  delegation record  ──▶  child session
     │                              │                      │
     │  keeps its locks             │  mode, scope,        │  bounded
     │  keeps the task              │  acceptance, lease   │  authority
     │                              │                      │
     ◀──────── reconcile ───────────┴──── return result ───┘
```

The parent creates the delegation, the child returns evidence, and the parent
reconciles. Each of those three is bound to a different party, by exact session
id rather than by worker name.

## Modes

A mode is a safety property, not a label. What a mode forbids is refused, not
noted.

| Mode | May do | May not |
|---|---|---|
| `READ_ONLY` | read, investigate, report, **read ATS coordination state** | write files, commit, restart services, submit expensive jobs, mutate task state, delegate onward |
| `IMPLEMENT` | write inside the declared scope, commit, run tests | restart services, submit expensive jobs, close the parent's task, delegate onward |
| `VERIFY` | read, run tests **in a disposable worktree**, **read ATS coordination state** | write to the lead tree, commit anything authoritative, push, deploy, restart services, close the parent's task, delegate onward |

Three things hold a mode in place. The server refuses a scope claim from a child
whose mode grants no edit authority. The launcher applies **that worker's own**
restrictions to the child process, so the prohibition survives the child
ignoring its instructions. And the record states the prohibitions plainly, so a
reader can check what was and was not permitted.

Enforcement is per worker, not one flag set for everybody. Codex is launched with
`--sandbox read-only`, which its own runtime enforces; Claude READ_ONLY with a
built-in allow-list and a per-tool MCP policy, and Claude VERIFY with that policy
plus a shell, an OS sandbox and a disposable worktree (both below). Handing Claude's flags to
Codex would mean nothing to it, so READ_ONLY would quietly decay from a
restriction into a request. A worker/mode pair with no enforcement mapping
**fails closed before anything is spawned** — it is refused, not run
unrestricted.

## READ_ONLY bounds the work product, not the coordination plane

Operator ruling 2026-09-12. **READ_ONLY means the child cannot mutate the work
product or Tower Task authority state.** It has never meant the child is expelled
from coordination, and conflating the two broke the delegated lifecycle itself.

Claude READ_ONLY used to be `--permission-mode plan`, which refuses *every* MCP
call including pure reads:

```
Cannot call mcp__ai-team-sync__my_authority while in plan mode.
```

A Codex-led canary hit exactly that. The child launched correctly, received the
full task envelope, did useful analysis — and then could not read its own
authority, its own session, its delegation state, or the decision history it was
sent to consult. Plan mode was also not a no-write guarantee: it writes a plan
file under `~/.claude/plans/`.

What Claude READ_ONLY carries now:

| Flag | What it does |
|---|---|
| `--tools Read,Grep,Glob` | allow-list of **built-ins**. An unlisted built-in is *absent*, not refused — and absent "in subagents as well as here", which closes the spawn-a-subagent-to-write escape. No `Bash`, so no `git commit`, no `git push`, no `echo > file`. |
| `--permission-prompts none` | anything that would prompt is denied automatically. This is what makes the policy fail-closed rather than dependent on nobody answering a prompt. |
| `--allowedTools …` | the specific coordination reads, per tool. |
| `--disallowedTools …` | explicit denial of the acts the ruling names. A **second** lock and a readable contract, never the primary mechanism — so a mutating tool nobody remembered to list is still denied by the two rows above. |

Available to a READ_ONLY child: `my_authority`, `get_session_details`,
`team_status`, `get_decision_history`, `delegation_status`, `ats_version`, and
Echo Brain's `get_tower_task` (the canonical envelope, same builder the packet is
rendered from).

Blocked: every file writer, the shell, the subagent spawner, every ATS
coordination mutation (including another worker's session), scope and authority
escalation, reconciling its own delegation, delegating onward, and Tower Task
mutation, closure or gate changes.

**Mutability was determined by inspecting handlers, not by reading tool names**,
because the names mislead in both directions. `check_locks` and `whos_editing`
are POSTs whose handlers issue no write at all. `delegate` issues no HTTP
whatsoever and shells out to `ats delegate`, which is recursive delegation — a
name-based or verb-based allow-list would have let that through.

`VERIFY` is covered in its own section below: it needs a shell, so its
containment is environmental rather than a tool list.

## VERIFY is verification-capable, non-authoritative, non-implementation

Operator ruling 2026-09-12, second coordination tranche. VERIFY had the same
plan-mode defect READ_ONLY had, and one more besides.

What plan mode actually did to VERIFY, measured rather than assumed:

- it refused every ATS and Echo Brain call, the same categorical refusal;
- and across **three** independent runs the child declined the shell writes
  *itself* rather than the harness denying them. It even declined to run the
  unmodified CI command `python3 -m pytest -q`, because pytest writes
  `__pycache__`.

So the mode whose entire purpose is to *run* verification could not run it, and
the filesystem guarantee it appeared to give was the model's own compliance. Plan
mode also left `Task`/`Agent` available, so the subagent escape was open.

### The honest model

VERIFY gets a **real shell**. A shell can write files, so no tool list makes
VERIFY filesystem-read-only, and this document does not claim otherwise. What
bounds it is two mechanisms that already existed:

| Layer | What it guarantees |
|---|---|
| Claude Code's Bash sandbox (bubblewrap + seccomp) | everything outside the working directory is read-only **at the kernel**. Measured: a write aimed at the lead tree fails with `Read-only file system`, not at a prompt and not at the model's discretion. Declared with `failIfUnavailable` so a host with no sandbox backend **refuses** rather than running the shell unconfined, and `allowUnsandboxedCommands: false` so the per-call escape hatch is rejected. |
| A disposable linked git worktree | the writable working directory, holding the lead's exact result. Side effects land there and die with it. |

VERIFY's coordination reads and its denial list are **identical** to READ_ONLY's,
deliberately: one answer to "what may a delegated child read", not two that drift.
The only capability VERIFY adds is `Bash`. A test asserts exactly that difference
as set algebra.

`Task`/`Agent` stay unavailable, so a VERIFY child cannot spawn a worker with a
different policy.

### The sandbox's writable set, and the arrangement it requires

Measured: the working directory and the **system temp root** are writable;
everything else, `$HOME` included, is read-only.

That has a sharp edge, and it is not hypothetical. The first containment
measurement put the fixture lead repository inside the temp root alongside the
worktree, and the child wrote into the lead with exit 0 and no sandbox violation,
because temp is on the writable side. So the supervisor **refuses** to launch when
the lead repository is inside the temp root, rather than launching something
contained only in appearance.

### Reviewing the actual result, provably

The lead's result is usually still uncommitted when review happens, so
`git worktree add <rev>` alone would reproduce the commit and silently drop the
work under review. The supervisor replays the tracked diff and copies untracked
files, and writes a manifest into the worktree carrying the base commit, the diff
hash, and a **per-file sha256 comparison** against the lead. That manifest is
placed in the child's packet ahead of the objective, so "you are reviewing the
lead's actual result" is something the child can check rather than trust. A diff
that does not apply cleanly is a refusal, because a partially reproduced result
would make an invalid review look valid.

### No VERIFY commit becomes authoritative

The worktree is checked out **detached**, so a commit in it advances no branch.
Teardown runs `git worktree remove --force` and prunes, leaving the commit
unreachable from any ref. `git push` needs a network the sandbox denies.

## The supervisor finalizes the child session

**The delegation launcher owns child-session finalization. The child is never
required to close itself.** Delegation correctness must not depend on a model
remembering to call `complete_session` — and under READ_ONLY it cannot, because
that call is a coordination mutation the launch spec denies.

After the child process reaches any terminal outcome — clean exit, non-zero exit,
lease expiry, a spawn that never ran, or a verification environment that was
refused — the supervisor submits the result on the child's behalf, completes
**that exact child session**, and removes the verification worktree if it built
one. Both steps are
unconditional, and the session close does not depend on the result submission
succeeding. A child that failed to start cannot clean up after itself, and a
supervisor that dies with its child leaves the orphan the contract exists to
prevent.

The parent session is never touched by any of this. Delegation is not handoff.

## Effective authority

Authority under a delegation is the **intersection** of what the worker class
already had and what the mode allows:

```
effective = min(base worker authority, delegation mode)
```

A mode can never grant what the worker lacks. Hand `IMPLEMENT` to a read-only
worker and it is still read-only — otherwise delegation becomes the way around
the worker registry rather than a bound on it.

`my_authority` reports **base**, **mode** and **effective** separately.
Collapsing them either hides the restriction or hides the reason for it.

## A child cannot widen its own bounds

- It cannot claim an edit scope its mode denies — the server refuses it.
- It cannot delegate onward. Recursive delegation is refused at depth 1,
  because a chain of workers collaborating on one bug is a chain in which
  nobody owns it.
- It cannot close its parent's task.
- It cannot reconcile its own delegation. Accepting a result is the owner
  judging evidence against acceptance criteria; a child accepting its own work
  is marking its own homework.

## Who may do what

| Act | Bound to |
|---|---|
| create the delegation | the parent session |
| submit the result | the child session — posted by the supervisor on its behalf, with `actor_session_id` naming the child |
| reconcile: accept or reject | the parent owner only; an unidentified caller is refused |
| inspect (`delegation_status`) | anyone who can reach the service |

## Acceptance criteria are required

A delegation without acceptance criteria is refused. Without them the parent
cannot judge what comes back, and "it worked" becomes the acceptance test —
which is exactly the failure mode of one worker reporting success and another
repeating it.

## Leases

A delegation carries a lease. Once it expires the delegation stops accepting
evidence: the parent re-delegates rather than accepting work of unknown age.

## Which worker actually ran

A delegation records two separate things, and only the second is evidence:

| Field | Meaning |
|---|---|
| `requested_worker` | the worker the parent asked for |
| `resolved_binary` | the absolute executable the parent resolved **at spawn** |

The child's own label cannot serve here. The parent injects `ATS_AGENT` into the
child's environment, so asking the child who it is returns the parent's text.
The binary is the one identity a child cannot influence.

The server re-derives the pairing from the worker registry before storing it, so
a record may claim a worker only if that worker's binary is what got resolved.
Requesting one worker and resolving another's is a **routing failure** that
stores nothing — not a satisfied delegation. Workers that are authority classes
rather than command-line agents (`local`, `default`, `restricted`) have no
launcher and are refused by having no spec.

## What the child receives

A freshly built packet: the objective, the acceptance criteria, its
prohibitions, and the context for the scope. It does **not** inherit the
parent's conversation. One worker's intermediate reasoning should not
contaminate the next, and the exchange stays reproducible from the record.

When the delegation names a task, the packet also carries that task's
**authority envelope**, rendered before the objective: id, key, project, title,
status, gate, priority, any claim, any operator ruling on the row, `verified_by`,
and the full description that holds the acceptance criteria and the prohibited
approaches. A closed task renders an explicit already-closed warning with its
closure evidence.

Ordering carries meaning. A worker that reads the objective first starts
solving, and should not meet the criteria it will be judged against after it has
chosen an approach. The envelope also stays a *separate block* from the context
brief: the envelope is the current binding definition of the task, the brief is
prior history carrying its own provenance per line. Merging them would let one
session's reading be mistaken for the task's definition of done.

Every worker receives the same packet. It is built once and handed to the
launcher as an opaque string, so authority is worker-independent by
construction rather than by convention.

A task named explicitly whose envelope cannot be fetched **refuses the
delegation** before any record exists. Launching anyway is worse than launching
with no task at all: the packet still names the task, so the child assumes the
constraints arrived and reconstructs them when they did not.

### Reaching ATS from inside the child

A delegated child gets its own ATS session, and it must be able to act *as
itself*. Claude inherits the environment we hand the process. Codex does not: it
starts MCP servers from its own config, and a declared `[mcp_servers.*.env]`
block **replaces** the inherited environment rather than extending it. The
isolation keys are therefore passed as per-invocation config overrides. Without
that, a child's ATS client falls back to the shared pointer and reports its
*parent's* session as its own.

## Example

```
# parent, owning some task
delegate(
  objective   = "Find every code path that can set the retry flag, with file:line",
  acceptance  = "names each path with file:line; no edits",
  mode        = "READ_ONLY",
  repo        = "/srv/my-repo",
  scope       = ["src/scheduler/**"],
)

# ... child investigates and returns evidence ...

delegation_status(delegation_id = "...")   # check what it did, and its authority
reconcile_delegation(state = "closed", verdict = "checked src/scheduler/retry.py:88 myself")
```

The parent's verdict should say what the parent verified, not repeat what the
child claimed.
