from __future__ import annotations

import fnmatch
from typing import ClassVar, Literal, Protocol, runtime_checkable

import structlog

from omnivore.pipeline.context import BlobRef, IngestContext
from omnivore.pipeline.models import ExtractionResult

logger = structlog.get_logger(__name__)


@runtime_checkable
class FormatHandler(Protocol):
    name: ClassVar[str]
    version: ClassVar[str]
    accepts: ClassVar[tuple[str, ...]]
    cost_class: ClassVar[Literal["io", "cpu", "gpu"]]
    timeout_seconds: ClassVar[int]

    async def extract(self, blob: BlobRef, ctx: IngestContext) -> ExtractionResult: ...


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, type] = {}

    def register(self, handler_cls: type) -> None:
        self._handlers[handler_cls.name] = handler_cls
        logger.debug("handler.registered", name=handler_cls.name, version=handler_cls.version)

    def discover(self) -> None:
        from importlib.metadata import entry_points

        for ep in entry_points(group="omnivore.handlers"):
            try:
                handler_cls = ep.load()
                self.register(handler_cls)
            except Exception as exc:
                logger.warning("handler.load.failed", entry_point=ep.name, error=str(exc))

    def resolve(self, mime_type: str) -> type | None:
        for handler_cls in self._handlers.values():
            for pattern in handler_cls.accepts:
                if fnmatch.fnmatch(mime_type, pattern):
                    return handler_cls
        return None

    def all_handlers(self) -> list[dict]:
        return [
            {
                "name": h.name,
                "version": h.version,
                "accepts": list(h.accepts),
                "cost_class": h.cost_class,
            }
            for h in self._handlers.values()
        ]


registry = HandlerRegistry()
