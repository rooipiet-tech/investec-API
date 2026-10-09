# Payments runbook (email-triggered payments, v2 batch approval)

Live money movement is OFF by default. The workflow
`.github/workflows/payments-cycle.yml` runs a dry-run cycle every 15 minutes, and only when the
repository variable `PAYMENTS_CYCLE_ENABLED` is exactly `true`. Nothing here turns live on.

## DEPRECATED: the legacy token-approval flow

The legacy token-approval flow (`PAYMENTS_MODE` unset or `legacy`, for example a manual
`invespend approve-payments --once`) is RETIRED, DEPRECATED and unsupported. It is never scheduled
and it is not fixed: it does not email approval tokens, and the bot must never be pointed at a
mailbox it also sends to. The scheduled workflow pins `PAYMENTS_MODE=v2` at job level and refuses to
run otherwise. Use only the v2 flow described below.

## Rollout gates (G1, G2, G3)

- **G1, dry-run soak:** run the scheduled dry-run cycle for at least 14 days and at least 5 real
  instructions, with the audit reviewed and zero unexpected outcomes. Also confirm on a real sample
  that Gmail exposes the topmost `Authentication-Results` header as expected.
- **G2, sandbox test:** exercise an end-to-end payment against the Investec sandbox
  (openapisandbox.investec.com, mocked money), including error bodies and the no-retry behaviour.
  The shape of Investec's rejection body is unverified until this run.
- **G3, the human enables live:** you set the `PAYMENTS_LIVE_ENABLE` repository variable, add the
  `INVESTEC_WRITE_*` secrets to the `payments` Environment, add the three `INVESTEC_WRITE_*` env
  lines to `.github/workflows/payments-cycle.yml` (they are not wired in the dry-run workflow),
  and confirm the caps, recorded in the audit. Live is never enabled by default
  and never by the build loop.

To keep live off: leave `PAYMENTS_LIVE_ENABLE` unset (the workflow defaults it to `false`), do
not wire or add the `INVESTEC_WRITE_*` secrets, and never set `INVESTEC_PAYMENTS_ENABLED=true`. To stop the
cycle entirely, unset or change the `PAYMENTS_CYCLE_ENABLED` variable.

## Trigger grammar

Send an email from an allowlisted address containing `pay NNN`, where `NNN` is the last 3 digits of
the source account (digits only). Anything else, such as `pay 12`, `pay 1234`, `pay abc`, two
different triggers, or a trigger next to a malformed one, is not a trigger. The amount and payee
come from marked amounts and labelled lines in the text, a forwarded or quoted section, a
supported attachment, or an image (advisory, see Images). The trigger digits never become an amount.

## Sender authentication

An instruction or reply is accepted only from an address in `PAYMENTS_ALLOWED_SENDERS`, with a
single ASCII From, and strictly aligned dkim and spf results in the topmost `Authentication-Results`
header added by a trusted authserv-id (`PAYMENTS_AUTHSERV_ID`, for Gmail `mx.google.com`). Gmail
Authentication-Results is an assumption to be confirmed in G1; the cycle's preflight fails closed
when the configuration is incomplete. A DMARC result that is absent or `none` is accepted; a
failing one is not. The strict address parser needs the latest Python 3.12 patch release, which is
why the workflow pins `python-version: "3.12"`.

## Routing paths

- **Registered payee:** the item is offered in the next batch email.
- **Unregistered payee:** nothing is created in Investec. You get a notify-only email asking you to
  paste the beneficiary details (payee name, bank, account number, reference; paste details in
  that order); the instruction is parked and joins a batch after the beneficiary appears in the
  live list and the hold has elapsed.
- Items over the per-payment cap are parked and never offered.
- A duplicate of an item already awaiting approval is parked.

## Batch approval flow

Every payment waits for your approval. Once per 15-minute cycle, when new items are ready, you get
one email with numbered items and a batch ref, for example `[BATCH B-1007-ab12]`. Each line shows
the payee, amount, source last-3, reference and `figures from:` (`typed`, `attachment` or `image`).

