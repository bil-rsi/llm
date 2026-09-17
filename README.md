# Local Qwen3.6 (llama.cpp b11011, Vulkan on Intel Iris Xe)

Scripts are run with `powershell -ExecutionPolicy Bypass -File C:\llm\scripts\<script>.ps1`. Only one model can run at a time.

## Server profiles (`start.ps1 -Profile <name>`)

| Profile | Model | Ctx | KV | Reasoning (router threshold / budget) | RAG (K / tokens) | Use for |
|---|---|---|---|---|---|---|
| `speed` (default) | 35B-A3B | 16K | f16 | auto (>= 4 / 512) | 2 / 800 | Interactive chat, lookups, extraction |
| `accuracy` | 35B-A3B | 32K | f16 | auto (>= 3 / 3072) | 4 / 2000 | Hard questions, math, planning |
| `deep` | 27B dense | 16K | q8_0 | auto (>= 3 / 4096) | 3 / 1200 | Offline batch jobs only (about 0.8 tok/s) |

Options override the profile: `-Model 27b|35b`, `-Think` (always reason), `-Ctx`, `-Ub`, `-Batch`, `-Ctk/-Ctv`,
`-Spec ngram-mod|ngram-cache|draft-simple`, `-Draft <gguf> -DraftMax N`, `-ReasoningBudget N`, `-BuildDir <dir>`, `-Backend cpu`, `-Port`.
Ub/Spec values are provisional until confirmed by the bench sweeps.

## Commands

| Task | Command |
|---|---|
| Start / stop | `start.ps1 -Profile speed` / `stop.ps1` |
| One question | `ask.ps1 "question" [-Think\|-NoThink] [-Rag on\|auto] [-Schema C:\llm\schemas\classify.json] [-ShowThinking]` |
| Terminal chat | `chat.ps1 [-Rag auto]` (commands `/think on\|off\|auto`, `/rag on\|off\|auto`, `/schema <file>\|off`, `/show`); `-Direct -Model 35b` without server |
| Override the think router | Start the prompt with `/think` or `/nothink` |
| Index documents for RAG | Put .md/.txt/code in `C:\llm\rag\docs`, run `rag-index.ps1`; inspect with `rag-index.ps1 -Query "..."` |
| Resource usage | `status.ps1` |
| Benchmark sweep | `bench.ps1 -Model 35b -Ub 256,512,1024,2048 -Pp 512,2048 -Tg 32 -Tag ub` (results in `logs\bench\`) |
| KV cache sweep | `bench.ps1 -Model 27b -Ctk f16,q8_0,q4_0 -Ctv f16,q8_0,q4_0 -Depth 0,8192 -Tg 32 -Reps 2 -Tag kv` |
| Accuracy eval | `eval.ps1 [-Think auto\|on\|off] [-Reps 2] [-Category math] [-Tag note]` (results in `logs\eval\`) |
| Compare two evals | `eval.ps1 -Compare logs\eval\a.json logs\eval\b.json` |
| Check think router | `eval.ps1 -RouterOnly` (uses the running profile's threshold) |

- **Web UI:** http://127.0.0.1:8080
- **OpenAI-compatible API:** http://127.0.0.1:8080/v1 (model ids `qwen3.6-27b` / `qwen3.6-35b-a3b`, no API key). Per request: `chat_template_kwargs: {"enable_thinking": true}`, `response_format: {"type": "json_schema", ...}`.
- **Logs:** `C:\llm\logs\server-<profile>-<model>.log`. Baseline benchmarks: `C:\llm\logs\bench-*.txt`.
- **Eval set:** `C:\llm\eval\evalset.jsonl` (starter set of 21 items; add your own). Schemas: `C:\llm\schemas\`, grammars: `C:\llm\grammars\`.
