"""Batch collector for concurrent descriptor resolution."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from anibridge_metadata.core.descriptors import DescriptorValidationError
from anibridge_metadata.models.metadata import MetadataEnvelope
from anibridge_metadata.services.cache import CacheEntry
from anibridge_metadata.services.providers.base import (
    ProviderConfigurationError,
    UpstreamNotFoundError,
    UpstreamResponseError,
)

logger = logging.getLogger(__name__)

# How many keys to read from Redis in a single pipeline batch.
_CACHE_PIPELINE_CHUNK = 1000
# Max concurrent upstream fetches for cache misses.
_UPSTREAM_CONCURRENCY = 20


class MetadataResolver(Protocol):
    """Protocol for resolving descriptors to metadata envelopes."""

    async def resolve(
        self,
        *,
        descriptor: str,
        force_refresh: bool = ...,
    ) -> MetadataEnvelope:
        """Resolve a descriptor to a metadata envelope."""
        ...

    async def resolve_many_cached(
        self,
        descriptors: list[str],
    ) -> dict[str, MetadataEnvelope | CacheEntry | None]:
        """Bulk-resolve descriptors from cache."""
        ...


@dataclass
class BatchResult:
    """Result for a single descriptor in a batch."""

    descriptor: str
    envelope: MetadataEnvelope | None = None
    error: str | None = None
    status_code: int = 200


class BatchCollector:
    """Resolve descriptors concurrently and stream results."""

    def __init__(self, *, resolver: MetadataResolver) -> None:
        """Initialize the batch collector."""
        self._resolver = resolver

    async def stream(
        self,
        descriptors: list[str],
    ) -> AsyncIterator[BatchResult]:
        """Yield results as each descriptor resolves."""
        seen: set[str] = set()
        unique: list[str] = []
        for d in descriptors:
            if d not in seen:
                seen.add(d)
                unique.append(d)

        misses: list[str] = []
        for i in range(0, len(unique), _CACHE_PIPELINE_CHUNK):
            chunk = unique[i : i + _CACHE_PIPELINE_CHUNK]
            cached = await self._resolver.resolve_many_cached(chunk)
            for desc in chunk:
                value = cached.get(desc)
                if isinstance(value, MetadataEnvelope):
                    yield BatchResult(descriptor=desc, envelope=value)
                elif isinstance(value, CacheEntry) and value.not_found:
                    yield BatchResult(
                        descriptor=desc,
                        error=f"Cached 404 for descriptor '{desc}'.",
                        status_code=404,
                    )
                else:
                    misses.append(desc)

        if not misses:
            return

        logger.info(
            "Batch: %d cache hits, %d misses to fetch upstream.",
            len(unique) - len(misses),
            len(misses),
        )

        result_queue: asyncio.Queue[BatchResult] = asyncio.Queue()
        miss_iter = iter(misses)

        async def _worker(worker_id: int) -> None:
            for desc in miss_iter:
                result = await self._resolve_one(desc)
                await result_queue.put(result)

        worker_count = min(_UPSTREAM_CONCURRENCY, len(misses))
        workers = [
            asyncio.create_task(_worker(i), name=f"batch-worker:{i}")
            for i in range(worker_count)
        ]

        try:
            for _ in range(len(misses)):
                yield await result_queue.get()
        finally:
            for worker in workers:
                worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*workers)

    async def collect(
        self,
        descriptors: list[str],
    ) -> list[BatchResult]:
        """Resolve all descriptors and return results as a list."""
        results: list[BatchResult] = []
        async for result in self.stream(descriptors):
            results.append(result)
        return results

    async def _resolve_one(self, descriptor: str) -> BatchResult:
        """Resolve a single descriptor, capturing any errors."""
        try:
            envelope = await self._resolver.resolve(descriptor=descriptor)
            return BatchResult(descriptor=descriptor, envelope=envelope)
        except DescriptorValidationError as exc:
            return BatchResult(
                descriptor=descriptor,
                error=str(exc),
                status_code=422,
            )
        except ProviderConfigurationError as exc:
            return BatchResult(
                descriptor=descriptor,
                error=str(exc),
                status_code=503,
            )
        except UpstreamNotFoundError as exc:
            return BatchResult(
                descriptor=descriptor,
                error=str(exc),
                status_code=404,
            )
        except UpstreamResponseError as exc:
            return BatchResult(
                descriptor=descriptor,
                error=str(exc),
                status_code=502,
            )
        except Exception:
            logger.exception("Unexpected error resolving %s", descriptor)
            return BatchResult(
                descriptor=descriptor,
                error="Internal server error",
                status_code=500,
            )
