"""Shared Pydantic models for normalized metadata."""

from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from anibridge_metadata.core.enums import (
    DescriptorProvider,
    EntityType,
    ImageType,
    TitleStatus,
)
from anibridge_metadata.models.database import MetadataRecord

type RuntimeBasis = Literal["derived", "provided"]
type RelationshipKind = Literal["twin"]


class MetadataId(BaseModel):
    """Stable provider identity for a metadata record."""

    descriptor: str
    provider: DescriptorProvider
    provider_id: str
    scope: str | None = None


class MetadataTitles(BaseModel):
    """Title variants exposed by the API."""

    display: str
    main: str
    original: str | None = None
    aliases: list[str] = Field(default_factory=list)
    franchise: str | None = None


class MetadataRelease(BaseModel):
    """Release window and lifecycle status."""

    start_date: date | None = None
    end_date: date | None = None
    status: TitleStatus = TitleStatus.UNKNOWN


class MetadataRuntime(BaseModel):
    """Normalized runtime semantics for a title."""

    minutes: int
    basis: RuntimeBasis


class MetadataClassification(BaseModel):
    """Audience and genre classification."""

    is_adult: bool = False
    genres: list[str] = Field(default_factory=list)


class MetadataRatings(BaseModel):
    """Provider ratings and popularity metrics."""

    average: float | None = None
    popularity: float | None = None


class MetadataRelationshipTarget(BaseModel):
    """Identity of a related metadata entry."""

    descriptor: str
    kind: EntityType


class MetadataRelationship(BaseModel):
    """A relationship between two metadata entries."""

    kind: RelationshipKind
    target: MetadataRelationshipTarget


class MetadataScope(BaseModel):
    """A scope (e.g. season) within a show entry."""

    id: MetadataId
    titles: MetadataTitles
    release: MetadataRelease | None = None
    runtime: MetadataRuntime | None = None
    units: int | None = None


class MetadataImageModel(BaseModel):
    """Normalized image metadata returned by the API."""

    kind: ImageType = ImageType.UNKNOWN
    url: str


class UnifiedMetadata(BaseModel):
    """Provider-agnostic metadata payload keyed by descriptor."""

    model_config = ConfigDict(use_enum_values=False)

    kind: EntityType
    id: MetadataId
    titles: MetadataTitles
    synopsis: str | None = None
    release: MetadataRelease | None = None
    runtime: MetadataRuntime | None = None
    units: int | None = None
    classification: MetadataClassification = Field(
        default_factory=MetadataClassification
    )
    ratings: MetadataRatings | None = None
    images: list[MetadataImageModel] = Field(default_factory=list)
    scopes: dict[str, MetadataScope] | None = None
    relationships: list[MetadataRelationship] = Field(default_factory=list)
    source: str | None = None


def build_metadata_id(
    *,
    descriptor: str,
    provider: DescriptorProvider,
    provider_id: str,
    scope: str | None = None,
) -> MetadataId:
    """Build a stable metadata id object."""
    return MetadataId(
        descriptor=descriptor,
        provider=provider,
        provider_id=provider_id,
        scope=scope,
    )


def build_titles(
    *,
    display: str,
    main: str | None = None,
    original: str | None = None,
    aliases: list[str] | None = None,
    franchise: str | None = None,
) -> MetadataTitles:
    """Build normalized title data."""
    return MetadataTitles(
        display=display,
        main=main or display,
        original=original,
        aliases=aliases or [],
        franchise=franchise,
    )


def build_release(
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    status: TitleStatus | None = None,
) -> MetadataRelease | None:
    """Build release metadata when any release fields exist."""
    if start_date is None and end_date is None and status is None:
        return None
    return MetadataRelease(
        start_date=start_date,
        end_date=end_date,
        status=status or TitleStatus.UNKNOWN,
    )


def build_runtime(
    *, minutes: int | None, basis: RuntimeBasis | None
) -> MetadataRuntime | None:
    """Build runtime metadata when runtime is known."""
    if minutes is None or basis is None:
        return None
    return MetadataRuntime(minutes=minutes, basis=basis)


def build_classification(
    *, is_adult: bool = False, genres: list[str] | None = None
) -> MetadataClassification:
    """Build classification metadata."""
    return MetadataClassification(is_adult=is_adult, genres=genres or [])


def build_ratings(
    *, average: float | None = None, popularity: float | None = None
) -> MetadataRatings | None:
    """Build ratings metadata when any ratings are known."""
    if average is None and popularity is None:
        return None
    return MetadataRatings(average=average, popularity=popularity)


def build_source(*, url: str | None) -> str | None:
    """Return the source URL when present."""
    return url or None


def build_relationship(*, descriptor: str, kind: EntityType) -> MetadataRelationship:
    """Build a twin relationship entry."""
    return MetadataRelationship(
        kind="twin",
        target=MetadataRelationshipTarget(
            descriptor=descriptor,
            kind=kind,
        ),
    )


class CacheState(BaseModel):
    """Cache metadata returned alongside normalized title data."""

    fetched_at: datetime
    expires_at: datetime
    stale: bool
    source: Literal["cache", "stale-cache", "upstream"]
    last_error: str | None = None


class MetadataEnvelope(BaseModel):
    """API response envelope for a descriptor lookup."""

    metadata: UnifiedMetadata
    cache: CacheState


def ensure_utc(value: datetime) -> datetime:
    """Normalize persisted datetimes to timezone-aware UTC values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def record_to_metadata(record: MetadataRecord) -> UnifiedMetadata:
    """Convert a persisted metadata record into the shared response model."""
    return UnifiedMetadata.model_validate(record.normalized_payload)


def record_to_envelope(
    record: MetadataRecord,
    *,
    source: Literal["cache", "stale-cache", "upstream"],
) -> MetadataEnvelope:
    """Convert a persisted metadata record into an API response envelope."""
    now = datetime.now(UTC)
    fetched_at = ensure_utc(record.fetched_at)
    expires_at = ensure_utc(record.expires_at)
    return MetadataEnvelope(
        metadata=record_to_metadata(record),
        cache=CacheState(
            fetched_at=fetched_at,
            expires_at=expires_at,
            stale=expires_at <= now,
            source=source,
            last_error=record.last_error,
        ),
    )
