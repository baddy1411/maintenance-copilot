"""`copilot <command>`. One image, one entrypoint per process type (DESIGN §4).

`api` and `mcp` are added by their Phase 2 slices.
"""

import argparse
import asyncio
import logging
from datetime import date

from app.config import Settings


async def _ingest_ads(args: argparse.Namespace) -> None:
    from app.ingest.ads import sync_ads
    from app.resources import ingest_resources

    s = Settings()
    async with ingest_resources(s, embed=not args.no_embed) as r:
        report = await sync_ads(s, r.fetcher, r.pool, r.gateway, since=args.since, until=args.until, limit=args.limit)
    print(report)


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
    parser = argparse.ArgumentParser(prog="copilot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply all database migrations (run before any service update)")

    ads = sub.add_parser("ingest-ads", help="sync Airworthiness Directives from the Federal Register")
    ads.add_argument("--since", type=date.fromisoformat, help="publication date to start from (backfill)")
    ads.add_argument("--until", type=date.fromisoformat)
    ads.add_argument("--limit", type=int, help="stop after this many FR documents")
    ads.add_argument("--no-embed", action="store_true", help="store chunks without vectors (no model spend)")

    emb = sub.add_parser("embed-missing", help="embed chunks that have no vector for the active model")
    emb.add_argument("--limit", type=int)
    emb.add_argument("--cap", type=float, default=1.00, help="hard dollar cap for this job")

    sub.add_parser("worker", help="run background jobs (ingestion, scheduled syncs)")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        from app.migrate import migrate

        print(f"database at revision {migrate(Settings())}")
    elif args.command == "ingest-ads":
        asyncio.run(_ingest_ads(args))
    elif args.command == "embed-missing":
        asyncio.run(_embed_missing(args))
    elif args.command == "worker":
        asyncio.run(_worker())


if __name__ == "__main__":
    main()
