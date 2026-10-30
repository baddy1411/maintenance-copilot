"""One partial HNSW index per embedding model (DESIGN §5.1, §8.1).

Partial, so two models can coexist during an embedder change without their vectors mixing in one graph.
The query must then name the model as a *literal*: the planner only uses a partial index if it can prove
the query's WHERE implies the index predicate, and a bind parameter can't be proven once Postgres switches
to a generic plan. Model names are validated against MODEL_NAME before being inlined.
"""

import re

MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")


def check_model_name(model: str) -> str:
    if not MODEL_NAME.fullmatch(model):
        raise ValueError(f"unexpected embedding model name: {model!r}")
    return model


def index_name(model: str) -> str:
    return "chunk_embeddings_hnsw_" + re.sub(r"[^a-z0-9]+", "_", check_model_name(model)).strip("_")


def create_index_sql(model: str) -> str:
    """HNSW over halfvec with cosine distance; pgvector defaults m=16, ef_construction=64.
    CONCURRENTLY, so ingestion and queries keep running during a rebuild (it can't run in a transaction)."""
    m = check_model_name(model)
    return (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name(m)} ON chunk_embeddings "
        f"USING hnsw (embedding halfvec_cosine_ops) WHERE embed_model = '{m}'"
    )
