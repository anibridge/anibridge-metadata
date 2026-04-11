"""Background revalidation for stale cache entries."""


import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from anibridge_metadata.core.config import Settings
from anibridge_metadata.core.descriptors import MetadataDescriptor
from anibridge_metadata.models.database import MetadataRecord
from anibridge_metadata.models.metadata import UnifiedMetadata
from anibridge_metadata.services.providers.base import ProviderError

if TYPE_CHECKING:
    from anibridge_metadata.services.cache import ProviderLookupRegistry

LOGGER = logging.getLogger(__name__)


class BackgroundRevalidator:
    """Manage background upstream refresh tasks for stale cache entries.

    When a cache entry is stale, callers can :meth:`schedule` a background
    refresh and optionally wait a bounded amount of time for it to finish.
    If the upstream fetch doesn't complete within the timeout the caller
    returns the stale data and the task keeps running in the background
    so that the next request sees fresh data.

    Duplicate refreshes for the same descriptor are coalesced. Only one
    task runs per descriptor key at a time.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        provider_registry: ProviderLookupRegistry,
    ) -> None:
        """Create a background revalidator with the given dependencies."""
        self._session_factory = session_factory
        self._settings = settings
        self._provider_registry = provider_registry
        self._in_flight: dict[str, asyncio.Task[bool]] = {}

    def schedule(self, descriptor: MetadataDescriptor) -> asyncio.Task[bool]:
        """Start or return an existing background refresh for *descriptor*.

        Returns an :class:`asyncio.Task` whose result is ``True`` when the
        upstream refresh succeeded.  The task may be awaited with a timeout
        via :func:`asyncio.wait_for` (wrapped in :func:`asyncio.shield` to
        avoid cancelling the background work on timeout).
        """
        key = descriptor.key
        existing = self._in_flight.get(key)
        if existing is not None and not existing.done():
            return existing

        task: asyncio.Task[bool] = asyncio.create_task(
            self._refresh(descriptor),
            name=f"revalidate:{key}",
        )
        self._in_flight[key] = task
        task.add_done_callback(lambda _t: self._in_flight.pop(key, None))
        return task

    async def close(self) -> None:
        """Cancel all in-flight background tasks."""
        tasks = list(self._in_flight.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._in_flight.clear()

    async def _refresh(self, descriptor: MetadataDescriptor) -> bool:
        """Fetch upstream data and persist the result in its own session."""
        adapter = self._provider_registry.get(descriptor.provider)
        try:
            raw_payload = await adapter.fetch_raw(descriptor=descriptor)
            normalized: UnifiedMetadata = await adapter.normalize(
                descriptor=descriptor,
                payload=raw_payload,
            )
        except ProviderError:
            LOGGER.debug(
                "Background revalidation failed for %s", descriptor.key, exc_info=True
            )
            return False
        except Exception:
            LOGGER.exception(
                "Unexpected error during background revalidation for %s",
                descriptor.key,
            )
            return False

        try:
            async with self._session_factory() as session:
                await self._upsert(session, descriptor, normalized)
                await session.commit()
        except Exception:
            LOGGER.exception(
                "Background revalidation DB write failed for %s", descriptor.key
            )
            return False

        LOGGER.debug("Background revalidation succeeded for %s", descriptor.key)
        return True

    @staticmethod
    async def _upsert(
        session: AsyncSession,
        descriptor: MetadataDescriptor,
        metadata: UnifiedMetadata,
    ) -> None:
        """Insert or update a metadata record for descriptor with metadata."""
        result = await session.execute(
            select(MetadataRecord).where(MetadataRecord.descriptor == descriptor.key)
        )
        record = result.scalar_one_or_none()

        if record is None:
            record = MetadataRecord(
                descriptor=descriptor.key,
                normalized_payload=metadata.model_dump(mode="json"),
            )
            session.add(record)
        else:
            record.normalized_payload = metadata.model_dump(mode="json")
            record.last_error = None
        await session.flush()
