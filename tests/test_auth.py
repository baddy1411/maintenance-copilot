"""Authentication, authorisation, rate limits and HTTP hardening (#13, DESIGN §7.3, docs/auth.md).

The route table below classifies every route the app serves. A new route that isn't classified fails
test_every_route_is_classified, so no endpoint can ship without an access decision. Needs Docker.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from pydantic import SecretStr

from app.api.auth import AuthConfigError, check_auth_config
from app.api.main import create_app
from app.config import Settings
from app.llm import Gateway
from app.qa.answer import DELIMITER
from tests.agent_fixtures import clear_approval_tables, create_run, seed
from tests.auth_helpers import PUBLIC_PEM, bearer, token, with_auth
from tests.fake_openrouter import FakeOpenRouter, MemoryLedger, sse_completion

pytestmark = pytest.mark.integration
RUN = "00000000-0000-4000-8000-000000000000"

PUBLIC = {("GET", "/healthz")}
DEV_ONLY = {("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/docs/oauth2-redirect")}
AUTHENTICATED = {("POST", "/ask"), ("POST", "/runs"), ("GET", "/runs/{run_id}"), ("GET", "/runs/{run_id}/events")}
SUPERVISOR = {("POST", "/runs/{run_id}/decision"), ("GET", "/approvals"), ("GET", "/audit/runs/{run_id}")}
ADMIN = {("GET", "/audit/verify")}
BODIES = {"/ask": {"question": "What does a chip detector do?"},
          "/runs": {"registration": "MX-001", "symptom": "Metal in the oil filter"},
          "/runs/{run_id}/decision": {"action": "approve", "draft_sha256": "0" * 64}}  # fmt: skip


@dataclass
class Env:
    http: httpx2.AsyncClient
    app: FastAPI
    fake: FakeOpenRouter
    db: Settings

    async def call(self, method: str, path: str, headers: dict[str, str] | None = None) -> httpx2.Response:
        url = path.replace("{run_id}", RUN)
        body = BODIES.get(path)
        return await self.http.request(method, url, json=body, headers=headers or {})


async def make_env(db: Settings, **extra: Any) -> AsyncIterator[Env]:
    seed(db)
    clear_approval_tables(db)
    with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("TRUNCATE limits")
    s = with_auth(db, openrouter_api_key=SecretStr("sk-test"), llm_max_retries=0, **extra)
    fake = FakeOpenRouter(embed_dim=1024)
    async with fake.client() as llm_http:
        app = create_app(s, Gateway(s, llm_http, MemoryLedger()))
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as http,
        ):
            yield Env(http, app, fake, db)


@pytest.fixture
async def env(db: Settings) -> AsyncIterator[Env]:
    async for e in make_env(db):
        yield e


# ---- every route has an access decision ------------------------------------------------------------------


def routes(app: FastAPI) -> set[tuple[str, str]]:
    """Every (method, path) the app serves. FastAPI 0.162 wraps included routers (_IncludedRouter), so walk into
    them. Add OpenAPI's view too, which covers include-time prefixes. Routes hidden from OpenAPI
    (include_in_schema=False) are still caught by the walk."""
    out: set[tuple[str, str]] = set()

    def walk(rs: list[Any]) -> None:
        for r in rs:
            if hasattr(r, "original_router"):
                walk(r.original_router.routes)
            elif hasattr(r, "path"):
                for m in (getattr(r, "methods", None) or {"GET"}) - {"HEAD", "OPTIONS"}:
                    out.add((m, r.path))

    walk(list(app.routes))
    for path, ops in app.openapi()["paths"].items():
        out |= {(m.upper(), path) for m in ops}
    return out


async def test_every_route_is_classified(env: Env) -> None:
    unclassified = routes(env.app) - PUBLIC - DEV_ONLY - AUTHENTICATED - SUPERVISOR - ADMIN
    assert unclassified == set(), f"classify these routes in tests/test_auth.py: {sorted(unclassified)}"


async def test_every_protected_route_rejects_a_missing_token(env: Env) -> None:
    for method, path in sorted(AUTHENTICATED | SUPERVISOR | ADMIN):
        r = await env.call(method, path)
        assert r.status_code == 401, (method, path, r.status_code)
        assert r.headers["www-authenticate"].startswith("Bearer")


async def test_role_restricted_routes_refuse_lower_roles(env: Env) -> None:
    for method, path in sorted(SUPERVISOR):
        assert (await env.call(method, path, bearer("tech-1", "technician"))).status_code == 403, path
    for method, path in sorted(ADMIN):
        assert (await env.call(method, path, bearer("sup-1", "supervisor"))).status_code == 403, path
        assert (await env.call(method, path, bearer("adm-1", "admin"))).status_code == 200, path


async def test_public_route_needs_no_token(env: Env) -> None:
    assert (await env.http.get("/healthz")).status_code == 200


# ---- bad tokens --------------------------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def hs256_with_public_key() -> str:
    """The classic algorithm-confusion attack: HMAC-signed, using the server's public key as the secret."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64(json.dumps({"sub": "mallory", "role": "admin", "iss": "https://idp.test",
                               "aud": "maintenance-copilot", "iat": now, "exp": now + 600}).encode())  # fmt: skip
    sig = hmac.new(PUBLIC_PEM.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(sig)}"


