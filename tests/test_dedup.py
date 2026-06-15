from invespend.db import assign_day_seq, transaction_hash


def _txn(amount, desc, value_date="2026-05-26", action_date="2026-05-26"):
    return {
        "valueDate": value_date,
        "actionDate": action_date,
        "amount": amount,
        "description": desc,
    }


def _posted(amount, desc, posted_order, action_date="2026-05-26"):
    return {**_txn(amount, desc, action_date=action_date), "postedOrder": posted_order}


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


def test_stable_id_survives_pending_to_posted_revision():
    # A pending row that later posts gets revised description/dates/balance. With
    # a stable id the dedup key is unchanged, so it upserts instead of duplicating.
    pending = {**_txn("250.00", "PENDING AUTH SHOP"), "status": "PENDING", "uuid": "abc-123"}
    posted = {**_txn("250.00", "SHOP FINAL NAME", value_date="2026-05-27"),
              "status": "POSTED", "uuid": "abc-123", "runningBalance": "1000.00"}
    assert transaction_hash("ACC1", pending, 0) == transaction_hash("ACC1", posted, 0)


def test_stable_id_ignores_day_seq():
    tx = {**_txn("45.00", "COFFEE"), "uuid": "u-1"}
    assert transaction_hash("ACC1", tx, 0) == transaction_hash("ACC1", tx, 5)


def test_posted_order_is_stable_when_action_date_advances():
    # THE BUG: Investec returns actionDate as the fetch date, so a transaction
    # in the rolling window is re-fetched daily with a new actionDate. Keyed on
    # postedOrder the hash is unchanged, so the daily re-pull dedupes instead of
    # inserting a new copy every day.
    mon = _posted("2400.00", "BUSSIE UB40", "10540", action_date="2026-06-08")
    sun = _posted("2400.00", "BUSSIE UB40", "10540", action_date="2026-06-14")
    assert transaction_hash("ACC1", mon, 0) == transaction_hash("ACC1", sun, 0)


def test_distinct_posted_orders_stay_distinct():
    # The legitimate case: many identical month-end fees, each a real, separate
    # transaction with its own postedOrder, must NOT collapse.
    fees = [_posted("-6.00", "ELECTRONIC DEBIT FEE", str(po)) for po in (9623, 9621, 9620)]
    hashes = {transaction_hash("ACC1", f, 0) for f in fees}
    assert len(hashes) == 3


def test_posted_order_zero_is_treated_as_no_id():
    # postedOrder 0 (and "0") is Investec's not-yet-posted sentinel → content hash.
    a = {**_txn("250.00", "AUTH"), "postedOrder": 0}
    b = {**_txn("250.00", "AUTH DIFFERENT"), "postedOrder": "0"}
    assert transaction_hash("ACC1", a, 0) != transaction_hash("ACC1", b, 0)  # falls back


def test_content_fallback_ignores_action_date():
    # Even without any stable id, actionDate must not affect the key.
    a = _txn("10.00", "X", action_date="2026-06-01")
    b = _txn("10.00", "X", action_date="2026-06-30")
    assert transaction_hash("ACC1", a, 0) == transaction_hash("ACC1", b, 0)


def test_falls_back_to_content_hash_without_id():
    # No id field → content hash still distinguishes different descriptions.
    a = _txn("45.00", "COFFEE")
    b = _txn("45.00", "COFFEE DIFFERENT")
    assert transaction_hash("ACC1", a, 0) != transaction_hash("ACC1", b, 0)


def test_id_field_precedence():
    # postedOrder preferred over uuid; fields are namespaced so a uuid value and
    # an id value that happen to be equal do not collide.
    assert transaction_hash("ACC1", _posted("1", "x", "555"), 0) \
        == transaction_hash("ACC1", {**_posted("1", "x", "555"), "uuid": "other"}, 0)
    assert transaction_hash("ACC1", {"uuid": "x"}, 0) != transaction_hash("ACC1", {"id": "x"}, 0)
