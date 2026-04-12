"""AniList provider adapter."""

import logging
from collections.abc import AsyncGenerator
from datetime import date
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from anibridge_metadata.core.descriptors import MetadataDescriptor
from anibridge_metadata.core.enums import (
    DescriptorProvider,
    EntityType,
    ImageType,
    TitleStatus,
)
from anibridge_metadata.models.metadata import (
    MetadataImageModel,
    UnifiedMetadata,
    build_classification,
    build_metadata_id,
    build_ratings,
    build_release,
    build_runtime,
    build_source,
    build_titles,
)
from anibridge_metadata.services.providers.base import (
    BatchableProvider,
    ProviderAdapter,
    ProviderPayload,
    UpstreamNotFoundError,
    UpstreamResponseError,
)
from anibridge_metadata.utils.http import HttpClientError

LOGGER = logging.getLogger(__name__)


class AnilistFuzzyDatePayload(BaseModel):
    """Fuzzy AniList date payload."""

    model_config = ConfigDict(extra="ignore")

    year: int | None = None
    month: int | None = None
    day: int | None = None


class AnilistTitlePayload(BaseModel):
    """AniList title variants."""

    model_config = ConfigDict(extra="ignore")

    romaji: str | None = None
    english: str | None = None
    native: str | None = None


class AnilistCoverImagePayload(BaseModel):
    """AniList cover image payload."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    extra_large: str | None = Field(default=None, alias="extraLarge")
    large: str | None = None
    medium: str | None = None


class AnilistMediaPayload(BaseModel):
    """Validated AniList media payload."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: int
    title: AnilistTitlePayload = Field(default_factory=AnilistTitlePayload)
    synonyms: list[str] = Field(default_factory=list)
    description: str | None = None
    status: str | None = None
    format: str | None = None
    episodes: int | None = None
    duration: int | None = None
    average_score: int | None = Field(default=None, alias="averageScore")
    popularity: int | None = None
    is_adult: bool = Field(default=False, alias="isAdult")
    genres: list[str] = Field(default_factory=list)
    start_date: AnilistFuzzyDatePayload = Field(
        default_factory=AnilistFuzzyDatePayload,
        alias="startDate",
    )
    end_date: AnilistFuzzyDatePayload = Field(
        default_factory=AnilistFuzzyDatePayload,
        alias="endDate",
    )
    cover_image: AnilistCoverImagePayload = Field(
        default_factory=AnilistCoverImagePayload,
        alias="coverImage",
    )
    banner_image: str | None = Field(default=None, alias="bannerImage")


