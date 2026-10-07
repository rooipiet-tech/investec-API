"""S9: static checks on db/migrations/0013_payment_instructions.sql (+ DB checks when INVESPEND_TEST_DATABASE_URL is set)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from invespend import db
from tests.instr_helpers import PG_URL, ins

pytestmark = pytest.mark.xfail(strict=False, reason="S9 red: migration 0013 not written yet")

MIGRATIONS = db.MIGRATIONS_DIR
M0012 = MIGRATIONS / "0012_beneficiary_matching.sql"
M0013 = MIGRATIONS / "0013_payment_instructions.sql"
FIXTURES = Path(__file__).parent / "fixtures"

INSTRUCTION_COLUMNS = [
    "instruction_id", "status", "path", "amount", "currency", "source_account_id", "source_profile_id",
    "source_account_last3", "payee_name_norm", "beneficiary_id", "beneficiary_fingerprint", "account_hmac",
    "recent_beneficiary", "daily_reserved", "reserved_day", "my_reference", "their_reference", "figures_source",
    "message_id_hash", "notify_to", "received_at", "first_seen_at", "eligible_at", "batch_ref", "item_no",
    "offered_at", "batch_notified_at", "offer_digest", "approved_at", "expires_at", "paste_notified_at",
    "hold_notified_at", "executed_at", "execution_mode", "outcome_code", "outcome_message", "updated_at",
]
EXPECTED_COLUMNS = {
    "payment_instruction": INSTRUCTION_COLUMNS,
    "payment_beneficiary_seen": ["beneficiary_id", "first_seen_at", "established", "last_fingerprint", "fingerprint_changed_at"],
    "payment_message_seen": ["instruction_id", "outcome", "seen_at", "auth_from", "resend_notified_at"],
    "payment_v2_meta": ["key", "value", "set_at"],
}
FOUR = tuple(EXPECTED_COLUMNS)
POST_APPROVAL = {"accepted", "submitting", "executed", "failed", "needs_review", "needs_authorisation"}


def _raw() -> str:
    return M0013.read_text()


def _sql() -> str:
    lines = [ln.split("--", 1)[0] for ln in _raw().splitlines()]
    return " ".join(" ".join(lines).lower().split())


def _create_body(table: str) -> str:
    sql = _sql()
    start = sql.index(f"create table if not exists {table} (") + len(f"create table if not exists {table} (")
    depth, i = 1, start
    while depth:
        depth += {"(": 1, ")": -1}.get(sql[i], 0)
        i += 1
    return sql[start:i - 1]


def _top_level_items(body: str) -> list[str]:
    items, depth, cur = [], 0, []
    for ch in body:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            items.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur).strip())
    return [i for i in items if i]


def _columns(table: str) -> list[str]:
    return [i.split()[0] for i in _top_level_items(_create_body(table)) if not i.startswith("check")]


def _do_blocks() -> list[str]:
    return re.findall(r"do \$\$ (.*?) \$\$;", _sql())


def test_migration_history_exact_set_with_pinned_hashes():
    pinned12 = json.loads((FIXTURES / "beneficiary" / "migration_hashes.json").read_text())["sha256"]
    pinned = json.loads((FIXTURES / "migration_hashes_v2.json").read_text())["sha256"]
    current = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(MIGRATIONS.glob("*.sql"))}
    assert set(current) == set(pinned12) | {M0012.name, M0013.name}
    assert sorted(current)[-1] == M0013.name
    assert set(pinned) == {M0012.name, M0013.name}
    for name, digest in {**pinned12, **pinned}.items():
        assert current[name] == digest, name
    assert [n for n in current if n.startswith("0013_")] == [M0013.name]


def test_only_additive_no_view_and_no_drop():
    sql = _sql()
    assert "create view" not in sql and "create or replace view" not in sql and "drop " not in sql
    assert "alter table payment_pending" not in sql and "payment_daily_total" not in sql
    for banned in ("account_number", "accountnumber", "body", "raw ", "image_data", "token"):
        assert banned not in " ".join(sum((_columns(t) for t in FOUR), [])), banned


@pytest.mark.parametrize("table", FOUR)
def test_column_set_equals_section_2b_list_per_table(table):
    assert _columns(table) == EXPECTED_COLUMNS[table]


def test_column_counts_are_37_5_5_3():
    assert [len(_columns(t)) for t in FOUR] == [37, 5, 5, 3]


def test_notify_to_and_expires_at_not_null():
    body = _create_body("payment_instruction")
    for col in ("notify_to text not null", "expires_at timestamptz not null", "received_at timestamptz not null",
                "figures_source text not null", "path text not null", "amount numeric(18,2) not null"):
        assert col in body, col


def test_check_status_list_equals_transitions_keys():
    body = _create_body("payment_instruction")
    m = re.search(r"status text not null check \(status in \((.*?)\)\)", body)
    statuses = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert statuses == set(ins().TRANSITIONS) and len(statuses) == 12
    assert re.search(r"path text not null check \(path in \('registered','new_payee'\)\)", body.replace(", ", ","))
    assert "figures_source in ('typed','attachment','image')" in body.replace(", ", ",")


def test_batch_columns_all_or_none_check_present():
    body = _create_body("payment_instruction")
    assert ("check ((batch_ref is null) = (item_no is null) and (batch_ref is null) = (offered_at is null) "
            "and (batch_ref is null) = (offer_digest is null))") in body


def test_post_approval_status_check_present_and_equals_post_approval_statuses():
    body = _create_body("payment_instruction")
    m = re.search(r"check \(status not in \((.*?)\) or \(approved_at is not null and batch_ref is not null\)\)", body)
    assert m, "post-approval CHECK missing"
    listed = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert listed == set(ins().POST_APPROVAL_STATUSES) == POST_APPROVAL
    # the set is exactly the descendants of 'accepted' through 'submitting' minus statuses reachable before approval
    t = ins().TRANSITIONS
    reach, todo = set(), ["accepted"]
    while todo:
        s = todo.pop()
        if s not in reach:
            reach.add(s)
            todo += list(t[s])
    assert reach - {"cancelled", "expired", "parked"} == POST_APPROVAL


def test_rls_and_policy_statements_for_all_four_tables_are_inside_guarded_do_blocks():
    sql = _sql()
    blocks = " ".join(_do_blocks())
    for table in FOUR:
        assert f"alter table {table} enable row level security" in blocks
        assert f"create policy deny_all on {table} for all to public using (false) with check (false)" in blocks
        assert f"to_regclass('{table}')" in blocks
        assert f"tablename = '{table}'" in blocks
    outside = re.sub(r"do \$\$ .*? \$\$;", " ", sql)
    for banned in ("enable row level security", "create policy", "create index", "create unique index", "drop policy",
                   "alter table"):
        assert banned not in outside, banned
    assert "relrowsecurity" in blocks and "pg_policies" in blocks


def test_unique_index_on_batch_ref_item_no_is_partial_and_guarded():
    blocks = " ".join(_do_blocks())
    assert "if to_regclass('payment_instruction_batch_item_idx') is null then" in blocks
    assert ("create unique index payment_instruction_batch_item_idx on payment_instruction (batch_ref, item_no) "
            "where batch_ref is not null") in blocks
    assert "if to_regclass('payment_instruction_status_idx') is null then" in blocks
    assert "create index if not exists" not in _sql()


def test_every_create_table_is_if_not_exists_and_nothing_else_unguarded():
    sql = _sql()
    stmts = [s.strip() for s in re.sub(r"do \$\$ .*? \$\$;", ";", sql).split(";") if s.strip()]
    assert len(stmts) == 4 and all(s.startswith("create table if not exists ") for s in stmts)


def test_no_migration_needed_to_change_hold():
    cols = set(_columns("payment_instruction"))
    assert {"batch_ref", "item_no", "offered_at", "batch_notified_at", "offer_digest", "approved_at", "eligible_at",
            "first_seen_at", "figures_source", "outcome_message", "daily_reserved", "reserved_day", "notify_to"} <= cols
    assert {"last_fingerprint", "fingerprint_changed_at"} <= set(_columns("payment_beneficiary_seen"))
    assert {"auth_from", "resend_notified_at"} <= set(_columns("payment_message_seen"))


def test_column_set_equals_column_owners_and_every_column_has_an_owner_method():
    owners = ins().COLUMN_OWNERS
    expected = {(t, c) for t, cols in EXPECTED_COLUMNS.items() for c in cols}
    assert set(owners) == expected and len(owners) == 50
    store_classes = [ins().MemoryInstructionStore]
    from invespend.payments import pg_instructions
    store_classes.append(pg_instructions.PgInstructionStore)
    for key, methods in owners.items():
        assert methods, key
        for method in methods:
            for cls in store_classes:
                assert callable(getattr(cls, method, None)), (key, method, cls.__name__)


def test_policy_on_pg_instructions_module_reexported_from_instructions():
    from invespend.payments import instructions, pg_instructions
    assert instructions.PgInstructionStore is pg_instructions.PgInstructionStore


# --------------------------------------------------------------- DB (gated)
needs_db = pytest.mark.skipif(not PG_URL, reason="no INVESPEND_TEST_DATABASE_URL")


def _counts(cur):
    cur.execute("select tablename, count(*) from pg_policies where tablename = any(%s) group by tablename", (list(FOUR),))
    pol = dict(cur.fetchall())
    cur.execute("select count(*) from pg_indexes where indexname in ('payment_instruction_status_idx', "
                "'payment_instruction_batch_item_idx')")
    return pol, cur.fetchone()[0]


@needs_db
def test_0013_init_db_twice_idempotent():
    with db.connect(PG_URL) as conn:
        db.init_db(conn)
    with db.connect(PG_URL) as conn:
        db.init_db(conn)
        with conn.cursor() as cur:
            pol, idx = _counts(cur)
            cur.execute("select relname, relrowsecurity from pg_class where relname = any(%s)", (list(FOUR),))
            rls = dict(cur.fetchall())
    assert pol == {t: 1 for t in FOUR} and idx == 2 and all(rls[t] for t in FOUR)


@needs_db
def test_0013_second_init_db_takes_no_share_or_stronger_lock_on_the_four_tables():
    import psycopg
    with db.connect(PG_URL) as conn:
        db.init_db(conn)
    holder = psycopg.connect(PG_URL, autocommit=False)
    try:
        with holder.cursor() as cur:
            for table in FOUR:
                cur.execute(f"lock table {table} in row exclusive mode")
        with db.connect(PG_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '2s'")
                cur.execute(M0013.read_text())      # any ShareLock-or-stronger request would time out here
    finally:
        holder.rollback()
        holder.close()
