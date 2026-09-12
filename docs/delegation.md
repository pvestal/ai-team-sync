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
| `READ_ONLY` | read, investigate, report | write files, commit, restart services, submit expensive jobs, mutate task state, delegate onward |
| `IMPLEMENT` | write inside the declared scope, commit, run tests | restart services, submit expensive jobs, close the parent's task, delegate onward |
| `VERIFY` | read, run tests | write files, commit, restart services, close the parent's task, delegate onward |

Three things hold a mode in place. The server refuses a scope claim from a child
whose mode grants no edit authority. The launcher applies the harness's own
restrictions to the child process, so the prohibition survives the child
ignoring its instructions. And the record states the prohibitions plainly, so a
reader can check what was and was not permitted.

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
| submit the result | the child session |
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

## What the child receives

A freshly built packet: the objective, the acceptance criteria, its
prohibitions, and the context for the scope. It does **not** inherit the
parent's conversation. One worker's intermediate reasoning should not
contaminate the next, and the exchange stays reproducible from the record.

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
