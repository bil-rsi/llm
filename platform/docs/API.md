# API

Base URL `http://127.0.0.1:8090`, loopback only. The live OpenAPI docs are at **`/docs`** (Swagger UI) and **`/openapi.json`**, and you need to be signed in to use them.

## Authentication

| Method | How | Use |
|---|---|---|
| Session | `POST /admin/login` (form) or `POST /api/auth/login` (JSON) sets the `aip_session` cookie (HttpOnly, SameSite=Strict) | browser: admin console and web UI |
| Bearer token | `Authorization: Bearer aip_<64 hex>`, created in Admin → Account (shown once) | scripts, OpenAI clients, the web UI's "API key" setting |

Scopes are `chat` (`/v1/*`), `read` (GET on `/api/*`, `/metrics`) and `admin` (everything else). `admin` implies the other two.

Rules every request passes:
- **Host header** must be `127.0.0.1:8090` or `localhost:8090`, otherwise **421**. This stops DNS rebinding.
- **Origin**, when present on POST/PUT/PATCH/DELETE, must be same-origin, otherwise **403**.
- **CSRF**: session requests that change state need `X-CSRF-Token` (the value returned at login) or a `csrf` form field. `/v1/chat/completions` from the web UI instead needs a same-origin `Origin`.
- **Elevation**: permission and Bypass changes, and token creation, need `POST /api/auth/elevate {password}` in the same session, valid 10 minutes. **API tokens can never change permissions (403).**

Errors return `{"error": {"code", "message", "detail?"}}` with status 400/401/403/404/409/421/422/429/5xx. Every response carries `X-Request-Id`, `X-Correlation-Id` (send your own to correlate), a `Server-Timing` header with per-stage milliseconds, and the security headers (CSP, `nosniff`, `X-Frame-Options: DENY`, …).

## OpenAI-compatible (web UI and any OpenAI client)

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/chat/completions` | `messages`, `stream`, `temperature`, `top_p`, `max_tokens`, `chat_template_kwargs.enable_thinking` / `think` / `reasoning_effort`. Memory and server-side tools are applied automatically. Tool and memory status arrives as `delta.reasoning_content`. The final chunk has `usage`, llama.cpp-style `timings` and `aip` (conversation id, stages, context stats, injected memory ids). Send `X-Conversation-Id` to pin a conversation. Passing your own `tools` switches to pass-through mode: your tools are offered, tool calls are returned to you, and server tools are off |
| GET | `/v1/models` | active model |
| GET | `/props` | llama.cpp-UI compatible properties (context size, model name) |
| GET | `/slots`, `/tools` | UI compatibility (empty lists) |

```bash
curl -N http://127.0.0.1:8090/v1/chat/completions -H "Authorization: Bearer $AIP_TOKEN" -H "Content-Type: application/json" -d "{\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"What do you remember about my projects?\"}]}"
```

## Platform API (`/api`)

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/login`, `POST /auth/logout`, `POST /auth/password {current,new}`, `POST /auth/elevate {password}`, `GET/POST /auth/tokens`, `DELETE /auth/tokens/{id}` |
| Conversations | `GET /conversations?q=&limit=&offset=`, `GET /conversations/{id}` (messages + short-term memory), `DELETE /conversations/{id}` |
| Memory | `GET /memories?status=&kind=&q=`, `GET /memories/search?q=&mode=hybrid|keyword|semantic`, `POST /memories {content,kind?,ttl_days?}`, `PATCH /memories/{id} {content?,kind?,importance?,expires_at?,clear_expiry?}`, `POST /memories/{id}/approve`, `POST /memories/{id}/reject`, `DELETE /memories/{id}` |
| Tools | `GET /tools` (catalogue + JSON schemas), `GET /tool-executions?tool=&status=` (with filesystem/web detail) |
| Approvals | `GET /approvals` (pending), `POST /approvals/{id} {approve, note?}` |
| Permissions | `GET /permissions` (settings, rules, roots, network). Elevated only: `PUT /permissions/settings/{mode|internet_mode|taint_escalation|shell_network|bypass|defaults} {value}`, `POST/PUT/DELETE /permissions/rules[/{id}]`, `PUT /permissions/tools/{name} {enabled}`, `POST/DELETE /permissions/roots[/{name}]`, `POST/DELETE /permissions/network[/{id}]`. `GET /permissions/mounts` (used by `platform-up.ps1`) |
| Audit | `GET /audit?action=&actor_type=&outcome=&before_seq=&limit=`, `GET /audit/verify` |
| Model | `GET /model` (params, provider health, embedding health, token calibration), `PUT /model/{model.provider|model.temperature|model.top_p|model.num_ctx|model.max_output_tokens|model.think} {value}` |
| Metrics | `GET /metrics/summary?hours=24` (dashboard JSON) |

## Health and metrics

| Path | Auth | Returns |
|---|---|---|
| `GET /health/live` | none | `{"status":"ok"}`. The container healthcheck uses it |
| `GET /health/ready` | none | database, embedding, shell sandbox and model checks. 503 if the DB is down |
| `GET /metrics` | `read` | Prometheus text:<br>`aip_http_requests_total`, `aip_http_request_ms`, `aip_stage_ms{stage}`<br>`aip_model_tokens_total`, `aip_model_ttft_ms`, `aip_model_tokens_per_second`<br>`aip_tool_calls_total{tool,status}`, `aip_blocked_total{tool,reason}`<br>`aip_memory_ops_total{outcome}`, `aip_memories{status}`, `aip_approvals_pending` |

## Admin console (HTML)

`/admin`: Dashboard, Approvals, Memory, Chats, Tool log, Permissions, Audit, Model and Account pages. The pages are server-rendered with forms, and htmx is only used to auto-refresh approvals.
