"""S13: the payments runbook, README section and .env.example cover the required topics."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "PAYMENTS_RUNBOOK.md"

pytestmark = pytest.mark.xfail(reason="S13 red: docs not written yet", strict=False)


@pytest.fixture(scope="module")
def runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _has(text: str, *needles: str) -> None:
    low = text.lower()
    for n in needles:
        assert n.lower() in low, n


@pytest.mark.parametrize("heading", [
    "Rollout gates", "Trigger grammar", "Sender authentication", "Routing paths", "Batch approval flow",
    "New-beneficiary hold", "Caps", "Images", "Environment variables", "Operational notes",
    "DEPRECATED", "Credentials",
])
def test_runbook_has_each_topic_heading(runbook, heading):
    assert re.search(r"^#{1,3} .*" + re.escape(heading), runbook, re.M | re.I), heading


def test_trigger_grammar(runbook):
    _has(runbook, "pay NNN", "digits only", "last 3")


def test_batch_flow_command_forms_and_reply_placement(runbook):
    _has(runbook, "`approve`", "`approve 1 3`", "`cancel 2`", "bare `cancel`", "above the quoted text",
         "NEXT cycle", "still pending", "will still run", "unless cancelled", "not-understood",
         "quote-stripped", "24h", "late approve", "keep the ref in the subject")


def test_expiry_hold_and_f41_caveats(runbook):
    _has(runbook, "PAYMENTS_HOLD_HOURS", "default 0", "NOT in the official swagger",
         "paid once in Investec Online first", "failed", "nothing is resent", "fresh instruction")


def test_caps_fail_closed_and_caveat(runbook):
    _has(runbook, "R20,000", "R50,000", "fail closed", "community", "unverified", "blocks payments")


def test_rollout_gates_and_live_off(runbook):
    _has(runbook, "G1", "G2", "G3", "14", "sandbox", "PAYMENTS_LIVE_ENABLE", "never enabled by default")


def test_image_engine_setup_and_popia(runbook):
    _has(runbook, "ANTHROPIC_API_KEY", "IMAGE_EXTRACTOR_MODEL", "api.anthropic.com", "POPIA", "figures from: image",
         "skipped", "never a command")


def test_operational_notes(runbook):
    _has(runbook, "0010", "row level security", "offset", "15s", "dry-run reservations",
         "payment_daily_total", "MAY HAVE BEEN PAID", "PAYMENTS_STATE_BACKEND=postgres", "init-db",
         "Authentication-Results", "preflight", "dedicated mailbox", "marks mail as read",
         "paste", "unregistered", "Playwright", "separate later project", "daily cap")


def test_docs_mark_legacy_deprecated(runbook):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for text in (runbook, readme):
        assert re.search(r"DEPRECATED[^\n]*legacy|legacy[^\n]*DEPRECATED", text, re.I)
        assert "PAYMENTS_MODE=v2" in text


def test_readme_links_the_runbook_and_keeps_live_off():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/PAYMENTS_RUNBOOK.md" in readme and "dry-run" in readme.lower()


def test_deploy_md_points_to_the_runbook():
    assert "PAYMENTS_RUNBOOK.md" in (ROOT / "DEPLOY.md").read_text(encoding="utf-8")


def test_env_example_lists_new_settings_without_values_for_secrets_or_model():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "ANTHROPIC_API_KEY=" in lines and "IMAGE_EXTRACTOR_MODEL=" in lines
    assert "PAYMENTS_HOLD_HOURS=0" in lines and "PAYMENTS_MODE=v2" in lines
    assert "PAYMENTS_FINGERPRINT_KEY=" in lines
    text = "\n".join(lines)
    assert "R20,000" in text and "PER_PAYMENT_CAP=20000" in lines and "DAILY_AGGREGATE_CAP=50000" in lines
    assert not re.search(r"sk-ant|claude-|sonnet|haiku|opus", text, re.I)
    assert "INVESTEC_PAYMENTS_ENABLED=false" in lines and "PAYMENTS_LIVE_ENABLE=false" in lines


def test_env_var_table_covers_every_documented_setting(runbook):
    for name in ("PAYMENTS_MODE", "PAYMENTS_STATE_BACKEND", "PAYMENTS_ALLOWED_SENDERS", "PAYMENTS_AUTHSERV_ID",
                 "PAYMENTS_FINGERPRINT_KEY", "PER_PAYMENT_CAP", "DAILY_AGGREGATE_CAP", "PAYMENTS_HOLD_HOURS",
                 "PAYMENTS_APPROVAL_EXPIRY_HOURS", "PAYMENTS_MAX_MESSAGE_AGE_HOURS", "PAYMENTS_MAX_IMAGE_BYTES",
                 "IMAGE_EXTRACTOR_MODEL", "ANTHROPIC_API_KEY", "PAYMENTS_LIVE_ENABLE", "IMAP_MAILBOX",
                 "PAYMENTS_CYCLE_ENABLED"):
        assert re.search(r"^\|\s*`" + name + r"`", runbook, re.M), name


def test_docs_contain_no_secret_shaped_values(runbook):
    for text in (runbook, (ROOT / "README.md").read_text(encoding="utf-8")):
        assert not re.search(r"sk-ant-[A-Za-z0-9]", text)
