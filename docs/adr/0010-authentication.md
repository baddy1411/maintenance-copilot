# ADR-0010: Bearer JWTs verified against an asymmetric key, with a local-only dev issuer

Status: Accepted · 2026-11-02 · implemented in #13 (`docs/auth.md`). Phase 5 adds OIDC discovery and JWKS.

## Context
Every API call needs an identity and a role (technician, supervisor, admin). The role decides who can decide on runs and read audit entries, and which documents retrieval may return. DESIGN §7.3 says JWT from Phase 2, OIDC in Phase 5, and **no dev issuer in production** (security review S9). The same verification path should run locally, in CI and in production, so the tests prove the real thing.

## Options
| Option | Pros | Cons |
| --- | --- | --- |
| Server-side sessions + cookies | Simple revocation | Needs CSRF protection, a session store, a login UI; awkward for the MCP server and API clients |
| Static API keys per user | Trivial | No standard roles or expiry; keys leak and live forever; no path to the company's identity provider |
| **Bearer JWTs, asymmetric (ES256), verified against a public key** | Stateless across replicas; standard claims (`exp`, `iss`, `aud`); the identity provider holds the private key; OIDC providers issue exactly this | Revocation only by expiry (short TTLs); needs key rotation (JWKS in Phase 5) |
| Full OIDC now | The end state | An identity provider in local dev; nothing to show until Phase 5 |

## Decision
- **Bearer JWTs**, verified with PyJWT 2.15.1 against a configured **public key**, with an **algorithm allow-list** (default ES256). That refuses `alg: none`, and refuses HMAC tokens signed with the public key as the secret (tested).
- **Required claims:** `exp`, `iat`, `sub`, `iss`, `aud`. Issuer and audience must match configuration. `role` must be one of `role_access`, which also maps the role to document access levels.
- **Dev issuer:** `copilot dev-keys` writes an EC P-256 key locally (git-ignored, mode 600), and `copilot dev-token --user --role` signs tokens with it. Local, CI and production all verify through the same code. **Outside ENV=local/test, startup fails** if a dev key is configured, if the issuer is the dev issuer, if no public key is set, if the algorithms include `none` or HMAC, or if `USER_HASH_SALT` is shorter than 16 characters.
- The earlier `X-Dev-User` / `X-Dev-Role` headers (#9–#12) are **removed**.

## Consequences
- Tokens can't be revoked before they expire. Phase 5 (OIDC) should keep access-token TTLs short. The dev default is 8 hours.
- Key rotation needs JWKS (`kid` → key). Phase 5 adds discovery and `PyJWKClient`.
- Clients that call the API, including the future MCP server (#24) and the web app (#14), get tokens from the identity provider. Locally, from `copilot dev-token`.
