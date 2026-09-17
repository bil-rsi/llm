# Plan: RAG chunking correctness upgrade

Source: `SPEC.md` (approved 2026-09-17). Branch: `rag-chunking` from `master`, local only.

## Overview

Rewrite `Split-Doc` as a small line-based state machine that splits on headings, paragraphs and code blocks. It flushes chunks in document order and trims overlap to sentence or word boundaries. The index gets a chunker version. Each bug gets a failing Pester test before its fix.

## Architecture decisions

- **Split-Doc stays pure** (string in, chunk objects out). All tests go through `& (Get-Module rag) { ... }`, so no internal function gets exported.
- **Chunker version** is `$ChunkerVersion = 2` in `rag.psm1`, stored as `chunker` in `index.json`. Reusing a file's old chunks requires the same mtime **and** the same version. An index without the field counts as version 1.
- **Blocks before chunks:** the text is first parsed into blocks (heading / paragraph / code fence), then packed into chunks. Headings flush the buffer, which fixes B1 and B3 by construction.
- **Overlap** is taken from the tail of the previous chunk *within the same section*. It's trimmed forward to the first sentence start (`(?<=[.!?])\s+`) inside the window, or else to the first whitespace.

## Task list (dependency order)

### Task 1: Pester harness + chunker version (B4)
- **Files:** `tests\rag.Tests.ps1` (new), `scripts\rag.psm1`
- **Acceptance:**
  - An index written without `chunker`, or with a different value, is fully re-chunked, and the output line says so.
  - With the same version and mtime, chunks are reused.
  - Tests use a temp `$RagRoot` and never touch `C:\llm\rag`.
- **Verify:** RED test first, then `check.ps1 -Stage task` passes. This is the first run where the "at least 1 test" floor can pass.

### Task 2: Block parser + heading boundaries + document order (B1, B3)
- **Files:** `scripts\rag.psm1`, `tests\rag.Tests.ps1`
- **Acceptance (spec criteria 1, 3):**
  - A short intro followed by a long paragraph comes out as intro first, then the long text.
  - Every chunk's `heading` is the heading its first line falls under.
  - No chunk's text contains a heading line other than its own at the start.
- **Verify:** RED tests with the reproductions from SPEC.md B1 and B3, then GREEN, then `check.ps1 -Stage task` (retrieval ≥ baseline).

### Task 3: Boundary-aware overlap and hard splits (B2, B5)
- **Files:** `scripts\rag.psm1`, `tests\rag.Tests.ps1`
- **Acceptance (spec criteria 2, 5):**
  - No chunk starts or ends mid-word.
  - Overlap starts at a sentence start when one exists in the window.
  - Length ≤ `Size` + `Overlap`, except for a single word longer than that.
- **Verify:** RED tests (8 × ~200-char paragraphs; one 5,000-char paragraph; one 3,000-char token with no spaces), then GREEN, then `check.ps1 -Stage task`.

### Checkpoint A: after Task 3
Review the chunk dumps for the 3 fixture docs (`rag-index.ps1 -Query`) and compare the retrieval eval against the baseline.

### Task 4: Code fences kept whole (criterion 4)
- **Files:** `scripts\rag.psm1`, `tests\rag.Tests.ps1`
- **Acceptance:**
  - A fence ≤ `Size` that contains blank lines stays in one chunk.
  - A longer fence is split only at line ends.
  - An unclosed fence runs to the end of the file without crashing.
- **Verify:** RED, then GREEN, then `check.ps1 -Stage task`.

### Task 5: Docs + end-to-end verification
- **Files:** `README.md`
- **Acceptance (criterion 9):**
  - README mentions `rag-eval.ps1`, `check.ps1`, and the re-index on chunker change.
  - `rag-index.ps1` on the real `C:\llm\rag\docs` reports a full rebuild once, then reports `0 changed`.
- **Verify:**
  - `check.ps1 -Stage task`.
  - `eval.ps1 -Category facts-local` before and after, compared with `-Compare`. This needs `start.ps1 -Profile speed`, which uses about 20 GB of shared memory; I'll ask before starting it.

### Checkpoint B: before /review
Full `check.ps1 -Stage task` passes; the diff stays under ~300 lines.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Heading boundaries shift BM25 length normalisation and `RagAutoMin = 4.0` starts dropping hits in `auto` mode | Retrieval eval at every task; compare `rag-index.ps1 -Query` scores on real docs before and after; the spec forbids retuning without asking |
| Pester 3.4 has fewer assertion forms | Stick to `Should Be`, `Should BeLessThan`, `Should Match` |
| A regex-heavy rewrite gets slower on large docs | Build and load times are measured by rag-eval; revert if much slower |

## Parallelization

None. Tasks 2–4 all edit `Split-Doc` in order.
