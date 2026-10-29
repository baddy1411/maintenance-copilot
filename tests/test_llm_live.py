"""Opt-in smoke test against the real OpenRouter API. It costs money, so it's skipped unless LIVE_LLM=1.

Expected spend: about $0.003 (chat, structured, embed, and one rerank at $0.0025). It has a hard cap of $0.02,
enforced by the gateway's own run cap against the costs recorded here.
Run: LIVE_LLM=1 uv run pytest tests/test_llm_live.py -v
"""

import os
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm import CallContext, Gateway
from app.llm.ledger import CallRecord, Spent

pytestmark = pytest.mark.skipif(os.environ.get("LIVE_LLM") != "1", reason="live API test; set LIVE_LLM=1")
CAP = 0.02


@dataclass
class SummingLedger:
    records: list[CallRecord] = field(default_factory=list)

    async def spent(self, run_id: UUID | None, user_hash: str | None) -> Spent:
        total = sum(r.cost for r in self.records)
        return Spent(run=total, user_day=total, global_day=total)

    async def record(self, rec: CallRecord, config_version: str) -> None:
        self.records.append(rec)


class Verdict(BaseModel):
    component: str
    urgent: bool


async def test_live_round_trip() -> None:
    s = Settings(run_cost_cap_usd=CAP, user_daily_cap_usd=CAP, global_daily_cap_usd=CAP)
    assert s.openrouter_api_key.get_secret_value(), "OPENROUTER_API_KEY missing from .env"
    ledger = SummingLedger()
    ctx = CallContext(kind="live_smoke", user_id="smoke", run_id=uuid4())
    async with httpx2.AsyncClient() as http:
        gw = Gateway(s, http, ledger)
        chat = await gw.chat(ctx, [{"role": "user", "content": "Reply with one word: torque"}], max_tokens=50)
        verdict = await gw.structured(
            ctx,
            [{"role": "user", "content": "Brake pedal sinks to the floor. Which component, and is it urgent?"}],
            Verdict,
            max_tokens=600,
        )
        vectors = (await gw.embed(ctx, ["metal particles on chip detector", "soft brake pedal"])).vectors
        ranked = await gw.rerank(ctx, "shiny bits in the oil", ["tyre pressure", "chip detector metal", "paint"])

    assert chat.text.strip() and verdict.component
    assert len(vectors) == 2 and len(vectors[0]) == 1024
    assert ranked[0][0] == 1
    assert all(r.status == "ok" and r.cost > 0 for r in ledger.records), ledger.records
    assert all(r.provider for r in ledger.records if r.ctx.kind != "rerank")
    total = sum(r.cost for r in ledger.records)
    print(f"\nlive spend ${total:.5f} over {len(ledger.records)} calls: "
          + ", ".join(f"{r.model}={r.cost:.5f} via {r.provider}" for r in ledger.records))  # fmt: skip
    assert total < CAP
