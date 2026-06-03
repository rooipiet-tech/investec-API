from invespend.bb import _infer_sign_convention, mask


def test_mask_keeps_last_four():
    assert mask("10012827853") == "*******7853"
    assert mask("123") == "***"
    assert mask(None) == ""


def test_infer_signed_convention():
    # balance delta equals Amount → Amount is already signed (negative = debit).
    rows = [
        {"Amount": -200.0, "RunningBalance": 800.0},
        {"Amount": -85.5, "RunningBalance": 714.5},
        {"Amount": 15000.0, "RunningBalance": 15714.5},
    ]
    assert "ALREADY SIGNED" in _infer_sign_convention(rows)


def test_infer_unsigned_convention():
    # balance falls by the Amount but Amount is reported positive → unsigned.
    rows = [
        {"Amount": 200.0, "RunningBalance": 800.0},
        {"Amount": 85.5, "RunningBalance": 714.5},   # balance dropped 85.5
        {"Amount": 100.0, "RunningBalance": 614.5},  # balance dropped 100
    ]
    assert "UNSIGNED" in _infer_sign_convention(rows)


def test_infer_indeterminate_without_pairs():
    assert "indeterminate" in _infer_sign_convention([{"Amount": 10.0}])
