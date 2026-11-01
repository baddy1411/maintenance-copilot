# Authentication, access and limits

Who can call the API, what each role can see and do, how much each user can use, and how the HTTP layer is hardened. It's the detailed companion to DESIGN §2 (spend limits), §7.2 and §7.3, and [ADR-0010](adr/0010-authentication.md).

Code: `app/api/auth.py`, `app/api/limits.py`, `app/api/security.py`, `app/api/admin.py`, `app/api/main.py`, `app/api/runs.py`. Tests: `tests/test_auth.py` (32), and every other API test now uses real signed tokens. Dates: 2026-11-02.

## 1. Using it locally

```bash
make env                                              # adds USER_HASH_SALT and the dev signing key (data/dev-jwt-key.pem)
TOKEN=$(uv run copilot dev-token --user tech-1 --role technician)
curl -H "Authorization: Bearer $TOKEN" -X POST localhost:8000/ask -H 'content-type: application/json' \
     -d '{"question": "What does a chip detector do?"}'
```

Roles: `technician`, `supervisor`, `admin`. Tokens last 8 hours by default (`--hours`).

## 2. Tokens
| Check | Rule |
| --- | --- |
| Scheme | `Authorization: Bearer <JWT>`; anything else is 401 |
| Signature | ES256 against `JWT_PUBLIC_KEY`, or locally the dev key's public half |
| Algorithms | allow-list (`JWT_ALGORITHMS`, default `["ES256"]`): `alg: none` and HMAC are refused, and startup refuses a config that allows them |
| Claims | `exp`, `iat`, `sub`, `iss`, `aud` required; issuer and audience must match; 30 s leeway |
| Role | `role` must be in `role_access`; `sub` must be 1–128 characters |
| Errors | 401 with `WWW-Authenticate: Bearer error="invalid_token"` and the error type, never the reason a signature failed |

Tested bad tokens, all 401: garbage, another signing key, expired, not yet valid, wrong audience, wrong issuer, no subject, empty subject, no expiry, unknown role, **`alg: none`**, **HS256 signed with the public key** (the algorithm-confusion attack), and HTTP Basic.

**Startup guards** (`check_auth_config`, tested). Outside ENV=local/test, the API refuses to start with:
- a dev key configured;
- the dev issuer name;
- no public key;
- `none` or HMAC in the algorithms;
- a `USER_HASH_SALT` shorter than 16 characters.

An empty salt had been the local default, which made the ledger's "pseudonymous" user ids plain hashes.

## 3. Who can do what

| Route | technician | supervisor | admin |
| --- | --- | --- | --- |
| `GET /healthz` | public | public | public |
| `POST /ask` | ✓ (public documents) | ✓ (public + restricted) | ✓ (public + restricted) |
| `POST /runs` | ✓ | ✓ | ✓ |
| `GET /runs/{id}`, `GET /runs/{id}/events` | own runs (others: 404) | any | any |
| `GET /approvals`, `POST /runs/{id}/decision` | 403 | ✓ (not own runs: 403) | ✓ |
| `GET /audit/runs/{id}` | 403 | ✓ | ✓ |
| `GET /audit/verify` | 403 | 403 | ✓ |
| `/docs`, `/openapi.json` | local/test only | | |

- **Document access** comes from `role_access`. Retrieval filters on it **inside the SQL**, before anything reaches the reranker or the model. Tested: a restricted shop-manual chunk appears in a supervisor's prompt and not in a technician's.
- **Every route must be classified.** `test_every_route_is_classified` lists every route the app serves, walking into FastAPI 0.162's wrapped sub-routers and adding the OpenAPI paths, and fails on any route not in the table. Checked: adding an unauthenticated `/debug` route makes it fail and names the route. Every protected route is also tested to return 401 without a token.

## 4. Rate limits
Counted in Postgres (ADR-0009), one atomic upsert per check, in UTC-aligned windows. Users are stored as the same salted hash the cost ledger uses.

| Limit | Default | Response |
| --- | --- | --- |
| Q&A per user | 60 per hour | 429 with `Retry-After` (seconds to the next window) |
| Runs per user | 20 per day | 429 with `Retry-After` |
| Runs in progress per user | 2 (queued, running or decided; a run waiting on a supervisor doesn't count) | 429 |
| Spend per user / globally | $1 / $5 per day | the gateway: Q&A degrades to retrieval only, runs fail with the reason (`docs/qa.md`, `docs/agent-runs.md`) |

- **A retry of `POST /runs` with the same Idempotency-Key returns the existing run before any limit is taken**, so client retries are free (tested: three retries at a 1-per-day limit).
- **The in-progress limit holds under a race.** A per-user advisory lock is held until the run insert commits. Tested: two simultaneous requests with the limit at 1 get exactly one 202 and one 429.
- The Q&A limit is a **dependency**, so its 429 goes out before the SSE response starts (the same lesson as run ownership in #10).
- Old `limits` rows need a periodic prune (#16).

## 5. HTTP hardening (`app/api/security.py`)
A pure ASGI middleware, so streaming responses aren't buffered. It sets these on every response, including SSE:

| Header | Value |
| --- | --- |
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'` (not on local `/docs`) |
| `Cache-Control` | `no-store`, **overriding** SSE's own `no-cache`: answers, drafts and audit entries mustn't be cached anywhere |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `no-referrer` |
| `Cross-Origin-Resource-Policy` | `same-site` |
| `Permissions-Policy` | camera, microphone and geolocation off |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains`, outside local/test only |

**CORS:** only origins in `CORS_ORIGINS` (none by default); `GET`/`POST`; the headers `Authorization`, `Content-Type`, `Idempotency-Key`, `Last-Event-ID`; no credentials. Tested: an allowed origin's preflight gets `Access-Control-Allow-Origin`, and another origin's doesn't. **Docs and the OpenAPI schema** are served only locally, because the schema maps every route for an attacker.

## 6. Live check
Against the local API with dev tokens. No model calls, no spend:

| Request | Result |
| --- | --- |
| `GET /approvals`, no token | 401 "missing bearer token" |
| garbage token | 401 "invalid token: DecodeError" |
| technician → `GET /approvals` | 403 "supervisors only" |
| supervisor → `GET /approvals` | 200 |
| technician → `POST /runs` for MX-999 | 404 "not in the fleet": refused before any work |
| supervisor → `GET /audit/verify` | 403 "admins only" |
| admin → `GET /audit/verify` | 200 `{"intact": true, "rows_checked": 4}` (the chain from #12) |

Security headers were present on every response.

## 7. Follow-ups
| Item | Where |
| --- | --- |
| OIDC discovery and JWKS (key rotation via `kid`), short access-token TTLs | Phase 5 |
| Prune old `limits` rows; alert on 429 spikes | #16 |
| Per-document grants beyond the two access levels | Phase 5 |
| Token handling in the web client (#14) and the MCP server's own OAuth (#24) | #14, #24 |
