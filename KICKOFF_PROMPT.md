# Kickoff prompt

Paste this into the Claude Code app session (repo open, CLAUDE.md + .claude/ present).
Only fill in <goal>. Leave the rest as-is.

----------------------------------------------------------------------
You are the orchestrator defined in CLAUDE.md. Run the autonomous build loop for this goal:

<goal>
Describe what "shipped" means in 3–6 sentences: what to build, the stack/constraints,
and the concrete success conditions (e.g. "a FastAPI service with a /health endpoint,
p95 < 200ms at 100 rps measured by k6, and an OpenAPI doc; deploys on Python 3.12").
</goal>

Procedure:
1. Spawn researcher, then domain-expert, then spec-writer. Persist .loop/research.md,
   .loop/domain.md, .loop/spec.json (freeze it).
2. STOP and show me the acceptance criteria. Wait for my "approved" before planning.
3. After approval, run the loop per CLAUDE.md: plan → plan-review gate → build →
   output-reviewer ∥ risk-security → tester → score → triage/ship/halt.
4. Maintain .loop/state.json each iteration: iteration, pass_rate, S, best, triage route,
   and a one-line decision_log entry with your scoring arithmetic.
5. Keep this thread clean: subagents return one-line summaries; do not paste their full
   output here. Ship only when the CLAUDE.md shippable gate passes; never ship a failing
   Must-have — halt and show me a diagnostic instead.
----------------------------------------------------------------------
