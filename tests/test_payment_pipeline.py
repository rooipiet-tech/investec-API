"""F1-F12,F16,F19: the orchestration invariants end-to-end (fully mocked)."""
import json

import pytest

from invespend.config import Settings
from invespend.payments import beneficiaries, pipeline, token
from invespend.payments.audit import AuditLog
from invespend.payments.inbox import InboundMessage
from invespend.payments.store import PaymentStore

ALLOW = [beneficiaries.Beneficiary(beneficiary_id="BEN1", name="Acme", match_names=["acme"])]
ACCOUNTS = [{"accountId": "ACC1", "accountNumber": "10010900709"}]


def _settings(tmp_path, **over):
    base = dict(
        investec_client_id="x", investec_client_secret="x", investec_api_key="x",
        investec_base_url="u", database_url="db", smtp_host="h", smtp_port=587,
        smtp_user="u", smtp_password="p", report_sender="from@x",
        approval_signing_secret="sekret-strong-test-key-0123456789",
        per_payment_cap=1000.0,
        daily_aggregate_cap=2000.0, state_dir=str(tmp_path),
    )
    base.update(over)
    return Settings(**base)


class _Client:
    def __init__(self):
        self.writes = []

    def get_accounts(self):
        return ACCOUNTS

    def create_payment(self, source_id, beneficiary_id, amount, *a, **k):
        self.writes.append((source_id, beneficiary_id, amount))
        return {"ok": True}


def _inbox(msgs):
    return type("I", (), {"fetch_messages": lambda self: list(msgs)})()


def _msg(body, token=None, last3="709", mid="m1", attachments=None):
    return InboundMessage(message_id=mid, from_addr="user@x", subject="",
                          body=body, token=token, last3=last3,
                          attachments=attachments or [])


def _run(settings, msgs, client=None, **kw):
    client = client or _Client()
    res = pipeline.run_approval_cycle(
        settings, inbox=_inbox(msgs), client=client,
        smtp_send=lambda *a: None, allowlist=ALLOW, **kw,
    )
    return res, client


# ── F1: inbound text never authorizes ────────────────────────────────────────
@pytest.mark.parametrize("body", ["approve", "app", "yes", "PLEASE APPROVE"])
def test_keyword_only_does_not_execute(tmp_path, body):
    s = _settings(tmp_path)
    msg = _msg(f"Payee: Acme\nAmount: R 500.00\n{body}")
    res, client = _run(s, [msg])
    assert res["executed"] == 0
    assert res["pending"] == 1
    assert client.writes == []


def test_forged_token_does_not_execute(tmp_path):
    s = _settings(tmp_path)
    forged = "fakenonce:99999999999:" + ("a" * 64)
    msg = _msg(f"Payee: Acme\nAmount: R 500.00\n{forged}", token=forged)
    res, client = _run(s, [msg])
    assert res["executed"] == 0
    assert client.writes == []


# ── F3: last-3 source selection ──────────────────────────────────────────────
def test_last3_zero_or_multi_fails_closed(tmp_path):
    s = _settings(tmp_path)
    res, client = _run(s, [_msg("Payee: Acme\nAmount: R 500.00", last3="999")])
    assert res["parked"] == 1
    assert client.writes == []


# ── F4: beneficiary allowlist ────────────────────────────────────────────────
def test_unregistered_beneficiary_fails_closed(tmp_path):
    s = _settings(tmp_path)
    res, client = _run(s, [_msg("Payee: Unknown\nAmount: R 500.00")])
    assert res["parked"] == 1
    assert client.writes == []


# ── F5/F11: extraction fail-closed ───────────────────────────────────────────
def test_ambiguous_amount_parked(tmp_path):
    s = _settings(tmp_path)
    res, _ = _run(s, [_msg("Payee: Acme\nAmount: R 100.00 and R 200.00")])
    assert res["parked"] == 1


def test_image_only_skipped_no_pending(tmp_path):
    s = _settings(tmp_path)
    msg = _msg("Payee: Acme", attachments=[("receipt.png", b"\x89PNG")])
    # Body has no amount and image is skipped -> not executed, no pending payment.
    res, client = _run(s, [msg])
    assert res["executed"] == 0
    assert client.writes == []


# ── F2/F6/F7/F8: valid token executes once in dry-run, caps + dedup ──────────
def _approve_flow(s, amount="500.00", client=None, mid="m1"):
    client = client or _Client()
    m1 = _msg(f"Payee: Acme\nAmount: R {amount}", mid=mid)
    pipeline.run_approval_cycle(s, inbox=_inbox([m1]), client=client,
                                smtp_send=lambda *a: None, allowlist=ALLOW)
    store = PaymentStore(s.state_dir)
    rec = [r for r in store.list_records() if r["status"] == "pending"][0]
    tok, _ = token.issue_token(
        s.approval_signing_secret, source_account_id=rec["source_account_id"],
        amount=rec["amount"], beneficiary_id=rec["beneficiary_id"],
        dedup_key=rec["dedup_key"], nonce=rec["nonce"],
    )
    m2 = _msg(f"Payee: Acme\nAmount: R {amount}\n{tok}", token=tok, mid=mid)
    res = pipeline.run_approval_cycle(s, inbox=_inbox([m2]), client=client,
                                      smtp_send=lambda *a: None, allowlist=ALLOW)
    return res, client, tok, store


