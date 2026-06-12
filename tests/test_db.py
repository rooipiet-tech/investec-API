from invespend.db import _TX_COLUMNS, _tx_params


def _tx(**kw):
    base = {"amount": "100.00", "type": "CREDIT", "description": "X",
            "valueDate": "2026-05-26", "actionDate": "2026-05-26"}
    base.update(kw)
    return base


def test_tx_params_has_one_value_per_column():
    n_cols = len(_TX_COLUMNS.split(","))
    params = _tx_params("ACC1", _tx(), "Income", 0)
    assert len(params) == n_cols == 16


def test_tx_params_signs_debits_negative():
    debit = _tx_params("ACC1", _tx(amount="250.00", type="DEBIT"), "Groceries", 0)
    credit = _tx_params("ACC1", _tx(amount="250.00", type="CREDIT"), "Income", 0)
    # amount is the 12th column (index 11).
    assert debit[11] == -250.0
    assert credit[11] == 250.0
