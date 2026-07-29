"""
Background jobs runner
======================
Runs the scheduled work that used to live inside the API process:

  - the hourly tiered-SLA escalation cycle (which also awards weekly streaks,
    purges expired OTPs and stale refresh tokens, and deactivates workers who
    never accepted their invitation)
  - the connection-pool health monitor

Run it as its own service::

    docker compose up -d jobs
    # or directly
    python -m app.jobs

Why it is separate
------------------
Both workers are started by the application's startup hook, which runs **once
per uvicorn worker process**. With ``--workers 2`` the escalation cycle ran
twice concurrently: duplicate escalations, duplicate notifications, and the
weekly streak bonus granted twice. Pinning the API to one worker avoided that
but capped throughput.

Splitting them out lets the API scale workers freely. ``RUN_BACKGROUND_JOBS`` is
false for the API service and true here. The escalation cycle additionally takes
a Postgres advisory lock, so even a misconfiguration that starts two runners
cannot double-process a cycle.

This module imports ``app.main``. That builds the FastAPI application object —
routers, middleware, Sentry — but starts no server, and it keeps a single
definition of the escalation cycle rather than a copy that can drift.
"""

import asyncio
import signal

from app.core.config import settings, validate_settings
from app.core.logger import get_logger
from app.database import check_db_connection, dispose_pool
from app.services.pool_monitor import PoolHealthCheckThread

logger = get_logger("jobs")


async def main() -> None:
    """Start the background workers and run until interrupted."""
    # Same contract as the API: a production process with an unsafe
    # configuration must not start.
    validate_settings()

    if not settings.RUN_BACKGROUND_JOBS:
        logger.error(
            "RUN_BACKGROUND_JOBS is false — refusing to start the jobs runner, "
            "since it would do nothing. Set it to true for this service."
        )
        raise SystemExit(1)

    if not check_db_connection():
        logger.critical("DATABASE IS UNREACHABLE — jobs will retry on the next cycle")

    # Imported here, not at module scope: it constructs the FastAPI app, and
    # deferring it keeps the failure above (config, database) ahead of it.
    from app.main import _auto_escalate_loop

    logger.info(
        "Background jobs runner started (environment=%s)", settings.ENVIRONMENT
    )

    pool_monitor = PoolHealthCheckThread(interval_seconds=30, daemon=True)
    pool_monitor.start()

    task = asyncio.create_task(_auto_escalate_loop())

    # Compose sends SIGTERM on `down`/`stop`; exit cleanly rather than being
    # killed mid-cycle.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    await stop.wait()
    logger.info("Shutdown signal received — stopping background jobs")

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    pool_monitor.stop()
    pool_monitor.join(timeout=5)
    dispose_pool()
    logger.info("Background jobs runner stopped")


if __name__ == "__main__":
    asyncio.run(main())
