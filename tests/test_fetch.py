"""SSRF guards in the ingestion fetcher (DESIGN §6.2)."""

from collections.abc import Callable

import httpx2
import pytest

from app.ingest.fetch import FetchFailed, FetchRefused, SafeFetcher, allowed

HOSTS = allowed(["www.govinfo.gov", "www.federalregister.gov"])


def fetcher(handler: Callable[[httpx2.Request], httpx2.Response], resolves: dict[str, str] | None = None,
            **kw: object) -> SafeFetcher:  # fmt: skip
    table = {"www.govinfo.gov": "23.10.1.1", "www.federalregister.gov": "23.10.1.2"} | (resolves or {})

    async def resolver(host: str) -> list[str]:
        return [table[host]]

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return SafeFetcher(http, HOSTS, "test-agent/1", resolver=resolver, min_interval_s=0, **kw)  # type: ignore[arg-type]


async def test_allowed_host_is_fetched_with_our_user_agent() -> None:
    seen = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req.headers["user-agent"])
        return httpx2.Response(200, text="<pre>ok</pre>")

    got = await fetcher(handler).get("https://www.govinfo.gov/x.htm")
    assert (got.status, got.body, seen) == (200, b"<pre>ok</pre>", ["test-agent/1"])


@pytest.mark.parametrize(
    "url",
    ["http://www.govinfo.gov/x", "https://evil.example/x", "https://169.254.169.254/latest/meta-data/",
     "https://www.govinfo.gov.evil.example/x"],
)  # fmt: skip
async def test_refuses_non_https_and_unlisted_hosts(url: str) -> None:
    with pytest.raises(FetchRefused):
        await fetcher(lambda r: httpx2.Response(200)).get(url)


@pytest.mark.parametrize("addr", ["169.254.169.254", "10.0.0.5", "127.0.0.1", "192.168.1.1", "::1", "fd00::1"])
async def test_refuses_allow_listed_host_resolving_to_private_address(addr: str) -> None:
    with pytest.raises(FetchRefused, match="non-public"):
        await fetcher(lambda r: httpx2.Response(200), {"www.govinfo.gov": addr}).get("https://www.govinfo.gov/x")


async def test_redirect_target_is_rechecked() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"location": "https://169.254.169.254/creds"})

    with pytest.raises(FetchRefused, match="not allow-listed"):
        await fetcher(handler).get("https://www.govinfo.gov/x")


async def test_redirect_within_allow_list_is_followed() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path == "/old":
            return httpx2.Response(301, headers={"location": "/new"})
        return httpx2.Response(200, text="moved")

    got = await fetcher(handler).get("https://www.govinfo.gov/old")
    assert got.url == "https://www.govinfo.gov/new" and got.body == b"moved"


async def test_redirect_loop_is_cut_off() -> None:
    with pytest.raises(FetchRefused, match="too many redirects"):
        await fetcher(lambda r: httpx2.Response(302, headers={"location": "/again"})).get("https://www.govinfo.gov/a")


async def test_oversized_response_is_abandoned() -> None:
    with pytest.raises(FetchRefused, match="over 1,000 bytes"):
        await fetcher(lambda r: httpx2.Response(200, content=b"x" * 5000), max_bytes=1000).get(
            "https://www.govinfo.gov/big"
        )


async def test_5xx_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ingest.fetch.asyncio.sleep", _no_sleep)
    responses = [httpx2.Response(503), httpx2.Response(503), httpx2.Response(200, text="ok")]
    got = await fetcher(lambda r: responses.pop(0)).get("https://www.govinfo.gov/x")
    assert got.body == b"ok" and responses == []


async def test_4xx_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ingest.fetch.asyncio.sleep", _no_sleep)
    calls = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(404)

    with pytest.raises(FetchFailed, match="HTTP 404"):
        await fetcher(handler).get("https://www.govinfo.gov/x")
    assert len(calls) == 1


async def test_gives_up_after_the_attempt_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ingest.fetch.asyncio.sleep", _no_sleep)
    with pytest.raises(FetchFailed, match="after 3 attempts"):
        await fetcher(lambda r: httpx2.Response(500), attempts=3).get("https://www.govinfo.gov/x")


async def _no_sleep(_: float) -> None:
    return None
