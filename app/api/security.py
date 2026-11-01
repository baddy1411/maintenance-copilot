"""Security headers on every response, including SSE streams (DESIGN §7.2).

A pure ASGI middleware: it adds headers to `http.response.start` and never buffers the body, so streaming
responses (Q&A answers, run events) pass through untouched. Starlette's BaseHTTPMiddleware is avoided for
the same reason.
"""

from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


class SecurityHeaders:
    def __init__(self, app: ASGIApp, hsts: bool) -> None:
        self.app, self.hsts = app, hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        docs = scope["path"] in ("/docs", "/redoc", "/docs/oauth2-redirect")  # Swagger UI needs its own scripts

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[Any] = list(message.get("headers", []))
                add = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                    b"cross-origin-resource-policy": b"same-site",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
                }
                if not docs:
                    add[b"content-security-policy"] = API_CSP.encode()
                    add[b"cache-control"] = b"no-store"
                if self.hsts:
                    add[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains"
                # Ours win over what a response set itself: SSE sends "Cache-Control: no-cache", and API data
                # (answers, drafts, audit entries) must not be stored by browsers or proxies at all.
                headers = [(k, v) for k, v in headers if k.lower() not in add]
                headers += list(add.items())
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, with_headers)
