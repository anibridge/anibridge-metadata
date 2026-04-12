"""Descriptor resolution with deduplication and caching."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol

from anibridge_metadata.core.descriptors import MetadataDescriptor, parse_descriptor
from anibridge_metadata.core.enums import DescriptorProvider
from anibridge_metadata.models.metadata import (
    CacheState,
    MetadataEnvelope,
    UnifiedMetadata,
)
from anibridge_metadata.services.cache import CacheEntry, CacheLayer, entry_to_envelope
from anibridge_metadata.services.providers.base import (
    ProviderError,
    UpstreamNotFoundError,
    UpstreamResponseError,
)

logger = logging.getLogger(__name__)


class ProviderLookupRegistry(Protocol):
    """Protocol for resolving providers to adapter instances."""

    def get(self, provider: DescriptorProvider):
        """Get the adapter instance for a given provider."""
        ...


class Resolver:
    """Parse `provider:id` and resolve to a metadata envelope.

    Deduplicates in-flight requests so each unique descriptor key has
    at most one upstream call; concurrent callers share the same Future.
    """

    def __init__(
        self,
        *,
        cache: CacheLayer,
        provider_registry: ProviderLookupRegistry,
    ) -> None:
        """Initialize the resolver."""
        self._cache = cache
        self._provider_registry = provider_registry
        self._in_flight: dict[str, asyncio.Future[UnifiedMetadata]] = {}

    async def resolve(
        self,
        *,
        descriptor: str,
        force_refresh: bool = False,
    ) -> MetadataEnvelope:
        """Resolve a single descriptor string to a metadata envelope."""
        parsed = parse_descriptor(descriptor)
        resolved = parsed.parent or parsed
        return await self._resolve(descriptor=resolved, force_refresh=force_refresh)

    async def resolve_many_cached(
        self,
        descriptors: list[str],
    ) -> dict[str, MetadataEnvelope | CacheEntry | None]:
        """Bulk-resolve descriptors from cache using a single pipeline.

        Returns a dict mapping each descriptor string to:
        - MetadataEnvelope for cache hits
        - CacheEntry (with not_found=True) for cached 404s
        - None if not in cache (caller must fetch upstream)
        """
        parsed_map: dict[str, MetadataDescriptor] = {}
        for desc in descriptors:
            p = parse_descriptor(desc)
            parsed_map[desc] = p.parent or p

        keys = [parsed_map[d].key for d in descriptors]
        entries = await self._cache.get_many(keys)

        result: dict[str, MetadataEnvelope | CacheEntry | None] = {}
        for desc in descriptors:
            key = parsed_map[desc].key
            entry = entries.get(key)
            if entry is not None and entry.is_fresh:
                if entry.not_found:
                    result[desc] = entry
                else:
                    result[desc] = entry_to_envelope(entry, source="cache")
            else:
                result[desc] = None
        return result

    async def _resolve(
        self,
        *,
        descriptor: MetadataDescriptor,
        force_refresh: bool,
    ) -> MetadataEnvelope:
        """Core resolution: cache check → deduplicated upstream fetch."""
        key = descriptor.key

        if not force_refresh:
            entry = await self._cache.get(key)
            if entry is not None and entry.is_fresh:
                if entry.not_found:
                    logger.debug("Cache 404 hit: %s", key)
                    raise UpstreamNotFoundError(f"Cached 404 for descriptor '{key}'.")
                logger.debug("Cache hit: %s", key)
                return entry_to_envelope(entry, source="cache")

        logger.info("Fetching upstream: %s", key)
        normalized = await self._fetch_deduplicated(descriptor)
        entry = await self._cache.get(key)
        if entry is not None:
            return entry_to_envelope(entry, source="upstream")
        now = datetime.now(UTC)
        return MetadataEnvelope(
            metadata=normalized,
            cache=CacheState(
                updated_at=now,
                expires_at=now,
                stale=False,
                source="upstream",
            ),
        )

    async def _fetch_deduplicated(
        self, descriptor: MetadataDescriptor
    ) -> UnifiedMetadata:
        """Ensure only one upstream call runs per descriptor key at a time."""
        key = descriptor.key
        existing = self._in_flight.get(key)
        if existing is not None and not existing.done():
            return await asyncio.shield(existing)

        future: asyncio.Future[UnifiedMetadata] = (
            asyncio.get_running_loop().create_future()
        )
        self._in_flight[key] = future
        try:
            normalized = await self._fetch_upstream(descriptor)
            future.set_result(normalized)
            return normalized
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            self._in_flight.pop(key, None)

    async def _fetch_upstream(self, descriptor: MetadataDescriptor) -> UnifiedMetadata:
        """Fetch from the upstream provider and write results back to cache."""
        adapter = self._provider_registry.get(descriptor.provider)
        try:
            raw_payload = await adapter.fetch_raw(descriptor=descriptor)
            normalized = await adapter.normalize(
                descriptor=descriptor, payload=raw_payload
            )
            await self._safe_cache_put(descriptor.key, normalized)
            logger.info(
                "Cached upstream result: %s (%s)",
                descriptor.key,
                normalized.titles.display,
            )
            return normalized
        except UpstreamNotFoundError:
            logger.info("Upstream 404: %s", descriptor.key)
            await self._safe_cache_put_not_found(descriptor.key)
            raise
        except (UpstreamResponseError, ProviderError) as exc:
            logger.warning(
                "Upstream error for %s: %s",
                descriptor.key,
                exc,
            )
            await self._safe_cache_mark_error(descriptor.key, "upstream refresh failed")
            raise

    async def _safe_cache_put(self, key: str, normalized: UnifiedMetadata) -> None:
        """Write to cache, logging failures without raising."""
        try:
            await self._cache.put(key, normalized)
        except Exception:
            logger.warning("Cache write failed for %s", key, exc_info=True)

    async def _safe_cache_put_not_found(self, key: str) -> None:
        """Write 404 marker to cache, logging failures without raising."""
        try:
            await self._cache.put_not_found(key)
        except Exception:
            logger.warning("Cache 404 write failed for %s", key, exc_info=True)

    async def _safe_cache_mark_error(self, key: str, error: str) -> None:
        """Mark cached entry with error, logging failures without raising."""
        try:
            await self._cache.mark_error(key, error)
        except Exception:
            logger.warning("Cache error mark failed for %s", key, exc_info=True)
