"""Pieces every ingestion source shares: the run report, the job spend cap, and embedding in batches."""

from dataclasses import dataclass, field

from app.ingest.chunk import Chunk
from app.llm import CallContext, Gateway

EMBED_BATCH = 64


@dataclass
class SyncReport:
    run_id: int = 0
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    quarantined: int = 0
    failed: int = 0
    cost: float = 0.0
    stopped_early: str | None = None
    errors: list[str] = field(default_factory=list)


class JobCapReached(RuntimeError):
    pass


async def embed_chunks(gateway: Gateway, chunks: list[Chunk], report: SyncReport, cap: float) -> list[list[float]]:
    """Embed in batches of 64, checking the job's dollar cap before each batch (DESIGN §6.2)."""
    vectors: list[list[float]] = []
    for i in range(0, len(chunks), EMBED_BATCH):
        if report.cost >= cap:
            raise JobCapReached(f"job cap ${cap:.2f} reached (${report.cost:.4f})")
        r = await gateway.embed(CallContext(kind="embed_corpus"), [c.text for c in chunks[i : i + EMBED_BATCH]])
        report.cost += r.cost
        vectors.extend(r.vectors)
    return vectors
