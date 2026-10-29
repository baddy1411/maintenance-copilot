"""An in-process stand-in for OpenRouter, built on httpx2.MockTransport.

Model listings mirror the real /models and /embeddings/models shapes (checked 2026-10-29), including
Haiku 5.5's >=100k-token price override. Chat, embedding and rerank responses are scripted per test.
"""

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx2

from app.llm.ledger import CallRecord, Spent

HAIKU = "anthropic/claude-haiku-5.5"
LUNA = "openai/gpt-6-luna"
VOYAGE = "voyageai/voyage-4-lite"

CHAT_MODELS = [
    {"id": HAIKU, "expiration_date": None,
     "supported_parameters": ["max_tokens", "reasoning", "response_format", "structured_outputs", "tools"],
     "pricing": {"prompt": "0.0000001", "completion": "0.0000005",
                 "overrides": [{"min_prompt_tokens": 100000, "prompt": "0.0000005", "completion": "0.0000025"}]}},
    {"id": LUNA, "expiration_date": None,
     "supported_parameters": ["max_tokens", "reasoning", "response_format", "tools"],
     "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}},
]  # fmt: skip
EMBED_MODELS = [{"id": VOYAGE, "supported_parameters": [], "pricing": {"prompt": "0.00000002", "completion": "0"}}]


def completion(content: str, finish_reason: str = "stop", cost: float = 0.0004, model: str = HAIKU) -> httpx2.Response:
    return httpx2.Response(200, json={
        "id": "gen-1", "object": "chat.completion", "created": 0, "model": model, "provider": "Anthropic",
        "choices": [{"index": 0, "finish_reason": finish_reason,
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150, "cost": cost,
                  "prompt_tokens_details": {"cached_tokens": 0}},
    })  # fmt: skip


def error(status: int, message: str, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"code": status, "message": message}}, headers=headers)


@dataclass
class FakeOpenRouter:
    chat: deque[httpx2.Response] = field(default_factory=deque)
    rerank_responses: deque[httpx2.Response] = field(default_factory=deque)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [b for p, b in self.requests if p == path]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/api/v1")
        if request.method == "GET":
            data = {"/models": CHAT_MODELS, "/embeddings/models": EMBED_MODELS}[path]
            return httpx2.Response(200, json={"data": data})
        body = json.loads(request.content)
        self.requests.append((path, body))
        if path == "/chat/completions":
            return self.chat.popleft()
        if path == "/embeddings":
            # Out of order on purpose: the gateway must sort by index.
            items = [{"object": "embedding", "index": i, "embedding": [float(i)] * 3}
                     for i in reversed(range(len(body["input"])))]  # fmt: skip
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "model": body["model"],
                    "data": items,
                    "usage": {"prompt_tokens": 10, "total_tokens": 10, "cost": 0.0000002},
                },
            )
        if path == "/rerank":
            return self.rerank_responses.popleft()
        raise AssertionError(f"unexpected {request.method} {path}")

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))


@dataclass
class MemoryLedger:
    records: list[CallRecord] = field(default_factory=list)
    already: Spent = field(default_factory=lambda: Spent(0.0, 0.0, 0.0))

    async def spent(self, run_id: UUID | None, user_hash: str | None) -> Spent:
        return self.already

    async def record(self, rec: CallRecord, config_version: str) -> None:
        self.records.append(rec)