Reply as a NORMAL reply with ONLY the command on the first line, ABOVE the quoted text, and keep
the ref in the subject:

- `approve` approves every item; `approve 1 3` approves items 1 and 3 only.
- `cancel 2` cancels item 2; bare `cancel` cancels every item of that batch that has not run yet.

Approved items execute in the NEXT cycle. Unapproved items expire after 24h (configurable) and a
late approve is a no-op that is answered with an acknowledgement (`expired` or `not applied`).
Partial approval leaves unmentioned items pending; they are listed under "still pending".

A quote-stripped mobile reply has no quote boundary: an `approve` from it gets a not-understood
notice and approves nothing, while `cancel` and `cancel 2` ARE still honoured from the raw first
line, because cancel can only reduce payments. Items already approved WILL still run in the next
cycle unless cancelled. A batch whose total exceeds the daily cap executes until the cap is reached
and parks the rest. A reply from a client that sets `Precedence: bulk/junk/list/auto_reply`,
`X-Autoreply`, `X-Autorespond` or `X-Auto-Response-Suppress` is ignored as an automatic message,
and the approve-and-cancel ambiguity check stops at an RFC 3676 `-- ` signature delimiter. A batch
email whose send outcome is unknown is re-sent with the SAME ref.

## New-beneficiary hold

A hold applies to a new beneficiary before its item is offered. It is configurable with
`PAYMENTS_HOLD_HOURS` (default 0 hours, the mechanism is retained; an unparsable value falls back
to 24h, unset means 0). Two caveats about Investec itself:

1. The "24 hour" new-beneficiary rule is NOT in the official swagger.
2. The community FAQ says a beneficiary must be paid once in Investec Online first, which may
   still block new beneficiaries even with hold 0.

If Investec rejects a payment, the item is `failed` with Investec's message, you are notified,
nothing is resent, and a new attempt needs a fresh instruction and a new approval.

## Caps

`PER_PAYMENT_CAP` is R20,000 and `DAILY_AGGREGATE_CAP` is R50,000. Both fail closed: unset, zero,
negative, malformed or non-finite means no payment passes (a bad value blocks payments instead of
crashing). R20,000 matches the community-documented Investec per-payment API limit, which is
unverified and not in the swagger; Investec may reject at a different limit, and the caps are
ours, Investec does not enforce them. A claim refused by the daily cap parks the instruction
(terminal, one email, no automatic retry the next day), so you resend.

## Images

Images are advisory and never a command or a trigger: an image cannot start, approve, route or
select an account, its figures show as `figures from: image`, and a conflict with any other source
parks the item. Images are skipped before any request when oversize (`PAYMENTS_MAX_IMAGE_BYTES`,
default 5 MB) or unsupported, and only after sender authentication, trigger and the age gate.

**Image engine setup:** add `ANTHROPIC_API_KEY` as an Actions secret in the `payments` Environment
and set `IMAGE_EXTRACTOR_MODEL` as a repository variable (any vision-capable model id; change the
variable to switch to a cheaper model, no code change). If either is empty, images are skipped with
an audit note, never an error. Egress is plain HTTPS to `api.anthropic.com` only, with timeouts and
no retries. POPIA note: bank details in an image leave the machine for that provider.

## Credentials

Payment secrets live in the GitHub Environment `payments`, restricted to the `main` branch. The
job has `contents: read` only and no `pull_request` trigger. The workflow keeps one Investec key
(reads and, in live mode, payments); the `INVESTEC_WRITE_*` trio is not passed to the job until G3.

## Environment variables

