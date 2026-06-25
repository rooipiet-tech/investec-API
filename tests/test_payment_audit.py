"""F9/F15/F20: append-only audit; no secret, no full account number recorded."""
from invespend.payments.audit import AuditLog


def test_append_only_ordered(tmp_path):
    a = AuditLog(tmp_path)
    a.append("step1", {"reason": "a"})
    a.append("step2", {"reason": "b"})
    entries = a.read_entries()
    assert [e["step"] for e in entries] == ["step1", "step2"]
    # Appending again does not mutate earlier entries.
    a.append("step3")
    again = a.read_entries()
    assert again[0] == entries[0]
    assert again[1] == entries[1]


def test_entries_are_timestamped(tmp_path):
    a = AuditLog(tmp_path)
    a.append("s")
    assert a.read_entries()[0]["ts"]


def test_secret_never_recorded(tmp_path):
    a = AuditLog(tmp_path)
    # Forbidden keys are dropped even if a caller passes them.
    a.append("oops", {"secret": "SUPERSECRET", "token": "abc:1:def", "reason": "ok"})
    blob = "\n".join(str(e) for e in a.read_entries())
    assert "SUPERSECRET" not in blob
    assert "abc:1:def" not in blob
    assert "ok" in blob


def test_full_account_number_redacted(tmp_path):
    a = AuditLog(tmp_path)
    a.append("x", {"reason": "paying 10010900709 now", "source_account_last3": "709"})
    blob = "\n".join(str(e) for e in a.read_entries())
    assert "10010900709" not in blob
    assert "709" in blob  # last-3 allowed


def test_account_number_key_dropped(tmp_path):
    a = AuditLog(tmp_path)
    a.append("x", {"account_number": "10010900709", "reason": "ok"})
    blob = "\n".join(str(e) for e in a.read_entries())
    assert "10010900709" not in blob


def test_numeric_amount_keys_not_mangled_but_pan_still_redacted(tmp_path):
    # OR-2/F9: large legitimate amounts stay readable; a free-text account
    # number elsewhere in the same entry is still redacted.
    a = AuditLog(tmp_path)
    a.append("x", {
        "daily_total": "100000.00",
        "amount": "250000.00",
        "reason": "paying 10010900709 now",
    })
    entry = a.read_entries()[0]
    assert entry["detail"]["daily_total"] == "100000.00"  # intact, not redacted
    assert entry["detail"]["amount"] == "250000.00"
    assert "10010900709" not in entry["detail"]["reason"]  # PAN still redacted
