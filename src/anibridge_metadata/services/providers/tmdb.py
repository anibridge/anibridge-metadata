"""TMDB provider adapter."""

import re
from typing import ClassVar

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
    MetadataRelationship,
    MetadataRuntime,
    MetadataScope,
    UnifiedMetadata,
    build_classification,
    build_metadata_id,
    build_ratings,
    build_relationship,
    build_release,
    build_runtime,
    build_source,
    build_titles,
)
from anibridge_metadata.services.providers.base import (
    ProviderAdapter,
    ProviderPayload,
    UpstreamNotFoundError,
    UpstreamResponseError,
)
from anibridge_metadata.utils.http import HttpClientError


class TmdbGenrePayload(BaseModel):
    """TMDB genre payload."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None


class TmdbEpisodePayload(BaseModel):
    """Minimal TMDB episode record for dating purposes."""

    model_config = ConfigDict(extra="ignore")

    air_date: str | None = None
    episode_number: int | None = None


class TmdbSeasonPayload(BaseModel):
    """TMDB season summary payload embedded in a show response."""

    model_config = ConfigDict(extra="ignore")

    air_date: str | None = None
    episode_count: int | None = None
    name: str | None = None
    poster_path: str | None = None
    season_number: int | None = None
    episodes: list[TmdbEpisodePayload] = Field(default_factory=list)


class TmdbExternalIdsPayload(BaseModel):
    """TMDB external ids payload."""

    model_config = ConfigDict(extra="ignore")

    imdb_id: str | None = None
    tvdb_id: int | str | None = None


class TmdbPayload(BaseModel):
    """Validated TMDB movie or show payload."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    resolved_entity_type: EntityType = Field(alias="_resolved_entity_type")
    title: str | None = None
    name: str | None = None
    original_title: str | None = None
    original_name: str | None = None
    overview: str | None = None
    status: str | None = None
    first_air_date: str | None = None
    release_date: str | None = None
    last_air_date: str | None = None
    number_of_episodes: int | None = None
    runtime: int | None = None
    episode_run_time: list[int] = Field(default_factory=list)
    vote_average: float | None = None
    popularity: float | None = None
    adult: bool | None = None
    genres: list[TmdbGenrePayload] = Field(default_factory=list)
    seasons: list[TmdbSeasonPayload] = Field(default_factory=list)
    poster_path: str | None = None
    backdrop_path: str | None = None
    external_ids: TmdbExternalIdsPayload | None = None


