"""Gateway behaviour against recorded responses, including every provider failure SPIKE §1 hit."""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx2
import pytest
from pydantic import BaseModel, Field

from app.config import Settings
from app.llm import (
    CallContext,
    CreditsExhausted,
    Gateway,
    ModelUnavailable,
    SpendLimitExceeded,
    StructuredOutputError,
    UnsupportedModel,
)
from app.llm.catalog import ModelCatalog
from app.llm.ledger import Spent
from tests.fake_openrouter import HAIKU, LUNA, FakeOpenRouter, MemoryLedger, completion, error

PASSWORDS = {f"db_password_{r}": "x" for r in ("migrator", "app_rw", "tools_ro", "ingest_rw", "mcp_rw")}
CTX = CallContext(kind="answer", user_id="tech1", run_id=uuid4())
ASK = [{"role": "user", "content": "Hydraulic leak on MX-007, call me at 555-123-4567 or pat@example.com"}]


def settings(**over: object) -> Settings:
    return Settings(**{**PASSWORDS, "openrouter_api_key": "sk-test", "user_hash_salt": "salt", **over})  # type: ignore[arg-type]


class Draft(BaseModel):
    title: str = Field(max_length=40)
    priority: str


@pytest.fixture
def fake() -> FakeOpenRouter:
    return FakeOpenRouter()


@pytest.fixture
def ledger() -> MemoryLedger:
    return MemoryLedger()


@pytest.fixture
async def gw(fake: FakeOpenRouter, ledger: MemoryLedger) -> AsyncIterator[Gateway]:
    async with fake.client() as http:
        yield Gateway(settings(), http, ledger)


async def test_chat_sends_only_supported_params_with_provider_prefs(gw: Gateway, fake: FakeOpenRouter,
                                                                    ledger: MemoryLedger) -> None:  # fmt: skip
    fake.chat.append(completion("Check the return line fitting."))
    r = await gw.chat(CTX, ASK, max_tokens=300, temperature=0, reasoning={"effort": "low"})
    body = fake.bodies("/chat/completions")[0]
    assert "temperature" not in body  # Haiku 5.5 doesn't list it; under require_parameters it would 404
    assert body["reasoning"] == {"effort": "low"}
    assert body["max_tokens"] == 300
    assert body["provider"] == {"data_collection": "deny", "require_parameters": True}
    assert "user" not in body
    assert r.text == "Check the return line fitting." and r.provider == "Anthropic"
    [rec] = ledger.records
    assert (rec.status, rec.cost, rec.prompt_tokens, rec.model) == ("ok", 0.0004, 120, HAIKU)
    assert rec.user_hash and rec.user_hash != "tech1"


async def test_outbound_text_is_redacted(gw: Gateway, fake: FakeOpenRouter) -> None:
    fake.chat.append(completion("ok"))
    await gw.chat(CTX, ASK, max_tokens=100)
    sent = fake.bodies("/chat/completions")[0]["messages"][0]["content"]
    assert sent == "Hydraulic leak on MX-007, call me at [PHONE] or [EMAIL]"


async def test_402_raises_credits_exhausted_without_trying_the_fallback(gw: Gateway, fake: FakeOpenRouter,
                                                                         ledger: MemoryLedger) -> None:  # fmt: skip
    fake.chat.append(error(402, "This request requires more credits, or fewer max_tokens"))
    with pytest.raises(CreditsExhausted):
        await gw.chat(CTX, ASK, max_tokens=100)
    assert len(fake.bodies("/chat/completions")) == 1
    assert [r.status for r in ledger.records] == ["error:402"]


async def test_404_no_endpoints_falls_back_to_the_other_model(gw: Gateway, fake: FakeOpenRouter,
                                                               ledger: MemoryLedger) -> None:  # fmt: skip
    fake.chat.extend([error(404, "No endpoints found that support the provided 'temperature' parameter"),
                      completion("from luna", model=LUNA)])  # fmt: skip
    r = await gw.chat(CTX, ASK, max_tokens=100)
    assert r.model == LUNA and r.text == "from luna"
    assert [b["model"] for b in fake.bodies("/chat/completions")] == [HAIKU, LUNA]
    assert [rec.status for rec in ledger.records] == ["error:404", "ok"]


async def test_429_is_retried_after_retry_after(gw: Gateway, fake: FakeOpenRouter, ledger: MemoryLedger) -> None:
    fake.chat.extend([error(429, "engine_overloaded", {"retry-after-ms": "10"}), completion("ok")])
    r = await gw.chat(CTX, ASK, max_tokens=100)
    assert r.text == "ok" and r.model == HAIKU
    assert len(fake.bodies("/chat/completions")) == 2
    assert [rec.status for rec in ledger.records] == ["ok"]


async def test_both_models_failing_raises_model_unavailable(fake: FakeOpenRouter, ledger: MemoryLedger) -> None:
    async with fake.client() as http:
        gw = Gateway(settings(llm_max_retries=0), http, ledger)
        fake.chat.extend([error(503, "Provider returned error"), error(503, "Provider returned error")])
        with pytest.raises(ModelUnavailable):
            await gw.chat(CTX, ASK, max_tokens=100)


async def test_structured_retries_truncated_json_with_a_larger_budget(gw: Gateway, fake: FakeOpenRouter) -> None:
    # SPIKE §1: DeepSeek spent 1,371 of 1,500 tokens reasoning and returned cut-off JSON.
    fake.chat.extend([completion('{"title": "Replace hyd', finish_reason="length"),
                      completion('{"title": "Replace hydraulic line", "priority": "high"}')])  # fmt: skip
    d = await gw.structured(CTX, ASK, Draft, max_tokens=1500)
    assert d == Draft(title="Replace hydraulic line", priority="high")
    first, second = fake.bodies("/chat/completions")
    assert (first["max_tokens"], second["max_tokens"]) == (1500, 3000)
    assert first["response_format"]["json_schema"]["schema"]["additionalProperties"] is False


