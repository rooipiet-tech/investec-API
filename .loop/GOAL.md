# GOAL — beneficiary matching (run: beneficiary-matching)

<goal>
Let the user see, for each outgoing EFT/payment transaction ingested from Investec, which of
their registered Investec beneficiaries it was paid to (beneficiary name, bank, and account
number). The Investec transactions API does not return beneficiary fields; the
`/za/pb/v1/accounts/beneficiaries` endpoint (already wrapped by
`InvestecClient.get_beneficiaries`) does. Deliver: (1) a beneficiary snapshot synced from the API,
(2) a deterministic, explainable matcher that links debit EFT transactions to at most one
beneficiary using the transaction `description` (and `raw`) against beneficiary
name / reference fields, failing closed (no match) on ambiguity, (3) a way to read the result
(DB view at minimum). Constraints: additive-only schema (new numbered migration, existing
tables/views untouched, RLS deny-all like 0011); existing ingest/report/statements output
byte-identical unless the spec explicitly allows a change; feature off by default behind a
setting; no secrets committed; full pytest stays green (>= current baseline, 0 fail/0 error);
no new dependencies.
</goal>

Previous run (email-payment-approval-loop) archived under `.loop/archive/payments-run/`.