def alg_none() -> str:
    now = int(time.time())
    header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"sub": "mallory", "role": "admin", "iss": "https://idp.test",
                               "aud": "maintenance-copilot", "iat": now, "exp": now + 600}).encode())  # fmt: skip
    return f"{header}.{payload}."


BAD_TOKENS = {
    "garbage": "not-a-jwt",
    "other signing key": token(key=ec.generate_private_key(ec.SECP256R1())),
    "expired": token(exp=int(time.time()) - 3600),
    "not yet valid": token(nbf=int(time.time()) + 3600),
    "wrong audience": token(aud="some-other-api"),
    "wrong issuer": token(iss="https://evil.example"),
    "no subject": token(sub=None),
    "no expiry": token(exp=None),
    "unknown role": token(role="superuser"),
    "empty subject": token(sub=""),
    "alg none": alg_none(),
    "HS256 with the public key": hs256_with_public_key(),
}


@pytest.mark.parametrize("name", sorted(BAD_TOKENS))
async def test_bad_tokens_are_401(env: Env, name: str) -> None:
    r = await env.http.get("/approvals", headers={"Authorization": f"Bearer {BAD_TOKENS[name]}"})
    assert r.status_code == 401, (name, r.text)
    assert 'error="invalid_token"' in r.headers["www-authenticate"]


async def test_basic_auth_is_not_accepted(env: Env) -> None:
    r = await env.http.get("/approvals", headers={"Authorization": "Basic YWRtaW46YWRtaW4="})
    assert r.status_code == 401


# ---- startup guards ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "message"),
    [({"env": "prod", "dev_jwt_key_file": "data/dev-jwt-key.pem"}, "dev token issuer"),
     ({"env": "prod", "jwt_issuer": "maintenance-copilot-dev"}, "JWT_ISSUER"),
     ({"env": "prod", "jwt_public_key": None}, "no JWT public key"),
     ({"env": "prod", "user_hash_salt": SecretStr("")}, "USER_HASH_SALT"),
     ({"jwt_algorithms": ["ES256", "HS256"]}, "asymmetric"),
     ({"jwt_algorithms": ["none"]}, "asymmetric")],
)  # fmt: skip
def test_unsafe_auth_configuration_refuses_to_start(db: Settings, change: dict[str, Any], message: str) -> None:
    with pytest.raises(AuthConfigError, match=message):
        check_auth_config(with_auth(db).model_copy(update=change))


def test_a_proper_production_configuration_starts(db: Settings) -> None:
    check_auth_config(with_auth(db, env="prod"))


async def test_docs_and_schema_are_off_outside_local(db: Settings) -> None:
    app = create_app(with_auth(db, env="prod"))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as http:
        assert (await http.get("/docs")).status_code == 404
        assert (await http.get("/openapi.json")).status_code == 404


# ---- rate limits -------------------------------------------------------------------------------------------


async def test_qa_is_limited_per_user_per_hour(db: Settings) -> None:
    async for e in make_env(db, qa_per_hour=2):
        e.fake.rerank_top = 0.1  # refused before generation: no model call needed for this test
        for _ in range(2):
            assert (
                await e.http.post("/ask", json={"question": "chip detector?"}, headers=bearer("t1"))
            ).status_code == 200
        r = await e.http.post("/ask", json={"question": "chip detector?"}, headers=bearer("t1"))
        assert r.status_code == 429 and 0 < int(r.headers["retry-after"]) <= 3600
        other = await e.http.post("/ask", json={"question": "chip detector?"}, headers=bearer("t2"))
        assert other.status_code == 200  # per user


