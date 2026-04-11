"""Cache orchestration for provider metadata lookups."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from anibridge_metadata.core.config import Settings
from anibridge_metadata.core.descriptors import MetadataDescriptor, parse_descriptor
from anibridge_metadata.core.enums import DescriptorProvider
from anibridge_metadata.models.database import MetadataRecord
from anibridge_metadata.models.metadata import (
    MetadataEnvelope,
    UnifiedMetadata,
    ensure_utc,
    record_to_envelope,
)
from anibridge_metadata.services.providers.base import (
    ProviderConfigurationError,
    ProviderError,
    UpstreamNotFoundError,
    UpstreamResponseError,
)

if TYPE_CHECKING:
    from anibridge_metadata.services.revalidator import BackgroundRevalidator

LOGGER = logging.getLogger(__name__)


class ProviderLookupRegistry(Protocol):
    """Protocol for resolving providers to adapter instances."""

    def get(self, provider: DescriptorProvider):
        """Return an adapter for the given provider."""


class CacheService:
    """Coordinate cached lookups and upstream refreshes for providers."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        provider_registry: ProviderLookupRegistry,
        revalidator: BackgroundRevalidator | None = None,
    ) -> None:
        """Create a cache service bound to a database session."""
        self._session = session
        self._settings = settings
        self._provider_registry = provider_registry
        self._revalidator = revalidator

    async def get_metadata(
        self,
        *,
        descriptor: str,
        force_refresh: bool = False,
    ) -> MetadataEnvelope:
        """Return normalized metadata, fetching upstream when cache is stale."""
        parsed = parse_descriptor(descriptor)
        resolved = parsed.parent or parsed
        return await self._get_metadata(
            descriptor=resolved,
            force_refresh=force_refresh,
        )

    async def _get_metadata(
        self,
        *,
        descriptor: MetadataDescriptor,
        force_refresh: bool,
    ) -> MetadataEnvelope:
        """Return normalized metadata for a validated descriptor."""
        record = await self._load_record(descriptor=descriptor)
        if record and self._is_fresh(record) and not force_refresh:
            return record_to_envelope(
                record,
                source="cache",
                cache_ttl_seconds=self._settings.cache_ttl_seconds,
            )

        # Stale-while-revalidate: when we have stale data and a background
        # revalidator is available, try to refresh within a short timeout.
        # If the refresh doesn't complete in time, return the stale entry
        # while the background task keeps running.
        if record is not None and not force_refresh and self._revalidator is not None:
            return await self._stale_while_revalidate(
                descriptor=descriptor,
                record=record,
            )

        # No stale data to fall back on (or force_refresh requested) —
        # perform a blocking upstream fetch in the request session.
        return await self._blocking_refresh(
            descriptor=descriptor,
            record=record,
        )

    async def _stale_while_revalidate(
        self,
        *,
        descriptor: MetadataDescriptor,
        record: MetadataRecord,
    ) -> MetadataEnvelope:
        """Wait briefly for a background refresh, falling back to stale data."""
        assert self._revalidator is not None
        task = self._revalidator.schedule(descriptor)
        try:
            # Shield the task so a timeout does not cancel the background work.
            succeeded = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._settings.stale_timeout_seconds,
            )
        except TimeoutError:
            LOGGER.debug(
                "Stale-while-revalidate timeout for %s; returning stale data.",
                descriptor.key,
            )
            return record_to_envelope(
                record,
                source="stale-cache",
                cache_ttl_seconds=self._settings.cache_ttl_seconds,
            )
        except Exception:
            LOGGER.debug(
                "Background revalidation raised for %s; returning stale data.",
                descriptor.key,
                exc_info=True,
            )
            return record_to_envelope(
                record,
                source="stale-cache",
                cache_ttl_seconds=self._settings.cache_ttl_seconds,
            )

        if not succeeded:
            return record_to_envelope(
                record,
                source="stale-cache",
                cache_ttl_seconds=self._settings.cache_ttl_seconds,
            )

        # The background task committed via its own session.  Expire any
        # cached ORM state so the next SELECT sees the fresh row.
        self._session.expire_all()
        refreshed = await self._load_record(descriptor=descriptor)
        if refreshed is not None:
            return record_to_envelope(
                refreshed,
                source="upstream",
                cache_ttl_seconds=self._settings.cache_ttl_seconds,
            )
        return record_to_envelope(
            record,
            source="stale-cache",
            cache_ttl_seconds=self._settings.cache_ttl_seconds,
        )

    async def _blocking_refresh(
        self,
        *,
        descriptor: MetadataDescriptor,
        record: MetadataRecord | None,
    ) -> MetadataEnvelope:
        """Fetch upstream data synchronously within the request session."""
        adapter = self._provider_registry.get(descriptor.provider)

        try:
            raw_payload = await adapter.fetch_raw(descriptor=descriptor)
            normalized = await adapter.normalize(
                descriptor=descriptor,
                payload=raw_payload,
            )
            await self._upsert_record(
                record=record,
                descriptor=descriptor,
                metadata=normalized,
            )
            await self._session.commit()
            refreshed = await self._load_record(descriptor=descriptor)
            if refreshed is None:
                raise UpstreamResponseError(
                    "Metadata refresh completed but no record was persisted."
                )
            return record_to_envelope(
                refreshed,
                source="upstream",
                cache_ttl_seconds=self._settings.cache_ttl_seconds,
            )
        except (
            ProviderConfigurationError,
            UpstreamNotFoundError,
            UpstreamResponseError,
            ProviderError,
        ):
            if record is not None:
                record.last_error = "upstream refresh failed"
                await self._session.commit()
                return record_to_envelope(
                    record,
                    source="stale-cache",
                    cache_ttl_seconds=self._settings.cache_ttl_seconds,
                )
            raise

    async def _load_record(
        self,
        *,
        descriptor: MetadataDescriptor,
    ) -> MetadataRecord | None:
        """Load a metadata record with its child collections."""
        statement = select(MetadataRecord).where(
            MetadataRecord.descriptor == descriptor.key
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    def _is_fresh(self, record: MetadataRecord) -> bool:
        """Return whether a cached record is still valid."""
        expires_at = ensure_utc(record.updated_at) + timedelta(
            seconds=self._settings.cache_ttl_seconds
        )
        return expires_at > datetime.now(UTC)

    async def _upsert_record(
        self,
        *,
        record: MetadataRecord | None,
        descriptor: MetadataDescriptor,
        metadata: UnifiedMetadata,
    ) -> None:
        """Create or update a persisted record from normalized metadata."""
        if record is None:
            record = MetadataRecord(
                descriptor=metadata.id.descriptor,
                normalized_payload=metadata.model_dump(mode="json"),
            )
            self._session.add(record)

        record.descriptor = descriptor.key
        record.normalized_payload = metadata.model_dump(mode="json")
        record.last_error = None
        await self._session.flush()
