# Human decisions — email-payment-v2 (spec approval, verbatim intent)

APPROVED the spec subject to these decisions:

- Q1: unregistered payee -> NOTIFY (no API creation). Separate Playwright helper = later project (out of scope).
- Q2 (CHANGED): The 24h wait applies ONLY to payees that are NEW (not yet registered / newly created beneficiary). Per the user's earlier analysis, Investec
  requires that a newly created beneficiary can only be paid 24 hours later. For payees that are ALREADY registered, pay IMMEDIATELY (no 24h hold, no cancel window).
  (Researcher note: the 24h rule is NOT in the official swagger we could read; the community FAQ instead says a beneficiary must have been paid once in Investec Online
  before API payments work. Spec must treat the 24h as a configurable, app-side rule for new beneficiaries and fail closed with a notification if Investec rejects.)
- Q3: check the sender's address (From allowlist) + Gmail Authentication-Results dkim/spf pass. NO secret code.
- Q4: GitHub Actions scheduled workflow for now. Dry-run default stays; rollout gates G1-G3 stay.
- Q5: digits only: 'pay' + 3 digits = last 3 digits of the source account number (any account).
- Q6: mobile-app beneficiary field order, EXACTLY: Beneficiary name, Bank, Account number, Amount, Their reference, My reference, Payment notification,
  Beneficiary email address, Beneficiary mobile number. (Branch code is not in the user's list: show it only as a separate 'reference only' line, not in the ordered list.)
- Q7: caps: R30,000 per payment; R50,000 per day. (Note: community FAQ says API per-payment limit is R20,000 — unverified; Investec may reject > R20,000; caps are ours.)
- Q8: do not trust relayed/ARC mail; require a direct dkim/spf pass on the outer sender.
- Q9: YES for now to a separate payment-only Investec credential; remove later if it does not work. (Note: listing beneficiaries needs the same scope as paying, so the
  read/ingest key may keep that scope anyway — the split has limited value unless beneficiary listing for matching also moves; spec F27 stays Should.)
- NEW REQUIREMENT: the bot must also read IMAGES in the email (JPEG screenshots, photos, etc.) for payment details, in addition to body, quoted text, pdf/xlsx/csv.
  (Previously 'Image OCR / cloud OCR egress' was out of scope — now IN scope. Engine choice is open: Q10.)

## Amendment (after plan-review round 3)
- RETIRE the legacy token-approval flow instead of fixing it (human: "2, retire the old flow"). F20/F21 dropped; F42 added. Legacy code stays byte-identical, is not scheduled, is documented as deprecated; the v2 workflow pins PAYMENTS_MODE=v2.

## Amendment 2 (human answers after plan approval, 2026-10-06)
- Q10: YES, read images with Claude vision, with the ability to switch to other (cheaper) models via configuration (model id is a setting, not hard-coded; adapter behind the existing ImageExtractor interface). Planner note: prefer plain HTTPS via `requests` (already a dependency) over adding the `anthropic` SDK, to avoid a new dependency.
- Q11 + NEW REQUEST (PENDING CONFIRMATION of details): the human wants an APPROVAL EMAIL every 15-minute cycle when payments have been prepared (a "payment batch for approval"), for EVERYTHING, not only image-derived amounts. This would supersede Q2's "registered payee pays immediately" and Q11's per-item confirm: all payments (registered, newly-held-then-ready, image-derived) wait for the user's reply to a batch email. Proposed defaults awaiting confirmation: one batch email per cycle with numbered items; reply 'approve' (all) or 'approve 1 3'; 'cancel 2'; approved items execute in the next cycle; unapproved items expire after 24h; authentication by sender checks only (no tokens). Requires spec amendment (F12, F13, F35) and a replan before build.
- Items 16 and 13-15: human asked for plain-language explanations; answers pending.
