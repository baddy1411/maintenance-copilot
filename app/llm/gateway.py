"""The single LLM gateway: every chat, embedding and rerank call goes through here (DESIGN §7.1).

Per call, in order:
1. redact the outbound text
2. look up the model's supported parameters and price (catalog, 1 h TTL)
3. estimate the cost and check the run, user and global caps against the ledger
4. call with provider preferences, with SDK retries on 429/5xx (honouring Retry-After)
5. on a model failure, try the fallback model once
6. record the real `usage.cost` in the ledger, whatever the outcome

Streaming answers are added by the Q&A slice (#9). This module is request/response only.
"""

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypeVar, cast

import httpx2
import openai
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.llm.catalog import ModelCatalog, estimate_tokens
from app.llm.errors import CreditsExhausted, ModelUnavailable, StructuredOutputError, UnsupportedModel
from app.llm.ledger import CallContext, CallRecord, Caps, Ledger, check_caps, user_hash
from app.llm.redact import redact
from app.llm.schema import strict_schema

T = TypeVar("T", bound=BaseModel)
Message = dict[str, Any]

# Errors where another model may succeed. 402 (credits) and 400 (our request) are not among them.
FALLBACK_ON = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
    openai.RateLimitError,
    openai.NotFoundError,  # "No endpoints found that support ..." under require_parameters
)
STRUCTURED_RETRIES = 2
MAX_TOKENS_CEILING = 8000


@dataclass(frozen=True)
class ChatResult:
    text: str
    finish_reason: str | None
    model: str
    provider: str | None
    cost: float


def _redact_messages(messages: Sequence[Message]) -> list[Message]:
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            m = {**m, "content": redact(content)}
        elif isinstance(content, list):
            m = {**m, "content": [{**p, "text": redact(p["text"])} if "text" in p else p for p in content]}
        out.append(m)
    return out


def _chars(messages: Sequence[Message]) -> int:
    return len(json.dumps(messages))


def _status(exc: Exception) -> str:
    code = getattr(exc, "status_code", None)
    return f"error:{code}" if code else f"error:{type(exc).__name__}"


