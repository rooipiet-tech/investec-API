# invespend — REFACTORING_PLAN.md

Iteration: **1**  ·  Spec: **FROZEN** (`.loop/spec.json`)  ·  Baseline: **62 tests passing**

---

## 1. Goal & scope

Maintainability/clarity refactor of the `invespend` Python package with **zero
observable-behaviour change**. Behaviour is frozen: ingestion, reporting, statement
generation, the CLI surface, the DB schema/migrations, and the dependency set must
all be byte-for-byte equivalent in effect.

In scope for iteration 1: a small, **certainly-safe** subset of isolated cleanups in
low-risk modules — consolidating one duplicated SMTP send path, and tidying two
redundant type annotations that have **no runtime effect** under
`from __future__ import annotations`.

Explicitly **not** in scope: any change to dedup/hashing, the read-time ROW_NUMBER
dedup CTEs, the intraday chain-sort, the flow classifier, money formatting/rounding,
the CLI argparse surface, any `.sql` file, or `pyproject.toml` dependencies. See §4.

Gating constraints honoured (from frozen spec F1–F7):
- No behavioural change to ingestion / reporting / statement generation (F3).
- No change to CLI subcommands, flags, dest names, defaults, or help text (F6).
- No schema change: zero diff under `db/` (F5).
- No new dependencies; Python 3.12 + current deps only (F7).
- `.env` stays git-ignored; no secrets (F7).
- pytest stays green (>=62 passing, 0 failures, 0 errors) (F4).
- Public function signatures preserved; `groups.GROUPS` / `groups.EXCLUDE_ACCOUNTS`
  remain patchable at `invespend.groups.<NAME>` (F3).

---

## 2. Staged change set

| ID | Description | Files touched (`src/invespend/`) | Risk | Criteria advanced | Iteration 1? |
|----|-------------|----------------------------------|------|-------------------|--------------|
| **S1′** | Extract the byte-identical 4-line smtplib send sequence (`SMTP(...).starttls()/login()/send_message()`) into a private `emailer._smtp_send(settings, msg)` helper, and call it from both `emailer.send_email` and `cli._alert_new_accounts`. The cli keeps building its own (attachment-less) `EmailMessage`, so the alert message is byte-identical; only the connection dance is shared. | `emailer.py`, `cli.py` (`_alert_new_accounts`) | **LOW** | F1, F2 | **Applied** |
| ~~S1~~ | **REJECTED at plan-review gate.** Routing the alert through `emailer.send_email` is NOT behaviour-preserving: `send_email` requires ≥1 attachment (`emailer.py:42-43` raises `ValueError` when none) and the alert is attachment-less — the `ValueError` would be swallowed by the alert's outer `try/except`, silently dropping the alert email (F3 violation). | — | — | — | **Rejected** |
| **S2** | Replace the redundant quoted forward-ref `"date | None"` in `statements.load_investec_balance`'s return annotation with the bare `date | None` (annotations are strings already; no runtime effect). | `statements.py:158` | **LOW** | F1, F2 | **Applied** |
| **S3** | Tighten the `loaded` local annotation in `statements.generate_account_statements` so the `investec_date` slot reads `date | None` instead of `object` (annotation only; the value already is a date-or-None). | `statements.py:558` | **LOW** | F1, F2 | **Applied** |
| S4 | Inline / remove the thin `emailer.send_report` back-compat wrapper and update its single caller `cli._send_group_report`. | `emailer.py`, `cli.py` | LOW | F2 | Deferred |
| S5 | Remove `db.upsert_transaction` (single-row upsert) if provably dead. | `db.py` | LOW | F2 | Deferred (see §4) |
| S6 | Extract a shared group fan-out helper for `cmd_report` / `cmd_statements`. | `cli.py` | MED | F2 | Deferred |
| S7 | Extract a shared `_send_group_report` / `_send_group_statements` recipient-resolution helper. | `cli.py` | MED | F2 | Deferred |
| S8 | Consolidate the 3-way account include/exclude filtering helper. | `report.py`, `statements.py`, `groups.py` | MED | F2 | Deferred |
| S9 | Name the flow-type string literals as constants. | `report.py`, `statements.py` | MED | F2 | Deferred |
| S10 | Split `statements.build_account_statement` into pure sub-helpers. | `statements.py` | MED–HIGH | F2 | Deferred |
| — | dedup hash / `assign_day_seq` / `_group_key`; ROW_NUMBER dedup CTEs; `_chain_sort_day`; flow classifier; `MONEY_FORMAT`/rounding/tolerances | db.py, report.py, statements.py, `db/*.sql` | **HIGH** | — | **OFF-LIMITS** (§4) |

