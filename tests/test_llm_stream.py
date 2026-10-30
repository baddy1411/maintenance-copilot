"""Gateway.stream_chat: tokens, real cost from the last chunk, fallback, and cleanup on early stop or
cancellation (the client-disconnect path)."""

from collections.abc import AsyncIterator
from contextlib import aclosing
from uuid import uuid4

import anyio
import pytest

from app.config import Settings
from app.llm import CallContext, CreditsExhausted, Gateway, ModelUnavailable, StreamEnd
from app.llm.ledger import CallRecord
from tests.fake_openrouter import HAIKU, LUNA, FakeOpenRouter, MemoryLedger, error, sse_completion

PASSWORDS = {f"db_password_{r}": "x" for r in ("migrator", "app_rw", "tools_ro", "ingest_rw", "mcp_rw")}
CTX = CallContext(kind="answer", user_id="tech1", run_id=uuid4())
MSGS = [{"role": "user", "content": "What does a chip detector do?"}]


@pytest.fixture
def fake() -> FakeOpenRouter:
    return FakeOpenRouter()


@pytest.fixture
def ledger() -> MemoryLedger:
    return MemoryLedger()


@pytest.fixture
async def gw(fake: FakeOpenRouter, ledger: MemoryLedger) -> AsyncIterator[Gateway]:
    s = Settings(**{**PASSWORDS, "openrouter_api_key": "sk-test", "user_hash_salt": "salt"})  # type: ignore[arg-type]
    async with fake.client() as http:
        yield Gateway(s, http, ledger)


async def test_streams_tokens_then_records_the_real_cost(gw: Gateway, fake: FakeOpenRouter,
                                                         ledger: MemoryLedger) -> None:  # fmt: skip
    fake.chat.append(sse_completion(["A chip ", "detector ", "collects metal."], cost=0.00061))
    items = [i async for i in gw.stream_chat(CTX, MSGS, max_tokens=900, reasoning={"effort": "none"}, temperature=0)]
    assert "".join(i for i in items if isinstance(i, str)) == "A chip detector collects metal."
    end = items[-1]
    assert isinstance(end, StreamEnd) and (end.cost, end.provider, end.finish_reason) == (0.00061, "Anthropic", "stop")
    body = fake.bodies("/chat/completions")[0]
    assert body["stream"] is True and body["max_tokens"] == 900 and body["reasoning"] == {"effort": "none"}
    assert "temperature" not in body  # not in Haiku's supported_parameters
    [rec] = ledger.records
    assert (rec.status, rec.cost, rec.prompt_tokens, rec.completion_tokens) == ("ok", 0.00061, 2400, 180)


async def test_402_before_streaming_is_credits_exhausted(gw: Gateway, fake: FakeOpenRouter) -> None:
    fake.chat.append(error(402, "requires more credits"))
    with pytest.raises(CreditsExhausted):
        async for _ in gw.stream_chat(CTX, MSGS, max_tokens=100):
            pass
    assert len(fake.bodies("/chat/completions")) == 1


async def test_failure_before_the_first_token_falls_back(gw: Gateway, fake: FakeOpenRouter) -> None:
    fake.chat.extend([error(404, "No endpoints found that support the provided 'reasoning' parameter"),
                      sse_completion(["from luna"], model=LUNA, provider="OpenAI")])  # fmt: skip
    items = [i async for i in gw.stream_chat(CTX, MSGS, max_tokens=100)]
    assert items[0] == "from luna" and isinstance(items[-1], StreamEnd) and items[-1].model == LUNA
    assert [b["model"] for b in fake.bodies("/chat/completions")] == [HAIKU, LUNA]


async def test_both_models_down_is_model_unavailable(fake: FakeOpenRouter, ledger: MemoryLedger) -> None:
    s = Settings(**{**PASSWORDS, "openrouter_api_key": "sk-test", "llm_max_retries": 0})  # type: ignore[arg-type]
    async with fake.client() as http:
        gw = Gateway(s, http, ledger)
        fake.chat.extend([error(503, "Provider returned error"), error(503, "Provider returned error")])
        with pytest.raises(ModelUnavailable):
            async for _ in gw.stream_chat(CTX, MSGS, max_tokens=100):
                pass


async def test_stopping_early_records_an_estimated_cost(gw: Gateway, fake: FakeOpenRouter,
                                                        ledger: MemoryLedger) -> None:  # fmt: skip
    fake.chat.append(sse_completion(["one ", "two ", "three ", "four"]))
    stream = gw.stream_chat(CTX, MSGS, max_tokens=900)
    async with aclosing(stream):
        async for item in stream:
            if item == "two ":
                break  # what a client disconnect looks like from here
    [rec] = ledger.records
    assert rec.status == "cancelled_estimated" and rec.cost > 0  # cut-off generations are still billed


class SuspendingLedger(MemoryLedger):
    """Like the Postgres ledger, record() suspends (a real network round trip). anyio only delivers
    cancellation at awaits that suspend, so without this the unshielded cleanup would pass by luck."""

    async def record(self, rec: CallRecord, config_version: str) -> None:
        await anyio.sleep(0.01)
        await super().record(rec, config_version)


async def test_cancellation_still_runs_the_cleanup(fake: FakeOpenRouter) -> None:
    """A disconnect *cancels* the request (anyio, level-triggered). Unshielded awaits in the generator's
    cleanup would be cancelled too, and the ledger row would never be written. Checked: this test fails
    with the shield removed."""
    ledger = SuspendingLedger()
    s = Settings(**{**PASSWORDS, "openrouter_api_key": "sk-test", "user_hash_salt": "salt"})  # type: ignore[arg-type]
    fake.chat.append(sse_completion(["one ", "two ", "three "]))
    first = anyio.Event()

    async with fake.client() as http:
        gw = Gateway(s, http, ledger)

        async def consume() -> None:
            stream = gw.stream_chat(CTX, MSGS, max_tokens=900)
            async with aclosing(stream):
                async for item in stream:
                    if isinstance(item, str):
                        first.set()
                        await anyio.sleep(10)  # the "client" stalls; then the request is cancelled

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await first.wait()
            tg.cancel_scope.cancel()
    assert [r.status for r in ledger.records] == ["cancelled_estimated"]