class AnilistGraphQLDataPayload(BaseModel):
    """AniList GraphQL data envelope."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    media: AnilistMediaPayload | None = Field(default=None, alias="Media")


class AnilistGraphQLResponse(BaseModel):
    """AniList GraphQL response envelope."""

    model_config = ConfigDict(extra="ignore")

    data: AnilistGraphQLDataPayload = Field(default_factory=AnilistGraphQLDataPayload)


class AnilistAdapter(ProviderAdapter, BatchableProvider):
    """Retrieve and normalize AniList anime metadata."""

    BASE_URL: ClassVar[str] = "https://graphql.anilist.co"

    BATCH_PER_PAGE: ClassVar[int] = 50
    BATCH_PAGES_PER_REQUEST: ClassVar[int] = 15

    # Full field set used for the paginated batch refresh query.
    _BATCH_MEDIA_FIELDS: ClassVar[str] = """
        id
        title { romaji english native }
        synonyms
        description(asHtml: false)
        status
        format
        episodes
        duration
        averageScore
        popularity
        isAdult
        genres
        startDate { year month day }
        endDate { year month day }
        coverImage { extraLarge large medium }
        bannerImage
    """

    STATUS_MAP: ClassVar[dict[str, TitleStatus]] = {
        "CANCELLED": TitleStatus.CANCELLED,
        "FINISHED": TitleStatus.FINISHED,
        "HIATUS": TitleStatus.HIATUS,
        "NOT_YET_RELEASED": TitleStatus.UPCOMING,
        "RELEASING": TitleStatus.ONGOING,
    }
    MEDIA_QUERY: ClassVar[str] = f"""
        query ($id: Int) {{
            Media(id: $id, type: ANIME) {{ {_BATCH_MEDIA_FIELDS} }}
        }}
    """

    async def fetch_raw(self, *, descriptor: MetadataDescriptor) -> ProviderPayload:
        """Fetch AniList media data through its GraphQL API."""
        try:
            response_payload = await self.http_client.post_json(
                self.BASE_URL,
                json_body={
                    "query": self.MEDIA_QUERY,
                    "variables": {"id": int(descriptor.provider_id)},
                },
            )
        except ValueError as exc:
            raise UpstreamResponseError("AniList id must be numeric.") from exc
        except HttpClientError as exc:
            if exc.status_code == 404:
                raise UpstreamNotFoundError(
                    "AniList did not find the requested title."
                ) from exc
            raise UpstreamResponseError(str(exc)) from exc

        try:
            payload = AnilistGraphQLResponse.model_validate(response_payload)
        except ValidationError as exc:
            raise UpstreamResponseError("AniList response validation failed.") from exc

        if payload.data.media is None:
            raise UpstreamNotFoundError("AniList did not return a media object.")
        return payload.data.media

    async def normalize(
        self,
        *,
        descriptor: MetadataDescriptor,
        payload: ProviderPayload,
    ) -> UnifiedMetadata:
        """Normalize AniList media data into the shared schema."""
        if not isinstance(payload, AnilistMediaPayload):
            raise UpstreamResponseError("AniList payload was not an object.")

        kind = self._map_entity_type(payload.format)
        title = payload.title
        preferred_title = self.first_non_empty(
            title.english,
            title.romaji,
            title.native,
        )
        if preferred_title is None:
            raise UpstreamResponseError("AniList response did not contain a title.")

        aliases = self.dedupe(
            [
                title.english,
                title.romaji,
                title.native,
                *payload.synonyms,
            ]
        )

        return UnifiedMetadata(
            kind=kind,
            id=build_metadata_id(
                descriptor=descriptor.key,
                provider=DescriptorProvider.ANILIST,
                provider_id=descriptor.provider_id,
            ),
            titles=build_titles(
                display=preferred_title,
                original=title.native,
                aliases=[value for value in aliases if value != preferred_title],
            ),
            synopsis=payload.description,
            release=build_release(
                start_date=self._fuzzy_date(payload.start_date),
                end_date=self._fuzzy_date(payload.end_date),
                status=self._map_status(payload.status),
            ),
            runtime=build_runtime(
                minutes=payload.duration,
                basis="provided" if kind == EntityType.MOVIE else "derived",
            ),
            units=payload.episodes,
            classification=build_classification(
                is_adult=payload.is_adult,
                genres=payload.genres,
            ),
            ratings=build_ratings(
                average=(payload.average_score or 0) / 10
                if payload.average_score is not None
                else None,
                popularity=payload.popularity,
            ),
            images=self._build_images(payload),
            source=build_source(url=f"https://anilist.co/anime/{payload.id}"),
        )

    @staticmethod
    def _fuzzy_date(payload: AnilistFuzzyDatePayload) -> date | None:
        """Convert a fuzzy AniList date payload into a date when possible."""
        year = payload.year
        month = payload.month
        day = payload.day
        if not year or not month or not day:
            return None
        return date(year, month, day)

    @staticmethod
    def _map_status(value: str | None) -> TitleStatus:
        """Map AniList media status to the shared enum."""
        return AnilistAdapter.map_value(
            value,
            AnilistAdapter.STATUS_MAP,
            default=TitleStatus.UNKNOWN,
        )

    @staticmethod
    def _map_entity_type(value: str | None) -> EntityType:
        """Map AniList format values into the shared entity model."""
        return EntityType.MOVIE if value == "MOVIE" else EntityType.SHOW

    @staticmethod
    def _build_images(payload: AnilistMediaPayload) -> list[MetadataImageModel]:
        """Build normalized AniList image metadata."""
        images: list[MetadataImageModel] = []
        cover_url = AnilistAdapter.first_non_empty(
            payload.cover_image.extra_large,
            payload.cover_image.large,
            payload.cover_image.medium,
        )
        if cover_url:
            images.append(MetadataImageModel(kind=ImageType.POSTER, url=cover_url))
        if payload.banner_image:
            images.append(
                MetadataImageModel(kind=ImageType.BANNER, url=payload.banner_image)
            )
        return images

    async def iter_all_normalized(
        self,
    ) -> AsyncGenerator[tuple[str, UnifiedMetadata]]:
        """Yield (descriptor_key, normalized) for every AniList anime entry.

        Pages through all anime using alias-batched GraphQL requests (up to
        `BATCH_PAGES_PER_REQUEST` page aliases per HTTP call, each page
        containing up to `BATCH_PER_PAGE` entries).
        """
        page = 1
        while True:
            end_page = page + self.BATCH_PAGES_PER_REQUEST
            batch_indices = list(range(page, end_page))
            query, variables = self._build_batch_page_query(batch_indices)
            try:
                response_payload = await self.http_client.post_json(
                    self.BASE_URL,
                    json_body={"query": query, "variables": variables},
                )
            except HttpClientError as exc:
                LOGGER.error(
                    "AniList batch: HTTP error fetching pages %d-%d: %s",
                    page,
                    end_page - 1,
                    exc,
                )
                break

            data: dict[str, Any] = response_payload.get("data") or {}
            has_more = False
            for idx in batch_indices:
                alias = f"p{idx}"
                page_data = data.get(alias) or {}
                media_list = page_data.get("media") or []
                page_info = page_data.get("pageInfo") or {}
                if page_info.get("hasNextPage"):
                    has_more = True
                for entry in media_list:
                    try:
                        payload = AnilistMediaPayload.model_validate(entry)
                        descriptor = MetadataDescriptor(
                            provider=DescriptorProvider.ANILIST,
                            provider_id=str(payload.id),
                        )
                        normalized = await self.normalize(
                            descriptor=descriptor, payload=payload
                        )
                    except (ValidationError, UpstreamResponseError, Exception) as exc:
                        LOGGER.warning(
                            "AniList batch: skipping entry %s: %s",
                            entry.get("id"),
                            exc,
                        )
                        continue
                    yield descriptor.key, normalized

            if not has_more:
                break
            page = end_page

    def _build_batch_page_query(
        self, page_indices: list[int]
    ) -> tuple[str, dict[str, Any]]:
        """Build an alias-batched GraphQL query for the given page numbers."""
        page_blocks = "\n".join(
            f"""
            p{idx}: Page(page: {idx}, perPage: $perPage) {{
                pageInfo {{ hasNextPage }}
                media(type: ANIME, sort: ID) {{
                    {self._BATCH_MEDIA_FIELDS}
                }}
            }}"""
            for idx in page_indices
        )
        query = f"query ($perPage: Int!) {{{page_blocks}\n}}"
        variables: dict[str, Any] = {"perPage": self.BATCH_PER_PAGE}
        return query, variables
