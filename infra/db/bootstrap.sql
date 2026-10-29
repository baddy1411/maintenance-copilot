-- One-time cluster bootstrap, run as the superuser (Compose init script, testcontainers, or Terraform on RDS).
-- Creates the extension and the login roles. All tables, views and grants come from `copilot migrate` (DESIGN §5.2).
-- Passwords arrive as psql variables, so nothing secret is stored in this file.
\set ON_ERROR_STOP on

CREATE EXTENSION IF NOT EXISTS vector;

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', r.name, r.pw)
FROM (VALUES ('migrator', :'migrator_pw'),
             ('app_rw', :'app_rw_pw'),
             ('tools_ro', :'tools_ro_pw'),
             ('ingest_rw', :'ingest_rw_pw'),
             ('mcp_rw', :'mcp_rw_pw')) AS r(name, pw)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r.name) \gexec

SELECT format('ALTER DATABASE %I OWNER TO migrator', current_database()) \gexec
ALTER SCHEMA public OWNER TO migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO app_rw, tools_ro, ingest_rw, mcp_rw;

-- Agent and MCP tool queries: read-only, bounded, and with the vector-search settings from DESIGN §8.
ALTER ROLE tools_ro SET default_transaction_read_only = on;
ALTER ROLE tools_ro SET statement_timeout = '5s';
ALTER ROLE tools_ro SET hnsw.ef_search = 100;
ALTER ROLE tools_ro SET hnsw.iterative_scan = 'relaxed_order';
ALTER ROLE mcp_rw SET statement_timeout = '5s';
