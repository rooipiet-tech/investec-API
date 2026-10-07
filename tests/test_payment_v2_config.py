"""S11: additive config fields, mode helper, audit `ref` key, scrub_message."""
from __future__ import annotations

import dataclasses
import json
import os

import pytest

from tests.v2_harness import make_settings, mod

pytestmark = pytest.mark.xfail(strict=False, reason="S11 red: config/mode/v2_cli not built yet")

REQUIRED = {"INVESTEC_CLIENT_ID": "cid", "INVESTEC_CLIENT_SECRET": "csec", "INVESTEC_API_KEY": "akey", "DATABASE_URL": "postgresql://x/y"}


def load(monkeypatch, **env):
    from invespend import config
    for k in list(os.environ):
        if k.startswith(("PAYMENTS_", "IMAGE_", "ANTHROPIC_")):
            monkeypatch.delenv(k, raising=False)
    for k, v in {**REQUIRED, **env}.items():
        monkeypatch.setenv(k, v)
    return config.Settings.load()


def test_defaults_are_additive_and_safe(monkeypatch):
    s = load(monkeypatch)
    assert s.payments_mode == "legacy" and s.payments_state_backend == "file"
    assert s.payments_allowed_senders == () and s.payments_hold_hours == 0 and s.payments_hold_registered_recent is True
    assert (s.payments_max_message_age_hours, s.payments_approval_expiry_hours, s.payments_approved_grace_hours) == (24, 24, 24)
    assert s.payments_max_batch_items == 50 and s.payments_submitting_stale_minutes == 30 and s.payments_stuck_message_minutes == 30
    assert s.payments_awaiting_expiry_days == 7 and s.payments_held_expiry_hours == 24 and s.payments_duplicate_window_days == 7
    assert s.payments_fingerprint_key == "" and s.image_extractor_model == "" and s.anthropic_api_key == ""
    assert s.per_payment_cap == 0.0 and s.daily_aggregate_cap == 0.0          # no code fallback: env only
    assert s.live_enabled() is False


def test_list_settings_are_lowercased_stripped_tuples(monkeypatch):
    s = load(monkeypatch, PAYMENTS_ALLOWED_SENDERS=" Piet@Example.com , other@example.com ,", PAYMENTS_AUTHSERV_IDS="mx.google.com, x.y",
             PAYMENTS_AUTHSERV_ID="mx.google.com", PAYMENTS_NOTIFY_RECIPIENTS="Owner@Example.com", PAYMENTS_MODE=" V2 ")
    assert s.payments_allowed_senders == ("piet@example.com", "other@example.com")
    assert s.payments_authserv_ids == ("mx.google.com", "x.y") and s.payments_notify_recipients == ("owner@example.com",)
    assert s.payments_mode == "v2"


def test_expiry_windows_configurable_and_bad_values_fail_closed(monkeypatch):
    s = load(monkeypatch, PAYMENTS_MAX_MESSAGE_AGE_HOURS="6", PAYMENTS_APPROVAL_EXPIRY_HOURS="abc", PAYMENTS_APPROVED_GRACE_HOURS="-1",
             PAYMENTS_HELD_EXPIRY_HOURS="nan", PAYMENTS_AWAITING_EXPIRY_DAYS="0")
    assert s.payments_max_message_age_hours == 6
    assert s.payments_approval_expiry_hours == 0 and s.payments_approved_grace_hours == 0
    assert s.payments_held_expiry_hours == 0 and s.payments_awaiting_expiry_days == 0          # 0 = already expired (fail closed)


def test_unparsable_hold_setting_falls_back_to_24h_unset_means_0(monkeypatch):
    assert load(monkeypatch).payments_hold_hours == 0
    assert load(monkeypatch, PAYMENTS_HOLD_HOURS="").payments_hold_hours == 0
    assert load(monkeypatch, PAYMENTS_HOLD_HOURS="6").payments_hold_hours == 6
    for bad in ("abc", "-3", "inf", "nan"):
        s = load(monkeypatch, PAYMENTS_HOLD_HOURS=bad)
        assert s.payments_hold_hours == 24 and s.payments_hold_hours_fallback is True, bad


def test_cycle_hold_hours_helper_and_audit_note(tmp_path):
    cfg = mod("mode").v2_settings(make_settings(payments_hold_hours=24.0, payments_hold_hours_fallback=True))
    assert cfg.hold_hours == 24 and cfg.hold_fallback is True


def test_mode_defaults_to_legacy_for_stub_settings_and_normalises():
    m = mod("mode")
    assert m.payments_mode(object()) == "legacy"
    assert m.payments_mode(make_settings(payments_mode=None)) == "legacy"
    assert m.payments_mode(make_settings(payments_mode="  V2 ")) == "v2"
    assert m.payments_mode(make_settings(payments_mode="")) == "legacy"
    assert m.payments_mode(make_settings(payments_mode="weird")) == "weird"


def test_v2_settings_read_through_getattr_defaults_for_a_minimal_stub():
    cfg = mod("mode").v2_settings(object())
    assert cfg.allowed_senders == frozenset() and cfg.max_age_hours == 24 and cfg.hold_hours == 0 and cfg.max_batch_items == 50
    assert cfg.fingerprint_key == "" and cfg.per_payment_cap == 0 and cfg.daily_cap == 0


def test_audit_ref_key_round_trips_and_other_keys_still_scrubbed(tmp_path):
    from invespend.payments.audit import AuditLog
    log = AuditLog(tmp_path)
    log.append("cycle_start", {"ref": "B-1006-3fa9"})
    log.append("x", {"ref": "ab123456cdef", "account_number": "1234567890", "note": "1234567890", "reason": "acct 123456789012"})
    first, second = log.read_entries()
    assert first["detail"] == {"ref": "B-1006-3fa9"}
    assert second["detail"]["ref"] == "ab[redacted]cdef"                        # documented R7-T10 behaviour
    assert "account_number" not in second["detail"] and "note" not in second["detail"]
    assert "123456789012" not in json.dumps(second)


def test_scrub_message_removes_settings_secret_values_and_6plus_digit_runs():
    s = make_settings(anthropic_api_key="sk-ant-api03-SECRETSECRET", imap_password="p4ssw0rd-value", database_url="postgresql://u:pw@h/db",
                      payments_fingerprint_key="fingerprint-key-1")
    text = ("boom 123456789012 with sk-ant-api03-SECRETSECRET and p4ssw0rd-value, postgresql://u:pw@h/db and "
            "fingerprint-key-1 ok 12345")
    out = mod("v2_cli").scrub_message(text, s)
    for secret in ("123456789012", "sk-ant-api03-SECRETSECRET", "p4ssw0rd-value", "postgresql://u:pw@h/db", "fingerprint-key-1"):
        assert secret not in out
    assert "[redacted]" in out and "12345" in out and "boom" in out
