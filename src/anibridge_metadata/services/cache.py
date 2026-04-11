"""Cache orchestration for provider metadata lookups."""

from datetime import UTC, datetime, timedelta
from typing import Protocol

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
    ) -> None:
        """Create a cache service bound to a database session."""
        self._session = session
        self._settings = settings
        self._provider_registry = provider_registry

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
