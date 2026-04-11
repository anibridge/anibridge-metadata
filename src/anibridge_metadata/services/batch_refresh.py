"""Scheduled batch cache refresh for providers that support full-catalog fetching."""

import asyncio
import logging
from collections.abc import Mapping
from contextlib import nullcontext, suppress
from datetime import UTC, datetime

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from anibridge_metadata.core.config import Settings
from anibridge_metadata.models.database import MetadataRecord
from anibridge_metadata.models.metadata import UnifiedMetadata
from anibridge_metadata.services.providers.base import BatchableProvider

LOGGER = logging.getLogger(__name__)

# Flush pending DB writes every N normalized records to bound memory usage.
_DB_COMMIT_EVERY = 200


class BatchRefreshService:
    """Coordinate full-catalog cache refreshes for batchable providers.

    Each registered provider is expected to implement
    :class:`~anibridge_metadata.services.providers.base.BatchableProvider`.
    The service streams normalized metadata from the provider and upserts
    each record into the shared cache database.

    A background task started by :meth:`start` drives the refresh on the
    schedule defined by the application settings.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        providers: Mapping[str, BatchableProvider],
    ) -> None:
        """Create the service with a set of named batchable providers."""
        self._session_factory = session_factory
        self._settings = settings
        self._providers = dict(providers)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Launch the background scheduling loop."""
        if not self._settings.batch_refresh.enabled:
            LOGGER.info("Batch refresh is disabled via settings; not starting.")
            return
        if self._task is None:
            self._task = asyncio.create_task(
                self._scheduled_loop(),
                name="batch-refresh-scheduler",
            )

    async def close(self) -> None:
        """Cancel the background scheduling loop."""
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def refresh_all(self) -> None:
        """Run a full batch refresh for every registered provider concurrently."""
        names = list(self._providers.keys())
        write_lock = asyncio.Lock()
        LOGGER.info("Batch refresh: starting %d providers concurrently.", len(names))
        results = await asyncio.gather(
            *(
                self._refresh_provider(name, provider, write_lock)
                for name, provider in self._providers.items()
            ),
            return_exceptions=True,
        )
        for name, result in zip(names, results, strict=False):
            if isinstance(result, BaseException):
                LOGGER.exception(
                    "Batch refresh: unhandled error for provider '%s'.",
                    name,
                    exc_info=result,
                )
            else:
                count, errors = result
                LOGGER.info(
                    "Batch refresh: provider '%s' complete: %d upserted, %d errors.",
                    name,
                    count,
                    errors,
                )

    async def _refresh_provider(
        self,
        name: str,
        provider: BatchableProvider,
        write_lock: asyncio.Lock | None = None,
    ) -> tuple[int, int]:
        """Stream and persist all items from a single provider.

        Returns a `(upserted, errors)` pair.
        """
        LOGGER.info("Batch refresh: starting provider '%s'.", name)
        upserted = 0
        errors = 0
        batch: list[tuple[str, UnifiedMetadata]] = []
        _lock = write_lock if write_lock is not None else nullcontext()

        async def flush(session: AsyncSession) -> None:
            nonlocal upserted
            async with _lock:
                for descriptor_key, normalized in batch:
                    await self._upsert(session, descriptor_key, normalized)
                    upserted += 1
                await session.commit()
            batch.clear()

        async with self._session_factory() as session:
            async for descriptor_key, normalized in provider.iter_all_normalized():
                try:
                    batch.append((descriptor_key, normalized))
                except Exception as exc:
                    errors += 1
                    LOGGER.warning(
                        "Batch refresh [%s]: error processing %s: %s",
                        name,
                        descriptor_key,
                        exc,
                    )
                    continue

                if len(batch) >= _DB_COMMIT_EVERY:
                    await flush(session)

            if batch:
                await flush(session)

        return upserted, errors

    async def _upsert(
        self,
        session: AsyncSession,
        descriptor_key: str,
        normalized: UnifiedMetadata,
    ) -> None:
        """Create or update a cache record for the given descriptor."""
        result = await session.execute(
            select(MetadataRecord).where(MetadataRecord.descriptor == descriptor_key)
        )
        record = result.scalar_one_or_none()

        if record is None:
            record = MetadataRecord(
                descriptor=descriptor_key,
                normalized_payload=normalized.model_dump(mode="json"),
            )
            session.add(record)
        else:
            record.normalized_payload = normalized.model_dump(mode="json")
            record.last_error = None

        await session.flush()

    async def _scheduled_loop(self) -> None:
        """Sleep until the next cron-scheduled run then loop forever."""
        while True:
            delay = self._seconds_until_next_run()
            LOGGER.info(
                "Batch refresh: next run in %.0f seconds (cron: %s).",
                delay,
                self._settings.batch_refresh.cron,
            )
            await asyncio.sleep(delay)
            try:
                await self.refresh_all()
            except Exception:
                LOGGER.exception("Batch refresh: scheduled run failed.")

    def _seconds_until_next_run(self) -> float:
        """Return seconds until the next fire time according to the cron expression."""
        now = datetime.now(UTC)
        cron = croniter(self._settings.batch_refresh.cron, now)
        next_dt: datetime = cron.get_next(datetime)
        return max((next_dt - now).total_seconds(), 0.0)
