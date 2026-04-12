"""AniDB provider adapter backed by the AnimeAggregations repository."""

import asyncio
import logging
import re
import shutil
from collections.abc import AsyncGenerator
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    field_validator,
)

from anibridge_metadata.core.descriptors import MetadataDescriptor
from anibridge_metadata.core.enums import DescriptorProvider, EntityType, TitleStatus
from anibridge_metadata.models.metadata import (
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
    BatchableProvider,
    ProviderAdapter,
    ProviderConfigurationError,
    ProviderPayload,
    UpstreamNotFoundError,
    UpstreamResponseError,
)

logger = logging.getLogger(__name__)


class AniDbTitlePayload(BaseModel):
    """AniDB title entry from AnimeAggregations."""

    model_config = ConfigDict(extra="ignore")

    language: str | None = None
    title: str | None = None
    type: str | None = None


class AniDbTagPayload(BaseModel):
    """AniDB tag entry from AnimeAggregations."""

    model_config = ConfigDict(extra="ignore")

    info_box: bool | None = Field(default=None, alias="info_box")
    name: str | None = None
    spoiler: bool | None = None
    weight: int | None = None


class AniDbRatingPayload(BaseModel):
    """AniDB rating payload from AnimeAggregations."""

    model_config = ConfigDict(extra="ignore")

    rating: float | None = None
    total: int | None = None


class AniDbEpisodePayload(BaseModel):
    """AniDB episode payload from AnimeAggregations."""

    model_config = ConfigDict(extra="ignore")

    air_date: str | None = None
    length: int | None = None
    number: int | None = None


class AniDbEpisodesPayload(RootModel[dict[str, list[AniDbEpisodePayload]]]):
    """AniDB episode collections keyed by category."""

    root: dict[str, list[AniDbEpisodePayload]] = Field(default_factory=dict)

    def for_type(self, episode_type: str) -> list[AniDbEpisodePayload]:
        """Return the episodes for a specific AniDB episode category."""
        return self.root.get(episode_type, [])

    @property
    def regular(self) -> list[AniDbEpisodePayload]:
        """Return regular episodes from the collection."""
        return self.for_type("REGULAR")

    @property
    def items(self) -> tuple[tuple[str, list[AniDbEpisodePayload]], ...]:
        """Return episode collections keyed by their AniDB category name."""
        return tuple(self.root.items())


class AniDbRatingsPayload(BaseModel):
    """AniDB ratings grouped by source category."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    permanent: AniDbRatingPayload | None = Field(default=None, alias="PERMANENT")
    temporary: AniDbRatingPayload | None = Field(default=None, alias="TEMPORARY")
    review: AniDbRatingPayload | None = Field(default=None, alias="REVIEW")

    @property
    def ordered(self) -> tuple[AniDbRatingPayload, ...]:
        """Return ratings in descending preference order."""
        return tuple(
            rating
            for rating in (self.permanent, self.temporary, self.review)
            if rating is not None
        )


class AniDbResourcesPayload(BaseModel):
    """AniDB external resources grouped by provider."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    anilist: list[str] = Field(default_factory=list, alias="ANILIST")
    imdb: list[str] = Field(default_factory=list, alias="IMDB")
    mal: list[str] = Field(default_factory=list, alias="MAL")
    tmdb: list[str] = Field(default_factory=list, alias="TMDB")
    tvdb: list[str] = Field(default_factory=list, alias="TVDB")

    @property
    def items(self) -> tuple[tuple[str, list[str]], ...]:
        """Return resource collections keyed by their upstream provider name."""
        return (
            ("ANILIST", self.anilist),
            ("IMDB", self.imdb),
            ("MAL", self.mal),
            ("TMDB", self.tmdb),
            ("TVDB", self.tvdb),
        )


