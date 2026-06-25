"""F6/F7/F17/F20: persistence — idempotent pending, atomic daily aggregate,
restart persistence, minimal fields, retention cleanup."""
from datetime import datetime, timedelta, timezone

import pytest

from invespend.payments.store import PaymentStore


def _add(store, key="dk1", amount="100.00"):
    return store.add_pending(
        dedup_key=key, amount=amount, currency="ZAR",
        beneficiary_id="BEN1", source_account_id="ACC1",
        source_account_last3="709", message_id="msg-1", nonce="n1",
    )


def test_add_pending_idempotent(tmp_path):
    s = PaymentStore(tmp_path)
    _add(s)
    _add(s)
    assert len(s.list_records()) == 1


def test_record_has_no_full_account_number(tmp_path):
    s = PaymentStore(tmp_path)
    rec = s.add_pending(
        dedup_key="dk", amount="100.00", currency="ZAR",
        beneficiary_id="BEN1", source_account_id="ACC1",
        source_account_last3="709", message_id="10010900709", nonce="n1",
    )
    blob = str(rec)
    assert "10010900709" not in blob  # raw message-id (PAN-shaped) only hashed
    assert rec["source_account_last3"] == "709"


def test_daily_total_persists_across_fresh_process(tmp_path):
    s = PaymentStore(tmp_path)
    _add(s, amount="500.00")
    out = s.execute_and_commit("dk1", "500.00", daily_aggregate_cap="2000",
                               execution_mode="dry-run")
    assert out["committed"] is True
    # Fresh store instance == fresh process re-reading from disk.
    s2 = PaymentStore(tmp_path)
    assert str(s2.daily_total()) == "500.00"
    assert s2.is_executed("dk1") is True


def test_two_pendings_jointly_exceeding_aggregate_blocked(tmp_path):
    # PR3: each under cap, together over the aggregate -> second blocked.
    s = PaymentStore(tmp_path)
    _add(s, key="dk1", amount="700.00")
    _add(s, key="dk2", amount="700.00")
    a = s.execute_and_commit("dk1", "700.00", daily_aggregate_cap="1000",
                             execution_mode="dry-run")
    b = s.execute_and_commit("dk2", "700.00", daily_aggregate_cap="1000",
                             execution_mode="dry-run")
    assert a["committed"] is True
    assert b["committed"] is False
    assert str(s.daily_total()) == "700.00"


def test_double_execute_does_not_double_count(tmp_path):
    s = PaymentStore(tmp_path)
    _add(s, amount="500.00")
    s.execute_and_commit("dk1", "500.00", daily_aggregate_cap="2000",
                         execution_mode="dry-run")
    again = s.execute_and_commit("dk1", "500.00", daily_aggregate_cap="2000",
                                 execution_mode="dry-run")
    assert again["committed"] is False
    assert str(s.daily_total()) == "500.00"


def test_nonce_burned_on_execute(tmp_path):
    s = PaymentStore(tmp_path)
    _add(s, amount="100.00")
    s.execute_and_commit("dk1", "100.00", daily_aggregate_cap="2000",
                         execution_mode="dry-run")
    assert s.get("dk1")["nonce"] == ""


def test_cleanup_removes_old_records(tmp_path):
    s = PaymentStore(tmp_path)
    rec = _add(s)
    # Backdate created_at beyond retention.
    data = s._load_pending()
    data["dk1"]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(days=120)
    ).isoformat()
    s._save_pending(data)
    removed = s.cleanup(retention_days=90)
    assert removed == 1
    assert s.list_records() == []


def test_executed_burned_state_durable_if_totals_write_fails(tmp_path, monkeypatch):
    # OR-3/F6/F7: the executed+burned pending state must be persisted BEFORE (or
    # independently of) the daily-total write, so a crash during the totals write
    # cannot leave a record still pending/un-burned (which would let a
    # re-presented token double-pay).
    from invespend.payments import store as store_mod

    s = PaymentStore(tmp_path)
    _add(s, amount="500.00")

    real_write = store_mod._atomic_write_json

    def write_failing_on_totals(path, payload):
        if str(path).endswith("daily_totals.json"):
            raise OSError("simulated crash writing totals")
        return real_write(path, payload)

    monkeypatch.setattr(store_mod, "_atomic_write_json", write_failing_on_totals)
    with pytest.raises(OSError):
        s.execute_and_commit("dk1", "500.00", daily_aggregate_cap="2000",
                             execution_mode="dry-run")

    # Restore real writer; re-read from disk as a fresh process would.
    monkeypatch.setattr(store_mod, "_atomic_write_json", real_write)
    s2 = PaymentStore(tmp_path)
    rec = s2.get("dk1")
    assert rec["status"] == "executed"   # durably executed
    assert rec["nonce"] == ""            # nonce durably burned
    assert s2.is_executed("dk1") is True
    # A re-presented token cannot re-execute: already-executed is a no-op.
    again = s2.execute_and_commit("dk1", "500.00", daily_aggregate_cap="2000",
                                  execution_mode="dry-run")
    assert again["committed"] is False
