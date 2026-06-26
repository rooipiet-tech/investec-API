"""Operator self-test path: ONE explicit CLI payment that reuses every guardrail
(caps, live gate, beneficiary verification, audit, dedup) and bypasses none.

Fully offline: the Investec client, store, and audit are mocked / file-backed in
a tmp dir; no real endpoint is ever hit."""
import json

import pytest

from invespend import cli
from invespend.config import Settings
from invespend.payments import beneficiaries
from invespend.payments.audit import AuditLog
from invespend.payments.selftest import run_selftest
from invespend.payments.store import PaymentStore

ALLOW = [beneficiaries.Beneficiary(beneficiary_id="BEN1", name="Acme", match_names=["acme"])]
ACCOUNTS = [{"accountId": "ACC1", "accountNumber": "10010900709"}]
NOW = "2026-06-26T10:00:00+00:00"


def _settings(tmp_path, **over):
    base = dict(
        investec_client_id="x", investec_client_secret="x", investec_api_key="x",
        investec_base_url="u", database_url="db", smtp_host="h", smtp_port=587,
        smtp_user="u", smtp_password="p", report_sender="from@x",
        approval_signing_secret="sekret-strong-test-key-0123456789",
        per_payment_cap=1000.0, daily_aggregate_cap=2000.0, state_dir=str(tmp_path),
    )
    base.update(over)
    return Settings(**base)


class _Client:
    def __init__(self, accounts=None, beneficiaries_raw=None):
        self._accounts = ACCOUNTS if accounts is None else accounts
        self._beneficiaries_raw = beneficiaries_raw or []
        self.writes = []

    def get_accounts(self):
        return self._accounts

    def get_beneficiaries(self):
        return self._beneficiaries_raw

    def create_payment(self, source_id, beneficiary_id, amount, *a, **k):
        self.writes.append((source_id, beneficiary_id, amount))
        return {"ok": True}


def _run(settings, client=None, **kw):
    client = client or _Client()
    kw.setdefault("allowlist", ALLOW)
    kw.setdefault("now", NOW)
    res = run_selftest(settings, client=client, **kw)
    return res, client


# ── dry-run default: no create_payment, mode 'dry-run', committed, exit 0 ──────
def test_dry_run_default_no_write(tmp_path):
    s = _settings(tmp_path)
    res, client = _run(s, beneficiary_id="BEN1", amount="1.00", source_last3="709")
    assert res["mode"] == "dry-run"
    assert res["execution_mode"] == "dry-run"
    assert res["committed"] is True
    assert res["reason"] == "executed:dry-run"
    assert client.writes == []  # NO write endpoint in dry-run
    assert res["source_account_last3"] == "709"


# ── live: create_payment called once with the registered beneficiaryId + source ─
def test_live_executes_once_with_registered_id(tmp_path):
    s = _settings(tmp_path, payments_live_enable=True, investec_payments_enabled=True)
    assert s.live_enabled() is True
    res, client = _run(s, beneficiary_id="BEN1", amount="1.00", source_last3="709")
    assert res["mode"] == "live"
    assert res["committed"] is True
    assert client.writes == [("ACC1", "BEN1", "1.00")]
    # audited: a live_execute entry exists.
    steps = [e["step"] for e in AuditLog(s.state_dir).read_entries()]
    assert "selftest_live_execute" in steps
    assert "selftest_executed" in steps


# ── unregistered beneficiary id -> fail closed, no create_payment ─────────────
def test_unregistered_beneficiary_fails_closed(tmp_path):
    s = _settings(tmp_path, payments_live_enable=True, investec_payments_enabled=True)
    res, client = _run(s, beneficiary_id="NOPE", amount="1.00", source_last3="709")
    assert res["committed"] is False
    assert res["reason"] == "beneficiary_unregistered"
    assert client.writes == []


# ── over per-payment cap -> blocked, no create_payment ────────────────────────
def test_over_per_payment_cap_blocked(tmp_path):
    s = _settings(tmp_path, per_payment_cap=10.0,
                  payments_live_enable=True, investec_payments_enabled=True)
    res, client = _run(s, beneficiary_id="BEN1", amount="100.00", source_last3="709")
    assert res["committed"] is False
    assert res["reason"].startswith("cap_blocked:")
    assert client.writes == []


