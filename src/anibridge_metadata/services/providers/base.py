"""Base provider adapter contracts and shared helpers."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import date
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from anibridge_metadata.core.config import Settings
from anibridge_metadata.core.descriptors import MetadataDescriptor
from anibridge_metadata.models.metadata import UnifiedMetadata
from anibridge_metadata.utils.http import HttpClient

type ProviderPayload = BaseModel | dict[str, Any] | list[Any] | str
type RuntimeBasisValue = Literal["provided", "derived"]

MappedValue = TypeVar("MappedValue")


class ProviderError(RuntimeError):
    """Base exception for provider integration failures."""


class ProviderConfigurationError(ProviderError):
    """Raised when a provider cannot be used with current settings."""


class UpstreamNotFoundError(ProviderError):
    """Raised when a provider does not know the requested descriptor."""


class UpstreamResponseError(ProviderError):
    """Raised when an upstream provider returns an unexpected response."""


class ProviderAdapter(ABC):
    """Abstract contract implemented by every upstream provider adapter."""

    def __init__(self, *, settings: Settings, http_client: HttpClient) -> None:
        """Store shared provider dependencies."""
        self.settings = settings
        self.http_client = http_client

    @abstractmethod
    async def fetch_raw(
        self,
        *,
        descriptor: MetadataDescriptor,
    ) -> ProviderPayload:
        """Fetch raw provider data for a descriptor."""

    @abstractmethod
    async def normalize(
        self,
        *,
        descriptor: MetadataDescriptor,
        payload: ProviderPayload,
    ) -> UnifiedMetadata:
        """Normalize provider-specific data into the shared metadata model."""

    async def start(self) -> None:
        """Start provider-specific background resources."""
        return None

    async def close(self) -> None:
        """Close provider-specific background resources."""
        return None

    def require(self, value: str | int | None, message: str) -> str | int:
        """Validate a required setting value."""
        if value in (None, ""):
            raise ProviderConfigurationError(message)
        return value

    @staticmethod
    def coerce_date(value: str | None) -> date | None:
        """Parse an ISO-style date string when possible."""
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def dedupe(values: Sequence[str | None]) -> list[str]:
        """Return unique non-empty values while preserving insertion order."""
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value:
                continue
            cleaned = value.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            result.append(cleaned)
        return result

    @staticmethod
    def first_non_empty(*values: str | None) -> str | None:
        """Return the first non-empty value from the candidates."""
        for value in values:
            if value:
                cleaned = value.strip()
                if cleaned:
                    return cleaned
        return None

    @staticmethod
    def map_value(
        value: str | None,
        mapping: Mapping[str, MappedValue],
        *,
        default: MappedValue,
    ) -> MappedValue:
        """Map a raw string value through a dict with a fallback default."""
        if value is None:
            return default
        return mapping.get(value, default)


class BatchableProvider(ABC):
    """Mixin for provider adapters that support full-catalog batch refresh.

    Providers implementing this interface can efficiently populate the cache
    for all known IDs by streaming normalized metadata without hitting the
    per-descriptor `fetch_raw` / `normalize` round-trip for each entry.
    """

    @abstractmethod
    def iter_all_normalized(self) -> AsyncIterator[tuple[str, UnifiedMetadata]]:
        """Yield `(descriptor_key, normalized_metadata)` for every known item."""
