"""The HTTP API (DESIGN §4): FastAPI, async end to end. `copilot api` runs it under uvicorn.

POST /ask streams Server-Sent Events (app/qa/service.py lists them). FastAPI's SSE support sends a
`: ping` comment after 15 s without an event, which is the heartbeat DESIGN §7.5 asks for, so there's no
separate timer. On a client disconnect the request is cancelled, and the gateway closes the upstream model
stream and records the (estimated) cost of what was generated.
"""

from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import Annotated

import httpx2
from fastapi import Depends, FastAPI, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from app.api.auth import Principal, check_dev_auth_allowed, dev_principal
from app.config import Settings
from app.llm import Gateway, PostgresLedger
from app.qa.service import answer_events
from app.retrieval.search import RetrievalConfig, Retriever


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)  # DESIGN §2 spend limits: question <= 2,000 chars


def create_app(settings: Settings | None = None, gateway: Gateway | None = None) -> FastAPI:
    """`gateway` can be injected (tests). Otherwise it's built from settings in the lifespan."""
    s = settings or Settings()
    check_dev_auth_allowed(s)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with (
            AsyncConnectionPool(s.dsn("tools_ro"), min_size=1, max_size=8, open=False) as ro,
            AsyncConnectionPool(s.dsn("app_rw"), min_size=1, max_size=4, open=False) as rw,
            httpx2.AsyncClient() as http,
        ):
            gw = gateway or Gateway(s, http, PostgresLedger(rw))
            app.state.settings = s
            app.state.gateway = gw
            app.state.retriever = Retriever(ro, gw, RetrievalConfig())
            yield

    app = FastAPI(title="Maintenance Copilot", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/ask", response_class=EventSourceResponse)
    async def ask(
        body: AskRequest, request: Request, principal: Annotated[Principal, Depends(dev_principal)]
    ) -> AsyncIterator[ServerSentEvent]:
        st = request.app.state
        events = answer_events(body.question, principal.user_id, principal.access_levels, st.settings, st.retriever,
                               st.gateway)  # fmt: skip
        async with aclosing(events):  # close the chain at once on disconnect (see app/qa/service.py)
            async for ev in events:
                yield ServerSentEvent(event=ev.event, data=ev.data)

    return app
