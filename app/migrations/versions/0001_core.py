"""Core schema, tool views and grants (DESIGN §5.1, §5.2).

Revision ID: 0001
Revises:
Create Date: 2026-10-29
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = """
CREATE TYPE access_level AS ENUM ('public', 'restricted');

CREATE TABLE documents (
    doc_id            text PRIMARY KEY,
    source            text NOT NULL CHECK (source IN ('handbook', 'ac', 'ad', 'upload')),
    ingest_origin     text NOT NULL CHECK (ingest_origin IN ('official', 'admin_upload')),
    title             text NOT NULL,
    ad_number         text UNIQUE,
    publication_date  date,
    effective_date    date,
    supersedes        text[] NOT NULL DEFAULT '{}',
    superseded_by     text,
    make              text[] NOT NULL DEFAULT '{}',
    models            text[] NOT NULL DEFAULT '{}',
    url               text CHECK (url IS NULL OR url ~ '^https://'),
    sha256            text NOT NULL,
    access_level      access_level NOT NULL DEFAULT 'restricted',
    ingested_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE chunks (
    chunk_id  text PRIMARY KEY,
    doc_id    text NOT NULL REFERENCES documents ON DELETE CASCADE,
    page      int,
    ord       int NOT NULL,
    text      text NOT NULL,
    tsv       tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
);
CREATE INDEX chunks_doc_id ON chunks (doc_id);
CREATE INDEX chunks_tsv ON chunks USING gin (tsv);

-- One row per (chunk, model), so a re-embed runs side by side with the active model (DESIGN §8.1).
-- Partial HNSW indexes, one per model, are created when a model's backfill finishes.
CREATE TABLE chunk_embeddings (
    chunk_id     text NOT NULL REFERENCES chunks ON DELETE CASCADE,
    embed_model  text NOT NULL,
    embedding    halfvec(1024) NOT NULL,
    PRIMARY KEY (chunk_id, embed_model)
);

CREATE TABLE retrieval_config (
    id                  boolean PRIMARY KEY DEFAULT true CHECK (id),
    active_embed_model  text NOT NULL,
    corpus_version      int NOT NULL DEFAULT 0,
    updated_at          timestamptz NOT NULL DEFAULT now()
);
INSERT INTO retrieval_config (active_embed_model) VALUES ('voyageai/voyage-4-lite');

CREATE TABLE sdr (
    sdr_id           text PRIMARY KEY,
    difficulty_date  date,
    jasc             text,
    make_raw         text,
    model_raw        text,
    make             text,
    model            text,
    part_name        text,
    part_number      text,
    stage            text,
    how_discovered   text,
    discrepancy      text,
    tsv              tsvector GENERATED ALWAYS AS
                       (to_tsvector('english', coalesce(discrepancy, '') || ' ' || coalesce(part_name, ''))) STORED,
    row_hash         text NOT NULL,
    tombstoned_at    timestamptz,
    ingested_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sdr_model_jasc_date ON sdr (model, jasc, difficulty_date);
CREATE INDEX sdr_tsv ON sdr USING gin (tsv);

CREATE TABLE model_aliases (
    kind       text NOT NULL CHECK (kind IN ('make', 'model')),
    raw        text NOT NULL,
    canonical  text NOT NULL,
    PRIMARY KEY (kind, raw)
);

CREATE TABLE fleet (
    registration  text PRIMARY KEY CHECK (registration ~ '^MX-[0-9]{3}$'),
    type          text NOT NULL,
    engines       text NOT NULL,
    hours         numeric(9, 1) NOT NULL DEFAULT 0,
    cycles        int NOT NULL DEFAULT 0,
    in_service    boolean NOT NULL DEFAULT true
);

CREATE TYPE run_status AS ENUM
    ('queued', 'running', 'pending_approval', 'decided', 'written', 'rejected', 'failed', 'expired');

CREATE TABLE runs (
    run_id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_by            text NOT NULL,
    registration          text NOT NULL,
    symptom               text NOT NULL,
    client_key            text NOT NULL,
    request_sha256        text NOT NULL,
    status                run_status NOT NULL DEFAULT 'queued',
    graph_version         text NOT NULL,
    prompt_version        text NOT NULL,
    config_version        text NOT NULL,
    state_schema_version  int NOT NULL,
    draft_sha256          text,
    cost                  numeric(10, 6) NOT NULL DEFAULT 0,
    attempts              int NOT NULL DEFAULT 0,
    heartbeat_at          timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (created_by, client_key)
);
CREATE INDEX runs_status_heartbeat ON runs (status, heartbeat_at);

CREATE TABLE run_events (
    run_id   uuid NOT NULL REFERENCES runs ON DELETE CASCADE,
    seq      int NOT NULL,
    ts       timestamptz NOT NULL DEFAULT now(),
    kind     text NOT NULL,
    payload  jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE approvals (
    approval_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        uuid NOT NULL UNIQUE REFERENCES runs,
    approver_id   text NOT NULL,
    action        text NOT NULL CHECK (action IN ('approve', 'edit', 'reject')),
    draft_sha256  text NOT NULL,
    edited_draft  jsonb,
    reason        text,
    ts            timestamptz NOT NULL DEFAULT now(),
    CHECK (action <> 'edit' OR edited_draft IS NOT NULL),
    CHECK (action <> 'reject' OR reason IS NOT NULL)
);

CREATE TABLE work_orders (
    wo_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        uuid NOT NULL UNIQUE REFERENCES runs,
    approval_id   uuid NOT NULL UNIQUE REFERENCES approvals,
    registration  text NOT NULL REFERENCES fleet,
    title         text NOT NULL CHECK (length(title) <= 120),
    priority      text NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    status        text NOT NULL DEFAULT 'open',
    diagnosis     jsonb NOT NULL,
    steps         jsonb NOT NULL,
    citations     jsonb NOT NULL,
    created_by    text NOT NULL,
    approved_by   text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (created_by <> approved_by)  -- no self-approval, also enforced by the decision endpoint
);

CREATE TABLE audit_log (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                timestamptz NOT NULL DEFAULT now(),
    actor             text NOT NULL,
    action            text NOT NULL,
    run_id            uuid,
    payload_redacted  jsonb NOT NULL DEFAULT '{}',
    prev_hash         text,
    row_hash          text NOT NULL
);

CREATE TABLE llm_calls (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                 timestamptz NOT NULL DEFAULT now(),
    run_id             uuid,
    user_hash          text,
    kind               text NOT NULL,
    model              text NOT NULL,
    provider           text,
    prompt_tokens      int,
    completion_tokens  int,
    cached_tokens      int,
    cost               numeric(12, 8),
    latency_ms         int,
    status             text NOT NULL,
    prompt_version     text,
    config_version     text
);
CREATE INDEX llm_calls_ts ON llm_calls (ts);
CREATE INDEX llm_calls_user_ts ON llm_calls (user_hash, ts);

CREATE TABLE limits (
    subject       text NOT NULL,
    kind          text NOT NULL,
    window_start  timestamptz NOT NULL,
    counter       numeric(14, 6) NOT NULL DEFAULT 0,
    PRIMARY KEY (subject, kind, window_start)
);

CREATE TABLE ingest_runs (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source       text NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    fetched      int NOT NULL DEFAULT 0,
    inserted     int NOT NULL DEFAULT 0,
    updated      int NOT NULL DEFAULT 0,
    quarantined  int NOT NULL DEFAULT 0,
    cost         numeric(10, 6) NOT NULL DEFAULT 0,
    error        text
);

CREATE TABLE sync_state (
    source           text PRIMARY KEY,
    high_water_mark  date NOT NULL,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE quarantine (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source       text NOT NULL,
    url          text NOT NULL,
    reason       text NOT NULL,
    body_sha256  text NOT NULL,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),
    attempts     int NOT NULL DEFAULT 1,
    resolved_at  timestamptz,
    UNIQUE (url, body_sha256)
);
"""

# The only objects `tools_ro` can read. Views run with the owner's rights, so tools never touch base tables.
TOOL_VIEWS = """
CREATE VIEW tools_chunks AS
    SELECT c.chunk_id, c.doc_id, c.page, c.ord, c.text, c.tsv,
           d.source, d.title, d.ad_number, d.publication_date, d.effective_date,
           d.superseded_by, d.make, d.models, d.url, d.access_level
    FROM chunks c JOIN documents d USING (doc_id);
CREATE VIEW tools_chunk_embeddings AS SELECT chunk_id, embed_model, embedding FROM chunk_embeddings;
CREATE VIEW tools_retrieval_config AS SELECT active_embed_model, corpus_version FROM retrieval_config;
CREATE VIEW tools_sdr AS
    SELECT sdr_id, difficulty_date, jasc, make, model, part_name, part_number, stage, how_discovered,
           discrepancy, tsv
    FROM sdr WHERE tombstoned_at IS NULL;
CREATE VIEW tools_model_aliases AS SELECT kind, raw, canonical FROM model_aliases;
CREATE VIEW tools_fleet AS SELECT registration, type, engines, hours, cycles, in_service FROM fleet;
"""

GRANTS = """
GRANT SELECT ON tools_chunks, tools_chunk_embeddings, tools_retrieval_config, tools_sdr,
                tools_model_aliases, tools_fleet TO tools_ro, mcp_rw;

-- API and agent runs (the worker also connects as app_rw for agent jobs).
GRANT SELECT ON documents, chunks, chunk_embeddings, retrieval_config, sdr, model_aliases, fleet TO app_rw;
GRANT SELECT, INSERT, UPDATE ON runs TO app_rw;
GRANT SELECT, INSERT, DELETE ON run_events TO app_rw;
GRANT SELECT, INSERT ON approvals, work_orders TO app_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON limits TO app_rw;
GRANT SELECT, INSERT ON llm_calls TO app_rw;
GRANT SELECT, INSERT ON audit_log TO app_rw;

-- Ingestion.
GRANT SELECT, INSERT, UPDATE, DELETE ON documents, chunks, chunk_embeddings, sdr, model_aliases TO ingest_rw;
GRANT SELECT, UPDATE ON retrieval_config TO ingest_rw;
GRANT SELECT, INSERT, UPDATE ON ingest_runs, sync_state, quarantine TO ingest_rw;
GRANT SELECT, INSERT ON llm_calls TO ingest_rw;
GRANT INSERT ON audit_log TO ingest_rw;

-- audit_log is append-only for every service role: no UPDATE or DELETE anywhere.
-- mcp_rw's draft rights are added with the MCP server in Phase 6 (DESIGN §6.4).
"""


def upgrade() -> None:
    op.execute(TABLES)
    op.execute(TOOL_VIEWS)
    op.execute(GRANTS)


def downgrade() -> None:
    # Expand/contract only: roll back by redeploying the previous image (DESIGN §5.2).
    raise NotImplementedError("down-migrations are not supported")
