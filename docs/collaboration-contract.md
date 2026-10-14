# Collaboration contract — slice 1 (#3585)

Operator-approved design: ticket roles, an owner-routed ticket mailbox and
structured VERIFY verdicts. Later slices (Tower controls and state propagation,
ticket-owned obligations, two-phase release of dead sessions, the completion
gate) are NOT part of this slice.

## Ticket roles

A session started with `ticket_id` has exactly one role on that ticket
(`sessions.ticket_role`, `ticket_roles.py`):

| Role | Who | General ticket mail | Scope |
|---|---|---|---|
| owner | the one accountable session | receives it | any |
| contributor | works the ticket under named files; IMPLEMENT delegation children | never | required |
| reviewer | claims nothing; VERIFY and READ_ONLY delegation children | never | none |

- At most one **live** (active or paused) owner per ticket per OS account,
  decided inside the acquisition guard and backed by the partial unique index
  `ux_sessions_live_ticket_owner`. A second owner request gets `409 ticket_owned`
  naming the holder (`owner_session_id`, `owner_agent`, nothing else); a writer
  that reaches the index anyway gets the same 409. The MCP `start_session` tool
  shows it as an ownership refusal with the same structured detail.
- Per account because OS accounts are ATS's trust boundary (#2741): mail never
  crosses accounts, so one account's owner must not block another account's
  sessions from receiving their own mail. On a single-operator host every
  interactive agent is one account, so there is exactly one owner per ticket.
- A delegation child works its parent's ticket: it inherits the parent's
  `ticket_id` (naming another is refused, `409 delegation_ticket_mismatch`) and
  takes the role its mode fixes. It can never be owner. An IMPLEMENT child on a
  ticket must name its files (`--scope`).
- Omitted role (callers that predate roles): owner if the ticket has no live
  owner; otherwise contributor when files are named, reviewer when none are.
  The response names the owner (`ticket_owner_session_id`).
- A reaped owner revived by heartbeat or reopened by PATCH comes back as a
  contributor (scoped) or reviewer if the ticket gained another live owner
  meanwhile, and its unread general ticket mail moves to that owner. A PATCH
  that races a new owner is refused with 409; retrying it demotes.

## Owner-routed ticket mailbox

Only **ticket-mode** mail changes (`addressing_mode = 'ticket'`: handoffs and
messages sent to a ticket rather than to a session):

- At send time it is addressed to the ticket's live owner in the sender's
  account (`delivery_history` reason `owner_route`).
- With no live owner it waits in the ticket pool; the next owner claims the
  pool when it starts and whenever it reads its inbox (reason `ticket_claim`).
- Contributors and reviewers never claim or receive it, by reading or
  otherwise. Briefs never claim anything (Stage 1).
- Explicit completion still returns unread ticket mail to the pool, for the
  next owner.
- Completing a ticket-bound session, a ticket-mode send and the owner's inbox
  claim take the same acquisition guard (SQLite: writer lock before the first
  read; PostgreSQL: transaction advisory lock), so none of them interleaves
  with another: a message is never routed to or claimed by an owner after its
  completion released the owner's unread mail.
- As a second net, after every ticket-mode send and every completion, unread
  ticket mail held by a session that may not hold it (a live contributor or
  reviewer, or an explicitly completed session) moves to the live owner or back
  to the pool (`ticket_roles.reconcile_ticket_mail`).

Unchanged, whatever the recipient's role: mail addressed to an exact session,
addressed decisions, ticket start/complete events and delegation traffic.

## Structured VERIFY verdicts

A VERIFY return must carry `structured_verdict` (`verdicts.py`); the server
refuses one without it (`422 verdict_required`), and refuses a structured
verdict on any other mode (`422 verdict_not_a_review`). Only the delegation's
child session, presenting its own capability, can record it (`403
not_the_reviewer`). The supervisor (`ats delegate`) builds it from the LAST
fenced `ats-verdict` JSON block in the child's output; a missing, unparsable or
invalid block becomes `NO_RESULT`, never a pass. Every VERIFY packet states the
contract.

- Items carry `status` (PASS/BLOCK), `severity` (material_safety,
  material_correctness, nonmaterial), `claim_category` and a citation.
- Severity follows operational impact: an inaccurate claim about security,
  authorization, data integrity or enforcement coverage is material even when
  the defect is only in docs or advisory text. Only `claim_category = other`
  may be nonmaterial (`422 invalid_verdict` otherwise).
- The **effective verdict** is derived from the items: any material BLOCK item
  → BLOCK; only nonmaterial BLOCK items → PASS_WITH_CITATION. The reported code
  is stored beside it, and a mismatch is flagged.
- A recorded verdict is final. The return moves the delegation from open to
  returned with a compare-and-set, so of several simultaneous submissions
  exactly one is written. Resubmitting the identical verdict (simultaneously or
  later, also after the parent closes the delegation) returns the recorded
  result unchanged; a different verdict is refused (`409 verdict_conflict`,
  naming the recorded effective verdict and reviewed head).
- Independence: a VERIFY delegation is refused (`409 review_not_independent`)
  when the reviewer is the same registered worker class as the requesting
  session, or when either class is not registered.
- `delegations.verdict` remains the parent's reconciliation note.

## Known limits of this slice

- This is coordination, not a security boundary against a hostile local
  process: every agent shares the operator's OS account, so anything that
  writes the database directly or calls the localhost API with a stolen
  capability is outside it.
- A reaped owner keeps the general mail it had already claimed while it is
  reaped; it moves when the session revives (to the new owner if it lost
  ownership) or is explicitly completed. Releasing it automatically is the
  later two-phase-release slice.
- Sessions that predate roles have none (they neither own nor claim) until a
  new session starts; mail they had already claimed stays with them.
- A ticket-mode send still requires the sender to be bound to that ticket.
