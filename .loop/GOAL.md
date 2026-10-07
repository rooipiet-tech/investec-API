# GOAL — email-triggered payments v2 (run: email-payment-v2)

User request (verbatim intent): "I want to be able to email payments to rooipiet@gmail.com, either a reply or a
forwarded email. If I say in the body 'pay' followed by three letters [sic: three DIGITS per existing last-3 convention —
confirm], it means you should search everything in the mail to locate either a beneficiary payment from any profile or a
new beneficiary to be loaded, and then paid 24 hours later. Research how to do that on the Investec API. The 3 characters
after 'pay' represent the last 3 digits of which account to pay from."

Phase 0 only (research -> domain -> spec -> STOP for human approval). An email-payment-approval pipeline already exists
under src/invespend/payments/ (shipped in an earlier run, archived at .loop/archive/payments-run/). This run must establish
the gap between that pipeline and the request, and what the Investec API actually permits (esp. creating beneficiaries and
scheduling/delaying payments). Dry-run default and all existing guardrails stay unless the human explicitly changes them.

## Human decisions so far
- Q1 (unregistered payee): RESOLVED by human — NOTIFY the user (email) that the payee must be added in Investec Online; do NOT try to create it via API (not available).
  The notification should carry the beneficiary details (name, bank, account number, branch code, reference, any email/cell) laid out in the
  order the Investec mobile app asks for them, so they can be pasted easily.
- FOLLOW-UP (separate future run, OUT OF SCOPE here): a Playwright-based helper that holds those details to paste into the mobile app in the correct
  order to create the beneficiary easily. Open point for that run: does it drive a browser/online banking itself, or only present the details? Do not
  automate banking login/2FA without a separate security review.
- Q2 (after 24h), Q3 (sender checks), Q4 (where it runs): NOT answered (question dialog dismissed). Spec drafts the RECOMMENDED defaults as PROPOSALS:
  Q2 pay automatically after 24h unless cancelled by reply, with summary email now + reminder; Q3 allowed sender + DKIM/SPF pass (+ optional secret code);
  Q4 GitHub Actions scheduled job. Human must confirm or change at the spec gate.
