"""Background jobs on the Postgres queue (ADR-0001). The worker process runs these."""

import procrastinate

from app.config import Settings
from app.ingest.ads import sync_ads
from app.resources import ingest_resources


def make_app(settings: Settings) -> procrastinate.App:
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=settings.dsn("ingest_rw")))

    # Every 6 h (DESIGN §6.2). The FR posts each day's issue in the US morning, and the 3-day overlap
    # re-reads anything that arrived late. `queueing_lock` stops runs piling up if one is slow.
    @app.periodic(cron="15 */6 * * *", periodic_id="sync_ads")
    @app.task(
        name="sync_ads",
        queueing_lock="sync_ads",
        retry=procrastinate.RetryStrategy(max_attempts=3, exponential_wait=60),
    )
    async def sync_ads_task(timestamp: int) -> None:
        async with ingest_resources(settings) as r:
            await sync_ads(settings, r.fetcher, r.pool, r.gateway)

    return app
