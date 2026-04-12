"""IMDB provider adapter backed by the public QLever endpoints."""

import logging
import re
from collections.abc import AsyncGenerator
from datetime import date
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from anibridge_metadata.core.descriptors import MetadataDescriptor
from anibridge_metadata.core.enums import DescriptorProvider, EntityType, TitleStatus
from anibridge_metadata.models.metadata import (
    MetadataRuntime,
    MetadataScope,
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


class ImdbSparqlValue(BaseModel):
    """Single SPARQL JSON binding value."""

    model_config = ConfigDict(extra="ignore")

    type: str
    value: str
    datatype: str | None = None


class ImdbSparqlResults(BaseModel):
    """SPARQL JSON results wrapper."""

    model_config = ConfigDict(extra="ignore")

    bindings: list[dict[str, ImdbSparqlValue]] = Field(default_factory=list)


class ImdbSparqlResponse(BaseModel):
    """Minimal SPARQL JSON response model for QLever."""

    model_config = ConfigDict(extra="ignore")

    results: ImdbSparqlResults = Field(default_factory=ImdbSparqlResults)


class ImdbPayload(BaseModel):
    """Normalized IMDB fields extracted from the QLever response."""

    canonical_id: str
    label: str | None = None
    primary_title: str | None = None
    original_title: str | None = None
    type_value: str | None = None
    is_adult: bool = False
    start_year: int | None = None
    end_year: int | None = None
    runtime_minutes: int | None = None
    average_rating: float | None = None
    num_votes: int | None = None
    genres: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    seasons: list[ImdbSeasonPayload] = Field(default_factory=list)

    @classmethod
    def from_binding(cls, binding: dict[str, ImdbSparqlValue]) -> ImdbPayload:
        """Build an IMDB payload from a single SPARQL result binding."""
        canonical_id = cls._required_text(binding, "canonicalId")
        return cls(
            canonical_id=canonical_id,
            label=cls._text(binding, "label"),
            primary_title=cls._text(binding, "primaryTitle"),
            original_title=cls._text(binding, "originalTitle"),
            type_value=cls._text(binding, "typeValue"),
            is_adult=cls._bool(binding, "isAdult") or False,
            start_year=cls._int(binding, "startYear"),
            end_year=cls._int(binding, "endYear"),
            runtime_minutes=cls._int(binding, "runtime"),
            average_rating=cls._float(binding, "rating"),
            num_votes=cls._int(binding, "votes"),
            genres=cls._split(binding, "genres"),
            aliases=cls._split(binding, "aliases"),
        )

    @staticmethod
    def _value(binding: dict[str, ImdbSparqlValue], key: str) -> ImdbSparqlValue | None:
        """Return a SPARQL binding value when present."""
        return binding.get(key)

    @classmethod
    def _required_text(cls, binding: dict[str, ImdbSparqlValue], key: str) -> str:
        """Return a required string field from a binding."""
        value = cls._text(binding, key)
        if value is None:
            raise UpstreamResponseError(f"IMDB response did not include {key}.")
        return value

    @classmethod
    def _text(cls, binding: dict[str, ImdbSparqlValue], key: str) -> str | None:
        """Return an optional string field from a binding."""
        value = cls._value(binding, key)
        if value is None:
            return None
        cleaned = value.value.strip()
        return cleaned or None

    @classmethod
    def _int(cls, binding: dict[str, ImdbSparqlValue], key: str) -> int | None:
        """Return an optional integer field from a binding."""
        value = cls._text(binding, key)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise UpstreamResponseError(
                f"IMDB field {key} was not an integer."
            ) from exc

    @classmethod
    def _float(cls, binding: dict[str, ImdbSparqlValue], key: str) -> float | None:
        """Return an optional float field from a binding."""
        value = cls._text(binding, key)
        if value is None:
            return None
        try:
            return float(value)
        except ValueError as exc:
            raise UpstreamResponseError(f"IMDB field {key} was not numeric.") from exc

    @classmethod
    def _bool(cls, binding: dict[str, ImdbSparqlValue], key: str) -> bool | None:
        """Return an optional boolean field from a binding."""
        value = cls._text(binding, key)
        if value is None:
            return None
        if value == "true":
            return True
        if value == "false":
            return False
        raise UpstreamResponseError(f"IMDB field {key} was not a boolean.")

    @classmethod
    def _split(cls, binding: dict[str, ImdbSparqlValue], key: str) -> list[str]:
        """Split a GROUP_CONCAT field into distinct values."""
        value = cls._text(binding, key)
        if value is None:
            return []
        return [part for part in value.split("|||") if part]

    @property
    def episode_count(self) -> int | None:
        """Return the total episode count derived from season groupings."""
        total = sum(season.episode_count or 0 for season in self.seasons)
        return total or None


class ImdbSeasonPayload(BaseModel):
    """Derived season summary for an IMDB show."""

    season_number: int
    episode_count: int | None = None
    start_year: int | None = None
    end_year: int | None = None

    @classmethod
    def from_binding(cls, binding: dict[str, ImdbSparqlValue]) -> ImdbSeasonPayload:
        """Build a season summary from a SPARQL aggregate binding."""
        season_number = ImdbPayload._int(binding, "season")
        if season_number is None:
            raise UpstreamResponseError("IMDB season query did not include a season.")
        return cls(
            season_number=season_number,
            episode_count=ImdbPayload._int(binding, "episodeCount"),
            start_year=ImdbPayload._int(binding, "seasonStartYear"),
            end_year=ImdbPayload._int(binding, "seasonEndYear"),
        )


class ImdbAdapter(ProviderAdapter, BatchableProvider):
    """Retrieve and normalize IMDB metadata from the QLever endpoints."""

    BASE_URL: ClassVar[str] = "https://qlever.dev/api/imdb"
    WIKIDATA_URL: ClassVar[str] = "https://qlever.dev/api/wikidata"
    TITLE_ID_PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"^tt\d+$")
    MOVIE_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"movie", "short", "tvMovie", "tvShort", "video"}
    )
    SHOW_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"tvEpisode", "tvMiniSeries", "tvSeries"}
    )
    WIKIDATA_IMDB_IDS_QUERY: ClassVar[str] = """
        PREFIX wd: <http://www.wikidata.org/entity/>
        PREFIX wdt: <http://www.wikidata.org/prop/direct/>

        SELECT DISTINCT ?imdbId WHERE {
          VALUES ?animeType {
            wd:Q20650540
            wd:Q63952888
          }

          ?item wdt:P31/wdt:P279* ?animeType ;
                wdt:P345 ?imdbId .
        }
        LIMIT 500000
    """
    QUERY_TEMPLATE: ClassVar[str] = """
        SELECT
          ?canonicalId
          ?label
          ?primaryTitle
          ?originalTitle
          ?typeValue
          ?isAdult
          ?startYear
          ?endYear
          ?runtime
          ?rating
          ?votes
          (GROUP_CONCAT(DISTINCT ?genre; separator="|||") AS ?genres)
          (GROUP_CONCAT(DISTINCT ?alias; separator="|||") AS ?aliases)
        WHERE {{
          VALUES ?title {{ <{title_uri}> }}
          ?title <https://www.imdb.com/id> ?canonicalId .
          OPTIONAL {{ ?title <http://www.w3.org/2000/01/rdf-schema#label> ?label }}
          OPTIONAL {{ ?title <https://www.imdb.com/primaryTitle> ?primaryTitle }}
          OPTIONAL {{ ?title <https://www.imdb.com/originalTitle> ?originalTitle }}
          OPTIONAL {{ ?title <https://www.imdb.com/type> ?typeValue }}
          OPTIONAL {{ ?title <https://www.imdb.com/isAdult> ?isAdult }}
          OPTIONAL {{ ?title <https://www.imdb.com/startYear> ?startYear }}
          OPTIONAL {{ ?title <https://www.imdb.com/endYear> ?endYear }}
          OPTIONAL {{ ?title <https://www.imdb.com/runtimeMinutes> ?runtime }}
          OPTIONAL {{ ?title <https://www.imdb.com/averageRating> ?rating }}
          OPTIONAL {{ ?title <https://www.imdb.com/numVotes> ?votes }}
          OPTIONAL {{ ?title <https://www.imdb.com/genre> ?genre }}
          OPTIONAL {{ ?title <http://www.w3.org/2004/02/skos/core#altLabel> ?alias }}
        }}
        GROUP BY
          ?canonicalId
          ?label
          ?primaryTitle
          ?originalTitle
          ?typeValue
          ?isAdult
          ?startYear
          ?endYear
          ?runtime
          ?rating
          ?votes
        """
    SEASON_QUERY_TEMPLATE: ClassVar[str] = """
            SELECT
                ?season
                (COUNT(DISTINCT ?episodeId) AS ?episodeCount)
                (MIN(?episodeYear) AS ?seasonStartYear)
                (MAX(?episodeYear) AS ?seasonEndYear)
            WHERE {{
                ?episode <https://www.imdb.com/parentTitle> <{title_uri}> ;
                    <https://www.imdb.com/id> ?episodeId ;
                    <https://www.imdb.com/seasonNumber> ?season .
                OPTIONAL {{
                    ?episode <https://www.imdb.com/startYear> ?episodeYear
                }}
            }}
            GROUP BY ?season
            ORDER BY ?season
    """

    BATCH_SIZE: ClassVar[int] = 50

    BATCH_TITLE_QUERY: ClassVar[str] = """
        SELECT
          ?titleUri ?canonicalId ?label ?primaryTitle ?originalTitle ?typeValue
          ?isAdult ?startYear ?endYear ?runtime ?rating ?votes
          (GROUP_CONCAT(DISTINCT ?genre; separator="|||") AS ?genres)
          (GROUP_CONCAT(DISTINCT ?alias; separator="|||") AS ?aliases)
        WHERE {{
          VALUES ?titleUri {{ {uri_list} }}
          ?titleUri <https://www.imdb.com/id> ?canonicalId .
          OPTIONAL {{ ?titleUri <http://www.w3.org/2000/01/rdf-schema#label> ?label }}
          OPTIONAL {{ ?titleUri <https://www.imdb.com/primaryTitle> ?primaryTitle }}
          OPTIONAL {{ ?titleUri <https://www.imdb.com/originalTitle> ?originalTitle }}
          OPTIONAL {{ ?titleUri <https://www.imdb.com/type> ?typeValue }}
          OPTIONAL {{ ?titleUri <https://www.imdb.com/isAdult> ?isAdult }}
          OPTIONAL {{ ?titleUri <https://www.imdb.com/startYear> ?startYear }}
          OPTIONAL {{ ?titleUri <https://www.imdb.com/endYear> ?endYear }}
          OPTIONAL {{ ?titleUri <https://www.imdb.com/runtimeMinutes> ?runtime }}
          OPTIONAL {{ ?titleUri <https://www.imdb.com/averageRating> ?rating }}
          OPTIONAL {{ ?titleUri <https://www.imdb.com/numVotes> ?votes }}
          OPTIONAL {{ ?titleUri <https://www.imdb.com/genre> ?genre }}
          OPTIONAL {{ ?titleUri <http://www.w3.org/2004/02/skos/core#altLabel> ?alias }}
        }}
        GROUP BY
          ?titleUri ?canonicalId ?label ?primaryTitle ?originalTitle ?typeValue
          ?isAdult ?startYear ?endYear ?runtime ?rating ?votes
    """

    BATCH_SEASON_QUERY: ClassVar[str] = """
        SELECT
          ?parentId ?season
          (COUNT(DISTINCT ?episodeId) AS ?episodeCount)
          (MIN(?episodeYear) AS ?seasonStartYear)
          (MAX(?episodeYear) AS ?seasonEndYear)
        WHERE {{
          VALUES ?parent {{ {uri_list} }}
          ?parent <https://www.imdb.com/id> ?parentId .
          ?episode <https://www.imdb.com/parentTitle> ?parent ;
              <https://www.imdb.com/id> ?episodeId ;
              <https://www.imdb.com/seasonNumber> ?season .
          OPTIONAL {{ ?episode <https://www.imdb.com/startYear> ?episodeYear }}
        }}
        GROUP BY ?parentId ?season
        ORDER BY ?parentId ?season
    """

    async def fetch_raw(self, *, descriptor: MetadataDescriptor) -> ProviderPayload:
        """Fetch IMDB title metadata through the QLever SPARQL endpoint."""
        provider_id = descriptor.provider_id.strip()
        if self.TITLE_ID_PATTERN.fullmatch(provider_id) is None:
            raise UpstreamResponseError("IMDB ids must look like tt1234567.")

        try:
            response_payload = await self.http_client.get_json(
                self.BASE_URL,
                params={"query": self._build_query(provider_id)},
            )
        except HttpClientError as exc:
            raise UpstreamResponseError(str(exc)) from exc

        try:
            response = ImdbSparqlResponse.model_validate(response_payload)
        except ValidationError as exc:
            raise UpstreamResponseError("IMDB response validation failed.") from exc

        if not response.results.bindings:
            raise UpstreamNotFoundError("IMDB did not find the requested title.")

        payload = ImdbPayload.from_binding(response.results.bindings[0])
        resolved_kind = self._resolve_kind(payload.type_value, descriptor=descriptor)
        if resolved_kind == EntityType.SHOW:
            payload.seasons = await self._fetch_seasons(provider_id)
        return payload

    async def normalize(
        self,
        *,
        descriptor: MetadataDescriptor,
        payload: ProviderPayload,
    ) -> UnifiedMetadata:
        """Normalize IMDB data into the shared metadata schema."""
        if not isinstance(payload, ImdbPayload):
            raise UpstreamResponseError("IMDB payload was not an object.")

        kind = self._resolve_kind(payload.type_value, descriptor=descriptor)
        show_status = self._map_status(payload=payload, kind=kind)
        main_title = self.first_non_empty(
            payload.primary_title,
            payload.label,
            payload.original_title,
        )
        if main_title is None:
            raise UpstreamResponseError("IMDB response did not contain a title.")

        display_title = self.first_non_empty(payload.label, payload.primary_title)
        if display_title is None:
            display_title = main_title

        aliases = [
            value
            for value in self.dedupe(payload.aliases)
            if value not in {display_title, main_title, payload.original_title}
        ]
        runtime = build_runtime(
            minutes=payload.runtime_minutes,
            basis="provided" if kind == EntityType.MOVIE else "derived",
        )
        scopes = (
            self._build_scopes(
                descriptor=descriptor,
                payload=payload,
                show_title=display_title,
                show_runtime=runtime,
                show_status=show_status,
            )
            if kind == EntityType.SHOW
            else None
        )

        return UnifiedMetadata(
            kind=kind,
            id=build_metadata_id(
                descriptor=descriptor.key,
                provider=descriptor.provider,
                provider_id=payload.canonical_id,
            ),
            titles=build_titles(
                display=display_title,
                main=main_title,
                original=payload.original_title,
                aliases=aliases,
            ),
            release=build_release(
                status=show_status,
            ),
            runtime=runtime,
            units=payload.episode_count if kind == EntityType.SHOW else 1,
            classification=build_classification(
                is_adult=payload.is_adult,
                genres=self.dedupe(payload.genres),
            ),
            ratings=build_ratings(
                average=payload.average_rating,
                popularity=float(payload.num_votes)
                if payload.num_votes is not None
                else None,
            ),
            scopes=scopes,
            source=build_source(url=self._title_url(payload.canonical_id)),
        )

    @classmethod
    def _build_query(cls, provider_id: str) -> str:
        """Build the one-shot SPARQL query for a specific IMDB id."""
        return cls.QUERY_TEMPLATE.format(title_uri=cls._title_url(provider_id))

    @classmethod
    def _build_season_query(cls, provider_id: str) -> str:
        """Build the season aggregation query for an IMDB show."""
        return cls.SEASON_QUERY_TEMPLATE.format(title_uri=cls._title_url(provider_id))

    async def _fetch_seasons(self, provider_id: str) -> list[ImdbSeasonPayload]:
        """Fetch season aggregates for an IMDB show."""
        try:
            response_payload = await self.http_client.get_json(
                self.BASE_URL,
                params={"query": self._build_season_query(provider_id)},
            )
        except HttpClientError as exc:
            raise UpstreamResponseError(str(exc)) from exc

        try:
            response = ImdbSparqlResponse.model_validate(response_payload)
        except ValidationError as exc:
            raise UpstreamResponseError(
                "IMDB season response validation failed."
            ) from exc

        return [
            ImdbSeasonPayload.from_binding(binding)
            for binding in response.results.bindings
        ]

    @staticmethod
    def _title_url(provider_id: str) -> str:
        """Return the canonical IMDB title URL for an id."""
        return f"https://www.imdb.com/title/{provider_id}"

    @staticmethod
    def _build_scopes(
        *,
        descriptor: MetadataDescriptor,
        payload: ImdbPayload,
        show_title: str,
        show_runtime: MetadataRuntime | None = None,
        show_status: TitleStatus = TitleStatus.UNKNOWN,
    ) -> dict[str, MetadataScope] | None:
        """Build scope entries from IMDB season aggregates."""
        if not payload.seasons:
            return None

        regular_season_numbers = sorted(
            season.season_number
            for season in payload.seasons
            if season.season_number > 0
        )
        last_regular_season = (
            regular_season_numbers[-1] if regular_season_numbers else None
        )

        scopes: dict[str, MetadataScope] = {}
        for season in payload.seasons:
            season_number = season.season_number
            scope_key = f"s{season_number}"
            display_title = (
                "Specials"
                if season_number == 0
                else f"{show_title} Season {season_number}"
            )
            scopes[scope_key] = MetadataScope(
                id=build_metadata_id(
                    descriptor=f"{descriptor.provider.value}:{descriptor.provider_id}:{scope_key}",
                    provider=descriptor.provider,
                    provider_id=descriptor.provider_id,
                    scope=scope_key,
                ),
                titles=build_titles(display=display_title),
                release=build_release(
                    status=ImdbAdapter._map_scope_status(
                        season_number=season_number,
                        last_regular_season=last_regular_season,
                        show_status=show_status,
                    )
                ),
                runtime=show_runtime,
                units=season.episode_count,
            )
        return scopes

    @classmethod
    def _resolve_kind(
        cls,
        type_value: str | None,
        *,
        descriptor: MetadataDescriptor,
    ) -> EntityType:
        """Resolve and validate the entity kind for a descriptor."""
        requested = descriptor.requested_entity_type
        if type_value in cls.MOVIE_TYPES:
            resolved = EntityType.MOVIE
        elif type_value in cls.SHOW_TYPES:
            resolved = EntityType.SHOW
        else:
            resolved = requested

        if resolved != requested:
            raise UpstreamNotFoundError(
                "IMDB title type did not match the requested descriptor namespace."
            )
        return resolved

    @staticmethod
    def _map_status(*, payload: ImdbPayload, kind: EntityType) -> TitleStatus:
        """Map IMDB year fields into the shared title lifecycle enum."""
        current_year = date.today().year
        if payload.start_year is not None and payload.start_year > current_year:
            return TitleStatus.UPCOMING
        if kind == EntityType.MOVIE:
            return (
                TitleStatus.FINISHED
                if payload.start_year is not None
                else TitleStatus.UNKNOWN
            )
        if payload.end_year is not None:
            return TitleStatus.FINISHED
        if payload.type_value == "tvMiniSeries" and payload.start_year is not None:
            return TitleStatus.FINISHED
        if payload.start_year is not None:
            return TitleStatus.ONGOING
        return TitleStatus.UNKNOWN

    @staticmethod
    def _map_scope_status(
        *,
        season_number: int,
        last_regular_season: int | None,
        show_status: TitleStatus,
    ) -> TitleStatus:
        """Map a derived season scope to the best available lifecycle status."""
        if season_number == 0:
            return (
                TitleStatus.FINISHED
                if show_status in (TitleStatus.FINISHED, TitleStatus.CANCELLED)
                else TitleStatus.UNKNOWN
            )
        if last_regular_season is None:
            return show_status
        if season_number < last_regular_season:
            return TitleStatus.FINISHED
        return show_status

    async def iter_all_normalized(
        self,
    ) -> AsyncGenerator[tuple[str, UnifiedMetadata]]:
        """Yield (descriptor_key, normalized) for every anime IMDB title in Wikidata.

        Enumerates anime IMDB IDs from the QLever Wikidata endpoint, then
        fetches each batch of `BATCH_SIZE` IDs in a single QLever IMDB SPARQL
        query (main titles and seasons combined).
        """
        imdb_ids = await self._enumerate_imdb_ids_from_wikidata()
        if not imdb_ids:
            LOGGER.warning("IMDB batch: no anime IMDB IDs found in Wikidata.")
            return

        id_list = sorted(imdb_ids)
        LOGGER.info("IMDB batch: refreshing %d titles.", len(id_list))
        for i in range(0, len(id_list), self.BATCH_SIZE):
            batch = id_list[i : i + self.BATCH_SIZE]
            async for item in self._fetch_batch_normalized(batch):
                yield item

    async def _fetch_batch_normalized(
        self,
        provider_ids: list[str],
    ) -> AsyncGenerator[tuple[str, UnifiedMetadata]]:
        """Fetch and normalize a batch of IMDB title IDs via QLever."""
        uri_list = " ".join(f"<{self._title_url(pid)}>" for pid in provider_ids)

        try:
            raw = await self.http_client.get_json(
                self.BASE_URL,
                params={"query": self.BATCH_TITLE_QUERY.format(uri_list=uri_list)},
            )
        except HttpClientError as exc:
            LOGGER.error("IMDB batch: HTTP error fetching title batch: %s", exc)
            return

        try:
            response = ImdbSparqlResponse.model_validate(raw)
        except ValidationError as exc:
            LOGGER.error("IMDB batch: response validation error: %s", exc)
            return

        payloads: dict[str, ImdbPayload] = {}
        for binding in response.results.bindings:
            try:
                payload = ImdbPayload.from_binding(binding)
                payloads[payload.canonical_id] = payload
            except (UpstreamResponseError, Exception) as exc:
                LOGGER.warning("IMDB batch: skipping binding: %s", exc)

        # Batch-fetch season data for all shows in this batch.
        show_ids = [
            pid
            for pid in provider_ids
            if pid in payloads and payloads[pid].type_value in self.SHOW_TYPES
        ]
        if show_ids:
            seasons_by_id = await self._fetch_batch_seasons(show_ids)
            for pid, seasons in seasons_by_id.items():
                if pid in payloads:
                    payloads[pid].seasons = seasons

        for provider_id in provider_ids:
            payload = payloads.get(provider_id)
            if payload is None:
                continue

            if payload.type_value in self.MOVIE_TYPES:
                provider_enum = DescriptorProvider.IMDB_MOVIE
            elif payload.type_value in self.SHOW_TYPES:
                provider_enum = DescriptorProvider.IMDB_SHOW
            else:
                continue  # unknown or unsupported type

            descriptor = MetadataDescriptor(
                provider=provider_enum,
                provider_id=payload.canonical_id,
            )
            try:
                normalized = await self.normalize(
                    descriptor=descriptor, payload=payload
                )
            except (UpstreamResponseError, Exception) as exc:
                LOGGER.warning("IMDB batch: skipping %s: %s", provider_id, exc)
                continue
            yield descriptor.key, normalized

    async def _fetch_batch_seasons(
        self,
        provider_ids: list[str],
    ) -> dict[str, list[ImdbSeasonPayload]]:
        """Fetch season aggregates for a batch of IMDB show IDs via QLever."""
        uri_list = " ".join(f"<{self._title_url(pid)}>" for pid in provider_ids)
        try:
            raw = await self.http_client.get_json(
                self.BASE_URL,
                params={"query": self.BATCH_SEASON_QUERY.format(uri_list=uri_list)},
            )
        except HttpClientError as exc:
            LOGGER.error("IMDB batch seasons: HTTP error: %s", exc)
            return {}

        try:
            response = ImdbSparqlResponse.model_validate(raw)
        except ValidationError:
            return {}

        result: dict[str, list[ImdbSeasonPayload]] = {}
        for binding in response.results.bindings:
            try:
                season = ImdbSeasonPayload.from_binding(binding)
                parent_id = ImdbPayload._text(binding, "parentId")
                if parent_id:
                    result.setdefault(parent_id, []).append(season)
            except UpstreamResponseError, Exception:
                continue
        return result

    async def _enumerate_imdb_ids_from_wikidata(self) -> set[str]:
        """Load anime IMDB IDs from the QLever Wikidata endpoint."""
        try:
            raw = await self.http_client.get_json(
                self.WIKIDATA_URL,
                params={"query": self.WIKIDATA_IMDB_IDS_QUERY, "format": "json"},
            )
        except HttpClientError as exc:
            LOGGER.error("IMDB batch: HTTP error fetching Wikidata IDs: %s", exc)
            return set()

        try:
            response = ImdbSparqlResponse.model_validate(raw)
        except ValidationError as exc:
            LOGGER.error("IMDB batch: Wikidata response validation error: %s", exc)
            return set()

        imdb_ids: set[str] = set()
        for binding in response.results.bindings:
            imdb_id = self._extract_imdb_id(ImdbPayload._text(binding, "imdbId") or "")
            if imdb_id is not None:
                imdb_ids.add(imdb_id)
        return imdb_ids

    @staticmethod
    def _extract_imdb_id(value: str) -> str | None:
        """Extract a canonical IMDB title ID from a string."""
        match = re.search(r"tt\d+", value)
        if match is None:
            return None
        return match.group(0)
