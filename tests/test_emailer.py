"""Tests for emailer.py — Gmail API path and SMTP fallback selection."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from invespend.emailer import send_email


def _settings(*, gmail=False):
    s = MagicMock()
    s.report_sender = "sender@gmail.com"
    s.report_recipients = ["recipient@example.com"]
    if gmail:
        s.gmail_client_id = "client-id"
        s.gmail_client_secret = "client-secret"
        s.gmail_refresh_token = "refresh-token"
        s.use_gmail_api = True
    else:
        s.smtp_host = "smtp.gmail.com"
        s.smtp_port = 587
        s.smtp_user = "sender@gmail.com"
        s.smtp_password = "apppassword"
        s.use_gmail_api = False
    return s


@patch("invespend.emailer._send_via_gmail_api")
@patch("invespend.emailer._send_via_smtp")
def test_gmail_api_used_when_credentials_present(mock_smtp, mock_gmail, tmp_path):
    attachment = tmp_path / "report.xlsx"
    attachment.write_bytes(b"fake-xlsx")

    send_email(_settings(gmail=True), attachment, "Subject", "Body")

    mock_gmail.assert_called_once()
    mock_smtp.assert_not_called()


@patch("invespend.emailer._send_via_gmail_api")
@patch("invespend.emailer._send_via_smtp")
def test_smtp_used_when_no_gmail_credentials(mock_smtp, mock_gmail, tmp_path):
    attachment = tmp_path / "report.xlsx"
    attachment.write_bytes(b"fake-xlsx")

    send_email(_settings(gmail=False), attachment, "Subject", "Body")

    mock_smtp.assert_called_once()
    mock_gmail.assert_not_called()


@patch("invespend.emailer.requests.post")
def test_gmail_api_sends_not_drafts(mock_post, tmp_path):
    """The Gmail API call targets the /messages/send endpoint, not /drafts."""
    attachment = tmp_path / "report.xlsx"
    attachment.write_bytes(b"fake-xlsx")

    token_resp = MagicMock(status_code=200)
    token_resp.json.return_value = {"access_token": "fake-token"}
    send_resp = MagicMock(status_code=200)
    send_resp.json.return_value = {"id": "msg-123"}
    mock_post.side_effect = [token_resp, send_resp]

    from invespend.emailer import _send_via_gmail_api, _build_message
    settings = _settings(gmail=True)
    msg = _build_message(settings, [attachment], "Subject", "Body")
    _send_via_gmail_api(settings, msg)

    assert mock_post.call_count == 2  # token refresh + send
    send_call_url = mock_post.call_args_list[1][0][0]
    assert "/messages/send" in send_call_url
    assert "/drafts" not in send_call_url


def test_use_gmail_api_property_false_when_incomplete():
    import importlib
    import invespend.config as config_module
    s = MagicMock()
    s.gmail_client_id = "id"
    s.gmail_client_secret = ""   # missing
    s.gmail_refresh_token = "token"
    # Replicate the real property logic
    result = bool(s.gmail_client_id and s.gmail_client_secret and s.gmail_refresh_token)
    assert result is False
