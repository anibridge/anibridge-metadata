"""Descriptor parsing and validation helpers."""

import re
from dataclasses import dataclass
from typing import Final

from anibridge.utils.mappings import descriptor_key, parse_mapping_descriptor

from anibridge_metadata.core.enums import DescriptorProvider, EntityType

SEASON_SCOPE_PATTERN = re.compile(r"^s(?P<number>\d+)$", re.IGNORECASE)
ANIDB_SCOPE_PATTERN = re.compile(r"^[A-Z]$")

ANIDB_SCOPE_CODES: Final[frozenset[str]] = frozenset({"R", "S", "C", "T", "P", "O"})

SCOPED_PROVIDERS = {
    DescriptorProvider.ANIDB,
    DescriptorProvider.TMDB_SHOW,
    DescriptorProvider.TVDB_SHOW,
}


class DescriptorValidationError(ValueError):
    """Raised when a metadata descriptor is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class MetadataDescriptor:
    """Validated descriptor components used throughout the service."""

    provider: DescriptorProvider
    provider_id: str
    scope: str | None = None

    @property
    def key(self) -> str:
        """Return the canonical descriptor string."""
        return descriptor_key((self.provider.value, self.provider_id, self.scope))

    @property
    def parent(self) -> MetadataDescriptor | None:
        """Return the parent descriptor when this descriptor is scoped."""
        if self.scope is None:
            return None
        if self.provider in SCOPED_PROVIDERS:
            return MetadataDescriptor(
                provider=self.provider, provider_id=self.provider_id
            )
        return None

    @property
    def requested_entity_type(self) -> EntityType:
        """Return the entity type implied by the descriptor shape."""
        if self.provider in {
            DescriptorProvider.IMDB_MOVIE,
            DescriptorProvider.TMDB_MOVIE,
            DescriptorProvider.TVDB_MOVIE,
        }:
            return EntityType.MOVIE
        return EntityType.SHOW


def parse_descriptor(value: str) -> MetadataDescriptor:
    """Parse and validate a provider descriptor string."""
    try:
        raw_provider, raw_id, raw_scope = parse_mapping_descriptor(value)
        provider = DescriptorProvider(raw_provider)
    except ValueError as exc:
        raise DescriptorValidationError(
            f"Invalid metadata descriptor: {value!r}."
        ) from exc

    provider_id = raw_id.strip()
    if not provider_id:
        raise DescriptorValidationError("Descriptor id segment cannot be empty.")

    scope = raw_scope.strip() if raw_scope else None

    if provider not in SCOPED_PROVIDERS and scope is not None:
        raise DescriptorValidationError(
            f"Provider {provider.value!r} does not accept a descriptor scope."
        )

    if provider in SCOPED_PROVIDERS and scope is not None:
        if provider == DescriptorProvider.ANIDB:
            normalized_scope = scope.upper()
            if ANIDB_SCOPE_PATTERN.fullmatch(normalized_scope) is None:
                raise DescriptorValidationError(
                    "Scoped 'anidb' descriptors must use one-letter episode type "
                    "codes like 'R' for regular or 'S' for special."
                )
            if normalized_scope not in ANIDB_SCOPE_CODES:
                raise DescriptorValidationError(
                    "AniDB scopes must be one of: R, S, C, T, P, O."
                )
            scope = normalized_scope
        else:
            match = SEASON_SCOPE_PATTERN.fullmatch(scope)
            if match is None:
                raise DescriptorValidationError(
                    f"Scoped {provider.value!r} descriptors must use season scopes "
                    "like 's1'."
                )
            if int(match.group("number")) < 0:
                raise DescriptorValidationError("Season scopes must be non-negative.")

    return MetadataDescriptor(provider=provider, provider_id=provider_id, scope=scope)