def test_valid_token_executes_dry_run_no_write(tmp_path):
    s = _settings(tmp_path)
    res, client, _, store = _approve_flow(s)
    assert res["executed"] == 1
    assert client.writes == []  # dry-run: NO write endpoint
    assert str(store.daily_total()) == "500.00"


def test_token_single_use(tmp_path):
    s = _settings(tmp_path)
    res, client, tok, _ = _approve_flow(s)
    # re-present the same approved token: nonce burned -> not re-executed.
    m = _msg(f"Payee: Acme\nAmount: R 500.00\n{tok}", token=tok)
    res2 = pipeline.run_approval_cycle(s, inbox=_inbox([m]), client=client,
                                       smtp_send=lambda *a: None, allowlist=ALLOW)
    assert "executed:" not in str(res2["results"])
    assert client.writes == []


def test_over_per_payment_cap_blocked(tmp_path):
    s = _settings(tmp_path, per_payment_cap=100.0)
    res, client = _run(s, [_msg("Payee: Acme\nAmount: R 500.00")])
    assert res["parked"] == 1
    assert client.writes == []


def test_daily_aggregate_persists_and_blocks(tmp_path):
    s = _settings(tmp_path, daily_aggregate_cap=600.0)
    res, client, _, store = _approve_flow(s, amount="500.00", mid="m1")
    assert res["executed"] == 1
    # fresh process reads persisted total; a different 500 payment would exceed 600.
    s2 = _settings(tmp_path, daily_aggregate_cap=600.0)
    res2, client2, _, _ = _approve_flow(s2, amount="500.00", mid="m2")
    assert res2["executed"] == 0
    assert client2.writes == []


# ── F8 live path (mocked) ────────────────────────────────────────────────────
def test_live_mode_calls_write_and_audits(tmp_path):
    s = _settings(
        tmp_path, payments_live_enable=True,
        investec_write_client_id="w", investec_write_client_secret="w",
        investec_write_api_key="w",
    )
    assert s.live_enabled() is True
    res, client, _, _ = _approve_flow(s)
    assert res["executed"] == 1
    assert client.writes == [("ACC1", "BEN1", "500.00")]
    steps = [e["step"] for e in AuditLog(s.state_dir).read_entries()]
    assert "live_execute" in steps


# ── F9/F15: audit has no secret / no full PAN ────────────────────────────────
def test_audit_has_no_secret_or_full_pan(tmp_path):
    s = _settings(tmp_path)
    _approve_flow(s)
    blob = "\n".join(json.dumps(e) for e in AuditLog(s.state_dir).read_entries())
    assert "sekret" not in blob
    assert "10010900709" not in blob


def test_store_has_no_secret_or_full_pan(tmp_path):
    s = _settings(tmp_path)
    _approve_flow(s)
    blob = "\n".join(json.dumps(r) for r in PaymentStore(s.state_dir).list_records())
    assert "sekret" not in blob
    assert "10010900709" not in blob


# ── F7: dedup -> one pending for same email twice ────────────────────────────
def test_same_email_twice_one_pending(tmp_path):
    s = _settings(tmp_path)
    msg = _msg("Payee: Acme\nAmount: R 500.00")
    _run(s, [msg])
    _run(s, [msg])
    store = PaymentStore(s.state_dir)
    assert len(store.list_records()) == 1


# ── F12: progress email gating ───────────────────────────────────────────────
def test_progress_email_only_with_contact(tmp_path):
    s = _settings(tmp_path)
    sent = []
    pipeline.run_approval_cycle(
        s, inbox=_inbox([_msg("Payee: Acme\nAmount: R 500.00")]),
        client=_Client(), smtp_send=lambda st, m: sent.append(m),
        allowlist=ALLOW, sender_contact=None,
    )
    assert sent == []  # no contact -> no progress email

    sent2 = []
    pipeline.run_approval_cycle(
        s, inbox=_inbox([_msg("Payee: Acme\nAmount: R 600.00", mid="m2")]),
        client=_Client(), smtp_send=lambda st, m: sent2.append(m),
        allowlist=ALLOW, sender_contact="sender@third.party",
    )
    progress = [m for m in sent2 if m["Subject"] == "Payment request received"]
    assert len(progress) == 1
    body = progress[0].get_content().lower()
    assert "beneficiary" not in body and "token" not in body


# ── F16: machine-readable summary ────────────────────────────────────────────
def test_summary_is_json_serializable(tmp_path):
    s = _settings(tmp_path)
    res, _ = _run(s, [_msg("Payee: Acme\nAmount: R 500.00")])
    assert json.loads(json.dumps(res))["execution_mode"] == "dry-run"


# ── F19: consolidated approval email lists multiple pendings ─────────────────
def test_approval_email_consolidates_pendings(tmp_path):
    s = _settings(tmp_path)
    sent = []
    msgs = [
        _msg("Payee: Acme\nAmount: R 100.00", mid="a"),
        _msg("Payee: Acme\nAmount: R 200.00", mid="b"),
    ]
    pipeline.run_approval_cycle(
        s, inbox=_inbox(msgs), client=_Client(),
        smtp_send=lambda st, m: sent.append(m), allowlist=ALLOW,
        approval_recipients=["owner@x"],
    )
    approval = [m for m in sent if "approval required" in (m["Subject"] or "")]
    assert len(approval) == 1
    body = approval[0].get_content()
    assert "100.00" in body and "200.00" in body
    # two distinct tokens.
    import re
    tokens = re.findall(r"token:\s*(\S+)", body)
    assert len(tokens) == 2 and tokens[0] != tokens[1]