class AniDbPayload(BaseModel):
    """Validated AniDB metadata payload from AnimeAggregations."""

    model_config = ConfigDict(extra="ignore")

    anime_id: int
    description: str | None = None
    end_date: date | None = None
    episodes: AniDbEpisodesPayload = Field(default_factory=AniDbEpisodesPayload)
    ratings: AniDbRatingsPayload = Field(default_factory=AniDbRatingsPayload)
    resources: AniDbResourcesPayload = Field(default_factory=AniDbResourcesPayload)
    start_date: date | None = None
    tags: dict[str, AniDbTagPayload] = Field(default_factory=dict)
    titles: list[AniDbTitlePayload] = Field(default_factory=list)
    type: str | None = None
    url: str | None = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _parse_partial_dates(cls, value: str | None) -> date | None:
        """Parse AniDB dates that may be partial year or year-month values."""
        if value in (None, ""):
            return None
        cleaned = value.strip()
        try:
            return date.fromisoformat(cleaned)
        except ValueError:
            pass

        parts = cleaned.split("-")
        try:
            if len(parts) == 2:
                return date(int(parts[0]), int(parts[1]), 1)
            if len(parts) == 1:
                return date(int(parts[0]), 1, 1)
        except ValueError:
            return None
        return None

    @property
    def regular_episodes(self) -> list[AniDbEpisodePayload]:
        """Return regular episodes from the parsed episode collections."""
        return self.episodes.regular

    @property
    def genre_names(self) -> list[str]:
        """Return weighted, non-spoiler AniDB tags as genre candidates."""
        return [
            tag.name
            for tag in self.tags.values()
            if tag.name and (tag.weight or 0) > 0 and tag.spoiler is not True
        ]

    @property
    def average_rating(self) -> float | None:
        """Return the preferred AniDB average rating."""
        for rating in self.ratings.ordered:
            if rating.rating is not None:
                return rating.rating
        return None

    @property
    def rating_popularity(self) -> float | None:
        """Return the AniDB rating count as a popularity proxy."""
        for rating in self.ratings.ordered:
            if rating.total is not None:
                return float(rating.total)
        return None

    @property
    def episode_count(self) -> int | None:
        """Return the count of regular episodes when available."""
        return len(self.regular_episodes) or None

    @property
    def runtime_minutes(self) -> int | None:
        """Return the average length of regular episodes."""
        lengths = [
            episode.length
            for episode in self.regular_episodes
            if episode.length and episode.length > 0
        ]
        if not lengths:
            return None
        return round(sum(lengths) / len(lengths))


