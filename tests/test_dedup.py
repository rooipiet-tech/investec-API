from invespend.db import assign_day_seq, transaction_hash


def _txn(amount, desc, value_date="2026-05-26", action_date="2026-05-26"):
    return {
        "valueDate": value_date,
        "actionDate": action_date,
        "amount": amount,
        "description": desc,
    }


def test_identical_same_day_txns_get_distinct_seq_and_hash():
    # Two genuinely identical R45 coffees on the same day must not collapse.
    txns = [_txn("45.00", "COFFEE SHOP"), _txn("45.00", "COFFEE SHOP")]
    paired = assign_day_seq("ACC1", txns)
    seqs = [seq for _, seq in paired]
    assert seqs == [0, 1]

    hashes = {transaction_hash("ACC1", tx, seq) for tx, seq in paired}
    assert len(hashes) == 2  # distinct rows, not deduped into one


def test_repull_is_idempotent():
    # Re-pulling the same window in the same order yields the same hashes.
    txns = [_txn("45.00", "COFFEE SHOP"), _txn("45.00", "COFFEE SHOP"), _txn("120.00", "LUNCH")]
    first = {transaction_hash("ACC1", tx, seq) for tx, seq in assign_day_seq("ACC1", txns)}
    second = {transaction_hash("ACC1", tx, seq) for tx, seq in assign_day_seq("ACC1", txns)}
    assert first == second
    assert len(first) == 3


def test_different_accounts_do_not_share_seq():
    a = assign_day_seq("ACC1", [_txn("10.00", "X")])
    b = assign_day_seq("ACC2", [_txn("10.00", "X")])
    assert transaction_hash("ACC1", a[0][0], a[0][1]) != transaction_hash("ACC2", b[0][0], b[0][1])
