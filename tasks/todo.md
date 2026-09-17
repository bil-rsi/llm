# Todo: RAG chunking correctness upgrade

- [x] Task 1: Pester harness + chunker version (B4)
- [x] Task 2: Block parser + heading boundaries + document order (B1, B3)
- [x] Task 1b: Relative file names for short/trailing-slash roots (B6)
- [x] Task 3: Boundary-aware overlap and hard splits (B2, B5)
- [x] Checkpoint A: fixture chunk dump + retrieval eval vs baseline (retrieval 1.0; tables still cut mid-row -> Task 4)
- [x] Task 4: Code fences kept whole
- [x] Task 5: README + end-to-end verification (facts-local 100% -> 100%; live index rebuilt once, then 0 changed)
- [x] Checkpoint B: check.ps1 -Stage task green (26 tests), rag.psm1 diff ~120 lines
- [x] /code-simplify: no behaviour-preserving simplification worth a change
- [x] /review: 1 Critical fixed (text dropped from code files / heading-only sections, chunker v3); facts-local eval 100% on v1, v2, v3
