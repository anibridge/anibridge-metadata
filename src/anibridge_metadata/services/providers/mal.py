"""MyAnimeList provider adapter."""

import logging
from collections.abc import AsyncGenerator
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


class MalAlternativeTitlesPayload(BaseModel):
    """MyAnimeList alternate titles payload."""

    model_config = ConfigDict(extra="ignore")

    en: str | None = None
    ja: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class MalPicturePayload(BaseModel):
    """MyAnimeList picture payload."""

    model_config = ConfigDict(extra="ignore")

    medium: str | None = None
    large: str | None = None


class MalGenrePayload(BaseModel):
    """MyAnimeList genre payload."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None


class MalAnimePayload(BaseModel):
    """Validated MyAnimeList anime payload."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    alternative_titles: MalAlternativeTitlesPayload = Field(
        default_factory=MalAlternativeTitlesPayload
    )
    synopsis: str | None = None
    media_type: str | None = None
    status: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    num_episodes: int | None = None
    average_episode_duration: int | None = None
    mean: float | None = None
    popularity: int | None = None
    nsfw: str | None = None
    genres: list[MalGenrePayload] = Field(default_factory=list)
    main_picture: MalPicturePayload = Field(default_factory=MalPicturePayload)


class MalAdapter(ProviderAdapter, BatchableProvider):
    """Retrieve and normalize MyAnimeList anime metadata."""

    BASE_URL: ClassVar[str] = "https://api.myanimelist.net/v2"

    MAL_FIELDS: ClassVar[str] = (
        "title,alternative_titles,synopsis,media_type,status,start_date,end_date,"
        "num_episodes,average_episode_duration,mean,popularity,nsfw,genres,main_picture"
    )
    STATUS_MAP: ClassVar[dict[str, TitleStatus]] = {
        "currently_airing": TitleStatus.ONGOING,
        "finished_airing": TitleStatus.FINISHED,
        "not_yet_aired": TitleStatus.UPCOMING,
    }

    async def fetch_raw(self, *, descriptor: MetadataDescriptor) -> ProviderPayload:
        """Fetch anime metadata from the MyAnimeList v2 API."""
        config = self.settings.mal
        client_id = self.require(
            config.client_id,
            "MyAnimeList lookups require ABM_MAL__CLIENT_ID.",
        )
        url = f"{self.BASE_URL}/anime/{descriptor.provider_id}"
        headers = {"X-MAL-CLIENT-ID": str(client_id)}
        try:
            payload = await self.http_client.get_json(
                url, headers=headers, params={"fields": MalAdapter.MAL_FIELDS}
            )
        except HttpClientError as exc:
            if exc.status_code == 404:
                raise UpstreamNotFoundError(
                    "MyAnimeList did not find the requested title."
                ) from exc
            raise UpstreamResponseError(str(exc)) from exc
        try:
            return MalAnimePayload.model_validate(payload)
        except ValidationError as exc:
            raise UpstreamResponseError(
                "MyAnimeList response validation failed."
            ) from exc

    async def normalize(
        self,
        *,
        descriptor: MetadataDescriptor,
        payload: ProviderPayload,
    ) -> UnifiedMetadata:
        """Normalize MyAnimeList data into the shared schema."""
        if not isinstance(payload, MalAnimePayload):
            raise UpstreamResponseError("MyAnimeList payload was not an object.")

        kind = self._map_entity_type(payload.media_type)
        alternative_titles = payload.alternative_titles
        aliases = self.dedupe(
            [
                alternative_titles.en,
                alternative_titles.ja,
                *alternative_titles.synonyms,
            ]
        )

        return UnifiedMetadata(
            kind=kind,
            id=build_metadata_id(
                descriptor=descriptor.key,
                provider=DescriptorProvider.MAL,
                provider_id=descriptor.provider_id,
            ),
            titles=build_titles(
                display=payload.title or descriptor.provider_id,
                original=alternative_titles.ja,
                aliases=aliases,
            ),
            synopsis=payload.synopsis,
            release=build_release(
                start_date=self.coerce_date(payload.start_date),
                end_date=self.coerce_date(payload.end_date),
                status=self._map_status(payload.status),
            ),
            runtime=build_runtime(
                minutes=self._runtime_minutes(payload.average_episode_duration),
                basis="provided" if kind == EntityType.MOVIE else "derived",
            ),
            units=payload.num_episodes,
            classification=build_classification(
                is_adult=payload.nsfw == "black",
                genres=[genre.name for genre in payload.genres if genre.name],
            ),
            ratings=build_ratings(
                average=payload.mean,
                popularity=payload.popularity,
            ),
            images=self._build_images(payload.main_picture),
            source=build_source(
                url=f"https://myanimelist.net/anime/{descriptor.provider_id}"
            ),
        )

    @staticmethod
    def _map_status(value: str | None) -> TitleStatus:
        """Map MyAnimeList status values to the shared enum."""
        return MalAdapter.map_value(
            value,
            MalAdapter.STATUS_MAP,
            default=TitleStatus.UNKNOWN,
        )

    @staticmethod
    def _map_entity_type(value: str | None) -> EntityType:
        """Map MAL media types into the shared entity model."""
        return EntityType.MOVIE if value == "movie" else EntityType.SHOW

    @staticmethod
    def _build_images(payload: MalPicturePayload) -> list[MetadataImageModel]:
        """Build normalized MyAnimeList image metadata."""
        poster_url = MalAdapter.first_non_empty(payload.large, payload.medium)
        if poster_url is None:
            return []
        return [MetadataImageModel(kind=ImageType.POSTER, url=poster_url)]

    @staticmethod
    def _runtime_minutes(duration_seconds: int | None) -> int | None:
        """Convert MAL episode duration seconds into minutes."""
        if duration_seconds is None:
            return None
        return int(duration_seconds / 60)

    async def iter_all_normalized(
        self,
    ) -> AsyncGenerator[tuple[str, UnifiedMetadata]]:
        """Yield (descriptor_key, normalized) for every anime in the MAL ranking.

        Pages through the `ranking_type=all` endpoint (500 entries per page)
        which covers the complete MAL anime catalogue.  Each ranking response
        already includes all fields required for normalization, so no extra
        per-entry requests are needed.
        """
        config = self.settings.mal
        client_id = self.require(
            config.client_id,
            "MyAnimeList batch refresh requires ABM_MAL__CLIENT_ID.",
        )
        headers = {"X-MAL-CLIENT-ID": str(client_id)}
        params: dict[str, str] = {
            "ranking_type": "all",
            "limit": "500",
            "fields": MalAdapter.MAL_FIELDS,
        }
        url = f"{self.BASE_URL}/anime/ranking"
        offset = 0

        while True:
            page_params = {**params, "offset": str(offset)}
            try:
                response: dict[str, Any] = await self.http_client.get_json(
                    url, headers=headers, params=page_params
                )
            except HttpClientError as exc:
                LOGGER.error("MAL batch: HTTP error at offset %d: %s", offset, exc)
                break

            data = response.get("data") or []
            for entry in data:
                node = entry.get("node") or {}
                node_id = node.get("id")
                if not node_id:
                    continue
                try:
                    payload = MalAnimePayload.model_validate(node)
                    descriptor = MetadataDescriptor(
                        provider=DescriptorProvider.MAL,
                        provider_id=str(node_id),
                    )
                    normalized = await self.normalize(
                        descriptor=descriptor, payload=payload
                    )
                except (ValidationError, UpstreamResponseError, Exception) as exc:
                    LOGGER.warning("MAL batch: skipping node %s: %s", node_id, exc)
                    continue
                yield descriptor.key, normalized

            if not response.get("paging", {}).get("next"):
                break
            offset += len(data)
