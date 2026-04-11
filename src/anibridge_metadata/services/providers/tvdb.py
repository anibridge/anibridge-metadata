"""TVDB provider adapter."""

from datetime import date
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
    UpstreamResponseError,
)
from anibridge_metadata.utils.http import HttpClientError


class TvdbAliasPayload(BaseModel):
    """TVDB alias payload."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None


class TvdbArtworkPayload(BaseModel):
    """TVDB artwork payload."""

    model_config = ConfigDict(extra="ignore")

    image: str | None = None
    thumbnail: str | None = None


class TvdbGenrePayload(BaseModel):
    """TVDB genre payload."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None


class TvdbStatusPayload(BaseModel):
    """TVDB status payload."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None


class TvdbTranslationPayload(BaseModel):
    """TVDB translation payload."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    aliases: list[str] = Field(default_factory=list)
    is_alias: bool | None = Field(default=None, alias="isAlias")
    is_primary: bool | None = Field(default=None, alias="isPrimary")
    language: str | None = None
    name: str | None = None
    overview: str | None = None


class TvdbRemoteIdPayload(BaseModel):
    """TVDB remote id payload from the On Other Sites section."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    source_name: str = Field(default=..., alias="sourceName")
    type: int


class TvdbEpisodeSummaryPayload(BaseModel):
    """Minimal TVDB episode record for counting and dating purposes."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    season_number: int | None = Field(default=None, alias="seasonNumber")
    season_name: str | None = Field(default=None, alias="seasonName")
    aired: str | None = None
    finale_type: str | None = Field(default=None, alias="finaleType")


class TvdbSeasonPayload(BaseModel):
    """TVDB season payload embedded in a series response."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: int | None = None
    number: int | None = None
    season_number: int | None = Field(default=None, alias="seasonNumber")
    name: str | None = None
    slug: str | None = None
    overview: str | None = None
    first_aired: str | None = Field(default=None, alias="firstAired")
    image: str | None = None
    artworks: list[TvdbArtworkPayload] = Field(default_factory=list)


class TvdbPayload(BaseModel):
    """Validated TVDB movie or series payload."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    resolved_entity_type: EntityType = Field(alias="_resolved_entity_type")
    name: str | None = None
    slug: str | None = None
    overview: str | None = None
    original_name: str | None = Field(default=None, alias="originalName")
    aliases: list[str | TvdbAliasPayload] = Field(default_factory=list)
    status: TvdbStatusPayload | str | None = None
    first_aired: str | None = Field(default=None, alias="firstAired")
    first_release: str | None = Field(default=None, alias="first_release")
    last_aired: str | None = Field(default=None, alias="lastAired")
    average_runtime: int | None = Field(default=None, alias="averageRuntime")
    runtime: int | None = None
    score: float | None = None
    genres: list[TvdbGenrePayload] = Field(default_factory=list)
    artworks: list[TvdbArtworkPayload] = Field(default_factory=list)
    image: str | None = None
    seasons: list[TvdbSeasonPayload] = Field(default_factory=list)
    episodes: list[TvdbEpisodeSummaryPayload] = Field(default_factory=list)
    remote_ids: list[TvdbRemoteIdPayload] = Field(
        default_factory=list, alias="remoteIds"
    )


