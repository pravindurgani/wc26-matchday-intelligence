# Audit Archive

Historical pressure-test rounds, audit deliverables, and process artifacts from the
pre-R32 hardening cycle. Files here are retained for traceability only — they are
no longer operational. For current operator docs, see the repo root: `README.md`,
`DEPLOY.md`, `RUNBOOK.md`, `PERSONAL_LIVE_PLAYBOOK.md`.

## Pressure-test rounds

Successive adversarial sweeps run on branch `hardening/r32-pressure-test-r2`
(local-only; push human-gated). Each round is keyed by suite delta.

| File | Date | Description |
| ---- | ---- | ----------- |
| [PRESSURE_TEST_REPORT.md](PRESSURE_TEST_REPORT.md) | 2026-06-17 | Initial R32 pressure-test report: S0–S9 brief + Wave-4 adversarial findings (916 → 990 passed). |
| [PRESSURE_TEST_R2.md](PRESSURE_TEST_R2.md) | 2026-06-17 | Round 2 — 990 → 1059 passed. |
| [PRESSURE_TEST_R3.md](PRESSURE_TEST_R3.md) | 2026-06-17 | Round 3 — post-audit hardening. |
| [PRESSURE_TEST_R4.md](PRESSURE_TEST_R4.md) | 2026-06-17 | Round 4 — deep adversarial sweep, YAML-only changes (1086 passed). |
| [PRESSURE_TEST_R5.md](PRESSURE_TEST_R5.md) | 2026-06-18 | Round 5 — deep adversarial sweep, post-R4 (1086 → 1090 passed). |
| [PRESSURE_TEST_R6.md](PRESSURE_TEST_R6.md) | 2026-06-18 | Round 6 — deep adversarial sweep, post-R5 (1090 → 1099 passed). |
| [PRESSURE_TEST_R7.md](PRESSURE_TEST_R7.md) | 2026-06-18 | Round 7 — deep adversarial sweep, post-R6 (1099 → 1105 passed). |
| [PRESSURE_TEST_R8.md](PRESSURE_TEST_R8.md) | 2026-06-18 | Round 8 — deep adversarial sweep, post-R7 (1105 → 1110 passed). |
| [PRESSURE_TEST_R9.md](PRESSURE_TEST_R9.md) | 2026-06-18 | Round 9 — deep adversarial sweep, post-R8 (1110 → 1135 passed, +25). |
| [PRESSURE_TEST_R10.md](PRESSURE_TEST_R10.md) | 2026-06-18 | Round 10 — deep adversarial sweep, post-R9 (1135 → 1150 passed, +15). |
| [PRESSURE_TEST_R11.md](PRESSURE_TEST_R11.md) | 2026-06-18 | Round 11 — deep adversarial sweep, post-R10, NO DEFERRALS (1150 → 1206 passed, +56). |
| [PRESSURE_TEST_R12.md](PRESSURE_TEST_R12.md) | 2026-06-24 | Round 12 — T-4d, normalization-focused adversarial sweep (1206 → 1250 passed, +44). |
| [PRESSURE_TEST_R13.md](PRESSURE_TEST_R13.md) | 2026-06-24 | Round 13 — T-4d, audit of R12 (1250 → 1272 passed, +22 across 2 new files). |
| [PRESSURE_TEST_R14.md](PRESSURE_TEST_R14.md) | 2026-06-24 | Round 14 — T-4d, audit of R13 (1272 → 1283 passed, +11; Korean-name convention extension). |

## Process artifacts

| File | Description |
| ---- | ----------- |
| [R32_READINESS.md](R32_READINESS.md) | R32 readiness checklist post-R14 (1283 passed) at T-4d before R32 first kickoff (2026-06-28). |
| [PRODUCTION_PARITY_PROOF.md](PRODUCTION_PARITY_PROOF.md) | Logical parity diff between `hardening/r32-pressure-test-r2` and `origin/main` (Round 2 update). |
| [COMMIT_PLAN.md](COMMIT_PLAN.md) | Manual commit plan for `hardening/r32-pressure-test-r2` (sandbox-restricted; commands provided for manual execution). |
| [CORRECTIONS.md](CORRECTIONS.md) | Deviations from `CLAUDE_CODE_PROMPT_WC26_IMPROVEMENTS.md` recorded pre-change in Wave B, traced to Wave-A discovery findings. |
