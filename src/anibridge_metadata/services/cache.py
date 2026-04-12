"""Redis-backed cache layer for provider metadata.

Stores normalized metadata keyed by `provider:id`.  Each provider can
have its own TTL, falling back to the global `cache_ttl_seconds` setting.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import orjson
from redis.asyncio import Redis

from anibridge_metadata.core.config import Settings
from anibridge_metadata.models.metadata import (
    CacheState,
    MetadataEnvelope,
    UnifiedMetadata,
)

logger = logging.getLogger(__name__)

# Redis key prefixes
_NORMALIZED_PREFIX = "meta:norm:"
_NOT_FOUND_PREFIX = "meta:404:"

# Provider key mapping: DescriptorProvider.value → Settings attribute name
_PROVIDER_KEY_MAP: dict[str, str] = {
    "anidb": "anidb",
    "anilist": "anilist",
    "imdb_movie": "imdb",
    "imdb_show": "imdb",
    "mal": "mal",
    "tmdb_movie": "tmdb",
    "tmdb_show": "tmdb",
    "tvdb_movie": "tvdb",
    "tvdb_show": "tvdb",
}


class CacheEntry:
    """In-memory representation of a cached metadata record."""

    def __init__(
        self,
        *,
        normalized: UnifiedMetadata | None,
        stored_at: datetime,
        ttl_seconds: int,
        not_found: bool = False,
        last_error: str | None = None,
    ) -> None:
        """Initialize a cache entry."""
        self.normalized = normalized
        self.stored_at = stored_at
        self.ttl_seconds = ttl_seconds
        self.not_found = not_found
        self.last_error = last_error

    @property
    def expires_at(self) -> datetime:
        """Return the cache expiration time."""
        return self.stored_at + timedelta(seconds=self.ttl_seconds)

    @property
    def is_fresh(self) -> bool:
        """Return whether this entry is still within its TTL window."""
        return self.expires_at > datetime.now(UTC)


class CacheLayer:
    """Redis-backed cache for normalized metadata."""

    def __init__(self, *, redis: Redis, settings: Settings) -> None:
        """Initialize the cache layer."""
        self._redis = redis
        self._settings = settings

    def _ttl_for_descriptor(self, descriptor_key: str) -> int:
        """Resolve TTL for a descriptor using provider-specific overrides."""
        provider = descriptor_key.split(":")[0]
        config_key = _PROVIDER_KEY_MAP.get(provider, provider)
        return self._settings.ttl_for_provider(config_key)

    async def get(self, descriptor_key: str) -> CacheEntry | None:
        """Retrieve a cached entry by descriptor key."""
        try:
            return await self._get(descriptor_key)
        except Exception:
            logger.debug("Cache read failed for %s", descriptor_key, exc_info=True)
            return None

    async def get_many(
        self, descriptor_keys: list[str]
    ) -> dict[str, CacheEntry | None]:
        """Retrieve multiple cached entries in a single Redis pipeline.

        Returns a dict mapping each descriptor key to its CacheEntry,
        or None if not found in cache.
        """
        if not descriptor_keys:
            return {}

        try:
            return await self._get_many(descriptor_keys)
        except Exception:
            logger.debug(
                "Bulk cache read failed for %d keys",
                len(descriptor_keys),
                exc_info=True,
            )
            return {k: None for k in descriptor_keys}

    async def _get_many(
        self, descriptor_keys: list[str]
    ) -> dict[str, CacheEntry | None]:
        """Internal pipelined bulk read."""
        pipe = self._redis.pipeline(transaction=False)
        for key in descriptor_keys:
            pipe.get(_NOT_FOUND_PREFIX + key)
            pipe.get(_NORMALIZED_PREFIX + key)
        raw_results = await pipe.execute()

        result: dict[str, CacheEntry | None] = {}
        for i, key in enumerate(descriptor_keys):
            nf_data = raw_results[i * 2]
            norm_data = raw_results[i * 2 + 1]
            ttl = self._ttl_for_descriptor(key)

            if nf_data is not None:
                parsed = orjson.loads(nf_data)
                result[key] = CacheEntry(
                    normalized=None,
                    stored_at=datetime.fromisoformat(parsed["stored_at"]),
                    ttl_seconds=ttl,
                    not_found=True,
                    last_error=parsed.get("last_error"),
                )
            elif norm_data is not None:
                parsed = orjson.loads(norm_data)
                result[key] = CacheEntry(
                    normalized=UnifiedMetadata.model_validate(parsed["normalized"]),
                    stored_at=datetime.fromisoformat(parsed["stored_at"]),
                    ttl_seconds=ttl,
                    last_error=parsed.get("last_error"),
                )
            else:
                result[key] = None
        return result

    async def _get(self, descriptor_key: str) -> CacheEntry | None:
        """Internal cache read, may raise on connection failure."""
        # Check 404 marker first
        nf_key = _NOT_FOUND_PREFIX + descriptor_key
        nf_data = await self._redis.get(nf_key)
        if nf_data is not None:
            parsed = orjson.loads(nf_data)
            return CacheEntry(
                normalized=None,
                stored_at=datetime.fromisoformat(parsed["stored_at"]),
                ttl_seconds=self._ttl_for_descriptor(descriptor_key),
                not_found=True,
                last_error=parsed.get("last_error"),
            )

        norm_key = _NORMALIZED_PREFIX + descriptor_key
        raw = await self._redis.get(norm_key)
        if raw is None:
            return None

        parsed = orjson.loads(raw)
        return CacheEntry(
            normalized=UnifiedMetadata.model_validate(parsed["normalized"]),
            stored_at=datetime.fromisoformat(parsed["stored_at"]),
            ttl_seconds=self._ttl_for_descriptor(descriptor_key),
            last_error=parsed.get("last_error"),
        )

    async def put(
        self,
        descriptor_key: str,
        normalized: UnifiedMetadata,
    ) -> None:
        """Store normalized metadata in the cache."""
        ttl = self._ttl_for_descriptor(descriptor_key)
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "normalized": normalized.model_dump(mode="json"),
            "stored_at": now.isoformat(),
        }
        norm_key = _NORMALIZED_PREFIX + descriptor_key
        await self._redis.set(norm_key, orjson.dumps(payload), ex=ttl)
        # Clear any previous 404 marker
        await self._redis.delete(_NOT_FOUND_PREFIX + descriptor_key)

    async def put_many(
        self,
        items: list[tuple[str, UnifiedMetadata]],
    ) -> int:
        """Store multiple entries in a single Redis pipeline.

        Returns the number of items successfully queued.  The pipeline
        executes atomically so either all commands in the batch succeed
        or the error propagates.
        """
        if not items:
            return 0
        now = datetime.now(UTC).isoformat()
        pipe = self._redis.pipeline(transaction=False)
        for descriptor_key, normalized in items:
            ttl = self._ttl_for_descriptor(descriptor_key)
            payload = orjson.dumps(
                {
                    "normalized": normalized.model_dump(mode="json"),
                    "stored_at": now,
                }
            )
            pipe.set(_NORMALIZED_PREFIX + descriptor_key, payload, ex=ttl)
            pipe.delete(_NOT_FOUND_PREFIX + descriptor_key)
        await pipe.execute()
        return len(items)

    async def put_not_found(self, descriptor_key: str) -> None:
        """Cache a 404 (not found) marker for a descriptor."""
        ttl = self._ttl_for_descriptor(descriptor_key)
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "stored_at": now.isoformat(),
            "last_error": "upstream returned 404",
        }
        nf_key = _NOT_FOUND_PREFIX + descriptor_key
        await self._redis.set(nf_key, orjson.dumps(payload), ex=ttl)

    async def mark_error(self, descriptor_key: str, error: str) -> None:
        """Update the last_error field on an existing cached entry."""
        try:
            norm_key = _NORMALIZED_PREFIX + descriptor_key
            raw = await self._redis.get(norm_key)
            if raw is None:
                return
            parsed = orjson.loads(raw)
            parsed["last_error"] = error
            ttl = await self._redis.ttl(norm_key)
            if ttl > 0:
                await self._redis.set(norm_key, orjson.dumps(parsed), ex=ttl)
        except Exception:
            logger.debug(
                "Cache error mark failed for %s", descriptor_key, exc_info=True
            )

    async def ping(self) -> None:
        """Verify Redis connectivity."""
        await self._redis.ping()  # ty:ignore[invalid-await]


def entry_to_envelope(
    entry: CacheEntry,
    *,
    source: Literal["cache", "upstream"],
) -> MetadataEnvelope:
    """Convert a CacheEntry into an API response envelope."""
    now = datetime.now(UTC)
    if entry.normalized is None:
        msg = "Cannot build envelope from a not-found cache entry"
        raise ValueError(msg)
    return MetadataEnvelope(
        metadata=entry.normalized,
        cache=CacheState(
            updated_at=entry.stored_at,
            expires_at=entry.expires_at,
            stale=entry.expires_at <= now,
            source=source,
            last_error=entry.last_error,
        ),
    )