class TmdbAdapter(ProviderAdapter):
    """Retrieve and normalize TMDB movie or TV metadata."""

    BASE_URL: ClassVar[str] = "https://api.themoviedb.org/3"

    TMDB_IMAGE_BASE: ClassVar[str] = "https://image.tmdb.org/t/p/original"
    STATUS_MAP: ClassVar[dict[str, TitleStatus]] = {
        "Canceled": TitleStatus.CANCELLED,
        "Ended": TitleStatus.FINISHED,
        "In Production": TitleStatus.ONGOING,
        "Planned": TitleStatus.UPCOMING,
        "Post Production": TitleStatus.ONGOING,
        "Released": TitleStatus.FINISHED,
        "Returning Series": TitleStatus.ONGOING,
    }

    async def fetch_raw(self, *, descriptor: MetadataDescriptor) -> ProviderPayload:
        """Fetch TMDB movie or show metadata."""
        headers, params = self._auth()
        if descriptor.provider == DescriptorProvider.TMDB_MOVIE:
            return await self._fetch_movie(
                descriptor=descriptor,
                headers=headers,
                params=params,
            )
        return await self._fetch_show(
            descriptor=descriptor,
            headers=headers,
            params=params,
        )

    async def normalize(
        self,
        *,
        descriptor: MetadataDescriptor,
        payload: ProviderPayload,
    ) -> UnifiedMetadata:
        """Normalize TMDB data into the shared schema."""
        if not isinstance(payload, TmdbPayload):
            raise UpstreamResponseError("TMDB payload was not an object.")

        resolved_entity_type = payload.resolved_entity_type
        runtime = payload.runtime or next(iter(payload.episode_run_time), None)

        genres = [genre.name for genre in payload.genres if genre.name]
        preferred_title = payload.title or payload.name or descriptor.key

        show_status = self._map_status(payload.status)

        scopes = (
            self._build_scopes(
                descriptor=descriptor,
                payload=payload,
                show_title=preferred_title,
                show_runtime=build_runtime(minutes=runtime, basis="derived"),
                show_status=show_status,
            )
            if resolved_entity_type == EntityType.SHOW
            else None
        )
        twins = self._build_twins(
            descriptor=descriptor,
            payload=payload,
            kind=resolved_entity_type,
        )
        source_path = (
            "movie" if descriptor.provider == DescriptorProvider.TMDB_MOVIE else "tv"
        )

        return UnifiedMetadata(
            kind=resolved_entity_type,
            id=build_metadata_id(
                descriptor=descriptor.key,
                provider=descriptor.provider,
                provider_id=descriptor.provider_id,
            ),
            titles=build_titles(
                display=preferred_title,
                main=payload.title or payload.name or descriptor.key,
                original=payload.original_title or payload.original_name,
            ),
            synopsis=payload.overview,
            release=build_release(
                start_date=self.coerce_date(
                    payload.first_air_date or payload.release_date
                ),
                end_date=self.coerce_date(payload.last_air_date),
                status=show_status,
            ),
            runtime=build_runtime(
                minutes=runtime,
                basis="provided"
                if resolved_entity_type == EntityType.MOVIE
                else "derived",
            ),
            units=payload.number_of_episodes,
            classification=build_classification(
                is_adult=payload.adult or False,
                genres=genres,
            ),
            ratings=build_ratings(
                average=payload.vote_average,
                popularity=payload.popularity,
            ),
            images=self._build_images(payload),
            scopes=scopes,
            relationships=twins,
            source=build_source(
                url=(
                    f"https://www.themoviedb.org/{source_path}/{descriptor.provider_id}"
                )
            ),
        )

    def _auth(self) -> tuple[dict[str, str] | None, dict[str, str] | None]:
        """Build TMDB authentication headers."""
        config = self.settings.tmdb
        access_token = self.require(
            config.access_token,
            message="TMDB lookups require ABM_TMDB__ACCESS_TOKEN.",
        )
        return {"Authorization": f"Bearer {access_token}"}, None

    @staticmethod
    def _map_status(value: str | None) -> TitleStatus:
        """Map TMDB status values to the shared enum."""
        return TmdbAdapter.map_value(
            value,
            TmdbAdapter.STATUS_MAP,
            default=TitleStatus.UNKNOWN,
        )

    async def _fetch_movie(
        self,
        *,
        descriptor: MetadataDescriptor,
        headers: dict[str, str] | None,
        params: dict[str, str] | None,
    ) -> TmdbPayload:
        """Fetch and validate a TMDB movie payload."""
        url = f"{self.BASE_URL}/movie/{descriptor.provider_id}"
        fetch_params = {**(params or {}), "append_to_response": "external_ids"}
        raw = await self._get_json(url, headers=headers, params=fetch_params)
        return self._validate_payload(raw, kind=EntityType.MOVIE)

    async def _fetch_show(
        self,
        *,
        descriptor: MetadataDescriptor,
        headers: dict[str, str] | None,
        params: dict[str, str] | None,
    ) -> TmdbPayload:
        """Fetch and enrich a TMDB show payload with season details."""
        url = f"{self.BASE_URL}/tv/{descriptor.provider_id}"
        payload = self._validate_payload(
            await self._get_json(url, headers=headers, params=params),
            kind=EntityType.SHOW,
        )
        append_parts = ["external_ids", *self._season_append_parts(payload)]
        enriched_params = {
            **(params or {}),
            "append_to_response": ",".join(append_parts[:20]),
        }
        try:
            enriched = await self.http_client.get_json(
                url,
                headers=headers,
                params=enriched_params,
            )
        except HttpClientError:
            return payload

        try:
            enriched_payload = self._validate_payload(enriched, kind=EntityType.SHOW)
        except UpstreamResponseError:
            return payload

        self._merge_season_episodes(enriched_payload, enriched)
        return enriched_payload

    async def _get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        params: dict[str, str] | None,
    ) -> dict:
        """Fetch a TMDB JSON payload and normalize not-found handling."""
        try:
            return await self.http_client.get_json(url, headers=headers, params=params)
        except HttpClientError as exc:
            raise UpstreamNotFoundError(
                "TMDB did not find the requested title."
            ) from exc

    @staticmethod
    def _validate_payload(raw: dict, *, kind: EntityType) -> TmdbPayload:
        """Validate a TMDB response into the typed payload model."""
        try:
            return TmdbPayload.model_validate(
                {**raw, "_resolved_entity_type": kind.value}
            )
        except ValidationError as exc:
            raise UpstreamResponseError("TMDB response validation failed.") from exc

    @staticmethod
    def _season_append_parts(payload: TmdbPayload) -> list[str]:
        """Build appended TMDB season response fragments."""
        season_numbers = sorted(
            season.season_number
            for season in payload.seasons
            if season.season_number is not None and season.season_number >= 0
        )
        return [f"season/{season_number}" for season_number in season_numbers]

    @staticmethod
    def _merge_season_episodes(payload: TmdbPayload, enriched: dict) -> None:
        """Merge appended TMDB season episode data back into the payload."""
        for season in payload.seasons:
            if season.season_number is None:
                continue
            season_data = enriched.get(f"season/{season.season_number}")
            if season_data is None:
                continue
            try:
                detail = TmdbSeasonPayload.model_validate(season_data)
            except ValidationError:
                continue
            season.episodes = detail.episodes

    @classmethod
    def _build_images(cls, payload: TmdbPayload) -> list[MetadataImageModel]:
        """Build TMDB image metadata."""
        images: list[MetadataImageModel] = []
        if payload.poster_path:
            images.append(
                MetadataImageModel(
                    kind=ImageType.POSTER,
                    url=f"{cls.TMDB_IMAGE_BASE}{payload.poster_path}",
                )
            )
        if payload.backdrop_path:
            images.append(
                MetadataImageModel(
                    kind=ImageType.BANNER,
                    url=f"{cls.TMDB_IMAGE_BASE}{payload.backdrop_path}",
                )
            )
        return images

    @staticmethod
    def _build_scopes(
        *,
        descriptor: MetadataDescriptor,
        payload: TmdbPayload,
        show_title: str,
        show_runtime: MetadataRuntime | None = None,
        show_status: TitleStatus = TitleStatus.UNKNOWN,
    ) -> dict[str, MetadataScope]:
        """Build scope entries from TMDB season summaries."""
        show_completed = show_status in (TitleStatus.FINISHED, TitleStatus.CANCELLED)
        regular_season_numbers = sorted(
            s.season_number
            for s in payload.seasons
            if s.season_number is not None and s.season_number > 0
        )
        last_season_number = (
            regular_season_numbers[-1] if regular_season_numbers else None
        )

        scopes: dict[str, MetadataScope] = {}
        for season in payload.seasons:
            season_number = season.season_number
            if season_number is None or season_number < 0:
                continue
            scope_key = f"s{season_number}"

            episodes = season.episodes
            earliest_aired = min(
                (TmdbAdapter.coerce_date(e.air_date) for e in episodes if e.air_date),
                default=None,
            )
            latest_aired = max(
                (TmdbAdapter.coerce_date(e.air_date) for e in episodes if e.air_date),
                default=None,
            )

            start_date = earliest_aired or TmdbAdapter.coerce_date(season.air_date)

            # End date derivation:
            # - Specials (s0): only if show is completed
            # - Last season: only if show is completed
            # - Regular seasons: always use latest episode date
            end_date = None
            is_specials = season_number == 0
            is_last_season = season_number == last_season_number

            if is_specials or is_last_season:
                if show_completed:
                    end_date = latest_aired
            else:
                end_date = latest_aired

            scopes[scope_key] = MetadataScope(
                id=build_metadata_id(
                    descriptor=f"{descriptor.provider.value}:{descriptor.provider_id}:{scope_key}",
                    provider=descriptor.provider,
                    provider_id=descriptor.provider_id,
                    scope=scope_key,
                ),
                titles=build_titles(
                    display=season.name or f"{show_title} Season {season_number}",
                ),
                release=build_release(
                    start_date=start_date,
                    end_date=end_date,
                ),
                runtime=show_runtime,
                units=season.episode_count,
            )
        return scopes

    @classmethod
    def _build_twins(
        cls,
        *,
        descriptor: MetadataDescriptor,
        payload: TmdbPayload,
        kind: EntityType,
    ) -> list[MetadataRelationship]:
        """Build cross-provider twin references from TMDB external ids."""
        external_ids = payload.external_ids
        if external_ids is None:
            return []

        twins: list[MetadataRelationship] = []
        imdb_id = cls._extract_imdb_id(external_ids.imdb_id)
        if imdb_id is not None:
            imdb_provider = (
                DescriptorProvider.IMDB_MOVIE
                if kind == EntityType.MOVIE
                else DescriptorProvider.IMDB_SHOW
            )
            twins.append(
                build_relationship(
                    descriptor=f"{imdb_provider}:{imdb_id}",
                    kind=kind,
                )
            )

        if descriptor.provider == DescriptorProvider.TMDB_SHOW:
            tvdb_id = cls._extract_numeric_id(external_ids.tvdb_id)
            if tvdb_id is not None:
                twins.append(
                    build_relationship(
                        descriptor=f"{DescriptorProvider.TVDB_SHOW}:{tvdb_id}",
                        kind=EntityType.SHOW,
                    )
                )

        return twins

    @staticmethod
    def _extract_imdb_id(value: str | None) -> str | None:
        """Extract a canonical IMDB title id from a TMDB external id value."""
        if value is None:
            return None
        match = re.search(r"tt\d+", value)
        if match is None:
            return None
        return match.group(0)

    @staticmethod
    def _extract_numeric_id(value: int | str | None) -> str | None:
        """Extract a canonical numeric provider id from an external id value."""
        if value is None:
            return None
        text = str(value).strip()
        return text if text.isdigit() else None