async def test_runs_are_limited_per_day_but_retries_are_free(db: Settings) -> None:
    async for e in make_env(db, runs_per_day=1, runs_concurrent=5):
        body = {"registration": "MX-001", "symptom": "Metal in the oil filter"}
        first = await e.http.post("/runs", json=body, headers=bearer("t1") | {"Idempotency-Key": "a"})
        assert first.status_code == 202
        for _ in range(3):  # retries of the same request return the same run and don't count
            again = await e.http.post("/runs", json=body, headers=bearer("t1") | {"Idempotency-Key": "a"})
            assert again.status_code == 200 and again.json()["run_id"] == first.json()["run_id"]
        new = await e.http.post("/runs", json=body, headers=bearer("t1") | {"Idempotency-Key": "b"})
        assert new.status_code == 429 and int(new.headers["retry-after"]) <= 86400


async def test_concurrent_runs_are_limited_even_under_a_race(db: Settings) -> None:
    async for e in make_env(db, runs_concurrent=1, runs_per_day=50):
        body = {"registration": "MX-001", "symptom": "Metal in the oil filter"}
        a, b = await asyncio.gather(
            e.http.post("/runs", json=body, headers=bearer("t1") | {"Idempotency-Key": "x"}),
            e.http.post("/runs", json=body, headers=bearer("t1") | {"Idempotency-Key": "y"}),
        )
        assert sorted([a.status_code, b.status_code]) == [202, 429]
        # A run waiting on a supervisor doesn't hold the worker, so it doesn't count.
        with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
            conn.execute("UPDATE runs SET status = 'pending_approval'")
        c = await e.http.post("/runs", json=body, headers=bearer("t1") | {"Idempotency-Key": "z"})
        assert c.status_code == 202


# ---- access ------------------------------------------------------------------------------------------------


async def test_restricted_documents_follow_the_role(db: Settings) -> None:
    async for e in make_env(db):
        with psycopg.connect(db.dsn("migrator"), autocommit=True) as conn:
            conn.execute("INSERT INTO documents (doc_id, source, ingest_origin, title, sha256, access_level) "
                         "VALUES ('upload:s', 'upload', 'admin_upload', 'Shop manual', 'x', 'restricted')")  # fmt: skip
            conn.execute("INSERT INTO chunks (chunk_id, doc_id, ord, text) VALUES ('upload:s:c0', 'upload:s', 0, "
                         "'Chip detector shop procedure: secret torque 42 in-lb.')")  # fmt: skip
            conn.execute("INSERT INTO chunk_embeddings SELECT 'upload:s:c0', embed_model, embedding "
                         "FROM chunk_embeddings LIMIT 1")  # fmt: skip
        answer = "A chip detector collects metal [1]."
        for _ in range(2):
            e.fake.chat.append(
                sse_completion([answer, "\n" + DELIMITER + "\n", '[1] "collects ferrous metal particles"'])
            )
        await e.http.post("/ask", json={"question": "chip detector torque?"}, headers=bearer("t1", "technician"))
        await e.http.post("/ask", json={"question": "chip detector torque?"}, headers=bearer("s1", "supervisor"))
        tech_prompt, sup_prompt = (json.dumps(b["messages"]) for b in e.fake.bodies("/chat/completions"))
        assert "secret torque" not in tech_prompt and "secret torque" in sup_prompt


async def test_audit_reads_by_role(env: Env) -> None:
    run_id = create_run(env.db)
    assert (await env.http.get(f"/audit/runs/{run_id}", headers=bearer("t", "technician"))).status_code == 403
    assert (await env.http.get(f"/audit/runs/{run_id}", headers=bearer("s", "supervisor"))).json() == []
    verify = await env.http.get("/audit/verify", headers=bearer("a", "admin"))
    assert verify.json() == {"intact": True, "first_bad_id": None, "rows_checked": 0}


# ---- HTTP hardening ----------------------------------------------------------------------------------------


async def test_security_headers_on_json_and_sse(env: Env) -> None:
    run_id = create_run(env.db)
    with psycopg.connect(env.db.dsn("migrator"), autocommit=True) as conn:
        conn.execute("UPDATE runs SET status = 'failed' WHERE run_id = %s", (run_id,))
    for r in [await env.http.get("/healthz"),
              await env.http.get(f"/runs/{run_id}/events", headers=bearer("tech-1"))]:  # fmt: skip
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["content-security-policy"].startswith("default-src 'none'")
        assert r.headers["cache-control"] == "no-store"
        assert "strict-transport-security" not in r.headers  # HSTS only outside local/test


async def test_cors_allows_only_configured_origins(db: Settings) -> None:
    async for e in make_env(db, cors_origins=["https://app.example"]):
        pre = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"}
        ok = await e.http.options("/ask", headers={"Origin": "https://app.example", **pre})
        assert ok.headers.get("access-control-allow-origin") == "https://app.example"
        bad = await e.http.options("/ask", headers={"Origin": "https://evil.example", **pre})
        assert "access-control-allow-origin" not in bad.headers
