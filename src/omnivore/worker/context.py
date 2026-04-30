from __future__ import annotations

import asyncio
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)

_QUEUE_MAP: dict[str, str] = {
    "io": "q:io",
    "cpu": "q:cpu",
    "gpu": "q:gpu",
    "enrich": "q:enrich",
    "write": "q:write",
}
_DEFAULT_QUEUE = "arq:default"


class WorkflowContext(Protocol):
    async def enqueue(self, stage: str, payload: dict) -> str: ...
    async def sleep(self, duration_seconds: float) -> None: ...
    async def complete(self) -> None: ...
    async def fail(self, reason: str) -> None: ...


class ArqWorkflowContext:
    """ARQ-backed WorkflowContext. Swap for a Temporal shim to migrate without touching handler code."""

    def __init__(self, ctx: dict) -> None:
        self._ctx = ctx

    async def enqueue(self, stage: str, payload: dict) -> str:
        queue = _QUEUE_MAP.get(stage, _DEFAULT_QUEUE)
        job = await self._ctx["redis"].enqueue_job(stage, _queue_name=queue, **payload)
        return job.job_id if job is not None else ""

    async def sleep(self, duration_seconds: float) -> None:
        await asyncio.sleep(duration_seconds)

    async def complete(self) -> None:
        logger.info("workflow.complete", job_id=self._ctx.get("job_id"))

    async def fail(self, reason: str) -> None:
        logger.error("workflow.fail", job_id=self._ctx.get("job_id"), reason=reason)
        raise RuntimeError(reason)
