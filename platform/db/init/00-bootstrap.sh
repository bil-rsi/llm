#!/bin/bash
# First-boot bootstrap (runs once, as the postgres superuser, only when the data volume is empty).
# Creates the least-privilege roles, the application database and extensions. Tables come from db/migrations.
set -euo pipefail

read_secret() { tr -d '\r\n' < "/run/secrets/$1"; }
OWNER_PW="$(read_secret pg_owner)"
APP_PW="$(read_secret pg_app)"
BACKUP_PW="$(read_secret pg_backup)"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v owner_pw="$OWNER_PW" -v app_pw="$APP_PW" -v backup_pw="$BACKUP_PW" <<'SQL'
CREATE ROLE aimem_owner  LOGIN PASSWORD :'owner_pw'  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE aimem_app    LOGIN PASSWORD :'app_pw'    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 40;
CREATE ROLE aimem_backup LOGIN PASSWORD :'backup_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 2;
GRANT pg_read_all_data TO aimem_backup;
CREATE DATABASE aiplatform OWNER aimem_owner;
REVOKE ALL ON DATABASE aiplatform FROM PUBLIC;
GRANT CONNECT ON DATABASE aiplatform TO aimem_app, aimem_backup;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname aiplatform <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE SCHEMA app AUTHORIZATION aimem_owner;
GRANT USAGE ON SCHEMA app TO aimem_app, aimem_backup;
ALTER ROLE aimem_owner IN DATABASE aiplatform SET search_path = app, public;
ALTER ROLE aimem_app   IN DATABASE aiplatform SET search_path = app, public;
ALTER ROLE aimem_app   IN DATABASE aiplatform SET statement_timeout = '15s';
ALTER ROLE aimem_app   IN DATABASE aiplatform SET idle_in_transaction_session_timeout = '30s';
-- Anything aimem_owner creates later is usable (DML only) by the app role; audit tables get narrowed per table.
ALTER DEFAULT PRIVILEGES FOR ROLE aimem_owner IN SCHEMA app GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aimem_app;
ALTER DEFAULT PRIVILEGES FOR ROLE aimem_owner IN SCHEMA app GRANT USAGE, SELECT ON SEQUENCES TO aimem_app;
ALTER DEFAULT PRIVILEGES FOR ROLE aimem_owner IN SCHEMA app GRANT EXECUTE ON FUNCTIONS TO aimem_app;
SQL
echo "aiplatform bootstrap complete"
