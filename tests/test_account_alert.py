"""Tests for account_alert: group suggestion and unclaimed detection."""
import importlib

import pytest

import invespend.config as config_module
from invespend.account_alert import build_alert_email, find_unclaimed, suggest_group


def _settings(monkeypatch, groups: dict | None = None, recipients="owner@example.com"):
    env = {
        "INVESTEC_CLIENT_ID": "cid",
        "INVESTEC_CLIENT_SECRET": "secret",
        "INVESTEC_API_KEY": "apikey",
        "DATABASE_URL": "postgresql://u:p@h/postgres",
        "REPORT_RECIPIENTS": recipients,
    }
    if groups:
        for n, (name, accounts, recips) in enumerate(groups, 1):
            env[f"REPORT_GROUP_{n}_NAME"] = name
            env[f"REPORT_GROUP_{n}_ACCOUNTS"] = accounts
            env[f"REPORT_GROUP_{n}_RECIPIENTS"] = recips
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    importlib.reload(config_module)
    return config_module.Settings.load()


def _account(name, number="12345", product="Private Bank Current Account", ref=None):
    return {
        "account_id": f"id_{name}",
        "account_name": name,
        "account_number": number,
        "product_name": product,
        "reference_name": ref or name,
        "profile_id": "profile1",
    }


class TestFindUnclaimed:
    def test_no_groups_all_unclaimed(self, monkeypatch):
        settings = _settings(monkeypatch)
        accounts = [_account("Alice"), _account("Bob")]
        assert find_unclaimed(accounts, settings) == accounts

    def test_claimed_by_group(self, monkeypatch):
        settings = _settings(
            monkeypatch,
            groups=[("personal", "Alice,Bob", "owner@example.com")],
        )
        accounts = [_account("Alice Savings"), _account("Carol")]
        unclaimed = find_unclaimed(accounts, settings)
        assert len(unclaimed) == 1
        assert unclaimed[0]["account_name"] == "Carol"

    def test_excluded_account_not_returned(self, monkeypatch):
        monkeypatch.setenv("REPORT_EXCLUDE_ACCOUNTS", "fin5")
        settings = _settings(monkeypatch)
        accounts = [_account("fin5 savings"), _account("Regular Account")]
        unclaimed = find_unclaimed(accounts, settings)
        assert len(unclaimed) == 1
        assert unclaimed[0]["account_name"] == "Regular Account"

    def test_empty_accounts_list(self, monkeypatch):
        settings = _settings(monkeypatch)
        assert find_unclaimed([], settings) == []


class TestSuggestGroup:
    def test_matches_existing_group_by_token(self, monkeypatch):
        settings = _settings(
            monkeypatch,
            groups=[
                ("canvas", "Canvas Intelligence,Canvas Kopano", "canvas@example.com"),
                ("personal", "JP van Zyl,Ella", "personal@example.com"),
            ],
        )
        acc = _account("Canvas Intelligence Private")
        suggestion = suggest_group(acc, settings)
        assert suggestion == "canvas"

    def test_personal_heuristic_no_groups(self, monkeypatch):
        settings = _settings(monkeypatch)
        acc = _account("Alice Private Bank Current Account", product="Private Bank Current Account")
        suggestion = suggest_group(acc, settings)
        assert suggestion == "personal"

    def test_business_heuristic_no_groups(self, monkeypatch):
        settings = _settings(monkeypatch)
        acc = _account("Acme Consulting Pty Ltd", product="Business Current Account")
        suggestion = suggest_group(acc, settings)
        assert suggestion == "business"

    def test_no_suggestion_generic_account(self, monkeypatch):
        settings = _settings(monkeypatch)
        acc = _account("Generic Account", product="Account")
        suggestion = suggest_group(acc, settings)
        assert suggestion is None


class TestBuildAlertEmail:
    def test_subject_contains_count(self, monkeypatch):
        settings = _settings(monkeypatch)
        accounts = [_account("Alice"), _account("Bob")]
        subject, _ = build_alert_email(accounts, settings)
        assert "2" in subject

    def test_body_lists_account_details(self, monkeypatch):
        settings = _settings(monkeypatch)
        accounts = [_account("Alice", number="9876543210")]
        _, body = build_alert_email(accounts, settings)
        assert "Alice" in body
        assert "9876543210" in body

    def test_body_includes_group_instructions_when_unclaimed(self, monkeypatch):
        settings = _settings(monkeypatch)
        accounts = [_account("NewCo Pty Ltd")]
        _, body = build_alert_email(accounts, settings)
        assert "REPORT_GROUP_" in body

    def test_body_shows_claimed_status_when_group_matches(self, monkeypatch):
        settings = _settings(
            monkeypatch,
            groups=[("personal", "Alice", "owner@example.com")],
        )
        accounts = [_account("Alice Savings")]
        _, body = build_alert_email(accounts, settings)
        assert "claimed" in body
        assert "REPORT_GROUP_" not in body

    def test_no_accounts_raises_nothing(self, monkeypatch):
        settings = _settings(monkeypatch)
        subject, body = build_alert_email([], settings)
        assert "0" in subject
