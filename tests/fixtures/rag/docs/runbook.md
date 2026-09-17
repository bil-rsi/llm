# Local LLM runbook

Operational notes for the llama.cpp server on the Iris Xe laptop. Each section answers one question that comes up when the server misbehaves.

## Server will not start

The most common cause is a second llama-server process still holding the port. Run `stop.ps1` first; it stops every llama-server process and waits for the port to close. If the log ends with "failed to allocate buffer", the model and context do not fit in the shared GPU memory: lower `-Ctx`, switch the KV cache to `q8_0`, or use the 35B-A3B model instead of the 27B dense one. A log line mentioning "vk::DeviceLostError" means the Intel driver reset during load; reboot, then retry with `-Ub 256` because large micro-batches are what trigger the reset on this driver version. If none of this applies, start with `-Backend cpu -Ngl 0` to confirm the model file itself is valid, then compare its hash with `models\expected-sha256.txt` before downloading it again.

## Slow generation

Generation speed on this laptop is limited by memory bandwidth, not compute. Expect roughly 6 tokens per second on the MoE model and under 1 token per second on the 27B dense model. If generation is much slower than that, check `status.ps1` for shared memory above 19 GB, which means the system started paging. Closing the browser usually frees 2 to 3 GB. Speculative decoding with `-Spec ngram-mod` helps most on code and structured output where the answer repeats earlier text.

## Reasoning budget

The think router decides per prompt whether to enable reasoning. It scores the prompt with keyword and length heuristics and turns thinking on when the score reaches the profile threshold. The reasoning budget caps how many thinking tokens are spent before the model is forced to answer. Raising the budget above 4096 on the speed profile rarely improves accuracy and makes answers take minutes.

## Structured output

Pass a JSON schema with `-Schema` and the server constrains sampling to valid JSON. Grammars in GBNF format are an alternative for non-JSON formats.

```powershell
ask.ps1 "Classify: the invoice is overdue" -Schema C:\llm\schemas\classify.json
ask.ps1 "List the people mentioned" -Schema C:\llm\schemas\extract-entities.json -NoThink
```

When a schema is set, thinking is disabled automatically because reasoning text would break the JSON constraint.

## Port and firewall

The server binds to 127.0.0.1 only, so no firewall rule is needed and other machines on the network cannot reach it. To expose it on the LAN, pass `-Host 0.0.0.0` and add an inbound rule for the port; there is no API key, so only do this on a trusted network.

## Log rotation

Server logs are appended, never rotated. Each profile writes to its own file named after the profile and model. Delete old logs manually when the logs folder grows past a few hundred megabytes; the scripts recreate the files on the next start.
