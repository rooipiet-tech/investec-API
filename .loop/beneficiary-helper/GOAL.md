# GOAL — Playwright beneficiary helper (run: beneficiary-helper)

Phase 0 only (research -> domain -> spec -> STOP for human approval).

Background: the email-payment pipeline (src/invespend/payments/) NOTIFIES the user when a payee is unregistered; the Investec
API cannot create beneficiaries. Follow-up (this run): a Playwright-based helper that holds the beneficiary details in the order
Investec Online / the mobile app asks for them, so the user can create the beneficiary easily.

Constraints (from .loop/GOAL.md, spec.json, decisions.md):
- NO automation of banking login or 2FA. No credentials/OTPs stored, logged or passed to Claude.
- Open question for the spec: does it drive a browser itself (user logs in manually, helper fills the form, user confirms the final
  submit) or only present details? Spec must propose options + a recommended default.
- Behaviour of existing CLI/ingest/report/payments frozen; no schema change unless spec says so. Dry-run/guardrails stay.
- Needs its own security review criteria.
- Active run email-payment-v2 is mid-build (slice 3) - do NOT touch its .loop/ files; write only under .loop/beneficiary-helper/.