class AniDbAdapter(ProviderAdapter, BatchableProvider):
    """Retrieve and normalize AniDB metadata from a local AnimeAggregations clone."""

    REPOSITORY_URL: ClassVar[str] = "https://github.com/notseteve/AnimeAggregations.git"
    REPOSITORY_BRANCH: ClassVar[str] = "main"
    REPOSITORY_PATH: ClassVar[Path] = (
        Path(__file__).resolve().parents[4] / "data" / "anime-aggregations"
    )

    EPISODE_TYPE_BY_SCOPE: ClassVar[dict[str, str]] = {
        "R": "REGULAR",
        "S": "SPECIAL",
        "C": "CREDITS",
        "T": "TRAILER",
        "P": "PARODY",
        "O": "OTHER",
    }
    SCOPE_BY_EPISODE_TYPE: ClassVar[dict[str, str]] = {
        episode_type: scope for scope, episode_type in EPISODE_TYPE_BY_SCOPE.items()
    }

    def __init__(self, **kwargs) -> None:
        """Create the adapter and initialize repository sync state."""
        super().__init__(**kwargs)
        self._repository_ready = False
        self._sync_lock = asyncio.Lock()
        self._sync_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Ensure the local AniDB snapshot exists and schedule daily refreshes."""
        await self._sync_repository()
        if self._sync_task is None:
            self._sync_task = asyncio.create_task(
                self._scheduled_sync_loop(),
                name="anidb-daily-sync",
            )

    async def close(self) -> None:
        """Stop the scheduled AniDB repository refresh task."""
        if self._sync_task is None:
            return
        self._sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._sync_task
        self._sync_task = None

    async def iter_all_normalized(self) -> AsyncGenerator[tuple[str, UnifiedMetadata]]:
        """Yield (descriptor_key, normalized) for every anime in the local snapshot."""
        await self._ensure_repository_ready()
        anime_dir = self.REPOSITORY_PATH / "anime"
        paths = sorted(anime_dir.glob("*.json"))
        for path in paths:
            provider_id = path.stem
            descriptor = MetadataDescriptor(
                provider=DescriptorProvider.ANIDB,
                provider_id=provider_id,
            )
            try:
                payload = await self.fetch_raw(descriptor=descriptor)
                normalized = await self.normalize(
                    descriptor=descriptor, payload=payload
                )
            except (
                UpstreamNotFoundError,
                UpstreamResponseError,
                ProviderConfigurationError,
            ):
                logger.warning("AniDB batch: skipping %s due to error", provider_id)
                continue
            yield descriptor.key, normalized

    async def fetch_raw(self, *, descriptor: MetadataDescriptor) -> ProviderPayload:
        """Fetch AniDB metadata from the local AnimeAggregations checkout."""
        await self._ensure_repository_ready()
        async with self._sync_lock:
            payload_path = self._payload_path(descriptor.provider_id)
            if not payload_path.is_file():
                raise UpstreamNotFoundError(
                    "AniDB did not find the requested title in AnimeAggregations."
                )

            try:
                raw_payload = payload_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise UpstreamResponseError(
                    f"AniDB payload could not be read: {exc}"
                ) from exc

        try:
            return AniDbPayload.model_validate_json(raw_payload)
        except ValidationError as exc:
            raise UpstreamResponseError("AniDB response validation failed.") from exc

    async def normalize(
        self,
        *,
        descriptor: MetadataDescriptor,
        payload: ProviderPayload,
    ) -> UnifiedMetadata:
        """Normalize AniDB metadata into the shared schema."""
        if not isinstance(payload, AniDbPayload):
            raise UpstreamResponseError("AniDB payload was not an object.")

        kind = self._map_entity_type(payload.type)
        display_title, main_title, original_title, alias_titles = (
            self._build_title_fields(payload)
        )
        start_date = payload.start_date
        end_date = payload.end_date

        return UnifiedMetadata(
            kind=kind,
            id=build_metadata_id(
                descriptor=descriptor.key,
                provider=DescriptorProvider.ANIDB,
                provider_id=descriptor.provider_id,
            ),
            titles=build_titles(
                display=display_title,
                main=main_title,
                original=original_title,
                aliases=alias_titles,
            ),
            synopsis=payload.description,
            release=build_release(
                start_date=start_date,
                end_date=end_date,
                status=self._map_status(
                    kind=kind,
                    start_date=start_date,
                    end_date=end_date,
                ),
            ),
            runtime=build_runtime(minutes=payload.runtime_minutes, basis="derived"),
            units=payload.episode_count,
            classification=build_classification(
                genres=self.dedupe(payload.genre_names)
            ),
            ratings=build_ratings(
                average=payload.average_rating,
                popularity=payload.rating_popularity,
            ),
            scopes=self._build_scopes(
                descriptor=descriptor,
                payload=payload,
                show_title=display_title,
                show_runtime=build_runtime(
                    minutes=payload.runtime_minutes,
                    basis="derived",
                ),
            ),
            relationships=self._build_twins(
                descriptor=descriptor,
                payload=payload,
                kind=kind,
            ),
            source=build_source(
                url=f"https://anidb.net/anime/{descriptor.provider_id}"
            ),
        )

    async def _ensure_repository_ready(self) -> None:
        """Synchronize the repository on first use when startup hooks were skipped."""
        if self._repository_ready:
            return
        await self._sync_repository()

    async def _scheduled_sync_loop(self) -> None:
        """Refresh the local AniDB snapshot every day at 02:00 UTC."""
        while True:
            await asyncio.sleep(self._seconds_until_next_sync())
            try:
                await self._sync_repository()
            except Exception:
                logger.exception(
                    "AniDB repository sync failed during scheduled refresh."
                )

    def _seconds_until_next_sync(self) -> float:
        """Return the seconds until the next 02:00 UTC sync window."""
        now = datetime.now(UTC)
        target = now.replace(hour=2, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return max((target - now).total_seconds(), 0.0)

    async def _sync_repository(self) -> None:
        """Force the local repository to match the latest upstream sparse checkout."""
        async with self._sync_lock:
            repo_path = self.REPOSITORY_PATH
            if repo_path.exists() and not (repo_path / ".git").is_dir():
                shutil.rmtree(repo_path)

            if not repo_path.exists():
                repo_path.parent.mkdir(parents=True, exist_ok=True)
                await self._run_git(
                    "clone",
                    "--depth",
                    "1",
                    "--filter=blob:none",
                    "--sparse",
                    "--branch",
                    self.REPOSITORY_BRANCH,
                    self.REPOSITORY_URL,
                    str(repo_path),
                )

            await self._run_git(
                "remote", "set-url", "origin", self.REPOSITORY_URL, cwd=repo_path
            )
            await self._run_git("sparse-checkout", "init", "--cone", cwd=repo_path)
            await self._run_git("sparse-checkout", "set", "anime", cwd=repo_path)
            await self._run_git(
                "fetch", "--depth", "1", "origin", self.REPOSITORY_BRANCH, cwd=repo_path
            )
            await self._run_git(
                "checkout",
                "-B",
                self.REPOSITORY_BRANCH,
                f"origin/{self.REPOSITORY_BRANCH}",
                cwd=repo_path,
            )
            await self._run_git(
                "reset", "--hard", f"origin/{self.REPOSITORY_BRANCH}", cwd=repo_path
            )
            await self._run_git("clean", "-fdx", cwd=repo_path)
            self._repository_ready = True

    async def _run_git(self, *args: str, cwd: Path | None = None) -> None:
        """Run a git command and raise a provider error on failure."""
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=None if cwd is None else str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ProviderConfigurationError(f"Unable to execute git: {exc}") from exc

        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            return

        output = (
            stderr.decode("utf-8", errors="ignore").strip()
            or stdout.decode("utf-8", errors="ignore").strip()
        )
        raise ProviderConfigurationError(
            f"AniDB repository sync failed for git {' '.join(args)}: {output}"
        )

    def _payload_path(self, provider_id: str) -> Path:
        """Return the JSON payload path for a specific AniDB ID."""
        return self.REPOSITORY_PATH / "anime" / f"{provider_id}.json"

    @staticmethod
    def _map_status(
        *, kind: EntityType, start_date: date | None, end_date: date | None
    ) -> TitleStatus:
        """Infer a coarse status from AniDB dates and entity type."""
        if start_date is None:
            return TitleStatus.UNKNOWN
        if kind == EntityType.MOVIE:
            return TitleStatus.FINISHED
        if end_date is None:
            return TitleStatus.ONGOING
        return TitleStatus.FINISHED

    @staticmethod
    def _map_entity_type(value: str | None) -> EntityType:
        """Map AniDB title types into the shared entity model."""
        if value and value.strip().upper() == "MOVIE":
            return EntityType.MOVIE
        return EntityType.SHOW

    def _build_title_fields(
        self,
        payload: AniDbPayload,
    ) -> tuple[str, str, str | None, list[str]]:
        """Select display, main, original, and alias titles from AniDB entries."""
        candidates = [title for title in payload.titles if title.title]
        if not candidates:
            raise UpstreamResponseError("AniDB response did not contain any titles.")

        display = (
            self._select_title(
                candidates,
                languages=("ENGLISH",),
                types=("OFFICIAL", "MAIN", "SYNONYM", "SHORT"),
            )
            or self._select_title(
                candidates,
                languages=None,
                types=("OFFICIAL", "MAIN"),
            )
            or candidates[0].title
        )

        main = (
            self._select_title(
                candidates,
                languages=None,
                types=("MAIN",),
            )
            or display
        )

        original = self._select_title(
            candidates,
            languages=("JAPANESE",),
            types=("OFFICIAL", "MAIN", "KANA_READING"),
        ) or self._select_title(
            candidates,
            languages=("JAPANESE_TRANSLITERATED",),
            types=("MAIN", "OFFICIAL"),
        )

        aliases = self.dedupe(
            [
                title.title
                for title in candidates
                if title.title not in {display, main, original}
            ]
        )

        return (
            display or str(payload.anime_id),
            main or display or str(payload.anime_id),
            original,
            aliases,
        )

    @classmethod
    def _build_scopes(
        cls,
        *,
        descriptor: MetadataDescriptor,
        payload: AniDbPayload,
        show_title: str,
        show_runtime: MetadataRuntime | None,
    ) -> dict[str, MetadataScope] | None:
        """Build AniDB scope entries from episode type groups."""
        scopes: dict[str, MetadataScope] = {}

        for episode_type, episodes in payload.episodes.items:
            scope_key = cls.SCOPE_BY_EPISODE_TYPE.get(episode_type)
            if scope_key is None:
                continue

            start_date, end_date = cls._episode_date_bounds(episodes)
            runtime_minutes = cls._episode_runtime_minutes(episodes)
            units = len(episodes)
            if (
                start_date is None
                and end_date is None
                and runtime_minutes is None
                and units is None
            ):
                continue

            scopes[scope_key] = MetadataScope(
                id=build_metadata_id(
                    descriptor=(
                        f"{descriptor.provider.value}:{descriptor.provider_id}:{scope_key}"
                    ),
                    provider=descriptor.provider,
                    provider_id=descriptor.provider_id,
                    scope=scope_key,
                ),
                titles=build_titles(display=f"{show_title} {episode_type.title()}"),
                release=build_release(
                    start_date=start_date,
                    end_date=end_date,
                ),
                runtime=build_runtime(minutes=runtime_minutes, basis="derived")
                or show_runtime,
                units=units,
            )

        return scopes or None

    @staticmethod
    def _episode_date_bounds(
        episodes: list[AniDbEpisodePayload],
    ) -> tuple[date | None, date | None]:
        """Return the earliest and latest known air dates for an episode group."""
        dates = sorted(
            date.fromisoformat(episode.air_date)
            for episode in episodes
            if episode.air_date
        )
        if not dates:
            return None, None
        return dates[0], dates[-1]

    @staticmethod
    def _episode_runtime_minutes(episodes: list[AniDbEpisodePayload]) -> int | None:
        """Return the average runtime for an episode group."""
        lengths = [
            episode.length
            for episode in episodes
            if episode.length and episode.length > 0
        ]
        if not lengths:
            return None
        return round(sum(lengths) / len(lengths))

    @staticmethod
    def _select_title(
        titles: list[AniDbTitlePayload],
        *,
        languages: tuple[str, ...] | None,
        types: tuple[str, ...],
    ) -> str | None:
        """Return the first title that matches the preferred languages and types."""
        for title in titles:
            if not title.title:
                continue
            if languages is not None and title.language not in languages:
                continue
            if title.type not in types:
                continue
            return title.title
        return None

    @classmethod
    def _build_twins(
        cls,
        *,
        descriptor: MetadataDescriptor,
        payload: AniDbPayload,
        kind: EntityType,
    ) -> list[MetadataRelationship]:
        """Build cross-provider twin references from AnimeAggregations resources."""
        relationships: list[MetadataRelationship] = []
        seen: set[str] = set()

        for provider_name, values in payload.resources.items:
            for value in values:
                target_descriptor = cls._resource_descriptor(
                    provider_name=provider_name,
                    value=value,
                    kind=kind,
                )
                if target_descriptor is None:
                    continue
                if target_descriptor == descriptor.key or target_descriptor in seen:
                    continue
                seen.add(target_descriptor)
                target_kind = (
                    EntityType.MOVIE if "_movie:" in target_descriptor else kind
                )
                relationships.append(
                    build_relationship(
                        descriptor=target_descriptor,
                        kind=target_kind,
                    )
                )

        return relationships

    @classmethod
    def _resource_descriptor(
        cls,
        *,
        provider_name: str,
        value: str,
        kind: EntityType,
    ) -> str | None:
        """Map an AnimeAggregations resource entry to a supported descriptor."""
        cleaned = value.strip()
        normalized_provider = provider_name.strip().upper()

        if normalized_provider == "ANILIST" and cleaned.isdigit():
            return f"{DescriptorProvider.ANILIST}:{cleaned}"

        if normalized_provider == "MAL" and cleaned.isdigit():
            return f"{DescriptorProvider.MAL}:{cleaned}"

        if normalized_provider == "IMDB":
            imdb_id = cls._extract_imdb_id(cleaned)
            if imdb_id is None:
                return None
            provider = (
                DescriptorProvider.IMDB_MOVIE
                if kind == EntityType.MOVIE
                else DescriptorProvider.IMDB_SHOW
            )
            return f"{provider}:{imdb_id}"

        if normalized_provider == "TMDB":
            match = re.fullmatch(r"(?P<scope>movie|tv)/(?P<id>\d+)", cleaned)
            if match is not None:
                provider = (
                    DescriptorProvider.TMDB_MOVIE
                    if match.group("scope") == "movie"
                    else DescriptorProvider.TMDB_SHOW
                )
                return f"{provider}:{match.group('id')}"
            if cleaned.isdigit():
                provider = (
                    DescriptorProvider.TMDB_MOVIE
                    if kind == EntityType.MOVIE
                    else DescriptorProvider.TMDB_SHOW
                )
                return f"{provider}:{cleaned}"

        if normalized_provider == "TVDB" and cleaned.isdigit():
            provider = (
                DescriptorProvider.TVDB_MOVIE
                if kind == EntityType.MOVIE
                else DescriptorProvider.TVDB_SHOW
            )
            return f"{provider}:{cleaned}"

        return None

    @staticmethod
    def _extract_imdb_id(value: str) -> str | None:
        """Extract a canonical IMDB title id."""
        match = re.search(r"tt\d+", value)
        if match is None:
            return None
        return match.group(0)
