# Local Qwen3.6 (llama.cpp b11011, Vulkan on Intel Iris Xe)

Scripts are run with `powershell -ExecutionPolicy Bypass -File C:\llm\scripts\<script>.ps1`. Only one model can run at a time.

| Task | Command |
|---|---|
| Start 27B (default: Vulkan, 32K ctx, thinking off) | `start.ps1 -Model 27b` |
| Start 35B-A3B (about 6x faster) | `start.ps1 -Model 35b` |
| Options | `-Think` (reasoning on), `-Ctx 65536`, `-Backend cpu -Threads 6 -Ngl 0`, `-Port 8081` |
| Stop | `stop.ps1` |
| Terminal chat (server running) | `chat.ps1` |
| Terminal chat without server | `chat.ps1 -Direct -Model 27b` |
| Resource usage | `status.ps1` |
| Benchmark | `bench.ps1 -Model 27b -Backend vulkan -Ngl 99 -Threads 6` |

- **Web UI:** http://127.0.0.1:8080
- **OpenAI-compatible API:** http://127.0.0.1:8080/v1 (model ids `qwen3.6-27b` / `qwen3.6-35b-a3b`, no API key)
- **Logs:** `C:\llm\logs\server-<model>.log`. Benchmarks are in `C:\llm\logs\bench-*.txt`.

## Hardware notes
- Laptop CPU: Intel Core i7-1355U (2 performance cores, 8 efficient cores, 12 threads), 40 GB RAM.
- Integrated GPU: Intel Iris Xe, about 20 GB of shared memory usable by Vulkan. Driver 32.0.101.7084.
- Baseline llama-bench (Vulkan, ngl 99, 6 threads): 27B dense pp512 12.6 t/s, tg128 0.83 t/s; 35B-A3B pp512 58.8 t/s, tg128 5.95 t/s.
- Generation speed is limited by memory bandwidth, so the MoE model (about 3B active parameters) is roughly 7x faster than the 27B dense model.
