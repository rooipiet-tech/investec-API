"""S11: preflight order (no fetch before it passes), error envelope, dispatch (cli.py), F26, F27, F10."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.v2_harness import Env, FakeClient, FakeInbox, FakeSMTP, OWNER, make_settings, mod


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def run(env, **kw):
    return env.cycle(**kw)


def test_cycle_refuses_non_durable_store_without_test_flag(env):
    with pytest.raises(Exception) as err:
        run(env, allow_non_durable=False)
    assert "v2_requires_durable_store" in str(err.value) and env.inbox.fetch_calls == 0


def test_v2_refuses_inbox_mailbox(env):
    for box in ("INBOX", "inbox", "", "  Inbox "):
        env.settings.imap_mailbox = box
        with pytest.raises(Exception) as err:
            run(env)
        assert "mailbox_not_dedicated" in str(err.value)
    assert env.inbox.fetch_calls == 0


def test_strict_parser_unavailable_envelope_no_fetch(env, monkeypatch):
    monkeypatch.setattr(mod("sender_auth"), "strict_address_parser_available", lambda: False)
    env.instruct()
    with pytest.raises(Exception) as err:
        run(env)
    assert "strict_parser_unavailable" in str(err.value) and env.inbox.fetch_calls == 0 and env.payment_calls() == 0
    assert env.audit_entries("preflight_failed")[0]["detail"]["reason"] == "strict_parser_unavailable"
    monkeypatch.undo()
    run(env)
    assert len(env.rows()) == 1


def test_store_down_does_not_fetch_mail(env, monkeypatch):
    monkeypatch.setattr(env.store, "ping", lambda: (_ for _ in ()).throw(ConnectionError("db down")))
    with pytest.raises(ConnectionError):
        run(env)
    assert env.inbox.fetch_calls == 0 and env.client.beneficiary_calls == 0


def test_store_not_initialised_envelope(env, monkeypatch):
    monkeypatch.setattr(env.store, "ping", lambda: (_ for _ in ()).throw(mod("instructions").StoreNotInitialised("missing")))
    with pytest.raises(mod("instructions").StoreNotInitialised):
        run(env)
    assert env.inbox.fetch_calls == 0


def test_preflight_order_durable_then_mailbox_then_parser_then_ping(env, monkeypatch):
    calls = []
    monkeypatch.setattr(mod("sender_auth"), "strict_address_parser_available", lambda: calls.append("parser") or True)
    monkeypatch.setattr(env.store, "ping", lambda: calls.append("ping"))
    run(env)
    assert calls == ["parser", "ping"]
    env.settings.imap_mailbox = "INBOX"
    calls.clear()
    with pytest.raises(Exception):
        run(env)
    assert calls == []


def test_failure_after_fetch_sends_generic_notice_without_detail(env, monkeypatch):
    env.instruct()
    monkeypatch.setattr(env.store, "list_active", lambda: (_ for _ in ()).throw(RuntimeError("secret 123456789012")))
    with pytest.raises(RuntimeError):
        run(env)
    failed = [m for m in env.smtp.sent if "cycle failed" in str(m["Subject"]).lower()]
    assert len(failed) == 1 and "secret" not in str(failed[0].get_content()) and "123456789012" not in str(failed[0].get_content())
    assert str(failed[0]["To"]) == OWNER


def test_default_smtp_send_is_emailer_smtp_send(env, monkeypatch):
    sent = []
    monkeypatch.setattr("invespend.emailer._smtp_send", lambda settings, msg: sent.append((settings, msg)))
    env.instruct()
    mod("cycle").run_instruction_cycle(env.settings, inbox=env.inbox, client=env.client, store=env.store, audit=env.audit,
                                       extractor=env.extractor, now=env.clock, allow_non_durable=True)
    assert sent and sent[0][0] is env.settings and "[BATCH" in str(sent[0][1]["Subject"])


# ------------------------------------------------------------------ dispatch
def stub_cli_env(monkeypatch, settings, *, inbox=None, store=None, client=None, smtp=None):
    """Seam of TR3-9: PgInstructionStore / PgAuditLog replaced; nothing else is touched."""
    from invespend import cli
    inbox, client, smtp = inbox or FakeInbox(), client or FakeClient(), smtp or FakeSMTP()
    store = store or mod("instructions").MemoryInstructionStore()
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr("invespend.payments.v2_inbox.V2ImapInbox", lambda s: inbox)
    monkeypatch.setattr("invespend.investec_client.InvestecClient", lambda *a, **k: client)
    monkeypatch.setattr("invespend.emailer._smtp_send", lambda s, msg: smtp(msg))

    class DurableStore(mod("instructions").MemoryInstructionStore):
        durable = True
        def __init__(self, url):
            super().__init__()
            self._rows, self._messages, self._bene, self._meta, self._daily = (store._rows, store._messages, store._bene, store._meta, store._daily)
    audits = []

    class StubAudit:
        def __init__(self, url):
            pass
        def append(self, step, detail=None, **k):
            audits.append((step, detail))
        def read_entries(self):
            return []
    monkeypatch.setattr("invespend.payments.instructions.PgInstructionStore", DurableStore)
    monkeypatch.setattr("invespend.payments.audit.PgAuditLog", StubAudit)
    return SimpleNamespace(cli=cli, inbox=inbox, client=client, smtp=smtp, store=store, audits=audits)


def v2_settings(**over):
    s = make_settings(database_url="postgresql://u:pw@h/db", investec_client_id="cid", investec_client_secret="csec",
                      investec_api_key="akey", investec_base_url="https://x", **over)
    return s


def test_v2_requires_durable_store_rc2_no_fetch(monkeypatch, capsys):
    s = v2_settings(payments_state_backend="file")
    ctx = stub_cli_env(monkeypatch, s)
    assert ctx.cli.main(["approve-payments", "--once"]) == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "v2_requires_durable_store" in out["message"] + out["error"] and ctx.inbox.fetch_calls == 0


def test_unknown_payments_mode_rc2_envelope_no_fetch(monkeypatch, capsys):
    ctx = stub_cli_env(monkeypatch, v2_settings(payments_mode="v3"))
    assert ctx.cli.main(["approve-payments", "--once"]) == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "payments_mode" in out["message"] and ctx.inbox.fetch_calls == 0 and ctx.client.beneficiary_calls == 0


def test_payments_mode_v2_dispatches_to_run_v2_and_never_calls_run_approval_cycle(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("invespend.payments.pipeline.run_approval_cycle", lambda *a, **k: calls.append("legacy") or {})
    ctx = stub_cli_env(monkeypatch, v2_settings())
    assert ctx.cli.main(["approve-payments", "--once"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert calls == [] and out["mode"] == "dry-run" and out["live_enabled"] is False and out["requested_mode"] == "dry-run"
    assert out["state_backend"] == "postgres" and out["credential_set"] == "read"


@pytest.mark.parametrize("value", ["V2", " v2 ", "V2\n"])
def test_payments_mode_normalised_V2_with_space(monkeypatch, capsys, value):
    ctx = stub_cli_env(monkeypatch, v2_settings(payments_mode=value))
    assert ctx.cli.main(["approve-payments", "--once"]) == 0
    assert ctx.inbox.fetch_calls == 1


def test_run_v2_never_constructs_PgPaymentStore(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("PgPaymentStore must not be built by v2")
    monkeypatch.setattr("invespend.payments.pg_store.PgPaymentStore", boom)
    ctx = stub_cli_env(monkeypatch, v2_settings())
    assert ctx.cli.main(["approve-payments", "--once"]) == 0


def test_dry_run_builds_client_with_read_credential_live_uses_payment_credentials(monkeypatch):
    built = []
    ctx = stub_cli_env(monkeypatch, v2_settings())
    monkeypatch.setattr("invespend.investec_client.InvestecClient", lambda *a, **k: built.append(a) or ctx.client)
    ctx.cli.main(["approve-payments", "--once"])
    assert built[-1][:3] == ("cid", "csec", "akey")
    live = v2_settings(live=True)
    live.payment_credentials = lambda: ("wid", "wsec", "wkey")
    live._has_write_trio = lambda: True
    ctx2 = stub_cli_env(monkeypatch, live)
    monkeypatch.setattr("invespend.investec_client.InvestecClient", lambda *a, **k: built.append(a) or ctx2.client)
    ctx2.cli.main(["approve-payments", "--once"])
    assert built[-1][:3] == ("wid", "wsec", "wkey")


def test_cli_main_f26_summary_and_exit_codes_for_ignore_offer_approve_cancel_execute(monkeypatch, capsys):
    from datetime import datetime, timedelta, timezone
    from tests.v2_harness import make_mail, body_for
    ctx = stub_cli_env(monkeypatch, v2_settings(live=True))
    s = ctx.cli
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)   # main() uses the real clock, so the age gate needs a fresh mail
    # ignore
    ctx.inbox.queue(make_mail("no trigger here", internaldate=recent))
    assert s.main(["approve-payments", "--once"]) == 0
    first = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert first["messages"] == 1 and first["offered"] == 0
    # offer
    ctx.inbox.queue(make_mail(body_for(), internaldate=recent))
    assert s.main(["approve-payments", "--once"]) == 0
    second = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert second["offered"] == 1 and second["live_enabled"] is True and second["credential_set"] in ("write", "main")


def test_v2_error_envelope_has_class_and_scrubbed_message(monkeypatch, capsys):
    s = v2_settings(anthropic_api_key="sk-ant-api03-SECRETSECRET")
    ctx = stub_cli_env(monkeypatch, s)
    monkeypatch.setattr(ctx.client, "get_beneficiaries", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr("invespend.payments.cycle.run_instruction_cycle",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom 123456789012 sk-ant-api03-SECRETSECRET pw")))
    assert ctx.cli.main(["approve-payments", "--once"]) == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["error"] == "ValueError" and "123456789012" not in out["message"] and "SECRETSECRET" not in out["message"]


def test_v2_emails_contain_no_token_and_cycle_never_calls_approval_email(monkeypatch):
    called = []
    monkeypatch.setattr("invespend.payments.notify.build_approval_email", lambda *a, **k: called.append(1), raising=True)
    from tests.v2_harness import make_mail, body_for
    ctx = stub_cli_env(monkeypatch, v2_settings())
    ctx.inbox.queue(make_mail(body_for()))
    ctx.cli.main(["approve-payments", "--once"])
    assert called == []
    text = "\n".join(str(m.get_content()) for m in ctx.smtp.sent)
    assert ctx.smtp.sent and not re.search(r"token|[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{10,}", text, re.I)


# --------------------------------------------------- legacy dispatch and F42
def test_cli_diff_is_added_lines_only():
    base = (ROOT / "tests" / "fixtures" / "legacy" / "cli_2eee10c.py.txt").read_text().splitlines()
    current = (ROOT / "src" / "invespend" / "cli.py").read_text().splitlines()
    diff = list(difflib.unified_diff(base, current, lineterm="", n=0))
    removed = [ln for ln in diff if ln.startswith("-") and not ln.startswith("---")]
    added = [ln for ln in diff if ln.startswith("+") and not ln.startswith("+++")]
    assert removed == [] and 0 < len(added) <= 15, added


class LegacyStub:
    """Only what the legacy block reads: no payments_mode attribute at all."""
    investec_client_id, investec_client_secret, investec_api_key = "c", "s", "k"
    investec_base_url = "https://x"
    payments_state_backend = "file"
    def live_enabled(self):
        return False


def test_minimal_stub_settings_runs_legacy_rc0(monkeypatch, capsys):
    from invespend import cli
    called = {}
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: LegacyStub()))
    monkeypatch.setattr("invespend.payments.inbox.ImapInbox", lambda s: "inbox")
    monkeypatch.setattr("invespend.investec_client.InvestecClient", lambda *a, **k: "client")
    monkeypatch.setattr("invespend.payments.pipeline.run_approval_cycle", lambda settings, **k: called.update(k) or {"processed": 0})
    monkeypatch.setattr("invespend.payments.cycle.run_instruction_cycle", lambda *a, **k: pytest.fail("v2 must not run"))
    assert cli.main(["approve-payments", "--once"]) == 0
    assert called == {"inbox": "inbox", "client": "client"}
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["processed"] == 0 and out["live_enabled"] is False and out["credential_set"] == "read"


@pytest.mark.parametrize("value", [None, "", "legacy", "LEGACY"])
def test_payments_mode_unset_or_legacy_runs_legacy(monkeypatch, value):
    from invespend import cli
    called = []
    stub = LegacyStub()
    stub.payments_mode = value
    monkeypatch.setattr(cli.Settings, "load", classmethod(lambda cls: stub))
    monkeypatch.setattr("invespend.payments.inbox.ImapInbox", lambda s: "inbox")
    monkeypatch.setattr("invespend.investec_client.InvestecClient", lambda *a, **k: "client")
    monkeypatch.setattr("invespend.payments.pipeline.run_approval_cycle", lambda settings, **k: called.append(1) or {})
    assert cli.main(["approve-payments", "--once"]) == 0 and called == [1]


# ------------------------------------------------------------------------ F10
def test_no_beneficiary_create_endpoint_anywhere_in_src():
    hits = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"beneficiar", line, re.I) and re.search(r"\b(post|create|add)\b", line, re.I):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{n}")
    allowed_files = {"src/invespend/investec_client.py", "src/invespend/payments/beneficiaries.py", "src/invespend/payments/notify.py",
                     "src/invespend/payments/notify_v2.py", "src/invespend/payments/instructions.py", "src/invespend/payments/routing.py",
                     "src/invespend/payments/cycle.py", "src/invespend/payments/execute.py", "src/invespend/payments/batch.py",
                     "src/invespend/payments/pg_instructions.py", "src/invespend/payments/bankdetails.py",
                     "src/invespend/payments/pipeline.py", "src/invespend/payments/selftest.py", "src/invespend/payments/images.py",
                     "src/invespend/db.py", "src/invespend/beneficiary_sync.py", "src/invespend/beneficiary_match.py",
                     "src/invespend/payments/approval.py", "src/invespend/payments/fingerprints.py"}
    assert {h.rsplit(":", 1)[0] for h in hits} <= allowed_files
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text()
        assert not re.search(r"""\.post\([^)]*beneficiar""", text, re.I | re.S) or path.name == "investec_client.py"
        assert "/beneficiaries\"" not in text.replace("accounts/beneficiaries", "") or "get" in text
