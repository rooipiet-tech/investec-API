import importlib

import pytest

import invespend.config as config_module
from invespend.config import Settings, signing_secret_is_valid


def _reload_with_env(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    importlib.reload(config_module)
    return config_module.Settings.load()


def test_credentials_are_stripped(monkeypatch):
    # Simulate secrets pasted with trailing newlines / surrounding spaces.
    s = _reload_with_env(
        monkeypatch,
        INVESTEC_CLIENT_ID="cid\n",
        INVESTEC_CLIENT_SECRET="  secret  ",
        INVESTEC_API_KEY="apikey\n",
        DATABASE_URL=" postgresql://u:p@h:6543/postgres?sslmode=require \n",
    )
    assert s.investec_client_id == "cid"
    assert s.investec_client_secret == "secret"
    assert s.investec_api_key == "apikey"
    assert s.database_url == "postgresql://u:p@h:6543/postgres?sslmode=require"


def test_app_password_spaces_removed(monkeypatch):
    s = _reload_with_env(
        monkeypatch,
        INVESTEC_CLIENT_ID="c",
        INVESTEC_CLIENT_SECRET="s",
        INVESTEC_API_KEY="a",
        DATABASE_URL="postgresql://u:p@h/postgres",
        SMTP_PASSWORD="abcd efgh ijkl mnop",
    )
    assert s.smtp_password == "abcdefghijklmnop"


def _settings(**over):
    base = dict(
        investec_client_id="x", investec_client_secret="x", investec_api_key="x",
        investec_base_url="u", database_url="db", smtp_host="h", smtp_port=587,
        smtp_user="u", smtp_password="p", report_sender="from@x",
    )
    base.update(over)
    return Settings(**base)


# ── RS1/F2/F15: placeholder / short signing secret fails CLOSED ──────────────
@pytest.mark.parametrize("bad", [
    "",
    "change_me_long_random_secret",   # .env.example placeholder verbatim
    "short",                          # below the 16-char minimum
])
def test_invalid_signing_secret_rejected(bad):
    assert signing_secret_is_valid(bad) is False
    with pytest.raises(RuntimeError):
        _settings(approval_signing_secret=bad).require_signing_secret()


def test_strong_signing_secret_accepted():
    strong = "a-properly-long-random-secret-value"
    assert signing_secret_is_valid(strong) is True
    assert _settings(approval_signing_secret=strong).require_signing_secret() == strong


def test_load_still_works_with_only_legacy_env(monkeypatch):
    # F13: the field stays optional; the guard only triggers when the payments
    # path actually calls require_signing_secret().
    for k in list(__import__("os").environ):
        if k.startswith(("INVESTEC_", "DATABASE_", "SMTP_", "REPORT_", "PAYMENTS_",
                         "APPROVAL_", "PER_PAYMENT", "DAILY_", "IMAP_", "RETENTION",
                         "BACKUP_", "INGEST_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("INVESTEC_CLIENT_ID", "a")
    monkeypatch.setenv("INVESTEC_CLIENT_SECRET", "b")
    monkeypatch.setenv("INVESTEC_API_KEY", "c")
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    importlib.reload(config_module)
    s = config_module.Settings.load()
    assert s.approval_signing_secret == ""  # optional, absent by default
