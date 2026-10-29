#!/bin/sh
# Runs bootstrap.sql with role passwords taken from the environment.
# Used as the Postgres image's init script (Compose) and by the integration tests.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v migrator_pw="$DB_PASSWORD_MIGRATOR" \
  -v app_rw_pw="$DB_PASSWORD_APP_RW" \
  -v tools_ro_pw="$DB_PASSWORD_TOOLS_RO" \
  -v ingest_rw_pw="$DB_PASSWORD_INGEST_RW" \
  -v mcp_rw_pw="$DB_PASSWORD_MCP_RW" \
  -f /bootstrap/bootstrap.sql
