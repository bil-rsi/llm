-- 0001_init: full initial schema (schema app, owned by aimem_owner). Applied by `python -m aiplatform.migrate`.
-- Enums are text + CHECK (cheaper to evolve than CREATE TYPE). uuidv7() is native in PostgreSQL 18.

CREATE OR REPLACE FUNCTION app.touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- ───────────────────────────── Identity ─────────────────────────────
CREATE TABLE app.users (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  username            text NOT NULL CHECK (username ~ '^[a-zA-Z0-9_.-]{3,64}$'),
  password_hash       text NOT NULL,
  must_change_password boolean NOT NULL DEFAULT true,
  created_at          timestamptz NOT NULL DEFAULT now(),
  password_changed_at timestamptz,
  disabled_at         timestamptz
);
CREATE UNIQUE INDEX users_username_uq ON app.users (lower(username));

CREATE TABLE app.api_tokens (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  user_id      uuid NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
  name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  token_hash   bytea NOT NULL UNIQUE,
  scopes       text[] NOT NULL CHECK (scopes <@ ARRAY['chat','read','admin']::text[] AND cardinality(scopes) > 0),
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  expires_at   timestamptz,
  revoked_at   timestamptz
);

CREATE TABLE app.sessions (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  user_id      uuid NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
  token_hash   bytea NOT NULL UNIQUE,
  csrf_hash    bytea NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  elevated_until timestamptz,                       -- step-up auth (password re-entry) for Bypass/permission edits
  expires_at   timestamptz NOT NULL,
  user_agent   text
);
CREATE INDEX sessions_expires_idx ON app.sessions (expires_at);

-- ───────────────────────────── Conversation ─────────────────────────────
CREATE TABLE app.conversations (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  user_id          uuid NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
  title            text NOT NULL DEFAULT '' CHECK (length(title) <= 200),
  fingerprint      bytea,
  client           text NOT NULL DEFAULT 'api' CHECK (client IN ('webui','api','admin')),
  summary          text NOT NULL DEFAULT '',
  summary_upto_seq integer NOT NULL DEFAULT 0,
  message_count    integer NOT NULL DEFAULT 0,
  token_count      integer NOT NULL DEFAULT 0,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  archived_at      timestamptz
);
CREATE INDEX conversations_fp_idx ON app.conversations (user_id, fingerprint) WHERE fingerprint IS NOT NULL;
CREATE INDEX conversations_recent_idx ON app.conversations (user_id, updated_at DESC);
CREATE TRIGGER conversations_touch BEFORE UPDATE ON app.conversations FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

CREATE TABLE app.messages (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  conversation_id  uuid NOT NULL REFERENCES app.conversations(id) ON DELETE CASCADE,
  seq              integer NOT NULL,
  role             text NOT NULL CHECK (role IN ('system','user','assistant','tool')),
  content          text NOT NULL,
  tool_calls       jsonb,
  tool_call_id     text,
  token_count      integer NOT NULL DEFAULT 0,
  model_request_id uuid,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conversation_id, seq)
);

-- ───────────────────────────── Memory: short-term ─────────────────────────────
-- Conversation-scoped working context: task state, active tool state, scratch notes, rolling summaries.
CREATE TABLE app.short_term_memories (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  conversation_id uuid NOT NULL REFERENCES app.conversations(id) ON DELETE CASCADE,
  kind            text NOT NULL CHECK (kind IN ('task_state','tool_state','note','summary','fact')),
  key             text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
  content         text NOT NULL CHECK (length(content) <= 8000),
  data            jsonb,
  importance      real NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
  token_count     integer NOT NULL DEFAULT 0,
  expires_at      timestamptz NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conversation_id, kind, key)
);
CREATE INDEX stm_expires_idx ON app.short_term_memories (expires_at);
CREATE TRIGGER stm_touch BEFORE UPDATE ON app.short_term_memories FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

