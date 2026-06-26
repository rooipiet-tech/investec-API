"""F8/F13/F15/F16: client write path additive + mock-only; live_enabled
conjunction; additive CLI subcommand headless JSON."""
import json
import os

import pytest

from invespend.cli import build_parser
from invespend.config import Settings
from invespend.investec_client import InvestecClient


def _settings(**over):
    base = dict(
        investec_client_id="x", investec_client_secret="x", investec_api_key="x",
        investec_base_url="https://example.invalid",
        database_url="db", smtp_host="h", smtp_port=587, smtp_user="u",
        smtp_password="p", report_sender="from@x",
    )
    base.update(over)
    return Settings(**base)


def test_create_payment_posts_via_mock(monkeypatch):
    # OQ1: endpoint path is an ASSUMPTION; only ever exercised through a mock.
    client = InvestecClient("id", "sec", "key", "https://example.invalid")
    captured = {}

    def fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(client, "_post", fake_post)
    out = client.create_payment("ACC1", "BEN1", "100.00", reference="rent")
    assert out == {"ok": True}
    # Pays by beneficiaryId only — never a raw account number (F4).
    item = captured["payload"]["paymentList"][0]
    assert item["beneficiaryId"] == "BEN1"
    assert item["amount"] == "100.00"
    assert "ACC1" in captured["path"]


def test_get_beneficiaries_parses_data(monkeypatch):
    client = InvestecClient("id", "sec", "key", "https://example.invalid")
    monkeypatch.setattr(
        client, "_get",
        lambda path: {"data": [{"beneficiaryId": "B1"}, {"beneficiaryId": "B2"}]},
    )
    out = client.get_beneficiaries()
    assert out == [{"beneficiaryId": "B1"}, {"beneficiaryId": "B2"}]


def test_get_beneficiaries_empty_when_no_data(monkeypatch):
    client = InvestecClient("id", "sec", "key", "https://example.invalid")
    monkeypatch.setattr(client, "_get", lambda path: {})
    assert client.get_beneficiaries() == []


def test_payments_enabled_acts_as_write_credential():
    # User declares the single main key can pay: no separate write trio needed.
    s = _settings(investec_payments_enabled=True)
    assert s.has_write_credential() is True
    # Flag-only still dry-run; live needs payments_live_enable too.
    assert s.live_enabled() is False
    s2 = _settings(investec_payments_enabled=True, payments_live_enable=True)
    assert s2.live_enabled() is True


def test_payment_credentials_prefers_write_trio_else_main():
    main = _settings()
    assert main.payment_credentials() == ("x", "x", "x")
    w = _settings(
        investec_write_client_id="wid",
        investec_write_client_secret="wsec",
        investec_write_api_key="wkey",
    )
    assert w.payment_credentials() == ("wid", "wsec", "wkey")
    # payments_enabled (no separate trio) -> the main trio is the payment cred.
    p = _settings(investec_payments_enabled=True)
    assert p.payment_credentials() == ("x", "x", "x")


def test_read_methods_present_and_additive():
    # Read methods still exist with their original signatures (PR5 guard).
    for name in ("get_accounts", "get_balance", "get_transactions", "_get", "_get_token"):
        assert hasattr(InvestecClient, name)
    # Write additions exist.
    assert hasattr(InvestecClient, "_post")
    assert hasattr(InvestecClient, "create_payment")


def test_live_enabled_default_false():
    assert _settings().live_enabled() is False
    assert _settings().payments_dry_run is True


def test_live_enabled_flag_only_stays_dry_run():
    # PR1: flag set but NO write credential -> still dry-run.
    s = _settings(payments_live_enable=True)
    assert s.has_write_credential() is False
    assert s.live_enabled() is False


def test_live_enabled_requires_flag_and_credential():
    s = _settings(
        payments_live_enable=True,
        investec_write_client_id="wid",
        investec_write_client_secret="wsec",
        investec_write_api_key="wkey",
    )
    assert s.has_write_credential() is True
    assert s.live_enabled() is True


def test_credential_without_flag_stays_dry_run():
    s = _settings(
        payments_live_enable=False,
        investec_write_client_id="wid",
        investec_write_client_secret="wsec",
        investec_write_api_key="wkey",
    )
    assert s.live_enabled() is False


