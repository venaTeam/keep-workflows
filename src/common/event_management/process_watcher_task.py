import asyncio
import datetime
import logging

import redis
from filelock import FileLock, Timeout

from src.common.bl.dismissal_expiry_bl import DismissalExpiryBl
from src.common.bl.maintenance_windows_bl import MaintenanceWindowsBl
from src.common.consts import REDIS, WATCHER_LAPSED_TIME

logger = logging.getLogger(__name__)


async def start_watcher_if_enabled():
    """Start the watcher (dismissal expiry + maintenance-window recovery).

    Mode flags are read at call time from src.api.config / src.common.consts.
    Returns the asyncio task for the non-Redis loop, None otherwise.
    """
    import src.api.config as api_config
    import src.common.consts as consts

    enabled = api_config.WATCHER or (
        api_config.MAINTENANCE_WINDOWS
        and consts.MAINTENANCE_WINDOW_ALERT_STRATEGY == "recover_previous_status"
    )
    if not enabled:
        logger.info("Watcher disabled, not starting")
        return None

    if consts.REDIS:
        from src.common.arq_pool import get_pool

        redis_pool = await get_pool()
        job = await redis_pool.enqueue_job(
            "async_process_watcher",
            _queue_name=consts.KEEP_ARQ_QUEUE_MAINTENANCE,
        )
        logger.info("Enqueued watcher job", extra={"job_id": job.job_id})
        return None

    task = asyncio.create_task(async_process_watcher())
    logger.info("Watcher task started (dismissal expiry + maintenance recovery)")
    return task


async def async_process_watcher(*args):
    if REDIS:
        ctx = args[0]
        redis_instance: redis.Redis = ctx.get("redis")
        lock_key = "lock:watcher:process"
        is_exec_stopped = await redis_instance.set(
            lock_key, "1", ex=WATCHER_LAPSED_TIME + 10, nx=True
        )
        if not is_exec_stopped:
            logger.info("Watcher process is already running, skipping this run.")
            return
        logger.info("Watcher process started, acquiring lock.")
        try:
            loop = asyncio.get_running_loop()

            # Run maintenance windows recovery
            resp = await loop.run_in_executor(
                ctx.get("pool"), MaintenanceWindowsBl.recover_strategy, logger
            )

            # Run dismissal expiry check
            await loop.run_in_executor(
                ctx.get("pool"), DismissalExpiryBl.check_dismissal_expiry, logger
            )

        except Exception as e:
            logger.error("Error in watcher process: %s", e, exc_info=True)
            raise
        finally:
            await redis_instance.delete(lock_key)
            logger.info("Watcher process completed and lock released.")
        return resp
    else:
        while True:
            init_time = datetime.datetime.now()
            try:
                with FileLock(
                    "/tmp/watcher_process.lock", timeout=WATCHER_LAPSED_TIME // 2
                ):
                    logger.info("Watcher process started, acquiring lock.")
                    loop = asyncio.get_running_loop()

                    # Run maintenance windows recovery
                    resp = await loop.run_in_executor(
                        None, MaintenanceWindowsBl.recover_strategy, logger
                    )

                    # Run dismissal expiry check
                    await loop.run_in_executor(
                        None, DismissalExpiryBl.check_dismissal_expiry, logger
                    )

                    logger.info(
                        f"Sleeping for {WATCHER_LAPSED_TIME} seconds before next run."
                    )
                    complete_time = datetime.datetime.now()
                    await asyncio.sleep(
                        max(
                            0,
                            WATCHER_LAPSED_TIME
                            - (complete_time - init_time).total_seconds(),
                        )
                    )
                    logger.info("Watcher process completed.")
            except Timeout:
                logger.info("Watcher process is already running, skipping this run.")
                # Yield before retrying so a held lock can't busy-spin the loop.
                await asyncio.sleep(WATCHER_LAPSED_TIME)
            except Exception:
                # A failed tick (e.g. DB connectivity) must not kill the loop —
                # log and retry on the next interval.
                logger.exception("Watcher iteration failed; retrying on next interval.")
                await asyncio.sleep(WATCHER_LAPSED_TIME)
