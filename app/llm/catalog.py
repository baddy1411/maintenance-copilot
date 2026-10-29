"""What each model accepts and costs, read from the endpoint's model listings and cached with a TTL.

OpenRouter lists chat models at /models and embedding models at /embeddings/models (checked 2026-10-29).
Rerank models are in neither list and report $0 per token, because they're billed per search. Their
price comes from config (`rerank_price_per_search`).
"""

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

import httpx2

from app.llm.errors import UnsupportedModel

logger = logging.getLogger(__name__)
TTL_S = 3600.0
# Conservative token estimate for the pre-call spend check. SPIKE §2 found words x 1.35 undercounts by
# about 25 % on this corpus; chars / 3 overestimates English prose instead, which is the safe side for a cap.
CHARS_PER_TOKEN = 3


@dataclass(frozen=True)
class PriceTier:
    min_prompt_tokens: int
    prompt: float  # USD per token
    completion: float


@dataclass(frozen=True)
class ModelInfo:
    id: str
    supported: frozenset[str]
    tiers: tuple[PriceTier, ...]  # ascending min_prompt_tokens; tier 0 starts at 0
    expiration_date: str | None = None

    def price(self, prompt_tokens: int) -> PriceTier:
        # Haiku 5.5 is 5x dearer per token once a prompt reaches 100k tokens: the listing's `overrides`.
        return [t for t in self.tiers if prompt_tokens >= t.min_prompt_tokens][-1]


def estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def _parse(m: dict[str, Any]) -> ModelInfo:
    """`pricing.overrides` come in two shapes (seen live, 2026-10-29): by prompt size (`min_prompt_tokens`)
    and by time (`utc_days`, `utc_start`/`utc_end`, e.g. DeepSeek's weekday peak). For a spend cap the safe
    reading of a time-based price is the highest one, so those are folded into the base tier by max()."""
    p = m.get("pricing") or {}
    prompt, completion = float(p.get("prompt") or 0), float(p.get("completion") or 0)
    by_size = [o for o in p.get("overrides") or [] if "min_prompt_tokens" in o]
    for o in p.get("overrides") or []:
        if "min_prompt_tokens" not in o:
            prompt = max(prompt, float(o.get("prompt") or 0))
            completion = max(completion, float(o.get("completion") or 0))
    tiers = [PriceTier(0, prompt, completion)]
    for o in by_size:
        tiers.append(
            PriceTier(
                int(o["min_prompt_tokens"]), float(o.get("prompt", prompt)), float(o.get("completion", completion))
            )
        )
    return ModelInfo(
        id=m["id"],
        supported=frozenset(m.get("supported_parameters") or []),
        tiers=tuple(sorted(tiers, key=lambda t: t.min_prompt_tokens)),
        expiration_date=m.get("expiration_date"),
    )


@dataclass
class ModelCatalog:
    http: httpx2.AsyncClient
    base_url: str
    ttl_s: float = TTL_S
    _models: dict[str, ModelInfo] = field(default_factory=dict)
    _loaded_at: float = float("-inf")

    async def refresh(self) -> None:
        models: dict[str, ModelInfo] = {}
        for path in ("/models", "/embeddings/models"):
            r = await self.http.get(f"{self.base_url}{path}")
            r.raise_for_status()
            for m in r.json()["data"]:
                try:
                    models[m["id"]] = _parse(m)
                except (KeyError, TypeError, ValueError):
                    # One odd listing must not take down the catalog. The model is simply unknown, and
                    # unknown models fail closed in get(). The daily canary reports it.
                    logger.warning("unparseable model listing skipped: %s", m.get("id"))
        self._models, self._loaded_at = models, time.monotonic()

    async def get(self, model: str) -> ModelInfo:
        if time.monotonic() - self._loaded_at > self.ttl_s:
            await self.refresh()
        try:
            return self._models[model]
        except KeyError:
            # Fail closed: without a price the spend check can't run.
            raise UnsupportedModel(f"{model} is not in the endpoint's model listings") from None
