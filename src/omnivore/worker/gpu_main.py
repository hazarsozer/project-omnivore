from __future__ import annotations

import arq
import structlog
from arq.connections import RedisSettings

from omnivore.config import get_settings
from omnivore.pipeline.registry import registry
from omnivore.worker.tasks import gpu_ingest_dispatch, on_shutdown

logger = structlog.get_logger(__name__)


async def _gpu_on_startup(ctx: dict) -> None:
    get_settings()
    registry.discover()
    # GPU model loading is deferred to first handler invocation — no warmup here.
    logger.info("gpu_worker.startup", handlers=len(registry.all_handlers()))


class GpuWorkerSettings:
    functions = [gpu_ingest_dispatch]
    on_startup = _gpu_on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    queue_name = "arq:gpu"
    max_jobs = 2
    job_timeout = 7200
    keep_result = 86400
    retry_jobs = True
    max_tries = 3


def main() -> None:
    arq.run_worker(GpuWorkerSettings)


if __name__ == "__main__":
    main()
