"""Runs one agent run in its own process, for the kill-and-resume test (tests/test_agent_kill.py).

    python -m tests.agent_proc <hang|finish> <run_id> <marker_file> <call_log>

The model is a deterministic stub (no network). Which act turn it's on is read from the conversation it's
given, not a process-local counter, so a resumed process continues the script where the checkpoint left
off. In `hang` mode the third act turn writes the marker file and blocks, so the parent can SIGKILL it with
two tool rounds already checkpointed. Each model call is appended to the call log.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel

from app.agent.runner import execute_run
from app.config import Settings
from app.llm import ChatResult, EmbedResult, ToolCall
from app.resources import agent_resources
from tests.agent_fixtures import DIAGNOSIS, DRAFT, SCRIPT_ACT, as_json

T = TypeVar("T", bound=BaseModel)


class StubGateway:
    def __init__(self, mode: str, marker: Path, log: Path) -> None:
        self.mode, self.marker, self.log = mode, marker, log

    def _note(self, what: str) -> None:
        with self.log.open("a") as f:
            f.write(f"{what}\n")

    async def chat(self, ctx: Any, messages: list[dict[str, Any]], **kw: Any) -> ChatResult:
        turn = sum(1 for m in messages if m["role"] == "assistant")
        self._note(f"act {turn}")
        if self.mode == "hang" and turn == 2:
            self.marker.write_text("ready")
            await asyncio.sleep(3600)  # the parent kills us here
        calls = SCRIPT_ACT[turn] if turn < len(SCRIPT_ACT) else []
        tcs = tuple(ToolCall(f"call_{turn}_{i}", n, as_json(a)) for i, (n, a) in enumerate(calls))
        return ChatResult("" if tcs else "Enough evidence.", "tool_calls" if tcs else "stop", "stub", "stub", 0.0, tcs)

    async def structured(self, ctx: Any, messages: Any, schema: type[T], **kw: Any) -> T:
        self._note(f"structured {schema.__name__}")
        return schema.model_validate(DIAGNOSIS if schema.__name__ == "Diagnosis" else DRAFT)

    async def embed(self, ctx: Any, texts: list[str], *, model: str | None = None) -> EmbedResult:
        return EmbedResult([[1.0] * 1024 for _ in texts], 0.0)

    async def rerank(self, ctx: Any, query: str, documents: list[str], **kw: Any) -> list[tuple[int, float]]:
        return [(i, 0.9 - i / 100) for i in range(min(kw.get("top_n") or len(documents), len(documents)))]


async def main() -> None:
    mode, run_id, marker, log = sys.argv[1], UUID(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
    s = Settings()
    async with agent_resources(s, StubGateway(mode, marker, log)) as deps:  # type: ignore[arg-type]
        print(await execute_run(run_id, deps))


if __name__ == "__main__":
    asyncio.run(main())