async def test_structured_feeds_validation_errors_back(gw: Gateway, fake: FakeOpenRouter) -> None:
    fake.chat.extend([completion('{"title": "' + "x" * 60 + '", "priority": "high"}'),
                      completion('{"title": "Short title", "priority": "high"}')])  # fmt: skip
    d = await gw.structured(CTX, ASK, Draft)
    assert d.title == "Short title"
    retry_messages = fake.bodies("/chat/completions")[1]["messages"]
    assert retry_messages[-2]["role"] == "assistant"
    assert "failed validation" in retry_messages[-1]["content"]


async def test_structured_gives_up_after_the_retry_budget(gw: Gateway, fake: FakeOpenRouter) -> None:
    fake.chat.extend([completion("not json")] * 3)
    with pytest.raises(StructuredOutputError):
        await gw.structured(CTX, ASK, Draft)
    assert len(fake.bodies("/chat/completions")) == 3


async def test_spend_cap_blocks_the_call_before_it_is_sent(fake: FakeOpenRouter) -> None:
    ledger = MemoryLedger(already=Spent(run=0.2499, user_day=0.3, global_day=1.0))
    async with fake.client() as http:
        gw = Gateway(settings(), http, ledger)
        with pytest.raises(SpendLimitExceeded, match="run spend cap"):
            await gw.chat(CTX, ASK, max_tokens=2000)
    assert fake.bodies("/chat/completions") == []


async def test_global_cap_applies_to_calls_without_a_user(fake: FakeOpenRouter) -> None:
    ledger = MemoryLedger(already=Spent(run=0.0, user_day=0.0, global_day=5.0))
    async with fake.client() as http:
        gw = Gateway(settings(), http, ledger)
        with pytest.raises(SpendLimitExceeded, match="global daily"):
            await gw.embed(CallContext(kind="embed_corpus"), ["a chunk"])


async def test_unknown_model_fails_closed(gw: Gateway, fake: FakeOpenRouter) -> None:
    with pytest.raises(UnsupportedModel):
        await gw.chat(CTX, ASK, max_tokens=100, model="someone/unlisted-model")
    assert fake.bodies("/chat/completions") == []


async def test_embed_returns_vectors_in_input_order(gw: Gateway, fake: FakeOpenRouter, ledger: MemoryLedger) -> None:
    result = await gw.embed(CallContext(kind="embed_query"), ["first", "second", "email pat@example.com"])
    assert result.vectors == [[1.0] * 3, [2.0] * 3, [3.0] * 3]
    assert result.cost == 0.0000002
    body = fake.bodies("/embeddings")[0]
    assert body["input"][2] == "email [EMAIL]"
    assert body["provider"]["data_collection"] == "deny"
    assert ledger.records[0].cost == 0.0000002


async def test_rerank_posts_provider_prefs_and_returns_ranked_indices(gw: Gateway, fake: FakeOpenRouter) -> None:
    fake.rerank_responses.append(httpx2.Response(200, json={
        "model": "cohere/rerank-4-pro", "provider": "Cohere",
        "results": [{"index": 2, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.40}],
        "usage": {"cost": 0.0025}}))  # fmt: skip
    ranked = await gw.rerank(CTX, "shiny bits in oil", ["a", "b", "chip detector"], top_n=2)
    assert ranked == [(2, 0.91), (0, 0.40)]
    body = fake.bodies("/rerank")[0]
    assert body["provider"]["data_collection"] == "deny" and body["top_n"] == 2


async def test_rerank_402_raises_credits_exhausted(gw: Gateway, fake: FakeOpenRouter, ledger: MemoryLedger) -> None:
    fake.rerank_responses.append(error(402, "Insufficient credits"))
    with pytest.raises(CreditsExhausted):
        await gw.rerank(CTX, "q", ["a"])
    assert ledger.records[0].status == "error:402"


async def test_price_override_applies_from_100k_prompt_tokens(fake: FakeOpenRouter) -> None:
    async with fake.client() as http:
        info = await ModelCatalog(http, "https://openrouter.ai/api/v1").get(HAIKU)
    assert info.price(99_999).prompt == pytest.approx(1e-7)
    assert info.price(100_000).prompt == pytest.approx(5e-7)
    assert info.price(100_000).completion == pytest.approx(2.5e-6)


async def test_time_based_price_uses_the_peak_and_bad_listings_are_skipped(fake: FakeOpenRouter) -> None:
    async with fake.client() as http:
        catalog = ModelCatalog(http, "https://openrouter.ai/api/v1")
        info = await catalog.get("deepseek/deepseek-v4.1-flash-1017")
        assert info.price(0).prompt == pytest.approx(3e-7) and info.price(0).completion == pytest.approx(1.2e-6)
        with pytest.raises(UnsupportedModel):
            await catalog.get("broken/listing")
        assert (await catalog.get(HAIKU)).id == HAIKU  # the rest of the catalog still loaded


def test_config_version_ignores_secrets_but_tracks_behaviour() -> None:
    base = settings().config_version()
    assert settings(openrouter_api_key="sk-other", db_password_app_rw="y").config_version() == base
    assert settings(chat_model=LUNA).config_version() != base
    assert settings(run_cost_cap_usd=0.5).config_version() != base
