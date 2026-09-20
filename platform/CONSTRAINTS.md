# Platform constraints

The quality bar for `platform/`. Checks run through `scripts\platform-check.ps1`, and a failing check **blocks** the change. Don't weaken a rule to get a change through. Record an exception below instead.

This file lives in `platform/` rather than extending the root `CONSTRAINTS.md`. The platform branch is based on `master`, which doesn't have that file (it exists on `rag-chunking`), so a separate file avoids a merge conflict. Merge them once both branches land.

## Enforced

| Rule | Command | Reason |
|---|---|---|
| `ruff check` clean (E,F,W,I,B,UP,S,ASYNC,RUF,SIM,PL; 130 columns) | `platform-check.ps1 -Only lint` | Catches bugs, blocking I/O in async code, and security smells (bandit-derived `S` rules) |
| `mypy --strict` clean on `src` | `platform-check.ps1 -Only types` | Tool arguments and provider payloads are untrusted, so the types are the first guard |
| pytest: ≥1 test, **0 failed, 0 errors, 0 skipped** | `platform-check.ps1 -Only tests` (JUnit gate `tests/_junit_gate.py`) | Skipped security tests hide regressions. DB and API tests run against a real pgvector Postgres |
| `bandit -ll` clean on `src` | `platform-check.ps1 -Only audit` | Second security linter |
| `pip-audit` no known vulnerabilities in locked dependencies | `platform-check.ps1 -Only audit` | There's a manifest now (`uv.lock`) |
| Every malicious-input test stays green: path traversal corpus + hypothesis, SSRF corpus, shell injection, SQL injection strings, XSS, CSRF/Host/Origin, prompt injection, taint escalation | part of pytest | These are the security boundaries in `docs/SECURITY.md` |
| No secrets in the tree (gitleaks with `platform/.gitleaks.toml`) | `platform-check.ps1 -Only audit` (when gitleaks is installed) | Secrets live only in `platform/secrets/` (gitignored, owner-only ACL) |

## Measured, not yet enforced

| Metric | Baseline | Command | Why not enforced |
|---|---|---|---|
| Internal stage latencies (DB, hybrid retrieval, context build, API) | see `docs/PERFORMANCE.md` | `scripts\platform-bench.ps1` | Laptop numbers are noisy, and the bench needs the live stack. Compare runs by hand |
| Live end-to-end scenarios with the real model | see `docs/PERFORMANCE.md` §4 | `docker compose exec -T backend python - < bench/e2e_live.py` | Takes minutes and depends on the model |

## Exceptions

| Rule | Exception | Expires |
|---|---|---|
| — | none | — |
