# Performance

Measured on this laptop (i7-1355U, 40 GB RAM, Iris Xe, Windows 11 + Docker Desktop/WSL2), 2026-09-20, with
`scripts\platform-bench.ps1` (1000 iterations per operation, 1000 memories seeded with real embeddings).

**Read this first.** The <2 ms goal applies to the platform's *own* work: API handling, database queries, memory
retrieval and context construction. It does **not** apply to a complete answer. On this hardware the model produces
about 6–7 tokens/s, so a short reply takes seconds. That is the model, not the platform. Every stage below is measured
separately and recorded per request (`Server-Timing` header, `model_responses.stage_ms`, `/metrics`, dashboard).

## 1. Platform overhead (no model involved)

| Stage | p50 | p95 | p99 | Target |
|---|---|---|---|---|
| Database `SELECT 1` (pooled, prepared) | 0.18 ms | 0.32 ms | 0.69 ms | ✅ |
| Recent 20 messages of a conversation | 0.24 ms | 0.45 ms | 0.94 ms | ✅ |
| Insert message / short-term memory | 0.96 ms | 2.04 ms | 3.11 ms | ✅ |
| Audit append (hash-chained, advisory lock) | 1.32 ms | 2.49 ms | 2.84 ms | ✅ |
| Context construction (40 turns + memories, token budgeting) | 0.012 ms | 0.030 ms | 0.038 ms | ✅ |
| Filesystem tool: read a file (container tmpfs) | 0.13 ms | 0.24 ms | 0.50 ms | ✅ |
| Filesystem tool: list 50 entries (container tmpfs) | 0.44 ms | 0.71 ms | 1.24 ms | ✅ |
| HTTP `GET /health/live` in-container (full middleware) | 1.42 ms | 2.67 ms | 3.54 ms | ✅ |
| HTTP `GET /api/memories?limit=10` in-container (auth + DB + JSON) | 5.1 ms | 7.8 ms | 9.8 ms | ⚠️ see §3 |
| **Memory retrieval, embedding cached** (gate + hybrid SQL + fusion + ranking) | **4.7 ms** | 6.2 ms | 7.2 ms | ⚠️ see §3 |
| Filesystem tool: read a file on the **Windows bind mount** | 17.3 ms | 21.3 ms | 22.5 ms | ⚠️ Docker Desktop file sharing |
| HTTP from the host through the published port (curl.exe) | 3.9 ms | 22.9 ms | 27.7 ms | includes process start-up |

## 2. Memory retrieval breakdown

| Step | p50 | Notes |
|---|---|---|
| Query embedding, cache miss (Qwen3-Embedding-0.6B, CPU container) | 98 ms | The dominant cost. ~55 ms for a short question, ~100 ms for the benchmark's longer ones |
| Query embedding, cache hit (LRU 2048) | 0.001 ms | Repeated questions cost nothing |
| Vector search, HNSW top-8 over 1000 memories | 1.76 ms | was 5.1 ms before the fixes below |
| Keyword search, full-text top-8 | 3.54 ms | 1000 memories, GIN index |
| Hybrid (vector + keyword in one round trip) | 4.47 ms | one statement, one round trip |
| Fusion, weighting, dedupe (Python) | < 0.3 ms | Part of the retrieval total |

The pgvector query needed work to become predictable:

| Change | Effect |
|---|---|
| `plan_cache_mode = force_custom_plan` on the pool | Prepared statements switched to a generic plan after 5 executions, which cannot use HNSW for a parameterised `ORDER BY <=>` (3.8 ms seq scan vs 1.1 ms index scan) |
| Migration 0003: search the HNSW index **first**, filter afterwards | With the user/status join inside the same `ORDER BY … LIMIT`, the planner walked all of the user's memories and sorted by distance (3.5 ms, 6387 buffers, detoasting every 4 KB vector). The index scan alone is 0.28 ms |
| Migration 0004: constant candidate pool (64) | `LIMIT k*4` is a parameter in the function's cached plan, so the planner priced HNSW too high and chose a bitmap scan + sort |
| Migration 0005: `enable_sort = off` inside the function | pgvector's cost estimate stays pessimistic on small tables, so the planner still preferred "pkey scan + top-N sort". Penalising sorts makes the index — which already returns rows in distance order — the only sensible plan |
| Hybrid outer query driven by candidate ids | Was driven by `long_term_memories` with two left joins and `IN (… UNION …)`; now joins from at most 2k ids |

