# Constraints

The quality bar for changes to this repo. Checks run through `scripts\check.ps1 -Stage fast|task|full`; a failing check **blocks** the task. Don't weaken a rule to get a change through. Record an exception below instead.

Environment these rules assume: Windows PowerShell 5.1, Pester 3.4.0 (the version built into Windows), PSScriptAnalyzer 1.25.0 (CurrentUser), gitleaks 8.30.1 at `%LOCALAPPDATA%\gitleaks\gitleaks.exe`.

## Floor (always enforced)

| Rule | Command | Stage | Reason |
|---|---|---|---|
| No PSScriptAnalyzer errors in changed scripts | `check.ps1 -Stage fast` | edit loop | Parse and correctness errors are never intentional |
| No secrets in uncommitted or untracked files | `check.ps1 -Stage fast` (gitleaks `stdin --redact`) | edit loop | The server has no API key; a leaked token elsewhere would be the only credential in the repo. This is the external check, independent of our own tests |
| Pester suite passes with 0 failed, 0 skipped, 0 pending, and at least 1 test | `check.ps1 -Stage task` | end of task | Skipped or empty suites hide regressions |

## Enforced with numbers

| Rule | Value | Command | Stage | Reason |
|---|---|---|---|---|
| PSScriptAnalyzer warnings in `scripts\` (excluding `PSAvoidUsingWriteHost`) | ≤ 9 | `check.ps1 -Stage task` | end of task | 9 warnings exist today (2026-09-17). Hold that and ratchet down. Write-Host is excluded because these are interactive CLI scripts |
| Retrieval quality on `tests\fixtures\rag` (hit@1, hit@4, MRR, recall at speed and accuracy RAG settings) | no metric below `tests\fixtures\rag\baseline.json` (all 1.0 today) | `rag-eval.ps1 -Baseline tests\fixtures\rag\baseline.json` (inside `check.ps1 -Stage task`) | end of task | Retrieval runs without the LLM server, so a chunking or ranking change is judged on retrieval alone, in seconds |

## Measured, not yet enforced

| Metric | Today (2026-09-17) | Direction | Command | Reason not enforced |
|---|---|---|---|---|
| Fixture index build time | 870 ms | lower | `rag-eval.ps1` (`timing_ms.build`) | Single noisy run on a laptop; no variance data yet |
| Fixture index load time | 268 ms | lower | `rag-eval.ps1` (`timing_ms.load`) | Same |
| Fixture chunk count / max chunk chars | 7 / 1356 | max chars ≤ chunk size + overlap | `rag-eval.ps1` (`index`) | Descriptive; depends on chunker parameters |
| End-to-end facts-local eval | not yet run | higher | `check.ps1 -Stage full`, then `eval.ps1 -Compare a.json b.json` | Needs the running server (minutes); `eval.ps1` does not exit non-zero on a score drop |

## Dropped dimensions

| Dimension | Why |
|---|---|
| Accessibility, web performance | No UI or served web page of our own (the llama.cpp web UI is upstream code) |
| Dependency CVE scanning | No package manifest; the only third-party code is the vendored llama.cpp binaries |
| Coverage percentage | Pester 3.4 coverage on PowerShell modules is slow and unreliable. Retrieval quality and tests on changed functions carry this instead |

## Stages and cost

| Stage | Contents | Measured time |
|---|---|---|
| `fast` | Lint errors on changed scripts, secret scan | ~20 s cold. Loading PSScriptAnalyzer takes most of it, which is over the few-seconds target for the edit loop. Accepted for now |
| `task` | `fast` + lint ratchet + Pester + retrieval eval | target < 60 s |
| `full` | `task` + `eval.ps1 -Category facts-local` | minutes, needs `start.ps1` |

## Exceptions

| Rule | Exception | Owner | Expires |
|---|---|---|---|
| — | none | — | — |

## Change log

| Date | Change | Approved by | Reason |
|---|---|---|---|
| 2026-09-17 | `deep-profile` query accepts either `Offline batch jobs only` (README table) or `offline batch jobs through the \`deep\` profile` (models.md) | user, during Task 2 | Both passages answer the query; the fixture had wrongly accepted only one. All other queries are unchanged |

## Known gaps (not constraints, tracked here so they aren't forgotten)

- The retrieval fixture already scores 1.0 on every metric, so it catches regressions but **can't show improvements**. It needs harder queries or a bigger corpus before it can justify ranking changes.
- The repo has no `.gitignore`. Model `.gguf` files, llama.cpp binaries and logs are committed.
