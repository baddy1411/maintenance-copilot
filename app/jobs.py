"""Background jobs on the Postgres queue (ADR-0001). The worker process runs these; the API only defers them.

Queues:
  default  ingestion (sync_ads every 6 h, sync_sdr weekly)
  agent    agent runs (ADR-0004); reap_runs every 5 min re-enqueues runs whose worker died;
           expire_approvals daily at 03:30 UTC
"""

import logging
from uuid import UUID

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued

from app.agent.runner import RunDeps, emit, execute_run, expire_approvals, fail, stale_runs
from app.config import Role, Settings
from app.ingest.ads import sync_ads
from app.ingest.sdr import sync_sdr
from app.resources import ingest_resources

logger = logging.getLogger(__name__)

# Opened once per worker process by `copilot worker` (app/cli.py), not per job.
RUNTIME: dict[str, RunDeps] = {}


def runtime() -> RunDeps:
    try:
        return RUNTIME["agent"]
    except KeyError:
        raise RuntimeError("agent runtime not initialised: run jobs with `copilot worker`") from None


async def enqueue_run(app: procrastinate.App, run_id: UUID) -> bool:
    """Defer the job for one run. The queueing lock stops a second copy being queued, and the lock stops
    two copies running at once (e.g. the reaper re-enqueues while a slow job is still running).
    Returns False if one was already queued."""
    key = f"run:{run_id}"
    try:
        await app.configure_task("run_agent", queue="agent", lock=key, queueing_lock=key).defer_async(
            run_id=str(run_id)
        )
    except AlreadyEnqueued:
        return False
    return True


async def reap(app: procrastinate.App, deps: RunDeps) -> dict[str, int]:
    out = {"requeued": 0, "failed": 0, "already_queued": 0}
    for run_id, attempts in await stale_runs(deps.rw_pool, deps.settings.run_stale_after_s):
        if attempts >= deps.settings.run_max_attempts:
            await fail(deps, run_id, f"no progress after {attempts} attempts (worker died or job lost)")
            out["failed"] += 1
        elif await enqueue_run(app, run_id):
            await emit(deps.rw_pool, run_id, "status", {"status": "requeued", "after_attempts": attempts})
            out["requeued"] += 1
        else:
            out["already_queued"] += 1
    return out


def make_app(settings: Settings, role: Role = "ingest_rw") -> procrastinate.App:
    """`role`: the worker connects as ingest_rw; the API (which only defers) as app_rw."""
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=settings.dsn(role)))

    # Every 6 h (DESIGN §6.2). The FR posts each day's issue in the US morning, and the 3-day overlap
    # re-reads anything that arrived late. `queueing_lock` stops runs piling up if one is slow.
    @app.periodic(cron="15 */6 * * *", periodic_id="sync_ads")
    @app.task(name="sync_ads", queueing_lock="sync_ads",
              retry=procrastinate.RetryStrategy(max_attempts=3, exponential_wait=60))  # fmt: skip
    async def sync_ads_task(timestamp: int) -> None:
        async with ingest_resources(settings) as r:
            await sync_ads(settings, r.fetcher, r.pool, r.gateway)

    # Weekly, Sunday 04:45 UTC (docs/sdr.md): the FAA files are regenerated daily, but SDRs feed pattern
    # questions where a week's lag doesn't matter, and each sync downloads ~90 MB.
    @app.periodic(cron="45 4 * * 0", periodic_id="sync_sdr")
    @app.task(name="sync_sdr", queueing_lock="sync_sdr",
              retry=procrastinate.RetryStrategy(max_attempts=3, exponential_wait=300))  # fmt: skip
    async def sync_sdr_task(timestamp: int) -> None:
        async with ingest_resources(settings, embed=False) as r:
            await sync_sdr(settings, r.fetcher, r.pool)

    # No procrastinate retry: the run's own checkpoint and the reaper handle failures (ADR-0004).
    @app.task(name="run_agent", queue="agent")
    async def run_agent_task(run_id: str) -> None:
        status = await execute_run(UUID(run_id), runtime())
        logger.info("run %s -> %s", run_id, status)

    @app.periodic(cron="*/5 * * * *", periodic_id="reap_runs")
    @app.task(name="reap_runs", queue="agent", queueing_lock="reap_runs")
    async def reap_runs_task(timestamp: int) -> None:
        logger.info("reap_runs: %s", await reap(app, runtime()))

    @app.periodic(cron="30 3 * * *", periodic_id="expire_approvals")
    @app.task(name="expire_approvals", queue="agent", queueing_lock="expire_approvals")
    async def expire_approvals_task(timestamp: int) -> None:
        expired = await expire_approvals(runtime().rw_pool)
        logger.info("expire_approvals: %d run(s) expired", len(expired))

    return app
