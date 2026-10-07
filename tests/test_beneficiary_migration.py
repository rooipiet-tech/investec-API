"""Static checks on db/migrations/0012_beneficiary_matching.sql (no live DB)."""
import hashlib
import json
import re
from pathlib import Path

from invespend import db

MIGRATIONS = db.MIGRATIONS_DIR
M0012 = MIGRATIONS / "0012_beneficiary_matching.sql"
FIXTURES = Path(__file__).parent / "fixtures" / "beneficiary"

NEW_TABLES = {"beneficiaries", "beneficiary_matches"}
NEW_VIEWS = {"transactions_beneficiary"}
EXISTING_OBJECTS = {
    "transactions", "accounts", "balances", "sync_runs", "category_map",
    "transactions_backup_20260615", "payment_pending", "payment_daily_total",
    "payment_audit", "payment_approvals",
    "transactions_normalized", "transactions_categorized", "spend_by_category",
    "monthly_movement", "balance_reconciliation", "transactions_flow", "monthly_flows",
}

VIEW_COLUMNS = [
    "transaction_hash", "account_id", "effective_date", "description", "amount",
    "type", "transaction_type", "flow_type", "match_status", "beneficiary_id",
    "beneficiary_name", "beneficiary_alt_name", "beneficiary_reference",
    "beneficiary_bank", "beneficiary_branch_code", "beneficiary_account_number",
    "beneficiary_active", "match_rule", "matched_token", "candidate_count",
    "snapshot_at", "matched_at",
]


def _sql() -> str:
    """0012 with `--` comments stripped, lower-cased, whitespace collapsed."""
    lines = [ln.split("--", 1)[0] for ln in M0012.read_text().splitlines()]
    return " ".join(" ".join(lines).lower().split())


def _statements() -> list[str]:
    return [s.strip() for s in _sql().split(";") if s.strip()]


def _table_ddl(name: str) -> str:
    for s in _statements():
        if s.startswith(f"create table if not exists {name} "):
            return s
    raise AssertionError(f"no create table for {name}")


def _view_select_list() -> list[str]:
    stmt = next(s for s in _statements()
                if s.startswith("create or replace view transactions_beneficiary"))
    body = re.search(r"\bas select (.*?) from beneficiary_matches m\b", stmt).group(1)
    return [item.strip().split()[-1].split(".")[-1] for item in body.split(",")]


def test_0012_is_last_and_unique_prefix():
    files = sorted(MIGRATIONS.glob("*.sql"))
    assert [p.name for p in files if p.name.startswith("0012_")] == [M0012.name]


def test_existing_migrations_unchanged():
    pinned = json.loads((FIXTURES / "migration_hashes.json").read_text())["sha256"]
    current = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(MIGRATIONS.glob("*.sql"))}
    assert set(pinned) | {M0012.name} <= set(current)
    for name, digest in pinned.items():
        assert current[name] == digest, name


def test_0012_idempotent_guards():
    allowed_heads = (
        "create table if not exists ",
        "alter table ",
        "drop policy if exists deny_all on ",
        "create policy deny_all on ",
        "create or replace view transactions_beneficiary ",
    )
    for stmt in _statements():
        assert stmt.startswith(allowed_heads), stmt[:80]
        assert stmt.split()[0] not in ("insert", "update", "delete", "truncate", "grant")
        if stmt.startswith("alter table "):
            target = stmt.split()[2]
            assert target in NEW_TABLES and stmt.endswith("enable row level security")
        if stmt.startswith(("drop policy", "create policy")):
            target = re.search(r"\bon (\w+)", stmt).group(1)
            assert target in NEW_TABLES
        if stmt.startswith("create table"):
            assert stmt.split()[5] in NEW_TABLES
        if stmt.startswith("create or replace view"):
            assert stmt.split()[4] in NEW_VIEWS
    # Every create policy is preceded by its drop-if-exists.
    stmts = _statements()
    for i, stmt in enumerate(stmts):
        if stmt.startswith("create policy"):
            target = re.search(r"\bon (\w+)", stmt).group(1)
            assert f"drop policy if exists deny_all on {target}" in stmts[:i]


def test_0012_no_ddl_on_existing_objects():
    for stmt in _statements():
        if stmt.startswith(("alter ", "drop ", "create or replace ")):
            words = stmt.split()
            for obj in EXISTING_OBJECTS:
                assert obj not in words[:6], stmt[:80]


def test_0012_rls_deny_all_and_security_invoker():
    stmts = _statements()
    for t in NEW_TABLES:
        assert f"alter table {t} enable row level security" in stmts
        assert f"drop policy if exists deny_all on {t}" in stmts
        assert (f"create policy deny_all on {t} for all to public "
                "using (false) with check (false)") in stmts
    view = next(s for s in stmts if s.startswith("create or replace view"))
    assert view.startswith(
        "create or replace view transactions_beneficiary with (security_invoker = on) as ")


def test_0012_no_fk_to_transactions_and_inner_join():
    sql = _sql()
    assert "references" not in sql
    assert " join transactions_flow f on f.transaction_hash = m.transaction_hash" in sql
    assert "left join transactions_flow" not in sql
    assert "left join beneficiaries b on b.beneficiary_id = m.beneficiary_id" in sql


def test_0012_view_columns_pinned_append_only():
    assert _view_select_list() == VIEW_COLUMNS
    sql = _sql()
    assert "from beneficiary_matches m join transactions_flow f" in sql
    assert "where f.flow_type is distinct from 'internal_transfer'" in sql
    assert "append-only" in M0012.read_text().lower()


def test_0012_cheap():
    sql = _sql()
    assert "create index" not in sql
    assert "insert" not in sql.split()
    assert "update" not in sql.split()


def test_0012_pii_columns():
    bene = _table_ddl("beneficiaries")
    for banned in ("jsonb", " raw ", "cell", "email", "reference_account"):
        assert banned not in bene
    matches = _table_ddl("beneficiary_matches")
    assert "transaction_hash text primary key" in matches
    assert "match_rule text not null" in matches
    assert "matched_token text not null" in matches
    assert "check (status in ('matched', 'ambiguous', 'no_candidate'))" in matches
