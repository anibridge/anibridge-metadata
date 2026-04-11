"""Application configuration models."""

from datetime import timedelta
from importlib.metadata import version

from anibridge.utils.cache import cache
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RateLimiterConfig(BaseModel):
    """Configuration for provider rate limiter."""

    rate: float = Field(default=..., gt=0, description="Tokens added per second")
    capacity: int = Field(
        default=1, gt=0, description="Maximum number of tokens in the bucket"
    )


class ProviderConfig(BaseModel):
    """Base configuration for a metadata provider."""

    enabled: bool = True
    rate_limiter: RateLimiterConfig | None = None


class AniDbConfig(ProviderConfig):
    """Configuration for AniDB provider integration."""

    client: str | None = None
    client_version: str | None = None
    rate_limiter: RateLimiterConfig | None = Field(
        default_factory=lambda: RateLimiterConfig(rate=0.5, capacity=1)
    )


class AnilistConfig(ProviderConfig):
    """Configuration for Anilist provider integration."""

    rate_limiter: RateLimiterConfig | None = Field(
        default_factory=lambda: RateLimiterConfig(rate=0.5, capacity=4)
    )


class MalConfig(ProviderConfig):
    """Configuration for MyAnimeList provider integration."""

    client_id: str | None = None
    rate_limiter: RateLimiterConfig | None = Field(
        default_factory=lambda: RateLimiterConfig(rate=1, capacity=1)
    )


class ImdbConfig(ProviderConfig):
    """Configuration for IMDB provider integration."""

    pass


class TvdbConfig(ProviderConfig):
    """Configuration for TVDB provider integration."""

    api_key: str | None = None
    pin: str | None = None


class TmdbConfig(ProviderConfig):
    """Configuration for TMDB provider integration."""

    access_token: str | None = None


class BatchRefreshConfig(BaseModel):
    """Configuration for the scheduled full-catalog batch refresh."""

    enabled: bool = False
    cron: str = Field(
        default="0 3 * * *",
        description="Cron expression (UTC) controlling when to run the batch refresh.",
    )
    refresh_on_startup: bool = Field(
        default=False,
        description="Run a full batch refresh immediately when the service starts.",
    )


class Settings(BaseSettings):
    """Runtime configuration for the metadata service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        env_parse_none_str="null",
        env_prefix="ABM_",
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./data/anibridge_metadata.db"
    sql_echo: bool = False
    cache_ttl_seconds: int = Field(default=21600, ge=1)
    request_timeout_seconds: float = Field(default=15.0, gt=0)
    user_agent: str = Field(
        default_factory=lambda: f"anibridge-metadata/{version('anibridge-metadata')}"
    )

    anidb: AniDbConfig = Field(default_factory=AniDbConfig)
    anilist: AnilistConfig = Field(default_factory=AnilistConfig)
    mal: MalConfig = Field(default_factory=MalConfig)
    imdb: ImdbConfig = Field(default_factory=ImdbConfig)
    tvdb: TvdbConfig = Field(default_factory=TvdbConfig)
    tmdb: TmdbConfig = Field(default_factory=TmdbConfig)
    batch_refresh: BatchRefreshConfig = Field(default_factory=BatchRefreshConfig)

    @property
    def cache_ttl(self) -> timedelta:
        """Return the global cache TTL as a timedelta."""
        return timedelta(seconds=self.cache_ttl_seconds)


@cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
