# Spec: RAG chunking correctness upgrade

Status: **APPROVED 2026-09-17**

## Objective

Make `Split-Doc` in `scripts\rag.psm1` produce chunks that are in document order, labelled with the right heading, and start and end on sensible boundaries. Also make the index rebuild itself when the chunker changes. The user is the single person running `ask.ps1` / `chat.ps1` / `eval.ps1` with `-Rag on|auto` on this laptop.

**Why a correctness fix, not a quality upgrade.** The retrieval fixture (`tests\fixtures\rag`, 18 queries over 3 docs) already scores 1.0 on hit@1, MRR and recall. Per `CONSTRAINTS.md`, a ranking or size change must beat the baseline or be reverted, and there is no headroom to show a gain. So nothing changes the chunk size, overlap, BM25 parameters, `MinRel` or `RagAutoMin`. The bugs below were reproduced directly (2026-09-17):

| # | Bug | Reproduction |
|---|---|---|
| B1 | A paragraph longer than `$Size` is emitted **before** the buffered text that came ahead of it | `# A` / short intro / 1,800-char paragraph → chunk 0 is the long paragraph, chunk 1 is the intro |
| B2 | Overlap is cut at a fixed character offset, so chunks start mid-word | 8 paragraphs of ~200 chars → chunk 1 starts with `5 word5` |
| B3 | The overlap carried into the next chunk is labelled with the *next* heading | `# Alpha` (1,100 chars) / `# Beta` → chunk 1 has heading `Beta` but starts with Alpha's text |
| B4 | Unchanged files keep chunks produced by an older chunker | `Update-RagIndex` reuses chunks when mtime matches (`rag.psm1:51`); the index has no chunker version |
| B6 | Stored file names are mangled when the RAG root is an 8.3 short path or ends in `\` (`odels.md`) | `Update-RagIndex` strips `docs.Length + 1` chars from long-form child paths (`rag.psm1:78`); found during Task 2, added with approval |
| B5 | Hard splits of long paragraphs and code blocks ignore sentence and line boundaries | Same cut as B2, applied inside the long-paragraph loop (`rag.psm1:24`) |

## Acceptance criteria

1. Chunks are in document order: joining each chunk's non-overlap text reproduces every paragraph in source order.
2. No chunk starts or ends mid-word. The overlap starts at a sentence boundary, or at a word boundary if no sentence boundary exists within the overlap window.
3. A new heading starts a new chunk. Overlap never crosses a heading, so each chunk's `heading` is the heading its first line falls under.
4. A fenced code block (```` ``` ````) no longer than `$Size` is never split, even if it contains blank lines. A longer one is split on line boundaries.
5. Chunk text length is ≤ `$Size` + `$Overlap`, except for a single word that is itself longer than that.
6. `index.json` records `chunker = <int>`. When it differs from the module's value, `Update-RagIndex` re-chunks every file and reports it in its output line.
7. `Search-Rag`, `Format-RagPrompt`, `Update-RagIndex` and `Get-Tokens` keep their signatures and output shapes (`id`, `file`, `heading`, `text`).
8. `check.ps1 -Stage task` passes: the retrieval eval has no metric below the baseline, and there are no new lint warnings.
9. `README.md` mentions that changing the chunker triggers a full re-index.

## Commands

| Purpose | Command |
|---|---|
| Unit tests | `Invoke-Pester C:\llm\tests` |
| All task checks | `powershell -ExecutionPolicy Bypass -File C:\llm\scripts\check.ps1 -Stage task` |
| Retrieval eval | `scripts\rag-eval.ps1 -Baseline tests\fixtures\rag\baseline.json` |
| End to end (server running) | `check.ps1 -Stage full`, then `eval.ps1 -Compare <before> <after>` |
| Manual inspection | `rag-index.ps1` then `rag-index.ps1 -Query "port"` |

## Project structure

- `scripts\rag.psm1`: the only production file changed (`Split-Doc`, `Update-RagIndex`)
- `tests\rag.Tests.ps1`: new Pester 3.4 tests
- `tests\fixtures\rag\`: retrieval eval corpus, queries and baseline (added by /constraints)
- `README.md`: one line on re-indexing

## Code style

Match `rag.psm1`: dense one-line statements, `System.Collections.ArrayList`, no new modules or dependencies, PowerShell 5.1 syntax. `Split-Doc` stays a pure function of (`Rel`, `Text`, `Size`, `Overlap`), so it can be tested without disk access.

## Testing strategy

- Write a failing test first for each bug B1–B5 and for acceptance criteria 4 and 6. `Split-Doc` is internal, so tests call it through `& (Get-Module rag) { Split-Doc ... }`.
- Test the rebuild trigger (B4) with a temporary `$RagRoot` set the same way `rag-eval.ps1` does. Tests never touch `C:\llm\rag`.
- Regression: the retrieval eval must match the baseline, and `eval.ps1 -Category facts-local` must not drop (checked at /review, needs the server).

## Boundaries

- **Always:** keep the public function signatures; run `check.ps1 -Stage task` before marking a task done; leave `logs\server-*.log` out of commits.
- **Ask first:** changing `$Size`, `$Overlap`, BM25 parameters, `MinRel` or `RagAutoMin`; adding any dependency; installing Pester 5; committing; anything involving a push or remote.
- **Never:** edit `C:\llm\rag\docs` or the live index during tests; weaken `CONSTRAINTS.md` or the baseline to make a check pass.

## Out of scope

- Embeddings or hybrid retrieval
- Markdown table-aware splitting beyond not cutting mid-line
- Making the fixture harder (tracked as a known gap in `CONSTRAINTS.md`)
- Cleaning up `.gitignore` and the committed model files

## Decisions

- Headings are hard chunk boundaries (approved 2026-09-17).
- Found at /review: headings and fences are recognised only in `.md`/`.markdown`. Other indexed files are plain paragraphs, cut at line ends (`.txt` at sentence ends), because `#` comments in `.ps1`/`.py`/`.yaml` were being taken as headings and dropped. A heading with no body (e.g. `# Parent` directly before `## Child`) is kept as leading text of the next section, whose `heading` is the innermost one. Chunker version 3.
- After /ship NO-GO (user chose to fix all findings), chunker version 4:
  - Headings with no body stay within `Size + Overlap` and are flushed as their own chunk when needed.
  - Heading labels are capped at 120 chars.
  - A fence closes only on the same marker char, at least as long, with nothing after it.
  - Overlap starts on a line when the overlap window holds table or fence lines.
  - A buffer no longer than the overlap merges with the next piece instead of being duplicated.
  - `Size` is clamped to at least 1.
  - Blocks are plain arrays; per-block objects and pipelines caused the 20–50× slowdown.

  Measured 2026-09-17, min of 3 runs, `master` → v3 → v4:

  | Input | `master` | v3 | v4 |
  |---|---|---|---|
  | 400 KB of 1-char paragraphs | 1.5 s | ~56 s | 4.6 s |
  | 1 MB prose | 49 ms | 318 ms | 134 ms |
  | Fixture ×20 | 20 ms | 129 ms | 37 ms |

  The remaining ≤3× overhead appears only on pathological inputs; this is accepted.
