# Iteration 2 — payee collapse (spec v1.1.0, F33–F36)
Single change set, LOW–MEDIUM risk, confined to src/invespend/beneficiary_match.py + new tests.
C1 beneficiary_match.py: add `payee_key(b) -> str` = f"acct:{digits}|{branch or ''}" when len(digits_only(account_number))>=MIN_ACCOUNT_DIGITS else f"id:{beneficiary_id}". In match_transaction, after collecting `hits` (id->token) for a rule, group ids by payee_key of the hitting beneficiary: 1 payee => MATCHED with beneficiary_id=min(hitting ids of that payee), token=min(tokens of that payee); >1 payees => AMBIGUOUS (token=min over all, candidate_count=#payees). No other logic changes.
C2 tests (NEW file tests/test_beneficiary_payee.py only): F33/F34/F36 cases above + shuffle determinism + account_number rule + prod-like fixture; existing tests/fixtures untouched. Run whole suite.
C3 docs: module docstring + README note (additive lines).
Not changing: db.py, migration, view, sync, ingest, config, workflow, PAYMENT_TRANSACTION_TYPES.
Rollout: after merge, ingest re-evaluates existing ambiguous rows automatically (candidate query skips only `matched`); matched rows frozen.