# ── daily aggregate already at cap -> blocked ─────────────────────────────────
def test_daily_aggregate_at_cap_blocked(tmp_path):
    s = _settings(tmp_path, per_payment_cap=1000.0, daily_aggregate_cap=5.0,
                  payments_live_enable=True, investec_payments_enabled=True)
    # First small payment consumes the whole daily cap.
    res1, client = _run(s, beneficiary_id="BEN1", amount="5.00", source_last3="709",
                        now="2026-06-26T09:00:00+00:00")
    assert res1["committed"] is True
    # A second one the same day must be blocked by the persisted aggregate.
    res2, _ = _run(s, client=client, beneficiary_id="BEN1", amount="1.00",
                   source_last3="709", now="2026-06-26T11:00:00+00:00")
    assert res2["committed"] is False
    assert res2["reason"].startswith("aggregate_blocked:")
    # Only the first payment hit the write path.
    assert client.writes == [("ACC1", "BEN1", "5.00")]


# ── ambiguous/zero source (no --source, >1 or 0 accounts) -> fail closed ───────
def test_no_source_multiple_accounts_fails_closed(tmp_path):
    s = _settings(tmp_path, payments_live_enable=True, investec_payments_enabled=True)
    two = [{"accountId": "A1", "accountNumber": "111"},
           {"accountId": "A2", "accountNumber": "222"}]
    res, client = _run(s, client=_Client(accounts=two), beneficiary_id="BEN1",
                       amount="1.00", source_last3=None)
    assert res["committed"] is False
    assert res["reason"] == "source_unresolved"
    assert client.writes == []


def test_no_source_zero_accounts_fails_closed(tmp_path):
    s = _settings(tmp_path, payments_live_enable=True, investec_payments_enabled=True)
    res, client = _run(s, client=_Client(accounts=[]), beneficiary_id="BEN1",
                       amount="1.00", source_last3=None)
    assert res["committed"] is False
    assert res["reason"] == "source_unresolved"
    assert client.writes == []


def test_no_source_single_account_uses_it(tmp_path):
    s = _settings(tmp_path, payments_live_enable=True, investec_payments_enabled=True)
    res, client = _run(s, beneficiary_id="BEN1", amount="1.00", source_last3=None)
    assert res["committed"] is True
    assert client.writes == [("ACC1", "BEN1", "1.00")]


# ── only beneficiaryId reaches create_payment, never an account number ────────
def test_only_beneficiary_id_reaches_create_payment(tmp_path):
    s = _settings(tmp_path, payments_live_enable=True, investec_payments_enabled=True)
    res, client = _run(s, beneficiary_id="BEN1", amount="1.00", source_last3="709")
    assert client.writes == [("ACC1", "BEN1", "1.00")]
    # The beneficiary account number is never the value passed to create_payment.
    _src, paid_id, _amt = client.writes[0]
    assert paid_id == "BEN1"
    assert paid_id != "10010900709"


# ── API-sourced verification: id must appear in the live beneficiaries list ────
def test_api_beneficiary_must_be_in_live_list(tmp_path):
    s = _settings(tmp_path, payments_beneficiaries_from_api=True,
                  payments_live_enable=True, investec_payments_enabled=True)
    client = _Client(beneficiaries_raw=[{"beneficiaryId": "BEN1", "beneficiaryName": "Acme"}])
    # allowlist=None forces the API path.
    res = run_selftest(s, client=client, beneficiary_id="BEN1", amount="1.00",
                       source_last3="709", allowlist=None, now=NOW)
    assert res["committed"] is True
    assert res["beneficiary_source"] == "api"
    assert client.writes == [("ACC1", "BEN1", "1.00")]


def test_api_unregistered_beneficiary_fails_closed(tmp_path):
    s = _settings(tmp_path, payments_beneficiaries_from_api=True,
                  payments_live_enable=True, investec_payments_enabled=True)
    client = _Client(beneficiaries_raw=[{"beneficiaryId": "OTHER"}])
    res = run_selftest(s, client=client, beneficiary_id="BEN1", amount="1.00",
                       source_last3="709", allowlist=None, now=NOW)
    assert res["committed"] is False
    assert res["reason"] == "beneficiary_unregistered"
    assert client.writes == []


