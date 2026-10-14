# Collaboration contract — slice 1 (#3585)

Operator-approved design: ticket roles, an owner-routed ticket mailbox and
structured VERIFY verdicts. Later slices (Tower controls and state propagation,
ticket-owned obligations, two-phase release of dead sessions, the completion
gate) are NOT part of this slice.

## Ticket roles

A session started with `ticket_id` has exactly one role on that ticket
(`sessions.ticket_role`, `ticket_roles.py`):

| Role | Who | Mailbox | Scope |
|---|---|---|---|
| owner | the one accountable session | receives the ticket's general mail | any |
| contributor | works the ticket under explicit files; IMPLEMENT delegation children | never | required when requested explicitly |
| reviewer | independent review; VERIFY and READ_ONLY delegation children | never | none |

- At most one **live** (active or paused) owner per ticket per OS account,
  decided inside the acquisition guard and backed by the partial unique index
  `ux_sessions_live_ticket_owner`. A second owner request gets `409 ticket_owned`
  naming the holder.
- Per account because OS accounts are ATS's trust boundary (#2741): mail never
  crosses accounts, so one account's owner must not block another account's
  sessions from receiving their own mail. On a single-operator host every
  interactive agent is one account, so there is exactly one owner per ticket.
- A delegation child's role is fixed by its mode; it can never be owner.
- Omitted role (callers that predate roles): owner if the ticket has no live
  owner, otherwise contributor; the response names the owner
  (`ticket_owner_session_id`).
- A reaped owner revived by heartbeat or reopened by PATCH comes back as a
  contributor if the ticket gained another live owner meanwhile.

## Owner-routed ticket mailbox

Only **ticket-mode** mail changes (`addressing_mode = 'ticket'`: handoffs and
messages sent to a ticket rather than to a session):

- At send time it is addressed to the ticket's live owner in the sender's
  account (`delivery_history` reason `owner_route`).
- With no live owner it waits in the ticket pool; the next owner claims the
  pool when it starts, and when it reads its inbox (reason `ticket_claim`).
- Contributors and reviewers never claim or receive it, by reading or
  otherwise. Briefs never claim anything (Stage 1).
- Explicit completion still returns unread ticket mail to the pool, for the
  next owner.

Unchanged, whatever the recipient's role: mail addressed to an exact session,
addressed decisions, ticket start/complete events and delegation traffic.

## Structured VERIFY verdicts

A VERIFY return must carry `structured_verdict` (`verdicts.py`); the server
refuses one without it (`422 verdict_required`). The supervisor (`ats
delegate`) builds it from the LAST fenced `ats-verdict` JSON block in the
child's output; a missing, unparsable or invalid block becomes `NO_RESULT`,
never a pass. The packet tells every VERIFY child the contract.

- Items carry `status` (PASS/BLOCK), `severity` (material_safety,
  material_correctness, nonmaterial), `claim_category` and a citation.
- Severity follows operational impact: an inaccurate claim about security,
  authorization, data integrity or enforcement coverage is material even when
  the defect is only in docs or advisory text. Only `claim_category = other`
  may be nonmaterial (`422 invalid_verdict` otherwise).
- The **effective verdict** is derived from the items: any material BLOCK item
  → BLOCK; only nonmaterial BLOCK items → PASS_WITH_CITATION. The reported code
  is stored beside it, and a mismatch is flagged.
- Independence: a VERIFY delegation whose reviewer is the same worker class as
  the requesting session is refused (`409 review_not_independent`).
- `delegations.verdict` remains the parent's reconciliation note.

## Known limits of this slice

- Ownership is advisory to anything that does not go through ATS: every agent
  shares the operator's OS account.
- `ats delegate` children are created without `ticket_id`, so their reviewer
  role applies only when a child is explicitly bound; independence is enforced
  at delegation creation instead.
- Existing sessions have no role until they start again; their tickets get an
  owner from the next session that starts on them.
- A reaped owner still holds the ticket mail it had claimed until it revives or
  completes (two-phase release is a later slice).
- A ticket-mode send still requires the sender to be bound to that ticket.