-- ───────────────────────────── Memory: long-term ─────────────────────────────
CREATE TABLE app.long_term_memories (
  id                     uuid PRIMARY KEY DEFAULT uuidv7(),
  user_id                uuid NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
  kind                   text NOT NULL CHECK (kind IN ('preference','fact','project','instruction','decision','context')),
  content                text NOT NULL CHECK (length(content) BETWEEN 1 AND 1000),
  content_hash           bytea NOT NULL,
  tsv                    tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
  importance             real NOT NULL CHECK (importance BETWEEN 0 AND 1),
  confidence             real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  sensitivity            text NOT NULL DEFAULT 'none' CHECK (sensitivity IN ('none','personal','secret')),
  status                 text NOT NULL CHECK (status IN ('candidate','active','superseded','rejected','expired','deleted')),
  source_type            text NOT NULL CHECK (source_type IN ('user_stated','model_extracted','tool_output','web','manual','import')),
  source_conversation_id uuid REFERENCES app.conversations(id) ON DELETE SET NULL,
  source_message_id      bigint REFERENCES app.messages(id) ON DELETE SET NULL,
  source_ref             text CHECK (length(source_ref) <= 2000),
  supersedes_id          uuid REFERENCES app.long_term_memories(id) ON DELETE SET NULL,
  flags                  text[] NOT NULL DEFAULT '{}',
  expires_at             timestamptz,
  last_accessed_at       timestamptz,
  access_count           integer NOT NULL DEFAULT 0,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ltm_dedupe_uq ON app.long_term_memories (user_id, content_hash) WHERE status IN ('candidate','active');
CREATE INDEX ltm_tsv_gin ON app.long_term_memories USING gin (tsv) WHERE status = 'active';
CREATE INDEX ltm_user_status_idx ON app.long_term_memories (user_id, status, kind);
CREATE INDEX ltm_expires_idx ON app.long_term_memories (expires_at) WHERE expires_at IS NOT NULL;
CREATE TRIGGER ltm_touch BEFORE UPDATE ON app.long_term_memories FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

-- One row per (memory, embedding model): switching models re-embeds without losing the old vectors mid-migration.
CREATE TABLE app.memory_embeddings (
  memory_id  uuid NOT NULL REFERENCES app.long_term_memories(id) ON DELETE CASCADE,
  model      text NOT NULL,
  dims       smallint NOT NULL CHECK (dims = 1024),
  embedding  vector(1024) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (memory_id, model)
);
CREATE INDEX memory_embeddings_hnsw ON app.memory_embeddings USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ───────────────────────────── Tools & permissions ─────────────────────────────
-- Tool catalogue; synced from the code registry at startup (description/schema/risk), `enabled` is yours.
CREATE TABLE app.tool_definitions (
  name         text PRIMARY KEY CHECK (name ~ '^[a-z]+\.[a-z_]+$'),
  category     text NOT NULL CHECK (category IN ('filesystem','shell','web','memory')),
  description  text NOT NULL,
  risk         text NOT NULL CHECK (risk IN ('read','write','destructive','execute','network')),
  input_schema jsonb NOT NULL,
  enabled      boolean NOT NULL DEFAULT true,
  version      integer NOT NULL DEFAULT 1,
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER tool_definitions_touch BEFORE UPDATE ON app.tool_definitions FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

-- Permission rules. Most specific match wins (priority, then deny > confirm > allow). Seeded from config/permissions.yaml.
CREATE TABLE app.tool_permissions (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  tool_pattern  text NOT NULL CHECK (tool_pattern ~ '^[a-z*]+(\.[a-z_*]+)?$'),
  mode          text NOT NULL CHECK (mode IN ('*','normal','autonomous','bypass')),
  scope_type    text NOT NULL DEFAULT 'any' CHECK (scope_type IN ('any','path','domain','command')),
  scope_pattern text NOT NULL DEFAULT '*' CHECK (length(scope_pattern) BETWEEN 1 AND 500),
  effect        text NOT NULL CHECK (effect IN ('allow','confirm','deny')),
  priority      integer NOT NULL DEFAULT 100,
  enabled       boolean NOT NULL DEFAULT true,
  note          text NOT NULL DEFAULT '',
  origin        text NOT NULL DEFAULT 'admin' CHECK (origin IN ('seed','admin')),
  created_by    uuid REFERENCES app.users(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tool_permissions_lookup_idx ON app.tool_permissions (mode, enabled);
CREATE TRIGGER tool_permissions_touch BEFORE UPDATE ON app.tool_permissions FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

-- Filesystem roots the tools may use. Default zones live under /workspace; extra folders are mounted by platform-up.ps1.
CREATE TABLE app.workspace_roots (
  name           text PRIMARY KEY CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,39}$'),
  host_path      text NOT NULL,
  container_path text NOT NULL UNIQUE CHECK (container_path ~ '^/(workspace|extra)/'),
  access         text NOT NULL CHECK (access IN ('ro','rw')),
  kind           text NOT NULL CHECK (kind IN ('zone','extra')),
  enabled        boolean NOT NULL DEFAULT true,
  bypass_only    boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER workspace_roots_touch BEFORE UPDATE ON app.workspace_roots FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

-- Internet allowlist (public domains) and, for Bypass, private/LAN/localhost targets.
CREATE TABLE app.network_allowlist (
  id         uuid PRIMARY KEY DEFAULT uuidv7(),
  kind       text NOT NULL CHECK (kind IN ('public_domain','private_host')),
  host       text NOT NULL CHECK (length(host) BETWEEN 1 AND 253),
  port       integer CHECK (port BETWEEN 1 AND 65535),     -- NULL = default ports (80/443)
  methods    text[] NOT NULL DEFAULT '{GET,HEAD}' CHECK (methods <@ ARRAY['GET','HEAD','POST','PUT','PATCH','DELETE']::text[]),
  enabled    boolean NOT NULL DEFAULT true,
  note       text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, host, port)
);

-- Runtime permission settings (mode, internet mode, bypass capabilities, taint escalation...). Human-only writes.
CREATE TABLE app.permission_settings (
  key        text PRIMARY KEY CHECK (key ~ '^[a-z_.]+$'),
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES app.users(id) ON DELETE SET NULL
);

-- Non-permission runtime settings you change in the admin console (model parameters, provider choice...).
CREATE TABLE app.runtime_settings (
  key        text PRIMARY KEY CHECK (key ~ '^[a-z_.]+$'),
  value      jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by uuid REFERENCES app.users(id) ON DELETE SET NULL
);

-- ───────────────────────────── Model ─────────────────────────────
CREATE TABLE app.model_requests (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  conversation_id   uuid REFERENCES app.conversations(id) ON DELETE SET NULL,
  request_id        text NOT NULL,
  correlation_id    text NOT NULL,
  provider          text NOT NULL,
  model             text NOT NULL,
  purpose           text NOT NULL CHECK (purpose IN ('chat','extract','summarise','title','bench')),
  params            jsonb NOT NULL DEFAULT '{}',
  context_budget    integer,
  prompt_tokens_est integer,
  memories_injected integer NOT NULL DEFAULT 0,
  tool_round        integer NOT NULL DEFAULT 0,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX model_requests_created_idx ON app.model_requests (created_at DESC);
CREATE INDEX model_requests_conv_idx ON app.model_requests (conversation_id);

CREATE TABLE app.model_responses (
  model_request_id  uuid PRIMARY KEY REFERENCES app.model_requests(id) ON DELETE CASCADE,
  status            text NOT NULL CHECK (status IN ('ok','error','cancelled','timeout')),
  finish_reason     text,
  prompt_tokens     integer,
  completion_tokens integer,
  ttft_ms           real,
  total_ms          real,
  tokens_per_s      real,
  stage_ms          jsonb NOT NULL DEFAULT '{}',
  tool_calls        integer NOT NULL DEFAULT 0,
  error             text,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX model_responses_created_idx ON app.model_responses (created_at DESC);
ALTER TABLE app.messages ADD CONSTRAINT messages_model_request_fk FOREIGN KEY (model_request_id) REFERENCES app.model_requests(id) ON DELETE SET NULL;

-- ───────────────────────────── Tool executions ─────────────────────────────
CREATE TABLE app.tool_executions (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  conversation_id  uuid REFERENCES app.conversations(id) ON DELETE SET NULL,
  model_request_id uuid REFERENCES app.model_requests(id) ON DELETE SET NULL,
  tool_name        text NOT NULL,
  arguments        jsonb NOT NULL,
  status           text NOT NULL CHECK (status IN ('requested','denied','awaiting_approval','approved','running','succeeded','failed','timed_out','expired')),
  decision         text CHECK (decision IN ('allow','confirm','deny')),
  policy_rule      text,
  decision_reason  text,
  mode             text NOT NULL,
  tainted          boolean NOT NULL DEFAULT false,
  dry_run          boolean NOT NULL DEFAULT false,
  requested_at     timestamptz NOT NULL DEFAULT now(),
  started_at       timestamptz,
  finished_at      timestamptz,
  duration_ms      real,
  result_summary   text,
  error            text
);
CREATE INDEX tool_exec_recent_idx ON app.tool_executions (requested_at DESC);
CREATE INDEX tool_exec_open_idx ON app.tool_executions (status) WHERE status IN ('awaiting_approval','running');
CREATE INDEX tool_exec_tool_idx ON app.tool_executions (tool_name, requested_at DESC);

CREATE TABLE app.approvals (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  tool_execution_id uuid NOT NULL UNIQUE REFERENCES app.tool_executions(id) ON DELETE CASCADE,
  summary           text NOT NULL,
  requested_at      timestamptz NOT NULL DEFAULT now(),
  expires_at        timestamptz NOT NULL,
  decided_at        timestamptz,
  decided_by        uuid REFERENCES app.users(id) ON DELETE SET NULL,
  decision          text CHECK (decision IN ('approved','denied','expired')),
  note              text
);
CREATE INDEX approvals_pending_idx ON app.approvals (requested_at) WHERE decision IS NULL;

CREATE TABLE app.filesystem_operations (
  tool_execution_id uuid PRIMARY KEY REFERENCES app.tool_executions(id) ON DELETE CASCADE,
  operation         text NOT NULL,
  root              text NOT NULL,
  path              text NOT NULL,
  dest_path         text,
  bytes             bigint,
  sha256_before     text,
  sha256_after      text,
  backup_path       text
);

CREATE TABLE app.web_requests (
  tool_execution_id uuid PRIMARY KEY REFERENCES app.tool_executions(id) ON DELETE CASCADE,
  method            text NOT NULL,
  url_redacted      text NOT NULL,
  host              text NOT NULL,
  resolved_ip       inet,
  status_code       integer,
  bytes             bigint,
  content_type      text,
  blocked_reason    text,
  duration_ms       real
);

-- ───────────────────────────── Audit (append-only, hash-chained) ─────────────────────────────
CREATE TABLE app.audit_log (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  seq            bigint NOT NULL UNIQUE,
  at             timestamptz NOT NULL DEFAULT now(),
  actor_type     text NOT NULL CHECK (actor_type IN ('user','model','system')),
  actor_id       text,
  action         text NOT NULL,
  target         text,
  outcome        text NOT NULL CHECK (outcome IN ('success','failure','denied','pending','info')),
  request_id     text,
  correlation_id text,
  detail         jsonb NOT NULL DEFAULT '{}',
  prev_hash      bytea NOT NULL,
  hash           bytea NOT NULL
);
CREATE INDEX audit_at_brin ON app.audit_log USING brin (at);
CREATE INDEX audit_action_idx ON app.audit_log (action, at DESC);

CREATE OR REPLACE FUNCTION app.audit_row_digest(p_prev bytea, p_seq bigint, p_at timestamptz, p_actor_type text, p_actor_id text,
    p_action text, p_target text, p_outcome text, p_request_id text, p_correlation_id text, p_detail jsonb)
RETURNS bytea LANGUAGE sql IMMUTABLE AS $$
  SELECT sha256(p_prev || convert_to(concat_ws(chr(31), p_seq::text, (extract(epoch FROM p_at) * 1000000)::bigint::text,
         p_actor_type, coalesce(p_actor_id, ''), p_action, coalesce(p_target, ''), p_outcome,
         coalesce(p_request_id, ''), coalesce(p_correlation_id, ''), p_detail::text), 'UTF8'))
$$;

-- The chain is computed in the database under a transaction-level advisory lock, so the app cannot forge or fork it.
CREATE OR REPLACE FUNCTION app.audit_chain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE last_seq bigint; last_hash bytea;
BEGIN
  PERFORM pg_advisory_xact_lock(7373001);
  SELECT seq, hash INTO last_seq, last_hash FROM app.audit_log ORDER BY seq DESC LIMIT 1;
  NEW.seq := coalesce(last_seq, 0) + 1;
  NEW.prev_hash := coalesce(last_hash, '\x'::bytea);
  NEW.at := coalesce(NEW.at, now());
  NEW.hash := app.audit_row_digest(NEW.prev_hash, NEW.seq, NEW.at, NEW.actor_type, NEW.actor_id, NEW.action, NEW.target,
                                   NEW.outcome, NEW.request_id, NEW.correlation_id, NEW.detail);
  RETURN NEW;
END $$;
CREATE TRIGGER audit_chain_ins BEFORE INSERT ON app.audit_log FOR EACH ROW EXECUTE FUNCTION app.audit_chain();

CREATE OR REPLACE FUNCTION app.audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'audit_log is append-only'; END $$;
CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE ON app.audit_log FOR EACH ROW EXECUTE FUNCTION app.audit_immutable();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON app.audit_log FOR EACH STATEMENT EXECUTE FUNCTION app.audit_immutable();

-- Returns the first seq whose hash does not verify (NULL = chain intact).
CREATE OR REPLACE FUNCTION app.audit_verify() RETURNS bigint LANGUAGE plpgsql STABLE AS $$
DECLARE r record; prev bytea := '\x'::bytea;
BEGIN
  FOR r IN SELECT * FROM app.audit_log ORDER BY seq LOOP
    IF r.prev_hash <> prev OR r.hash <> app.audit_row_digest(r.prev_hash, r.seq, r.at, r.actor_type, r.actor_id, r.action,
        r.target, r.outcome, r.request_id, r.correlation_id, r.detail) THEN RETURN r.seq; END IF;
    prev := r.hash;
  END LOOP;
  RETURN NULL;
END $$;

-- ───────────────────────────── Grants (narrow the default DML grant) ─────────────────────────────
REVOKE UPDATE, DELETE, TRUNCATE ON app.audit_log FROM aimem_app;
REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA app FROM aimem_app;
