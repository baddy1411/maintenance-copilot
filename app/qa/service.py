"""The cited-Q&A flow (DESIGN §6.1) as a stream of events, independent of HTTP.

Events, in order (`data` is JSON):
  status     {"stage": "searching"}                              sent before any work: the 1 s first-event target
  status     {"stage": "answering", "passages": n, "degraded": [...]}
  token      {"text": "..."}                                     many
  citations  {"items": [Citation...], "dropped": [...]}
  done       {"verdict": ..., "message"?, "unsupported": u, "sentences": s, "degraded": [...], "timings_ms": {...}}

Verdicts:
  answered                the answer streamed; >= 70 % of its sentences cite a verified quote
  not_found               no passage scored above the relevance threshold, or the model said NOT_FOUND
  refused_unsupported     too few verified citations: the UI must replace the streamed text with `message`
  generation_unavailable  the model (and fallback) failed or a spend limit was hit; the passages are sent as
                          citations so the user still gets the sources (DESIGN §6.1 graceful degradation)
"""

import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import asdict, dataclass
from typing import Any

from app.config import Settings
from app.llm import CallContext, CreditsExhausted, Gateway, LLMError, ModelUnavailable, SpendLimitExceeded, StreamEnd
from app.qa.answer import (
    PROMPT_VERSION,
    REFUSAL,
    Citation,
    SourcesSplitter,
    build_messages,
    validate_citations,
    verdict,
)
from app.retrieval.search import AccessLevel, Hit, Retriever

UNAVAILABLE = "The answer service is unavailable right now. These are the most relevant passages."


@dataclass(frozen=True)
class Event:
    event: str
    data: dict[str, Any]


def _passage_citation(n: int, h: Hit) -> Citation:
    return Citation(n, h.chunk_id, h.doc_id, h.title, h.source, h.page, h.page_label, h.ad_number, h.superseded_by,
                    " ".join(h.text.split()[:40]))  # fmt: skip


async def answer_events(
    question: str, user_id: str, levels: tuple[AccessLevel, ...], settings: Settings, retriever: Retriever,
    gateway: Gateway,
) -> AsyncGenerator[Event]:  # fmt: skip
    t0 = time.perf_counter()
    yield Event("status", {"stage": "searching"})

    timings: dict[str, float] = {}
    result = await retriever.search(question, CallContext(kind="retrieval", user_id=user_id), levels=levels)
    timings["retrieval"] = round((time.perf_counter() - t0) * 1000, 1)
    hits, degraded = result.hits, list(result.degraded)

    top = hits[0].rerank_score if hits else None
    if not hits or (top is not None and top < settings.relevance_threshold):
        timings["total"] = round((time.perf_counter() - t0) * 1000, 1)
        yield Event("done", {"verdict": "not_found", "message": REFUSAL, "unsupported": 0, "sentences": 0,
                             "degraded": degraded, "top_score": top, "timings_ms": timings})  # fmt: skip
        return

    yield Event("status", {"stage": "answering", "passages": len(hits), "degraded": degraded})
    ctx = CallContext(kind="answer", user_id=user_id, prompt_version=PROMPT_VERSION)
    splitter = SourcesSplitter()
    end: StreamEnd | None = None
    t_gen = time.perf_counter()
    try:
        # aclosing: if the client disconnects, this request is cancelled. Leaving `async for` on that path does
        # NOT close an async generator, so without it the upstream stream close and the ledger row in
        # stream_chat's finally would wait for garbage collection.
        stream = gateway.stream_chat(ctx, build_messages(question, hits), max_tokens=settings.answer_max_tokens,
                                     reasoning={"effort": settings.answer_reasoning_effort})  # fmt: skip
        async with aclosing(stream):
            async for item in stream:
                if isinstance(item, StreamEnd):
                    end = item
                    continue
                shown = splitter.feed(item)
                if shown:
                    if "first_token" not in timings:
                        timings["first_token"] = round((time.perf_counter() - t0) * 1000, 1)
                    yield Event("token", {"text": shown})
    except (ModelUnavailable, CreditsExhausted, SpendLimitExceeded, LLMError) as exc:
        cites = [_passage_citation(i, h) for i, h in enumerate(hits, start=1)]
        yield Event("citations", {"items": [asdict(c) for c in cites], "dropped": []})
        timings["total"] = round((time.perf_counter() - t0) * 1000, 1)
        yield Event("done", {
            "verdict": "generation_unavailable", "message": UNAVAILABLE, "reason": type(exc).__name__,
            "unsupported": 0, "sentences": 0, "degraded": degraded, "timings_ms": timings,
        })  # fmt: skip
        return

    tail = splitter.finish()
    if tail:
        if "first_token" not in timings:
            timings["first_token"] = round((time.perf_counter() - t0) * 1000, 1)
        yield Event("token", {"text": tail})
    timings["generation"] = round((time.perf_counter() - t_gen) * 1000, 1)

    cites, dropped = validate_citations(splitter.sources, hits)
    v, unsupported, total = verdict(splitter.answer, cites)
    yield Event("citations", {"items": [asdict(c) for c in cites] if v == "answered" else [], "dropped": dropped})
    timings["total"] = round((time.perf_counter() - t0) * 1000, 1)
    done: dict[str, Any] = {"verdict": v, "unsupported": unsupported, "sentences": total, "degraded": degraded,
                            "model": end.model if end else None, "finish_reason": end.finish_reason if end else None,
                            "cost": round(end.cost, 6) if end else None, "timings_ms": timings}  # fmt: skip
    if v != "answered":
        done["message"] = REFUSAL
    yield Event("done", done)
