# invespend — autonomous build loop

This repository uses an **autonomous build loop** as its canonical workflow for
non-trivial changes. The orchestrator (you, the top-level Claude Code session)
coordinates a small fleet of subagents through a fixed pipeline, persists state
under `.loop/`, and ships only when an explicit gate passes.

## Project shape

- Package: `invespend` (`src/invespend/`), installed as a console script
  `invespend = invespend.cli:main`.
- Stack: Python 3.12, deps `requests`, `psycopg`, `pandas`, `openpyxl`,
  `python-dotenv`. Dev: `pytest`.
- Purpose: ingest Investec Programmable Banking transactions into Postgres and
  produce weekly Excel spend reports + account statements.
- CLI subcommands (the observable surface): `init-db`, `ingest`, `report`,
  `statements`, `backfill-hashes`, `backup`.
- Database migrations live in `db/migrations/` and apply in numeric order.

## Golden rules

1. **Behaviour is frozen.** No new features, no schema changes, no change to the
   CLI surface or to the observable output of ingestion, reporting, or statement
   generation, unless the spec explicitly says so.
2. **Secrets stay out of git.** `.env` is git-ignored; never commit credentials.
3. **The test suite is the floor, not the ceiling.** The full `pytest` suite must
   stay green (baseline: 62 passing, 0 failures, 0 errors). Migrations must still
   apply in order; views/schema unchanged.
4. **Ship only through the gate** (see Shippable gate below). Never ship a failing
   Must-have — halt and surface a diagnostic instead.

## The loop

```
research → domain → spec  ──(human approval gate)──▶
plan → plan-review gate → build → (output-reviewer ∥ risk-security) → tester
     → score → triage ──▶ ship | iterate | halt
```

### Phase 0 — research & spec (one-time, frozen)
- **researcher** → `.loop/research.md`: factual map of modules, CLI surface, tests,
  migrations, code smells (ranked by risk), coupling.
- **domain-expert** → `.loop/domain.md`: domain invariants and observable-output
  contracts that must not change.
- **spec-writer** → `.loop/spec.json`: the acceptance criteria (Must-have / Should /
  Nice), each measurable. Validate/refine the existing draft rather than restart.
  Once shown to the human and **approved**, the spec is **frozen**.

The orchestrator **STOPS** after the spec and shows the human the acceptance
criteria. No planning or building happens before explicit approval.

### Phase 1+ — iterate
- **planner** → `REFACTORING_PLAN.md` + a concrete change set for this iteration,
  staged by risk and mapped to modules.
- **plan-review gate**: the orchestrator checks the plan is low-risk, behaviour-
  preserving, and mapped to spec criteria. If not, bounce back to planner.
- **builder** applies the agreed change set (smallest safe steps).
- **output-reviewer** ∥ **risk-security** review the diff in parallel: one for
  correctness/clarity/behaviour-preservation, one for risk + secret leakage.
- **tester** runs the full `pytest` suite and migration-order check, reports
  pass/fail counts.

### Scoring & triage
Each iteration the orchestrator computes:
- `pass_rate` = passing criteria / total criteria (Must-haves are gating).
- `S` = weighted score (Must-have = 3, Should = 2, Nice = 1), `S = Σ earned / Σ max`.
- Triage route ∈ {`ship`, `iterate`, `halt`}.

Record everything in `.loop/state.json` (see below) with a one-line
`decision_log` entry showing the scoring arithmetic.

### Shippable gate
Ship **iff** all of:
- every **Must-have** criterion passes,
- `pytest` is green (≥62 passing, 0 failures, 0 errors),
- migrations still apply in order; views/schema unchanged,
- no secrets committed; `.env` still git-ignored,
- no behavioural/CLI/schema change beyond what the (frozen) spec allows.

If any Must-have fails → **halt** and show a diagnostic. Never ship red.

## `.loop/state.json` schema

```json
{
  "iteration": 1,
  "pass_rate": "x/y",
  "S": 0.00,
  "best": 0.00,
  "triage": "iterate|ship|halt",
  "decision_log": ["it1: <scoring arithmetic, one line>"]
}
```

## Thread hygiene

Subagents return **one-line** summaries to the orchestrator and write their full
output to files under `.loop/` (or the diff). The orchestrator never pastes full
subagent transcripts into the main thread.

## Agents

Definitions live in `.claude/agents/`. Roles: `researcher`, `domain-expert`,
`spec-writer`, `planner`, `builder`, `output-reviewer`, `risk-security`,
`tester`. Each is a focused, mostly read-only specialist except `builder`
(edits code) and `spec-writer`/`planner` (write `.loop/` + plan docs).