# ── idempotency: identical self-test in the same instant does not double-pay ───
def test_repeated_identical_selftest_is_idempotent(tmp_path):
    s = _settings(tmp_path, payments_live_enable=True, investec_payments_enabled=True)
    res1, client = _run(s, beneficiary_id="BEN1", amount="1.00", source_last3="709")
    assert res1["committed"] is True
    res2, _ = _run(s, client=client, beneficiary_id="BEN1", amount="1.00",
                   source_last3="709")
    assert res2["committed"] is False
    assert res2["reason"] == "already_executed"
    assert client.writes == [("ACC1", "BEN1", "1.00")]  # exactly once


# ── CLI wiring ────────────────────────────────────────────────────────────────
def test_pay_selftest_subcommand_registered():
    choices = cli.build_parser()._subparsers._group_actions[0].choices
    assert "pay-selftest" in choices


def test_existing_subcommands_unchanged():
    choices = cli.build_parser()._subparsers._group_actions[0].choices
    for name in ("init-db", "ingest", "report", "statements", "backup",
                 "backfill-hashes", "approve-payments"):
        assert name in choices


def _cli_stub_settings(tmp_path, *, live):
    class _S:
        state_dir = str(tmp_path)
        investec_client_id = "READ_ID"
        investec_client_secret = "READ_SEC"
        investec_api_key = "READ_KEY"
        investec_write_client_id = "WRITE_ID"
        investec_write_client_secret = "WRITE_SEC"
        investec_write_api_key = "WRITE_KEY"
        investec_base_url = "u"
        report_sender = "from@x"
        per_payment_cap = 1000.0
        daily_aggregate_cap = 2000.0
        payments_state_backend = "file"
        payments_beneficiaries_from_api = False

        def live_enabled(self):
            return live

        def _has_write_trio(self):
            return True

        def payment_credentials(self):
            return (self.investec_write_client_id,
                    self.investec_write_client_secret,
                    self.investec_write_api_key)

    return _S()


def _patch_client(monkeypatch, client):
    monkeypatch.setattr("invespend.investec_client.InvestecClient.__init__",
                        lambda self, *a, **k: None)
    monkeypatch.setattr("invespend.investec_client.InvestecClient.get_accounts",
                        lambda self: client.get_accounts())
    monkeypatch.setattr("invespend.investec_client.InvestecClient.get_beneficiaries",
                        lambda self: client.get_beneficiaries())
    monkeypatch.setattr("invespend.investec_client.InvestecClient.create_payment",
                        lambda self, *a, **k: client.create_payment(*a, **k))


def test_cli_dry_run_exit_zero_no_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.Settings, "load",
                        classmethod(lambda c: _cli_stub_settings(tmp_path, live=False)))
    client = _Client()
    _patch_client(monkeypatch, client)
    monkeypatch.setattr("invespend.payments.beneficiaries.ALLOWLIST", ALLOW)
    rc = cli.main(["pay-selftest", "--beneficiary", "BEN1", "--amount", "1.00",
                   "--source", "709"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry-run"
    assert out["committed"] is True
    assert out["credential_set"] == "read"
    assert client.writes == []


def test_cli_fail_closed_exit_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.Settings, "load",
                        classmethod(lambda c: _cli_stub_settings(tmp_path, live=False)))
    client = _Client()
    _patch_client(monkeypatch, client)
    monkeypatch.setattr("invespend.payments.beneficiaries.ALLOWLIST", ALLOW)
    rc = cli.main(["pay-selftest", "--beneficiary", "NOPE", "--amount", "1.00",
                   "--source", "709"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["committed"] is False
    assert out["reason"] == "beneficiary_unregistered"
    assert client.writes == []


def test_cli_error_envelope(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.Settings, "load",
                        classmethod(lambda c: _cli_stub_settings(tmp_path, live=False)))
    monkeypatch.setattr("invespend.investec_client.InvestecClient.__init__",
                        lambda self, *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("api unreachable")

    monkeypatch.setattr("invespend.payments.selftest.run_selftest", boom)
    rc = cli.main(["pay-selftest", "--beneficiary", "BEN1", "--amount", "1.00"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "RuntimeError"
    assert "api unreachable" in out["message"]
