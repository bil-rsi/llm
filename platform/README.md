# Local AI platform for Qwen3.6

Persistent memory, permission-controlled tools (files, shell, web) and observability around the model that already runs
on this laptop. Everything stays local: the only listeners are `127.0.0.1:8090` (this platform) and `127.0.0.1:11434`
(Ollama). Postgres, the embedding server and the shell sandboxes are not reachable from the host or the network.

```
Browser ── 127.0.0.1:8090 ──►  backend (FastAPI)
   • llama.cpp web UI at /            • admin console at /admin          • OpenAPI at /docs
                                   │
        ┌──────────────────────────┼─────────────────────────────┬────────────────────────┐
   short-term memory        long-term memory (pgvector)      tool system            model provider
   messages, task state     hybrid semantic + keyword        files · shell · web    Ollama (or llama.cpp)
        └──────────────────────────┴─────────────────────────────┴────────────────────────┘
                              PostgreSQL 18 (Docker, internal network)
```

## Start

```powershell
powershell -ExecutionPolicy Bypass -File C:\llm-platform\scripts\platform-setup.ps1    # once: secrets + C:\AIWorkspace
powershell -ExecutionPolicy Bypass -File C:\llm-platform\scripts\platform-up.ps1 -Build
```

Then open **http://127.0.0.1:8090/admin** and sign in as `admin` with the password from
`platform\secrets\admin_bootstrap.txt` (you'll be asked to change it). The chat is at **http://127.0.0.1:8090**.

| | |
|---|---|
| Chat (llama.cpp web UI, unmodified) | http://127.0.0.1:8090 |
| Admin console | http://127.0.0.1:8090/admin |
| API docs | http://127.0.0.1:8090/docs |
| Stop / backup / restore / checks / benchmarks | `scripts\platform-down.ps1`, `platform-backup.ps1`, `platform-restore.ps1`, `platform-check.ps1`, `platform-bench.ps1` |

Only one model fits in RAM at a time. To use llama.cpp instead of Ollama: quit Ollama from the tray, run
`C:\llm\scripts\start.ps1`, then switch the provider in Admin → Model.

## What it does

- **Remembers.** Say "remember that …" and it's stored immediately. Otherwise a background job proposes memories from
  your messages, and they're deduplicated, scored and (when they come from web or tool content) held for your approval.
  Retrieval is hybrid (pgvector HNSW + full-text), fused and capped to a token budget.
- **Uses tools under your rules.** Files, shell and web, each call validated, permission-checked, sandboxed and audited.
  Modes: **Normal** (asks before changes), **Autonomous** (approved work runs, destructive stays blocked),
  **Bypass** (no prompts, plus extra folders, any shell command, LAN targets you list, unrestricted internet).
- **Stays inside its box.** Only `C:\AIWorkspace` and folders you add are visible. The web tools can't reach loopback,
  your LAN or the platform's own services. The model can't change its own permissions.
- **Shows its work.** Every tool call, approval, memory change and login is in an append-only, hash-chained audit log,
  with a dashboard for latency, tokens, tool usage and blocked operations.

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Containers, contexts, chat turn, tool call, providers, decisions |
| [docs/DATABASE.md](docs/DATABASE.md) | PostgreSQL 18 + pgvector, roles, every table, hot queries, migrations |
| [docs/MEMORY.md](docs/MEMORY.md) | Short- and long-term memory, lifecycle, retrieval, context budget |
| [docs/TOOLS.md](docs/TOOLS.md) | Tool catalogue, modes, hard limits, filesystem/shell/web details, approvals |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, boundaries, 20 attack classes with tests, audit results |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | Measured latencies per stage, retrieval tuning, model numbers |
| [docs/API.md](docs/API.md) | REST and OpenAI-compatible endpoints, auth, metrics |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Layout, workflows, tests, conventions, secret rotation |
| [CONSTRAINTS.md](CONSTRAINTS.md) | The quality bar enforced by `platform-check.ps1` |

## Known limitations

- **The model is the bottleneck.** ~6.5 tokens/s and 1–4 s to the first token on the Iris Xe. The platform adds single-digit milliseconds; a full answer still takes seconds. Only one model fits in 40 GB RAM, so Ollama and llama.cpp can't both hold a 22 GB model at once.
- **Files on the Windows bind mount are ~17 ms per operation** (Docker Desktop file sharing) against 0.2 ms inside the container.
- **Query embedding costs ~55–100 ms** on the CPU container when the answer isn't in the LRU cache.
- **`filesystem.open` does not launch programs.** It returns details, a preview and the Windows path; opening is yours to do. A container cannot (and should not) start processes on Windows.
- **Secrets are redacted from tool output**, so the model can't round-trip an edit of a file containing API keys.
- **Shell commands run in a Linux sandbox**, not on Windows, and only see the mounted folders. There is no PowerShell.
- **Taint escalation is off by default** (your choice). In Autonomous/Bypass a malicious web page or file can steer actions those modes already allow without asking. Switch it on under Permissions → Other.
- **Web search uses DuckDuckGo's HTML endpoint.** It can break if they change the markup; SearXNG is available as an optional profile.
- **Single user.** The schema carries `user_id` throughout, but there is no user management UI.
- **The model can't see images** through this platform (the UI's vision features are not wired to the tools).

## Possible next steps

- Re-embed memories with a larger embedding model, or run embeddings on the iGPU, if retrieval quality or latency matters more than RAM.
- Memory consolidation: periodically merge related memories into summaries so the store stays small as it grows.
- A pgvector `halfvec` index (2 bytes per dimension) if the memory store grows past ~100k entries.
- Per-tool metrics in the dashboard with sparklines, and an export of the audit log.
- MCP support, so tools you already have elsewhere can be registered through the same permission engine.
- A second workspace profile (for example "work" and "personal") with separate memory scopes.
