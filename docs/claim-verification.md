# Claim verification (`tower-console --verify-ticket` / `--serve-verify`)

The verification view lists every claim recorded about a ticket and gives each
one verdict. This page states what each verdict REQUIRES, per claim type, as
implemented. The acceptance suite `tests/test_claim_acceptance_3589.py` exercises
every rule below; run `python tests/test_claim_acceptance_3589.py` for the
full input / expected / actual table.

## Verdicts

| Verdict | Meaning |
|---|---|
| VERIFIED | Independently observed evidence establishes the claim, and nothing observed contradicts it or shows it went stale. |
| CONTRADICTED | Observed evidence, or the claimant's own record (an admission), is inconsistent with the claim. |
| STALE | The claim held for an earlier state, but what it describes has changed since. |
| UNVERIFIED | Only self-reported records support it, or the evidence is missing. |

Precedence is CONTRADICTED > STALE > VERIFIED > UNVERIFIED, applied in one
place (`verify.decide`). Contradicting evidence is never dropped by a builder.

**Observed** sources: git objects and refs, installed file bytes, systemd
state, a running service's own HTTP answer, an intake service's receipts, a
migration ledger, and transcripts written by the Claude Code and Codex
harnesses. **Self-reported** sources: ATS rows, ticket text and `verified_by`,
agent messages, reviewer verdict labels. Self-reported evidence never makes a
claim VERIFIED; it can make one CONTRADICTED only as an admission against
itself.

## Requirements by claim type

### Review (`review:<delegation>`)
VERIFIED requires ONE Codex transcript, bound to this delegation by the
delegation id in its own opening prompt, that shows BOTH:
1. a successful `git diff`, `git log` or `git rev-list` naming the reviewed
   base before the reviewed head (head..base is a different range).
   "Successful" means a recorded exit code of 0, or, when Codex records no
   exit code, git's own diff/log output in the result. When `||`, a pipe or
   `;` follows git, the exit code is not git's and git's output is required.
   Any git `fatal:` in the call's output rejects it. A run in a different
   existing repository does not count; and
2. a final answer (`phase=final_answer`; legacy transcripts without phases:
   the last reply) whose overall verdict -- the verdict stated LAST, from an
   `ats-verdict` block or a `VERDICT:` line -- equals the recorded verdict.

CONTRADICTED: the reviewer's own final verdict differs from the record, or the
claimed base/head does not exist. A reviewer still running (commentary only)
has no verdict. VERIFIED does not mean the code is correct; the reviewer's
commands, tests and final message are shown for the operator to read.

### Review coverage (`coverage:<target>`)
VERIFIED requires a PASSING review whose own claim is VERIFIED and whose head
is exactly the deployed commit. STALE: the deployed commit descends from a
VERIFIED passing review's head with changes no review examined (the diff is
shown). CONTRADICTED: the only review of exactly the deployed commit is not
passing.

### Deployment (`deploy:<target>`)
VERIFIED requires every part observed:
- **code**: every file of the package at the commit (code and package data)
  byte-identical in the install, or the live checkout's HEAD exactly the
  commit with no modified tracked files;
- **each unit**: active, with a live PID, started after the code landed
  (install file times; for a checkout, when HEAD moved there per the reflog,
  not the commit's own date);
- **probe**: the running service answers its endpoint. Where the service
  reports its build (ATS `/api/version`), the commit must match and its
  `package_path` must be the install compared. A probe that only answers
  health shows liveness only.

Any part missing keeps the claim UNVERIFIED. CONTRADICTED: files differ or are
missing, a unit is not running, the probe does not answer, or the service
reports another build or package path. STALE: the install or checkout now
holds a later commit, or a unit started before the code landed. Feature
behavior is NOT tested.

### Migration (`migration:<target>:<id>`)
VERIFIED requires BOTH: the migration file at the deployed commit hashes as
claimed, AND the database's migration ledger records the id. Either alone --
including a ledger that cannot be read, or a target with no ledger
configured -- stays UNVERIFIED. CONTRADICTED:
the file is missing or hashes otherwise, or the ledger has no row. A ledger
row says the migration was recorded as applied; it does not show that the
migration's intended behavior works.

### Test result (`test:<key>`)
VERIFIED requires harness-recorded executions in this ticket's sessions (the
Claude session's own transcript inside its active window, or a bound Codex
transcript) that printed EVERY count the claim states and ran every test file
it names, with no failure the claim omits and no later failing run of the same
file. Output merely printed from a file (`cat`, `grep`, ...) is shown but
cannot verify. pytest exits 2-5 (never completed) support nothing either way.
CONTRADICTED: a run printed failures the claim omits, or exited 1 with no
failure printed, after that file's last clean pass. An earlier failure
followed by a clean pass (a red run before a fix) is shown as history.