class TvdbAdapter(ProviderAdapter):
    """Retrieve and normalize TVDB movie and series metadata."""

    BASE_URL: ClassVar[str] = "https://api4.thetvdb.com/v4"

    PREFERRED_LANGUAGE = "eng"
    STATUS_MAP: ClassVar[dict[str, TitleStatus]] = {
        "Continuing": TitleStatus.ONGOING,
        "Ended": TitleStatus.FINISHED,
        "In Development": TitleStatus.UPCOMING,
        "Upcoming": TitleStatus.UPCOMING,
    }

    def __init__(self, **kwargs) -> None:
        """Create a TVDB adapter with lazy token management."""
        super().__init__(**kwargs)
        self._token: str | None = None

    async def fetch_raw(self, *, descriptor: MetadataDescriptor) -> ProviderPayload:
        """Fetch a TVDB movie or show payload."""
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        if descriptor.provider == DescriptorProvider.TVDB_MOVIE:
            payload = await self._fetch_payload(
                url=f"{self.BASE_URL}/movies/{descriptor.provider_id}/extended",
                kind=descriptor.requested_entity_type,
                headers=headers,
            )
            return await self._apply_preferred_translation(
                payload=payload,
                translation_path=f"movies/{descriptor.provider_id}",
                headers=headers,
            )

        show_payload = await self._fetch_payload(
            url=f"{self.BASE_URL}/series/{descriptor.provider_id}/extended?meta=episodes",
            kind=descriptor.requested_entity_type,
            headers=headers,
        )
        return await self._apply_preferred_translation(
            payload=show_payload,
            translation_path=f"series/{descriptor.provider_id}",
            headers=headers,
        )

    async def normalize(
        self, *, descriptor: MetadataDescriptor, payload: ProviderPayload
    ) -> UnifiedMetadata:
        """Normalize TVDB data into the shared schema."""
        if not isinstance(payload, TvdbPayload):
            raise UpstreamResponseError("TVDB payload was not an object.")

        resolved_entity_type = payload.resolved_entity_type
        status = (
            payload.status.name
            if isinstance(payload.status, TvdbStatusPayload)
            else payload.status
        )
        source_path = (
            "movies"
            if descriptor.provider == DescriptorProvider.TVDB_MOVIE
            else "series"
        )
        preferred_title = payload.name or payload.slug or descriptor.key
        runtime_minutes = payload.average_runtime or payload.runtime
        show_runtime = build_runtime(minutes=runtime_minutes, basis="derived")
        show_status = self._map_status(status)

        scopes = (
            self._build_scopes(
                descriptor=descriptor,
                payload=payload,
                show_title=preferred_title,
                show_runtime=show_runtime,
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

        return UnifiedMetadata(
            kind=resolved_entity_type,
            id=build_metadata_id(
                descriptor=descriptor.key,
                provider=descriptor.provider,
                provider_id=descriptor.provider_id,
            ),
            titles=build_titles(
                display=preferred_title,
                original=payload.original_name,
                aliases=self._extract_aliases(payload),
            ),
            synopsis=payload.overview,
            release=build_release(
                start_date=self.coerce_date(
                    payload.first_aired or payload.first_release
                ),
                end_date=self.coerce_date(payload.last_aired),
                status=show_status,
            ),
            runtime=build_runtime(
                minutes=runtime_minutes,
                basis="provided"
                if resolved_entity_type == EntityType.MOVIE
                else "derived",
            ),
            units=len([e for e in payload.episodes if (e.season_number or 0) > 0])
            or None,
            classification=build_classification(
                genres=[genre.name for genre in payload.genres if genre.name],
            ),
            ratings=build_ratings(
                popularity=payload.score,
            ),
            images=self._build_images(payload),
            scopes=scopes,
            relationships=twins,
            source=build_source(
                url=f"https://thetvdb.com/{source_path}/{descriptor.provider_id}"
            ),
        )

    async def _get_token(self) -> str:
        """Fetch and cache a TVDB access token."""
        if self._token:
            return self._token

        config = self.settings.tvdb
        api_key = self.require(
            config.api_key,
            "TVDB lookups require ABM_TVDB__API_KEY.",
        )
        payload = {"apikey": api_key}
        if config.pin:
            payload["pin"] = config.pin
        try:
            response = await self.http_client.post_json(
                f"{self.BASE_URL}/login",
                json_body=payload,
            )
        except HttpClientError as exc:
            raise UpstreamResponseError(str(exc)) from exc

        token = response.get("data", {}).get("token") or response.get("token")
        if not token:
            raise UpstreamResponseError("TVDB login did not return an access token.")
        self._token = token
        return token

    async def _fetch_payload(
        self,
        *,
        url: str,
        kind: EntityType,
        headers: dict[str, str],
    ) -> TvdbPayload:
        """Fetch and validate a TVDB entity payload."""
        try:
            response_payload = await self.http_client.get_json(url, headers=headers)
        except HttpClientError as exc:
            raise UpstreamResponseError(str(exc)) from exc

        payload = response_payload.get("data", response_payload)
        try:
            return TvdbPayload.model_validate(
                {**payload, "_resolved_entity_type": kind.value}
            )
        except ValidationError as exc:
            raise UpstreamResponseError("TVDB response validation failed.") from exc

    async def _apply_preferred_translation(
        self, *, payload: TvdbPayload, translation_path: str, headers: dict[str, str]
    ) -> TvdbPayload:
        """Overlay an English title and synopsis when TVDB exposes them."""
        translation = await self._fetch_translation(
            translation_path=translation_path, headers=headers
        )
        if translation is None:
            return payload

        updates: dict[str, str] = {}
        translated_name = (
            translation.name
            if translation.name and translation.is_alias is not True
            else None
        )
        if translated_name:
            updates["name"] = translated_name
            if (
                payload.original_name is None
                and payload.name
                and payload.name != translated_name
            ):
                updates["original_name"] = payload.name
        if translation.overview:
            updates["overview"] = translation.overview

        if not updates:
            return payload
        return payload.model_copy(update=updates)

    async def _fetch_translation(
        self, *, translation_path: str, headers: dict[str, str]
    ) -> TvdbTranslationPayload | None:
        """Fetch a best-effort TVDB translation record for the preferred language."""
        url = (
            f"{self.BASE_URL}/{translation_path}/translations/{self.PREFERRED_LANGUAGE}"
        )
        try:
            response_payload = await self.http_client.get_json(url, headers=headers)
        except HttpClientError:
            return None

        payload = response_payload.get("data", response_payload)
        try:
            return TvdbTranslationPayload.model_validate(payload)
        except ValidationError:
            return None

    @staticmethod
    def _extract_aliases(payload: TvdbPayload) -> list[str]:
        """Normalize TVDB aliases into a list of plain strings."""
        aliases: list[str] = []
        for alias in payload.aliases:
            if isinstance(alias, str):
                aliases.append(alias)
                continue
            if alias.name:
                aliases.append(alias.name)
        return aliases

    @staticmethod
    def _build_images(payload: TvdbPayload) -> list[MetadataImageModel]:
        """Build TVDB image metadata."""
        images: list[MetadataImageModel] = []
        if payload.image:
            images.append(MetadataImageModel(kind=ImageType.POSTER, url=payload.image))
        for artwork in payload.artworks[:3]:
            image_url = artwork.image or artwork.thumbnail
            if not image_url:
                continue
            images.append(MetadataImageModel(kind=ImageType.POSTER, url=image_url))
        return images

    @staticmethod
    def _build_scopes(
        *,
        descriptor: MetadataDescriptor,
        payload: TvdbPayload,
        show_title: str,
        show_runtime: MetadataRuntime | None = None,
        show_status: TitleStatus = TitleStatus.UNKNOWN,
    ) -> dict[str, MetadataScope]:
        """Build scope entries from TVDB season summaries."""
        show_completed = show_status in (TitleStatus.FINISHED, TitleStatus.CANCELLED)
        regular_season_numbers = sorted(
            s.number
            if s.number is not None
            else (s.season_number if s.season_number is not None else -1)
            for s in payload.seasons
            if (
                s.number
                if s.number is not None
                else (s.season_number if s.season_number is not None else -1)
            )
            > 0
        )
        last_season_number = (
            regular_season_numbers[-1] if regular_season_numbers else None
        )

        scopes: dict[str, MetadataScope] = {}
        for season in payload.seasons:
            season_number = (
                season.number if season.number is not None else season.season_number
            )
            if season_number is None or season_number < 0:
                continue
            scope_key = f"s{season_number}"
            season_episodes = [
                e for e in payload.episodes if e.season_number == season_number
            ]
            earliest_aired = min(
                (TvdbAdapter.coerce_date(e.aired) for e in season_episodes if e.aired),
                default=None,
            )
            latest_aired: date | None = max(
                (TvdbAdapter.coerce_date(e.aired) for e in season_episodes if e.aired),
                default=None,
            )
            season_name = next(
                (e.season_name for e in season_episodes if e.season_name),
                None,
            )

            # End date derivation:
            # - Specials (s0): only if show is completed
            # - Last season: only if show is completed or a season/series finale exists
            # - Regular seasons: always use latest episode date
            end_date: date | None = None
            if season_number == 0:
                if show_completed:
                    end_date = latest_aired
            elif season_number == last_season_number:
                has_finale = any(
                    e.finale_type in ("season", "series")
                    for e in season_episodes
                    if e.finale_type
                )
                if show_completed or has_finale:
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
                    display=season_name
                    or season.name
                    or f"{show_title} Season {season_number}",
                ),
                release=build_release(
                    start_date=earliest_aired,
                    end_date=end_date,
                ),
                runtime=show_runtime,
                units=len(season_episodes) or None,
            )
        return scopes

    @classmethod
    def _build_twins(
        cls,
        *,
        descriptor: MetadataDescriptor,
        payload: TvdbPayload,
        kind: EntityType,
    ) -> list[MetadataRelationship]:
        """Build cross-provider twin references from TVDB remote ids."""
        twins: list[MetadataRelationship] = []
        for remote_id in payload.remote_ids:
            provider, provider_id = cls._remote_descriptor(
                remote_id=remote_id, kind=kind
            )
            if not provider or not provider_id:
                continue
            target_descriptor = f"{provider}:{provider_id}"
            if target_descriptor == descriptor.key:
                continue
            twins.append(
                build_relationship(
                    descriptor=target_descriptor,
                    kind=kind,
                )
            )
        return twins

    @staticmethod
    def _remote_descriptor(
        *, remote_id: TvdbRemoteIdPayload, kind: EntityType
    ) -> tuple[DescriptorProvider | None, str | None]:
        """Map a TVDB remote id source ID to a supported provider."""
        provider = {
            2: DescriptorProvider.IMDB_MOVIE
            if kind == EntityType.MOVIE
            else DescriptorProvider.IMDB_SHOW,
            10: DescriptorProvider.TMDB_MOVIE,
            12: DescriptorProvider.TMDB_SHOW,
        }.get(remote_id.type)
        return provider, remote_id.id

    @staticmethod
    def _map_status(value: str | None) -> TitleStatus:
        """Map TVDB status values to the shared enum."""
        return TvdbAdapter.map_value(
            value,
            TvdbAdapter.STATUS_MAP,
            default=TitleStatus.UNKNOWN,
        )