---

## 3. Iteration-1 stages — exact edit intent, why behaviour-preserving, verification

### S1′ — Extract a shared `_smtp_send` helper  (`emailer.py`, `cli.py`, LOW)

> **Plan-review gate note.** The original S1 (route the alert through
> `emailer.send_email`) was **rejected**: `send_email` requires ≥1 attachment
> (`emailer.py:42-43` raises `ValueError("send_email called with no attachments")`)
> while the new-account alert is attachment-less, so the `ValueError` would be
> caught by the alert's outer `try/except Exception` and the email silently dropped
> — a behavioural regression (F3). S1′ below consolidates only the part that is
> genuinely identical.

**Current state.** The exact 4-line connection sequence
```
with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
    server.starttls()
    server.login(settings.smtp_user, settings.smtp_password)
    server.send_message(msg)
```
appears verbatim in `emailer.send_email` (emailer.py:57-60) and in
`cli._alert_new_accounts` (cli.py:90-93). The two `EmailMessage` builds differ
(reports carry attachments; the alert does not), so only the send sequence is shared.

**Edit intent.**
1. Add `emailer._smtp_send(settings: Settings, msg: EmailMessage) -> None` containing
   exactly the 4-line block above (verbatim).
2. In `emailer.send_email`, replace lines 57-60 with `_smtp_send(settings, msg)`.
3. In `cli._alert_new_accounts`, replace lines 90-93 with `_smtp_send(settings, msg)`,
   add `_smtp_send` to the existing `from .emailer import ...` line (cli.py:13), and
   drop the now-unused function-local `import smtplib` (cli.py:54). The
   `from email.message import EmailMessage` import stays — the cli still builds its
   own attachment-less `msg`.

**Why behaviour-preserving.**
- The extracted block is byte-identical to both originals; relocating identical code
  into a helper changes no host/port/TLS/login/send semantics.
- Both call sites still build their own `msg` exactly as before — the report message
  (with attachments) and the alert message (without) are unchanged, From/To/Subject/
  body identical.
- The alert's outer `try/except Exception` is retained, so failures still warn-not-
  crash; the `log.info(...)` and `print(f"Alert email sent to: {msg['To']}")` success
  lines are preserved verbatim.

**Verification.** No SMTP is opened in tests (pure-unit), so `_smtp_send` is exercised
structurally; `tests/test_account_alert.py` (14 tests) guards the alert path's inputs
and full `pytest` must stay >=62/0/0 (F4). `git diff` confined to `emailer.py` +
`cli.py`; `git diff --stat db/` empty (F5).

### S2 — Drop redundant quoted forward-ref  (`statements.py:158`, LOW)

**Edit intent.** `load_investec_balance` is annotated
`tuple[float | None, "date | None"]`; the quotes around `date | None` are redundant
because the module already has `from __future__ import annotations`, so **all**
annotations are strings evaluated lazily and `date` is imported. Change to
`tuple[float | None, date | None]`.

**Why behaviour-preserving.** Under PEP 563 the annotation is never evaluated at
runtime; this is a cosmetic source change with zero runtime effect. Function
signature/return shape unchanged.

**Verification.** `tests/test_statements.py` (20 tests) exercises this module; import
+ collection must succeed and the suite stay green (F4). No behavioural assertion
touches annotations.

### S3 — Tighten `object` → `date | None` annotation  (`statements.py:558`, LOW)

