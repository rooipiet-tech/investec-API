"""S12: the payments-cycle workflow invariants (parsed, not grepped). No network."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests import yaml_lite

ROOT = Path(__file__).resolve().parent.parent
WF_DIR = ROOT / ".github" / "workflows"
WF = WF_DIR / "payments-cycle.yml"


@pytest.fixture(scope="module")
def wf() -> dict:
    return yaml_lite.load(WF.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def job(wf) -> dict:
    jobs = wf["jobs"]
    assert len(jobs) == 1
    return next(iter(jobs.values()))


def _all_envs(wf, job):
    yield "workflow", wf.get("env") or {}
    yield "job", job.get("env") or {}
    for step in job["steps"]:
        yield "step", step.get("env") or {}


def test_trigger_schedule_dispatch_and_nothing_else(wf):
    on = wf["on"]
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert len(on["schedule"]) == 1


def test_no_pull_request_trigger(wf):
    text = WF.read_text(encoding="utf-8")
    assert "pull_request" not in text and "pull_request_target" not in text and "workflow_run" not in text


def test_cron_is_fifteen_minutes_and_offset_from_ingest(wf):
    cron = wf["on"]["schedule"][0]["cron"]
    minutes, *rest = cron.split()
    assert rest == ["*", "*", "*", "*"]
    mins = sorted(int(m) for m in minutes.split(","))
    assert len(mins) == 4 and [b - a for a, b in zip(mins, mins[1:])] == [15, 15, 15]
    ingest = yaml_lite.load((WF_DIR / "ingest.yml").read_text(encoding="utf-8"))
    ing_min = int(ingest["on"]["schedule"][0]["cron"].split()[0])
    assert ing_min not in mins and all(abs(m - ing_min) >= 5 for m in mins)   # never the ingest minute (0010 RLS lock)


def test_concurrency_never_cancels_and_timeout_set(wf, job):
    assert wf["concurrency"] == {"group": "payments-cycle", "cancel-in-progress": "false"}
    assert 0 < int(job["timeout-minutes"]) <= 15


def test_minimal_permissions(wf, job):
    assert wf["permissions"] == {"contents": "read"} and "permissions" not in job


def test_job_gated_on_PAYMENTS_CYCLE_ENABLED(job):
    assert job["if"].replace(" ", "") == "${{vars.PAYMENTS_CYCLE_ENABLED=='true'}}"


def test_job_uses_environment_payments(job):
    assert job["environment"] == "payments"


def test_state_backend_is_postgres(job):
    assert job["env"]["PAYMENTS_STATE_BACKEND"] == "postgres"


def test_workflow_pins_v2_and_never_invokes_legacy(wf, job):
    assert job["env"]["PAYMENTS_MODE"] == "v2"
    assert (wf.get("env") or {}).get("PAYMENTS_MODE") is None
    for step in job["steps"]:
        assert "PAYMENTS_MODE" not in (step.get("env") or {})
    runs = [s.get("run", "") for s in job["steps"]]
    assert any('"$PAYMENTS_MODE" = "v2"' in r and "test" in r for r in runs)
    assert any(r.strip().startswith("invespend approve-payments") and "--once" in r for r in runs)
    assert not any("--live" in r for r in runs)
    for other in WF_DIR.glob("*.yml"):
        if other.name != WF.name:
            assert "approve-payments" not in other.read_text(encoding="utf-8"), other.name


def test_guard_step_runs_before_the_cycle(job):
    runs = [s.get("run", "") for s in job["steps"]]
    guard = next(i for i, r in enumerate(runs) if '"$PAYMENTS_MODE" = "v2"' in r)
    cycle = next(i for i, r in enumerate(runs) if "approve-payments" in r)
    assert guard < cycle


def test_workflow_python_version_is_3_12_not_an_old_patch(job):
    versions = [s["with"]["python-version"] for s in job["steps"] if "setup-python" in s.get("uses", "")]
    assert versions == ["3.12"]


def test_INVESTEC_PAYMENTS_ENABLED_is_not_true(wf, job):
    truthy = {"true", "1", "yes", "on"}
    text = WF.read_text(encoding="utf-8")
    assert "INVESTEC_PAYMENTS_ENABLED" not in text
    for _level, env in _all_envs(wf, job):
        for key in ("INVESTEC_PAYMENTS_ENABLED", "PAYMENTS_LIVE_ENABLE", "PAYMENTS_ENABLED"):
            assert str(env.get(key, "")).strip().lower() not in truthy


def test_no_default_in_workflow_turns_live_on(job):
    live = job["env"]["PAYMENTS_LIVE_ENABLE"]
    assert live == "${{ vars.PAYMENTS_LIVE_ENABLE || 'false' }}"
    assert "PAYMENTS_DRY_RUN" not in job["env"] or job["env"]["PAYMENTS_DRY_RUN"].lower() != "false"
    text = WF.read_text(encoding="utf-8")
    assert not re.search(r"PAYMENTS_LIVE_ENABLE\s*:\s*[\"']?true", text, re.I)
    assert "|| 'true'" not in text and '|| "true"' not in text


def test_workflow_anthropic_key_only_via_secrets_context_and_model_via_vars(wf, job):
    text = WF.read_text(encoding="utf-8")
    assert job["env"]["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    assert job["env"]["IMAGE_EXTRACTOR_MODEL"] == "${{ vars.IMAGE_EXTRACTOR_MODEL }}"
    for m in re.finditer(r"ANTHROPIC_API_KEY", text):
        line = text[text.rfind("\n", 0, m.start()) + 1: text.find("\n", m.start())]
        assert "${{ secrets.ANTHROPIC_API_KEY }}" in line, line
    assert not re.search(r"sk-ant|claude-|sonnet|haiku|opus", text, re.I)


def test_secrets_come_from_secrets_context_never_literals(job):
    sensitive = re.compile(r"(PASSWORD|SECRET|API_KEY|FINGERPRINT_KEY|DATABASE_URL|CLIENT_ID)$")
    for key, value in job["env"].items():
        if sensitive.search(key):
            assert re.fullmatch(r"\$\{\{ secrets\.[A-Z0-9_]+ \}\}", value), key


def test_caps_and_senders_come_from_vars(job):
    env = job["env"]
    assert env["PER_PAYMENT_CAP"] == "${{ vars.PER_PAYMENT_CAP }}"
    assert env["DAILY_AGGREGATE_CAP"] == "${{ vars.DAILY_AGGREGATE_CAP }}"
    assert env["PAYMENTS_ALLOWED_SENDERS"] == "${{ vars.PAYMENTS_ALLOWED_SENDERS }}"
    assert "PAYMENTS_HOLD_HOURS" not in env or env["PAYMENTS_HOLD_HOURS"].startswith("${{ vars.")


def test_dry_run_is_the_command_default(job):
    cycle = next(s["run"] for s in job["steps"] if "approve-payments" in s.get("run", ""))
    assert "--dry-run" in cycle and "--live" not in cycle


def test_dotenv_is_git_ignored():
    r = subprocess.run(["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0
