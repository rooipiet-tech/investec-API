"""F12/F14/F19: isolated send path; progress gating + minimisation; consolidation."""
import pytest

from invespend.emailer import send_email
from invespend.payments import notify


class _Settings:
    smtp_host = "h"
    smtp_port = 587
    smtp_user = "u"
    smtp_password = "p"
    report_sender = "from@example.com"
    report_recipients = ["to@example.com"]

    def require_email(self):  # satisfy send_email's pre-flight
        return None


def test_send_email_still_raises_on_zero_attachments(monkeypatch):
    # F14: the existing guard must keep firing for existing callers.
    monkeypatch.setattr("invespend.emailer._smtp_send", lambda *a, **k: None)
    with pytest.raises(ValueError):
        send_email(_Settings(), [], "subj", "body")


def test_notify_does_not_route_through_send_email(monkeypatch):
    # F14: approval/progress sends use _smtp_send directly, not send_email.
    sent = {}
    monkeypatch.setattr("invespend.payments.notify._smtp_send",
                        lambda s, msg: sent.setdefault("msg", msg))

    def boom(*a, **k):
        raise AssertionError("notify must not call send_email")

    monkeypatch.setattr("invespend.emailer.send_email", boom)
    notify.send_approval_email(_Settings(), ["o@example.com"], [
        {"amount": "100.00", "currency": "ZAR", "source_account_last3": "709",
         "beneficiary_name": "Acme", "token": "tok-1"},
    ])
    assert sent["msg"]["Subject"]


def test_progress_email_not_sent_without_contact(monkeypatch):
    calls = []
    monkeypatch.setattr("invespend.payments.notify._smtp_send",
                        lambda s, msg: calls.append(msg))
    assert notify.send_progress_email(_Settings(), "", 3, 0) is None
    assert notify.send_progress_email(_Settings(), None, 3, 0) is None
    assert calls == []


def test_progress_email_minimised(monkeypatch):
    sent = {}
    monkeypatch.setattr("invespend.payments.notify._smtp_send",
                        lambda s, msg: sent.setdefault("msg", msg))
    notify.send_progress_email(_Settings(), "sender@third.party", 2, 1)
    msg = sent["msg"]
    blob = (msg["Subject"] or "") + "\n" + msg.get_content()
    low = blob.lower()
    # F12: NONE of beneficiaryId / full account / balance / caps / token.
    assert "beneficiary" not in low
    assert "balance" not in low
    assert "cap" not in low
    assert "token" not in low
    assert "10010900709" not in blob


def test_approval_email_lists_each_pending_with_own_token():
    items = [
        {"amount": "100.00", "currency": "ZAR", "source_account_last3": "709",
         "beneficiary_name": "Acme", "token": "TOK1"},
        {"amount": "200.00", "currency": "ZAR", "source_account_last3": "123",
         "beneficiary_name": "Beta", "token": "TOK2"},
    ]
    msg = notify.build_approval_email("from@x", ["o@x"], items)
    body = msg.get_content()
    assert "TOK1" in body and "TOK2" in body
    assert "Acme" in body and "Beta" in body
    assert "2 pending" in msg["Subject"]
