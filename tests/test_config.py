import importlib

import pytest

import invespend.config as config_module


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


def test_investec_creds_only_required_for_api_commands(monkeypatch):
    # The report/backup jobs run without banking credentials injected at all;
    # Settings must load, and only require_investec() should complain.
    for var in ("INVESTEC_CLIENT_ID", "INVESTEC_CLIENT_SECRET", "INVESTEC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/postgres")
    importlib.reload(config_module)
    s = config_module.Settings.load()
    assert s.investec_client_id == ""
    with pytest.raises(RuntimeError, match="INVESTEC_CLIENT_ID"):
        s.require_investec()


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