## 3. Honest notes on the numbers above

- **`/api/memories?limit=10` at 5.1 ms** is a list endpoint that serialises 10 full memories with `ILIKE` filters. The 2 ms budget is met by the individual pieces (auth ~0.3 ms, query ~1 ms); JSON serialisation and pydantic validation make up the rest.
- **Keyword search** was 5.5 ms when the benchmark seeded 1000 near-identical sentences (every row matched every query term and had to be ranked). With a varied corpus it is 3.5 ms. Your real memories will differ again; the GIN index only ranks what matches.
- **The Windows bind mount** (Docker Desktop file sharing) costs 10–30 ms per file operation, against 0.2 ms on container-local storage. That's the price of the AI reading your real folders; it's still far below model latency. It also varies with host load.
- **Embedding** runs on CPU in a container. Measurements taken while another model was loaded in Ollama were noticeably slower, and the numbers here were taken with a busy host: treat ±30 % as normal.
- The benchmark measures what the running platform does; it doesn't mock anything.

## 4. Model performance (the dominant cost)

Qwen3.6-35B-A3B Q4_K_M via Ollama on the Iris Xe (Vulkan), 16K context:

| Metric | Value |
|---|---|
| Cold load (first request after start) | 26–30 s |
| Time to first token, warm, short prompt | 1.1–3.7 s (varies with host load and iGPU clocks) |
| Generation | 6.4–7.1 tokens/s |
| Prompt processing | 8–43 tokens/s on short prompts (high variance); ~65 tokens/s on longer ones |
| Full short answer (20 tokens, warm) | 4.0–6.7 s |

Live end-to-end turns (memory + tools + model, from `bench/e2e_live.py`, first run before the prompt-cache change):

| Turn | Wall time | Platform share |
|---|---|---|
| "Remember that …" (includes a `memory.search` tool round) | 143 s | 0.8 ms DB + 0.2 ms context + 585 ms retrieval; the rest is the model |
| Recall in a new chat | 10.1 s | 259 ms retrieval (cold embedding), 0.2 ms context |
| Read a file and answer | 32.4 s | 15 ms tool, 0.3 ms context |
| Delete → approval → denied | 30.0 s | 0.1 ms permission decision |
| Blocked SSRF fetch | 80.3 s | 4 ms to block |
| Fetch an allowlisted page | 100.6 s | 7.2 s tool (network), rest model |

The platform contributes single-digit milliseconds to each of those turns.

## 5. What makes a turn faster

1. **Keep the model loaded.** `keep_alive` is 30 minutes. A cold load costs ~26 s.
2. **Prompt-cache-friendly ordering** (implemented): the system prompt and tool schemas, then history, then the per-turn memory block right before your message. The stable prefix lets the runtime reuse its KV cache instead of re-processing ~2.3k tokens of tool schemas every turn.
3. **Tool schemas cost tokens.** 17 tools ≈ 9.2 KB of JSON ≈ 2.3k tokens of prompt. Disabling tools you don't need (Permissions → Tools) shortens every prompt.
4. **Context budget.** The builder caps memories at 15 % and short-term at 10 % of the budget, and folds old turns into a summary.
5. **Embedding cache.** Repeated or similar questions skip the 50–100 ms embedding.
6. **Background jobs never block you**: extraction and summarisation wait for an idle model and are cancelled the moment you send a message.

## 6. Reproducing

```powershell
powershell -ExecutionPolicy Bypass -File C:\llm-platform\scripts\platform-bench.ps1 -N 1000 -Memories 1000 -LlmRuns 5
```
Results land in `platform\bench\results\bench-<timestamp>.json` (host + in-container sections). `-LlmRuns 0` skips the
model part, which is useful while you're using Ollama for something else. `bench/sql_variants.py` compares retrieval SQL
variants on real embeddings, and `bench/e2e_live.py` runs the live end-to-end scenarios.