### Delegation (`delegation:<id>`)
VERIFIED requires a Codex transcript bound by its own opening prompt, written
by the Codex runtime, started inside the delegation window, that ran at least
one command and gave a final answer. It does not mean the objective was met:
review and test claims judge that. CONTRADICTED: the bound transcript is
outside the window or from another runtime, or ATS records a return with no
result. Delegations to Claude workers have no transcript binding and stay
UNVERIFIED.

### Handoff delivery (`handoff:<id>`)
VERIFIED requires the intake service's own receipt to name an ATS completion event
and say it stored that event's handoff, AND ATS's completion outbox to record
that this event carried THIS handoff (the shared identifier is the event id;
the outbox payload names the handoff id). A receipt that cannot be tied to the
specific handoff -- same session, same second, but no outbox link -- is an
ambiguous correlation and stays UNVERIFIED. It says nothing about whether the
handoff's content is true.

### Operator acceptance (`acceptance:<ticket>`)
Never VERIFIED. Matching quoted text cannot tell the operator's decision from a
negation ("this is NOT approved"), a quote, or agent text the operator pasted.
Candidate turns -- typed turns in main (not subagent) transcripts of sessions
working this ticket, inside the ticket's lifetime, containing the quoted
decision -- are shown with their excerpt for the operator to read, marked when
they are pasted text or contain negating words. The verdict stays UNVERIFIED;
approval is never inferred, and neither is rejection. The close stamp's uid is
shown as context only: the operator and every agent run as the same uid.

### Completion (`completion:<ticket>`)
Never VERIFIED: no acceptance criterion has an independent probe. CONTRADICTED
when the closing record's own residuals admit a criterion was not done (a
negation sharing at least two word stems with the criterion). Each criterion
is listed with its status.

## Local configuration

Nothing site-specific is built in. The checker reads where evidence lives from
a machine-local file, `$ATS_VERIFY_CONFIG` or else
`~/.config/ai-team-sync/verify.toml`, which is never committed. Without it no
ticket service, receipts, ledgers or deploy targets are consulted, and the
claims that need them stay UNVERIFIED. Example with placeholder values:

```toml
# A JSON ticket record: status, description, verified_by. {ticket} is the id.
ticket_url = "http://127.0.0.1:9000/api/tickets/{ticket}"
# Intake receipts for a session's ATS completion events.
receipt_url = "http://127.0.0.1:9000/api/receipts/{session_id}"

# Deployed by install: package files compared with the commit.
[[targets]]
repo = "~/src/my-app"
install_dir = "~/.local/share/pipx/venvs/my-app/lib/python3.12/site-packages/my_app"
source_prefix = "src/my_app"
probe_url = "http://127.0.0.1:9100/api/version"   # reports {"commit", "package_path"}
probe_reports_commit = true
units = [{ name = "my-app", user = true }]

# Deployed as a live checkout.
[[targets]]
repo = "/srv/other-app"
probe_url = "http://127.0.0.1:9200/health"
units = [{ name = "other-app" }, { name = "other-app-worker" }]

# Migration ledger per deploy target name (as used in verified_by.deployed).
[ledgers.other]
db = "other_db"
host = "127.0.0.1"
user = "readonly"
table = "schema_migrations"
```

## What the recorded evidence cannot establish

- That code is correct: a VERIFIED review proves who examined which code and
  what they concluded, not that they were right.
- Feature behavior after a deploy: probes show a service answers and, where it
  reports one, which build; a health-only probe does not name the revision.
- Whether a long-running service loads the files that changed after it
  started.
- Acceptance criteria: none has an independent probe.
- Work by Claude delegates: their transcripts are not bound to delegations.
- Handoffs written before the intake service issued receipts.
- Test claims that state no count.
- Who closed a ticket: every process runs as the same uid.