| Variable | Where | Meaning |
| --- | --- | --- |
| `PAYMENTS_CYCLE_ENABLED` | repo variable | Must be `true` for the scheduled job to run; unset = off |
| `PAYMENTS_MODE` | workflow (pinned) | `v2`; the legacy flow is DEPRECATED |
| `PAYMENTS_STATE_BACKEND` | workflow (pinned) | `postgres` is required, else the cycle refuses |
| `PAYMENTS_ALLOWED_SENDERS` | repo variable | Comma-separated allowlisted sender addresses |
| `PAYMENTS_AUTHSERV_ID` | repo variable | Trusted authserv-id (Gmail: `mx.google.com`) |
| `PAYMENTS_FINGERPRINT_KEY` | secret | HMAC key for beneficiary and account fingerprints |
| `IMAP_MAILBOX` | repo variable | Dedicated mailbox label to read (never INBOX) |
| `PER_PAYMENT_CAP` | repo variable | Rand, intended 20000; fail-closed when unset |
| `DAILY_AGGREGATE_CAP` | repo variable | Rand, intended 50000; fail-closed when unset |
| `PAYMENTS_HOLD_HOURS` | repo variable | New-beneficiary hold, default 0 |
| `PAYMENTS_APPROVAL_EXPIRY_HOURS` | env | Approval window, default 24 |
| `PAYMENTS_MAX_MESSAGE_AGE_HOURS` | env | Maximum age of an instruction email, default 24 |
| `PAYMENTS_MAX_IMAGE_BYTES` | env | Image size limit, default 5000000 |
| `ANTHROPIC_API_KEY` | secret | Image reading key; empty disables images |
| `IMAGE_EXTRACTOR_MODEL` | repo variable | Vision model id; empty disables images |
| `PAYMENTS_LIVE_ENABLE` | repo variable | Live switch, default `false`; G3 only |

## Operational notes

- **0010 interaction:** migration 0010 re-enables row level security on `payment_daily_total` and
  `payment_audit` on every `init_db`, and so on every ingest. That takes an AccessExclusive lock on
  two tables the payments cycle also uses. `init_db` sets a 15s `lock_timeout`, so the ingest and
  payments crons are offset (ingest 02:00 UTC, payments at minutes 7, 22, 37 and 52). A cycle that
  loses the lock race fails safe: a claim error and no POST, or a status update that fails after a
  POST leaves `submitting`, which the stale sweep moves to `needs_review`. Migration 0013 takes no
  lock on re-run.
- **Dry-run reservations:** a dry-run `executed` instruction keeps its reservation in the shared
  `payment_daily_total`, so the dry-run soak consumes the real day's R50,000 cap. This is the safe
  direction; check the day's total before G3 or wait for the next SAST day.
- `needs_review` emails say the payment MAY HAVE BEEN PAID.
- Run `init-db` before enabling v2; `PAYMENTS_STATE_BACKEND=postgres` is required.
- Dedicated mailbox label: the owner chose to run the bot on their own Gmail account (IMAP user = the account that
  receives instructions and sends the approval batches), reading ONLY a dedicated mailbox label
  (for example `payments-in`, with Show in IMAP ticked), never INBOX (the preflight refuses INBOX).
  A Gmail filter applies the label to mail from the allowed senders and skips the Inbox. The IMAP
  fetch marks mail as read (only the labelled mail), and nothing else is touched. The filter also labels the
  bot's own approval batches (same From address); the loop guard ignores them. Mail you send to
  yourself may lack `dkim=pass`/`spf=pass` in `Authentication-Results`, in which case it is
  ignored (fail closed): verify with "Show original" at G1 and send from another allowed address
  if so. Keep the account below its 15 GB storage limit, or approval emails will bounce.
- Cap parsing blocks on bad values (value 0) rather than crashing or accepting them.
- Scheduling: GitHub cron is best effort and scheduled workflows pause after 60 days of repository
  inactivity; windows and expiry tolerate late runs (an approved item executes in the next cycle
  that runs).
- A stuck message is re-sent by the notice sweep; the bootstrap snapshot of existing beneficiaries
  is trusted as established.
- The Playwright helper for creating beneficiaries in Investec Online is a separate later project;
  nothing here creates beneficiaries.
