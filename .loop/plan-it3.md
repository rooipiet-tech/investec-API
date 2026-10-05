# Iteration 3 — representative name preference (spec v1.2.0, F37–F38)
C1 beneficiary_match.py: in match_transaction, when exactly one payee is hit, pick the representative id by `_representative(payee_key, benes, hit_ids, variants)`: among ALL benes with that payee_key, key = (0 if normalise(beneficiary_name) in variants (len>=MIN_EXACT_LEN) else 1 if normalise(name) in variants else 2, 0 if id in hit_ids else 1, beneficiary_id); take min. matched_token/rule/candidate_count unchanged. Plain dict/loops, no new imports, no banned tokens (time., print(, open(, datetime, random., logging.) incl. docstrings.
C2 tests: add cases to NEW file tests/test_beneficiary_representative.py (F37 list). Do NOT edit any existing test (plan-review T3). Shuffle over all permutations.
C3 README/docstring additive.
Not changing db.py/migrations/config/ingest/workflow/payments/fixtures.
Rollout (separate, needs human OK): frozen matched rows keep old label; to apply F37 clear matched rows (derived data) after a snapshot, re-run ingest.
