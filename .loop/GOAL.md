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