**Edit intent.** The local `loaded: list[tuple[Account, pd.DataFrame, float | None,
float | None, object]]` uses `object` for the trailing `investec_date` slot. The
value stored there is a date-or-None (`load_investec_balance` returns
`date | None`). Change the slot to `date | None`.

**Why behaviour-preserving.** A local-variable annotation; never evaluated at runtime
under `from __future__ import annotations`. No change to logic, control flow, or the
tuple's actual contents.

**Verification.** Same as S2 — `tests/test_statements.py` + full suite green (F4).

---

## 4. Deferred / out of scope (HIGH-risk zones — left untouched)

These are flagged HIGH-risk by research/domain and are **explicitly not planned for
change** in any iteration of this loop unless the spec is reopened:

1. **Dedup hash & day-seq grouping** — `db.transaction_hash`, `db.assign_day_seq`,
   `db._group_key` (db.py). The hash formula (field set/order, `|` separator,
   `or ""` null-coalescing, `str()` coercion) is frozen: any change re-keys every
   row and breaks rolling-window dedup idempotency. Must be **byte-identical** (F2).
2. **Read-time ROW_NUMBER dedup CTEs** — triplicated across `report.py` (37-60) and
   `statements.py` (91-110, 133-149), partitioned by
   `(account_id, amount, description, running_balance, effective_date)` ordered by
   `ingested_at desc`. Untested against any DB; DRYing one copy risks silent drift in
   which duplicate survives → different amounts/balances. Leave **byte-identical**.
3. **`statements._chain_sort_day`** — intraday greedy min-distance chain-sort,
   orphan anchor, NULL-rb arithmetic placement (`< 0.015` tolerance), balance
   carry-forward. Most intricate logic in the codebase; easy to break on uncovered
   shapes. Leave untouched.
4. **Flow classification** — the `internal_transfer` / `external_inflow` /
   `external_outflow` string literals and the Tier A/B/C SQL (`db/migrations`).
   Untested in code; renaming/regex changes re-label transfers ↔ spend.
5. **`MONEY_FORMAT`, rounding, tolerances** — `__init__.MONEY_FORMAT`, the scattered
   `round(x, 2)`, and the `0.01` / `0.015` tolerances. Centralising risks divergence;
   leave as-is.
6. **All `db/` SQL** — migrations, views, `roles.sql`. Zero diff required (F5);
   filename sort order 0001..0008 is load-bearing.
7. **CLI argparse surface** — `build_parser` subcommands/flags/dests/defaults/help
   (F6). Body/recipient-helper refactors S6/S7 are deferred precisely because they
   border this contract.

**Also deferred but not high-risk** (candidates for later iterations, held back to
keep iteration 1 minimal and certainly-safe):
- **S5 `db.upsert_transaction` removal** — grep confirms **no caller** in `src/`.
  However it is listed in research §1 as public surface and its helper `_tx_params`
  is unit-tested (`tests/test_db.py`). Per the spec's "public function signatures
  preserved" Must-have and the "document rather than remove if any doubt" guidance,
  it is **documented here as dead-but-retained** rather than removed in iteration 1.
- **S4 `send_report` inlining**, **S6/S7 cli helper extraction**, **S8 filter
  consolidation**, **S9 flow-literal constants**, **S10 statement-builder split** —
  all LOW–MED but touch more surface or border observable contracts; deferred.

---

## 5. Rollback

Each iteration-1 stage is an **independent, commit-sized, revertable** change with no
cross-stage dependency:

- **S1** is confined to `cli._alert_new_accounts` (+ two removed local imports) —
  revert restores the inline SMTP block verbatim.
- **S2** and **S3** are single-line annotation edits in `statements.py` — revert is a
  one-line restore each.

Recommended ordering: S2, S3 (annotation-only, lowest risk), then S1. Run
`python -m pytest -q` after each stage; if any stage drops below 62 passed / 0
failed / 0 errors, revert that stage's commit and stop. `git diff --stat db/` must
stay empty (F5) and `git diff` must show changes confined to `cli.py` and
`statements.py` only (F2).
