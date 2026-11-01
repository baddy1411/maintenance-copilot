"""The HTTP API (DESIGN §4): FastAPI, async end to end. `copilot api` runs it under uvicorn.

Routes: POST /ask (cited Q&A), and the agent-run routes in app/api/runs.py.

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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.sse import EventSourceResponse, ServerSentEvent
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from app.api.admin import router as admin_router
from app.api.auth import DEV_ENVS, Principal, check_auth_config, current_principal
from app.api.limits import limit_subject, take
from app.api.runs import router as runs_router
from app.api.security import SecurityHeaders
from app.config import Settings
from app.jobs import make_app
from app.llm import Gateway, PostgresLedger
from app.qa.service import answer_events
from app.retrieval.search import RetrievalConfig, Retriever


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)  # DESIGN §2 spend limits: question <= 2,000 chars


def create_app(settings: Settings | None = None, gateway: Gateway | None = None) -> FastAPI:
    """`gateway` can be injected (tests). Otherwise it's built from settings in the lifespan."""
    s = settings or Settings()
    jwt_key = check_auth_config(s)  # fail at startup, not on the first request

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with (
            AsyncConnectionPool(s.dsn("tools_ro"), min_size=1, max_size=8, open=False) as ro,
            AsyncConnectionPool(s.dsn("app_rw"), min_size=1, max_size=4, open=False) as rw,
            httpx2.AsyncClient() as http,
        ):
            gw = gateway or Gateway(s, http, PostgresLedger(rw))
            jobs = make_app(s, role="app_rw")  # the API only defers jobs; the worker runs them
            async with jobs.open_async():
                app.state.settings = s
                app.state.jwt_key = jwt_key
                app.state.gateway = gw
                app.state.retriever = Retriever(ro, gw, RetrievalConfig())
                app.state.ro, app.state.rw, app.state.jobs = ro, rw, jobs
                yield

    dev = s.env in DEV_ENVS
    # Interactive docs and the OpenAPI schema only locally: the schema maps every route for an attacker.
    app = FastAPI(
        title="Maintenance Copilot", version="0.1.0", lifespan=lifespan,
        docs_url="/docs" if dev else None, redoc_url=None, openapi_url="/openapi.json" if dev else None,
    )  # fmt: skip
    app.include_router(runs_router)
    app.include_router(admin_router)
    # Outermost first: CORS answers preflights, then every response gets the security headers.
    app.add_middleware(SecurityHeaders, hsts=not dev)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID"],
                       allow_credentials=False, max_age=600)  # fmt: skip

    async def qa_allowance(request: Request, principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        """As a dependency, so the 429 is sent before the SSE response starts."""
        async with request.app.state.rw.connection() as conn:
            await take(conn, limit_subject(s, principal.user_id), "qa", "hour", s.qa_per_hour)
        return principal

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/ask", response_class=EventSourceResponse)
    async def ask(
        body: AskRequest, request: Request, principal: Annotated[Principal, Depends(qa_allowance)]
    ) -> AsyncIterator[ServerSentEvent]:
        st = request.app.state
        events = answer_events(body.question, principal.user_id, principal.access_levels, st.settings, st.retriever,
                               st.gateway)  # fmt: skip
        async with aclosing(events):  # close the chain at once on disconnect (see app/qa/service.py)
            async for ev in events:
                yield ServerSentEvent(event=ev.event, data=ev.data)

    return app
