# Compound learnings index
One line per learning. Roles read this first, then pull full files in relevant classes.

L-0001 [security] An authorization token for an irreversible action must cryptographically BIND to every parameter defining the action (source, amount, destination, dedup key) + be single-use + expiring + constant-time compared. (active)
L-0002 [architecture] A credential/flag presence-CHECKED at a gate but never WIRED into the client performing the gated op is a recurring latent gap: the gate passes while the privileged call runs with wrong/default creds. (active)
L-0003 [data-residency] Audit/PII minimisation must be allowlist-at-write (build the record from explicit permitted fields), never deny-by-shape regex (fail-open + corrupts legit numeric fields like a >=6-digit total). (active)
L-0004 [domain-constraints] A persisted counter gating an irreversible action must increment atomically with the state burn, committed BEFORE the dependent partner write, and re-read under a single ordering pre-execution; else a crash window or same-cycle race exceeds the cap / double-acts. (active)
L-0005 [security] Placeholder secrets in .env.example/sample config must be rejected at load/first use; a copy-verbatim deploy otherwise runs a known public signing key (forgeable tokens) while presence checks pass. (active)
L-0006 [conventions] A new operable surface must emit a machine-readable envelope on BOTH success and error with a deterministic exit code; success-only JSON / hardcoded return 0 hides failures from automation. Every parsed flag must be consumed. (active)
L-0007 [architecture] Extend a frozen-behaviour codebase via one isolated subpackage with additions-only edits to shared files, gated by a git-diff freeze check proving existing signatures/behaviour byte-identical and protected dirs (migrations/views) empty-diff. (active)
