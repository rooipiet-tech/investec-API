# GOAL — invespend refactor under the build loop

Paste the block below into a fresh session's `KICKOFF_PROMPT.md` `<goal>` slot (or let the
orchestrator read it from here). It is the GOAL the researcher → domain-expert → spec-writer
chain works from, and the standard the loop terminates against.

<goal>
Adopt the autonomous build loop (CLAUDE.md + .claude/agents) as the canonical workflow for this
repository, and use it to refactor the existing `invespend` package for maintainability and
clarity WITHOUT changing observable behaviour. The stack stays Python 3.12 with the current
dependencies (requests, psycopg, pandas, openpyxl, python-dotenv); the `invespend` console entry
point and every subcommand (init-db, ingest, report, statements, backfill-hashes, backup) keep an
identical CLI surface and output. "Shipped" means all of: (1) a reviewed REFACTORING_PLAN.md at
the repo root describing the staged changes and their risk, mapped to the modules they touch;
(2) the agreed low-risk refactors applied; (3) the full pytest suite green (>= 62 passing, 0
failures, 0 errors); (4) the database migrations still apply in order and the views/schema are
unchanged; (5) no secrets committed and `.env` still git-ignored. No new features, no schema
changes, and no behavioural change to ingestion, reporting, or statement generation.
</goal>

## Status of this seed
This GOAL and the accompanying `.loop/spec.json` were drafted by the orchestrator **outside** a
running loop (the custom subagents were not yet loaded). Treat the spec as a **DRAFT at the spec
gate**, not a frozen, partially-passed spec. In the fresh session: run researcher → domain-expert
→ spec-writer to validate/refine these criteria, then STOP at the spec gate for explicit approval
before the spec is frozen and planning begins. No builds, tests, or scores have been run yet.
