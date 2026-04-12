"""Scheduled full-catalog batch refresh service.

Periodically iterates every :class:`BatchableProvider` registered in the
system, streaming all known items into the Redis-backed :class:`CacheLayer`
so that cache hits are available *before* a user asks for them.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from croniter import croniter

from anibridge_metadata.core.config import BatchRefreshConfig
from anibridge_metadata.models.metadata import UnifiedMetadata
from anibridge_metadata.services.cache import CacheLayer
from anibridge_metadata.services.providers.base import BatchableProvider

logger = logging.getLogger(__name__)

_PIPELINE_SIZE = 500


class BatchRefreshService:
    """Cron-driven cache pre-population from batchable providers."""

    def __init__(
        self,
        *,
        config: BatchRefreshConfig,
        cache: CacheLayer,
        providers: dict[str, BatchableProvider],
    ) -> None:
        """Initialize the batch refresh service."""
        self._config = config
        self._cache = cache
        self._providers = providers
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Begin the background cron loop (if enabled)."""
        if not self._config.enabled:
            logger.info("Batch refresh disabled.")
            return
        self._task = asyncio.create_task(self._loop(), name="batch-refresh")
        logger.info("Batch refresh scheduled (cron=%s).", self._config.cron)

    async def close(self) -> None:
        """Cancel the background loop, if running."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            logger.info("Batch refresh stopped.")

    async def _loop(self) -> None:
        if self._config.refresh_on_startup:
            await self._refresh_all()

        cron = croniter(self._config.cron, datetime.now(UTC))
        while True:
            next_dt: datetime = cron.get_next(datetime)
            delay = (next_dt - datetime.now(UTC)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            await self._refresh_all()

    async def _refresh_all(self) -> None:
        logger.info(
            "Batch refresh starting (%d provider(s)).",
            len(self._providers),
        )
        for name, provider in self._providers.items():
            await self._refresh_provider(name, provider)
        logger.info("Batch refresh complete.")

    async def _refresh_provider(
        self,
        name: str,
        provider: BatchableProvider,
    ) -> None:
        count = 0
        errors = 0
        buffer: list[tuple[str, UnifiedMetadata]] = []

        async def _flush() -> None:
            nonlocal count, errors
            if not buffer:
                return
            try:
                written = await self._cache.put_many(buffer)
                count += written
            except Exception:
                errors += len(buffer)
                logger.debug(
                    "Pipeline write failed (%d items)",
                    len(buffer),
                    exc_info=True,
                )
            buffer.clear()

        try:
            async for descriptor_key, normalized in provider.iter_all_normalized():
                buffer.append((descriptor_key, normalized))
                if len(buffer) >= _PIPELINE_SIZE:
                    await _flush()
            await _flush()
        except Exception:
            logger.exception("Batch refresh failed for provider %s", name)

        logger.info(
            "Provider %s: cached %d, errors %d.",
            name,
            count,
            errors,
        )