def test_settings_load_works_with_only_legacy_env(monkeypatch):
    # F13: legacy env (no new vars) still constructs Settings.
    for k in list(os.environ):
        if k.startswith(("INVESTEC_", "DATABASE_", "SMTP_", "REPORT_", "PAYMENTS_",
                         "APPROVAL_", "PER_PAYMENT", "DAILY_", "IMAP_", "RETENTION",
                         "BACKUP_", "INGEST_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("INVESTEC_CLIENT_ID", "a")
    monkeypatch.setenv("INVESTEC_CLIENT_SECRET", "b")
    monkeypatch.setenv("INVESTEC_API_KEY", "c")
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    s = Settings.load()
    assert s.payments_dry_run is True
    assert s.live_enabled() is False
    assert s.per_payment_cap == 0.0
    assert s.state_dir == ".invespend_state"


def test_signing_secret_from_env_not_hardcoded(monkeypatch):
    monkeypatch.setenv("INVESTEC_CLIENT_ID", "a")
    monkeypatch.setenv("INVESTEC_CLIENT_SECRET", "b")
    monkeypatch.setenv("INVESTEC_API_KEY", "c")
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setenv("APPROVAL_SIGNING_SECRET", "from-env-secret")
    assert Settings.load().approval_signing_secret == "from-env-secret"


# ── F13/F16: additive CLI subcommand ─────────────────────────────────────────
def test_approve_payments_subcommand_registered():
    choices = build_parser()._subparsers._group_actions[0].choices
    assert "approve-payments" in choices


def test_existing_six_subcommands_present():
    choices = build_parser()._subparsers._group_actions[0].choices
    for name in ("init-db", "ingest", "report", "statements", "backup", "backfill-hashes"):
        assert name in choices


def test_approve_payments_headless_json(monkeypatch, capsys, tmp_path):
    from invespend import cli

    class _S:
        state_dir = str(tmp_path)
        investec_client_id = investec_client_secret = investec_api_key = "x"
        investec_base_url = "u"
        report_sender = "from@x"
        retention_days = 90

        def live_enabled(self):
            return False

    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda c: _S()))
    monkeypatch.setattr(
        "invespend.payments.pipeline.run_approval_cycle",
        lambda settings, **kw: {"execution_mode": "dry-run", "processed": 0,
                                "executed": 0, "pending": 0, "parked": 0,
                                "skipped": 0, "results": []},
    )
    monkeypatch.setattr("invespend.investec_client.InvestecClient.__init__",
                        lambda self, *a, **k: None)
    monkeypatch.setattr("invespend.payments.inbox.ImapInbox.__init__",
                        lambda self, *a, **k: None)
    rc = cli.main(["approve-payments"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["execution_mode"] == "dry-run"


def _cli_stub_settings(tmp_path, *, live, write_trio=True):
    class _S:
        state_dir = str(tmp_path)
        investec_client_id = "READ_ID"
        investec_client_secret = "READ_SEC"
        investec_api_key = "READ_KEY"
        investec_write_client_id = "WRITE_ID" if write_trio else ""
        investec_write_client_secret = "WRITE_SEC" if write_trio else ""
        investec_write_api_key = "WRITE_KEY" if write_trio else ""
        investec_base_url = "u"
        report_sender = "from@x"
        retention_days = 90
        payments_beneficiaries_from_api = False

        def live_enabled(self):
            return live

        def _has_write_trio(self):
            return bool(
                self.investec_write_client_id
                and self.investec_write_client_secret
                and self.investec_write_api_key
            )

        def payment_credentials(self):
            if self._has_write_trio():
                return (self.investec_write_client_id,
                        self.investec_write_client_secret,
                        self.investec_write_api_key)
            return (self.investec_client_id,
                    self.investec_client_secret,
                    self.investec_api_key)

    return _S()


def test_live_mode_uses_write_credentials(monkeypatch, capsys, tmp_path):
    # OR-1/RS3/F8: in live mode the WRITE-scoped cred (not the read one) must
    # reach the InvestecClient used for create_payment. No real endpoint hit.
    from invespend import cli

    captured = {}
    monkeypatch.setattr(cli.Settings, "load",
                        classmethod(lambda c: _cli_stub_settings(tmp_path, live=True)))

    def fake_init(self, client_id, client_secret, api_key, base_url, *a, **k):
        captured["args"] = (client_id, client_secret, api_key)

    monkeypatch.setattr("invespend.investec_client.InvestecClient.__init__", fake_init)
    monkeypatch.setattr("invespend.payments.inbox.ImapInbox.__init__",
                        lambda self, *a, **k: None)
    monkeypatch.setattr(
        "invespend.payments.pipeline.run_approval_cycle",
        lambda settings, **kw: {"execution_mode": "live", "processed": 0,
                                "executed": 0, "pending": 0, "parked": 0,
                                "skipped": 0, "results": []},
    )
    rc = cli.main(["approve-payments", "--live"])
    assert rc == 0
    assert captured["args"] == ("WRITE_ID", "WRITE_SEC", "WRITE_KEY")
    out = json.loads(capsys.readouterr().out)
    assert out["live_enabled"] is True
    assert out["requested_mode"] == "live"


def test_dry_run_uses_read_credentials(monkeypatch, capsys, tmp_path):
    from invespend import cli

    captured = {}
    monkeypatch.setattr(cli.Settings, "load",
                        classmethod(lambda c: _cli_stub_settings(tmp_path, live=False)))
    monkeypatch.setattr(
        "invespend.investec_client.InvestecClient.__init__",
        lambda self, cid, csec, akey, burl, *a, **k: captured.__setitem__("args", (cid, csec, akey)),
    )
    monkeypatch.setattr("invespend.payments.inbox.ImapInbox.__init__",
                        lambda self, *a, **k: None)
    monkeypatch.setattr(
        "invespend.payments.pipeline.run_approval_cycle",
        lambda settings, **kw: {"execution_mode": "dry-run", "processed": 0,
                                "executed": 0, "pending": 0, "parked": 0,
                                "skipped": 0, "results": []},
    )
    # --live flag set but gate is closed -> read creds, flag advisory only.
    rc = cli.main(["approve-payments", "--live"])
    assert rc == 0
    assert captured["args"] == ("READ_ID", "READ_SEC", "READ_KEY")
    out = json.loads(capsys.readouterr().out)
    assert out["requested_mode"] == "live"   # advisory value surfaced
    assert out["live_enabled"] is False      # effective gate stays closed


def test_live_mode_uses_main_creds_when_no_write_trio(monkeypatch, capsys, tmp_path):
    # Single-key case: no separate write trio, but payments declared on the main
    # credential. The live client must be built from the MAIN trio.
    from invespend import cli

    captured = {}
    monkeypatch.setattr(
        cli.Settings, "load",
        classmethod(lambda c: _cli_stub_settings(tmp_path, live=True, write_trio=False)),
    )
    monkeypatch.setattr(
        "invespend.investec_client.InvestecClient.__init__",
        lambda self, cid, csec, akey, burl, *a, **k: captured.__setitem__("args", (cid, csec, akey)),
    )
    monkeypatch.setattr("invespend.payments.inbox.ImapInbox.__init__",
                        lambda self, *a, **k: None)
    monkeypatch.setattr(
        "invespend.payments.pipeline.run_approval_cycle",
        lambda settings, **kw: {"execution_mode": "live", "processed": 0,
                                "executed": 0, "pending": 0, "parked": 0,
                                "skipped": 0, "results": [], "beneficiary_source": "static"},
    )
    rc = cli.main(["approve-payments", "--live"])
    assert rc == 0
    assert captured["args"] == ("READ_ID", "READ_SEC", "READ_KEY")
    out = json.loads(capsys.readouterr().out)
    assert out["live_enabled"] is True
    assert out["credential_set"] == "main"
    assert out["beneficiary_source"] == "static"


def test_approve_payments_error_envelope(monkeypatch, capsys, tmp_path):
    # AN1/F16: an injected failure yields a JSON error envelope + stable rc, not
    # an uncaught traceback.
    from invespend import cli

    monkeypatch.setattr(cli.Settings, "load",
                        classmethod(lambda c: _cli_stub_settings(tmp_path, live=False)))
    monkeypatch.setattr("invespend.investec_client.InvestecClient.__init__",
                        lambda self, *a, **k: None)
    monkeypatch.setattr("invespend.payments.inbox.ImapInbox.__init__",
                        lambda self, *a, **k: None)

    def boom(settings, **kw):
        raise RuntimeError("imap unreachable")

    monkeypatch.setattr("invespend.payments.pipeline.run_approval_cycle", boom)
    rc = cli.main(["approve-payments"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "RuntimeError"
    assert "imap unreachable" in out["message"]
    assert out["requested_mode"] == "dry-run"
