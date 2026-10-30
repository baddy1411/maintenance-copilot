"""`copilot <command>`. One image, one entrypoint per process type (DESIGN §4).

`api` and `mcp` are added by their Phase 2 slices.
"""

import argparse
import asyncio
import logging
from datetime import date
from pathlib import Path

from app.config import Settings


async def _ingest_ads(args: argparse.Namespace) -> None:
    from app.ingest.ads import sync_ads
    from app.resources import ingest_resources

    s = Settings()
    async with ingest_resources(s, embed=not args.no_embed) as r:
        report = await sync_ads(s, r.fetcher, r.pool, r.gateway, since=args.since, until=args.until, limit=args.limit)
    print(report)


async def _ingest_handbooks(args: argparse.Namespace) -> None:
    from app.ingest.pdf_docs import ingest_official
    from app.resources import ingest_resources

    s = Settings()
    async with ingest_resources(s, embed=not args.no_embed) as r:
        report = await ingest_official(s, r.fetcher, r.pool, r.gateway, only=set(args.only) if args.only else None)
    print(report)


async def _ingest_pdf(args: argparse.Namespace) -> None:
    from app.ingest.pdf_docs import ingest_upload
    from app.resources import ingest_resources

    s = Settings()
    async with ingest_resources(s, embed=not args.no_embed) as r:
        report, doc_id = await ingest_upload(s, r.pool, r.gateway, args.file, args.title, access_level=args.access)
    print(doc_id, report)


async def _embed_missing(args: argparse.Namespace) -> None:
    from app.ingest.embed_missing import embed_missing
    from app.resources import ingest_resources

    s = Settings()
    async with ingest_resources(s) as r:
        assert r.gateway is not None
        report = await embed_missing(r.pool, r.gateway, s.embed_model, cap_usd=args.cap, limit=args.limit)
    print(report)


async def _worker() -> None:
    from app.jobs import make_app

    app = make_app(Settings())
    async with app.open_async():
        await app.run_worker_async()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # one INFO line per request drowns the job logs
    parser = argparse.ArgumentParser(prog="copilot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply all database migrations (run before any service update)")

    ads = sub.add_parser("ingest-ads", help="sync Airworthiness Directives from the Federal Register")
    ads.add_argument("--since", type=date.fromisoformat, help="publication date to start from (backfill)")
    ads.add_argument("--until", type=date.fromisoformat)
    ads.add_argument("--limit", type=int, help="stop after this many FR documents")
    ads.add_argument("--no-embed", action="store_true", help="store chunks without vectors (no model spend)")

    hb = sub.add_parser("ingest-handbooks", help="fetch and ingest the FAA handbooks and AC 43.13-1B")
    hb.add_argument("--only", nargs="+", help="doc_ids to (re)ingest, e.g. faa-ac-43-13-1b")
    hb.add_argument("--no-embed", action="store_true", help="store chunks without vectors (no model spend)")

    up = sub.add_parser("ingest-pdf", help="ingest a local PDF as an admin upload (restricted by default)")
    up.add_argument("file", type=Path)
    up.add_argument("--title", required=True)
    up.add_argument("--access", choices=["public", "restricted"], default="restricted")
    up.add_argument("--no-embed", action="store_true")

    emb = sub.add_parser("embed-missing", help="embed chunks that have no vector for the active model")
    emb.add_argument("--limit", type=int)
    emb.add_argument("--cap", type=float, default=1.00, help="hard dollar cap for this job")

    sub.add_parser("worker", help="run background jobs (ingestion, scheduled syncs)")
    api = sub.add_parser("api", help="run the HTTP API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.command == "migrate":
        from app.migrate import migrate

        print(f"database at revision {migrate(Settings())}")
    elif args.command == "ingest-ads":
        asyncio.run(_ingest_ads(args))
    elif args.command == "ingest-handbooks":
        asyncio.run(_ingest_handbooks(args))
    elif args.command == "ingest-pdf":
        asyncio.run(_ingest_pdf(args))
    elif args.command == "embed-missing":
        asyncio.run(_embed_missing(args))
    elif args.command == "worker":
        asyncio.run(_worker())
    elif args.command == "api":
        import uvicorn

        from app.api.main import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
