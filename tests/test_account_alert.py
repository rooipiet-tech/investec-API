"""Tests for account_alert: group suggestion and unclaimed detection."""
import pytest

import invespend.groups as groups_module
from invespend.groups import Group
from invespend.account_alert import build_alert_email, find_unclaimed, suggest_group


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
        monkeypatch.setattr(groups_module, "GROUPS", [])
        monkeypatch.setattr(groups_module, "EXCLUDE_ACCOUNTS", [])
        accounts = [_account("Alice"), _account("Bob")]
        assert find_unclaimed(accounts, None) == accounts

    def test_claimed_by_name_pattern(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [
            Group(name="personal", recipients=["o@example.com"], name_patterns=["Alice", "Bob"]),
        ])
        monkeypatch.setattr(groups_module, "EXCLUDE_ACCOUNTS", [])
        accounts = [_account("Alice Savings"), _account("Carol")]
        unclaimed = find_unclaimed(accounts, None)
        assert len(unclaimed) == 1
        assert unclaimed[0]["account_name"] == "Carol"

    def test_claimed_by_account_number(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [
            Group(name="personal", recipients=["o@example.com"], account_numbers=["99999"]),
        ])
        monkeypatch.setattr(groups_module, "EXCLUDE_ACCOUNTS", [])
        accounts = [_account("Some Account", number="99999"), _account("Other", number="11111")]
        unclaimed = find_unclaimed(accounts, None)
        assert len(unclaimed) == 1
        assert unclaimed[0]["account_name"] == "Other"

    def test_excluded_account_not_returned(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [])
        monkeypatch.setattr(groups_module, "EXCLUDE_ACCOUNTS", ["fin5"])
        accounts = [_account("fin5 savings"), _account("Regular Account")]
        unclaimed = find_unclaimed(accounts, None)
        assert len(unclaimed) == 1
        assert unclaimed[0]["account_name"] == "Regular Account"

    def test_empty_accounts_list(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [])
        monkeypatch.setattr(groups_module, "EXCLUDE_ACCOUNTS", [])
        assert find_unclaimed([], None) == []


class TestSuggestGroup:
    def test_matches_existing_group_by_token(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [
            Group(name="canvas", recipients=["c@example.com"],
                  name_patterns=["Canvas Intelligence", "Canvas Kopano"]),
            Group(name="personal", recipients=["p@example.com"],
                  name_patterns=["JP van Zyl", "Ella"]),
        ])
        acc = _account("Canvas Intelligence Private")
        assert suggest_group(acc) == "canvas"

    def test_personal_heuristic_no_groups(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [])
        acc = _account("Alice Private Bank Current Account", product="Private Bank Current Account")
        assert suggest_group(acc) == "personal"

    def test_business_heuristic_no_groups(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [])
        acc = _account("Acme Consulting Pty Ltd", product="Business Current Account")
        assert suggest_group(acc) == "business"

    def test_no_suggestion_generic_account(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [])
        acc = _account("Generic Account", product="Account")
        assert suggest_group(acc) is None


class TestBuildAlertEmail:
    def test_subject_contains_count(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [])
        monkeypatch.setattr(groups_module, "EXCLUDE_ACCOUNTS", [])
        accounts = [_account("Alice"), _account("Bob")]
        subject, _ = build_alert_email(accounts, None)
        assert "2" in subject

    def test_body_lists_account_details(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [])
        monkeypatch.setattr(groups_module, "EXCLUDE_ACCOUNTS", [])
        accounts = [_account("Alice", number="9876543210")]
        _, body = build_alert_email(accounts, None)
        assert "Alice" in body
        assert "9876543210" in body

    def test_body_includes_groups_py_instructions_when_unclaimed(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [])
        monkeypatch.setattr(groups_module, "EXCLUDE_ACCOUNTS", [])
        accounts = [_account("NewCo Pty Ltd")]
        _, body = build_alert_email(accounts, None)
        assert "groups.py" in body

    def test_body_shows_claimed_status_when_group_matches(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [
            Group(name="personal", recipients=["o@example.com"], name_patterns=["Alice"]),
        ])
        monkeypatch.setattr(groups_module, "EXCLUDE_ACCOUNTS", [])
        accounts = [_account("Alice Savings")]
        _, body = build_alert_email(accounts, None)
        assert "claimed" in body
        assert "groups.py" not in body

    def test_no_accounts_empty_subject_count(self, monkeypatch):
        monkeypatch.setattr(groups_module, "GROUPS", [])
        monkeypatch.setattr(groups_module, "EXCLUDE_ACCOUNTS", [])
        subject, _ = build_alert_email([], None)
        assert "0" in subject