class Gateway:
    def __init__(self, settings: Settings, http: httpx2.AsyncClient, ledger: Ledger) -> None:
        self.s = settings
        self.http = http
        self.ledger = ledger
        self.catalog = ModelCatalog(http, settings.llm_base_url)
        self.client = openai.AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.openrouter_api_key.get_secret_value(),
            http_client=http,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )
        self.caps = Caps(settings.run_cost_cap_usd, settings.user_daily_cap_usd, settings.global_daily_cap_usd)
        self.config_version = settings.config_version()

    def _provider(self) -> dict[str, Any]:
        # data_collection=deny keeps out providers that store or train on prompts (DATA.md).
        # require_parameters: an endpoint that would silently ignore a parameter is never chosen.
        prefs: dict[str, Any] = {"data_collection": "deny", "require_parameters": True}
        if self.s.provider_order:
            prefs |= {"order": self.s.provider_order, "allow_fallbacks": False}
        return prefs

    def _user_hash(self, ctx: CallContext) -> str | None:
        # Kept in our ledger only. Nothing that identifies the user is sent upstream (DESIGN P2-5).
        return user_hash(self.s.user_hash_salt.get_secret_value(), ctx.user_id)

    async def _check_spend(self, ctx: CallContext, estimate: float) -> None:
        spent = await self.ledger.spent(ctx.run_id, self._user_hash(ctx))
        check_caps(spent, estimate, self.caps, has_run=ctx.run_id is not None, has_user=ctx.user_id is not None)

    async def _record(self, ctx: CallContext, model: str, started: float, status: str, **usage: Any) -> None:
        rec = CallRecord(ctx=ctx, user_hash=self._user_hash(ctx), model=model, status=status,
                         latency_ms=round((time.perf_counter() - started) * 1000), **usage)  # fmt: skip
        await self.ledger.record(rec, self.config_version)

    # ---- chat -------------------------------------------------------------------------------------

    async def _chat_once(
        self, ctx: CallContext, model: str, messages: list[Message], max_tokens: int, params: dict[str, Any]
    ) -> ChatResult:
        info = await self.catalog.get(model)
        if "response_format" in params and "response_format" not in info.supported:
            raise UnsupportedModel(f"{model} does not support response_format")
        prompt_tokens = estimate_tokens(_chars(messages))
        tier = info.price(prompt_tokens)
        await self._check_spend(ctx, prompt_tokens * tier.prompt + max_tokens * tier.completion)

        sent = {k: v for k, v in params.items() if k in info.supported}
        body_params = {k: sent.pop(k) for k in ("reasoning",) if k in sent}  # not an SDK keyword
        started = time.perf_counter()
        try:
            r = await self.client.chat.completions.create(
                model=model,
                messages=cast(Any, messages),
                max_tokens=max_tokens,
                extra_body={"provider": self._provider(), **body_params},
                **sent,
            )
        except openai.APIError as exc:
            await self._record(ctx, model, started, _status(exc))
            raise
        u = r.usage.model_dump() if r.usage else {}
        cost = float(u.get("cost") or 0.0)
        provider = (r.model_extra or {}).get("provider")
        await self._record(
            ctx, model, started, "ok", cost=cost, provider=provider,
            prompt_tokens=u.get("prompt_tokens"), completion_tokens=u.get("completion_tokens"),
            cached_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens"),
        )  # fmt: skip
        choice = r.choices[0]
        return ChatResult(choice.message.content or "", choice.finish_reason, model, provider, cost)

    async def chat(
        self, ctx: CallContext, messages: Sequence[Message], *, max_tokens: int, model: str | None = None,
        **params: Any,
    ) -> ChatResult:  # fmt: skip
        """One completion. `params` (temperature, reasoning, response_format, ...) are sent only where the
        model supports them. `max_tokens` is required, because without it a low balance returns 402 (SPIKE §1)."""
        safe = _redact_messages(messages)
        primary = model or self.s.chat_model
        try:
            return await self._chat_once(ctx, primary, safe, max_tokens, params)
        except openai.APIStatusError as exc:
            if exc.status_code == 402:
                raise CreditsExhausted(str(exc)) from exc
            if not isinstance(exc, FALLBACK_ON):
                raise
            failure: Exception = exc
        except FALLBACK_ON as exc:
            failure = exc
        fallback = self.s.fallback_model
        if not fallback or fallback == primary:
            raise ModelUnavailable(f"{primary} failed: {failure}") from failure
        try:
            return await self._chat_once(ctx, fallback, safe, max_tokens, params)
        except openai.APIStatusError as exc:
            if exc.status_code == 402:
                raise CreditsExhausted(str(exc)) from exc
            raise ModelUnavailable(f"{primary} and fallback {fallback} failed: {exc}") from exc
        except FALLBACK_ON as exc:
            raise ModelUnavailable(f"{primary} and fallback {fallback} failed: {exc}") from exc

    async def structured(
        self, ctx: CallContext, messages: Sequence[Message], schema: type[T], *, max_tokens: int = 2000,
        model: str | None = None, reasoning_effort: str = "low",
    ) -> T:  # fmt: skip
        """JSON output validated against `schema`. A validation error is fed back to the model, at most
        STRUCTURED_RETRIES times. A reply cut off at max_tokens (reasoning tokens count against it,
        SPIKE §1) is retried with double the budget."""
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "strict": True, "schema": strict_schema(schema)},
        }
        convo = list(messages)
        for attempt in range(STRUCTURED_RETRIES + 1):
            r = await self.chat(ctx, convo, max_tokens=max_tokens, model=model, response_format=response_format,
                                reasoning={"effort": reasoning_effort})  # fmt: skip
            try:
                return schema.model_validate_json(r.text)
            except ValidationError as exc:
                if attempt == STRUCTURED_RETRIES:
                    raise StructuredOutputError(f"{schema.__name__}: {exc.error_count()} errors") from exc
                if r.finish_reason == "length":
                    max_tokens = min(max_tokens * 2, MAX_TOKENS_CEILING)
                    continue
                convo += [
                    {"role": "assistant", "content": r.text},
                    {"role": "user", "content": f"That JSON failed validation. Fix it and reply with JSON "
                                                f"only.\n{exc.errors(include_url=False)}"},
                ]  # fmt: skip
        raise AssertionError("unreachable")

    # ---- embeddings and rerank --------------------------------------------------------------------

    async def embed(self, ctx: CallContext, texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
        model = model or self.s.embed_model
        info = await self.catalog.get(model)
        safe = [redact(t) for t in texts]
        await self._check_spend(ctx, estimate_tokens(sum(map(len, safe))) * info.price(0).prompt)
        started = time.perf_counter()
        try:
            r = await self.client.embeddings.create(model=model, input=safe, extra_body={"provider": self._provider()})
        except openai.APIStatusError as exc:
            await self._record(ctx, model, started, _status(exc))
            if exc.status_code == 402:
                raise CreditsExhausted(str(exc)) from exc
            raise
        u = r.usage.model_dump() if r.usage else {}
        await self._record(ctx, model, started, "ok", cost=float(u.get("cost") or 0.0),
                           provider=(r.model_extra or {}).get("provider"),
                           prompt_tokens=u.get("prompt_tokens"))  # fmt: skip
        return [d.embedding for d in sorted(r.data, key=lambda d: d.index)]

    async def rerank(
        self, ctx: CallContext, query: str, documents: Sequence[str], *, top_n: int | None = None,
        timeout_s: float = 1.5,
    ) -> list[tuple[int, float]]:  # fmt: skip
        """(index into `documents`, relevance score), best first. The SDK has no rerank, so this POSTs to
        /rerank. No retries: the Q&A path falls back to RRF order on any failure (DESIGN §6.1)."""
        model = self.s.rerank_model
        await self._check_spend(ctx, self.s.rerank_price_per_search)
        body = {"model": model, "query": redact(query), "documents": [redact(d) for d in documents],
                "top_n": top_n or len(documents), "provider": self._provider()}  # fmt: skip
        started = time.perf_counter()
        try:
            resp = await self.http.post(
                f"{self.s.llm_base_url}/rerank",
                json=body,
                timeout=timeout_s,
                headers={"Authorization": f"Bearer {self.s.openrouter_api_key.get_secret_value()}"},
            )
            resp.raise_for_status()
        except httpx2.HTTPError as exc:
            code = exc.response.status_code if isinstance(exc, httpx2.HTTPStatusError) else None
            await self._record(ctx, model, started, f"error:{code or type(exc).__name__}")
            if code == 402:
                raise CreditsExhausted("402 from /rerank") from exc
            raise
        data = resp.json()
        await self._record(ctx, model, started, "ok", cost=float((data.get("usage") or {}).get("cost") or 0.0),
                           provider=data.get("provider"))  # fmt: skip
        return [(int(x["index"]), float(x["relevance_score"])) for x in data["results"]]
